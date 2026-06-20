"""Load and validate the TOML configuration into pydantic models."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_CONFIG_PATH = "./data/config.toml"


class ApiKey(BaseModel):
    name: str
    key: str
    max_devices: int = 0  # 0 = unlimited distinct devices


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    json_logs: bool = False
    db_path: str = "./data/audit.sqlite3"
    cache_ttl_seconds: int = 300
    ffmpeg_path: str = "ffmpeg"
    public_base_url: str = ""  # override the /v1/stream base URL (else derived from request)
    merge_url_ttl_seconds: int = 21600  # how long a /v1/stream token stays valid
    merge_max_height: int = 1080


class FormatFilter(BaseModel):
    prefer_vcodec: list[str] = Field(default_factory=lambda: ["avc1", "h264"])
    prefer_acodec: list[str] = Field(default_factory=lambda: ["mp4a", "aac"])
    prefer_ext: list[str] = Field(default_factory=lambda: ["mp4", "m4a", "jpg"])


class ExtractorConfig(BaseModel):
    """yt-dlp auth for video extraction.

    Some sites need cookies (age-gated or login-required YouTube, private
    posts). Supply a Netscape ``cookies.txt`` file or a browser to pull cookies
    from. Both empty => no cookies (yt-dlp's default).
    """

    cookies: str = ""
    cookies_from_browser: str = ""


class GalleryConfig(BaseModel):
    """gallery-dl auth/config for photo posts and image galleries.

    Some sites (notably Instagram) require login. Supply a cookies file, a
    browser to pull cookies from, or a full gallery-dl JSON config. All empty =>
    use gallery-dl's own default config (``~/.config/gallery-dl/config.json``).
    """

    config_file: str = ""
    cookies: str = ""
    cookies_from_browser: str = ""


class Selector(BaseModel):
    """A yt-dlp ``-S`` format sort plus ``-f`` format selector for one endpoint."""

    sort: str = ""
    format: str


class SiteConfig(BaseModel):
    enabled: bool = True
    domains: list[str]
    best: Selector
    medium: Selector
    audio: Selector
    video: Selector
    merge_best: bool = False  # server-side merge HD video+audio on /v1/best


class Config(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    api_keys: list[ApiKey] = Field(default_factory=list)
    format_filter: FormatFilter = Field(default_factory=FormatFilter)
    extractor: ExtractorConfig = Field(default_factory=ExtractorConfig)
    gallery: GalleryConfig = Field(default_factory=GalleryConfig)
    sites: dict[str, SiteConfig] = Field(default_factory=dict)

    def key_name_for(self, key: str) -> str | None:
        for api_key in self.api_keys:
            if api_key.key == key:
                return api_key.name
        return None

    def max_devices_for(self, name: str) -> int:
        for api_key in self.api_keys:
            if api_key.name == name:
                return api_key.max_devices
        return 0


def selector_for(site: SiteConfig, kind: str) -> Selector:
    return getattr(site, kind)


def load_config(
    path: str | os.PathLike[str] = DEFAULT_CONFIG_PATH,
    *,
    host: str | None = None,
    port: int | None = None,
    json_logs: bool | None = None,
) -> Config:
    """Load TOML config and apply CLI/env overrides for server fields."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {config_path}")
    with config_path.open("rb") as fh:
        data = tomllib.load(fh)
    config = Config(**data)

    # Env overrides (lowest precedence after file, below explicit flags)
    env_json_logs = os.getenv("JSON_LOGS")
    if env_json_logs is not None:
        config.server.json_logs = env_json_logs.strip().lower() in ("1", "true", "yes", "on")

    # Explicit flag overrides (highest precedence)
    if host is not None:
        config.server.host = host
    if port is not None:
        config.server.port = port
    if json_logs is not None:
        config.server.json_logs = json_logs

    return config
