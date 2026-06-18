"""Format selection, choice cleaning, and download-item assembly."""
from __future__ import annotations

import re
import shlex
from urllib.parse import urlparse

from yt_dlp import YoutubeDL

from .config import FormatFilter, Selector
from .extractor import CookieSnapshot
from .models import DownloadItem, FormatChoice

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "heic", "bmp", "avif"}
AUDIO_EXTS = {"mp3", "m4a", "aac", "opus", "ogg", "wav", "flac"}

_MIME_BY_EXT = {
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "aac": "audio/aac",
    "opus": "audio/opus",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "heic": "image/heic",
}

def _has_video(fmt: dict) -> bool:
    return fmt.get("vcodec") not in (None, "none")


def _has_audio(fmt: dict) -> bool:
    return fmt.get("acodec") not in (None, "none")


def item_has_video(item_info: dict) -> bool:
    """True if the item exposes at least one video stream."""
    for fmt in item_info.get("formats") or []:
        if _has_video(fmt):
            return True
    return _has_video(item_info)


def item_has_audio(item_info: dict) -> bool:
    """True if the item exposes at least one audio stream."""
    for fmt in item_info.get("formats") or []:
        if _has_audio(fmt):
            return True
    return _has_audio(item_info)


# --- multi-item normalization ------------------------------------------------


def normalize_items(info: dict) -> list[dict]:
    """Flatten an extract_info result into a list of media-item info dicts."""
    if not info:
        return []
    if info.get("_type") == "playlist" or info.get("entries") is not None:
        return [entry for entry in (info.get("entries") or []) if entry]
    return [info]


def _formats_of(item: dict) -> list[dict]:
    formats = item.get("formats")
    if formats:
        return [f for f in formats if f.get("url")]
    if item.get("url"):  # single-stream / image item
        return [item]
    return []


# --- selection ---------------------------------------------------------------


def select(item_info: dict, selector: Selector, kind: str) -> dict | None:
    """Pick a single format using yt-dlp's own sort (``-S``) + selector (``-f``).

    The ``sort`` (e.g. ``"vcodec:avc,res:720"``) is applied first so a plain
    ``best`` picks the preferred codec/resolution — this is what keeps TikTok's
    video-only HEVC streams from being chosen. Falls back to a manual heuristic
    if the selector engine yields nothing.
    """
    formats = item_info.get("formats")
    if not formats:
        # Image or url-only item: the item itself is the downloadable thing.
        return item_info if item_info.get("url") else None

    try:
        opts = {"quiet": True, "no_warnings": True, "format": selector.format}
        if selector.sort:
            opts["format_sort"] = [s.strip() for s in selector.sort.split(",") if s.strip()]
        ydl = YoutubeDL(opts)

        holder = {"formats": [dict(f) for f in formats]}
        ydl.sort_formats(holder)
        sorted_formats = holder["formats"]

        selector_func = ydl.build_format_selector(selector.format)
        ctx = {
            "formats": sorted_formats,
            "has_merged_format": any(
                _has_video(f) and _has_audio(f) for f in sorted_formats
            ),
            "incomplete_formats": _incomplete(sorted_formats),
        }
        chosen = list(selector_func(ctx))
        if chosen:
            return chosen[0]
    except Exception:
        pass

    return _manual_select(_formats_of(item_info), kind)


def _incomplete(formats: list[dict]) -> bool:
    return all(_has_video(f) and not _has_audio(f) for f in formats) or all(
        not _has_video(f) and _has_audio(f) for f in formats
    )


def _manual_select(formats: list[dict], kind: str) -> dict | None:
    real = [f for f in formats if f.get("url")]
    if not real:
        return None

    if kind == "audio":
        cands = [f for f in real if _has_audio(f) and not _has_video(f)]
        cands = cands or [f for f in real if _has_audio(f)] or real
        return max(cands, key=lambda f: (f.get("abr") or f.get("tbr") or 0))

    if kind == "video":
        cands = [f for f in real if _has_video(f) and not _has_audio(f)]
        cands = cands or [f for f in real if _has_video(f)] or real
        return max(cands, key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 0)))

    # best / medium -> prefer progressive (audio + video together)
    progressive = [f for f in real if _has_video(f) and _has_audio(f)]
    if kind == "medium":
        capped = [f for f in progressive if (f.get("height") or 0) <= 720]
        cands = capped or progressive or real
    else:
        cands = progressive or real
    return max(cands, key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 0)))


# --- /v1/formats choices -----------------------------------------------------


def clean_choices(item_info: dict, filter_cfg: FormatFilter) -> list[FormatChoice]:
    """Dedup, sort, and mark a recommended format for the /v1/formats response."""
    # Drop storyboards / non-media (e.g. mhtml thumbnail sheets with no av streams).
    real = [
        f
        for f in _formats_of(item_info)
        if f.get("ext") != "mhtml" and (_has_video(f) or _has_audio(f))
    ]
    deduped: dict[tuple, dict] = {}
    for fmt in real:
        key = (fmt.get("height"), fmt.get("ext"))
        current = deduped.get(key)
        if current is None or (fmt.get("tbr") or 0) > (current.get("tbr") or 0):
            deduped[key] = fmt

    chosen = sorted(
        deduped.values(),
        key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 0)),
        reverse=True,
    )
    recommended_id = _pick_recommended(chosen, filter_cfg)

    choices = []
    for fmt in chosen:
        filesize = fmt.get("filesize") or fmt.get("filesize_approx")
        choices.append(
            FormatChoice(
                format_id=str(fmt.get("format_id")),
                label=build_label(
                    resolution=_resolution(fmt),
                    filesize=filesize,
                    note=fmt.get("format_note"),
                    video_only=_has_video(fmt) and not _has_audio(fmt),
                    audio_only=_has_audio(fmt) and not _has_video(fmt),
                ),
                ext=fmt.get("ext"),
                resolution=_resolution(fmt),
                fps=fmt.get("fps"),
                vcodec=fmt.get("vcodec"),
                acodec=fmt.get("acodec"),
                tbr=fmt.get("tbr"),
                filesize=filesize,
                note=fmt.get("format_note"),
                recommended=str(fmt.get("format_id")) == recommended_id,
            )
        )
    return choices


def _size_mb(size: int | None) -> str | None:
    if not size:
        return None
    mb = size / (1024 * 1024)
    return f"{mb:.1f}MB" if mb < 10 else f"{round(mb)}MB"


def _res_label(resolution: str | None) -> str | None:
    """Normalize a resolution to a "<short-side>p" label (720x1280 -> 720p)."""
    if not resolution:
        return None
    match = re.match(r"(\d+)x(\d+)", resolution)
    if match:
        return f"{min(int(match.group(1)), int(match.group(2)))}p"
    if resolution == "audio only":
        return None
    return resolution  # already e.g. "720p"


def build_label(
    *,
    resolution: str | None = None,
    filesize: int | None = None,
    note: str | None = None,
    video_only: bool = False,
    audio_only: bool = False,
    image: bool = False,
    image_index: int | None = None,
    server_processed: bool = False,
) -> str:
    """Compose a human-readable label like "720p 20MB (Video Only)"."""
    parts: list[str] = []
    if image:
        if image_index is not None:
            parts.append(f"Img {image_index}")
        if resolution:
            parts.append(resolution)  # keep WxH for stills
    else:
        res = _res_label(resolution)
        if res:
            parts.append(res)
    size = _size_mb(filesize)
    if size:
        parts.append(size)
    # Skip notes that just repeat the resolution (e.g. yt-dlp's "1080p"/"720p60").
    if note and not re.match(r"^\d{3,4}p\d{0,3}$", note.strip()):
        parts.append(f"({note})")
    if server_processed:
        parts.append("(Server Processed)")
    elif video_only:
        parts.append("(Video Only)")
    elif audio_only:
        parts.append("(Audio Only)")
    return " ".join(parts) if parts else "media"


def _pick_recommended(chosen: list[dict], filter_cfg: FormatFilter) -> str | None:
    progressive = [f for f in chosen if _has_video(f) and _has_audio(f)]
    pool = progressive or chosen
    if not pool:
        return None

    def score(fmt: dict) -> tuple:
        vcodec = fmt.get("vcodec") or ""
        ext = fmt.get("ext") or ""
        codec_match = any(vcodec.startswith(p) for p in filter_cfg.prefer_vcodec)
        ext_match = ext in filter_cfg.prefer_ext
        return (codec_match, ext_match, fmt.get("height") or 0, fmt.get("tbr") or 0)

    best = max(pool, key=score)
    return str(best.get("format_id"))


# --- download item assembly --------------------------------------------------


def find_format(item_info: dict, format_id: str) -> dict | None:
    for fmt in item_info.get("formats") or []:
        if str(fmt.get("format_id")) == format_id:
            return fmt
    if str(item_info.get("format_id")) == format_id:
        return item_info
    return None


def build_item(
    item_info: dict,
    fmt: dict | None,
    idx: int,
    cookies: CookieSnapshot,
) -> DownloadItem | None:
    """Assemble a download item from the chosen format (or the item itself)."""
    if fmt and fmt.get("requested_formats"):
        # A merged selection — surface the first stream so there's a usable URL.
        fmt = fmt["requested_formats"][0]
    source = fmt or item_info
    download_url = source.get("url") or item_info.get("url")
    if not download_url:
        return None

    ext = source.get("ext") or item_info.get("ext")
    title = item_info.get("title") or item_info.get("id")
    headers = dict(source.get("http_headers") or item_info.get("http_headers") or {})

    return DownloadItem(
        item_index=idx,
        media_type=_media_type(source, item_info),
        title=title,
        format_id=str(source.get("format_id")) if source.get("format_id") else None,
        ext=ext,
        width=source.get("width"),
        height=source.get("height"),
        fps=source.get("fps"),
        filesize=source.get("filesize") or source.get("filesize_approx"),
        mimetype=_mimetype(source, ext),
        suggested_filename=_filename(title, idx, ext),
        download_url=download_url,
        http_headers=headers,
        cookies=_cookies_for(cookies, download_url),
    )


def _media_type(fmt: dict, item: dict) -> str:
    if _has_video(fmt):
        return "video"
    if _has_audio(fmt):
        return "audio"
    ext = (fmt.get("ext") or item.get("ext") or "").lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    # No codec info and not a known image/audio ext: best-effort guess.
    return "image" if item.get("_type") == "image" else "video"


def _resolution(fmt: dict) -> str | None:
    if fmt.get("resolution"):
        return fmt["resolution"]
    width, height = fmt.get("width"), fmt.get("height")
    if width and height:
        return f"{width}x{height}"
    if height:
        return f"{height}p"
    if _has_audio(fmt) and not _has_video(fmt):
        return "audio only"
    return None


def _mimetype(fmt: dict, ext: str | None) -> str | None:
    if fmt.get("mimetype"):
        return fmt["mimetype"]
    if fmt.get("mime_type"):
        return fmt["mime_type"]
    return _MIME_BY_EXT.get((ext or "").lower())


def _filename(title: str | None, idx: int, ext: str | None) -> str:
    base = re.sub(r"[^\w\-.]+", "-", (title or "media")).strip("-.") or "media"
    base = base[:80]
    suffix = f"-{idx + 1}"
    extension = f".{ext}" if ext else ""
    return f"{base}{suffix}{extension}"


def curl_command(item: DownloadItem) -> str:
    """Render a ready-to-run ``curl`` command that downloads one item."""
    cmd = ["curl -L"]
    for key, value in item.http_headers.items():
        cmd.append(f"-H {shlex.quote(f'{key}: {value}')}")
    if item.cookies:
        cmd.append(f"-H {shlex.quote('Cookie: ' + item.cookies)}")
    cmd.append(f"-o {shlex.quote(item.suggested_filename or 'download')}")
    cmd.append(shlex.quote(item.download_url))

    header = f"# item {item.item_index} ({item.media_type})"
    if item.title:
        header += f": {item.title}"
    return header + "\n" + " \\\n  ".join(cmd)


def curl_script(items: list[DownloadItem]) -> str:
    """Join per-item curl commands into a single copy-pasteable script."""
    if not items:
        return "# no downloadable items\n"
    return "\n\n".join(curl_command(item) for item in items) + "\n"


def _cookies_for(snapshot: CookieSnapshot, url: str) -> str:
    if not snapshot or not url:
        return ""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return ""
    pairs = []
    for domain, name, value in snapshot:
        d = domain.lstrip(".").lower()
        if not d or host == d or host.endswith("." + d):
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)
