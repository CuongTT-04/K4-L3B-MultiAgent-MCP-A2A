from __future__ import annotations

import os
from typing import Any

from .coordinator import CoordinatorDependencies, coordinate_case
from .llm_synthesis import OPENROUTER_MODEL, OpenRouterSynthesizer
from .mcp_gateway import EvidenceGateway
from .payment_agent import investigate_payment
from .specialists import EntitySpecialist, OrderShipmentSpecialist
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the evidence-backed specialists, OpenRouter synthesis and verifier."""
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required in .env")
    model = os.getenv("OPENROUTER_MODEL", OPENROUTER_MODEL).strip() or OPENROUTER_MODEL
    dependencies = CoordinatorDependencies(
        EntitySpecialist(),
        OrderShipmentSpecialist(),
        investigate_payment,
        OpenRouterSynthesizer(api_key, model=model),
    )
    return await coordinate_case(case, gateway, trace, dependencies)
