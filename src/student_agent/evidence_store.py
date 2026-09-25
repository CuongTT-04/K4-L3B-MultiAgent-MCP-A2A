from __future__ import annotations

import asyncio
import logging
from typing import Any

from .mcp_gateway import EvidenceGateway, MCPToolError
from .trace import TraceWriter

logger = logging.getLogger(__name__)


class EvidenceStore:
    """Centralized Evidence Repository, In-Memory Deduplication Cache & Safe Retry Handler.

    Maintains audit-compliant evidence references across all specialist agents,
    prevents duplicate MCP calls to preserve efficiency score, and manages trace events.
    """

    def __init__(self, gateway: EvidenceGateway, trace: TraceWriter | None = None) -> None:
        self.gateway = gateway
        self.trace = trace
        # Cache key: (case_id, tool_name, tuple(sorted(args.items()))) -> evidence object
        self._cache: dict[tuple[str, str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
        # Evidence by ref: evidence_ref -> evidence object
        self._evidence_by_ref: dict[str, dict[str, Any]] = {}
        # Case to refs mapping: case_id -> list of evidence_refs in order of consumption
        self._case_evidence_refs: dict[str, list[str]] = {}

    def _make_cache_key(
        self, case_id: str, tool_name: str, arguments: dict[str, str]
    ) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        sorted_args = tuple(sorted((str(k), str(v)) for k, v in arguments.items()))
        return (case_id, tool_name, sorted_args)

    async def call_tool_safe(
        self,
        tool_name: str,
        *,
        case_id: str,
        actor: str = "coordinator",
        max_retries: int = 2,
        initial_delay: float = 0.5,
        **arguments: str,
    ) -> dict[str, Any]:
        """Execute an MCP tool call with caching, retry logic, and trace emission.

        If already called for the same case with identical arguments, returns the cached
        evidence without making an extra network roundtrip.
        """
        cache_key = self._make_cache_key(case_id, tool_name, arguments)
        if cache_key in self._cache:
            cached_evidence = self._cache[cache_key]
            ref = cached_evidence.get("evidence_ref")
            if ref and ref not in self._case_evidence_refs.get(case_id, []):
                self._case_evidence_refs.setdefault(case_id, []).append(ref)
            logger.debug(f"[CACHE HIT] {tool_name} for case {case_id}")
            return cached_evidence

        delay = initial_delay
        last_exception: Exception | None = None

        for attempt in range(1, max_retries + 2):
            try:
                evidence = await self.gateway.call(tool_name, case_id=case_id, **arguments)
                evidence_ref = evidence.get("evidence_ref")

                # Cache evidence
                self._cache[cache_key] = evidence
                if evidence_ref:
                    self._evidence_by_ref[evidence_ref] = evidence
                    if evidence_ref not in self._case_evidence_refs.setdefault(case_id, []):
                        self._case_evidence_refs[case_id].append(evidence_ref)

                    # Emit trace event if trace writer is present
                    if self.trace is not None:
                        try:
                            self.trace.emit(
                                case_id=case_id,
                                event_type="tool_result_consumed",
                                actor=actor,
                                tool_name=tool_name,
                                evidence_refs=[evidence_ref],
                                attributes={
                                    "cached": False,
                                    "attempt": attempt,
                                },
                            )
                        except Exception as trace_err:
                            logger.warning(f"Trace emit failed: {trace_err}")

                return evidence

            except MCPToolError:
                logger.info("MCP %s rejected request for case %s; not retrying", tool_name, case_id)
                raise
            except Exception as exc:
                last_exception = exc
                if attempt <= max_retries:
                    logger.warning(
                        f"MCP {tool_name} failed (attempt {attempt}/{max_retries + 1}): {exc}. "
                        f"Retrying in {delay}s..."
                    )
                    await asyncio.sleep(delay)
                    delay *= 2.0
                else:
                    logger.error(
                        f"MCP {tool_name} failed permanently after {attempt} attempts: {exc}"
                    )

        raise RuntimeError(
            f"Tool {tool_name} failed for case {case_id}: {last_exception}"
        ) from last_exception

    def get_evidence_by_ref(self, evidence_ref: str) -> dict[str, Any] | None:
        """Lookup full evidence by reference ID."""
        return self._evidence_by_ref.get(evidence_ref)

    def get_case_evidence_refs(self, case_id: str) -> list[str]:
        """Return all distinct evidence refs consumed for the given case."""
        return list(dict.fromkeys(self._case_evidence_refs.get(case_id, [])))

    # Convenience shortcuts for Person 1, Person 2, and Person 3:

    async def get_order(
        self, case_id: str, order_id: str, actor: str = "entity_specialist"
    ) -> dict[str, Any]:
        """Fetch order evidence."""
        return await self.call_tool_safe(
            "get_order", case_id=case_id, actor=actor, order_id=order_id
        )

    async def get_customer_history(
        self, case_id: str, customer_unique_id: str, actor: str = "entity_specialist"
    ) -> dict[str, Any]:
        """Fetch customer purchase history evidence."""
        return await self.call_tool_safe(
            "get_customer_history",
            case_id=case_id,
            actor=actor,
            customer_unique_id=customer_unique_id,
        )
