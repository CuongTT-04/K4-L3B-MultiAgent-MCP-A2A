from __future__ import annotations

from abc import ABC, abstractmethod

from ..evidence_store import EvidenceStore
from ..models import AgentTask, SpecialistResult


class BaseSpecialist(ABC):
    """Abstract Base Class for all Domain Specialists in the Multi-Agent System."""

    def __init__(self, actor_name: str) -> None:
        self.actor_name = actor_name

    @abstractmethod
    async def execute(self, task: AgentTask, store: EvidenceStore) -> SpecialistResult:
        """Process the delegated task and return a unified SpecialistResult."""
        raise NotImplementedError
