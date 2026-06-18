# ios-media-downloader-gateway

A small HTTP/JSON gateway that wraps [yt-dlp](https://github.com/yt-dlp/yt-dlp)
and [gallery-dl](https://github.com/mikf/gallery-dl) so an **iOS Shortcut**
(triggered from the Share sheet) can download social-media videos and pictures.

The server normally **does not download media itself**: it runs extraction
(`download=False`) and returns **direct media URL(s)** plus the `http_headers` /
session `cookies` needed, so the Shortcut performs the actual download and iOS
saves the file natively. The one exception is some services, which requires the
server to merge separate video+audio streams.

Only a configured allow-list of sites is accepted. Callers authenticate with
named API keys, which double as metrics labels and audit-log identities.

## Contents

- [Quick start](#quick-start)
  - [Docker (published image)](#docker-published-image)
  - [Docker Compose](#docker-compose)
  - [From source (uv)](#from-source-uv)
- [iOS Shortcuts](#ios-shortcuts)
- [Configuration](#configuration)
- [Endpoints](#endpoints)
- [Responses](#responses)
- [Operations](#operations)
  - [CLI commands](#cli-commands)
  - [Device limits](#device-limits)
  - [Audit log, metrics & logs](#audit-log-metrics--logs)
- [Docker reference](#docker-reference)
- [Notes & caveats](#notes--caveats)

## Quick start

The container bundles **ffmpeg** (needed for stream merge). Config and the
SQLite audit DB live on a `./data` volume that you mount; create your config
first:

```bash
mkdir -p data
curl -sL https://raw.githubusercontent.com/aurimasniekis/ios-media-downloader-gateway/main/data/config.example.toml \
  -o data/config.toml
$EDITOR data/config.toml          # set real API keys (replace the CHANGE_ME values)
```

### Docker (published image)

```bash
docker pull aurimasniekis/ios-media-downloader-gateway

docker run -d \
  --name ios-media-downloader-gateway \
  --restart unless-stopped \
  -p 8080:8080 \
  -v "$PWD/data:/app/data" \
  -e JSON_LOGS=true \
  aurimasniekis/ios-media-downloader-gateway

docker logs -f ios-media-downloader-gateway
curl -s localhost:8080/healthz       # {"status":"ok"}
```

### Docker Compose

The repo ships a `compose.yaml`:

```bash
docker compose up -d            # pulls/builds and starts
docker compose logs -f
docker compose ps
docker compose down             # stop & remove
```

`compose.yaml` references the published image but also has `build: .`, so
`docker compose up -d --build` builds locally and `docker compose pull` grabs the
latest published image.

### From source (uv)

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). ffmpeg is optional
(only for video merge).

```bash
git clone https://github.com/aurimasniekis/ios-media-downloader-gateway
cd ios-media-downloader-gateway
uv sync
cp data/config.example.toml data/config.toml      # set real API keys

uv run python -m app.main                          # reads ./data/config.toml
uv run python -m app.main --json-logs              # structured JSON logs
uv run python -m app.main --config /etc/gw.toml --host 127.0.0.1 --port 9000
```

A `Makefile` wraps common tasks — run `make help` to list them
(`make run`, `make health`, `make metrics`, `make devices`, `make logs`, …).

## iOS Shortcuts

Two ready-made Shortcuts are available. On your iPhone/iPad, open an **iCloud**
link below — it opens straight in the Shortcuts app — and tap **Add Shortcut**,
then set your gateway URL and `X-API-Key` in the shortcut's import questions.
(The `.shortcut` files in the repo root are the same shortcuts, for reference or
self-hosting the install link.)

| Shortcut           | Install (iCloud)                                                                                    | File                                                                                                                                 | What it does                       |
|--------------------|-----------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------|------------------------------------|
| **Download**       | [icloud.com/shortcuts/3fb0…2166](https://www.icloud.com/shortcuts/3fb0c3449f934815b0ab181434af2166) | [`Download.shortcut`](https://raw.githubusercontent.com/aurimasniekis/ios-media-downloader-gateway/main/Download.shortcut)           | Asks how to download, then saves   |
| **Download Quick** | [icloud.com/shortcuts/1e84…8fc7](https://www.icloud.com/shortcuts/1e84b73570884256bc50d8d0b2508fc7) | [`DownloadQuick.shortcut`](https://raw.githubusercontent.com/aurimasniekis/ios-media-downloader-gateway/main/DownloadQuick.shortcut) | One-tap medium quality, no prompts |

**Download** — when run from the Share sheet it shows a menu:

| Choice         | Calls                                                        | Result                                        |
|----------------|--------------------------------------------------------------|-----------------------------------------------|
| **Best**       | `POST /v1/best`                                              | best quality (stream merge for some services) |
| **Medium**     | `POST /v1/medium`                                            | ~720p                                         |
| **Audio Only** | `POST /v1/audio`                                             | audio track                                   |
| **Video Only** | `POST /v1/video`                                             | video without audio                           |
| **Custom**     | `POST /v1/formats` → menu of `choices` → `POST /v1/download` | pick an exact format                          |

**Download Quick** — skips the menu and always uses `POST /v1/medium`.

Both follow the same flow:

1. Share sheet → **Get URLs from Input**.
2. `POST /v1/check` with `X-API-Key` and the shortcut `version` + `device` info.
   On `426`, prompt the user to update the shortcut.
3. The media call above (or `/v1/formats` → `choices` menu → `/v1/download` for
   **Custom**). For some services **Best**, `download_url` points back at
   `/v1/stream`, which the shortcut fetches like any other URL.
4. **Repeat** over `items`: **Get Contents of URL** against each `download_url`
   with its `http_headers` (and a `Cookie` header when `cookies` is non-empty).
5. Save to Photos / Files.

> The `.shortcut` files are exported from the Shortcuts app. To rebuild or tweak
> them, recreate the flow above and re-export — the gateway only needs the HTTP
> calls described here.

## Configuration

Config is a TOML file (default `./data/config.toml`, or `--config <path>`). See
[`data/config.example.toml`](data/config.example.toml) for the full annotated
file. Sections:

```toml
[server]
host = "0.0.0.0"
port = 8080
json_logs = false                 # or JSON_LOGS=true env / --json-logs flag
db_path = "./data/audit.sqlite3"
cache_ttl_seconds = 300           # reuse extraction between /formats and /download
ffmpeg_path = "ffmpeg"            # for stream merge
public_base_url = ""              # set behind a reverse proxy (for /v1/stream URLs)
merge_url_ttl_seconds = 21600     # /v1/stream token lifetime (6h)
merge_max_height = 1080           # cap merged video resolution

[[api_keys]]
name = "tom-iphone"
key = "CHANGE_ME_1"
max_devices = 1                   # 0 = unlimited distinct devices

[format_filter]
prefer_vcodec = ["avc1", "h264"]  # iOS-friendly
prefer_acodec = ["mp4a", "aac"]
prefer_ext    = ["mp4", "m4a", "jpg"]

[gallery]                         # gallery-dl auth (login-gated content only)
cookies = ""                      # path to a Netscape cookies.txt, or
cookies_from_browser = ""         # "firefox" / "chrome" / "safari", or
config_file = ""                  # a full gallery-dl JSON config
```

Each site endpoint is a yt-dlp **format sort** (`-S`) plus **selector** (`-f`).
Env `JSON_LOGS` and the `--host/--port/--json-logs` flags override `[server]`.

## Endpoints

All return JSON. Media endpoints require the `X-API-Key` header.

| Method | Path                 | Purpose                                                       |
|--------|----------------------|---------------------------------------------------------------|
| GET    | `/healthz`           | liveness, no auth                                             |
| GET    | `/metrics`           | Prometheus exposition, no auth                                |
| GET    | `/v1/stream?token=…` | streams a server-merged mp4; token is the auth                |
| POST   | `/v1/check`          | client version gate — `{ "version": "0.1.0", "device": {…} }` |
| POST   | `/v1/best`           | best quality (stream merge for some services)                 |
| POST   | `/v1/medium`         | ~720p progressive                                             |
| POST   | `/v1/audio`          | audio-only stream                                             |
| POST   | `/v1/video`          | video-only (no audio) stream                                  |
| POST   | `/v1/formats`        | one flat menu of `choices` for the post                       |
| POST   | `/v1/download`       | resolve one chosen `format_id` to a download item             |

Media endpoints take `{ "url": "...", "device": {…} }` (device optional).
`/v1/download` takes `{ "url": "...", "format_id": "..." }` — the `format_id`
from `/v1/formats` is globally unique, so **no item index is needed**.

Add `?curl=true` to any media endpoint (or `/v1/download`) to get a ready-to-run
`curl` command (with headers/cookies/filename) as `text/plain` instead of JSON.

### Examples

```bash
KEY=CHANGE_ME_1
BASE=http://localhost:8080

# liveness
curl -s $BASE/healthz

# version gate (the Shortcut calls this first)
curl -s -X POST $BASE/v1/check \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"version":"0.1.0","device":{"name":"iPhone","type":"Phone","model":"iPhone15"}}'

# best quality
curl -s -X POST $BASE/v1/best \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.tiok.com/@user/video/123"}'

# list choices, then download one by format_id
curl -s -X POST $BASE/v1/formats \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.yotbe.com/shorts/XXXX"}'

curl -s -X POST $BASE/v1/download \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"url":"https://www.yotbe.com/shorts/XXXX","format_id":"merge"}'
```

## Responses

Every JSON response has a top-level `status` (`"ok"` or `"error"`).

**Media endpoints** return a list of items (a post may hold several):

```json
{
  "status": "ok",
  "title": "post title",
  "site": "tiok",
  "kind": "best",
  "count": 1,
  "items": [
    {
      "item_index": 0,
      "media_type": "video",
      "format_id": "h264_720p",
      "ext": "mp4",
      "width": 720, "height": 1280, "fps": 30,
      "filesize": 1234567,
      "mimetype": "video/mp4",
      "suggested_filename": "title-1.mp4",
      "download_url": "https://…",
      "http_headers": { "User-Agent": "…", "Referer": "…" },
      "cookies": "name=value; name2=value2"
    }
  ]
}
```

**`/v1/formats`** returns a single flat `choices` menu with globally-unique
`format_id`s and human-readable `label`s:

```json
{
  "status": "ok", "site": "yotbe", "kind": "formats", "count": 15,
  "choices": [
    { "format_id": "merge",   "label": "1080p (Server Processed)", "recommended": true },
    { "format_id": "137",     "label": "1080p 7.8MB (Video Only)" },
    { "format_id": "140",     "label": "0.3MB (Audio Only)" }
  ]
}
```

Label rules: resolution → `<short-side>p` (`720x1280` and `1280x720` both read
`720p`); size in MB when known; non-redundant `format_note` in `(parens)`; type
tags `(Video Only)` / `(Audio Only)` / `(Server Processed)`. Multi-media posts
flatten into the one menu — images become `Img 1`, `Img 2`, … (`format_id`
`image-1`, `audio-0`, …) and multiple videos become `Vid 1`, `Vid 2`, …
(`format_id` `1:137`).

**Errors** all share one shape:

```json
{ "status": "error", "error": "human readable message", "error_details": { } }
```

Unsupported/disabled sites → `422`; extraction failures → `502`; bad
`format_id` → `404`; old Shortcut version → `426`; device limit → `403`.

## Operations

### CLI commands

The same entrypoint runs the server and management commands. In Docker, run them
via `exec` (compose service is `gateway`):

```bash
# Docker Compose
docker compose exec gateway python -m app.main devices list
docker compose exec gateway python -m app.main logs --days 10 --api-key tom-iphone

# plain Docker
docker exec ios-media-downloader-gateway python -m app.main devices list

# from source
uv run python -m app.main devices list
```

### Device limits

Each API key may cap how many **distinct devices** use it via `max_devices`
(`0` = unlimited). Devices are fingerprinted from stable `device` fields (name,
hostname, model, type, screen size — the two screen dimensions are sorted, so
portrait vs landscape is the *same* device). The first `max_devices` devices are
registered; a newer one is rejected with `403 device limit reached`.

A rejection logs (and audits) a diff against the closest registered device, so
you can see *what changed* (e.g. a renamed phone or new screen size):

```
device limit reached: api_key=tom-iphone rejected='iPhone' max=1;
closest registered device 'iPhone' differs in: screen_width '1179'->'1206',
screen_height '2556'->'2622'; registered=['iPhone']
```

Manage registrations:

```bash
docker compose exec gateway python -m app.main devices list
docker compose exec gateway python -m app.main devices list --api-key family-ipad

# free a slot (by device id, by label, or all of a key's devices)
docker compose exec gateway python -m app.main devices remove --api-key family-ipad --device-id <id>
docker compose exec gateway python -m app.main devices remove --api-key family-ipad --label "iPhone-B"
docker compose exec gateway python -m app.main devices remove --api-key family-ipad --all
```

### Audit log, metrics & logs

- **Audit log** — every request appends a row to `downloads` in
  `./data/audit.sqlite3` (timestamp, api key, ip, site, endpoint, url, status,
  per-field device columns). Browse it via the CLI:

  ```bash
  docker compose exec gateway python -m app.main logs --days 10
  docker compose exec gateway python -m app.main logs --days 7 --api-key tom-iphone --limit 50
  # or directly:
  sqlite3 ./data/audit.sqlite3 "select * from downloads order by ts desc limit 20;"
  ```

- **Metrics** — Prometheus at `/metrics`: `media_gateway_requests_total`,
  `media_gateway_success_total`, `media_gateway_failures_total`,
  `media_gateway_items_returned`, `media_gateway_extract_duration_seconds`.

  ```bash
  curl -s localhost:8080/metrics | grep '^media_gateway_'
  ```

- **Logs** — human-readable by default; `JSON_LOGS=true` (or `--json-logs`)
  emits one structured line per request with request id, key, site, endpoint,
  item count, latency and status.

## Docker reference

```bash
# build locally from a checkout
docker build -t aurimasniekis/ios-media-downloader-gateway .

# run (published image)
docker run -d --name ios-media-downloader-gateway \
  -p 8080:8080 -v "$PWD/data:/app/data" -e JSON_LOGS=true \
  aurimasniekis/ios-media-downloader-gateway

# compose lifecycle
docker compose up -d                  # start
docker compose up -d --build          # rebuild from source and start
docker compose pull && docker compose up -d   # update to latest published image
docker compose logs -f                # follow logs
docker compose restart                # restart
docker compose down                   # stop & remove

# one-off commands in the running container
docker compose exec gateway python -m app.main devices list
docker compose exec gateway sqlite3 /app/data/audit.sqlite3 "select count(*) from downloads;"
```

Notes:

- **Volume:** mount your `./data` at `/app/data`. It holds `config.toml` (you
  create it) and `audit.sqlite3` (created on first run). The container runs as
  root, so on Linux the SQLite file is root-owned on the host.
- **Config required:** if `data/config.toml` is missing, the container exits with
  `config file not found` — create it first.
- **Behind a proxy:** set `public_base_url` in the config (and run the proxy with
  forwarded headers) so the stream `/v1/stream` merge URL is reachable by the
  phone.
- **Port:** change the host side of `-p`/`ports` to remap; the in-container port
  comes from `[server].port` (default 8080).

## Notes & caveats

- **Public content only** — no server-side login cookie files for video. Per-
  format `http_headers` and any session cookies are surfaced; `cookies` is often
  empty for public content.

- **Individual stream server-side merge.** Some services
  serves HD only as *separate* video and audio streams. With `merge_best = true`
and ffmpeg present, **`/v1/best`** returns a
  `/v1/stream?token=…` URL back to this server; when fetched, ffmpeg merges the
  best h264 video-only + m4a audio (`-c copy`, no re-encode) and streams the mp4
  up to `merge_max_height` (1080p). **This is the only case where the server
  proxies media bandwidth.** `/v1/medium` stays the direct ~360p progressive,
  `/v1/audio` returns m4a, `/v1/video` the video-only HD stream. `/v1/formats`
  lists a `"merge"` choice (or `"<n>:merge"` for the nth video). Tokens are
  HMAC-signed and expire (`merge_url_ttl_seconds`). Without ffmpeg, merge is
  disabled and `/v1/best` falls back to the best single-file rendition.

- **gallery-dl auth (login-gated content).** Public posts need no auth. For
  private/followers-only content or other login-walled gallery-dl sites, set
  `cookies` / `cookies_from_browser` / `config_file` in the `[gallery]` config
  section.

## Contributing

Contributions to the library are welcome! If you encounter any issues or have suggestions for
improvements,
please feel free to submit a pull request or open an issue on the project's repository.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.