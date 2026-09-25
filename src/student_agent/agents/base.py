from __future__ import annotations

from typing import Any, Protocol

from ..trace import TraceWriter


class ToolGateway(Protocol):
    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]: ...


EvidenceCache = dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]]


class ScopedToolClient:
    """Least-privilege, case-scoped view of the MCP gateway for one actor.

    - only tools in ``allowed_tools`` can be called;
    - every call is bound to one ``case_id``;
    - identical calls are served from ``cache`` (share one cache per case across agents);
    - each consumed evidence emits exactly one ``tool_result_consumed`` event.
    """

    def __init__(
        self,
        gateway: ToolGateway,
        trace: TraceWriter,
        *,
        case_id: str,
        actor: str,
        allowed_tools: frozenset[str],
        cache: EvidenceCache | None = None,
    ) -> None:
        self._gateway = gateway
        self._trace = trace
        self.case_id = case_id
        self.actor = actor
        self.allowed_tools = allowed_tools
        self._cache: EvidenceCache = cache if cache is not None else {}
        self._consumed: set[str] = set()

    async def call(self, tool_name: str, **arguments: str) -> dict[str, Any]:
        if tool_name not in self.allowed_tools:
            raise PermissionError(f"{self.actor} is not allowed to call {tool_name}")
        key = (tool_name, tuple(sorted(arguments.items())))
        evidence = self._cache.get(key)
        if evidence is None:
            evidence = await self._gateway.call(tool_name, case_id=self.case_id, **arguments)
            self._cache[key] = evidence
        ref = evidence["evidence_ref"]
        if ref not in self._consumed:
            self._consumed.add(ref)
            self._trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=self.actor,
                tool_name=tool_name,
                evidence_refs=[ref],
            )
        return evidence
