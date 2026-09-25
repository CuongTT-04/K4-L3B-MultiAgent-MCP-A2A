from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .evidence_store import EvidenceStore
from .models import AgentTask, SpecialistResult, TaskStatus, TaskType
from .output_builder import Synthesizer, build_output
from .payment_agent import (
    ACTOR_POLICY,
    PaymentDecision,
    PaymentFacts,
    PaymentFindings,
)
from .verifier import verify_output


class Specialist(Protocol):
    async def execute(self, task: AgentTask, store: EvidenceStore) -> SpecialistResult: ...


PaymentInvestigator = Callable[..., Awaitable[tuple[PaymentFindings, PaymentDecision]]]


@dataclass(frozen=True)
class CoordinatorDependencies:
    entity_specialist: Specialist
    order_shipment_specialist: Specialist
    investigate_payment: PaymentInvestigator
    synthesizer: Synthesizer | None


def _task(case_id: str, actor: str, task_type: TaskType, data: dict[str, Any]) -> AgentTask:
    return AgentTask(
        task_id=f"{task_type.value}-{case_id}",
        case_id=case_id,
        assigned_to=actor,
        task_type=task_type,
        input_data=data,
    )


def _emit_assignment(trace: Any, case_id: str, task: AgentTask) -> None:
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=task.assigned_to,
        decision_code=str(task.task_type),
        attributes={"task_id": task.task_id},
    )


def _emit_handoff(trace: Any, result: SpecialistResult) -> None:
    status = result.status.value if hasattr(result.status, "value") else str(result.status)
    trace.emit(
        case_id=result.case_id,
        event_type="handoff",
        actor=result.actor,
        target="coordinator",
        decision_code=status.upper(),
        evidence_refs=list(dict.fromkeys(result.evidence_refs))[:20] or None,
        attributes={
            "task_id": result.task_id,
            "error_count": len(result.errors),
            "warning_count": len(result.warnings),
        },
    )


def _fallback_shipment(case_id: str, order_ids: list[str]) -> SpecialistResult:
    return SpecialistResult(
        task_id=f"{TaskType.INVESTIGATE_ORDER_SHIPMENT.value}-{case_id}",
        case_id=case_id,
        actor="order_shipment_specialist",
        status=TaskStatus.COMPLETED,
        data={
            "affected_entities": {
                "order_ids": order_ids,
                "item_ids": [],
                "seller_ids": [],
                "payment_references": [],
                "shipment_ids": [],
            },
            "shipment_analysis": {
                "verdict": "insufficient_evidence",
                "late_seller_ids": [],
                "timeline_complete": False,
            },
            "responsible_parties": [],
            "cause_codes": [],
            "data_conflicts": [],
            "claim_verdicts": {},
            "confidence": 0.4,
        },
        warnings=["shipment investigation skipped because entity was unresolved"],
    )


def _fallback_payment(order_id: str) -> tuple[PaymentFindings, PaymentDecision]:
    findings = PaymentFindings(
        order_id=order_id,
        facts=PaymentFacts(),
        view=None,
        issues=[],
        missing_tools=["get_payment_timeline", "get_refund_timeline", "get_policy"],
    )
    return findings, findings.decide()


def _order_row(envelope: Mapping[str, Any]) -> dict[str, Any]:
    data = envelope.get("data")
    if not isinstance(data, Mapping):
        return {}
    nested = data.get("order")
    return dict(nested) if isinstance(nested, Mapping) else dict(data)


def _order_total(order: Mapping[str, Any]) -> float | None:
    for key in ("order_total_brl", "total_brl", "total", "payment_value"):
        value = order.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    return None


def _shipment_issue(result: SpecialistResult) -> str | None:
    verdict = (result.data.get("shipment_analysis") or {}).get("verdict")
    return {
        "seller_delay": "late_delivery_seller",
        "logistics_delay": "late_delivery_logistics",
        "lost": "late_delivery_logistics",
    }.get(verdict)


async def coordinate_case(
    case: Mapping[str, Any],
    gateway: Any,
    trace: Any,
    dependencies: CoordinatorDependencies,
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    store = EvidenceStore(gateway, trace)

    entity_task = _task(
        case_id,
        "entity_specialist",
        TaskType.RESOLVE_ENTITY,
        dict(case),
    )
    _emit_assignment(trace, case_id, entity_task)
    entity = await dependencies.entity_specialist.execute(entity_task, store)
    _emit_handoff(trace, entity)

    entity_data = entity.data.get("entity_resolution") or {}
    order_ids = list(dict.fromkeys(entity_data.get("resolved_order_ids") or []))
    resolved = entity_data.get("status") == "resolved" and bool(order_ids)
    if resolved:
        shipment_task = _task(
            case_id,
            "order_shipment_specialist",
            TaskType.INVESTIGATE_ORDER_SHIPMENT,
            {
                "case": dict(case),
                "resolved_order_ids": order_ids,
                "emit_handoff": False,
            },
        )
        _emit_assignment(trace, case_id, shipment_task)
        shipment = await dependencies.order_shipment_specialist.execute(shipment_task, store)
        _emit_handoff(trace, shipment)

        order_id = order_ids[0]
        try:
            order = _order_row(
                await store.get_order(case_id, order_id, actor="payment-agent")
            )
        except (OSError, RuntimeError, ValueError, TimeoutError):
            order = {}
        payment_task = _task(
            case_id,
            "payment-agent",
            TaskType.INVESTIGATE_PAYMENT_POLICY,
            {"case": dict(case), "resolved_order_ids": order_ids},
        )
        _emit_assignment(trace, case_id, payment_task)
        try:
            payment, decision = await dependencies.investigate_payment(
                case,
                order_id,
                gateway.call,
                trace,
                order=order,
                order_total_brl=_order_total(order),
                emit_policy_event=False,
            )
        except (OSError, RuntimeError, ValueError, TimeoutError):
            payment, decision = _fallback_payment(order_id)

        final_issue = payment.suggested_issue or _shipment_issue(shipment)
        decision = payment.decide(final_issue)
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor=ACTOR_POLICY,
            target="coordinator",
            decision_code=(decision.resolution_actions or ["no_policy_rule"])[0],
            evidence_refs=payment.evidence_refs[:20] or None,
            attributes={
                "issue": decision.issue,
                "payment_verdict": decision.payment_analysis["verdict"],
                "recommended_refund_brl": decision.financial_resolution[
                    "recommended_refund_brl"
                ],
            },
        )
        payment_result = SpecialistResult(
            task_id=payment_task.task_id,
            case_id=case_id,
            actor="payment-agent",
            status=TaskStatus.COMPLETED,
            evidence_refs=payment.evidence_refs,
        )
        _emit_handoff(trace, payment_result)
    else:
        shipment = _fallback_shipment(case_id, order_ids)
        payment, decision = _fallback_payment(order_ids[0] if order_ids else "")

    output = await build_output(
        case,
        entity,
        shipment,
        payment,
        decision,
        synthesizer=dependencies.synthesizer,
    )
    available_refs = set(
        [
            *entity.evidence_refs,
            *shipment.evidence_refs,
            *payment.evidence_refs,
            *store.get_case_evidence_refs(case_id),
        ]
    )
    verification = verify_output(case, output, available_refs, trace.contracts)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="PASSED" if verification.passed else "FAILED",
        evidence_refs=output["evidence_refs"][:20] or None,
        attributes={"error_count": len(verification.error_codes)},
    )
    if not verification.passed:
        joined = ",".join(verification.error_codes)
        raise ValueError(f"{case_id}: verification failed: {joined}")
    return output
