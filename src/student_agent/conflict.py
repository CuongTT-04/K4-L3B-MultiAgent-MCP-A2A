from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def _normalize(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    field = value.get("field")
    raw_sources = value.get("sources")
    selected = value.get("selected_source")
    code = value.get("resolution_code")
    if not isinstance(field, str) or not field:
        return None
    if not isinstance(raw_sources, list | tuple):
        return None
    sources = list(dict.fromkeys(source for source in raw_sources if isinstance(source, str)))
    if len(sources) < 2:
        return None
    if selected is not None and (not isinstance(selected, str) or selected not in sources):
        return None
    if not isinstance(code, str) or not code:
        return None
    return {
        "field": field,
        "sources": sources[:5],
        "selected_source": selected,
        "resolution_code": code,
    }


def merge_conflicts(*groups: Iterable[Any]) -> list[dict[str, Any]]:
    """Return at most five valid, distinct conflicts in first-seen order."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for group in groups:
        for value in group:
            conflict = _normalize(value)
            if conflict is None:
                continue
            key = (
                conflict["field"],
                tuple(conflict["sources"]),
                conflict["selected_source"],
                conflict["resolution_code"],
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(conflict)
            if len(result) == 5:
                return result
    return result


def has_unresolved_conflict(conflicts: Iterable[Mapping[str, Any]]) -> bool:
    return any(conflict.get("selected_source") is None for conflict in conflicts)
