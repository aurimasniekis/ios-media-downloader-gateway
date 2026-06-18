# ios-media-downloader-gateway
# https://github.com/aurimasniekis/ios-media-downloader-gateway
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# ffmpeg is required for YouTube HD server-side merge (/v1/best -> /v1/stream).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# Install dependencies first (cached unless pyproject.toml / uv.lock change).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Then the app source + install the project itself.
COPY README.md ./
COPY app ./app
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Config (config.toml) and the SQLite audit DB live under /app/data — mount it.
VOLUME ["/app/data"]
EXPOSE 8080

CMD ["python", "-m", "app.main", "--config", "/app/data/config.toml"]
