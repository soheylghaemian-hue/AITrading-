"""Provider-neutral boundary with no network access or credential handling."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ProviderUnavailable(RuntimeError):
    pass


class ProviderProtocolError(RuntimeError):
    pass


class ModelProvider(Protocol):
    def complete(self, *, role: str, task: str, schema: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(slots=True)
class ScriptedProvider:
    """Deterministic, offline provider used by tests and an approved host adapter."""

    responses: list[dict[str, Any]]

    def complete(self, *, role: str, task: str, schema: dict[str, Any]) -> dict[str, Any]:
        del role, task, schema
        if not self.responses:
            raise ProviderUnavailable("scripted provider exhausted")
        response = self.responses.pop(0)
        if not isinstance(response, dict):
            raise ProviderProtocolError("provider response must be a JSON object")
        return response
