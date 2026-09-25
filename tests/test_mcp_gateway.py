from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from student_agent.contracts import Contracts
from student_agent.mcp_gateway import EvidenceGateway

EVIDENCE = {
    "schema_version": "day09-mcp-evidence-v1",
    "evidence_ref": "ev_gateway0000000000000001",
    "result_hash": "sha256:" + "a" * 64,
    "domain": "order",
    "data": {"order_id": "order-1"},
}


class FakeSession:
    def __init__(self, result: Any) -> None:
        self.result = result

    async def call_tool(self, tool_name: str, arguments: dict[str, str]) -> Any:
        return self.result


def gateway(result: Any) -> EvidenceGateway:
    root = Path(__file__).resolve().parents[1]
    contracts = Contracts(root / "contracts" / "schemas")
    return EvidenceGateway(FakeSession(result), contracts)  # type: ignore[arg-type]


def test_call_accepts_original_camel_case_result_fields() -> None:
    result = SimpleNamespace(
        isError=False,
        structuredContent=EVIDENCE,
        content=[],
    )

    evidence = asyncio.run(gateway(result).call("get_order", case_id="CASE_001", order_id="1"))

    assert evidence == EVIDENCE


def test_call_raises_runtime_error_for_mcp_v2_error_result() -> None:
    result = SimpleNamespace(
        isError=True,
        structuredContent=None,
        content=[SimpleNamespace(text="not found")],
    )

    with pytest.raises(RuntimeError, match="not found"):
        asyncio.run(gateway(result).call("get_order", case_id="CASE_001", order_id="1"))
