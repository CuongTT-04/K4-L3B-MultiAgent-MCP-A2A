"""Payment / refund / policy specialist.

Observed on the L3B gateway (probe of cases 002-009):

* `get_payment_timeline` already returns `payments` + `events`, so `get_order_payments` is
  only a fallback. `get_refund_timeline` errors when the order has no refund records.
* Every timeline mixes the order's real lifecycle with a distractor scenario that matches
  the customer's claim topic. The real lifecycle is anchored on the order purchase day;
  refunds and reconciliation events belong to the capture they follow.
* `get_policy` returns one rule per issue (`case_status`, `recommended_action`,
  `refund_brl`, `responsible_parties`); refund amounts come from there, not from arithmetic.

All decisions are deterministic: money is never produced by an LLM.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

ACTOR_PAYMENT = "payment-agent"
ACTOR_POLICY = "policy-agent"

TOOL_ORDER_PAYMENTS = "get_order_payments"
TOOL_PAYMENT_TIMELINE = "get_payment_timeline"
TOOL_REFUND_TIMELINE = "get_refund_timeline"
TOOL_POLICY = "get_policy"

TOLERANCE_BRL = 0.01
MCP_ERRORS = (RuntimeError, ValueError, OSError, TimeoutError)

# Priority when the evidence supports several payment issues and no claim picks one.
PAYMENT_ISSUES = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "refund_failed",
    "refund_pending",
    "payment_mismatch",
    "duplicate_charge",
    "valid_split_payment",
)
VERDICT_BY_ISSUE = {
    "refund_failed": "refund_failed",
    "refund_pending": "refund_pending",
    "payment_mismatch": "capture_mismatch",
    "duplicate_charge": "duplicate_capture",
}

_FAILED_WORDS = ("fail", "declin", "reject", "error", "denied")
_DONE_WORDS = ("complet", "succe", "refunded", "processed", "settled", "confirm", "done", "paid")
_PENDING_WORDS = ("pend", "request", "processing", "initiat", "submitted", "queued", "open")


class Caller(Protocol):
    def __call__(
        self, tool_name: str, *, case_id: str, **arguments: str
    ) -> Awaitable[dict[str, Any]]: ...


class TraceSink(Protocol):
    def emit(self, **kwargs: Any) -> Any: ...


# --------------------------------------------------------------------------- parsing


def _money(value: float) -> float:
    return round(value, 2)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class Event:
    kind: str
    status: str
    amount: float | None
    at: datetime | None
    index: int

    @property
    def day(self) -> date | None:
        return self.at.date() if self.at else None

    @property
    def phase(self) -> str:
        text = self.status or self.kind
        if any(word in text for word in _FAILED_WORDS):
            return "failed"
        if any(word in text for word in _DONE_WORDS):
            return "done"
        if any(word in text for word in _PENDING_WORDS):
            return "pending"
        return "unknown"

    @property
    def sort_key(self) -> tuple[str, int]:
        return (self.at.isoformat() if self.at else "", self.index)


def _event_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, Mapping):
        data = data.get("events", data.get("refunds", []))
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def parse_events(data: Any) -> list[Event]:
    events = []
    for index, row in enumerate(_event_rows(data)):
        events.append(
            Event(
                kind=str(row.get("event_type") or row.get("type") or "").lower(),
                status=str(row.get("status") or "").lower(),
                amount=_number(row.get("amount_brl", row.get("amount"))),
                at=_when(row.get("event_at") or row.get("occurred_at")),
                index=index,
            )
        )
    return sorted(events, key=lambda event: event.sort_key)


def parse_payment_rows(data: Any) -> list[dict[str, Any]]:
    rows = data.get("payments", []) if isinstance(data, Mapping) else data
    if not isinstance(rows, list):
        return []
    unique: dict[tuple[str, str, float | None], dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict):
            key = (
                str(row.get("payment_sequential")),
                str(row.get("payment_type")),
                _number(row.get("payment_value")),
            )
            unique.setdefault(key, row)
    return list(unique.values())


# --------------------------------------------------------------------------- authoritative view


@dataclass(frozen=True)
class PaymentView:
    """Events of the order's own lifecycle, separated from distractor records."""

    captures: tuple[Event, ...] = ()
    mismatches: tuple[Event, ...] = ()
    refunds: tuple[Event, ...] = ()
    ignored_events: int = 0
    anchored: bool = False
    payment_types: tuple[str, ...] = ()


def _anchor_days(order: Mapping[str, Any] | None) -> list[date]:
    if not order:
        return []
    days = []
    for key in ("order_purchase_timestamp", "order_approved_at"):
        moment = _when(order.get(key))
        if moment:
            days.append(moment.date())
    return days


def build_view(
    payment_timeline: Any, refund_timeline: Any, order: Mapping[str, Any] | None = None
) -> PaymentView:
    events = parse_events(payment_timeline)
    captures = [e for e in events if "captur" in e.kind and e.phase not in ("failed", "pending")]
    anchors = _anchor_days(order)
    capture_days = sorted({e.day for e in captures if e.day})
    if anchors and capture_days:
        purchase = anchors[0]
        real_day = min(capture_days, key=lambda day: (abs((day - purchase).days), day))
        real = [e for e in captures if e.day == real_day]
    else:
        real = captures
    real_ids = {e.index for e in real}

    def owner(event: Event) -> Event | None:
        before = [c for c in captures if c.at is None or event.at is None or c.at <= event.at]
        same_amount = [
            c for c in before
            if c.amount is not None and event.amount is not None
            and abs(c.amount - event.amount) <= TOLERANCE_BRL
        ]
        pool = same_amount or before
        return pool[-1] if pool else None

    def belongs(event: Event) -> bool:
        if not anchors:
            return True
        capture = owner(event)
        return capture is not None and capture.index in real_ids

    mismatches = [e for e in events if "mismatch" in e.kind and e.phase == "pending"]
    refunds = parse_events(refund_timeline)
    kept_mismatches = [e for e in mismatches if belongs(e)]
    kept_refunds = [e for e in refunds if belongs(e)]
    ignored = (
        len(captures) - len(real)
        + len(mismatches) - len(kept_mismatches)
        + len(refunds) - len(kept_refunds)
    )
    real_amounts = {e.amount for e in real}
    types = sorted(
        {
            str(row.get("payment_type"))
            for row in parse_payment_rows(payment_timeline)
            if _number(row.get("payment_value")) in real_amounts
        }
    )
    return PaymentView(
        captures=tuple(real),
        mismatches=tuple(kept_mismatches),
        refunds=tuple(kept_refunds),
        ignored_events=ignored,
        anchored=bool(anchors),
        payment_types=tuple(types),
    )


# --------------------------------------------------------------------------- facts


@dataclass(frozen=True)
class PaymentFacts:
    """Numbers of the authoritative lifecycle. `None` means the source is missing."""

    captured_total_brl: float | None = None
    refunded_total_brl: float | None = None
    refund_pending_brl: float = 0.0
    refund_failed_brl: float = 0.0
    open_mismatch_brl: float = 0.0
    repeated_capture_brl: float = 0.0
    payment_types: tuple[str, ...] = ()


def _refund_states(refunds: Sequence[Event]) -> tuple[float, float, float]:
    groups: dict[float | None, list[Event]] = defaultdict(list)
    for event in refunds:
        groups[event.amount].append(event)
    done = pending = failed = 0.0
    for amount, events in groups.items():
        phase = events[-1].phase
        value = amount or 0.0
        if phase == "done":
            done += value
        elif phase == "failed":
            failed += value
        else:
            pending += value
    return _money(done), _money(pending), _money(failed)


def build_facts(view: PaymentView | None, refund_available: bool = True) -> PaymentFacts:
    if view is None:
        return PaymentFacts()
    done, pending, failed = _refund_states(view.refunds)
    counts = Counter(e.amount for e in view.captures if e.amount is not None)
    repeated = sum(amount * (count - 1) for amount, count in counts.items() if count > 1)
    return PaymentFacts(
        captured_total_brl=_money(sum(e.amount or 0.0 for e in view.captures)),
        refunded_total_brl=done if refund_available or view.refunds else 0.0,
        refund_pending_brl=pending,
        refund_failed_brl=failed,
        open_mismatch_brl=_money(sum(e.amount or 0.0 for e in view.mismatches)),
        repeated_capture_brl=_money(repeated),
        payment_types=view.payment_types,
    )


# --------------------------------------------------------------------------- decisions


def policy_rules(policy: Any) -> dict[str, dict[str, Any]]:
    rules = policy.get("rules") if isinstance(policy, Mapping) else None
    if not isinstance(rules, dict):
        return {}
    return {issue: rule for issue, rule in rules.items() if isinstance(rule, dict)}


def _is_duplicate(
    facts: PaymentFacts, order_total_brl: float | None, rules: Mapping[str, Any]
) -> bool:
    """Two equal captures on one day are a split payment or a duplicate charge."""
    captured = facts.captured_total_brl or 0.0
    if order_total_brl is not None:
        return captured > order_total_brl + TOLERANCE_BRL
    duplicate_refund = _number(rules.get("duplicate_charge", {}).get("refund_brl"))
    if duplicate_refund is not None:
        return abs(facts.repeated_capture_brl - duplicate_refund) <= TOLERANCE_BRL
    return len(facts.payment_types) <= 1


def detect_payment_issues(
    facts: PaymentFacts,
    *,
    order_status: str | None = None,
    order_total_brl: float | None = None,
    policy: Any = None,
) -> list[str]:
    """Payment issues supported by the authoritative lifecycle, in priority order."""
    if facts.captured_total_brl is None:
        return []
    found: set[str] = set()
    status = (order_status or "").lower()
    unrefunded = facts.captured_total_brl - (facts.refunded_total_brl or 0.0)
    if unrefunded > TOLERANCE_BRL and status in ("canceled", "cancelled"):
        found.add("canceled_order_paid")
    if unrefunded > TOLERANCE_BRL and status == "unavailable":
        found.add("unavailable_order_paid")
    if facts.refund_failed_brl > TOLERANCE_BRL:
        found.add("refund_failed")
    if facts.refund_pending_brl > TOLERANCE_BRL:
        found.add("refund_pending")
    if facts.open_mismatch_brl > TOLERANCE_BRL:
        found.add("payment_mismatch")
    if facts.repeated_capture_brl > TOLERANCE_BRL:
        rules = policy_rules(policy)
        duplicate = _is_duplicate(facts, order_total_brl, rules)
        found.add("duplicate_charge" if duplicate else "valid_split_payment")
    return [issue for issue in PAYMENT_ISSUES if issue in found]


def _fallback_refund(issue: str | None, facts: PaymentFacts) -> float:
    refundable = max((facts.captured_total_brl or 0.0) - (facts.refunded_total_brl or 0.0), 0.0)
    return {
        "refund_failed": facts.refund_failed_brl,
        "payment_mismatch": facts.open_mismatch_brl,
        "duplicate_charge": facts.repeated_capture_brl,
        "canceled_order_paid": refundable,
        "unavailable_order_paid": refundable,
    }.get(issue or "", 0.0)


@dataclass
class PaymentDecision:
    """Payment fields plus the policy recommendation for one issue."""

    issue: str | None
    payment_analysis: dict[str, Any]
    financial_resolution: dict[str, Any]
    case_status: str
    resolution_actions: list[str]
    responsible_parties: list[dict[str, Any]]
    policy_applied: bool

    def output_fragment(self) -> dict[str, Any]:
        return {
            "payment_analysis": self.payment_analysis,
            "financial_resolution": self.financial_resolution,
        }


@dataclass
class PaymentFindings:
    """Everything the payment specialist observed; `decide` is cheap and call-free."""

    order_id: str
    facts: PaymentFacts
    view: PaymentView | None
    issues: list[str]
    policy: Any = None
    evidence_by_tool: dict[str, str] = field(default_factory=dict)
    missing_tools: list[str] = field(default_factory=list)

    @property
    def evidence_refs(self) -> list[str]:
        return list(dict.fromkeys(self.evidence_by_tool.values()))

    @property
    def suggested_issue(self) -> str | None:
        """Highest-priority payment issue; the coordinator owns the case's primary issue."""
        return self.issues[0] if self.issues else None

    def decide(self, issue: str | None = None) -> PaymentDecision:
        """Apply the policy rule of `issue` (any issue code, chosen by the coordinator)."""
        primary = issue or self.suggested_issue
        facts = self.facts
        rule = policy_rules(self.policy).get(primary or "")

        if facts.captured_total_brl is None:
            verdict = "insufficient_evidence"
        elif primary in VERDICT_BY_ISSUE:
            verdict = VERDICT_BY_ISSUE[primary]
        elif (facts.refunded_total_brl or 0.0) > TOLERANCE_BRL:
            verdict = "refunded"
        else:
            verdict = "reconciled"

        refundable = None
        if facts.captured_total_brl is not None and facts.refunded_total_brl is not None:
            refundable = _money(max(facts.captured_total_brl - facts.refunded_total_brl, 0.0))

        if rule is not None:
            amount = _number(rule.get("refund_brl")) or 0.0
            status = str(rule.get("case_status") or "action_required")
            actions = [str(rule["recommended_action"])] if rule.get("recommended_action") else []
            parties = [dict(p) for p in rule.get("responsible_parties", []) if isinstance(p, dict)]
        else:
            amount = _fallback_refund(primary, facts)
            status = (
                "needs_investigation" if verdict == "insufficient_evidence" or primary is None
                else "action_required" if amount > TOLERANCE_BRL else "no_action"
            )
            actions, parties = [], []
        amount = _money(amount)
        lines = (
            [{"reason_code": primary, "amount_brl": amount, "entity_id": self.order_id}]
            if primary and amount > TOLERANCE_BRL else []
        )

        return PaymentDecision(
            issue=primary,
            payment_analysis={
                "verdict": verdict,
                "captured_total_brl": facts.captured_total_brl,
                "refunded_total_brl": facts.refunded_total_brl,
                "refundable_total_brl": refundable,
            },
            financial_resolution={
                "currency": "BRL",
                "recommended_refund_brl": amount if lines else 0.0,
                "refund_lines": lines,
            },
            case_status=status,
            resolution_actions=actions,
            responsible_parties=parties,
            policy_applied=rule is not None,
        )


def analyze(
    order_id: str,
    data_by_tool: Mapping[str, Any],
    *,
    order: Mapping[str, Any] | None = None,
    order_total_brl: float | None = None,
) -> PaymentFindings:
    """Pure step: MCP `data` payloads in, findings out."""
    timeline = data_by_tool.get(TOOL_PAYMENT_TIMELINE)
    if timeline is None and TOOL_ORDER_PAYMENTS in data_by_tool:
        # Payment rows without lifecycle events: each distinct row counts as one capture.
        rows = parse_payment_rows(data_by_tool[TOOL_ORDER_PAYMENTS])
        timeline = {
            "payments": rows,
            "events": [
                {"event_type": "captured", "status": "confirmed",
                 "amount_brl": row.get("payment_value")}
                for row in rows
            ],
        }
    view = None if timeline is None else build_view(
        timeline, data_by_tool.get(TOOL_REFUND_TIMELINE), order
    )
    facts = build_facts(view, refund_available=TOOL_REFUND_TIMELINE in data_by_tool)
    policy = data_by_tool.get(TOOL_POLICY)
    issues = detect_payment_issues(
        facts,
        order_status=(order or {}).get("order_status"),
        order_total_brl=order_total_brl,
        policy=policy,
    )
    return PaymentFindings(order_id, facts, view, issues, policy)


# --------------------------------------------------------------------------- agent


async def _consume(
    call: Caller, trace: TraceSink, case_id: str, order_id: str, tool_name: str,
    arguments: dict[str, str],
) -> tuple[Any, str] | None:
    try:
        envelope = await call(tool_name, case_id=case_id, **arguments)
    except MCP_ERRORS:
        return None
    evidence_ref = envelope.get("evidence_ref")
    if not isinstance(evidence_ref, str):
        return None
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=ACTOR_POLICY if tool_name == TOOL_POLICY else ACTOR_PAYMENT,
        tool_name=tool_name,
        evidence_refs=[evidence_ref],
        attributes={"domain": envelope.get("domain"), "order_id": order_id},
    )
    return envelope.get("data"), evidence_ref


async def investigate_payment(
    case: Mapping[str, Any],
    order_id: str,
    call: Caller,
    trace: TraceSink,
    *,
    order: Mapping[str, Any] | None = None,
    order_total_brl: float | None = None,
    issue: str | None = None,
) -> tuple[PaymentFindings, PaymentDecision]:
    """Payment/refund/policy specialist for one resolved order.

    Inputs from other agents: `order` is the `get_order` data row (purchase date anchors the
    lifecycle, `order_status` flags canceled/unavailable), `order_total_brl` the items total,
    `issue` the coordinator's primary issue. Call `findings.decide(issue)` again if the
    coordinator settles on a different issue later; it makes no MCP call.
    `call` is `gateway.call` or a cached wrapper with the same signature. Three calls per
    case; `get_order_payments` only when the payment timeline is unavailable.
    """
    case_id = case["case_id"]
    data: dict[str, Any] = {}
    evidence: dict[str, str] = {}
    missing: list[str] = []
    plan = [
        (TOOL_PAYMENT_TIMELINE, {"order_id": order_id}),
        (TOOL_REFUND_TIMELINE, {"order_id": order_id}),
        (TOOL_POLICY, {"policy_version": str(case.get("policy_version") or "")}),
    ]
    for tool_name, arguments in plan:
        if tool_name == TOOL_POLICY and not arguments["policy_version"]:
            missing.append(tool_name)
            continue
        consumed = await _consume(call, trace, case_id, order_id, tool_name, arguments)
        if consumed is None:
            missing.append(tool_name)
            if tool_name == TOOL_PAYMENT_TIMELINE:
                fallback = await _consume(
                    call, trace, case_id, order_id, TOOL_ORDER_PAYMENTS, {"order_id": order_id}
                )
                if fallback is not None:
                    data[TOOL_ORDER_PAYMENTS], evidence[TOOL_ORDER_PAYMENTS] = fallback
            continue
        data[tool_name], evidence[tool_name] = consumed

    findings = analyze(order_id, data, order=order, order_total_brl=order_total_brl)
    findings.evidence_by_tool = evidence
    findings.missing_tools = missing
    decision = findings.decide(issue)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor=ACTOR_POLICY,
        decision_code=(decision.resolution_actions or ["no_policy_rule"])[0],
        evidence_refs=findings.evidence_refs or None,
        attributes={
            "issue": decision.issue,
            "payment_verdict": decision.payment_analysis["verdict"],
            "recommended_refund_brl": decision.financial_resolution["recommended_refund_brl"],
            "ignored_events": findings.view.ignored_events if findings.view else 0,
        },
    )
    return findings, decision
