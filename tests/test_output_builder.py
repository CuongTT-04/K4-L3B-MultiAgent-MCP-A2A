from __future__ import annotations

import asyncio
from pathlib import Path

from student_agent.contracts import Contracts
from student_agent.llm_synthesis import SynthesisDecision
from student_agent.models import SpecialistResult
from student_agent.output_builder import build_output
from student_agent.payment_agent import PaymentDecision, PaymentFacts, PaymentFindings

ENTITY_EV = "ev_entity00000000000000001"
SHIPMENT_EV = "ev_shipment0000000000001"
PAYMENT_EV = "ev_payment00000000000001"
POLICY_EV = "ev_policy000000000000001"

CASE = {
    "case_id": "L3B_CASE_001",
    "customer_request": {
        "claimed_order_id": "order-1",
        "claims": [
            {"claim_id": "claim-delivery", "topic": "late_delivery_logistics"},
            {"claim_id": "claim-refund", "topic": "requested_full_refund"},
        ],
    },
}


def contracts() -> Contracts:
    return Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")


def entity_result(status: str = "resolved") -> SpecialistResult:
    return SpecialistResult(
        task_id="entity-L3B_CASE_001",
        case_id="L3B_CASE_001",
        actor="entity_specialist",
        evidence_refs=[ENTITY_EV],
        data={
            "entity_resolution": {
                "status": status,
                "resolved_order_ids": ["order-1"] if status == "resolved" else [],
                "rejected_candidates": ["order-decoy"],
                "confidence": 0.95 if status == "resolved" else 0.2,
            },
            "customer_context": {
                "customer_unique_id": "customer-1",
                "related_order_ids": ["order-1"],
            },
        },
    )


def shipment_result() -> SpecialistResult:
    return SpecialistResult(
        task_id="shipment-L3B_CASE_001",
        case_id="L3B_CASE_001",
        actor="order_shipment_specialist",
        evidence_refs=[SHIPMENT_EV],
        data={
            "affected_entities": {
                "order_ids": ["order-1"],
                "item_ids": ["order-1:1"],
                "seller_ids": ["seller-1"],
                "payment_references": [],
                "shipment_ids": ["shipment-1"],
            },
            "shipment_analysis": {
                "verdict": "logistics_delay",
                "late_seller_ids": [],
                "timeline_complete": True,
            },
            "responsible_parties": [
                {"party_type": "logistics_provider", "party_id": "carrier-1"}
            ],
            "cause_codes": ["LOGISTICS_TRANSIT_DELAY"],
            "data_conflicts": [],
            "claim_verdicts": {"claim-delivery": "supported"},
            "confidence": 0.85,
        },
    )


def payment_result() -> tuple[PaymentFindings, PaymentDecision]:
    findings = PaymentFindings(
        order_id="order-1",
        facts=PaymentFacts(
            captured_total_brl=100.0,
            refunded_total_brl=0.0,
            refund_pending_brl=100.0,
        ),
        view=None,
        issues=["refund_pending"],
        evidence_by_tool={
            "get_payment_timeline": PAYMENT_EV,
            "get_policy": POLICY_EV,
        },
    )
    decision = PaymentDecision(
        issue="refund_pending",
        payment_analysis={
            "verdict": "refund_pending",
            "captured_total_brl": 100.0,
            "refunded_total_brl": 0.0,
            "refundable_total_brl": 100.0,
        },
        financial_resolution={
            "currency": "BRL",
            "recommended_refund_brl": 100.0,
            "refund_lines": [
                {"reason_code": "refund_pending", "amount_brl": 100.0, "entity_id": "order-1"}
            ],
        },
        case_status="action_required",
        resolution_actions=["complete_refund"],
        responsible_parties=[{"party_type": "payment_provider", "party_id": None}],
        policy_applied=True,
    )
    return findings, decision


def test_build_output_combines_real_specialist_contracts() -> None:
    findings, decision = payment_result()
    output = asyncio.run(
        build_output(CASE, entity_result(), shipment_result(), findings, decision)
    )

    contracts().validate_output(output, "test output")
    assert output["assessment"] == {
        "primary_issue": "refund_pending",
        "secondary_issues": ["late_delivery_logistics"],
        "case_status": "action_required",
        "confidence": 0.85,
    }
    assert output["evidence_refs"] == [ENTITY_EV, SHIPMENT_EV, PAYMENT_EV, POLICY_EV]
    assert output["root_cause_analysis"]["ranked_causes"] == [
        {"cause_code": "REFUND_PROCESSING_PENDING", "rank": 1},
        {"cause_code": "LOGISTICS_TRANSIT_DELAY", "rank": 2},
    ]
    assert output["claim_assessments"][0]["verdict"] == "supported"
    assert output["claim_assessments"][1]["verdict"] == "supported"


class FakeSynthesizer:
    async def synthesize(self, **_: object) -> SynthesisDecision:
        return SynthesisDecision(
            primary_issue="late_delivery_logistics",
            secondary_issues=("refund_pending",),
            case_status="action_required",
            confidence=0.99,
            ranked_cause_codes=("LOGISTICS_TRANSIT_DELAY", "REFUND_PROCESSING_PENDING"),
        )


def test_build_output_uses_llm_choice_but_caps_confidence() -> None:
    findings, decision = payment_result()
    output = asyncio.run(
        build_output(
            CASE,
            entity_result(),
            shipment_result(),
            findings,
            decision,
            synthesizer=FakeSynthesizer(),
        )
    )

    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["confidence"] == 0.85
    assert output["root_cause_analysis"]["ranked_causes"][0]["cause_code"] == (
        "LOGISTICS_TRANSIT_DELAY"
    )


POLICY = {
    "rules": {
        "refund_pending": {
            "case_status": "needs_investigation",
            "recommended_action": "monitor_refund",
            "refund_brl": 0.0,
            "responsible_parties": [{"party_id": None, "party_type": "payment_provider"}],
        },
        "late_delivery_logistics": {
            "case_status": "action_required",
            "recommended_action": "refund_freight",
            "refund_brl": 16.0,
            "responsible_parties": [{"party_id": None, "party_type": "logistics_provider"}],
        },
    }
}


def test_financials_follow_the_issue_chosen_by_synthesis() -> None:
    findings, _ = payment_result()
    findings.policy = POLICY
    output = asyncio.run(
        build_output(
            CASE,
            entity_result(),
            shipment_result(),
            findings,
            findings.decide("refund_pending"),
            synthesizer=FakeSynthesizer(),
        )
    )

    contracts().validate_output(output, "test output")
    assert output["assessment"]["primary_issue"] == "late_delivery_logistics"
    assert output["assessment"]["case_status"] == "action_required"
    assert output["financial_resolution"]["recommended_refund_brl"] == 16.0
    assert output["financial_resolution"]["refund_lines"][0]["reason_code"] == (
        "late_delivery_logistics"
    )
    assert output["resolution_actions"] == ["refund_freight"]
    assert output["claim_assessments"][1]["verdict"] == "partially_supported"


def test_policy_status_is_used_without_synthesis() -> None:
    findings, _ = payment_result()
    findings.policy = POLICY
    output = asyncio.run(
        build_output(
            CASE, entity_result(), shipment_result(), findings, findings.decide("refund_pending")
        )
    )

    assert output["assessment"]["primary_issue"] == "refund_pending"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["resolution_actions"] == ["monitor_refund"]
    assert output["claim_assessments"][1]["verdict"] == "unsupported"


def test_build_output_is_conservative_when_entity_is_not_resolved() -> None:
    findings, decision = payment_result()
    output = asyncio.run(
        build_output(CASE, entity_result("not_found"), shipment_result(), findings, decision)
    )

    contracts().validate_output(output, "test output")
    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["assessment"]["confidence"] == 0.2
    assert output["financial_resolution"] == {
        "currency": "BRL",
        "recommended_refund_brl": 0.0,
        "refund_lines": [],
    }
