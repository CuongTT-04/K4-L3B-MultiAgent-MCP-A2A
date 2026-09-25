from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx2

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "openai/gpt-4o-mini"

PRIMARY_ISSUES = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
)
CASE_STATUSES = ("action_required", "no_action", "needs_investigation")

RequestFunction = Callable[
    [str, dict[str, str], dict[str, Any]], Awaitable[dict[str, Any]]
]


@dataclass(frozen=True)
class SynthesisDecision:
    primary_issue: str
    secondary_issues: tuple[str, ...]
    case_status: str
    confidence: float
    ranked_cause_codes: tuple[str, ...]


async def _post_json(
    url: str, headers: dict[str, str], payload: dict[str, Any]
) -> dict[str, Any]:
    timeout = httpx2.Timeout(60.0, connect=20.0, write=20.0, pool=20.0)
    async with httpx2.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        value = response.json()
    if not isinstance(value, dict):
        raise ValueError("OpenRouter response must be a JSON object")
    return value


class OpenRouterSynthesizer:
    """One structured OpenRouter decision over already verified specialist facts."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = OPENROUTER_MODEL,
        request: RequestFunction | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OPENROUTER_API_KEY is required")
        self.api_key = api_key.strip()
        self.model = model
        self.request = request or _post_json

    async def synthesize(
        self,
        *,
        case: Mapping[str, Any],
        facts: Mapping[str, Any],
        allowed_issues: Sequence[str],
        allowed_cause_codes: Sequence[str],
        confidence_ceiling: float,
    ) -> SynthesisDecision:
        issues = tuple(dict.fromkeys(str(value) for value in allowed_issues))
        causes = tuple(dict.fromkeys(str(value) for value in allowed_cause_codes))
        if not issues:
            raise ValueError("allowed_issues must not be empty")
        if any(value not in PRIMARY_ISSUES for value in issues):
            raise ValueError("allowed_issues contains an invalid primary issue")

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Select a case assessment using only the supplied specialist facts and "
                        "allowed values. Do not invent identifiers, evidence, money, actions, or "
                        "causes. Return only the requested JSON object without reasoning text."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "case_id": case.get("case_id"),
                            "customer_claims": (case.get("customer_request") or {}).get(
                                "claims", []
                            ),
                            "specialist_facts": facts,
                            "allowed_primary_issues": issues,
                            "allowed_cause_codes": causes,
                            "confidence_ceiling": confidence_ceiling,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": 500,
            "provider": {"require_parameters": True},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "l3b_case_synthesis",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "primary_issue",
                            "secondary_issues",
                            "case_status",
                            "confidence",
                            "ranked_cause_codes",
                        ],
                        "properties": {
                            "primary_issue": {"type": "string", "enum": list(PRIMARY_ISSUES)},
                            "secondary_issues": {
                                "type": "array",
                                "maxItems": 10,
                                "items": {"type": "string", "enum": list(PRIMARY_ISSUES)},
                            },
                            "case_status": {"type": "string", "enum": list(CASE_STATUSES)},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "ranked_cause_codes": {
                                "type": "array",
                                "maxItems": 5,
                                "items": {
                                    "type": "string",
                                    "pattern": "^[A-Z][A-Z0-9_]{2,79}$",
                                },
                            },
                        },
                    },
                },
            },
        }
        response = await self.request(
            OPENROUTER_CHAT_URL,
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://vinaction.ai",
                "X-OpenRouter-Title": "K4 L3B Multi-Agent MCP A2A",
            },
            payload,
        )
        content = self._content(response)
        decision = json.loads(content)
        if not isinstance(decision, dict):
            raise ValueError("OpenRouter structured content must be an object")
        return self._validate_decision(decision, issues, causes, confidence_ceiling)

    @staticmethod
    def _content(response: Mapping[str, Any]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("OpenRouter response has no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise ValueError("OpenRouter response has no text content")
        return content

    @staticmethod
    def _validate_decision(
        value: Mapping[str, Any],
        allowed_issues: tuple[str, ...],
        allowed_causes: tuple[str, ...],
        confidence_ceiling: float,
    ) -> SynthesisDecision:
        primary = value.get("primary_issue")
        if primary not in allowed_issues:
            raise ValueError("OpenRouter primary_issue is not supported by specialist facts")
        raw_secondary = value.get("secondary_issues")
        if not isinstance(raw_secondary, list) or any(
            issue not in allowed_issues for issue in raw_secondary
        ):
            raise ValueError("OpenRouter secondary issue is not supported by specialist facts")
        status = value.get("case_status")
        if status not in CASE_STATUSES:
            raise ValueError("OpenRouter case_status is invalid")
        confidence = value.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            raise ValueError("OpenRouter confidence is invalid")
        raw_causes = value.get("ranked_cause_codes")
        if not isinstance(raw_causes, list) or any(
            cause not in allowed_causes for cause in raw_causes
        ):
            raise ValueError("OpenRouter cause is not supported by specialist facts")

        secondary = tuple(
            dict.fromkeys(str(issue) for issue in raw_secondary if issue != primary)
        )
        ranked_causes = tuple(dict.fromkeys(str(cause) for cause in raw_causes))
        ceiling = max(0.0, min(1.0, float(confidence_ceiling)))
        return SynthesisDecision(
            primary_issue=str(primary),
            secondary_issues=secondary,
            case_status=str(status),
            confidence=round(min(max(0.0, float(confidence)), ceiling), 4),
            ranked_cause_codes=ranked_causes,
        )
