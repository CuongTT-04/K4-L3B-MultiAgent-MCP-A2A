from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from student_agent.evidence_store import EvidenceStore
from student_agent.models import (
    AgentTask,
    CustomerContextData,
    EntityResolutionData,
    EntityResolutionStatus,
    SpecialistResult,
    TaskStatus,
    TaskType,
)
from student_agent.specialists.entity_specialist import EntitySpecialist


def test_models_contracts_compliance() -> None:
    """Ensure Person 1 models format strictly according to L3B schema specifications."""
    customer_ctx = CustomerContextData(
        customer_unique_id="cust-12345",
        related_order_ids=["ord-1", "ord-2"],
    )
    entity_data = EntityResolutionData(
        status=EntityResolutionStatus.RESOLVED,
        resolved_order_ids=["ord-1"],
        rejected_candidates=["cand-bad"],
        confidence=0.95,
        customer_context=customer_ctx,
    )

    resolved_dict = entity_data.to_contract_entity_resolution()
    assert resolved_dict["status"] == "resolved"
    assert resolved_dict["resolved_order_ids"] == ["ord-1"]
    assert resolved_dict["rejected_candidates"] == ["cand-bad"]
    assert resolved_dict["confidence"] == 0.95

    ctx_dict = entity_data.to_contract_customer_context()
    assert ctx_dict["customer_unique_id"] == "cust-12345"
    assert ctx_dict["related_order_ids"] == ["ord-1", "ord-2"]


def test_evidence_store_caching_prevents_duplicate_calls() -> None:
    """Ensure EvidenceStore caches repeated calls for the same case and arguments."""
    async def _run() -> None:
        mock_gateway = AsyncMock()
        mock_gateway.call = AsyncMock(
            return_value={
                "schema_version": "day09-mcp-evidence-v1",
                "evidence_ref": "ev_test_123456789012345678901234",
                "result_hash": "sha256:" + "a" * 64,
                "domain": "order",
                "data": {"order_id": "ord-001", "status": "delivered"},
            }
        )

        store = EvidenceStore(gateway=mock_gateway, trace=None)

        # First call - hits gateway
        res1 = await store.get_order(case_id="CASE_TEST", order_id="ord-001")
        # Second call with same arguments - should hit cache
        res2 = await store.get_order(case_id="CASE_TEST", order_id="ord-001")

        assert res1 == res2
        assert mock_gateway.call.call_count == 1
        assert store.get_case_evidence_refs("CASE_TEST") == ["ev_test_123456789012345678901234"]

    import asyncio
    asyncio.run(_run())


def test_evidence_store_retry_success() -> None:
    """Ensure EvidenceStore retries on transient errors."""
    async def _run() -> None:
        mock_gateway = AsyncMock()
        expected_evidence = {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": "ev_retry_123456789012345678901234",
            "result_hash": "sha256:" + "b" * 64,
            "domain": "order",
            "data": {"order_id": "ord-002"},
        }
        # Fail first, succeed second
        mock_gateway.call = AsyncMock(
            side_effect=[RuntimeError("Temporary network timeout"), expected_evidence]
        )

        store = EvidenceStore(gateway=mock_gateway, trace=None)
        res = await store.get_order(case_id="CASE_RETRY", order_id="ord-002")

        assert res == expected_evidence
        assert mock_gateway.call.call_count == 2

    import asyncio
    asyncio.run(_run())


def test_entity_specialist_resolution_and_ranking() -> None:
    """Test candidate ranking, resolving valid order, and customer context."""
    async def _run() -> None:
        mock_gateway = AsyncMock()

        async def fake_call(tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
            if tool_name == "get_order":
                order_id = arguments.get("order_id")
                if order_id == "good-order-1":
                    return {
                        "schema_version": "day09-mcp-evidence-v1",
                        "evidence_ref": "ev_good_123456789012345678901234",
                        "result_hash": "sha256:" + "c" * 64,
                        "domain": "order",
                        "data": {
                            "order_id": "good-order-1",
                            "customer_unique_id": "customer-target-001",
                        },
                    }
                raise RuntimeError(f"Order {order_id} not found in database")
            elif tool_name == "get_customer_history":
                return {
                    "schema_version": "day09-mcp-evidence-v1",
                    "evidence_ref": "ev_cust_123456789012345678901234",
                    "result_hash": "sha256:" + "d" * 64,
                    "domain": "customer",
                    "data": {
                        "customer_unique_id": arguments.get("customer_unique_id"),
                        "order_ids": ["good-order-1", "older-order-99"],
                    },
                }
            raise ValueError(f"Unknown tool: {tool_name}")

        mock_gateway.call = AsyncMock(side_effect=fake_call)
        store = EvidenceStore(gateway=mock_gateway, trace=None)

        task = AgentTask(
            task_id="task_entity_01",
            case_id="L3B_CASE_001",
            assigned_to="entity_specialist",
            task_type=TaskType.RESOLVE_ENTITY,
            input_data={
                "customer_request": {
                    "claimed_order_id": "good-order-1",
                },
                "candidate_order_ids": ["good-order-1", "bad-candidate-999"],
                "customer_unique_id_hint": "customer-target-001",
            },
        )

        specialist = EntitySpecialist()
        result = await specialist.execute(task, store)

        assert isinstance(result, SpecialistResult)
        assert result.status == TaskStatus.COMPLETED
        assert "ev_good_123456789012345678901234" in result.evidence_refs
        assert "ev_cust_123456789012345678901234" in result.evidence_refs

        data = result.data
        res_info = data["entity_resolution"]
        assert res_info["status"] == "resolved"
        assert res_info["resolved_order_ids"] == ["good-order-1"]
        assert "bad-candidate-999" in res_info["rejected_candidates"]
        assert res_info["confidence"] >= 0.9

        cust_info = data["customer_context"]
        assert cust_info["customer_unique_id"] == "customer-target-001"
        assert "good-order-1" in cust_info["related_order_ids"]
        assert "older-order-99" in cust_info["related_order_ids"]

    import asyncio
    asyncio.run(_run())

