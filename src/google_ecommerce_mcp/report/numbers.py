"""The one definition of "a usable number" for the whole report package.

A value is a number only when it is an int or a float (never a bool, never a numeric string), finite, and
of a plausible size: ``abs(value) < MAX_ABS``. Anything else (NaN, infinity, 1e308, an integer of 400 digits
that would overflow ``float``) is invalid data and is shown as "not provided"; it never crashes a section and
never becomes a zero. MAX_ABS (1e15) is far above any real shop figure (impressions, revenue in any currency)
and keeps every product, sum or ratio computed from such values finite.
"""

from __future__ import annotations

import math
from typing import Any

MAX_ABS = 1e15


def is_num(value: Any) -> bool:
    """True for a finite int/float (bool excluded) with ``abs(value) < MAX_ABS``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        as_float = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(as_float) and abs(as_float) < MAX_ABS
