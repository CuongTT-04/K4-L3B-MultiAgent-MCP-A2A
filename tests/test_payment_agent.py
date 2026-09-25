"""Payment agent tests. Fixtures mirror payloads observed on the L3B gateway (cases 002-009)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.payment_agent import (
    TOOL_ORDER_PAYMENTS,
    TOOL_PAYMENT_TIMELINE,
    TOOL_POLICY,
    TOOL_REFUND_TIMELINE,
    analyze,
    investigate_payment,
)

ROOT = Path(__file__).resolve().parents[1]
ORDER = "b04477ada8d2ad7fa9d95358baaa785d"


def rule(status: str, action: str, refund: float, party: str, party_id: str | None = None):
    return {
        "case_status": status,
        "recommended_action": action,
        "refund_brl": refund,
        "responsible_parties": [{"party_id": party_id, "party_type": party}],
    }


POLICY = {
    "currency": "BRL",
    "policy_version": "EC_POLICY_V2",
    "rules": {
        "canceled_order_paid": rule("action_required", "issue_refund", 79.0, "platform"),
        "duplicate_charge": rule(
            "action_required", "refund_duplicate_charge", 64.0, "payment_provider"
        ),
        "late_delivery_logistics": rule(
            "action_required", "refund_freight", 16.0, "logistics_provider"
        ),
        "late_delivery_seller": rule(
            "action_required", "refund_freight", 18.0, "seller", "seller-9b75cdaf2d85"
        ),
        "payment_mismatch": rule("action_required", "reconcile_payment", 35.0, "payment_provider"),
        "refund_failed": rule("action_required", "retry_refund", 52.0, "payment_provider"),
        "refund_pending": rule("needs_investigation", "monitor_refund", 0.0, "payment_provider"),
        "unavailable_order_paid": rule(
            "action_required", "issue_refund", 89.0, "seller", "seller-eb09635680fa"
        ),
        "unsupported_claim": rule("no_action", "document_no_action", 0.0, "customer"),
        "valid_split_payment": rule("no_action", "document_no_action", 0.0, "customer"),
    },
}


def order(bought: str, status: str = "delivered") -> dict[str, Any]:
    return {
        "order_id": ORDER,
        "order_status": status,
        "order_purchase_timestamp": f"{bought}T09:00:00-03:00",
        "order_approved_at": f"{bought}T10:00:00-03:00",
    }


def timeline(payments: list[tuple[str, str, str]], events: list[tuple[str, str, str, str]]):
    return {
        "order_id": ORDER,
        "payments": [
            {"order_id": ORDER, "payment_sequential": seq, "payment_type": kind,
             "payment_installments": "1", "payment_value": value}
            for seq, kind, value in payments
        ],
        "events": [
            {"order_id": ORDER, "event_at": f"{at}:00-03:00", "event_type": kind,
             "amount_brl": amount, "status": status}
            for at, kind, amount, status in events
        ],
    }


def refunds(*events: tuple[str, str, str]) -> dict[str, Any]:
    return {
        "order_id": ORDER,
        "events": [
            {"order_id": ORDER, "event_at": f"{at}:00-03:00", "event_type": "refund_requested",
             "amount_brl": amount, "status": status}
            for at, amount, status in events
        ],
    }


def payloads(timeline_data: Any, refund_data: Any = None, policy: Any = POLICY) -> dict[str, Any]:
    result = {TOOL_PAYMENT_TIMELINE: timeline_data}
    if refund_data is not None:
        result[TOOL_REFUND_TIMELINE] = refund_data
    if policy is not None:
        result[TOOL_POLICY] = policy
    return result


# Case 002: real lifecycle = capture 52 on purchase day, refund 52 failed; split is a distractor.
CASE_002 = payloads(
    timeline(
        [("1", "credit_card", "52.00"), ("1", "credit_card", "44.50"), ("2", "voucher", "44.50")],
        [("2018-04-10T10:00", "captured", "52.00", "confirmed"),
         ("2018-01-21T10:00", "captured", "44.50", "confirmed"),
         ("2018-01-21T11:00", "captured", "44.50", "confirmed")],
    ),
    refunds(("2018-04-21T09:00", "52.00", "failed")),
)
# Case 006: mirror of 002 - the split is real, the failed refund is the distractor.
CASE_006 = payloads(
    timeline(
        [("1", "credit_card", "44.50"), ("2", "voucher", "44.50"), ("1", "credit_card", "52.00")],
        [("2018-09-06T10:00", "captured", "44.50", "confirmed"),
         ("2018-09-06T11:00", "captured", "44.50", "confirmed"),
         ("2018-05-25T10:00", "captured", "52.00", "confirmed")],
    ),
    refunds(("2018-06-05T09:00", "52.00", "failed")),
)
# Case 003: real = capture 89 + refund pending; the open mismatch belongs to an older capture.
CASE_003 = payloads(
    timeline(
        [("1", "credit_card", "89.00"), ("1", "credit_card", "35.00")],
        [("2018-03-09T10:00", "captured", "89.00", "confirmed"),
         ("2018-02-19T10:00", "captured", "35.00", "confirmed"),
         ("2018-02-19T12:00", "reconciliation_mismatch", "35.00", "open")],
    ),
    refunds(("2018-03-20T09:00", "89.00", "pending")),
)
# Case 005: mirror of 003 - the mismatch is real, the pending refund is the distractor.
CASE_005 = payloads(
    timeline(
        [("1", "credit_card", "35.00"), ("1", "credit_card", "89.00")],
        [("2018-01-07T10:00", "captured", "35.00", "confirmed"),
         ("2018-01-07T12:00", "reconciliation_mismatch", "35.00", "open"),
         ("2018-04-23T10:00", "captured", "89.00", "confirmed")],
    ),
    refunds(("2018-05-04T09:00", "89.00", "pending")),
)
# Case 004: 64 captured twice on purchase day; get_refund_timeline errors (no refunds).
CASE_004 = payloads(
    timeline(
        [("1", "credit_card", "64.00"), ("2", "voucher", "64.00")] * 2,
        [("2018-02-08T10:00", "captured", "64.00", "confirmed"),
         ("2018-02-08T11:00", "captured", "64.00", "confirmed"),
         ("2018-03-23T10:00", "captured", "64.00", "confirmed"),
         ("2018-03-23T11:00", "captured", "64.00", "confirmed")],
    ),
)
CASE_009 = payloads(
    timeline(
        [("1", "credit_card", "89.00")] * 2,
        [("2018-06-03T10:00", "captured", "89.00", "confirmed"),
         ("2018-08-28T10:00", "captured", "89.00", "confirmed")],
    ),
)
CASE_008 = payloads(
    timeline(
        [("1", "credit_card", "18.00"), ("1", "credit_card", "79.00")],
        [("2018-07-04T10:00", "captured", "18.00", "confirmed"),
         ("2018-07-27T10:00", "captured", "79.00", "confirmed")],
    ),
)


@pytest.mark.parametrize(
    ("data", "bought", "status", "issues", "analysis", "refund", "case_status", "action"),
    [
        (CASE_002, "2018-04-10", "delivered", ["refund_failed"],
         ("refund_failed", 52.0, 0.0, 52.0), 52.0, "action_required", "retry_refund"),
        (CASE_006, "2018-09-06", "delivered", ["valid_split_payment"],
         ("reconciled", 89.0, 0.0, 89.0), 0.0, "no_action", "document_no_action"),
        (CASE_003, "2018-03-09", "delivered", ["refund_pending"],
         ("refund_pending", 89.0, 0.0, 89.0), 0.0, "needs_investigation", "monitor_refund"),
        (CASE_005, "2018-01-07", "delivered", ["payment_mismatch"],
         ("capture_mismatch", 35.0, 0.0, 35.0), 35.0, "action_required", "reconcile_payment"),
        (CASE_004, "2018-02-08", "delivered", ["duplicate_charge"],
         ("duplicate_capture", 128.0, 0.0, 128.0), 64.0, "action_required",
         "refund_duplicate_charge"),
        (CASE_009, "2018-06-03", "unavailable", ["unavailable_order_paid"],
         ("reconciled", 89.0, 0.0, 89.0), 89.0, "action_required", "issue_refund"),
    ],
)
def test_observed_cases_follow_the_authoritative_lifecycle(
    data: dict[str, Any], bought: str, status: str, issues: list[str],
    analysis: tuple[str, float, float, float], refund: float, case_status: str, action: str,
) -> None:
    findings = analyze(ORDER, data, order=order(bought, status))
    assert findings.issues == issues
    decision = findings.decide()
    verdict, captured, refunded, refundable = analysis
    assert decision.payment_analysis == {
        "verdict": verdict,
        "captured_total_brl": captured,
        "refunded_total_brl": refunded,
        "refundable_total_brl": refundable,
    }
    assert decision.financial_resolution["recommended_refund_brl"] == refund
    assert decision.case_status == case_status
    assert decision.resolution_actions == [action]
    assert findings.view is not None and findings.view.ignored_events > 0


def test_refund_lines_carry_issue_and_order() -> None:
    decision = analyze(ORDER, CASE_002, order=order("2018-04-10")).decide()
    assert decision.financial_resolution == {
        "currency": "BRL",
        "recommended_refund_brl": 52.0,
        "refund_lines": [{"reason_code": "refund_failed", "amount_brl": 52.0, "entity_id": ORDER}],
    }
    assert decision.responsible_parties == [{"party_id": None, "party_type": "payment_provider"}]


def test_coordinator_issue_outside_payment_domain_uses_policy() -> None:
    findings = analyze(ORDER, CASE_008, order=order("2018-07-04"))
    assert findings.issues == []
    decision = findings.decide("late_delivery_seller")
    assert decision.payment_analysis["verdict"] == "reconciled"
    assert decision.payment_analysis["captured_total_brl"] == 18.0
    assert decision.financial_resolution["recommended_refund_brl"] == 18.0
    assert decision.resolution_actions == ["refund_freight"]
    assert decision.responsible_parties[0]["party_id"] == "seller-9b75cdaf2d85"


def test_decide_is_recomputable_without_new_calls() -> None:
    findings = analyze(ORDER, CASE_006, order=order("2018-09-06"))
    assert findings.decide().financial_resolution["recommended_refund_brl"] == 0.0
    assert findings.decide("unsupported_claim").resolution_actions == ["document_no_action"]


def test_without_order_row_every_event_counts() -> None:
    findings = analyze(ORDER, CASE_002)
    assert findings.issues == ["refund_failed", "valid_split_payment"]
    assert findings.facts.captured_total_brl == 141.0
    assert findings.view is not None and not findings.view.anchored


def test_items_total_separates_duplicate_from_split_without_policy() -> None:
    data = payloads(CASE_004[TOOL_PAYMENT_TIMELINE], policy=None)
    bought = order("2018-02-08")
    assert analyze(ORDER, data, order=bought, order_total_brl=64.0).issues == ["duplicate_charge"]
    assert analyze(ORDER, data, order=bought, order_total_brl=128.0).issues == [
        "valid_split_payment"
    ]
    decision = analyze(ORDER, data, order=bought, order_total_brl=64.0).decide()
    assert decision.financial_resolution["recommended_refund_brl"] == 64.0
    assert decision.policy_applied is False


def test_missing_timeline_is_insufficient_evidence_not_zero() -> None:
    decision = analyze(ORDER, {TOOL_POLICY: POLICY}, order=order("2018-02-08")).decide()
    assert decision.payment_analysis == {
        "verdict": "insufficient_evidence",
        "captured_total_brl": None,
        "refunded_total_brl": None,
        "refundable_total_brl": None,
    }
    assert decision.case_status == "needs_investigation"
    assert decision.financial_resolution["refund_lines"] == []


def test_completed_refund_is_counted() -> None:
    data = payloads(
        timeline([("1", "credit_card", "40.00")],
                 [("2018-05-01T10:00", "captured", "40.00", "confirmed")]),
        refunds(
            ("2018-05-03T09:00", "40.00", "pending"), ("2018-05-05T09:00", "40.00", "completed")
        ),
    )
    decision = analyze(ORDER, data, order=order("2018-05-01")).decide()
    assert decision.payment_analysis == {
        "verdict": "refunded",
        "captured_total_brl": 40.0,
        "refunded_total_brl": 40.0,
        "refundable_total_brl": 0.0,
    }


# ----------------------------------------------------------------------------- agent wiring


class FakeGateway:
    """Mimics the real gateway: get_refund_timeline raises when the order has no refunds."""

    def __init__(self, data: dict[str, Any], failing: set[str] | None = None) -> None:
        self.data = data
        self.failing = failing or set()
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        if tool_name in self.failing or tool_name not in self.data:
            raise RuntimeError(f"MCP tool {tool_name} failed: Error executing tool {tool_name}")
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool_name}_{case_id}_abcdefghij",
            "result_hash": "sha256:" + "0" * 64,
            "domain": "policy" if tool_name == TOOL_POLICY else "payment",
            "data": self.data[tool_name],
            "warnings": [],
        }


class FakeTrace:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, **kwargs: Any) -> dict[str, Any]:
        self.events.append(kwargs)
        return kwargs


CASE = {"case_id": "L3B_CASE_004", "policy_version": "EC_POLICY_V2"}


def test_agent_uses_three_calls_and_tolerates_missing_refunds() -> None:
    gateway = FakeGateway(CASE_004)
    trace = FakeTrace()
    findings, decision = asyncio.run(
        investigate_payment(CASE, ORDER, gateway.call, trace, order=order("2018-02-08"))
    )
    assert [c[0] for c in gateway.calls] == [
        TOOL_PAYMENT_TIMELINE, TOOL_REFUND_TIMELINE, TOOL_POLICY
    ]
    assert all(c[1] == "L3B_CASE_004" for c in gateway.calls)
    assert gateway.calls[0][2] == {"order_id": ORDER}
    assert gateway.calls[2][2] == {"policy_version": "EC_POLICY_V2"}
    assert findings.missing_tools == [TOOL_REFUND_TIMELINE]
    assert len(findings.evidence_refs) == 2
    assert decision.issue == "duplicate_charge"

    consumed = [e for e in trace.events if e["event_type"] == "tool_result_consumed"]
    assert [e["evidence_refs"][0] for e in consumed] == findings.evidence_refs
    assert trace.events[-1]["event_type"] == "policy_decided"
    assert trace.events[-1]["decision_code"] == "refund_duplicate_charge"


def test_agent_falls_back_to_order_payments_only_when_timeline_fails() -> None:
    data = dict(CASE_004, **{TOOL_ORDER_PAYMENTS: CASE_004[TOOL_PAYMENT_TIMELINE]["payments"]})
    gateway = FakeGateway(data, failing={TOOL_PAYMENT_TIMELINE})
    findings, decision = asyncio.run(
        investigate_payment(CASE, ORDER, gateway.call, FakeTrace(), order=order("2018-02-08"))
    )
    assert [c[0] for c in gateway.calls] == [
        TOOL_PAYMENT_TIMELINE, TOOL_ORDER_PAYMENTS, TOOL_REFUND_TIMELINE, TOOL_POLICY
    ]
    assert TOOL_ORDER_PAYMENTS in findings.evidence_by_tool
    assert decision.payment_analysis["captured_total_brl"] == 128.0


def test_real_trace_writer_accepts_agent_events(tmp_path: Path) -> None:
    from student_agent.trace import TraceWriter

    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts" / "schemas"))
    asyncio.run(
        investigate_payment(
            CASE, ORDER, FakeGateway(CASE_002).call, trace, order=order("2018-04-10")
        )
    )
    assert len((tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()) == 4


@pytest.mark.parametrize(
    ("data", "bought"),
    [(CASE_002, "2018-04-10"), (CASE_004, "2018-02-08"), (CASE_006, "2018-09-06"),
     ({TOOL_POLICY: POLICY}, "2018-01-01")],
)
def test_fragment_passes_the_public_output_schema(data: dict[str, Any], bought: str) -> None:
    decision = analyze(ORDER, data, order=order(bought)).decide()
    output = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": "L3B_CASE_001",
        "assessment": {
            "primary_issue": decision.issue or "insufficient_evidence",
            "secondary_issues": [],
            "case_status": decision.case_status,
            "confidence": 0.5,
        },
        "affected_entities": {
            "order_ids": [ORDER], "item_ids": [], "seller_ids": [],
            "payment_references": [], "shipment_ids": [],
        },
        "entity_resolution": {
            "status": "resolved", "resolved_order_ids": [ORDER],
            "rejected_candidates": [], "confidence": 0.9,
        },
        "customer_context": {"customer_unique_id": None, "related_order_ids": []},
        "shipment_analysis": {
            "verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False,
        },
        "root_cause_analysis": {
            "ranked_causes": [], "responsible_parties": decision.responsible_parties,
        },
        "evidence_refs": ["ev_payment_timeline_abcdefghijk"],
        "data_conflicts": [],
        "resolution_actions": decision.resolution_actions,
        **decision.output_fragment(),
    }
    Contracts(ROOT / "contracts" / "schemas").validate_output(output, "payment fragment")
