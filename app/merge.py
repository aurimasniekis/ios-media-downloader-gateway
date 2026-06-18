"""Server-side video+audio merge (for sites like YouTube that only serve HD as
separate streams).

A signed, expiring token encodes the source URL + item index + max height. The
``/v1/stream`` endpoint validates the token, re-selects the best video-only +
audio streams, and pipes them through ffmpeg (``-c copy``, no re-encode) as a
fragmented mp4 streamed straight to the client — nothing is written to disk.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import shutil
import subprocess
import time

from . import formats
from .config import Selector


class MergeError(Exception):
    """Raised for invalid/expired tokens or failed merge planning."""


# --- token signing -----------------------------------------------------------


def make_token(secret: bytes, url: str, item_index: int, max_height: int, ttl: int) -> str:
    payload = {
        "u": url,
        "i": item_index,
        "h": max_height,
        "e": int(time.time()) + ttl,
    }
    raw = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    sig = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def parse_token(secret: bytes, token: str) -> tuple[str, int, int]:
    try:
        raw, sig = token.rsplit(".", 1)
    except ValueError as exc:
        raise MergeError("malformed token") from exc
    expected = hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        raise MergeError("bad token signature")
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception as exc:
        raise MergeError("undecodable token") from exc
    if data.get("e", 0) < time.time():
        raise MergeError("token expired")
    return data["u"], int(data.get("i", 0)), int(data.get("h", 1080))


# --- format selection / planning ---------------------------------------------


def select_merge_formats(item_info: dict, max_height: int) -> tuple[dict | None, dict | None]:
    # Sort by `res` (the smaller dimension) so portrait video is capped on its
    # short side — a height filter would wrongly exclude 1080x1920 portrait clips.
    video = formats.select(
        item_info,
        Selector(sort=f"vcodec:avc,res:{max_height}", format="bv"),
        "video",
    )
    audio = formats.select(
        item_info,
        Selector(sort="acodec:aac", format="ba[ext=m4a]/ba"),
        "audio",
    )
    return video, audio


def plan(item_info: dict, max_height: int) -> tuple[dict, dict] | None:
    """Return ``(video_fmt, audio_fmt)`` if merging beats the best progressive.

    Returns None when there's no separate video-only stream, no audio, or the
    best progressive (single-file) rendition is already as good — in which case
    the normal direct-URL path should be used.
    """
    video, audio = select_merge_formats(item_info, max_height)
    if not (video and audio and video.get("url") and audio.get("url")):
        return None
    if formats._has_audio(video):
        return None  # 'video' is actually a progressive stream, not video-only
    progressive = [
        f
        for f in (item_info.get("formats") or [])
        if formats._has_video(f) and formats._has_audio(f)
    ]
    prog_height = max((f.get("height") or 0 for f in progressive), default=0)
    if (video.get("height") or 0) <= prog_height:
        return None
    return video, audio


# --- ffmpeg ------------------------------------------------------------------


def ffmpeg_available(path: str) -> bool:
    return shutil.which(path) is not None


def _header_args(fmt: dict) -> list[str]:
    headers = dict(fmt.get("http_headers") or {})
    user_agent = headers.pop("User-Agent", None)
    args: list[str] = []
    if user_agent:
        args += ["-user_agent", user_agent]
    if headers:
        args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    return args


def ffmpeg_command(ffmpeg_path: str, video_fmt: dict, audio_fmt: dict) -> list[str]:
    return [
        ffmpeg_path,
        "-loglevel",
        "error",
        *_header_args(video_fmt),
        "-i",
        video_fmt["url"],
        *_header_args(audio_fmt),
        "-i",
        audio_fmt["url"],
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof",
        "-f",
        "mp4",
        "pipe:1",
    ]


def stream(cmd: list[str]):
    """Yield merged mp4 bytes from ffmpeg's stdout, terminating it on disconnect."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
        proc.wait()
