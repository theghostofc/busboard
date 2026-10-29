"""Background polling of the departure board for both configured stops.

The poller runs inside the ``busboard-web`` process (which also serves the
kiosk board), keeping the last successful result per stop so a transient API
error or rate-limit hit doesn't blank the display.

Polling follows the display schedule: a fetch 30 s before each on-time, no
poll less than 60 s before an off-time, nothing while the display is off.
API errors pause or back off according to their kind (quota, auth,
transient - see api_client.classify_error).

Poll interval is adaptive (see :func:`compute_poll_interval`):

- never faster than the configured ``poll_interval_seconds``;
- slower when nothing departs in the next ~20 minutes (realtime changes for
  a bus that far out don't matter yet);
- slowed down just enough to make the daily/hourly request budget last until
  it resets, counting only the time the display schedule is actually on.
  With the old fixed 30 s interval, two stops used 5,760 requests/day against
  a 4,000 budget, so the board went stale every evening from ~18:40.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Optional

from busboard.core.api_client import (
    ERR_AUTH,
    ERR_BAD_REQUEST,
    ERR_QUOTA,
    ERR_TRANSIENT,
    ApiError,
    Departure,
    MobiliteitClient,
    get_timezone,
)
from busboard.core.config import Config, ScheduleConfig, StopConfig
from busboard.core.ratelimiter import RateLimitExceeded

logger = logging.getLogger(__name__)

# While the display is scheduled off, re-check at least this often (so a
# schedule edit in the admin UI is picked up) - costs no API call.
SCHEDULE_INACTIVE_RECHECK_SECONDS = 300
# Fetch this long before the display switches on, so it wakes up with
# current times.
WARM_UP_SECONDS = 30
# Don't poll if the display switches off within this time - nobody would
# see the result.
SKIP_BEFORE_OFF_SECONDS = 60

# Error backoff (API "kind" -> behaviour, see api_client.classify_error).
TRANSIENT_BACKOFF_MAX_SECONDS = 300
AUTH_RETRY_SECONDS = 600
QUOTA_MAX_PAUSE_SECONDS = 3600

# Extra journeys requested beyond what's displayed, so rows that depart
# between polls (and are dropped from the board) are backfilled.
EXTRA_JOURNEYS = 4

# Nothing departing sooner than this -> poll at a relaxed pace.
RELAXED_IF_NEXT_DEPARTURE_AFTER_MIN = 20
RELAXED_INTERVAL_SECONDS = 60

MIN_INTERVAL_SECONDS = 10
MAX_INTERVAL_SECONDS = 900


@dataclass
class StopSnapshot:
    stop: StopConfig
    departures: list[Departure] = field(default_factory=list)
    error: Optional[str] = None
    # ERR_* kind of the last error (None after a success)
    error_kind: Optional[str] = None
    # Time of the last *successful* fetch for this stop.
    updated_at: Optional[float] = None

    @property
    def is_stale(self) -> bool:
        return self.updated_at is None


@dataclass
class BoardSnapshot:
    stop_a: StopSnapshot
    stop_b: StopSnapshot
    # Current (adaptive) poll interval, so the board can tell "a bit old"
    # from "stale".
    poll_interval_seconds: float = 30.0
    last_poll_at: Optional[float] = None
    # Set while polling is paused (quota / auth problem), wall clock.
    paused_until: Optional[float] = None
    pause_reason: Optional[str] = None


def active_seconds_between(schedule: ScheduleConfig, tz: tzinfo, start: float, end: float) -> float:
    """Seconds in [start, end) during which the display schedule is on."""
    if end <= start:
        return 0.0
    if not any(z.enabled for z in schedule.zones):
        return end - start
    active = 0.0
    t = start
    while t < end:
        step = min(60.0, end - t)
        if schedule.is_active(datetime.fromtimestamp(t, tz)):
            active += step
        t += step
    return active


def _ceil_minute(t: float) -> float:
    return math.ceil(t / 60.0) * 60.0


def _is_on(schedule: ScheduleConfig, tz: tzinfo, t: float) -> bool:
    return schedule.is_active(datetime.fromtimestamp(t, tz))


def next_switch_on(schedule: ScheduleConfig, tz: tzinfo, start: float, horizon: float = 2 * 86400) -> Optional[float]:
    """Wall-clock time of the next off->on transition after ``start``
    (schedule windows are minute-granular), or ``None`` if there is none
    (no zone enabled = always on)."""
    if not any(z.enabled for z in schedule.zones):
        return None
    b = _ceil_minute(start)
    prev = _is_on(schedule, tz, b - 60)
    end = start + horizon
    while b < end:
        cur = _is_on(schedule, tz, b)
        if cur and not prev:
            return b
        prev = cur
        b += 60
    return None


def compute_poll_interval(
    config: Config,
    remaining: Optional[dict],
    stops_per_poll: int,
    soonest_minutes: Optional[int],
    now: float,
    tz: tzinfo,
) -> float:
    """Pure: how long to wait before the next poll (see module docstring).

    ``remaining`` is :meth:`RateLimiter.remaining` output (or ``None`` if
    unknown).
    """
    base = float(config.api.poll_interval_seconds)
    if soonest_minutes is None or soonest_minutes > RELAXED_IF_NEXT_DEPARTURE_AFTER_MIN:
        base = max(base, RELAXED_INTERVAL_SECONDS)
    if not remaining or stops_per_poll <= 0:
        return min(max(base, MIN_INTERVAL_SECONDS), MAX_INTERVAL_SECONDS)

    # Keep a little headroom for the admin UI's lookups.
    day_reserve = max(20, math.ceil(config.api.daily_limit * 0.05))
    hour_reserve = max(5, math.ceil(config.api.hourly_limit * 0.05))

    day_left = remaining["day_remaining"] - day_reserve
    if day_left > 0:
        active = active_seconds_between(config.schedule, tz, now, now + remaining["day_resets_in"])
        day_floor = active * stops_per_poll / day_left
    else:
        day_floor = remaining["day_resets_in"]

    hour_left = remaining["hour_remaining"] - hour_reserve
    hour_floor = (
        remaining["hour_resets_in"] * stops_per_poll / hour_left if hour_left > 0 else remaining["hour_resets_in"]
    )

    interval = max(base, day_floor, hour_floor)
    return min(max(interval, MIN_INTERVAL_SECONDS), MAX_INTERVAL_SECONDS)


class DeparturePoller:
    """Polls both configured stops in a background thread, keeping the most
    recent successful result per stop so a transient API error or rate
    limit hit doesn't blank the display."""

    def __init__(self, config: Config, client_factory):
        """``client_factory`` is a zero-arg callable returning a configured
        :class:`MobiliteitClient` (or raising if the API key isn't set
        yet), so the poller can cope with the key being added later via the
        admin UI without needing a restart.
        """
        self._config = config
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._snapshot = BoardSnapshot(
            stop_a=StopSnapshot(stop=config.stop_a),
            stop_b=StopSnapshot(stop=config.stop_b),
            poll_interval_seconds=float(config.api.poll_interval_seconds),
        )
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._transient_failures = 0
        self._last_logged_interval = 0.0
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="departure-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def update_config(self, config: Config) -> None:
        with self._lock:
            old = self._config
            self._config = config
            self._snapshot.stop_a.stop = config.stop_a
            self._snapshot.stop_b.stop = config.stop_b
            stops_changed = (old.stop_a.query_key, old.stop_b.query_key) != (
                config.stop_a.query_key,
                config.stop_b.query_key,
            )
        if stops_changed:
            # New stop/filter saved in the admin UI: show it now, not after
            # the (possibly relaxed) interval.
            self._wake_event.set()

    def snapshot(self) -> BoardSnapshot:
        with self._lock:
            return BoardSnapshot(
                stop_a=dataclasses.replace(self._snapshot.stop_a, departures=list(self._snapshot.stop_a.departures)),
                stop_b=dataclasses.replace(self._snapshot.stop_b, departures=list(self._snapshot.stop_b.departures)),
                poll_interval_seconds=self._snapshot.poll_interval_seconds,
                last_poll_at=self._snapshot.last_poll_at,
                paused_until=self._snapshot.paused_until,
                pause_reason=self._snapshot.pause_reason,
            )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                interval = self._poll_once()
            except Exception:  # noqa: BLE001 - the poller thread must never die
                logger.exception("poll cycle failed")
                interval = 30
            self._wake_event.wait(max(interval, 1.0))
            self._wake_event.clear()

    def _schedule_wait(self, config: Config, tz: tzinfo, now: float) -> Optional[float]:
        """Seconds to wait if the schedule says not to poll now, else None.

        Polls from WARM_UP_SECONDS before an on-window until
        SKIP_BEFORE_OFF_SECONDS before it ends.
        """
        schedule = config.schedule
        on_now = _is_on(schedule, tz, now)
        on_soon = _is_on(schedule, tz, now + WARM_UP_SECONDS)
        if not on_now and not on_soon:
            reason = "display off"
        elif on_now and not _is_on(schedule, tz, now + SKIP_BEFORE_OFF_SECONDS):
            reason = "display switching off"
        else:
            return None
        nxt = next_switch_on(schedule, tz, now)
        if nxt is None:
            return SCHEDULE_INACTIVE_RECHECK_SECONDS
        wait = nxt - WARM_UP_SECONDS - now
        logger.debug("%s - next fetch in %.0fs", reason, wait)
        return min(max(wait, 1.0), SCHEDULE_INACTIVE_RECHECK_SECONDS)

    def _poll_once(self) -> float:
        with self._lock:
            config = self._config
            paused_until = self._snapshot.paused_until
        tz = get_timezone(config.timezone)
        now = time.time()

        wait = self._schedule_wait(config, tz, now)
        if wait is not None:
            return wait

        if paused_until and now < paused_until:
            return min(paused_until - now, SCHEDULE_INACTIVE_RECHECK_SECONDS)

        try:
            client = self._client_factory()
        except Exception as exc:  # noqa: BLE001 - surfaced to the display as a message
            logger.warning("cannot build API client: %s", exc)
            self._record_error(config.stop_a, str(exc), which="a", kind=ERR_AUTH)
            self._record_error(config.stop_b, str(exc), which="b", kind=ERR_AUTH)
            return config.api.poll_interval_seconds

        kinds: list[Optional[str]] = []
        retry_hint: Optional[float] = None
        for which, stop in (("a", config.stop_a), ("b", config.stop_b)):
            kind, hint = self._poll_stop(client, config, stop, which=which)
            kinds.append(kind)
            retry_hint = hint if hint is not None else retry_hint
            if kind in (ERR_QUOTA, ERR_AUTH, "limit"):
                # Same key/quota for both stops: don't waste the second call.
                other = ("b", config.stop_b) if which == "a" else None
                if other:
                    with self._lock:
                        src = self._snapshot.stop_a
                    self._record_error(other[1], src.error or "", which="b", kind=kind)
                break

        now = time.time()
        stops_per_poll = int(config.stop_a.configured) + int(config.stop_b.configured)
        try:
            remaining = client.rate_limiter.remaining()
        except AttributeError:
            remaining = None
        interval = compute_poll_interval(config, remaining, stops_per_poll, self._soonest_minutes(tz), now, tz)

        pause_until = None
        pause_reason = None
        if ERR_QUOTA in kinds:
            # mobiliteit.lu doesn't say when the quota resets; the docs say
            # "wait an hour or so". Use the server's hint, else the next
            # full hour.
            hour_end = math.floor(now / 3600 + 1) * 3600
            pause = retry_hint if retry_hint else (hour_end - now + 30)
            pause_until = now + min(max(pause, 60), QUOTA_MAX_PAUSE_SECONDS)
            pause_reason = "quota"
            logger.warning("mobiliteit.lu quota exceeded - pausing API calls for %.0fs", pause_until - now)
        elif "limit" in kinds:
            pause_until = now + min(max(retry_hint or 60, 60), 900)
            pause_reason = "limit"
        elif ERR_AUTH in kinds:
            pause_until = now + AUTH_RETRY_SECONDS
            pause_reason = "auth"
            logger.warning("mobiliteit.lu rejected the API key - retrying in %ss", AUTH_RETRY_SECONDS)

        if ERR_TRANSIENT in kinds:
            self._transient_failures += 1
            backoff = min(config.api.poll_interval_seconds * (2 ** self._transient_failures), TRANSIENT_BACKOFF_MAX_SECONDS)
            if retry_hint:
                backoff = max(backoff, min(retry_hint, TRANSIENT_BACKOFF_MAX_SECONDS))
            interval = max(interval, backoff)
        elif any(k is None for k in kinds):
            self._transient_failures = 0

        with self._lock:
            self._snapshot.poll_interval_seconds = interval
            self._snapshot.last_poll_at = now
            self._snapshot.paused_until = pause_until
            self._snapshot.pause_reason = pause_reason

        if pause_until:
            return pause_until - now
        if abs(interval - self._last_logged_interval) >= 15:
            logger.info("next poll in %.0fs", interval)
            self._last_logged_interval = interval
        return interval

    def _soonest_minutes(self, tz: tzinfo) -> Optional[int]:
        now = datetime.now(tz)
        soonest: Optional[int] = None
        with self._lock:
            deps = self._snapshot.stop_a.departures + self._snapshot.stop_b.departures
        for d in deps:
            if d.cancelled or d.has_departed(now, tz):
                continue
            m = d.minutes_until(now, tz)
            if m is not None and (soonest is None or m < soonest):
                soonest = m
        return soonest

    def _poll_stop(
        self, client: MobiliteitClient, config: Config, stop: StopConfig, which: str
    ) -> tuple[Optional[str], Optional[float]]:
        """Poll one stop. Returns (error kind or None on success, retry hint)."""
        if not stop.configured:
            self._record_error(stop, "Stop not configured", which=which, clear_departures=True, kind=ERR_BAD_REQUEST)
            return ERR_BAD_REQUEST, None
        try:
            departures = client.departure_board(
                stop_id=stop.id,
                direction_filter=stop.direction_filter,
                lines=stop.lines,
                platform=stop.platform,
                duration_minutes=config.api.duration_minutes,
                max_journeys=config.api.max_journeys + EXTRA_JOURNEYS,
            )
        except RateLimitExceeded as exc:
            logger.info("local request budget used up (stop %s): %s", stop.id, exc)
            self._record_error(stop, "Request budget used up", which=which, kind="limit")
            return "limit", exc.retry_after_seconds
        except ApiError as exc:
            logger.warning("API error for stop %s (%s): %s", stop.id, exc.kind, exc)
            messages = {
                ERR_QUOTA: "mobiliteit.lu request quota reached",
                ERR_AUTH: "API key rejected by mobiliteit.lu",
                ERR_BAD_REQUEST: f"Stop settings rejected by mobiliteit.lu: {exc}",
            }
            self._record_error(stop, messages.get(exc.kind, "mobiliteit.lu not responding"), which=which, kind=exc.kind)
            return exc.kind, exc.retry_after
        except Exception:  # noqa: BLE001
            logger.exception("unexpected error polling stop %s", stop.id)
            self._record_error(stop, "Unexpected error fetching departures", which=which, kind=ERR_TRANSIENT)
            return ERR_TRANSIENT, None
        with self._lock:
            target = self._snapshot.stop_a if which == "a" else self._snapshot.stop_b
            target.stop = stop
            target.departures = departures
            target.error = None
            target.error_kind = None
            target.updated_at = time.time()
        return None, None

    def _record_error(
        self,
        stop: StopConfig,
        message: str,
        which: str,
        clear_departures: bool = False,
        kind: Optional[str] = None,
    ) -> None:
        with self._lock:
            target = self._snapshot.stop_a if which == "a" else self._snapshot.stop_b
            target.stop = stop
            target.error = message
            target.error_kind = kind
            if clear_departures:
                target.departures = []
                target.updated_at = None
