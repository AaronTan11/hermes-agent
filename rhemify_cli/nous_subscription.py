"""Subscription feature stub.

This module historically provided subscription-managed tool features.
For this personal deployment those are unused — all functions are
no-ops that return empty/false values so callers don't break.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class _Feature:
    key: str = ""
    label: str = ""
    active: bool = False
    managed_by_nous: bool = False
    included_by_default: bool = False
    current_provider: str = ""


@dataclass
class _Features:
    nous_auth_present: bool = False
    _items: List[_Feature] = field(default_factory=list)

    def items(self):
        return self._items


def get_nous_subscription_features(config=None) -> _Features:
    """Return empty subscription features (no-op)."""
    return _Features()


def get_nous_subscription_explainer_lines() -> List[str]:
    """Return empty list — no subscription explainer to show."""
    return []


def apply_nous_provider_defaults(*args, **kwargs) -> None:
    """No-op."""
    return None


def apply_nous_managed_defaults(*args, **kwargs) -> None:
    """No-op."""
    return None
