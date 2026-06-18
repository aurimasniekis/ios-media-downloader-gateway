"""yt-dlp extraction wrapper with a short TTL in-memory cache."""
from __future__ import annotations

import threading
import time
import urllib.request
from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError


class ExtractError(Exception):
    """Raised when yt-dlp extraction fails."""


CookieSnapshot = list[tuple[str, str, str]]  # (domain, name, value)

# TikTok short-link hosts that 30x-redirect to a canonical tiktok.com URL.
_TIKTOK_SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}
_REDIRECT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def _resolve_redirect(url: str) -> str | None:
    """Follow redirects and return the final URL (None on failure)."""
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(
                url, method=method, headers={"User-Agent": _REDIRECT_UA}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.geturl()
        except Exception:
            continue
    return None


def _normalize_url(url: str) -> str:
    """Resolve TikTok short links and rewrite ``/photo/`` URLs to ``/video/``.

    yt-dlp returns "Unsupported URL" for TikTok photo/slideshow posts
    (``tiktok.com/@user/photo/<id>``); the ``/video/`` form extracts fine. Short
    links (vm./vt.tiktok.com) redirect to a ``/photo/`` or ``/video/`` URL, so we
    resolve them first.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
        if host in _TIKTOK_SHORT_HOSTS:
            url = _resolve_redirect(url) or url
            host = (urlparse(url).hostname or "").lower()
        if host.endswith("tiktok.com") and "/photo/" in url:
            url = url.replace("/photo/", "/video/")
    except Exception:
        pass
    return url


def _snapshot_cookies(ydl: YoutubeDL) -> CookieSnapshot:
    out: CookieSnapshot = []
    try:
        for cookie in ydl.cookiejar:
            out.append((cookie.domain or "", cookie.name, cookie.value or ""))
    except Exception:
        pass
    return out


class Extractor:
    """Runs ``extract_info(url, download=False)`` with a TTL cache.

    The cache is keyed on the normalized URL so the ``/formats`` -> ``/download``
    flow (and repeated quality endpoints) reuse a single extraction.
    """

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[float, dict, CookieSnapshot]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(url: str) -> str:
        return url.strip()

    @staticmethod
    def _ydl_opts() -> dict:
        return {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": False,  # let carousels / slideshows expand
            "extract_flat": False,
            "ignore_no_formats_error": True,
        }

    def extract(self, url: str) -> tuple[dict, CookieSnapshot]:
        key = self._key(url)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < self.ttl:
                return hit[1], hit[2]

        try:
            target = _normalize_url(url)
            with YoutubeDL(self._ydl_opts()) as ydl:
                info = ydl.extract_info(target, download=False)
                cookies = _snapshot_cookies(ydl)
        except (DownloadError, ExtractorError) as exc:
            raise ExtractError(_clean_error(str(exc))) from exc
        except Exception as exc:  # network / parsing / unexpected
            raise ExtractError(_clean_error(str(exc))) from exc

        if info is None:
            raise ExtractError("no information extracted")

        with self._lock:
            self._cache[key] = (time.time(), info, cookies)
        return info, cookies


def _clean_error(message: str) -> str:
    """Strip yt-dlp's ANSI / prefix noise from an error message."""
    message = message.replace("\x1b[0;31m", "").replace("\x1b[0m", "")
    for prefix in ("ERROR: ", "ERROR:"):
        if message.startswith(prefix):
            message = message[len(prefix):]
    return message.strip().splitlines()[0] if message.strip() else "extraction failed"
