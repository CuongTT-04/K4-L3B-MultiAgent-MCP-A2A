from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.models import SpecialistResult
from student_agent.output_builder import build_output
from student_agent.payment_agent import PaymentDecision, PaymentFacts, PaymentFindings
from student_agent.verifier import VerificationResult, verify_output

ENTITY_EV = "ev_entity00000000000000001"
SHIPMENT_EV = "ev_shipment0000000000001"
PAYMENT_EV = "ev_payment00000000000001"
CASE = {"case_id": "L3B_CASE_001", "customer_request": {"claims": []}}
CONTRACTS = Contracts(Path(__file__).resolve().parents[1] / "contracts" / "schemas")


def good_output() -> dict[str, Any]:
    entity = SpecialistResult(
        "entity-task",
        "L3B_CASE_001",
        "entity_specialist",
        evidence_refs=[ENTITY_EV],
        data={
            "entity_resolution": {
                "status": "resolved",
                "resolved_order_ids": ["order-1"],
                "rejected_candidates": ["order-2"],
                "confidence": 0.9,
            },
            "customer_context": {
                "customer_unique_id": "customer-1",
                "related_order_ids": ["order-1"],
            },
        },
    )
    shipment = SpecialistResult(
        "shipment-task",
        "L3B_CASE_001",
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
    findings = PaymentFindings(
        "order-1",
        PaymentFacts(captured_total_brl=100.0, refunded_total_brl=0.0),
        None,
        [],
        evidence_by_tool={"get_payment_timeline": PAYMENT_EV},
    )
    decision = PaymentDecision(
        issue=None,
        payment_analysis={
            "verdict": "reconciled",
            "captured_total_brl": 100.0,
            "refunded_total_brl": 0.0,
            "refundable_total_brl": 100.0,
        },
        financial_resolution={
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        case_status="no_action",
        resolution_actions=[],
        responsible_parties=[],
        policy_applied=False,
    )
    return asyncio.run(build_output(CASE, entity, shipment, findings, decision))


def _unknown_evidence(output: dict[str, Any]) -> None:
    output["evidence_refs"].append("ev_unknown0000000000000000")


def _overlap_entities(output: dict[str, Any]) -> None:
    output["entity_resolution"]["rejected_candidates"] = ["order-1"]


def _remove_affected_order(output: dict[str, Any]) -> None:
    output["affected_entities"]["order_ids"] = []


def _refund_sum_mismatch(output: dict[str, Any]) -> None:
    output["financial_resolution"] = {
        "currency": "BRL",
        "recommended_refund_brl": 10.0,
        "refund_lines": [
            {"reason_code": "refund_pending", "amount_brl": 8.0, "entity_id": "order-1"}
        ],
    }
    output["assessment"]["case_status"] = "action_required"


def _refunded_above_captured(output: dict[str, Any]) -> None:
    output["payment_analysis"]["refunded_total_brl"] = 101.0


def _no_action_with_refund(output: dict[str, Any]) -> None:
    output["financial_resolution"]["recommended_refund_brl"] = 10.0


def _unknown_late_seller(output: dict[str, Any]) -> None:
    output["shipment_analysis"]["late_seller_ids"] = ["seller-unknown"]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda output: output.update(case_id="OTHER_CASE"), "CASE_ID_MISMATCH"),
        (_unknown_evidence, "UNKNOWN_EVIDENCE_REF"),
        (_overlap_entities, "ENTITY_SET_OVERLAP"),
        (_remove_affected_order, "RESOLVED_ORDER_NOT_AFFECTED"),
        (_refund_sum_mismatch, "REFUND_SUM_MISMATCH"),
        (_refunded_above_captured, "REFUNDED_EXCEEDS_CAPTURED"),
        (_no_action_with_refund, "NO_ACTION_WITH_REFUND"),
        (_unknown_late_seller, "LATE_SELLER_NOT_AFFECTED"),
    ],
)
def test_verifier_reports_invariant(
    mutation: Callable[[dict[str, Any]], None], expected_code: str
) -> None:
    output = deepcopy(good_output())
    mutation(output)

    result = verify_output(
        CASE, output, {ENTITY_EV, SHIPMENT_EV, PAYMENT_EV}, CONTRACTS
    )

    assert expected_code in result.error_codes


def test_verifier_accepts_known_good_output() -> None:
    assert verify_output(
        CASE,
        good_output(),
        {ENTITY_EV, SHIPMENT_EV, PAYMENT_EV},
        CONTRACTS,
    ) == VerificationResult(True, ())


def test_verifier_accepts_one_cent_refund_rounding_tolerance() -> None:
    output = good_output()
    output["financial_resolution"] = {
        "currency": "BRL",
        "recommended_refund_brl": 10.01,
        "refund_lines": [
            {"reason_code": "rounding", "amount_brl": 10.0, "entity_id": "order-1"}
        ],
    }
    output["assessment"]["case_status"] = "action_required"

    result = verify_output(
        CASE, output, {ENTITY_EV, SHIPMENT_EV, PAYMENT_EV}, CONTRACTS
    )

    assert "REFUND_SUM_MISMATCH" not in result.error_codes
