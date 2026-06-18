"""Shortcut client version gating.

The minimum supported Shortcut version is defined here in code. Bump
``MIN_SHORTCUT_VERSION`` when a new Shortcut release requires server changes that
older Shortcuts can't speak to.
"""
from __future__ import annotations

MIN_SHORTCUT_VERSION = "0.1.0"


def parse_version(value: str) -> tuple[int, ...]:
    """Parse a dotted version string into a tuple of ints (lenient)."""
    parts: list[int] = []
    for chunk in value.strip().split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_supported(version: str, minimum: str = MIN_SHORTCUT_VERSION) -> bool:
    """Return True if ``version`` is >= ``minimum`` (zero-padded compare)."""
    try:
        a, b = parse_version(version), parse_version(minimum)
        length = max(len(a), len(b))
        a += (0,) * (length - len(a))
        b += (0,) * (length - len(b))
        return a >= b
    except Exception:
        return False
