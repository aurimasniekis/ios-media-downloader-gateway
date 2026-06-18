"""Map a URL host to a configured, enabled site."""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import HTTPException

from .config import Config, SiteConfig


def resolve(url: str, config: Config) -> tuple[str, SiteConfig]:
    """Return (site_name, site_config) for the URL or raise 422.

    Unknown and disabled sites are rejected.
    """
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        raise HTTPException(status_code=422, detail="could not parse host from url")

    for name, site in config.sites.items():
        if not site.enabled:
            continue
        for domain in site.domains:
            domain = domain.lower()
            if host == domain or host.endswith("." + domain):
                return name, site

    raise HTTPException(status_code=422, detail=f"unsupported site: {host}")
