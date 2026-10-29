"""Persisted, thread-safe request budget tracking for the mobiliteit.lu API.

The API enforces hourly and daily quotas (defaults documented as 500/hour
and 5000/day). We track our own usage against a slightly lower configured
limit and persist counters to disk so a service restart doesn't forget how
many requests were already spent in the current hour/day - that matters
because the board poller and the admin UI (for stop lookups) share the
same quota and busboard-web can be restarted often.
"""

from __future__ import annotations

import atexit
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from busboard.core.config import runtime_dir, state_dir

STATE_FILENAME = "rate_limit_state.json"

# Where the counters live:
#   - every request updates the tmpfs copy (no SD card write);
#   - the SD card copy is written only when the process exits cleanly
#     (service stop/restart, reboot), so counts survive a reboot.
# After a power cut the counts restart from the last clean shutdown; the
# default limits sit below the real quota to absorb that.


def rate_limit_state_path() -> Path:
    """On-disk copy (SD card): written only at a clean shutdown/restart."""
    return state_dir() / STATE_FILENAME


def rate_limit_runtime_path() -> Path:
    """Live copy on tmpfs (/run/busboard): updated on every request at no
    SD card cost, and survives a service restart (but not a reboot)."""
    return runtime_dir() / STATE_FILENAME


class RateLimitExceeded(Exception):
    """Raised when a call would exceed the configured hourly/daily budget."""

    def __init__(self, scope: str, retry_after_seconds: float):
        self.scope = scope
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"{scope} request budget exceeded; retry in {retry_after_seconds:.0f}s"
        )


@dataclass
class _Window:
    period_start: int  # unix epoch, floored to the window boundary
    count: int


def _hour_bucket(ts: float) -> int:
    return int(ts // 3600) * 3600


def _day_bucket(ts: float) -> int:
    return int(ts // 86400) * 86400


class RateLimiter:
    """Sliding-window-by-bucket limiter for hourly and daily quotas.

    Not perfectly precise (it resets at fixed clock boundaries rather than a
    true rolling window), but that's exactly how HAFAS-style quotas are
    typically enforced server-side, and it's simple enough to reason about
    and to persist safely across process restarts.
    """

    def __init__(
        self,
        hourly_limit: int,
        daily_limit: int,
        state_path: Optional[Path] = None,
        disk_path: Optional[Path] = None,
    ):
        """``state_path`` is the live (tmpfs) copy, ``disk_path`` the copy
        written at exit. Passing only ``state_path`` uses it for both."""
        self.hourly_limit = hourly_limit
        self.daily_limit = daily_limit
        self._state_path = state_path or rate_limit_runtime_path()
        self._disk_path = disk_path or (state_path if state_path else rate_limit_state_path())
        self._lock = threading.Lock()
        self._hour = _Window(period_start=0, count=0)
        self._day = _Window(period_start=0, count=0)
        self._dirty = False
        self._runtime_ok = True
        self._load()

    def _load(self) -> None:
        # Take the highest count for the current windows from either copy.
        now = time.time()
        for path in {self._state_path, self._disk_path}:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if raw.get("hour_start", 0) == _hour_bucket(now):
                count = int(raw.get("hour_count", 0))
                if count > self._hour.count or self._hour.period_start == 0:
                    self._hour = _Window(period_start=raw["hour_start"], count=max(count, self._hour.count))
            if raw.get("day_start", 0) == _day_bucket(now):
                count = int(raw.get("day_count", 0))
                if count > self._day.count or self._day.period_start == 0:
                    self._day = _Window(period_start=raw["day_start"], count=max(count, self._day.count))

    def _persist(self, path: Optional[Path] = None, fsync: bool = False) -> None:
        path = path or self._state_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        payload = {
            "hour_start": self._hour.period_start,
            "hour_count": self._hour.count,
            "day_start": self._day.period_start,
            "day_count": self._day.count,
        }
        data = json.dumps(payload).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{STATE_FILENAME}.")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                if fsync:
                    f.flush()
                    os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def _roll_windows(self, now: float) -> None:
        hb = _hour_bucket(now)
        if self._hour.period_start != hb:
            self._hour = _Window(period_start=hb, count=0)
        db = _day_bucket(now)
        if self._day.period_start != db:
            self._day = _Window(period_start=db, count=0)

    def _record(self) -> None:
        self._dirty = True
        if not self._runtime_ok or self._state_path == self._disk_path:
            return  # no tmpfs available: only write at exit
        try:
            self._persist(self._state_path)
        except OSError:
            # Never fail an API call because the state file couldn't be written.
            self._runtime_ok = False

    def flush(self) -> None:
        """Write the counters to the SD card copy (called at exit)."""
        with self._lock:
            if not self._dirty:
                return
            try:
                self._persist(self._disk_path, fsync=True)
                self._dirty = False
            except OSError:
                pass

    def update_limits(self, hourly_limit: int, daily_limit: int) -> None:
        with self._lock:
            self.hourly_limit = hourly_limit
            self.daily_limit = daily_limit

    def remaining(self) -> dict:
        """Requests left and seconds until each window resets."""
        with self._lock:
            now = time.time()
            self._roll_windows(now)
            return {
                "hour_remaining": max(self.hourly_limit - self._hour.count, 0),
                "hour_resets_in": self._hour.period_start + 3600 - now,
                "day_remaining": max(self.daily_limit - self._day.count, 0),
                "day_resets_in": self._day.period_start + 86400 - now,
                "day_resets_at": float(self._day.period_start + 86400),
            }

    def usage(self) -> dict:
        with self._lock:
            self._roll_windows(time.time())
            return {
                "hour_count": self._hour.count,
                "hour_limit": self.hourly_limit,
                "day_count": self._day.count,
                "day_limit": self.daily_limit,
            }

    def try_acquire(self) -> None:
        """Reserve one request's worth of budget, or raise
        :class:`RateLimitExceeded` if that would exceed either quota.

        Must be called before making the actual HTTP request.
        """
        with self._lock:
            now = time.time()
            self._roll_windows(now)
            if self._hour.count >= self.hourly_limit:
                retry = self._hour.period_start + 3600 - now
                raise RateLimitExceeded("hourly", max(retry, 0.0))
            if self._day.count >= self.daily_limit:
                retry = self._day.period_start + 86400 - now
                raise RateLimitExceeded("daily", max(retry, 0.0))
            self._hour.count += 1
            self._day.count += 1
            self._record()


_SHARED: dict[str, RateLimiter] = {}
_SHARED_LOCK = threading.Lock()


def get_shared_limiter(hourly_limit: int, daily_limit: int, state_path: Optional[Path] = None) -> RateLimiter:
    """One limiter per state file per process.

    The board poller and the admin UI's stop/platform lookups all run in the
    busboard-web process. Separate limiter instances each kept their own
    in-memory counts and overwrote each other's state file, so usage was
    under-counted. Sharing one instance keeps a single, correct count.
    """
    path = state_path or rate_limit_state_path()
    key = str(path)
    with _SHARED_LOCK:
        limiter = _SHARED.get(key)
        if limiter is None:
            limiter = RateLimiter(hourly_limit, daily_limit, state_path=path)
            _SHARED[key] = limiter
        else:
            limiter.update_limits(hourly_limit, daily_limit)
        return limiter


@atexit.register
def _flush_shared() -> None:
    for limiter in list(_SHARED.values()):
        limiter.flush()
