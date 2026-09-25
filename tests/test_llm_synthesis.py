from __future__ import annotations

import asyncio
from typing import Any

import pytest

from student_agent.llm_synthesis import (
    OPENROUTER_CHAT_URL,
    OPENROUTER_MODEL,
    OpenRouterSynthesizer,
)


class RecordingRequest:
    def __init__(self, content: dict[str, Any]) -> None:
        self.content = content
        self.calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    async def __call__(
        self, url: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((url, headers, payload))
        import json

        return {
            "choices": [
                {"message": {"content": json.dumps(self.content)}}
            ]
        }


def test_openrouter_uses_gpt_4o_mini_and_strict_structured_output() -> None:
    request = RecordingRequest(
        {
            "primary_issue": "refund_pending",
            "secondary_issues": ["late_delivery_logistics"],
            "case_status": "action_required",
            "confidence": 0.82,
            "ranked_cause_codes": ["REFUND_PROCESSING_PENDING", "LOGISTICS_TRANSIT_DELAY"],
        }
    )
    synthesizer = OpenRouterSynthesizer("sk-or-test", request=request)

    result = asyncio.run(
        synthesizer.synthesize(
            case={"case_id": "L3B_CASE_001", "customer_request": {"claims": []}},
            facts={"shipment_verdict": "logistics_delay"},
            allowed_issues=("refund_pending", "late_delivery_logistics"),
            allowed_cause_codes=("REFUND_PROCESSING_PENDING", "LOGISTICS_TRANSIT_DELAY"),
            confidence_ceiling=0.82,
        )
    )

    assert result.primary_issue == "refund_pending"
    assert result.confidence == 0.82
    assert len(request.calls) == 1
    url, headers, payload = request.calls[0]
    assert url == OPENROUTER_CHAT_URL
    assert headers["Authorization"] == "Bearer sk-or-test"
    assert payload["model"] == OPENROUTER_MODEL == "openai/gpt-4o-mini"
    assert payload["temperature"] == 0
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["provider"]["require_parameters"] is True


def test_openrouter_rejects_issue_not_supported_by_specialists() -> None:
    request = RecordingRequest(
        {
            "primary_issue": "duplicate_charge",
            "secondary_issues": [],
            "case_status": "action_required",
            "confidence": 0.9,
            "ranked_cause_codes": [],
        }
    )
    synthesizer = OpenRouterSynthesizer("sk-or-test", request=request)

    with pytest.raises(ValueError, match="primary_issue"):
        asyncio.run(
            synthesizer.synthesize(
                case={"case_id": "L3B_CASE_001"},
                facts={},
                allowed_issues=("refund_pending",),
                allowed_cause_codes=(),
                confidence_ceiling=0.8,
            )
        )


def test_openrouter_filters_duplicate_secondary_issues_and_caps_confidence() -> None:
    request = RecordingRequest(
        {
            "primary_issue": "refund_pending",
            "secondary_issues": [
                "refund_pending",
                "late_delivery_logistics",
                "late_delivery_logistics",
            ],
            "case_status": "action_required",
            "confidence": 0.99,
            "ranked_cause_codes": [
                "LOGISTICS_TRANSIT_DELAY",
                "LOGISTICS_TRANSIT_DELAY",
            ],
        }
    )
    synthesizer = OpenRouterSynthesizer("sk-or-test", request=request)

    result = asyncio.run(
        synthesizer.synthesize(
            case={"case_id": "L3B_CASE_001"},
            facts={},
            allowed_issues=("refund_pending", "late_delivery_logistics"),
            allowed_cause_codes=("LOGISTICS_TRANSIT_DELAY",),
            confidence_ceiling=0.75,
        )
    )

    assert result.secondary_issues == ("late_delivery_logistics",)
    assert result.ranked_cause_codes == ("LOGISTICS_TRANSIT_DELAY",)
    assert result.confidence == 0.75


def test_openrouter_rejects_unknown_cause_code() -> None:
    request = RecordingRequest(
        {
            "primary_issue": "refund_pending",
            "secondary_issues": [],
            "case_status": "action_required",
            "confidence": 0.7,
            "ranked_cause_codes": ["INVENTED_CAUSE"],
        }
    )
    synthesizer = OpenRouterSynthesizer("sk-or-test", request=request)

    with pytest.raises(ValueError, match="cause"):
        asyncio.run(
            synthesizer.synthesize(
                case={"case_id": "L3B_CASE_001"},
                facts={},
                allowed_issues=("refund_pending",),
                allowed_cause_codes=("REFUND_PROCESSING_PENDING",),
                confidence_ceiling=0.8,
            )
        )
