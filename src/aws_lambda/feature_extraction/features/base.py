from __future__ import annotations
from typing import Dict, Any, Protocol


class BaseFeatureCalculator(Protocol):
    """Protocol for incremental feature calculators.

    Each calculator maintains its own mutable state (a dict).
    """

    name: str

    def init_state(self) -> Dict[str, Any]:  # noqa: D401
        return {}

    def update(self, state: Dict[str, Any], event: Dict[str, Any]) -> None:  # noqa: D401
        raise NotImplementedError

    def finalize(
        self, state: Dict[str, Any], shared: Dict[str, Any]
    ) -> Dict[str, Any]:  # noqa: D401
        raise NotImplementedError
