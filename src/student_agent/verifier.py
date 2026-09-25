from __future__ import annotations

import math
from collections.abc import Mapping, Set
from dataclasses import dataclass
from typing import Any

from .contracts import ContractError, Contracts


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    error_codes: tuple[str, ...]


def verify_output(
    case: Mapping[str, Any],
    output: Mapping[str, Any],
    available_evidence_refs: Set[str],
    contracts: Contracts,
) -> VerificationResult:
    errors: list[str] = []

    def add(code: str, condition: bool) -> None:
        if condition and code not in errors:
            errors.append(code)

    add("CASE_ID_MISMATCH", output.get("case_id") != case.get("case_id"))

    evidence_refs = output.get("evidence_refs") or []
    claim_refs = [
        ref
        for claim in output.get("claim_assessments") or []
        if isinstance(claim, Mapping)
        for ref in claim.get("evidence_refs") or []
    ]
    add(
        "UNKNOWN_EVIDENCE_REF",
        any(ref not in available_evidence_refs for ref in [*evidence_refs, *claim_refs]),
    )
    add("DUPLICATE_EVIDENCE_REF", len(evidence_refs) != len(set(evidence_refs)))

    entity = output.get("entity_resolution") or {}
    resolved = set(entity.get("resolved_order_ids") or [])
    rejected = set(entity.get("rejected_candidates") or [])
    affected = output.get("affected_entities") or {}
    affected_orders = set(affected.get("order_ids") or [])
    add("ENTITY_SET_OVERLAP", bool(resolved & rejected))
    add("RESOLVED_ORDER_NOT_AFFECTED", not resolved <= affected_orders)

    financial = output.get("financial_resolution") or {}
    recommended = financial.get("recommended_refund_brl")
    lines = financial.get("refund_lines") or []
    line_sum = sum(
        float(line.get("amount_brl", 0.0))
        for line in lines
        if isinstance(line, Mapping)
    )
    if isinstance(recommended, int | float) and not isinstance(recommended, bool):
        add(
            "REFUND_SUM_MISMATCH",
            not math.isclose(float(recommended), line_sum, rel_tol=0.0, abs_tol=0.01),
        )

    payment = output.get("payment_analysis") or {}
    captured = payment.get("captured_total_brl")
    refunded = payment.get("refunded_total_brl")
    if isinstance(captured, int | float) and isinstance(refunded, int | float):
        add("REFUNDED_EXCEEDS_CAPTURED", float(refunded) > float(captured) + 0.01)

    assessment = output.get("assessment") or {}
    add(
        "NO_ACTION_WITH_REFUND",
        assessment.get("case_status") == "no_action"
        and isinstance(recommended, int | float)
        and float(recommended) > 0.0,
    )
    confidence = assessment.get("confidence")
    add(
        "CONFIDENCE_OUT_OF_RANGE",
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not 0.0 <= float(confidence) <= 1.0,
    )

    shipment = output.get("shipment_analysis") or {}
    late_sellers = set(shipment.get("late_seller_ids") or [])
    affected_sellers = set(affected.get("seller_ids") or [])
    add("LATE_SELLER_NOT_AFFECTED", not late_sellers <= affected_sellers)

    actions = output.get("resolution_actions") or []
    add("DUPLICATE_RESOLUTION_ACTION", len(actions) != len(set(actions)))

    try:
        contracts.validate_output(dict(output), "verified output")
    except ContractError:
        add("SCHEMA_INVALID", True)

    return VerificationResult(not errors, tuple(errors))
