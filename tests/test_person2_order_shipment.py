from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from student_agent import evidence_store
from student_agent.contracts import Contracts
from student_agent.evidence_store import EvidenceStore
from student_agent.models import AgentTask, SpecialistResult, TaskStatus, TaskType
from student_agent.specialists.order_shipment_specialist import (
    ALLOWED_TOOLS,
    OrderShipmentSpecialist,
    OrderShipmentView,
    assess_delivery_claim,
    assess_order,
    parse_ts,
)
from student_agent.trace import TraceWriter

ROOT = Path(__file__).resolve().parents[1]
OPENED = datetime(2018, 3, 1, 9, 0)
ORDER_ID = "af0bbb47f125381ce9f3597dc70ef07b"


def order_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "order_id": ORDER_ID,
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-01-02 10:00:00",
        "order_delivered_carrier_date": "2018-01-04 15:00:00",
        "order_delivered_customer_date": "2018-01-12 18:00:00",
        "order_estimated_delivery_date": "2018-01-20 00:00:00",
    }
    row.update(overrides)
    return row


def items(limit: str = "2018-01-05 23:59:59") -> list[dict[str, Any]]:
    return [
        {"order_item_id": 1, "seller_id": "seller-a", "shipping_limit_date": limit},
        {"order_item_id": 2, "seller_id": "seller-b", "shipping_limit_date": "2018-01-10 00:00:00"},
    ]


def view(order: dict[str, Any], shipment: dict[str, Any] | None = None, **kw: Any):
    return OrderShipmentView(
        ORDER_ID, order=order, items=kw.get("items", items()), shipment=shipment or {}
    )


# ----------------------------------------------------------------- pure analysis


def test_on_time_delivery_has_complete_timeline_and_no_responsibility() -> None:
    result = assess_order(view(order_row()), opened_at=OPENED)
    assert result.verdict == "on_time"
    assert result.timeline_complete is True
    assert result.late_seller_ids == []
    assert result.responsible_parties == []


def test_delivery_on_estimated_day_is_on_time() -> None:
    order = order_row(order_delivered_customer_date="2018-01-20 21:00:00")
    assert assess_order(view(order), opened_at=OPENED).verdict == "on_time"


def test_late_delivery_with_late_carrier_handoff_blames_only_late_seller() -> None:
    order = order_row(
        order_delivered_carrier_date="2018-01-08 10:00:00",
        order_delivered_customer_date="2018-01-25 10:00:00",
    )
    result = assess_order(view(order), opened_at=OPENED)
    assert result.verdict == "seller_delay"
    assert result.late_seller_ids == ["seller-a"]
    assert result.responsible_parties == [{"party_type": "seller", "party_id": "seller-a"}]
    assert result.cause_codes == ["SELLER_LATE_HANDOFF"]


def test_late_delivery_with_timely_handoff_is_logistics_delay() -> None:
    order = order_row(order_delivered_customer_date="2018-01-25 10:00:00")
    result = assess_order(view(order, {"carrier_id": "carrier-9"}), opened_at=OPENED)
    assert result.verdict == "logistics_delay"
    assert result.late_seller_ids == []
    assert result.responsible_parties == [
        {"party_type": "logistics_provider", "party_id": "carrier-9"}
    ]


def test_undelivered_order_never_handed_off_past_limit_is_seller_delay() -> None:
    order = order_row(
        order_status="processing",
        order_delivered_carrier_date=None,
        order_delivered_customer_date=None,
    )
    result = assess_order(view(order), opened_at=OPENED)
    assert result.verdict == "seller_delay"
    assert set(result.late_seller_ids) == {"seller-a", "seller-b"}
    assert result.timeline_complete is False


def test_lost_and_returned_events_take_precedence() -> None:
    lost = assess_order(
        view(order_row(), {"events": [{"event_type": "LOST_IN_TRANSIT"}]}), opened_at=OPENED
    )
    returned = assess_order(
        view(order_row(), {"events": [{"type": "returned_to_seller"}]}), opened_at=OPENED
    )
    assert lost.verdict == "lost"
    assert lost.responsible_parties[0]["party_type"] == "logistics_provider"
    assert returned.verdict == "returned"


def test_missing_estimate_is_insufficient_not_guessed() -> None:
    order = order_row(order_estimated_delivery_date=None)
    result = assess_order(view(order), opened_at=OPENED)
    assert result.verdict == "insufficient_evidence"
    assert result.timeline_complete is False


def test_conflict_that_changes_verdict_is_unresolved_without_policy() -> None:
    shipment = {"order_delivered_customer_date": "2018-01-28 10:00:00"}
    result = assess_order(view(order_row(), shipment), opened_at=OPENED)
    assert result.verdict == "conflicting"
    assert result.conflicts == [
        {
            "field": "order_delivered_customer_date",
            "sources": ["get_order", "get_shipment_summary"],
            "selected_source": None,
            "resolution_code": "UNRESOLVED_SOURCE_CONFLICT",
        }
    ]


def test_conflict_resolved_by_policy_precedence() -> None:
    shipment = {"order_delivered_customer_date": "2018-01-28 10:00:00"}
    result = assess_order(
        view(order_row(), shipment),
        opened_at=OPENED,
        source_precedence=("get_shipment_summary", "get_order"),
    )
    assert result.verdict == "logistics_delay"
    assert result.conflicts[0]["selected_source"] == "get_shipment_summary"
    assert result.conflicts[0]["resolution_code"] == "SOURCE_PRECEDENCE_POLICY"


def test_harmless_conflict_is_recorded_but_keeps_verdict() -> None:
    shipment = {"order_delivered_customer_date": "2018-01-13 09:00:00"}
    result = assess_order(view(order_row(), shipment), opened_at=OPENED)
    assert result.verdict == "on_time"
    assert result.conflicts[0]["resolution_code"] == "NO_VERDICT_IMPACT"


def test_shipment_seller_handoffs_override_order_level_carrier_date() -> None:
    order = order_row(order_delivered_customer_date="2018-01-25 10:00:00")
    shipment = {
        "seller_handoffs": [
            {"seller_id": "seller-a", "handed_to_carrier_at": "2018-01-04 10:00:00"},
            {"seller_id": "seller-b", "handed_to_carrier_at": "2018-01-11 10:00:00"},
        ]
    }
    result = assess_order(view(order, shipment), opened_at=OPENED)
    assert result.verdict == "seller_delay"
    assert result.late_seller_ids == ["seller-b"]


def test_parse_ts_normalises_offsets_to_brazil_local() -> None:
    assert parse_ts("2018-01-01T12:00:00Z") == datetime(2018, 1, 1, 9, 0)
    assert parse_ts("2018-01-01T09:00:00-03:00") == datetime(2018, 1, 1, 9, 0)
    assert parse_ts("not a date") is None


@pytest.mark.parametrize(
    ("topic", "verdict", "expected"),
    [
        ("late_delivery_seller", "seller_delay", "supported"),
        ("late_delivery_seller", "logistics_delay", "unsupported"),
        ("late_delivery_logistics", "lost", "supported"),
        ("late_delivery_logistics", "conflicting", "insufficient_evidence"),
        ("requested_full_refund", "on_time", None),
    ],
)
def test_delivery_claim_mapping(topic: str, verdict: str, expected: str | None) -> None:
    assert assess_delivery_claim(topic, verdict) == expected


# ------------------------------------------------------------ agent + gateway


class FakeGateway:
    def __init__(self, data: dict[str, Any], failing: set[str] | None = None) -> None:
        self.data = data
        self.failing = failing or set()
        self.calls: list[tuple[str, str, str]] = []

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments["order_id"]))
        if tool_name in self.failing:
            raise RuntimeError(f"MCP tool {tool_name} failed: not found")
        digest = hashlib.sha256(f"{tool_name}{case_id}{arguments}".encode()).hexdigest()
        domain = {
            "get_order": "order",
            "get_order_items": "item",
            "get_sellers": "seller",
            "get_product_context": "product",
            "get_shipment_summary": "shipment",
        }
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{digest[:32]}",
            "result_hash": f"sha256:{digest}",
            "domain": domain[tool_name],
            "data": self.data[tool_name],
        }


def make_case(**scope: bool) -> dict[str, Any]:
    return {
        "case_id": "L3B_CASE_001",
        "opened_at": "2018-03-01T09:00:00-03:00",
        "customer_request": {
            "claims": [
                {"claim_id": "claim-001-a", "topic": "late_delivery_seller"},
                {"claim_id": "claim-001-b", "topic": "requested_full_refund"},
            ]
        },
        "investigation_scope": {"include_product_context": True, **scope},
    }


def gateway_data(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "get_order": order,
        "get_order_items": {"items": items()},
        "get_shipment_summary": {"shipment_id": "shp-1"},
        "get_sellers": [{"seller_id": "seller-a", "seller_state": "SP"}],
        "get_product_context": [{"product_id": "p1"}],
    }


@pytest.fixture
def trace(tmp_path: Path) -> TraceWriter:
    return TraceWriter(tmp_path / "trace.jsonl", Contracts(ROOT / "contracts" / "schemas"))


def read_trace(trace: TraceWriter) -> list[dict[str, Any]]:
    return [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]


def run_specialist(
    store: EvidenceStore, case: dict[str, Any], order_ids: list[str]
) -> SpecialistResult:
    task = AgentTask(
        task_id="task_shipment_L3B_CASE_001",
        case_id=case["case_id"],
        assigned_to="order_shipment_specialist",
        task_type=TaskType.INVESTIGATE_ORDER_SHIPMENT,
        input_data={"case": case, "resolved_order_ids": order_ids},
    )
    return asyncio.run(OrderShipmentSpecialist().execute(task, store))


def test_specialist_seller_delay_end_to_end(trace: TraceWriter) -> None:
    order = order_row(
        order_delivered_carrier_date="2018-01-08 10:00:00",
        order_delivered_customer_date="2018-01-25 10:00:00",
    )
    gateway = FakeGateway(gateway_data(order))
    store = EvidenceStore(gateway=gateway, trace=trace)
    result = run_specialist(store, make_case(), [ORDER_ID])

    assert result.status == TaskStatus.COMPLETED
    assert result.actor == "order_shipment_specialist"
    assert result.data["shipment_analysis"] == {
        "verdict": "seller_delay",
        "late_seller_ids": ["seller-a"],
        "timeline_complete": True,
    }
    assert result.data["affected_entities"] == {
        "order_ids": [ORDER_ID],
        "item_ids": [f"{ORDER_ID}:1", f"{ORDER_ID}:2"],
        "seller_ids": ["seller-a", "seller-b"],
        "payment_references": [],
        "shipment_ids": ["shp-1"],
    }
    assert result.data["claim_verdicts"] == {"claim-001-a": "supported"}
    assert {tool for tool, _, _ in gateway.calls} == ALLOWED_TOOLS
    assert all(case_id == "L3B_CASE_001" for _, case_id, _ in gateway.calls)
    assert set(result.evidence_refs) == set(store.get_case_evidence_refs("L3B_CASE_001"))

    events = read_trace(trace)
    consumed = [e for e in events if e["event_type"] == "tool_result_consumed"]
    assert len(consumed) == 5
    assert all(e["actor"] == "order_shipment_specialist" for e in consumed)
    assert events[-1]["event_type"] == "handoff"
    assert events[-1]["target"] == "coordinator"
    assert events[-1]["decision_code"] == "SHIPMENT_SELLER_DELAY"


def test_specialist_skips_optional_tools_when_not_needed(trace: TraceWriter) -> None:
    gateway = FakeGateway(gateway_data(order_row()))
    store = EvidenceStore(gateway=gateway, trace=trace)
    result = run_specialist(store, make_case(include_product_context=False), [ORDER_ID])
    assert result.data["shipment_analysis"]["verdict"] == "on_time"
    assert result.data["claim_verdicts"] == {"claim-001-a": "unsupported"}
    assert {tool for tool, _, _ in gateway.calls} == {
        "get_order",
        "get_order_items",
        "get_shipment_summary",
    }


def test_get_order_from_entity_specialist_is_not_repeated(trace: TraceWriter) -> None:
    gateway = FakeGateway(gateway_data(order_row()))
    store = EvidenceStore(gateway=gateway, trace=trace)
    asyncio.run(store.get_order(case_id="L3B_CASE_001", order_id=ORDER_ID))
    run_specialist(store, make_case(include_product_context=False), [ORDER_ID])
    assert [tool for tool, _, _ in gateway.calls].count("get_order") == 1
    assert len(gateway.calls) == 3


def test_missing_order_degrades_to_insufficient_evidence(
    trace: TraceWriter, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(evidence_store.asyncio, "sleep", no_sleep)
    gateway = FakeGateway(gateway_data(order_row()), failing={"get_order"})
    store = EvidenceStore(gateway=gateway, trace=trace)
    result = run_specialist(store, make_case(), [ORDER_ID])
    assert result.status == TaskStatus.FAILED
    assert result.data["shipment_analysis"] == {
        "verdict": "insufficient_evidence",
        "late_seller_ids": [],
        "timeline_complete": False,
    }
    assert result.data["affected_entities"]["order_ids"] == []
    assert result.data["confidence"] <= 0.5
    assert result.errors


def test_no_resolved_orders_makes_no_calls(trace: TraceWriter) -> None:
    gateway = FakeGateway(gateway_data(order_row()))
    result = run_specialist(EvidenceStore(gateway=gateway, trace=trace), make_case(), [])
    assert gateway.calls == []
    assert result.data["shipment_analysis"]["verdict"] == "insufficient_evidence"
    assert result.warnings
