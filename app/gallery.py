"""gallery-dl wrapper.

Extracts image/audio gallery items (Instagram carousels, TikTok photo
slideshows, Twitter image posts, …) that yt-dlp can't handle, returning them as
yt-dlp-like info dicts so they flow through the same ``formats.build_item``
pipeline. Used as a fallback when yt-dlp finds no video.
"""
from __future__ import annotations

import io
import threading
import time
from urllib.parse import urlparse

from .extractor import _resolve_redirect

# Short-link hosts that redirect to a canonical URL. Unlike the yt-dlp path we
# keep the resolved URL as-is (gallery-dl wants the /photo/ form).
_SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}

AUDIO_EXTS = {"mp3", "m4a", "aac", "opus", "ogg", "wav", "flac"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "heic", "bmp", "avif"}

_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# gallery-dl's config is process-global, so serialize extractions.
_gdl_lock = threading.Lock()


class GalleryError(Exception):
    """Raised when gallery-dl extraction fails or yields nothing."""


def _media_kind(ext: str) -> str:
    ext = (ext or "").lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    return "video"


def _ext_from_url(url: str) -> str:
    path = urlparse(url).path
    if "." in path:
        return path.rsplit(".", 1)[-1].lower()[:5]
    return ""


def _normalize_url(url: str) -> str:
    try:
        if (urlparse(url).hostname or "").lower() in _SHORT_HOSTS:
            return _resolve_redirect(url) or url
    except Exception:
        pass
    return url


def _extract_raw(url: str, auth: dict | None = None) -> list[dict]:
    from gallery_dl import config, job
    from gallery_dl.job import Message

    auth = auth or {}
    url = _normalize_url(url)
    with _gdl_lock:
        config.clear()
        config.load()  # gallery-dl's own default config files, if any
        if auth.get("config_file"):
            config.load([auth["config_file"]])
        if auth.get("cookies"):
            config.set(("extractor",), "cookies", auth["cookies"])
        elif auth.get("cookies_from_browser"):
            config.set(("extractor",), "cookies", [auth["cookies_from_browser"]])

        data_job = job.DataJob(url, file=io.StringIO())
        try:
            data_job.run()
        except Exception as exc:  # network / unsupported / auth
            raise GalleryError(str(exc)) from exc
        messages = list(data_job.data)

    items: list[dict] = []
    fallback_title = None
    error_message = None
    for entry in messages:
        kind = entry[0]
        if kind == Message.Directory:
            meta = entry[1] or {}
            fallback_title = (
                fallback_title
                or meta.get("title")
                or meta.get("desc")
                or meta.get("content")
            )
        elif kind == Message.Url:
            _, media_url, meta = entry
            meta = meta or {}
            ext = (meta.get("extension") or _ext_from_url(media_url) or "").lower()
            mk = _media_kind(ext)
            num = meta.get("num")
            idx = len(items)
            items.append(
                {
                    "url": media_url,
                    "ext": ext,
                    "format_id": f"{mk}-{num if num is not None else idx}",
                    "id": str(meta.get("id") or num or idx),
                    "title": meta.get("title")
                    or meta.get("desc")
                    or meta.get("content")
                    or fallback_title,
                    "width": meta.get("width"),
                    "height": meta.get("height"),
                    "filesize": meta.get("filesize") or meta.get("size"),
                    "http_headers": {"User-Agent": _UA},
                    "media_kind": mk,
                }
            )
        elif len(entry) > 1 and isinstance(entry[1], dict) and entry[1].get("message"):
            # gallery-dl error/abort message (e.g. login wall)
            error_message = entry[1].get("message")

    if not items:
        raise GalleryError(error_message or "no media found")
    return items


class GalleryExtractor:
    """TTL-cached gallery-dl extraction, mirroring :class:`Extractor`."""

    def __init__(
        self,
        ttl_seconds: int,
        *,
        config_file: str = "",
        cookies: str = "",
        cookies_from_browser: str = "",
    ) -> None:
        self.ttl = ttl_seconds
        self._auth = {
            "config_file": config_file,
            "cookies": cookies,
            "cookies_from_browser": cookies_from_browser,
        }
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    def extract(self, url: str) -> list[dict]:
        key = url.strip()
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < self.ttl:
                return hit[1]
        items = _extract_raw(url, self._auth)
        with self._lock:
            self._cache[key] = (time.time(), items)
        return items
