"""API key authentication dependency and device fingerprinting."""
from __future__ import annotations

import hashlib

from fastapi import Header, HTTPException, Request

# Stable-per-device fields (volume/brightness/lock-state/os-version change too
# often to identify a physical device).
_FINGERPRINT_FIELDS = (
    "name",
    "hostname",
    "model",
    "type",
    "screen_width",
    "screen_height",
)
_IDENTITY_FIELDS = ("name", "hostname", "model", "type")


def _screen_pair(device: dict) -> list[str]:
    """The two screen dimensions sorted numerically, so portrait/landscape
    (swapped width/height) produce the same fingerprint."""
    def as_num(value: object) -> int:
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return -1

    dims = [str(device.get("screen_width") or ""), str(device.get("screen_height") or "")]
    return sorted(dims, key=as_num)


def device_fingerprint(device: dict | None) -> tuple[str | None, str | None]:
    """Return ``(fingerprint_id, label)`` for a device dict, or ``(None, None)``.

    The fingerprint is a short hash of stable identifying fields; the label is a
    human-readable name for CLI listings. Screen dimensions are order-independent
    so the same device in portrait vs landscape isn't seen as two devices.
    """
    if not device:
        return None, None
    parts = [str(device.get(field) or "") for field in _IDENTITY_FIELDS]
    parts += _screen_pair(device)
    if not any(parts):
        return None, None
    fingerprint = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    label = device.get("name") or device.get("hostname") or device.get("model") or "device"
    return fingerprint, label


def device_fields(device: dict | None) -> dict:
    """The fingerprint source fields (for storing and diffing on rejection)."""
    if not device:
        return {}
    return {field: device.get(field) for field in _FINGERPRINT_FIELDS}


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    """Resolve the X-API-Key header to a configured key *name*.

    Raises 401 if the header is missing or unknown. The returned name is used
    for metrics labels and audit logging.
    """
    config = request.app.state.config
    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing X-API-Key header")
    name = config.key_name_for(x_api_key)
    if name is None:
        raise HTTPException(status_code=401, detail="invalid API key")
    request.state.api_key_name = name
    return name
