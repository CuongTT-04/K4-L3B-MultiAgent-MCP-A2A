from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.coordinator import CoordinatorDependencies, coordinate_case
from student_agent.models import SpecialistResult
from student_agent.payment_agent import PaymentDecision, PaymentFacts, PaymentFindings

ENTITY_EV = "ev_entity00000000000000001"
ORDER_EV = "ev_order000000000000000001"
SHIPMENT_EV = "ev_shipment0000000000001"
PAYMENT_EV = "ev_payment00000000000001"
POLICY_EV = "ev_policy000000000000001"
CASE = {
    "case_id": "L3B_CASE_001",
    "opened_at": "2018-01-01T09:00:00-03:00",
    "policy_version": "EC_POLICY_V2",
    "customer_request": {
        "claimed_order_id": "order-1",
        "claims": [{"claim_id": "claim-1", "topic": "refund_pending"}],
    },
}


class RecordingTrace:
    def __init__(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.contracts = Contracts(root / "contracts" / "schemas")
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> dict[str, Any]:
        self.events.append(event)
        return event


class FakeGateway:
    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        assert tool_name == "get_order"
        assert case_id == CASE["case_id"]
        assert arguments == {"order_id": "order-1"}
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": ORDER_EV,
            "result_hash": "sha256:" + "a" * 64,
            "domain": "order",
            "data": {
                "order_id": "order-1",
                "order_status": "delivered",
                "order_purchase_timestamp": "2018-01-01T09:00:00-03:00",
            },
        }


class FakeEntitySpecialist:
    def __init__(self, *, status: str = "resolved", overlap: bool = False) -> None:
        self.status = status
        self.overlap = overlap
        self.calls = 0

    async def execute(self, task: Any, store: Any) -> SpecialistResult:
        self.calls += 1
        resolved = ["order-1"] if self.status == "resolved" else []
        rejected = ["order-1"] if self.overlap else ["order-decoy"]
        return SpecialistResult(
            task.task_id,
            task.case_id,
            "entity_specialist",
            evidence_refs=[ENTITY_EV],
            data={
                "entity_resolution": {
                    "status": self.status,
                    "resolved_order_ids": resolved,
                    "rejected_candidates": rejected,
                    "confidence": 0.95 if resolved else 0.2,
                },
                "customer_context": {
                    "customer_unique_id": "customer-1",
                    "related_order_ids": resolved,
                },
            },
        )


class FakeShipmentSpecialist:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, task: Any, store: Any) -> SpecialistResult:
        self.calls += 1
        assert task.input_data["emit_handoff"] is False
        return SpecialistResult(
            task.task_id,
            task.case_id,
            "order_shipment_specialist",
            evidence_refs=[SHIPMENT_EV],
            data={
                "affected_entities": {
                    "order_ids": ["order-1"],
                    "item_ids": [],
                    "seller_ids": [],
                    "payment_references": [],
                    "shipment_ids": [],
                },
                "shipment_analysis": {
                    "verdict": "on_time",
                    "late_seller_ids": [],
                    "timeline_complete": True,
                },
                "responsible_parties": [],
                "cause_codes": [],
                "data_conflicts": [],
                "claim_verdicts": {},
                "confidence": 0.9,
            },
        )


class FakePaymentInvestigator:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> tuple[PaymentFindings, PaymentDecision]:
        self.calls += 1
        assert kwargs["emit_policy_event"] is False
        findings = PaymentFindings(
            "order-1",
            PaymentFacts(
                captured_total_brl=100.0,
                refunded_total_brl=0.0,
                refund_pending_brl=100.0,
            ),
            None,
            ["refund_pending"],
            evidence_by_tool={
                "get_payment_timeline": PAYMENT_EV,
                "get_policy": POLICY_EV,
            },
        )
        return findings, findings.decide()


def dependencies(
    entity: FakeEntitySpecialist | None = None,
) -> tuple[
    CoordinatorDependencies,
    FakeEntitySpecialist,
    FakeShipmentSpecialist,
    FakePaymentInvestigator,
]:
    entity = entity or FakeEntitySpecialist()
    shipment = FakeShipmentSpecialist()
    payment = FakePaymentInvestigator()
    return (
        CoordinatorDependencies(entity, shipment, payment, None),
        entity,
        shipment,
        payment,
    )


def test_coordinate_case_runs_specialists_and_verifies_output() -> None:
    deps, entity, shipment, payment = dependencies()
    trace = RecordingTrace()

    output = asyncio.run(coordinate_case(CASE, FakeGateway(), trace, deps))

    trace.contracts.validate_output(output, "coordinator output")
    assert (entity.calls, shipment.calls, payment.calls) == (1, 1, 1)
    lifecycle = [
        event["event_type"]
        for event in trace.events
        if event["event_type"] != "tool_result_consumed"
    ]
    assert lifecycle == [
        "task_assigned",
        "handoff",
        "task_assigned",
        "handoff",
        "task_assigned",
        "policy_decided",
        "handoff",
        "verification_completed",
    ]
    assert trace.events[-1]["decision_code"] == "PASSED"


def test_unresolved_entity_skips_domain_specialists() -> None:
    deps, entity, shipment, payment = dependencies(FakeEntitySpecialist(status="not_found"))
    trace = RecordingTrace()

    output = asyncio.run(coordinate_case(CASE, FakeGateway(), trace, deps))

    assert (entity.calls, shipment.calls, payment.calls) == (1, 0, 0)
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert trace.events[-1]["event_type"] == "verification_completed"


def test_non_fatal_verification_failure_is_traced_and_output_kept() -> None:
    deps, _, _, _ = dependencies(FakeEntitySpecialist(overlap=True))
    trace = RecordingTrace()

    output = asyncio.run(coordinate_case(CASE, FakeGateway(), trace, deps))

    assert output["case_id"] == CASE["case_id"]
    assert trace.events[-1]["event_type"] == "verification_completed"
    assert trace.events[-1]["decision_code"] == "FAILED"
    assert "ENTITY_SET_OVERLAP" in trace.events[-1]["attributes"]["error_codes"]
