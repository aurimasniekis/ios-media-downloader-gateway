"""Pydantic request/response schemas."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Device(BaseModel):
    """Client device metadata sent by the iOS Shortcut.

    Extra/unknown keys are kept so the Shortcut can add fields without breaking
    older servers; everything is optional.
    """

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    type: str | None = None
    os: str | None = None
    system_version: str | None = None
    system_build_number: str | None = None
    model: str | None = None
    hostname: str | None = None
    details: str | None = None
    is_locked: str | None = None
    current_volume: str | None = None
    current_brightness: str | None = None
    current_appearance: str | None = None
    screen_width: str | None = None
    screen_height: str | None = None


class UrlRequest(BaseModel):
    url: str
    device: Device | None = None


class DownloadRequest(BaseModel):
    url: str
    format_id: str
    device: Device | None = None


class CheckRequest(BaseModel):
    version: str
    device: Device | None = None


class CheckResponse(BaseModel):
    status: str = "ok"
    supported: bool = True
    version: str
    min_version: str
    message: str


class ErrorResponse(BaseModel):
    status: str = "error"
    error: str
    error_details: dict = Field(default_factory=dict)


class DownloadItem(BaseModel):
    item_index: int
    media_type: str
    title: str | None = None
    format_id: str | None = None
    ext: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    filesize: int | None = None
    mimetype: str | None = None
    suggested_filename: str | None = None
    download_url: str
    http_headers: dict[str, str] = Field(default_factory=dict)
    cookies: str = ""


class MediaEnvelope(BaseModel):
    status: str = "ok"
    title: str | None = None
    site: str
    kind: str
    count: int
    items: list[DownloadItem]


class FormatChoice(BaseModel):
    format_id: str
    label: str | None = None  # human-readable, e.g. "720p 20MB (watermarked)"
    ext: str | None = None
    resolution: str | None = None
    fps: float | None = None
    vcodec: str | None = None
    acodec: str | None = None
    tbr: float | None = None
    filesize: int | None = None
    note: str | None = None
    recommended: bool = False


class FormatsEnvelope(BaseModel):
    status: str = "ok"
    title: str | None = None
    site: str
    kind: str = "formats"
    count: int
    choices: list[FormatChoice]
