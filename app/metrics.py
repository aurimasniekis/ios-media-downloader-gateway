"""Prometheus metrics and the /metrics exposition payload."""
from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

REQUESTS = Counter(
    "media_gateway_requests_total",
    "Total media requests received",
    ["endpoint", "api_key_name", "site"],
)
SUCCESS = Counter(
    "media_gateway_success_total",
    "Total successful media requests",
    ["endpoint", "api_key_name", "site"],
)
FAILURES = Counter(
    "media_gateway_failures_total",
    "Total failed media requests",
    ["endpoint", "api_key_name", "site", "reason"],
)
ITEMS_RETURNED = Histogram(
    "media_gateway_items_returned",
    "Number of media items returned per request",
    buckets=(0, 1, 2, 3, 5, 10, 20, 50),
)
EXTRACT_DURATION = Histogram(
    "media_gateway_extract_duration_seconds",
    "Duration of yt-dlp extract_info calls",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30),
)


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
