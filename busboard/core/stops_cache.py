"""Local cache of all mobiliteit.lu stops, used to power the admin Stops page's
autocomplete without hammering the API on every keystroke.

The mobiliteit.lu API has no text-search endpoint (only ``departureBoard``
and ``location.nearbystops`` by coordinate exist - confirmed by hitting
``location.name`` directly, which 404s). The documented way to enumerate
*all* stops in the country in one call is to query ``nearbystops`` from a
central coordinate with a large enough radius; see the "get all stops"
example in the official dataset docs (data.public.lu, "API mobiliteit.lu").

So the flow is:

1. On demand (first autocomplete request, or when the cache is stale/
   missing), fetch the full stop list from the API once via
   :func:`refresh_cache` and persist it to disk under ``BUSBOARD_STATE_DIR``.
2. All subsequent autocomplete queries filter that cached list locally in
   memory/string matching - no further API calls, no risk of eating into the
   hourly/daily request budget just because someone is typing in the setup
   page.
3. The cache has a configurable max age (default 7 days, stops rarely
   change) after which the next autocomplete request triggers one
   background refresh.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from busboard.core.api_client import MobiliteitClient, NearbyStop
from busboard.core.config import state_dir

logger = logging.getLogger(__name__)

STOPS_CACHE_FILENAME = "stops_cache.json"

# The location.nearbystops API caps results at 5000 per call. A single query
# from a country-center coordinate works fine for a plain stop list, but with
# type=SE the item count can approach or exceed that cap, causing the API to
# silently truncate by distance from the query origin. Luxembourg City stops
# (Hamilius, Gare Centrale etc.) disappear first because they're the farthest
# from a northern-Luxembourg origin point.
#
# Solution: two overlapping queries whose combined coverage spans the whole
# country, each centered to ensure neither drops the other's region under the
# cap. Merge by stop ID to de-duplicate the overlap zone.
#
# Query A: centered on Luxembourg City (49.60, 6.13) with r=60km — covers the
#   southern half including the capital and the French/Belgian border region.
# Query B: centered on the northern plateau (50.00, 6.10) with r=60km — covers
#   the north (Ardennes, Our valley, Ettelbruck area) down into the overlap.
#
# Combined these cover the full ~80km N-S extent with a comfortable overlap
# in the center of the country, and neither region alone generates enough
# items to approach 5000.
_QUERY_REGIONS = (
    {"lat": 49.60, "lon": 6.13, "r": 60_000},  # Luxembourg City + south
    {"lat": 50.00, "lon": 6.10, "r": 60_000},  # Northern plateau
)
REGION_MAX_RESULTS = 5000

DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 3600


def stops_cache_path() -> Path:
    return state_dir() / STOPS_CACHE_FILENAME


class StopsCacheError(Exception):
    """Raised when the stops cache can't be loaded or refreshed."""


@dataclass
class CachedStop:
    id: str
    name: str
    lat: float
    lon: float
    ext_id: str = ""  # numeric platform/stop code from the HAFAS L= field

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "lat": self.lat, "lon": self.lon, "ext_id": self.ext_id}

    @classmethod
    def from_dict(cls, d: dict) -> "CachedStop":
        return cls(
            id=str(d["id"]),
            name=str(d["name"]),
            lat=float(d["lat"]),
            lon=float(d["lon"]),
            ext_id=str(d.get("ext_id", "")),
        )

    @classmethod
    def from_nearby_stop(cls, stop: NearbyStop) -> "CachedStop":
        return cls(id=stop.id, name=stop.name, lat=stop.lat, lon=stop.lon, ext_id=stop.ext_id)

    @property
    def display_label(self) -> str:
        """Human-readable label for the autocomplete dropdown — just the
        stop name. The platform/quai is entered separately by the user once
        they've selected the stop."""
        return self.name


@dataclass
class StopsCache:
    fetched_at: float
    stops: list[CachedStop]
    # Precomputed accent-stripped/lowercased name per stop (parallel list,
    # same order as `stops`), built once here rather than on every
    # search_stops() call. With up to ~5000 nationwide stops, redoing
    # Unicode NFKD decomposition for the whole list on every keystroke would
    # be wasted CPU on a Pi 3B+; this is computed once per cache load/refresh
    # instead, which happens at most a few times a week.
    normalized_names: list[str] = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self) -> None:
        if len(self.normalized_names) != len(self.stops):
            # Normalize the name only (not ext_id) so searching "hamilius"
            # still matches even though the display label has "[200426002]".
            self.normalized_names = [_normalize(s.name) for s in self.stops]

    def is_stale(self, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> bool:
        return (time.time() - self.fetched_at) > max_age_seconds

    def to_dict(self) -> dict:
        return {"fetched_at": self.fetched_at, "stops": [s.to_dict() for s in self.stops]}

    @classmethod
    def from_dict(cls, d: dict) -> "StopsCache":
        return cls(
            fetched_at=float(d.get("fetched_at", 0)),
            stops=[CachedStop.from_dict(s) for s in d.get("stops", [])],
        )


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def load_cache(path: Optional[Path] = None) -> Optional[StopsCache]:
    """Load the on-disk stops cache, or ``None`` if it doesn't exist yet or
    is corrupt (treated the same as missing, triggering a fresh refresh)."""
    p = path or stops_cache_path()
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return StopsCache.from_dict(raw)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("stops cache at %s is unreadable, will refresh: %s", p, exc)
        return None


def refresh_cache(client: MobiliteitClient, path: Optional[Path] = None) -> StopsCache:
    """Fetch the full nationwide stop list from the API and persist it.

    Issues one ``nearbystops`` call per region in ``_QUERY_REGIONS`` and
    merges results by stop ID, so the API's 5000-result-per-call cap cannot
    silently drop a geographic area due to distance from a single origin.
    """
    p = path or stops_cache_path()
    by_id: dict[str, NearbyStop] = {}
    region_counts: list[int] = []

    for region in _QUERY_REGIONS:
        region_stops = client.nearby_stops(
            lat=region["lat"],
            lon=region["lon"],
            radius_meters=region["r"],
            max_results=REGION_MAX_RESULTS,
        )
        region_counts.append(len(region_stops))
        for stop in region_stops:
            if stop.id and stop.name:
                by_id[stop.id] = stop

    cache = StopsCache(
        fetched_at=time.time(),
        stops=[CachedStop.from_nearby_stop(s) for s in by_id.values()],
    )
    _atomic_write_json(p, cache.to_dict())
    logger.info(
        "refreshed stops cache: %d unique stops from %d regions (per-region counts: %s)",
        len(cache.stops),
        len(region_counts),
        region_counts,
    )
    if any(count >= int(REGION_MAX_RESULTS * 0.95) for count in region_counts):
        logger.warning(
            "a region returned close to the %d-result cap; some stops may still be missing",
            REGION_MAX_RESULTS,
        )
    return cache


# A single process-wide lock so concurrent autocomplete requests don't all
# trigger simultaneous refreshes against the API.
_refresh_lock = threading.Lock()


def get_or_refresh_cache(
    client_factory,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    path: Optional[Path] = None,
) -> StopsCache:
    """Return a usable stops cache, refreshing from the API only if missing
    or stale. ``client_factory`` is a zero-arg callable building a
    :class:`MobiliteitClient` (raises if no API key is configured yet).
    """
    cache = load_cache(path)
    if cache is not None and not cache.is_stale(max_age_seconds):
        return cache

    with _refresh_lock:
        # Re-check after acquiring the lock in case another thread already
        # refreshed while we were waiting.
        cache = load_cache(path)
        if cache is not None and not cache.is_stale(max_age_seconds):
            return cache
        try:
            return refresh_cache(client_factory(), path)
        except Exception as exc:  # noqa: BLE001
            if cache is not None:
                logger.warning("stops cache refresh failed, serving stale cache: %s", exc)
                return cache
            raise StopsCacheError(f"could not build stops cache: {exc}") from exc


def _normalize(text: str) -> str:
    """Lowercase and strip accents for forgiving matching (e.g. "Cote" should
    match "Côte") without needing a fuzzy-matching dependency."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def search_stops(cache: StopsCache, query: str, limit: int = 40) -> list[CachedStop]:
    """Rank cached stops by relevance to a free-text query.

    Ranking (best first): name starts with the query, a "word" within the
    name starts with the query, the query appears anywhere in the name.
    Ties are broken by shorter name first (more likely to be the intended
    match rather than an unrelated stop that happens to contain the text).
    """
    q = _normalize(query.strip())
    if not q:
        return []

    scored: list[tuple[int, int, CachedStop]] = []
    for stop, name_norm in zip(cache.stops, cache.normalized_names):
        if q not in name_norm:
            continue
        if name_norm.startswith(q):
            rank = 0
        elif any(word.startswith(q) for word in name_norm.replace(",", " ").split()):
            rank = 1
        else:
            rank = 2
        scored.append((rank, len(name_norm), stop))

    scored.sort(key=lambda t: (t[0], t[1]))
    return [s for _, _, s in scored[:limit]]
