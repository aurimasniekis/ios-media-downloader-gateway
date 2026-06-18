"""Anonymous Instagram image extraction.

yt-dlp reaches Instagram anonymously (via its GraphQL endpoint) but keeps only
videos — for image posts it raises "There is no video in this post". This module
reuses yt-dlp's own ``InstagramIE`` helpers, plus the ``doc_id`` read from the
installed yt-dlp at runtime (so it tracks yt-dlp updates), to pull the images
yt-dlp discards. No login or cookies required for public posts.
"""
from __future__ import annotations

import inspect
import json
import re
import threading
import time

_SHORTCODE_RE = re.compile(r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([^/?#]+)")
_FALLBACK_DOC_ID = "8845758582119845"
_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

_doc_id: str | None = None
_doc_lock = threading.Lock()


class InstagramError(Exception):
    """Raised when anonymous Instagram image extraction fails."""


def is_instagram_post(url: str) -> bool:
    return bool(_SHORTCODE_RE.search(url or ""))


def _doc_id_from_ytdlp() -> str:
    """Read the GraphQL doc_id from the installed yt-dlp so it tracks updates."""
    global _doc_id
    if _doc_id is None:
        with _doc_lock:
            if _doc_id is None:
                try:
                    from yt_dlp.extractor.instagram import InstagramIE

                    src = inspect.getsource(InstagramIE._real_extract)
                    match = re.search(r"'doc_id':\s*'(\d+)'", src)
                    _doc_id = match.group(1) if match else _FALLBACK_DOC_ID
                except Exception:
                    _doc_id = _FALLBACK_DOC_ID
    return _doc_id


def _fetch_media(url: str) -> dict:
    from yt_dlp import YoutubeDL
    from yt_dlp.extractor.instagram import InstagramIE, _id_to_pk
    from yt_dlp.utils import traverse_obj

    match = _SHORTCODE_RE.search(url)
    if not match:
        raise InstagramError("not an instagram post url")
    shortcode = match.group(1)

    ydl = YoutubeDL({"quiet": True, "no_warnings": True})
    ie = InstagramIE()
    ie.set_downloader(ydl)
    pk = _id_to_pk(shortcode)

    try:
        # Sets the csrftoken cookie needed by the GraphQL call.
        ie._download_json(
            f"{ie._API_BASE_URL}/web/get_ruling_for_content/"
            f"?content_type=MEDIA&target_id={pk}",
            shortcode,
            headers=ie._api_headers,
            fatal=False,
            note=False,
            errnote=False,
        )
        csrf = ie._get_cookies("https://www.instagram.com").get("csrftoken")
        variables = {
            "shortcode": shortcode,
            "child_comment_count": 3,
            "fetch_comment_count": 40,
            "parent_comment_count": 24,
            "has_threaded_comments": True,
        }
        general = ie._download_json(
            "https://www.instagram.com/graphql/query/",
            shortcode,
            fatal=False,
            note=False,
            errnote=False,
            headers={
                **ie._api_headers,
                "X-CSRFToken": csrf.value if csrf else "",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": url,
            },
            query={
                "doc_id": _doc_id_from_ytdlp(),
                "variables": json.dumps(variables, separators=(",", ":")),
            },
        )
    except Exception as exc:
        raise InstagramError(str(exc)) from exc

    media = traverse_obj(general, ("data", "xdt_shortcode_media", {dict})) or {}
    if not media:
        raise InstagramError(
            "empty media response (post may be private, login-gated, or rate-limited)"
        )
    return media


def extract_images(url: str) -> list[dict]:
    """Return image items (yt-dlp-like dicts) for a public Instagram post.

    Video children are skipped — those are served by yt-dlp's normal path.
    """
    from yt_dlp.utils import traverse_obj, url_or_none

    media = _fetch_media(url)
    owner = traverse_obj(media, ("owner", "username"))
    caption = traverse_obj(media, ("edge_media_to_caption", "edges", 0, "node", "text"))
    title = caption or (f"Post by {owner}" if owner else None)

    nodes = traverse_obj(
        media, ("edge_sidecar_to_children", "edges", ..., "node")
    ) or [media]

    items: list[dict] = []
    for node in nodes:
        if node.get("is_video"):
            continue
        display = url_or_none(traverse_obj(node, "display_url", "display_src"))
        if not display:
            continue
        idx = len(items)
        items.append(
            {
                "url": display,
                "ext": "jpg",
                "format_id": f"image-{idx}",
                "id": str(node.get("shortcode") or node.get("id") or idx),
                "title": title,
                "width": traverse_obj(node, ("dimensions", "width")),
                "height": traverse_obj(node, ("dimensions", "height")),
                "http_headers": {"Referer": "https://www.instagram.com/", "User-Agent": _UA},
                "media_kind": "image",
            }
        )

    if not items:
        raise InstagramError("no images in this post")
    return items


class InstagramExtractor:
    """TTL-cached anonymous Instagram image extraction."""

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    def extract(self, url: str) -> list[dict]:
        key = url.strip()
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < self.ttl:
                return hit[1]
        items = extract_images(url)
        with self._lock:
            self._cache[key] = (time.time(), items)
        return items
