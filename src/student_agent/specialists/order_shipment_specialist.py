"""Order/Product/Shipment specialist (Người 2).

Owns: affected entities (order/item/seller/shipment ids), shipment verdict, timeline
completeness and seller/logistics responsibility. Payment references are left to the
payment specialist.

The MCP ``data`` payload has no published schema, so field lookup accepts the Olist
column names plus a few common aliases. Missing evidence never becomes a guess: it
downgrades the verdict to ``insufficient_evidence`` and ``timeline_complete=False``.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..evidence_store import EvidenceStore
from ..models import AgentTask, SpecialistResult, TaskStatus
from .base import BaseSpecialist

ALLOWED_TOOLS = frozenset(
    {"get_order", "get_order_items", "get_product_context", "get_sellers", "get_shipment_summary"}
)

# Olist timestamps are local Brazil time without offset.
_BRT = timezone(timedelta(hours=-3))

_PURCHASE = ("order_purchase_timestamp", "purchase_timestamp", "purchased_at")
_CARRIER = (
    "order_delivered_carrier_date",
    "delivered_carrier_date",
    "carrier_handoff_at",
    "handed_to_carrier_at",
    "carrier_date",
)
_DELIVERED = (
    "order_delivered_customer_date",
    "delivered_customer_date",
    "delivered_at",
    "delivery_date",
)
_ESTIMATED = (
    "order_estimated_delivery_date",
    "estimated_delivery_date",
    "promised_delivery_date",
    "estimated_at",
)
_TIMELINE_FIELDS = {
    "order_purchase_timestamp": _PURCHASE,
    "order_delivered_carrier_date": _CARRIER,
    "order_delivered_customer_date": _DELIVERED,
    "order_estimated_delivery_date": _ESTIMATED,
}
_SHIPPING_LIMIT = ("shipping_limit_date", "shipping_limit", "handoff_limit", "handoff_deadline")
_SELLER_ID = ("seller_id", "seller")
_EVENT_TYPE = ("event_type", "type", "status", "code", "event")

# Higher wins when merging several resolved orders into one case verdict.
_VERDICT_PRIORITY = {
    "insufficient_evidence": 0,
    "on_time": 1,
    "logistics_delay": 2,
    "seller_delay": 3,
    "returned": 4,
    "lost": 5,
    "conflicting": 6,
}
_CAUSE_CODES = {
    "seller_delay": "SELLER_LATE_HANDOFF",
    "logistics_delay": "LOGISTICS_TRANSIT_DELAY",
    "lost": "SHIPMENT_LOST",
    "returned": "SHIPMENT_RETURNED",
    "conflicting": "SHIPMENT_SOURCE_CONFLICT",
}
_DELIVERY_CLAIMS = {
    "late_delivery_seller": {"seller_delay"},
    "late_delivery_logistics": {"logistics_delay", "lost"},
}


# --------------------------------------------------------------------------- parsing


def _get(row: dict[str, Any] | None, names: tuple[str, ...]) -> Any:
    if not row:
        return None
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _records(data: Any, *keys: str) -> list[dict[str, Any]]:
    """Return a list of row dicts from a list, a wrapped list or a single object."""
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return [row for row in data[key] if isinstance(row, dict)]
        return [data]
    return []


def _single(data: Any, *keys: str) -> dict[str, Any]:
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), dict):
                return data[key]
        return data
    rows = _records(data)
    return rows[0] if rows else {}


def parse_ts(value: Any) -> datetime | None:
    """Parse ISO / Olist timestamps into naive Brazil-local datetimes."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_BRT).replace(tzinfo=None)
    return parsed


def _item_id(order_id: str, item: dict[str, Any]) -> str | None:
    explicit = _get(item, ("item_id", "order_item_uid"))
    if explicit is not None:
        return str(explicit)
    number = _get(item, ("order_item_id",))
    return f"{order_id}:{number}" if number is not None else None


# ------------------------------------------------------------------------- analysis


@dataclass
class OrderShipmentView:
    """Evidence for one order, already unwrapped from MCP envelopes."""

    order_id: str
    order: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    shipment: dict[str, Any] = field(default_factory=dict)
    sellers: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ShipmentAssessment:
    verdict: str
    late_seller_ids: list[str]
    timeline_complete: bool
    conflicts: list[dict[str, Any]]
    responsible_parties: list[dict[str, Any]]
    cause_codes: list[str]


def _shipment_events(shipment: dict[str, Any]) -> list[str]:
    rows = _records(shipment.get("events") or shipment.get("shipment_events") or [], "events")
    labels = []
    for row in rows:
        label = _get(row, _EVENT_TYPE)
        if label is not None:
            labels.append(str(label).lower())
    status = _get(shipment, ("status", "shipment_status"))
    if status is not None:
        labels.append(str(status).lower())
    return labels


def _seller_limits(view: OrderShipmentView) -> dict[str, datetime]:
    """Earliest carrier-handoff deadline per seller, from items and shipment summary."""
    limits: dict[str, datetime] = {}

    def add(seller: Any, raw: Any) -> None:
        when = parse_ts(raw)
        if seller is None or when is None:
            return
        seller = str(seller)
        if seller not in limits or when < limits[seller]:
            limits[seller] = when

    for item in view.items:
        add(_get(item, _SELLER_ID), _get(item, _SHIPPING_LIMIT))
    raw = view.shipment.get("seller_handoff_limits") or view.shipment.get("shipping_limits")
    if isinstance(raw, dict):
        for seller, when in raw.items():
            add(seller, when.get("shipping_limit_date") if isinstance(when, dict) else when)
    else:
        for row in _records(raw or []):
            add(_get(row, _SELLER_ID), _get(row, _SHIPPING_LIMIT))
    return limits


def _seller_handoffs(shipment: dict[str, Any]) -> dict[str, datetime]:
    """Per-seller actual carrier handoff, when the shipment summary provides it."""
    result: dict[str, datetime] = {}
    raw = shipment.get("seller_handoffs") or shipment.get("seller_handoff_limits") or []
    for row in _records(raw):
        seller = _get(row, _SELLER_ID)
        when = parse_ts(_get(row, _CARRIER + ("handed_off_at", "actual_handoff_at")))
        if seller is not None and when is not None:
            result[str(seller)] = when
    return result


def _timeline(
    view: OrderShipmentView, source_precedence: tuple[str, ...] | None
) -> tuple[dict[str, datetime | None], dict[str, dict[str, datetime | None]], list[str]]:
    """Merge timestamps from get_order and get_shipment_summary.

    Returns the selected timeline, each source's own timeline and the conflicting fields.
    """
    by_source: dict[str, dict[str, datetime | None]] = {
        "get_order": {},
        "get_shipment_summary": {},
    }
    for name, aliases in _TIMELINE_FIELDS.items():
        by_source["get_order"][name] = parse_ts(_get(view.order, aliases))
        by_source["get_shipment_summary"][name] = parse_ts(_get(view.shipment, aliases))
    order_first = not source_precedence or source_precedence[0] != "get_shipment_summary"
    primary, secondary = (
        ("get_order", "get_shipment_summary")
        if order_first
        else ("get_shipment_summary", "get_order")
    )
    selected: dict[str, datetime | None] = {}
    conflicts: list[str] = []
    for name in _TIMELINE_FIELDS:
        first, second = by_source[primary][name], by_source[secondary][name]
        selected[name] = first if first is not None else second
        if first is not None and second is not None and first != second:
            conflicts.append(name)
    return selected, by_source, conflicts


def _verdict_for(
    timeline: dict[str, datetime | None],
    view: OrderShipmentView,
    events: list[str],
    opened_at: datetime | None,
) -> tuple[str, list[str]]:
    if any("lost" in label for label in events):
        return "lost", []
    if any("return" in label for label in events):
        return "returned", []

    carrier = timeline["order_delivered_carrier_date"]
    delivered = timeline["order_delivered_customer_date"]
    estimated = timeline["order_estimated_delivery_date"]
    if estimated is None:
        return "insufficient_evidence", []

    if delivered is not None:
        late = delivered.date() > estimated.date()
    elif opened_at is not None and opened_at.date() > estimated.date():
        late = True  # still undelivered after the promised date
    else:
        return "insufficient_evidence", []
    if not late:
        return "on_time", []

    limits = _seller_limits(view)
    handoffs = _seller_handoffs(view.shipment)
    late_sellers: list[str] = []
    for seller, limit in sorted(limits.items()):
        reference = handoffs.get(seller, carrier)
        if reference is None and delivered is None:
            # Never handed to the carrier and already past the handoff limit.
            reference = opened_at
        if reference is not None and reference > limit:
            late_sellers.append(seller)
    if late_sellers:
        return "seller_delay", late_sellers
    if carrier is None and not handoffs:
        return "insufficient_evidence", []
    return "logistics_delay", []


def assess_order(
    view: OrderShipmentView,
    *,
    opened_at: datetime | None,
    source_precedence: tuple[str, ...] | None = None,
) -> ShipmentAssessment:
    """Pure shipment analysis for one order (unit-testable, no I/O)."""
    events = _shipment_events(view.shipment)
    timeline, by_source, conflict_fields = _timeline(view, source_precedence)
    verdict, late_sellers = _verdict_for(timeline, view, events, opened_at)

    conflicts: list[dict[str, Any]] = []
    for name in conflict_fields:
        if source_precedence:
            selected, code = source_precedence[0], "SOURCE_PRECEDENCE_POLICY"
        else:
            # Without a policy decision, check whether the disagreement changes the verdict.
            alternatives = {
                _verdict_for({**timeline, name: by_source[src][name]}, view, events, opened_at)[0]
                for src in by_source
            }
            if len(alternatives) > 1:
                selected, code = None, "UNRESOLVED_SOURCE_CONFLICT"
                verdict, late_sellers = "conflicting", []
            else:
                selected, code = "get_order", "NO_VERDICT_IMPACT"
        conflicts.append(
            {
                "field": name,
                "sources": ["get_order", "get_shipment_summary"],
                "selected_source": selected,
                "resolution_code": code,
            }
        )

    if verdict in ("lost", "returned"):
        complete = timeline["order_purchase_timestamp"] is not None and bool(events)
    else:
        complete = all(value is not None for value in timeline.values())

    return ShipmentAssessment(
        verdict=verdict,
        late_seller_ids=late_sellers,
        timeline_complete=complete,
        conflicts=conflicts,
        responsible_parties=_responsible(verdict, late_sellers, view.shipment),
        cause_codes=[_CAUSE_CODES[verdict]] if verdict in _CAUSE_CODES else [],
    )


def _responsible(
    verdict: str, late_sellers: list[str], shipment: dict[str, Any]
) -> list[dict[str, Any]]:
    if verdict == "seller_delay":
        return [{"party_type": "seller", "party_id": seller} for seller in late_sellers[:5]]
    if verdict in ("logistics_delay", "lost"):
        carrier = _get(shipment, ("carrier_id", "logistics_provider_id", "carrier"))
        return [
            {
                "party_type": "logistics_provider",
                "party_id": str(carrier) if carrier is not None else None,
            }
        ]
    return []


def assess_delivery_claim(topic: str, verdict: str) -> str | None:
    """Claim verdict for delivery-related topics; ``None`` if not a delivery claim."""
    expected = _DELIVERY_CLAIMS.get(topic)
    if expected is None:
        return None
    if verdict in ("insufficient_evidence", "conflicting"):
        return "insufficient_evidence"
    return "supported" if verdict in expected else "unsupported"


# --------------------------------------------------------------------------- agent


def _merge(assessments: list[ShipmentAssessment]) -> ShipmentAssessment:
    if not assessments:
        return ShipmentAssessment("insufficient_evidence", [], False, [], [], [])
    top = max(assessments, key=lambda a: _VERDICT_PRIORITY[a.verdict])
    late: list[str] = []
    parties: list[dict[str, Any]] = []
    causes: list[str] = []
    conflicts: list[dict[str, Any]] = []
    for item in assessments:
        late += [s for s in item.late_seller_ids if s not in late]
        parties += [p for p in item.responsible_parties if p not in parties]
        causes += [c for c in item.cause_codes if c not in causes]
        conflicts += [c for c in item.conflicts if c not in conflicts]
    return ShipmentAssessment(
        verdict=top.verdict,
        late_seller_ids=late if top.verdict == "seller_delay" else [],
        timeline_complete=all(a.timeline_complete for a in assessments),
        conflicts=conflicts[:5],
        responsible_parties=parties[:5],
        cause_codes=causes[:5],
    )


def _confidence(assessment: ShipmentAssessment, failed: bool) -> float:
    if assessment.verdict == "insufficient_evidence" or failed:
        return 0.4
    if assessment.verdict == "conflicting":
        return 0.5
    score = 0.9 if assessment.timeline_complete else 0.7
    return score - 0.1 if assessment.conflicts else score


class OrderShipmentSpecialist(BaseSpecialist):
    """Person 2 Domain Specialist: order/items/product/sellers and shipment timeline.

    Expects ``task.input_data`` = ``{"case": case, "resolved_order_ids": [...]}`` and
    optionally ``"source_precedence"`` from the policy specialist, e.g.
    ``["get_shipment_summary", "get_order"]``. Only ``ALLOWED_TOOLS`` are ever called.
    """

    def __init__(self, actor_name: str = "order_shipment_specialist") -> None:
        super().__init__(actor_name=actor_name)

    async def _call(
        self, store: EvidenceStore, tool_name: str, case_id: str, order_id: str
    ) -> dict[str, Any]:
        if tool_name not in ALLOWED_TOOLS:
            raise PermissionError(f"{self.actor_name} is not allowed to call {tool_name}")
        return await store.call_tool_safe(
            tool_name, case_id=case_id, actor=self.actor_name, order_id=order_id
        )

    async def execute(self, task: AgentTask, store: EvidenceStore) -> SpecialistResult:
        case_id = task.case_id
        case = task.input_data.get("case") or {}
        order_ids = list(dict.fromkeys(task.input_data.get("resolved_order_ids") or []))
        precedence = task.input_data.get("source_precedence")
        source_precedence = tuple(precedence) if precedence else None
        scope = case.get("investigation_scope") or {}
        opened_at = parse_ts(case.get("opened_at"))

        evidence_by_domain: dict[str, list[str]] = {}

        def keep(evidence: dict[str, Any]) -> Any:
            refs = evidence_by_domain.setdefault(evidence["domain"], [])
            if evidence["evidence_ref"] not in refs:
                refs.append(evidence["evidence_ref"])
            return evidence["data"]

        entities: dict[str, list[str]] = {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        }

        def add(kind: str, value: Any) -> None:
            if value is not None and str(value) not in entities[kind]:
                entities[kind].append(str(value))

        assessments: list[ShipmentAssessment] = []
        errors: list[str] = []
        warnings: list[str] = []
        if not order_ids:
            warnings.append("no resolved_order_ids; shipment verdict is insufficient_evidence")
        for order_id in order_ids:
            try:
                order_ev, items_ev, ship_ev = await asyncio.gather(
                    self._call(store, "get_order", case_id, order_id),
                    self._call(store, "get_order_items", case_id, order_id),
                    self._call(store, "get_shipment_summary", case_id, order_id),
                )
            except RuntimeError as exc:
                errors.append(f"order {order_id}: {exc}")
                continue
            view = OrderShipmentView(
                order_id=order_id,
                order=_single(keep(order_ev), "order"),
                items=_records(keep(items_ev), "items", "order_items", "rows"),
                shipment=_single(keep(ship_ev), "shipment", "summary"),
            )
            assessment = assess_order(
                view, opened_at=opened_at, source_precedence=source_precedence
            )

            # Seller records only when they back a responsibility claim or ids are missing.
            needs_sellers = assessment.verdict == "seller_delay" or not any(
                _get(item, _SELLER_ID) for item in view.items
            )
            if needs_sellers:
                with contextlib.suppress(RuntimeError):
                    sellers_ev = await self._call(store, "get_sellers", case_id, order_id)
                    view.sellers = _records(keep(sellers_ev), "sellers", "rows")
            if scope.get("include_product_context"):
                with contextlib.suppress(RuntimeError):
                    keep(await self._call(store, "get_product_context", case_id, order_id))

            add("order_ids", order_id)
            for item in view.items:
                add("item_ids", _item_id(order_id, item))
                add("seller_ids", _get(item, _SELLER_ID))
            for seller in view.sellers:
                add("seller_ids", _get(seller, _SELLER_ID))
            add("shipment_ids", _get(view.shipment, ("shipment_id", "tracking_id")))
            assessments.append(assessment)

        merged = _merge(assessments)
        claim_verdicts: dict[str, str] = {}
        for claim in (case.get("customer_request") or {}).get("claims", []):
            verdict = assess_delivery_claim(claim.get("topic", ""), merged.verdict)
            if verdict is not None:
                claim_verdicts[claim["claim_id"]] = verdict

        evidence_refs = [ref for refs in evidence_by_domain.values() for ref in refs]
        if store.trace is not None:
            store.trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor=self.actor_name,
                target="coordinator",
                decision_code=f"SHIPMENT_{merged.verdict.upper()}",
                evidence_refs=evidence_refs[:20],
                attributes={
                    "timeline_complete": merged.timeline_complete,
                    "late_seller_count": len(merged.late_seller_ids),
                    "conflict_count": len(merged.conflicts),
                    "failed_order_count": len(errors),
                },
            )
        return SpecialistResult(
            task_id=task.task_id,
            case_id=case_id,
            actor=self.actor_name,
            status=TaskStatus.FAILED if order_ids and not assessments else TaskStatus.COMPLETED,
            evidence_refs=evidence_refs,
            data={
                "affected_entities": {key: value[:20] for key, value in entities.items()},
                "shipment_analysis": {
                    "verdict": merged.verdict,
                    "late_seller_ids": merged.late_seller_ids[:20],
                    "timeline_complete": merged.timeline_complete,
                },
                "responsible_parties": merged.responsible_parties,
                "cause_codes": merged.cause_codes,
                "data_conflicts": merged.conflicts,
                "claim_verdicts": claim_verdicts,
                "evidence_by_domain": evidence_by_domain,
                "confidence": _confidence(merged, bool(errors)),
            },
            errors=errors,
            warnings=warnings,
        )
