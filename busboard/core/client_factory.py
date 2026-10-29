"""Small shared helper for building a :class:`MobiliteitClient` from the
persisted config + API key, used by both the web app (for stop lookups and
the status page) and the display process (for polling).

Kept in its own module (rather than duplicated in each entrypoint) so both
have consistent behavior around error messages when the API key hasn't been
configured yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from busboard.core.api_client import MobiliteitClient
from busboard.core.config import Config, get_api_key
from busboard.core.ratelimiter import get_shared_limiter


class ApiKeyNotConfigured(Exception):
    """Raised when no API key has been set up yet."""


def build_client(config: Config, api_key_path: Optional[Path] = None) -> MobiliteitClient:
    api_key = get_api_key(api_key_path)
    if not api_key:
        raise ApiKeyNotConfigured(
            "No mobiliteit.lu API key configured yet. Add one on the setup page."
        )
    limiter = get_shared_limiter(
        hourly_limit=config.api.hourly_limit,
        daily_limit=config.api.daily_limit,
    )
    return MobiliteitClient(
        api_key=api_key,
        rate_limiter=limiter,
        base_url=config.api.base_url,
        timeout_seconds=config.api.request_timeout_seconds,
    )


class CachedClientFactory:
    """Reuses a single :class:`MobiliteitClient` across repeated calls
    instead of rebuilding one from scratch every time.

    ``build_client`` on its own is cheap to call occasionally (e.g. once
    per web request), but the display's poller calls its client factory on
    every poll cycle - by default every 30 seconds, forever. Rebuilding
    from scratch each time discards the client's ``requests.Session``,
    forcing a fresh TCP+TLS handshake with the API on every poll and
    re-reading the rate limiter's on-disk state every time, both
    unnecessary CPU/latency overhead on a Raspberry Pi 3B+.

    This wrapper keeps the same client (and its connection pool and
    in-memory rate limiter) alive as long as the API key and the relevant
    :class:`ApiConfig` fields haven't changed since the last call, so
    changes made via the admin UI still take effect on the next poll,
    without paying the rebuild cost when nothing relevant has changed.
    """

    def __init__(self, api_key_path: Optional[Path] = None):
        self._api_key_path = api_key_path
        self._cache_key: Optional[tuple] = None
        self._client: Optional[MobiliteitClient] = None

    def get(self, config: Config) -> MobiliteitClient:
        api_key = get_api_key(self._api_key_path)
        if not api_key:
            self._client = None
            self._cache_key = None
            raise ApiKeyNotConfigured(
                "No mobiliteit.lu API key configured yet. Add one on the setup page."
            )
        cache_key = (
            api_key,
            config.api.base_url,
            config.api.request_timeout_seconds,
            config.api.hourly_limit,
            config.api.daily_limit,
        )
        if self._client is None or cache_key != self._cache_key:
            self._client = build_client(config, self._api_key_path)
            self._cache_key = cache_key
        return self._client
