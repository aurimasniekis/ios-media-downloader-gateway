"""Argparse entrypoint, FastAPI app, route wiring, and uvicorn launch."""
from __future__ import annotations

import argparse
import logging
import os
import time
import uuid

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import formats, merge, metrics, version
from .auth import device_fields, device_fingerprint, require_api_key
from .config import Config, DEFAULT_CONFIG_PATH, load_config, selector_for
from .db import AuditDB
from .extractor import ExtractError, Extractor
from .gallery import GalleryError, GalleryExtractor
from .instagram import InstagramError, InstagramExtractor, is_instagram_post
from .logging_setup import setup_logging
from .models import (
    CheckRequest,
    CheckResponse,
    Device,
    DownloadItem,
    DownloadRequest,
    FormatChoice,
    FormatsEnvelope,
    MediaEnvelope,
    UrlRequest,
)
from .site_resolver import resolve

logger = logging.getLogger("media_gateway")

MEDIA_KINDS = ("best", "medium", "audio", "video")


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _device_dict(device: Device | None) -> dict | None:
    return device.model_dump() if device is not None else None


def api_error(status_code: int, message: str, **details) -> HTTPException:
    """Build an HTTPException carrying the unified error payload.

    The body is rendered by the exception handlers as
    ``{"status": "error", "error": message, "error_details": {...}}``.
    """
    return HTTPException(
        status_code=status_code,
        detail={"error": message, "error_details": details},
    )


def _error_body(message: str, details: dict | None = None) -> dict:
    return {"status": "error", "error": message, "error_details": details or {}}


def _device_change_summary(new_fields: dict, registered: list[dict]) -> dict:
    """Diff a rejected device against the closest registered one.

    Returns the changed fields (old -> new) plus a human-readable summary, so a
    rejection log shows *what* differs (e.g. a renamed phone or new screen size).
    """
    if not registered:
        return {"summary": "no devices registered yet", "changed": {}, "matched": None}

    def score(reg: dict) -> int:
        rf = reg.get("fields") or {}
        return sum(
            1
            for f, v in new_fields.items()
            if v is not None and str(rf.get(f)) == str(v)
        )

    best = max(registered, key=score)
    best_fields = best.get("fields") or {}
    changed = {
        field: {"from": best_fields.get(field), "to": value}
        for field, value in new_fields.items()
        if str(best_fields.get(field)) != str(value)
    }
    if changed:
        diff = ", ".join(
            f"{f} {c['from']!r}->{c['to']!r}" for f, c in changed.items()
        )
        summary = f"closest registered device {best['label']!r} differs in: {diff}"
    else:
        summary = f"distinct new device (closest registered: {best['label']!r})"
    return {"summary": summary, "changed": changed, "matched": best["label"]}


def _build_media_items(source, items_info, cookies, site, kind):
    """Turn resolved items into DownloadItems for the requested ``kind``.

    For yt-dlp ``video`` sources the per-site quality selector is applied. For
    ``photo`` (gallery-dl) sources the audio endpoint returns the audio item(s)
    and every other endpoint returns the images.
    """
    items = []
    if source == "video":
        selector = selector_for(site, kind)
        for idx, item_info in enumerate(items_info):
            chosen = formats.select(item_info, selector, kind)
            built = formats.build_item(item_info, chosen, idx, cookies)
            if built is not None:
                items.append(built)
        return items

    want = "audio" if kind == "audio" else "image"
    chosen = [g for g in items_info if g.get("media_kind") == want]
    if not chosen and kind != "audio":
        # gallery returned no images but maybe videos — surface those instead
        chosen = [g for g in items_info if g.get("media_kind") != "audio"]
    for idx, gallery_item in enumerate(chosen):
        built = formats.build_item(gallery_item, None, idx, [])
        if built is not None:
            items.append(built)
    return items


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="ios-media-downloader-gateway", version="0.1.0")
    app.state.config = config
    app.state.extractor = Extractor(config.server.cache_ttl_seconds)
    app.state.gallery = GalleryExtractor(
        config.server.cache_ttl_seconds,
        config_file=config.gallery.config_file,
        cookies=config.gallery.cookies,
        cookies_from_browser=config.gallery.cookies_from_browser,
    )
    app.state.instagram = InstagramExtractor(config.server.cache_ttl_seconds)
    app.state.db = AuditDB(config.server.db_path)
    app.state.merge_secret = os.urandom(32)
    app.state.ffmpeg_ok = merge.ffmpeg_available(config.server.ffmpeg_path)
    if not app.state.ffmpeg_ok and any(s.merge_best for s in config.sites.values()):
        logger.warning(
            "ffmpeg not found at %r — HD merge disabled; /v1/best falls back to "
            "the best single-file rendition",
            config.server.ffmpeg_path,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            message = detail["error"]
            details = detail.get("error_details", {})
        else:
            message = detail if isinstance(detail, str) else str(detail)
            details = {}
        return JSONResponse(status_code=exc.status_code, content=_error_body(message, details))

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=_error_body(
                "invalid request body", {"errors": jsonable_encoder(exc.errors())}
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("unhandled error")
        return JSONResponse(
            status_code=500, content=_error_body("internal server error")
        )

    @app.middleware("http")
    async def request_logger(request: Request, call_next):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        start = time.perf_counter()
        response = await call_next(request)
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        logger.info(
            "request",
            extra={
                "request_id": request_id,
                "endpoint": request.url.path,
                "status": response.status_code,
                "latency_ms": latency_ms,
                "api_key_name": getattr(request.state, "api_key_name", None),
                "site": getattr(request.state, "site", None),
                "item_count": getattr(request.state, "item_count", None),
            },
        )
        response.headers["X-Request-ID"] = request_id
        return response

    async def _audit(request: Request, **fields) -> None:
        await run_in_threadpool(app.state.db.record, **fields)

    async def _enforce_device(
        request: Request,
        api_key_name: str,
        device: dict | None,
        endpoint: str,
        site_name: str,
    ) -> None:
        """Reject the request if it comes from a new device beyond the key's limit."""
        max_devices = config.max_devices_for(api_key_name)
        if not max_devices:
            return
        fingerprint, label = device_fingerprint(device)
        if not fingerprint:
            return  # no device info to identify; can't enforce
        fields = device_fields(device)
        allowed = await run_in_threadpool(
            app.state.db.check_and_register_device,
            api_key_name,
            fingerprint,
            label,
            fields,
            max_devices,
        )
        if not allowed:
            registered = await run_in_threadpool(
                app.state.db.list_registered, api_key_name
            )
            diff = _device_change_summary(fields, registered)
            metrics.FAILURES.labels(endpoint, api_key_name, site_name, "device_limit").inc()
            logger.warning(
                "device limit reached: api_key=%s endpoint=%s rejected=%r "
                "fingerprint=%s max=%d; %s; registered=%s",
                api_key_name,
                endpoint,
                label,
                fingerprint,
                max_devices,
                diff["summary"],
                [r["label"] for r in registered],
            )
            await _audit(
                request,
                api_key_name=api_key_name,
                ip=_client_ip(request),
                site=None if site_name == "-" else site_name,
                endpoint=endpoint,
                url="",
                item_count=0,
                format_ids=None,
                title=f"device {label}",
                status="device_rejected",
                error=f"device limit reached: {diff['summary']}",
                device=device,
            )
            raise api_error(
                403,
                "device limit reached for this API key",
                max_devices=max_devices,
                device=label,
                changed=list(diff["changed"].keys()),
                registered=[r["label"] for r in registered],
            )

    async def _resolve(
        request: Request,
        url: str,
        kind: str,
        endpoint: str,
        site_name: str,
        api_key_name: str,
        device: dict | None,
    ):
        """Resolve a URL to media items via yt-dlp, falling back to gallery-dl.

        Returns ``(source, items_info, cookies, title)`` where ``source`` is
        ``"video"`` (yt-dlp) or ``"photo"`` (gallery-dl). yt-dlp is used when it
        finds a video, or for the audio endpoint when it finds audio (e.g. the
        music track of a photo post). Otherwise gallery-dl supplies the images.
        """
        start = time.perf_counter()
        ytdlp_error = None
        try:
            info, cookies = await run_in_threadpool(app.state.extractor.extract, url)
            items_info = formats.normalize_items(info)
            has_video = any(formats.item_has_video(i) for i in items_info)
            has_audio = any(formats.item_has_audio(i) for i in items_info)
            if has_video or (kind == "audio" and has_audio):
                metrics.EXTRACT_DURATION.observe(time.perf_counter() - start)
                return "video", items_info, cookies, info.get("title")
        except ExtractError as exc:
            ytdlp_error = str(exc)

        # Photo fallback. Instagram is served anonymously via yt-dlp's GraphQL
        # endpoint (no login needed); everything else goes through gallery-dl.
        photo_items = None
        errors: dict[str, str] = {}
        if is_instagram_post(url):
            try:
                photo_items = await run_in_threadpool(app.state.instagram.extract, url)
            except InstagramError as exc:
                errors["instagram"] = str(exc)
        if photo_items is None:
            try:
                photo_items = await run_in_threadpool(app.state.gallery.extract, url)
            except GalleryError as exc:
                errors["gallery"] = str(exc)

        if photo_items is None:
            metrics.EXTRACT_DURATION.observe(time.perf_counter() - start)
            detail = errors.get("instagram") or errors.get("gallery") or ytdlp_error
            metrics.FAILURES.labels(endpoint, api_key_name, site_name, "extract").inc()
            await _audit(
                request,
                api_key_name=api_key_name,
                ip=_client_ip(request),
                site=site_name,
                endpoint=endpoint,
                url=url,
                item_count=0,
                format_ids=None,
                title=None,
                status="error",
                error=detail,
                device=device,
            )
            raise api_error(
                502,
                "extraction failed",
                site=site_name,
                reason="extract",
                detail=detail,
                ytdlp=ytdlp_error,
            )

        metrics.EXTRACT_DURATION.observe(time.perf_counter() - start)
        title = next((g.get("title") for g in photo_items if g.get("title")), None)
        return "photo", photo_items, [], title

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics_endpoint():
        payload, content_type = metrics.render()
        return Response(content=payload, media_type=content_type)

    @app.get("/v1/stream")
    async def stream_route(request: Request, token: str):
        """Merge HD video+audio for a signed token and stream the mp4.

        Auth is the signed, expiring token itself (minted by /v1/best or
        /v1/download), so the Shortcut can fetch it with no extra headers.
        """
        if not app.state.ffmpeg_ok:
            raise api_error(503, "merging unavailable (ffmpeg not installed)")
        try:
            url, item_index, max_height = merge.parse_token(app.state.merge_secret, token)
        except merge.MergeError as exc:
            raise api_error(403, "invalid or expired stream token", detail=str(exc))
        try:
            info, _cookies = await run_in_threadpool(app.state.extractor.extract, url)
        except ExtractError as exc:
            raise api_error(502, "extraction failed", detail=str(exc))
        items_info = formats.normalize_items(info)
        if not 0 <= item_index < len(items_info):
            raise api_error(404, "item_index out of range")
        mergeable = merge.plan(items_info[item_index], max_height)
        if not mergeable:
            raise api_error(409, "no mergeable video/audio streams for this url")
        video, audio = mergeable
        cmd = merge.ffmpeg_command(config.server.ffmpeg_path, video, audio)
        filename = formats._filename(info.get("title"), item_index, "mp4")
        return StreamingResponse(
            merge.stream(cmd),
            media_type="video/mp4",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/v1/check", response_model=CheckResponse)
    async def check_route(
        request: Request,
        body: CheckRequest,
        api_key_name: str = Depends(require_api_key),
    ) -> CheckResponse:
        endpoint = "/v1/check"
        device = _device_dict(body.device)
        metrics.REQUESTS.labels(endpoint, api_key_name, "-").inc()
        await _enforce_device(request, api_key_name, device, endpoint, "-")
        supported = version.is_supported(body.version)

        if supported:
            message = f"shortcut version {body.version} is supported"
            metrics.SUCCESS.labels(endpoint, api_key_name, "-").inc()
        else:
            message = (
                f"shortcut version {body.version} is no longer supported — please "
                f"upgrade to at least version {version.MIN_SHORTCUT_VERSION}"
            )
            metrics.FAILURES.labels(endpoint, api_key_name, "-", "version").inc()

        await _audit(
            request,
            api_key_name=api_key_name,
            ip=_client_ip(request),
            site=None,
            endpoint=endpoint,
            url="",
            item_count=0,
            format_ids=None,
            title=f"shortcut {body.version}",
            status="ok" if supported else "unsupported",
            error=None if supported else message,
            device=device,
        )

        if not supported:
            raise api_error(
                426,
                message,
                version=body.version,
                min_version=version.MIN_SHORTCUT_VERSION,
            )

        return CheckResponse(
            supported=True,
            version=body.version,
            min_version=version.MIN_SHORTCUT_VERSION,
            message=message,
        )

    def _merge_url(request: Request, url: str, item_index: int) -> str:
        token = merge.make_token(
            app.state.merge_secret,
            url,
            item_index,
            config.server.merge_max_height,
            config.server.merge_url_ttl_seconds,
        )
        base = (config.server.public_base_url or str(request.base_url)).rstrip("/")
        return f"{base}/v1/stream?token={token}"

    def _merge_item(
        request: Request, url: str, item_info: dict, idx: int, video: dict, audio: dict
    ) -> DownloadItem:
        title = item_info.get("title") or item_info.get("id")
        return DownloadItem(
            item_index=idx,
            media_type="video",
            title=title,
            format_id=f"merge-{video.get('format_id')}+{audio.get('format_id')}",
            ext="mp4",
            width=video.get("width"),
            height=video.get("height"),
            fps=video.get("fps"),
            mimetype="video/mp4",
            suggested_filename=formats._filename(title, idx, "mp4"),
            download_url=_merge_url(request, url, idx),
            http_headers={},
            cookies="",
        )

    def _merge_enabled(site) -> bool:
        return site.merge_best and app.state.ffmpeg_ok

    def _build_best_items(request: Request, url: str, items_info, cookies, site):
        """Best-quality items, upgrading to a merged HD stream where worthwhile."""
        items = []
        selector = selector_for(site, "best")
        for idx, item_info in enumerate(items_info):
            mergeable = merge.plan(item_info, config.server.merge_max_height)
            if mergeable:
                video, audio = mergeable
                items.append(_merge_item(request, url, item_info, idx, video, audio))
                continue
            built = formats.build_item(
                item_info, formats.select(item_info, selector, "best"), idx, cookies
            )
            if built is not None:
                items.append(built)
        return items

    def _video_choices(item_info: dict, site) -> list[FormatChoice]:
        """Quality choices for one video, with the server-merge choice on top."""
        choices = formats.clean_choices(item_info, config.format_filter)
        if _merge_enabled(site):
            mergeable = merge.plan(item_info, config.server.merge_max_height)
            if mergeable:
                video, _audio = mergeable
                resolution = (
                    f"{video.get('width')}x{video.get('height')}"
                    if video.get("width") and video.get("height")
                    else None
                )
                for choice in choices:
                    choice.recommended = False
                choices.insert(
                    0,
                    FormatChoice(
                        format_id="merge",
                        label=formats.build_label(
                            resolution=resolution, server_processed=True
                        ),
                        ext="mp4",
                        resolution=resolution,
                        vcodec=video.get("vcodec"),
                        acodec="mp4a",
                        note="server-merged HD (video+audio)",
                        recommended=True,
                    ),
                )
        return choices

    async def handle_media(
        request: Request,
        body: UrlRequest,
        kind: str,
        api_key_name: str,
        as_curl: bool = False,
    ):
        endpoint = f"/v1/{kind}"
        device = _device_dict(body.device)
        site_name, site = resolve(body.url, config)
        request.state.site = site_name
        metrics.REQUESTS.labels(endpoint, api_key_name, site_name).inc()
        await _enforce_device(request, api_key_name, device, endpoint, site_name)

        source, items_info, cookies, title = await _resolve(
            request, body.url, kind, endpoint, site_name, api_key_name, device
        )
        if kind == "best" and source == "video" and _merge_enabled(site):
            items = _build_best_items(request, body.url, items_info, cookies, site)
        else:
            items = _build_media_items(source, items_info, cookies, site, kind)

        request.state.item_count = len(items)
        metrics.ITEMS_RETURNED.observe(len(items))
        metrics.SUCCESS.labels(endpoint, api_key_name, site_name).inc()
        await _audit(
            request,
            api_key_name=api_key_name,
            ip=_client_ip(request),
            site=site_name,
            endpoint=endpoint,
            url=body.url,
            item_count=len(items),
            format_ids=",".join(i.format_id for i in items if i.format_id) or None,
            title=title,
            status="ok",
            error=None,
            device=device,
        )
        if as_curl:
            return PlainTextResponse(formats.curl_script(items))
        return MediaEnvelope(
            title=title,
            site=site_name,
            kind=kind,
            count=len(items),
            items=items,
        )

    def _make_media_route(kind: str):
        async def route(
            request: Request,
            body: UrlRequest,
            curl: bool = False,
            api_key_name: str = Depends(require_api_key),
        ):
            return await handle_media(request, body, kind, api_key_name, curl)

        return route

    for kind in MEDIA_KINDS:
        app.add_api_route(
            f"/v1/{kind}",
            _make_media_route(kind),
            methods=["POST"],
            response_model=MediaEnvelope,
            name=f"media_{kind}",
        )

    @app.post("/v1/formats", response_model=FormatsEnvelope)
    async def formats_route(
        request: Request,
        body: UrlRequest,
        api_key_name: str = Depends(require_api_key),
    ) -> FormatsEnvelope:
        endpoint = "/v1/formats"
        device = _device_dict(body.device)
        site_name, site = resolve(body.url, config)
        request.state.site = site_name
        metrics.REQUESTS.labels(endpoint, api_key_name, site_name).inc()
        await _enforce_device(request, api_key_name, device, endpoint, site_name)

        source, items_info, _cookies, title = await _resolve(
            request, body.url, "best", endpoint, site_name, api_key_name, device
        )

        # One flat menu of choices. format_id is globally unique so /v1/download
        # needs only the format_id (item_index optional).
        choices: list[FormatChoice] = []
        if source == "video":
            if len(items_info) == 1:
                choices = _video_choices(items_info[0], site)
            else:  # multiple videos -> prefix each with "Vid N" + a vid-index id
                for vid_idx, item_info in enumerate(items_info):
                    for choice in _video_choices(item_info, site):
                        choice.format_id = f"{vid_idx}:{choice.format_id}"
                        choice.label = f"Vid {vid_idx + 1} {choice.label}"
                        choice.recommended = False
                        choices.append(choice)
        else:  # photo / gallery — images numbered Img N, plus any audio track
            image_no = 0
            for gallery_item in items_info:
                width, height = gallery_item.get("width"), gallery_item.get("height")
                resolution = f"{width}x{height}" if width and height else None
                media_kind = gallery_item.get("media_kind") or "image"
                image_index = None
                if media_kind == "image":
                    image_no += 1
                    image_index = image_no
                choices.append(
                    FormatChoice(
                        format_id=str(gallery_item.get("format_id")),
                        label=formats.build_label(
                            resolution=resolution,
                            filesize=gallery_item.get("filesize"),
                            image=media_kind == "image",
                            image_index=image_index,
                            audio_only=media_kind == "audio",
                        ),
                        ext=gallery_item.get("ext"),
                        resolution=resolution,
                        filesize=gallery_item.get("filesize"),
                        recommended=False,
                    )
                )

        request.state.item_count = len(choices)
        metrics.ITEMS_RETURNED.observe(len(choices))
        metrics.SUCCESS.labels(endpoint, api_key_name, site_name).inc()
        await _audit(
            request,
            api_key_name=api_key_name,
            ip=_client_ip(request),
            site=site_name,
            endpoint=endpoint,
            url=body.url,
            item_count=len(choices),
            format_ids=None,
            title=title,
            status="ok",
            error=None,
            device=device,
        )
        return FormatsEnvelope(
            title=title,
            site=site_name,
            count=len(choices),
            choices=choices,
        )

    @app.post("/v1/download", response_model=MediaEnvelope)
    async def download_route(
        request: Request,
        body: DownloadRequest,
        curl: bool = False,
        api_key_name: str = Depends(require_api_key),
    ):
        endpoint = "/v1/download"
        device = _device_dict(body.device)
        site_name, site = resolve(body.url, config)
        request.state.site = site_name
        metrics.REQUESTS.labels(endpoint, api_key_name, site_name).inc()
        await _enforce_device(request, api_key_name, device, endpoint, site_name)

        source, items_info, cookies, title = await _resolve(
            request, body.url, "best", endpoint, site_name, api_key_name, device
        )

        # format_id alone identifies the download. For multi-video posts the id is
        # prefixed "<videoIndex>:<formatId>" (e.g. "1:18"); unwrap that here.
        format_id = body.format_id
        candidates = list(enumerate(items_info))
        if source == "video":
            head, sep, tail = format_id.partition(":")
            if sep and head.isdigit() and 0 <= int(head) < len(items_info):
                idx = int(head)
                candidates = [(idx, items_info[idx])]
                format_id = tail

        built = None
        if format_id == "merge":
            if _merge_enabled(site):
                for idx, item_info in candidates:
                    mergeable = merge.plan(item_info, config.server.merge_max_height)
                    if mergeable:
                        video, audio = mergeable
                        built = _merge_item(request, body.url, item_info, idx, video, audio)
                        break
            if built is None:
                metrics.FAILURES.labels(endpoint, api_key_name, site_name, "bad_format_id").inc()
                raise api_error(409, "no mergeable streams for this url")
        elif source == "video":
            for idx, item_info in candidates:
                fmt = formats.find_format(item_info, format_id)
                if fmt is not None:
                    built = formats.build_item(item_info, fmt, idx, cookies)
                    break
        else:  # photo / gallery
            for idx, gallery_item in candidates:
                if str(gallery_item.get("format_id")) == format_id:
                    built = formats.build_item(gallery_item, None, idx, [])
                    break

        if built is None and format_id != "merge":
            metrics.FAILURES.labels(endpoint, api_key_name, site_name, "bad_format_id").inc()
            raise api_error(404, "format not found", format_id=body.format_id)

        if built is None:
            metrics.FAILURES.labels(endpoint, api_key_name, site_name, "no_url").inc()
            raise api_error(502, "no download url for chosen format", format_id=body.format_id)

        request.state.item_count = 1
        metrics.ITEMS_RETURNED.observe(1)
        metrics.SUCCESS.labels(endpoint, api_key_name, site_name).inc()
        await _audit(
            request,
            api_key_name=api_key_name,
            ip=_client_ip(request),
            site=site_name,
            endpoint=endpoint,
            url=body.url,
            item_count=1,
            format_ids=built.format_id,
            title=title,
            status="ok",
            error=None,
            device=device,
        )
        if curl:
            return PlainTextResponse(formats.curl_script([built]))
        return MediaEnvelope(
            title=title,
            site=site_name,
            kind="download",
            count=1,
            items=[built],
        )

    return app


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="yt-dlp HTTP API for iOS Shortcuts")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="path to config.toml")
    parser.add_argument("--host", default=None, help="override server host")
    parser.add_argument("--port", type=int, default=None, help="override server port")
    parser.add_argument(
        "--json-logs",
        action="store_true",
        default=None,
        help="emit structured JSON logs",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the API server (default)")

    devices = sub.add_parser("devices", help="manage registered devices")
    dev_sub = devices.add_subparsers(dest="device_command", required=True)
    dev_list = dev_sub.add_parser("list", help="list registered devices")
    dev_list.add_argument("--api-key", default=None, help="filter by API key name")
    dev_rm = dev_sub.add_parser("remove", help="remove device registration(s)")
    dev_rm.add_argument("--api-key", required=True, help="API key name")
    rm_group = dev_rm.add_mutually_exclusive_group(required=True)
    rm_group.add_argument("--device-id", help="remove a specific device id")
    rm_group.add_argument("--label", help="remove device(s) by label")
    rm_group.add_argument("--all", action="store_true", help="remove all for the key")

    logs = sub.add_parser("logs", help="show recent audit log entries")
    logs.add_argument("--days", type=int, default=7, help="look back this many days")
    logs.add_argument("--api-key", default=None, help="filter by API key name")
    logs.add_argument("--limit", type=int, default=100, help="max rows to show")

    return parser.parse_args(argv)


def _run_server(config: Config) -> None:
    setup_logging(config.server.json_logs)
    logger.info(
        "starting yt-dlp-api on %s:%s (json_logs=%s, sites=%s)",
        config.server.host,
        config.server.port,
        config.server.json_logs,
        ",".join(config.sites),
    )
    app = create_app(config)
    uvicorn.run(app, host=config.server.host, port=config.server.port, log_config=None)


def _print_table(headers: list[str], rows: list[tuple]) -> None:
    cols = [headers] + [[str(c) if c is not None else "" for c in row] for row in rows]
    widths = [max(len(r[i]) for r in cols) for i in range(len(headers))]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        print(fmt.format(*[str(c) if c is not None else "" for c in row]))
    print(f"\n{len(rows)} row(s)")


def _cmd_devices(args: argparse.Namespace, config: Config) -> None:
    db = AuditDB(config.server.db_path)
    try:
        if args.device_command == "list":
            rows = db.list_devices(args.api_key)
            _print_table(
                ["API_KEY", "DEVICE_ID", "LABEL", "FIRST_SEEN", "LAST_SEEN"], rows
            )
        elif args.device_command == "remove":
            removed = db.remove_device(
                args.api_key,
                device_id=args.device_id,
                label=args.label,
                remove_all=args.all,
            )
            print(f"removed {removed} device registration(s)")
    finally:
        db.close()


def _cmd_logs(args: argparse.Namespace, config: Config) -> None:
    db = AuditDB(config.server.db_path)
    try:
        rows = db.list_audit(days=args.days, api_key_name=args.api_key, limit=args.limit)
        _print_table(
            ["TS", "API_KEY", "IP", "SITE", "ENDPOINT", "ITEMS", "STATUS", "DEVICE", "URL"],
            rows,
        )
    finally:
        db.close()


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    config = load_config(
        args.config,
        host=args.host,
        port=args.port,
        json_logs=args.json_logs,
    )
    command = args.command or "serve"
    if command == "devices":
        _cmd_devices(args, config)
    elif command == "logs":
        _cmd_logs(args, config)
    else:
        _run_server(config)


if __name__ == "__main__":
    main()
