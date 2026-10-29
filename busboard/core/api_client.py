"""Client for the mobiliteit.lu (ATP) HAFAS-based open data API.

Two endpoints are used:

- ``location.nearbystops``: look up stop IDs near a coordinate (used only by
  the setup web UI to help you find a stop id, not by the display loop).
- ``departureBoard``: real time departures for a given stop id (used by the
  display loop every poll interval).

All calls go through a :class:`busboard.core.ratelimiter.RateLimiter` so the
configured hourly/daily budget is respected; the limiter is consulted before
every HTTP request, and a :class:`RateLimitExceeded` propagates straight to
the caller if the budget is used up.

The API key is passed to ``__init__`` explicitly by the caller (read from
the secured api_key file) rather than read from disk in here, so this module
has no direct knowledge of where secrets are stored and stays easy to test.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, tzinfo
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from busboard.core.ratelimiter import RateLimiter

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://cdt.hafas.de/opendata/apiserver"


# ApiError.kind values - what the caller should do about an error.
ERR_QUOTA = "quota"  # API_QUOTA / HTTP 429: stop calling until the quota resets
ERR_AUTH = "auth"  # API_AUTH / 401 / 403: key missing/invalid, retrying won't help soon
ERR_NO_RESULT = "no_result"  # SVC_NO_RESULT / SVC_NO_MATCH: a valid "nothing departs"
ERR_BAD_REQUEST = "bad_request"  # API_PARAM, SVC_LOC, ...: configuration problem
ERR_TRANSIENT = "transient"  # network, timeouts, 5xx, INT_ERR: retry with backoff

_NO_RESULT_CODES = {"SVC_NO_RESULT", "SVC_NO_MATCH"}
_QUOTA_CODES = {"API_QUOTA"}
_AUTH_CODES = {"API_AUTH"}
_BAD_REQUEST_PREFIXES = ("API_PARAM", "API_FORMAT", "SVC_PARAM", "SVC_LOC", "SVC_DATATIME", "SVC_PROD", "SVC_CTX")


def classify_error(error_code: Optional[str], http_status: Optional[int]) -> str:
    """Map a HAFAS errorCode / HTTP status to an ``ERR_*`` kind (see the
    error code table in the mobiliteit.lu departureBoard docs)."""
    code = (error_code or "").upper()
    if code in _NO_RESULT_CODES:
        return ERR_NO_RESULT
    if code in _QUOTA_CODES or http_status == 429:
        return ERR_QUOTA
    if code in _AUTH_CODES or http_status in (401, 403):
        return ERR_AUTH
    if code.startswith(_BAD_REQUEST_PREFIXES):
        return ERR_BAD_REQUEST
    return ERR_TRANSIENT


class ApiError(Exception):
    """Raised for API-level errors (bad key, bad params, server error).

    ``kind`` is one of the ``ERR_*`` constants; ``retry_after`` is the
    server's Retry-After hint in seconds, when it sent one.
    """

    def __init__(
        self,
        message: str,
        error_code: str | None = None,
        http_status: int | None = None,
        retry_after: float | None = None,
        kind: str | None = None,
    ):
        self.error_code = error_code
        self.http_status = http_status
        self.retry_after = retry_after
        self.kind = kind or classify_error(error_code, http_status)
        super().__init__(message)


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None  # HTTP-date form: not worth parsing here


DEFAULT_TIMEZONE = "Europe/Luxembourg"


@lru_cache(maxsize=8)
def get_timezone(name: str = DEFAULT_TIMEZONE) -> tzinfo:
    """The API returns local (Luxembourg) wall-clock times, so all
    "minutes until" maths is done in that zone, independent of whatever
    timezone the Pi's OS happens to be set to."""
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("unknown timezone %r, falling back to %s", name, DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE)


@dataclass(frozen=True)
class Departure:
    """A single upcoming departure, normalized for display."""

    line: str
    category: str  # e.g. "Bus", "Tram", "Train"
    direction: str  # human readable destination
    scheduled_time: str  # "HH:MM"
    scheduled_date: str  # "YYYY-MM-DD"
    realtime_time: Optional[str]  # "HH:MM" if realtime info is available
    cancelled: bool
    delay_minutes: Optional[int]  # None if no realtime info yet
    platform: str = ""  # quai number, e.g. "1" or "2", empty if not provided
    # Date of the realtime time; differs from scheduled_date when a delay
    # pushes a departure past midnight. Empty when there's no realtime info.
    realtime_date: str = ""
    # Step-free / wheelchair accessible vehicle, from the journey's notes.
    accessible: bool = False
    # Disruption / info texts for this journey (realtime + infotext notes and
    # HIM messages), most important first.
    notices: tuple = ()

    @property
    def display_time(self) -> str:
        return self.realtime_time or self.scheduled_time

    @property
    def display_date(self) -> str:
        return (self.realtime_date if self.realtime_time else "") or self.scheduled_date

    @property
    def has_realtime(self) -> bool:
        return self.realtime_time is not None

    @property
    def is_delayed(self) -> bool:
        return bool(self.delay_minutes and self.delay_minutes > 0)

    @property
    def sort_key(self) -> tuple:
        """Chronological order (realtime if known). Sorting on the bare
        "HH:MM" string put 00:05 before 23:55 around midnight."""
        return (self.display_date, self.display_time)

    def departure_datetime(self, tz: Optional[tzinfo] = None) -> Optional[datetime]:
        """Timezone-aware departure time (realtime if available)."""
        try:
            naive = datetime.strptime(f"{self.display_date} {self.display_time}", "%Y-%m-%d %H:%M")
        except ValueError:
            return None
        return naive.replace(tzinfo=tz or get_timezone())

    def epoch(self, tz: Optional[tzinfo] = None) -> Optional[float]:
        dt = self.departure_datetime(tz)
        return dt.timestamp() if dt else None

    def has_departed(self, now: Optional[datetime] = None, tz: Optional[tzinfo] = None) -> bool:
        """True once the departure minute has fully passed (HH:MM:59), so a
        bus stays on the board as "Due" during its departure minute and is
        then dropped instead of lingering while the data is stale."""
        tz = tz or get_timezone()
        dt = self.departure_datetime(tz)
        if dt is None:
            return False
        now = now or datetime.now(tz)
        return (now - dt).total_seconds() >= 60

    def minutes_until(self, now: Optional[datetime] = None, tz: Optional[tzinfo] = None) -> Optional[int]:
        """Whole minutes from ``now`` until the (realtime, else scheduled)
        departure, rounded down like real departure boards, so "Due" means
        less than a minute to go. Returns ``None`` if the time can't be
        parsed and ``0`` once the departure time has been reached.

        ``now`` may be naive (interpreted in ``tz``) or aware.
        """
        tz = tz or get_timezone()
        dt = self.departure_datetime(tz)
        if dt is None:
            return None
        now = now or datetime.now(tz)
        if now.tzinfo is None:
            now = now.replace(tzinfo=tz)
        seconds = (dt - now).total_seconds()
        return max(math.floor(seconds / 60), 0)


@dataclass(frozen=True)
class NearbyStop:
    id: str
    ext_id: str
    name: str
    lat: float
    lon: float
    distance_meters: Optional[int]


def _minutes_between(date_a: str, time_a: str, date_b: str, time_b: str) -> Optional[int]:
    fmt = "%Y-%m-%d %H:%M:%S"

    def _norm_time(t: str) -> str:
        # API times are "HH:MM:SS"; guard against "HH:MM" just in case.
        return t if t.count(":") == 2 else f"{t}:00"

    try:
        dt_a = datetime.strptime(f"{date_a} {_norm_time(time_a)}", fmt)
        dt_b = datetime.strptime(f"{date_b} {_norm_time(time_b)}", fmt)
    except ValueError:
        return None
    return round((dt_b - dt_a).total_seconds() / 60)


_CANCEL_NOTE_KEYS = {"text.realtime.stop.cancelled", "text.realtime.journey.cancelled"}
_ACCESSIBLE_NOTE_KEYS = {"aa", "eh"}
_ACCESSIBLE_WORDS = ("barrierefrei", "accessible", "rollstuhl", "fauteuil", "pmr", "wheelchair")
MAX_NOTICES_PER_DEPARTURE = 3
MAX_NOTICE_LENGTH = 220


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _clean_text(text) -> str:
    return " ".join(str(text or "").split())[:MAX_NOTICE_LENGTH]


def parse_notes(raw: dict) -> tuple[bool, bool, tuple]:
    """Extract (cancelled_at_stop, accessible, notices) from a departure's
    ``Notes`` and ``Messages``.

    Note ``type`` is A (attribute), R (realtime) or I (infotext); R/I notes
    are the operator's service messages. ``priority``: lower = more
    important. ``Messages`` (HIM disruption messages) are handled
    defensively since their exact shape isn't documented for this API.
    """
    notes_container = raw.get("Notes") or {}
    notes = _as_list(notes_container.get("Note") if isinstance(notes_container, dict) else notes_container)
    cancelled = False
    accessible = False
    ranked: list[tuple[int, str]] = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        key = str(note.get("key") or "")
        ntype = str(note.get("type") or "").upper()
        texts = " ".join(str(note.get(k) or "") for k in ("value", "txtN", "txtL")).lower()
        if key in _CANCEL_NOTE_KEYS:
            cancelled = True
            continue
        if key.lower() in _ACCESSIBLE_NOTE_KEYS or any(w in texts for w in _ACCESSIBLE_WORDS):
            accessible = True
            continue
        if ntype in ("R", "I") and key != "OPERATOR":
            text = _clean_text(note.get("txtL") or note.get("value") or note.get("txtN"))
            if text:
                try:
                    prio = int(note.get("priority", 500))
                except (TypeError, ValueError):
                    prio = 500
                ranked.append((prio, text))

    messages_container = raw.get("Messages") or {}
    messages = _as_list(messages_container.get("Message") if isinstance(messages_container, dict) else messages_container)
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("act") is False:
            continue
        head = _clean_text(msg.get("head"))
        body = _clean_text(msg.get("text") or msg.get("lead"))
        text = f"{head}: {body}" if head and body and body != head else (head or body)
        if text:
            try:
                prio = int(msg.get("priority", 100))
            except (TypeError, ValueError):
                prio = 100
            ranked.append((prio, _clean_text(text)))

    seen: set[str] = set()
    notices = []
    for _, text in sorted(ranked, key=lambda r: r[0]):
        if text not in seen:
            seen.add(text)
            notices.append(text)
    return cancelled, accessible, tuple(notices[:MAX_NOTICES_PER_DEPARTURE])


def _parse_departure(raw: dict) -> Departure:
    # ProductAtStop describes the product as it serves *this* stop; Product
    # is a list over the whole journey (the line can change mid-journey).
    product = raw.get("ProductAtStop") or raw.get("Product") or {}
    # Product can be a list (e.g. several products over one journey). Take the first.
    if isinstance(product, list):
        product = product[0] if product else {}

    scheduled_time = str(raw.get("time") or "")
    scheduled_date = str(raw.get("date") or "")

    # rtTime/rtDate can also be lists in rare API responses — take first element
    realtime_time = raw.get("rtTime")
    if isinstance(realtime_time, list):
        realtime_time = realtime_time[0] if realtime_time else None
    realtime_date = raw.get("rtDate") or scheduled_date
    if isinstance(realtime_date, list):
        realtime_date = realtime_date[0] if realtime_date else scheduled_date
    # "reachable": false means this stop is skipped in realtime (e.g. a
    # diversion) - for someone waiting here that's the same as cancelled.
    cancelled = bool(raw.get("cancelled", False)) or raw.get("reachable") is False
    note_cancelled, accessible, notices = parse_notes(raw)
    cancelled = cancelled or note_cancelled

    delay_minutes = None
    if realtime_time:
        delay_minutes = _minutes_between(scheduled_date, scheduled_time, realtime_date, realtime_time)

    # platform is a dict like {"type": "ST", "text": "2"} or None; a
    # realtime platform change (rtPlatform) wins over the planned one.
    platform_raw = raw.get("rtPlatform") or raw.get("platform")
    if isinstance(platform_raw, dict):
        platform = str(platform_raw.get("text", "")).strip()
    elif platform_raw:
        platform = str(platform_raw).strip()
    else:
        platform = ""

    return Departure(
        line=str(product.get("line") or raw.get("trainNumber") or ""),
        category=str(product.get("catOutL") or product.get("catOut") or ""),
        direction=str(raw.get("direction", "")),
        scheduled_time=scheduled_time[:5],
        scheduled_date=scheduled_date,
        realtime_time=(str(realtime_time)[:5] if realtime_time else None),
        cancelled=cancelled,
        delay_minutes=delay_minutes,
        platform=platform,
        realtime_date=(str(realtime_date) if realtime_time else ""),
        accessible=accessible,
        notices=notices,
    )


def _parse_nearby_stop(raw: dict) -> NearbyStop:
    return NearbyStop(
        id=str(raw.get("id", "")),
        ext_id=str(raw.get("extId", "")),
        name=str(raw.get("name", "")),
        lat=float(raw.get("lat", 0.0)),
        lon=float(raw.get("lon", 0.0)),
        distance_meters=(int(raw["dist"]) if "dist" in raw else None),
    )


def _describe_network_error(path: str, exc: requests.RequestException) -> str:
    """Turn a low-level ``requests``/``urllib3`` exception into a short,
    human-readable message safe to show on the board or write to logs.

    Deliberately never interpolates ``str(exc)`` (or the request URL) into
    the returned message: ``requests``/``urllib3`` connection errors (e.g.
    "Max retries exceeded with url: ...") embed the *full request URL*,
    including the ``accessId`` query parameter - which is the mobiliteit.lu
    API key. Letting that string reach ``ApiError`` would leak the key onto
    the physical display and into the journal.
    """
    if isinstance(exc, requests.ConnectTimeout):
        return f"Timed out connecting to mobiliteit.lu while calling {path}"
    if isinstance(exc, requests.ReadTimeout):
        return f"mobiliteit.lu did not respond in time while calling {path}"
    if isinstance(exc, requests.Timeout):
        return f"Request to mobiliteit.lu timed out while calling {path}"
    if isinstance(exc, requests.ConnectionError):
        return f"Could not reach mobiliteit.lu while calling {path} (no network / DNS / connection failure)"
    return f"Network error calling {path}: {type(exc).__name__}"


class MobiliteitClient:
    """Thin wrapper around the mobiliteit.lu HAFAS opendata API."""

    def __init__(
        self,
        api_key: str,
        rate_limiter: RateLimiter,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: int = 10,
        session: Optional[requests.Session] = None,
    ):
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._rate_limiter = rate_limiter
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._session = session or requests.Session()

    @property
    def rate_limiter(self) -> RateLimiter:
        return self._rate_limiter

    def _get(self, path: str, params: dict) -> dict:
        self._rate_limiter.try_acquire()
        query = {"accessId": self._api_key, "format": "json", **params}
        url = f"{self._base_url}/{path}"
        try:
            resp = self._session.get(url, params=query, timeout=self._timeout)
        except requests.RequestException as exc:
            # Only log/raise the sanitized description - see
            # _describe_network_error for why the raw exception (which
            # embeds the full request URL, including the API key) must
            # never be logged or surfaced, not even at debug level.
            message = _describe_network_error(path, exc)
            logger.warning("%s (%s)", message, type(exc).__name__)
            raise ApiError(message, kind=ERR_TRANSIENT) from exc

        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
        try:
            data = resp.json()
        except ValueError as exc:
            raise ApiError(
                f"Non-JSON response from {path} (HTTP {resp.status_code})",
                http_status=resp.status_code,
                retry_after=retry_after,
            ) from exc

        if isinstance(data, dict) and data.get("errorCode"):
            code = str(data.get("errorCode"))
            raise ApiError(
                str(data.get("errorText") or code),
                error_code=code,
                http_status=resp.status_code,
                retry_after=retry_after,
            )
        if not resp.ok:
            raise ApiError(
                f"HTTP {resp.status_code} calling {path}",
                http_status=resp.status_code,
                retry_after=retry_after,
            )
        return data

    def departure_board(
        self,
        stop_id: str,
        direction_filter: str = "",
        lines: str = "",
        platform: str = "",
        duration_minutes: int = 60,
        max_journeys: int = 8,
    ) -> list[Departure]:
        if not stop_id:
            raise ValueError("stop_id must not be empty")
        params: dict = {
            "id": stop_id,
            "duration": duration_minutes,
            "maxJourneys": max_journeys,
            # This server only accepts OFF or SERVER_DEFAULT (FULL, as some
            # docs suggest, is rejected with API_PARAM). SERVER_DEFAULT = realtime on.
            "rtMode": "SERVER_DEFAULT",
        }
        if direction_filter:
            params["direction"] = direction_filter
        if lines:
            params["lines"] = lines
        if platform:
            params["platforms"] = platform

        try:
            data = self._get("departureBoard", params)
        except ApiError as exc:
            if exc.kind == ERR_NO_RESULT:
                return []  # "no result found" just means nothing departs
            raise
        raw_departures = data.get("Departure") or []
        if isinstance(raw_departures, dict):
            # HAFAS returns a bare dict (not a list) when there's exactly one result.
            raw_departures = [raw_departures]
        departures = [_parse_departure(d) for d in raw_departures]
        departures.sort(key=lambda d: d.sort_key)
        return departures

    def nearby_stops(
        self,
        lat: float,
        lon: float,
        radius_meters: int = 1000,
        max_results: int = 50,
    ) -> list[NearbyStop]:
        params = {
            "originCoordLat": lat,
            "originCoordLong": lon,
            "r": radius_meters,
            "maxNo": max_results,
            # "SE" (not plain "S") per the official mobiliteit.lu docs'
            # "get all stops" example - it returns stops, platforms/quays,
            # AND entrance points. Plain "S" is narrower and was silently
            # excluding real stops from the nationwide fetch (e.g.
            # "Hamilius" in Luxembourg City never showed up in the cache).
            "type": "SE",
        }
        data = self._get("location.nearbystops", params)
        raw = data.get("stopLocationOrCoordLocation") or data.get("StopLocation") or []
        if isinstance(raw, dict):
            raw = [raw]
        stops = []
        for item in raw:
            # Some responses nest under "StopLocation" per item.
            stop_data = item.get("StopLocation", item) if isinstance(item, dict) else item
            stops.append(_parse_nearby_stop(stop_data))
        return stops
