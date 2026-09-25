from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class TaskType(StrEnum):
    RESOLVE_ENTITY = "resolve_entity"
    INVESTIGATE_ORDER_SHIPMENT = "investigate_order_shipment"
    INVESTIGATE_PAYMENT_POLICY = "investigate_payment_policy"
    VERIFY_CASE = "verify_case"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class EntityResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


@dataclass(slots=True)
class AgentTask:
    """Standard task envelope for Agent-to-Agent (A2A) delegation."""

    task_id: str
    case_id: str
    assigned_to: str  # target specialist actor name
    task_type: TaskType | str
    input_data: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 30.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CustomerContextData:
    """Customer identity and historical order context."""

    customer_unique_id: str | None = None
    related_order_ids: list[str] = field(default_factory=list)

    def to_contract_dict(self) -> dict[str, Any]:
        return {
            "customer_unique_id": self.customer_unique_id,
            "related_order_ids": list(dict.fromkeys(self.related_order_ids)),
        }


@dataclass(slots=True)
class EntityResolutionData:
    """Specialist deliverable for Person 1 (Entity Resolution & Customer Context)."""

    status: EntityResolutionStatus | str = EntityResolutionStatus.NOT_FOUND
    resolved_order_ids: list[str] = field(default_factory=list)
    rejected_candidates: list[str] = field(default_factory=list)
    confidence: float = 0.0
    customer_context: CustomerContextData = field(default_factory=CustomerContextData)
    notes: list[str] = field(default_factory=list)

    def to_contract_entity_resolution(self) -> dict[str, Any]:
        """Format strictly according to l3b-output-v2 entity_resolution schema."""
        status_val = self.status.value if hasattr(self.status, "value") else str(self.status)
        return {
            "status": status_val,
            "resolved_order_ids": list(dict.fromkeys(self.resolved_order_ids)),
            "rejected_candidates": list(dict.fromkeys(self.rejected_candidates)),
            "confidence": round(max(0.0, min(1.0, float(self.confidence))), 4),
        }

    def to_contract_customer_context(self) -> dict[str, Any]:
        """Format strictly according to l3b-output-v2 customer_context schema."""
        return self.customer_context.to_contract_dict()


@dataclass(slots=True)
class OrderShipmentData:
    """Specialist deliverable for Person 2 (Order, Product & Shipment)."""

    affected_entities: dict[str, list[str]] = field(
        default_factory=lambda: {
            "order_ids": [],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        }
    )
    shipment_verdict: str = "insufficient_evidence"
    late_seller_ids: list[str] = field(default_factory=list)
    timeline_complete: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PaymentPolicyData:
    """Specialist deliverable for Person 3 (Payment, Refund & Policy)."""

    payment_verdict: str = "insufficient_evidence"
    captured_total_brl: float | None = None
    refunded_total_brl: float | None = None
    refundable_total_brl: float | None = None
    policy_recommendation: dict[str, Any] = field(default_factory=dict)
    data_conflicts: list[dict[str, Any]] = field(default_factory=list)
    financial_resolution: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SpecialistResult:
    """Unified result envelope returned by any specialist agent to Coordinator."""

    task_id: str
    case_id: str
    actor: str
    status: TaskStatus | str = TaskStatus.COMPLETED
    evidence_refs: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        status_val = self.status.value if hasattr(self.status, "value") else str(self.status)
        return {
            "task_id": self.task_id,
            "case_id": self.case_id,
            "actor": self.actor,
            "status": status_val,
            "evidence_refs": list(dict.fromkeys(self.evidence_refs)),
            "data": self.data,
            "errors": self.errors,
            "warnings": self.warnings,
        }
