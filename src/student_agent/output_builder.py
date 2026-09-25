from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .conflict import has_unresolved_conflict, merge_conflicts
from .llm_synthesis import SynthesisDecision
from .models import SpecialistResult
from .payment_agent import PaymentDecision, PaymentFindings


class Synthesizer(Protocol):
    async def synthesize(self, **kwargs: Any) -> SynthesisDecision: ...


_SHIPMENT_ISSUES = {
    "seller_delay": "late_delivery_seller",
    "logistics_delay": "late_delivery_logistics",
    "lost": "late_delivery_logistics",
}
_ISSUE_CAUSES = {
    "canceled_order_paid": "ORDER_CANCELED_AFTER_CAPTURE",
    "unavailable_order_paid": "ORDER_UNAVAILABLE_AFTER_CAPTURE",
    "valid_split_payment": "VALID_SPLIT_PAYMENT",
    "payment_mismatch": "PAYMENT_CAPTURE_MISMATCH",
    "duplicate_charge": "DUPLICATE_PAYMENT_CAPTURE",
    "refund_pending": "REFUND_PROCESSING_PENDING",
    "refund_failed": "REFUND_PROCESSING_FAILED",
    "unsupported_claim": "CLAIM_NOT_SUPPORTED",
    "insufficient_evidence": "INSUFFICIENT_EVIDENCE",
}


def _unique(values: Sequence[Any], limit: int) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value not in (None, "")))[:limit]


def _payment_confidence(findings: PaymentFindings) -> float:
    if findings.facts.captured_total_brl is None:
        return 0.4
    if "get_payment_timeline" in findings.missing_tools:
        return 0.55
    if "get_policy" in findings.missing_tools:
        return 0.7
    return 0.9


def _allowed_issues(
    entity: SpecialistResult,
    shipment: SpecialistResult,
    payment: PaymentFindings,
) -> tuple[str, ...]:
    entity_status = (entity.data.get("entity_resolution") or {}).get("status")
    if entity_status != "resolved":
        return ("insufficient_evidence",)
    issues = list(payment.issues)
    shipment_data = shipment.data.get("shipment_analysis") or {}
    shipment_issue = _SHIPMENT_ISSUES.get(shipment_data.get("verdict"))
    if shipment_issue:
        issues.append(shipment_issue)
    if not issues:
        evidence_is_missing = (
            shipment_data.get("verdict") in ("insufficient_evidence", "conflicting", None)
            or payment.facts.captured_total_brl is None
        )
        issues.append("insufficient_evidence" if evidence_is_missing else "unsupported_claim")
    return tuple(dict.fromkeys(issues))


def _cause_codes(issues: Sequence[str], shipment: SpecialistResult) -> tuple[str, ...]:
    result = [_ISSUE_CAUSES[issue] for issue in issues if issue in _ISSUE_CAUSES]
    result.extend(shipment.data.get("cause_codes") or [])
    return tuple(_unique(result, 5))


def _fallback_synthesis(
    issues: tuple[str, ...],
    causes: tuple[str, ...],
    decision: PaymentDecision,
    confidence: float,
) -> SynthesisDecision:
    primary = issues[0]
    if primary == "insufficient_evidence":
        status = "needs_investigation"
    elif primary in ("valid_split_payment", "unsupported_claim"):
        status = "no_action"
    else:
        status = decision.case_status
    return SynthesisDecision(primary, issues[1:], status, confidence, causes)


def _claim_assessments(
    case: Mapping[str, Any],
    shipment: SpecialistResult,
    payment: PaymentFindings,
    decision: PaymentDecision,
    entity_refs: list[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    shipment_verdicts = shipment.data.get("claim_verdicts") or {}
    shipment_refs = list(shipment.evidence_refs)
    payment_refs = payment.evidence_refs
    for claim in (case.get("customer_request") or {}).get("claims", [])[:5]:
        claim_id = claim.get("claim_id")
        topic = claim.get("topic")
        if not isinstance(claim_id, str) or not claim_id:
            continue
        if claim_id in shipment_verdicts:
            verdict = shipment_verdicts[claim_id]
            refs = shipment_refs
            confidence = float(shipment.data.get("confidence", 0.4))
        elif topic == "requested_full_refund":
            amount = decision.financial_resolution.get("recommended_refund_brl") or 0.0
            captured = payment.facts.captured_total_brl or 0.0
            if amount <= 0:
                verdict = "unsupported"
            elif amount >= captured - 0.01:
                verdict = "supported"
            else:
                verdict = "partially_supported"
            refs = payment_refs
            confidence = _payment_confidence(payment)
        elif topic == "unsupported_claim":
            verdict = "unsupported"
            refs = _unique([*entity_refs, *shipment_refs, *payment_refs], 30)
            confidence = min(0.9, max(float(shipment.data.get("confidence", 0.4)), 0.4))
        elif topic in payment.issues:
            verdict = "supported"
            refs = payment_refs
            confidence = _payment_confidence(payment)
        elif payment.facts.captured_total_brl is None:
            verdict = "insufficient_evidence"
            refs = payment_refs
            confidence = 0.4
        else:
            verdict = "unsupported"
            refs = payment_refs
            confidence = _payment_confidence(payment)
        result.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": round(max(0.0, min(1.0, confidence)), 4),
                "evidence_refs": _unique(refs, 30),
            }
        )
    return result


async def build_output(
    case: Mapping[str, Any],
    entity: SpecialistResult,
    shipment: SpecialistResult,
    payment: PaymentFindings,
    payment_decision: PaymentDecision,
    *,
    synthesizer: Synthesizer | None = None,
) -> dict[str, Any]:
    """Combine teammate results; the optional model may only rank allowed facts."""
    entity_resolution = dict(entity.data.get("entity_resolution") or {})
    customer_context = dict(entity.data.get("customer_context") or {})
    resolved = entity_resolution.get("status") == "resolved"

    shipment_analysis = dict(shipment.data.get("shipment_analysis") or {})
    affected = dict(shipment.data.get("affected_entities") or {})
    for key in (
        "order_ids",
        "item_ids",
        "seller_ids",
        "payment_references",
        "shipment_ids",
    ):
        affected[key] = _unique(affected.get(key) or [], 20)
    affected["order_ids"] = _unique(
        [*(entity_resolution.get("resolved_order_ids") or []), *affected["order_ids"]], 20
    )

    conflicts = merge_conflicts(
        entity.data.get("data_conflicts") or [],
        shipment.data.get("data_conflicts") or [],
    )
    unresolved = has_unresolved_conflict(conflicts)
    issues = _allowed_issues(entity, shipment, payment)
    causes = _cause_codes(issues, shipment)
    entity_confidence = float(entity_resolution.get("confidence", 0.0))
    shipment_confidence = float(shipment.data.get("confidence", 0.4))
    confidence_ceiling = round(
        min(entity_confidence, shipment_confidence, _payment_confidence(payment)), 4
    )
    if unresolved:
        confidence_ceiling = min(confidence_ceiling, 0.6)

    synthesis = _fallback_synthesis(issues, causes, payment_decision, confidence_ceiling)
    if synthesizer is not None and resolved:
        try:
            synthesis = await synthesizer.synthesize(
                case=case,
                facts={
                    "entity_status": entity_resolution.get("status"),
                    "shipment_verdict": shipment_analysis.get("verdict"),
                    "payment_verdict": payment_decision.payment_analysis.get("verdict"),
                    "payment_issues": payment.issues,
                    "unresolved_conflict": unresolved,
                },
                allowed_issues=issues,
                allowed_cause_codes=causes,
                confidence_ceiling=confidence_ceiling,
            )
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            synthesis = _fallback_synthesis(issues, causes, payment_decision, confidence_ceiling)

    synthesis = SynthesisDecision(
        synthesis.primary_issue,
        synthesis.secondary_issues,
        synthesis.case_status,
        min(synthesis.confidence, confidence_ceiling),
        synthesis.ranked_cause_codes,
    )

    # Money, actions and parties must follow the issue finally chosen, not the pre-synthesis one.
    if resolved and synthesis.primary_issue != payment_decision.issue:
        payment_decision = payment.decide(synthesis.primary_issue)

    financial = dict(payment_decision.financial_resolution)
    if not resolved:
        synthesis = SynthesisDecision(
            "insufficient_evidence",
            (),
            "needs_investigation",
            entity_confidence,
            ("INSUFFICIENT_EVIDENCE",),
        )
        financial = {"currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": []}
    elif unresolved:
        synthesis = SynthesisDecision(
            synthesis.primary_issue,
            synthesis.secondary_issues,
            "needs_investigation",
            min(synthesis.confidence, 0.6),
            synthesis.ranked_cause_codes,
        )

    recommended_refund = financial.get("recommended_refund_brl") or 0.0
    case_status = synthesis.case_status
    if resolved and not unresolved and payment_decision.policy_applied:
        case_status = payment_decision.case_status
    if recommended_refund > 0:
        case_status = "action_required"
    elif synthesis.primary_issue in ("valid_split_payment", "unsupported_claim"):
        case_status = "no_action"
    elif synthesis.primary_issue == "insufficient_evidence":
        case_status = "needs_investigation"

    evidence_refs = _unique(
        entity.evidence_refs if not resolved else [
            *entity.evidence_refs,
            *shipment.evidence_refs,
            *payment.evidence_refs,
        ],
        30,
    )
    parties: list[dict[str, Any]] = []
    for value in [
        *(shipment.data.get("responsible_parties") or []),
        *payment_decision.responsible_parties,
    ]:
        if isinstance(value, dict) and value not in parties:
            parties.append(value)

    ranked_causes = [
        {"cause_code": code, "rank": rank}
        for rank, code in enumerate(synthesis.ranked_cause_codes[:5], 1)
    ]
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": str(case["case_id"]),
        "assessment": {
            "primary_issue": synthesis.primary_issue,
            "secondary_issues": list(synthesis.secondary_issues)[:10],
            "case_status": case_status,
            "confidence": round(max(0.0, min(1.0, synthesis.confidence)), 4),
        },
        "affected_entities": affected,
        "claim_assessments": _claim_assessments(
            case, shipment, payment, payment_decision, list(entity.evidence_refs)
        ),
        "entity_resolution": entity_resolution,
        "customer_context": customer_context,
        "shipment_analysis": shipment_analysis,
        "payment_analysis": dict(payment_decision.payment_analysis),
        "root_cause_analysis": {
            "ranked_causes": ranked_causes,
            "responsible_parties": parties[:5],
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": conflicts,
        "financial_resolution": financial,
        "resolution_actions": _unique(payment_decision.resolution_actions, 8),
    }
