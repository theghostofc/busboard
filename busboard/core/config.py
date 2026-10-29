"""Configuration and secret storage for BusBoard.

All persistent state lives under two directories (overridable via env vars,
mainly so this can be exercised on a dev machine instead of the Pi):

- ``BUSBOARD_CONFIG_DIR`` (default ``/etc/busboard``): the YAML config file
  and the API key file. Both are written with ``0600`` permissions and are
  meant to be owned by the dedicated ``busboard`` system user, so only that
  user (and root) can read the API key or flip settings.
- ``BUSBOARD_STATE_DIR`` (default ``/var/lib/busboard``): mutable runtime
  state that isn't secret (rate limiter counters, cached departures).

Nothing in this module talks to the network or to systemd; it is pure
config/secret plumbing so it can be unit tested anywhere.
"""

from __future__ import annotations

import copy
import math
import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from werkzeug.security import check_password_hash, generate_password_hash

from busboard.core.countries import COUNTRIES

CONFIG_FILENAME = "config.yaml"
API_KEY_FILENAME = "api_key"
SECRET_KEY_FILENAME = "flask_secret_key"

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_QUARTER_HOUR_MINUTES = {"00", "15", "30", "45"}

# Number of on/off zones shown on the admin Schedule page. Kept fixed (rather
# than a dynamic add/remove list) so the page stays simple - three windows
# per day (e.g. morning commute / evening commute / late night) covers the
# vast majority of use cases without needing an "add zone" control.
SCHEDULE_ZONE_COUNT = 5

# Time display modes for departure rows on the board.
TIME_DISPLAY_MODES = ("clock", "minutes", "both")
CLOCK_FORMATS = ("24h", "12h")
HOTSPOT_SECURITY = ("open", "wpa2")
# "Leave now" badge: off, walking icon only, or icon + "Leave now".
LEAVE_NOW_STYLES = ("off", "icon", "text")
LEAVE_LINE_MODES = ("all", "highlighted")
DEFAULT_LEAVE_MARGIN_MINUTES = 2
MAX_LEAVE_MARGIN_MINUTES = 10
ALL_DAYS = (0, 1, 2, 3, 4, 5, 6)  # Monday = 0, like datetime.weekday()
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Sensible, conservative defaults. mobiliteit.lu's documented default quota
# is 500 requests/hour and 5000/day; we default to a bit under that so a
# single BusBoard never eats the whole quota on its own.
DEFAULT_HOURLY_LIMIT = 400
DEFAULT_DAILY_LIMIT = 4000

DEFAULT_WALK_MINUTES = 5
MAX_WALK_MINUTES = 30

# WiFi regulatory domain (ISO 3166-1 alpha-2), chosen on the admin WiFi page.
DEFAULT_WIFI_COUNTRY = "LU"
WIFI_COUNTRIES = COUNTRIES  # all countries, sorted by name
_WIFI_COUNTRY_CODES = {c for c, _ in WIFI_COUNTRIES}


def _choice(value, allowed: tuple, default: str) -> str:
    v = str(value if value is not None else default).strip().lower()
    return v if v in allowed else default


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def config_dir() -> Path:
    return Path(os.environ.get("BUSBOARD_CONFIG_DIR", "/etc/busboard"))


def state_dir() -> Path:
    return Path(os.environ.get("BUSBOARD_STATE_DIR", "/var/lib/busboard"))


def runtime_dir() -> Path:
    """tmpfs directory shared by the BusBoard services (systemd
    ``RuntimeDirectory=busboard``). For frequently-changing state that must
    not wear the SD card; cleared on reboot."""
    return Path(os.environ.get("BUSBOARD_RUNTIME_DIR", "/run/busboard"))


def config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def api_key_path() -> Path:
    return config_dir() / API_KEY_FILENAME


class ConfigError(ValueError):
    """Raised for invalid configuration values."""


def _validate_time(value: str, field_name: str) -> str:
    if not _TIME_RE.match(value):
        raise ConfigError(f"{field_name} must be in HH:MM 24h format, got {value!r}")
    return value


@dataclass
class StopConfig:
    """One physical stop/platform the board watches for departures."""

    id: str = ""
    name: str = ""
    # Optional: restrict to a terminus stop id, filtering only journeys
    # heading that way (see the API's "direction" parameter). Usually left
    # blank since direction is normally implied by which physical stop id
    # is used in Luxembourg.
    direction_filter: str = ""
    # Optional comma separated line filter, e.g. "12,16".
    lines: str = ""
    label: str = ""
    # Optional platform/quai filter, e.g. "1" or "2". At bus stops that
    # serve multiple directions from the same stop ID, each direction has
    # its own platform number (quai). This maps to the API's `platforms`
    # parameter on the departureBoard call.
    platform: str = ""
    # Optional comma-separated list of line numbers to highlight on the board,
    # e.g. "305,306". Matching rows get a visual accent so they stand out
    # from other departures at the same stop. Does not filter — all lines
    # still appear, favourites are just emphasised.
    favourites: str = ""
    # Minutes it takes to walk from the board to this stop. The board marks
    # the next departure you can still catch with a "Leave now" badge.
    # 0 disables the badge for this stop.
    walk_minutes: int = DEFAULT_WALK_MINUTES
    # Which lines get the badge: "all", or "highlighted" (the favourites
    # above; falls back to all lines when none are highlighted).
    leave_lines: str = "all"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "direction_filter": self.direction_filter,
            "lines": self.lines,
            "label": self.label,
            "platform": self.platform,
            "favourites": self.favourites,
            "walk_minutes": self.walk_minutes,
            "leave_lines": self.leave_lines,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "StopConfig":
        data = data or {}
        return cls(
            id=str(data.get("id", "")).strip(),
            name=str(data.get("name", "")).strip(),
            direction_filter=str(data.get("direction_filter", "")).strip(),
            lines=str(data.get("lines", "")).strip(),
            label=str(data.get("label", "")).strip(),
            platform=str(data.get("platform", "")).strip(),
            favourites=str(data.get("favourites", "")).strip(),
            walk_minutes=_int_or(data.get("walk_minutes"), DEFAULT_WALK_MINUTES),
            leave_lines=_choice(data.get("leave_lines"), LEAVE_LINE_MODES, "all"),
        )

    def validate(self) -> None:
        if not (0 <= self.walk_minutes <= MAX_WALK_MINUTES):
            raise ConfigError(f"walking time must be between 0 and {MAX_WALK_MINUTES} minutes")

    @property
    def query_key(self) -> tuple:
        """Fields that change what the API is asked for."""
        return (self.id, self.direction_filter, self.lines, self.platform)

    @property
    def configured(self) -> bool:
        return bool(self.id)

    @property
    def favourites_set(self) -> set[str]:
        """Normalised set of favourite line numbers for fast membership tests."""
        return {f.strip() for f in self.favourites.split(",") if f.strip()}


@dataclass
class ApiConfig:
    poll_interval_seconds: int = 30
    duration_minutes: int = 60
    max_journeys: int = 8
    request_timeout_seconds: int = 10
    hourly_limit: int = DEFAULT_HOURLY_LIMIT
    daily_limit: int = DEFAULT_DAILY_LIMIT
    base_url: str = "https://cdt.hafas.de/opendata/apiserver"

    def to_dict(self) -> dict:
        return {
            "poll_interval_seconds": self.poll_interval_seconds,
            "duration_minutes": self.duration_minutes,
            "max_journeys": self.max_journeys,
            "request_timeout_seconds": self.request_timeout_seconds,
            "hourly_limit": self.hourly_limit,
            "daily_limit": self.daily_limit,
            "base_url": self.base_url,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ApiConfig":
        d = data or {}
        defaults = cls()
        return cls(
            poll_interval_seconds=int(d.get("poll_interval_seconds", defaults.poll_interval_seconds)),
            duration_minutes=int(d.get("duration_minutes", defaults.duration_minutes)),
            max_journeys=int(d.get("max_journeys", defaults.max_journeys)),
            request_timeout_seconds=int(d.get("request_timeout_seconds", defaults.request_timeout_seconds)),
            hourly_limit=int(d.get("hourly_limit", defaults.hourly_limit)),
            daily_limit=int(d.get("daily_limit", defaults.daily_limit)),
            base_url=str(d.get("base_url", defaults.base_url)),
        )

    def validate(self) -> None:
        if self.poll_interval_seconds < 10:
            raise ConfigError("poll_interval_seconds must be >= 10 to respect API throttling")
        if not (1 <= self.duration_minutes <= 1439):
            raise ConfigError("duration_minutes must be between 1 and 1439")
        if self.max_journeys < 1:
            raise ConfigError("max_journeys must be >= 1")
        if self.hourly_limit < 1 or self.daily_limit < 1:
            raise ConfigError("rate limits must be >= 1")


def _validate_quarter_hour(value: str, field_name: str) -> str:
    _validate_time(value, field_name)
    if value[3:5] not in _QUARTER_HOUR_MINUTES:
        raise ConfigError(f"{field_name} must be on a 15-minute increment (:00, :15, :30, :45), got {value!r}")
    return value


def _parse_days(value) -> list[int]:
    if value is None:
        return list(ALL_DAYS)
    days = set()
    for v in value if isinstance(value, (list, tuple)) else [value]:
        try:
            d = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= d <= 6:
            days.add(d)
    return sorted(days)


@dataclass
class ScheduleZone:
    """One on/off window for the display, on selected days of the week.

    Several of these (see :data:`SCHEDULE_ZONE_COUNT`) express e.g. "weekdays
    06:30-09:00 and 16:00-20:00, weekends 08:00-12:00". An overnight window
    (off earlier than on) belongs to the day it *starts*: a Friday
    22:00-02:00 window covers Friday night into Saturday morning.
    """

    enabled: bool = False
    on_time: str = "06:30"
    off_time: str = "23:30"
    days: list[int] = field(default_factory=lambda: list(ALL_DAYS))

    def to_dict(self) -> dict:
        return {"enabled": self.enabled, "on_time": self.on_time, "off_time": self.off_time, "days": list(self.days)}

    @classmethod
    def from_dict(cls, data: dict | None) -> "ScheduleZone":
        d = data or {}
        defaults = cls()
        return cls(
            enabled=bool(d.get("enabled", defaults.enabled)),
            on_time=str(d.get("on_time", defaults.on_time)),
            off_time=str(d.get("off_time", defaults.off_time)),
            days=_parse_days(d.get("days")),
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        _validate_quarter_hour(self.on_time, "on_time")
        _validate_quarter_hour(self.off_time, "off_time")
        if not self.days:
            raise ConfigError("an enabled window needs at least one day")

    def is_active(self, when: datetime) -> bool:
        """Whether this window covers ``when`` (a local-time datetime)."""
        if not self.enabled:
            return False
        hhmm = when.strftime("%H:%M")
        today = when.weekday()
        yesterday = (today - 1) % 7
        if self.on_time == self.off_time:
            return today in self.days  # all day
        if self.on_time < self.off_time:
            return today in self.days and self.on_time <= hhmm < self.off_time
        # Overnight: evening part belongs to today, early-morning part to yesterday.
        return (today in self.days and hhmm >= self.on_time) or (yesterday in self.days and hhmm < self.off_time)


@dataclass
class ScheduleConfig:
    """Optional daily on/off windows for the display (to save the panel and
    avoid a lit screen glowing all night, e.g. in a bedroom).

    Made up of a fixed set of independent :class:`ScheduleZone` windows
    (see :data:`SCHEDULE_ZONE_COUNT`) rather than a single window, so users
    can express things like "on for the morning commute, off mid-day, on
    again in the evening". If none of the zones are enabled, the display
    stays on at all times (matches the previous single-window behavior when
    that window was disabled).
    """

    zones: list[ScheduleZone] = field(default_factory=lambda: [ScheduleZone() for _ in range(SCHEDULE_ZONE_COUNT)])

    def to_dict(self) -> dict:
        return {"zones": [z.to_dict() for z in self.zones]}

    @classmethod
    def from_dict(cls, data: dict | None) -> "ScheduleConfig":
        d = data or {}
        if "zones" in d and isinstance(d["zones"], list):
            zones = [ScheduleZone.from_dict(z) for z in d["zones"][:SCHEDULE_ZONE_COUNT]]
        elif "enabled" in d or "on_time" in d or "off_time" in d:
            # Legacy single-window format (pre-multi-zone config.yaml) —
            # migrate it into the first zone, leaving the rest disabled.
            zones = [ScheduleZone.from_dict(d)]
        else:
            zones = []
        while len(zones) < SCHEDULE_ZONE_COUNT:
            zones.append(ScheduleZone())
        return cls(zones=zones)

    def validate(self) -> None:
        for zone in self.zones:
            zone.validate()

    def is_active(self, when: datetime) -> bool:
        """Whether the display should be ON at ``when`` (local datetime).

        ON if no zone is enabled (schedule effectively off), or if any
        enabled zone's window covers the current time.
        """
        enabled_zones = [z for z in self.zones if z.enabled]
        if not enabled_zones:
            return True
        return any(z.is_active(when) for z in enabled_zones)


@dataclass
class DisplayConfig:
    """Presentation options for the departure board itself (not the setup
    UI). Applied by the browser board (templates/board.html)."""

    # Physical mounting can require the panel to be installed upside down
    # (e.g. cable routing); since there's no accelerometer/gyroscope to
    # auto-detect orientation, the user picks it once here.
    rotate_180: bool = False
    # "clock": HH:MM only (previous/default behaviour).
    # "minutes": minutes-until-departure only (e.g. "12 min").
    # "both": minutes-until as the primary figure with the clock time shown
    # underneath, similar to real transit departure boards.
    time_display_mode: str = "clock"
    # Shows the device's local IP address in small text under the
    # date/time block, so whoever is standing in front of the board can
    # find the admin UI without needing router access. On by default since
    # it's low-risk (LAN-local address, not a secret) and genuinely useful
    # for a headless device with no other way to discover its address.
    show_ip_address: bool = True
    # Wheelchair icon on step-free journeys. Off by default: in Luxembourg
    # nearly every bus is marked accessible, so the icon adds clutter.
    show_accessibility: bool = False
    # "24h" (23:58) or "12h" (11:58 PM) - applies to every clock time shown.
    clock_format: str = "24h"
    leave_now_style: str = "text"
    # "Leave now" shows from walking time + margin down to walking time.
    leave_now_margin_minutes: int = DEFAULT_LEAVE_MARGIN_MINUTES

    def to_dict(self) -> dict:
        return {
            "rotate_180": self.rotate_180,
            "time_display_mode": self.time_display_mode,
            "show_ip_address": self.show_ip_address,
            "show_accessibility": self.show_accessibility,
            "clock_format": self.clock_format,
            "leave_now_style": self.leave_now_style,
            "leave_now_margin_minutes": self.leave_now_margin_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "DisplayConfig":
        d = data or {}
        defaults = cls()
        mode = str(d.get("time_display_mode", defaults.time_display_mode)).lower()
        if mode not in TIME_DISPLAY_MODES:
            mode = defaults.time_display_mode
        return cls(
            rotate_180=bool(d.get("rotate_180", defaults.rotate_180)),
            time_display_mode=mode,
            show_ip_address=bool(d.get("show_ip_address", defaults.show_ip_address)),
            show_accessibility=bool(d.get("show_accessibility", defaults.show_accessibility)),
            clock_format=_choice(d.get("clock_format"), CLOCK_FORMATS, defaults.clock_format),
            leave_now_style=_choice(d.get("leave_now_style"), LEAVE_NOW_STYLES, defaults.leave_now_style),
            leave_now_margin_minutes=_int_or(d.get("leave_now_margin_minutes"), defaults.leave_now_margin_minutes),
        )

    def validate(self) -> None:
        if self.clock_format not in CLOCK_FORMATS:
            raise ConfigError(f"clock format must be one of {CLOCK_FORMATS}")
        if self.leave_now_style not in LEAVE_NOW_STYLES:
            raise ConfigError(f"leave-now style must be one of {LEAVE_NOW_STYLES}")
        if not (0 <= self.leave_now_margin_minutes <= MAX_LEAVE_MARGIN_MINUTES):
            raise ConfigError(f"leave-now margin must be between 0 and {MAX_LEAVE_MARGIN_MINUTES} minutes")
        if self.time_display_mode not in TIME_DISPLAY_MODES:
            raise ConfigError(
                f"time_display_mode must be one of {TIME_DISPLAY_MODES} — got {self.time_display_mode!r}"
            )


@dataclass
class WifiApConfig:
    """Settings for netwatch: the self-healing ladder and the fallback
    access point it brings up when the Pi has no network link at all.

    See docs/ARCHITECTURE.md ("Network resilience") for the full timeline.
    """

    ssid: str = "BusBoard-Setup"
    password: str = ""
    # "open": no password, run by NetworkManager (works on the Pi 3).
    # "wpa2": uses `password`, run by hostapd (busboard-hotspot.service) -
    # NetworkManager's WPA2 hotspot rejects every phone on this WiFi chip.
    security: str = "open"
    # How long the WiFi link must be down (with no Ethernet either) before
    # the fallback AP is started. Long enough to ride out a router reboot
    # (~2-3 min), during which netwatch already tries to reconnect, cycle
    # the radio and restart NetworkManager.
    grace_period_seconds: int = 300
    check_interval_seconds: int = 20
    # While the AP is up (and nobody is connected to it), take it down this
    # often and try the saved networks again - the single radio can't scan
    # or join a network while it's running the AP.
    retry_interval_seconds: int = 300
    # Reboot after this long without any network link / with a broken WiFi
    # link, as the last resort for a wedged WiFi chip. Backs off
    # exponentially (x2 each consecutive reboot, capped at 24h) so a router
    # that's gone for good doesn't cause a reboot loop. 0 disables.
    reboot_after_seconds: int = 1800

    @property
    def effective_password(self) -> str:
        """Password phones need to join, or "" for an open hotspot."""
        return self.password if self.security == "wpa2" else ""

    def to_dict(self) -> dict:
        return {
            "ssid": self.ssid,
            "password": self.password,
            "security": self.security,
            "grace_period_seconds": self.grace_period_seconds,
            "check_interval_seconds": self.check_interval_seconds,
            "retry_interval_seconds": self.retry_interval_seconds,
            "reboot_after_seconds": self.reboot_after_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "WifiApConfig":
        d = data or {}
        defaults = cls()
        return cls(
            ssid=str(d.get("ssid", defaults.ssid)) or defaults.ssid,
            password=str(d.get("password", defaults.password)),
            security=_choice(d.get("security"), HOTSPOT_SECURITY, defaults.security),
            grace_period_seconds=int(d.get("grace_period_seconds", defaults.grace_period_seconds)),
            check_interval_seconds=int(d.get("check_interval_seconds", defaults.check_interval_seconds)),
            retry_interval_seconds=int(d.get("retry_interval_seconds", defaults.retry_interval_seconds)),
            reboot_after_seconds=int(d.get("reboot_after_seconds", defaults.reboot_after_seconds)),
        )

    def validate(self) -> None:
        if self.password and len(self.password) < 8:
            raise ConfigError("WiFi AP password must be empty (open) or at least 8 characters (WPA2 minimum)")
        if self.security not in HOTSPOT_SECURITY:
            raise ConfigError(f"hotspot security must be one of {HOTSPOT_SECURITY}")
        if self.security == "wpa2" and len(self.password) < 8:
            raise ConfigError("a WPA2 hotspot needs a password of at least 8 characters")
        if self.check_interval_seconds < 5:
            raise ConfigError("check_interval_seconds must be >= 5")
        if self.grace_period_seconds < 30:
            raise ConfigError("grace_period_seconds must be >= 30")
        if self.retry_interval_seconds < 60:
            raise ConfigError("retry_interval_seconds must be >= 60")
        if self.reboot_after_seconds and self.reboot_after_seconds < 600:
            raise ConfigError("reboot_after_seconds must be 0 (disabled) or >= 600")


@dataclass
class AdminConfig:
    """Login for the admin pages."""

    username: str = "admin"
    password_hash: str = ""
    # The generated first-run (or reset) password, kept in plaintext only
    # until it's changed on /admin/account, so the board can show it. Lives
    # in config.yaml (0600, busboard user only); never logged.
    initial_password: str = ""

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "password_hash": self.password_hash,
            "initial_password": self.initial_password,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "AdminConfig":
        d = data or {}
        defaults = cls()
        return cls(
            username=str(d.get("username", defaults.username)) or defaults.username,
            password_hash=str(d.get("password_hash", "")),
            initial_password=str(d.get("initial_password", "") or ""),
        )

    @property
    def configured(self) -> bool:
        return bool(self.password_hash)

    def verify(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)


def is_daytime(lat: float, lon: float, when: Optional[datetime] = None) -> bool:
    """Return True if the sun is above the horizon at the given location and time.

    Uses the NOAA simplified solar position algorithm (accurate to ~1 minute).
    ``when`` defaults to the current UTC time. ``lat``/``lon`` are in decimal
    degrees (positive = N/E, negative = S/W).

    Falls back to True (light theme) on any arithmetic error.
    """
    try:
        now = when or datetime.now(timezone.utc)
        # Julian date
        jd = now.timestamp() / 86400.0 + 2440587.5
        # Julian century from J2000.0
        jc = (jd - 2451545.0) / 36525.0
        # Geometric mean longitude of sun (degrees)
        gml = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
        # Geometric mean anomaly of sun (degrees)
        gma = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
        # Sun equation of centre
        gmar = math.radians(gma)
        eoc = (math.sin(gmar) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
               + math.sin(2 * gmar) * (0.019993 - 0.000101 * jc)
               + math.sin(3 * gmar) * 0.000289)
        # Sun true longitude
        stl = gml + eoc
        # Sun apparent longitude
        sal = stl - 0.00569 - 0.00478 * math.sin(math.radians(125.04 - 1934.136 * jc))
        # Mean obliquity of ecliptic
        moe = (23 + (26 + (21.448 - jc * (46.8150 + jc * (0.00059 - jc * 0.001813))) / 60) / 60)
        # Obliquity correction
        oc = moe + 0.00256 * math.cos(math.radians(125.04 - 1934.136 * jc))
        # Sun declination
        dec = math.degrees(math.asin(math.sin(math.radians(oc)) * math.sin(math.radians(sal))))
        # Equation of time (minutes)
        vary = math.tan(math.radians(oc / 2)) ** 2
        gmlr = math.radians(gml)
        gmar2 = math.radians(gma)
        eot = 4 * math.degrees(
            vary * math.sin(2 * gmlr)
            - 2 * _ecc(jc) * math.sin(gmar2)
            + 4 * _ecc(jc) * vary * math.sin(gmar2) * math.cos(2 * gmlr)
            - 0.5 * vary ** 2 * math.sin(4 * gmlr)
            - 1.25 * _ecc(jc) ** 2 * math.sin(2 * gmar2)
        )
        # Hour angle at sunrise/sunset (degrees), solar zenith = 90.833°
        latr = math.radians(lat)
        decr = math.radians(dec)
        cos_ha = (math.cos(math.radians(90.833)) / (math.cos(latr) * math.cos(decr))
                  - math.tan(latr) * math.tan(decr))
        # Clamp: above ±1 means polar day/night
        if cos_ha <= -1:
            return True   # polar day
        if cos_ha >= 1:
            return False  # polar night
        ha = math.degrees(math.acos(cos_ha))
        # Solar noon in minutes from midnight UTC
        solar_noon = 720 - 4 * lon - eot
        # Sunrise / sunset in minutes from midnight UTC
        sunrise_utc = solar_noon - ha * 4
        sunset_utc  = solar_noon + ha * 4
        # Current time in minutes from midnight UTC
        minutes_utc = now.hour * 60 + now.minute + now.second / 60
        return sunrise_utc <= minutes_utc <= sunset_utc
    except Exception:  # noqa: BLE001
        return True  # safe fallback: light


def _ecc(jc: float) -> float:
    """Earth orbit eccentricity at Julian century jc."""
    return 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)


@dataclass
class Config:
    stop_a: StopConfig = field(default_factory=StopConfig)
    stop_b: StopConfig = field(default_factory=StopConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    wifi_ap: WifiApConfig = field(default_factory=WifiApConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    timezone: str = "Europe/Luxembourg"
    # Applied to the WiFi chip by the root-owned busboard-wifi-country
    # service whenever config.yaml changes (and at boot).
    wifi_country: str = DEFAULT_WIFI_COUNTRY
    # Colour theme for the departure board.
    # "light" and "dark" are explicit overrides.
    # "auto" switches between dark (night) and light (day) based on
    # sunrise/sunset at the configured stop's location.
    theme: str = "auto"

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "timezone": self.timezone,
            "wifi_country": self.wifi_country,
            "theme": self.theme,
            "stop_a": self.stop_a.to_dict(),
            "stop_b": self.stop_b.to_dict(),
            "api": self.api.to_dict(),
            "schedule": self.schedule.to_dict(),
            "display": self.display.to_dict(),
            "wifi_ap": self.wifi_ap.to_dict(),
            "admin": self.admin.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Config":
        d = data or {}
        raw_theme = str(d.get("theme", "auto")).lower()
        theme = raw_theme if raw_theme in ("light", "dark", "auto") else "auto"
        return cls(
            stop_a=StopConfig.from_dict(d.get("stop_a")),
            stop_b=StopConfig.from_dict(d.get("stop_b")),
            api=ApiConfig.from_dict(d.get("api")),
            schedule=ScheduleConfig.from_dict(d.get("schedule")),
            display=DisplayConfig.from_dict(d.get("display")),
            wifi_ap=WifiApConfig.from_dict(d.get("wifi_ap")),
            admin=AdminConfig.from_dict(d.get("admin")),
            timezone=str(d.get("timezone", "Europe/Luxembourg")),
            wifi_country=str(d.get("wifi_country", DEFAULT_WIFI_COUNTRY)).strip().upper() or DEFAULT_WIFI_COUNTRY,
            theme=theme,
        )

    def validate(self) -> None:
        self.stop_a.validate()
        self.stop_b.validate()
        if self.wifi_country not in _WIFI_COUNTRY_CODES:
            raise ConfigError(f"unsupported WiFi country {self.wifi_country!r}")
        self.api.validate()
        self.schedule.validate()
        self.display.validate()
        self.wifi_ap.validate()
        if self.theme not in ("light", "dark", "auto"):
            raise ConfigError(f"theme must be one of light, dark, auto — got {self.theme!r}")

    def copy(self) -> "Config":
        return copy.deepcopy(self)


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically with restrictive permissions.

    Writes to a temp file in the same directory then ``os.replace``s it into
    place, so a crash mid-write never leaves a corrupt/partial config file.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def load_config(path: Path | None = None) -> Config:
    """Load config from disk, returning defaults if the file doesn't exist.

    Does not write anything to disk; callers that want to persist a freshly
    created default config should call :func:`save_config` explicitly.
    """
    p = path or config_path()
    if not p.exists():
        return Config()
    with open(p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Config.from_dict(raw)


def save_config(config: Config, path: Path | None = None) -> None:
    config.validate()
    p = path or config_path()
    data = yaml.safe_dump(config.to_dict(), sort_keys=False, allow_unicode=True).encode("utf-8")
    _atomic_write(p, data, mode=0o600)


def get_api_key(path: Path | None = None) -> Optional[str]:
    p = path or api_key_path()
    if not p.exists():
        return None
    key = p.read_text(encoding="utf-8").strip()
    return key or None


def set_api_key(key: str, path: Path | None = None) -> None:
    key = key.strip()
    if not key:
        raise ConfigError("API key must not be empty")
    p = path or api_key_path()
    _atomic_write(p, key.encode("utf-8"), mode=0o600)


def api_key_configured(path: Path | None = None) -> bool:
    return bool(get_api_key(path))


def get_or_create_flask_secret_key(path: Path | None = None) -> str:
    """Return a persisted random secret key for Flask session signing,
    generating one on first run. Kept in the same secured config directory
    as the API key (0600, busboard-user-owned) so session cookies can't be
    forged by other local users."""
    p = path or (config_dir() / SECRET_KEY_FILENAME)
    if p.exists():
        key = p.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_hex(32)
    _atomic_write(p, key.encode("utf-8"), mode=0o600)
    return key


def generate_password(length: int = 16) -> str:
    """Generate a random, URL-safe password for first-run admin credentials
    or the fallback WiFi AP."""
    # Letters and digits only (no "-"/"_", no look-alikes like 0/O, 1/l/I):
    # it's read off a screen and typed on a phone.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def set_admin_password(config: Config, password: str) -> Config:
    if len(password) < 8:
        raise ConfigError("Admin password must be at least 8 characters")
    new_config = config.copy()
    new_config.admin.password_hash = generate_password_hash(password)
    new_config.admin.initial_password = ""  # a chosen password: stop showing it
    return new_config


def ensure_admin_password(config: Config) -> tuple[Config, Optional[str]]:
    """Make sure an admin password exists.

    Returns the (possibly updated) config and the plaintext password if one
    was freshly generated (so the caller can display/print it once), or
    ``None`` if a password was already configured.
    """
    if config.admin.configured:
        return config, None
    return reset_admin_password(config)


def reset_admin_password(config: Config) -> tuple[Config, str]:
    """Generate a new admin password and keep it as ``initial_password`` so
    the board shows it until it's changed."""
    plaintext = generate_password(12)
    new_config = set_admin_password(config, plaintext)
    new_config.admin.initial_password = plaintext
    return new_config, plaintext


def file_permissions_ok(path: Path) -> bool:
    """Best-effort check that a secret file isn't group/world readable."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return True
    return not (mode & (stat.S_IRWXG | stat.S_IRWXO))
