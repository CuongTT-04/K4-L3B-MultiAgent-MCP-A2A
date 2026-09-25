from __future__ import annotations

import logging
import re
from typing import Any

from ..evidence_store import EvidenceStore
from ..models import (
    AgentTask,
    CustomerContextData,
    EntityResolutionData,
    EntityResolutionStatus,
    SpecialistResult,
    TaskStatus,
)
from .base import BaseSpecialist

logger = logging.getLogger(__name__)

_PLACEHOLDER_CANDIDATE = re.compile(r"^candidate-\d+$", re.IGNORECASE)


class EntitySpecialist(BaseSpecialist):
    """Person 1 Domain Specialist: Candidate Ranking, Entity Resolution & Customer History.

    MCP Tools used:
    - `get_order`
    - `get_customer_history`
    """

    def __init__(self, actor_name: str = "entity_specialist") -> None:
        super().__init__(actor_name=actor_name)

    async def execute(self, task: AgentTask, store: EvidenceStore) -> SpecialistResult:
        case_data = task.input_data
        case_id = task.case_id
        customer_req = case_data.get("customer_request", {})
        claimed_order_id = customer_req.get("claimed_order_id")
        candidate_order_ids = case_data.get("candidate_order_ids", [])
        customer_hint = case_data.get("customer_unique_id_hint")

        # Compile candidate pool in order
        candidates_to_check: list[str] = []
        if claimed_order_id and claimed_order_id not in candidates_to_check:
            candidates_to_check.append(claimed_order_id)
        for cand in candidate_order_ids:
            if cand and cand not in candidates_to_check:
                candidates_to_check.append(cand)

        resolved_candidates: list[dict[str, Any]] = []
        rejected_candidates: list[str] = []
        consumed_refs: list[str] = []
        errors: list[str] = []
        notes: list[str] = []

        # 1. Probe each candidate order using get_order
        for cand_id in candidates_to_check:
            if _PLACEHOLDER_CANDIDATE.fullmatch(cand_id):
                rejected_candidates.append(cand_id)
                notes.append(f"Candidate {cand_id} rejected as a synthetic placeholder")
                continue
            try:
                evidence = await store.get_order(
                    case_id=case_id, order_id=cand_id, actor=self.actor_name
                )
                ref = evidence.get("evidence_ref")
                if ref:
                    consumed_refs.append(ref)

                order_data = evidence.get("data", {})
                score = 1.0
                cand_customer_uid = (
                    order_data.get("customer_unique_id")
                    or order_data.get("customer", {}).get("customer_unique_id")
                )

                if customer_hint and cand_customer_uid == customer_hint:
                    score += 10.0
                    notes.append(f"Candidate {cand_id} matches customer_hint ({customer_hint})")
                elif customer_hint and cand_customer_uid and cand_customer_uid != customer_hint:
                    score -= 5.0
                    rejected_candidates.append(cand_id)
                    notes.append(
                        f"Candidate {cand_id} rejected: customer mismatch "
                        f"({cand_customer_uid} != {customer_hint})"
                    )
                    continue

                if cand_id == claimed_order_id:
                    score += 3.0

                resolved_candidates.append({
                    "order_id": cand_id,
                    "score": score,
                    "customer_unique_id": cand_customer_uid,
                    "order_data": order_data,
                })

            except Exception as exc:
                logger.info(f"Candidate order {cand_id} failed get_order probe: {exc}")
                rejected_candidates.append(cand_id)
                notes.append(f"Candidate {cand_id} rejected due to probe failure: {exc}")

        # 2. Rank candidates and determine resolution status
        status = EntityResolutionStatus.NOT_FOUND
        resolved_order_ids: list[str] = []
        target_customer_uid: str | None = customer_hint
        confidence = 0.0

        if resolved_candidates:
            # Sort by score descending
            resolved_candidates.sort(key=lambda item: item["score"], reverse=True)
            top = resolved_candidates[0]

            is_single = len(resolved_candidates) == 1
            has_clear_lead = not is_single and top["score"] > resolved_candidates[1]["score"]

            if is_single or has_clear_lead:
                status = EntityResolutionStatus.RESOLVED
                resolved_order_ids = [top["order_id"]]
                confidence = 0.95
                if top["customer_unique_id"]:
                    target_customer_uid = top["customer_unique_id"]
                # Reject remaining lower scoring candidates
                for lower in resolved_candidates[1:]:
                    if lower["order_id"] not in rejected_candidates:
                        rejected_candidates.append(lower["order_id"])
            else:
                # Multiple candidates tied for top score
                status = EntityResolutionStatus.AMBIGUOUS
                resolved_order_ids = [cand["order_id"] for cand in resolved_candidates]
                confidence = 0.50
        else:
            status = EntityResolutionStatus.NOT_FOUND
            resolved_order_ids = []
            confidence = 0.10
            rejected_candidates = list(dict.fromkeys(candidates_to_check))

        # 3. Customer context investigation
        related_order_ids: list[str] = list(resolved_order_ids)

        if target_customer_uid:
            try:
                hist_evidence = await store.get_customer_history(
                    case_id=case_id,
                    customer_unique_id=target_customer_uid,
                    actor=self.actor_name,
                )
                ref = hist_evidence.get("evidence_ref")
                if ref:
                    consumed_refs.append(ref)

                hist_data = hist_evidence.get("data", {})
                raw_orders = hist_data.get("order_ids") or hist_data.get("orders") or []
                if isinstance(raw_orders, list):
                    for item in raw_orders:
                        if isinstance(item, str) and item not in related_order_ids:
                            related_order_ids.append(item)
                        elif isinstance(item, dict) and "order_id" in item:
                            oid = item["order_id"]
                            if oid and oid not in related_order_ids:
                                related_order_ids.append(oid)
            except Exception as exc:
                notes.append(f"get_customer_history failed for {target_customer_uid}: {exc}")

        customer_ctx = CustomerContextData(
            customer_unique_id=target_customer_uid,
            related_order_ids=list(dict.fromkeys(related_order_ids)),
        )

        entity_res = EntityResolutionData(
            status=status,
            resolved_order_ids=list(dict.fromkeys(resolved_order_ids)),
            rejected_candidates=list(dict.fromkeys(rejected_candidates)),
            confidence=confidence,
            customer_context=customer_ctx,
            notes=notes,
        )

        return SpecialistResult(
            task_id=task.task_id,
            case_id=case_id,
            actor=self.actor_name,
            status=TaskStatus.COMPLETED,
            evidence_refs=list(dict.fromkeys(consumed_refs)),
            data={
                "entity_resolution": entity_res.to_contract_entity_resolution(),
                "customer_context": entity_res.to_contract_customer_context(),
                "notes": notes,
            },
            errors=errors,
        )
