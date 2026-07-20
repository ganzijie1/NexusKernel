"""Small internal validators shared by framework-owned runtime boundaries."""

from __future__ import annotations

import math


def is_positive_finite_number(value: object) -> bool:
    """Return whether value is a non-boolean positive finite int or float."""

    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(value)
        and value > 0
    )


def require_positive_finite_number(value: object, name: str) -> None:
    if not is_positive_finite_number(value):
        raise ValueError(f"{name} must be a positive finite number")


def require_positive_integer(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
