from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from student_agent.cases import CaseSet
from student_agent.contracts import Contracts
from student_agent.coordinator import PIPELINE_TOOLS, undiscovered_tools
from student_agent.submission import SIMULATED_REF_PREFIX, validate_artifacts

ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "L3B_CASE_001"


def output(evidence_ref: str) -> dict[str, Any]:
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": CASE_ID,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "secondary_issues": [],
            "case_status": "needs_investigation",
            "confidence": 0.1,
        },
        "affected_entities": {
            "order_ids": [], "item_ids": [], "seller_ids": [],
            "payment_references": [], "shipment_ids": [],
        },
        "entity_resolution": {
            "status": "not_found", "resolved_order_ids": [],
            "rejected_candidates": [], "confidence": 0.1,
        },
        "customer_context": {"customer_unique_id": None, "related_order_ids": []},
        "shipment_analysis": {
            "verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False,
        },
        "payment_analysis": {
            "verdict": "insufficient_evidence", "captured_total_brl": None,
            "refunded_total_brl": None, "refundable_total_brl": None,
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": [evidence_ref],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": [],
        },
        "resolution_actions": [],
    }


def artifacts(tmp_path: Path, evidence_ref: str, trace_extra: str = "") -> None:
    (tmp_path / "outputs").mkdir()
    (tmp_path / "traces").mkdir()
    (tmp_path / "outputs" / f"{CASE_ID}.json").write_text(
        json.dumps(output(evidence_ref)), encoding="utf-8"
    )
    event = {
        "schema_version": "day09-trace-event-v1",
        "event_id": "evt_abcdefghijklmnop",
        "case_id": CASE_ID,
        "event_type": "case_received",
        "occurred_at": "2026-09-25T10:00:00Z",
        "actor": "coordinator",
    }
    if trace_extra:
        event["attributes"] = {"note": trace_extra}
    (tmp_path / "traces" / "trace.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")


def check(tmp_path: Path, **kwargs: Any) -> None:
    case_set = CaseSet("test-v1", "l3b", (CASE_ID,), {})
    validate_artifacts(tmp_path, case_set, Contracts(ROOT / "contracts" / "schemas"), **kwargs)


def test_real_looking_refs_pass(tmp_path: Path) -> None:
    artifacts(tmp_path, "ev_S_vc47IG3WIQuXGbNWLNcAWIh5HDJJ0V")
    check(tmp_path)


def test_simulated_refs_can_never_be_packaged(tmp_path: Path) -> None:
    artifacts(tmp_path, f"{SIMULATED_REF_PREFIX}abcdefghijklmnop")
    with pytest.raises(ValueError, match="simulated evidence refs"):
        check(tmp_path)
    check(tmp_path, allow_simulated=True)


def test_model_api_key_is_blocked(tmp_path: Path) -> None:
    artifacts(
        tmp_path, "ev_S_vc47IG3WIQuXGbNWLNcAWIh5HDJJ0V", trace_extra="sk-or-v1-0123456789abcdef"
    )
    with pytest.raises(ValueError, match="API key"):
        check(tmp_path)


def test_tool_discovery_reports_missing_pipeline_tools() -> None:
    assert undiscovered_tools(sorted(PIPELINE_TOOLS)) == []
    assert undiscovered_tools(["get_order", "get_policy"]) == sorted(
        PIPELINE_TOOLS - {"get_order", "get_policy"}
    )
