"""Flask application factory for BusBoard: the kiosk board, the public status
page and the admin area.

This is the only network-exposed service in the project (listens on the
LAN, and on the fallback WiFi AP when there's no uplink). It is gated by a
username/password login (credentials are generated on first run, see
:mod:`busboard.core.config`), sessions are signed with a persisted random
secret key, and all state-changing routes are CSRF-protected via
Flask-WTF-style hidden tokens (implemented by hand here since the rest of
the project intentionally keeps its dependency footprint small).

Security note: this is a small setup tool meant for trusted home network /
temporary-AP use, not a hardened multi-user web app. It is not rate-limited
against brute-force login attempts beyond what Flask's session mechanism
gives for free - acceptable for a device that lives on a private network,
but worth knowing if you plan to expose this beyond your LAN (don't).
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from datetime import datetime
from functools import wraps
from typing import Optional

import segno
import segno.helpers
from flask import Flask, flash, g, redirect, render_template, request, session, url_for

from busboard.core import network
from busboard.core.api_client import Departure, get_timezone
from busboard.core.client_factory import (
    ApiKeyNotConfigured,
    CachedClientFactory,
    build_client,
)
from busboard.core.config import (
    ALL_DAYS,
    CLOCK_FORMATS,
    DAY_NAMES,
    HOTSPOT_SECURITY,
    LEAVE_LINE_MODES,
    LEAVE_NOW_STYLES,
    TIME_DISPLAY_MODES,
    WIFI_COUNTRIES,
    ConfigError,
    api_key_configured,
    ensure_admin_password,
    generate_password,
    get_or_create_flask_secret_key,
    is_daytime,
    load_config,
    save_config,
    set_admin_password,
    set_api_key,
)
from busboard.core.poller import DeparturePoller
from busboard.core.ratelimiter import get_shared_limiter
from busboard.core.stops_cache import (
    StopsCacheError,
    get_or_refresh_cache,
    search_stops,
)

logger = logging.getLogger(__name__)

# ---- Server-side platform/quai cache ----------------------------------
# Keyed by stop ID, value is (timestamp, platforms_list).
# Avoids repeating the departureBoard API call every time the user clicks
# the same stop in the admin UI. 6-hour TTL is plenty — platforms don't
# change during a service day.
_time = time
_PLATFORM_CACHE: dict[str, tuple[float, list]] = {}
_PLATFORM_CACHE_TTL = 6 * 3600  # seconds


def _get_cached_platforms(stop_id: str) -> list | None:
    entry = _PLATFORM_CACHE.get(stop_id)
    if entry and (_time.time() - entry[0]) < _PLATFORM_CACHE_TTL:
        return entry[1]
    return None


def _set_cached_platforms(stop_id: str, platforms: list) -> None:
    _PLATFORM_CACHE[stop_id] = (_time.time(), platforms)


# Stop coordinates for the "auto" theme, memoized per (stop ids, cache file
# mtime). Loading the nationwide stops cache means parsing a multi-MB JSON
# file and normalizing every stop name - that used to happen on every board
# refresh (every 30 s) on the Pi.
_THEME_COORDS_MEMO: dict[tuple, Optional[tuple[float, float]]] = {}


def _stop_coordinates(config) -> Optional[tuple[float, float]]:
    from busboard.core.stops_cache import load_cache, stops_cache_path

    try:
        mtime = stops_cache_path().stat().st_mtime
    except OSError:
        return None
    key = (config.stop_a.id, config.stop_b.id, mtime)
    if key in _THEME_COORDS_MEMO:
        return _THEME_COORDS_MEMO[key]
    coords = None
    try:
        cache = load_cache()
        if cache:
            wanted = [sid for sid in (config.stop_a.id, config.stop_b.id) if sid]
            by_id = {s.id: s for s in cache.stops if s.id in wanted}
            for sid in wanted:
                s = by_id.get(sid)
                if s and s.lat and s.lon:
                    coords = (s.lat, s.lon)
                    break
    except Exception:  # noqa: BLE001
        coords = None
    _THEME_COORDS_MEMO.clear()
    _THEME_COORDS_MEMO[key] = coords
    return coords


# Luxembourg City - used for sunrise/sunset until a stop is configured.
_FALLBACK_COORDS = (49.6116, 6.1319)


def day_night_theme(config) -> str:
    """What "auto" means everywhere (board and admin pages): "light"
    between sunrise and sunset at the configured stop (A first, then B),
    "dark" otherwise."""
    coords = _stop_coordinates(config) or _FALLBACK_COORDS
    return "light" if is_daytime(*coords) else "dark"


def _resolve_board_theme(config) -> str:
    """Return "light" or "dark" - never "auto" - for the board."""
    if config.theme in ("light", "dark"):
        return config.theme
    return day_night_theme(config)


# ---- setup hotspot QR codes (segno, pure Python) ----

_QR_MEMO: dict[tuple, str] = {}


def _qr_data_uri(key: tuple, make) -> str:
    if key not in _QR_MEMO:
        if len(_QR_MEMO) > 8:
            _QR_MEMO.clear()
        # dark/light chosen for contrast in both board themes
        _QR_MEMO[key] = make().svg_data_uri(scale=6, border=2, dark="#000000", light="#ffffff")
    return _QR_MEMO[key]


def hotspot_qr_data_uri(config, password: Optional[str] = None) -> str:
    """Standard WiFi QR code (phones join by scanning) for the hotspot.
    ``password`` defaults to the configured setting; the board passes the
    password of the hotspot that is actually running."""
    ssid = config.wifi_ap.ssid
    password = config.wifi_ap.effective_password if password is None else password
    return _qr_data_uri(
        ("wifi", ssid, password),
        lambda: segno.helpers.make_wifi(ssid=ssid, password=password or None, security="WPA" if password else "nopass"),
    )


def setup_url_qr_data_uri(url: str) -> str:
    return _qr_data_uri(("url", url), lambda: segno.make(url, error="m"))


# Changes whenever the web process restarts (e.g. after a code update); the
# board page reloads itself when it sees a new value.
APP_VERSION = str(int(time.time()))

# Board shows at most this many rows per column (fits 800x600).
BOARD_MAX_ROWS = 8


def _departure_payload(dep: Departure, tz) -> dict:
    return {
        "line": dep.line,
        "direction": dep.direction,
        "category": dep.category,
        "platform": dep.platform,
        "scheduled": dep.scheduled_time,
        "time": dep.display_time,
        "delay": dep.delay_minutes,
        "delayed": dep.is_delayed,
        "cancelled": dep.cancelled,
        "realtime": dep.has_realtime,
        "accessible": dep.accessible,
        "notices": list(dep.notices),
        "epoch": dep.epoch(tz),
        # Used when live data is unavailable (timetable mode).
        "scheduled_epoch": _scheduled_epoch(dep, tz),
    }


def _scheduled_epoch(dep: Departure, tz) -> Optional[float]:
    try:
        naive = datetime.strptime(f"{dep.scheduled_date} {dep.scheduled_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=tz).timestamp()


def _stop_payload(snap, stop_cfg, now, tz, limit: int) -> dict:
    deps = [d for d in snap.departures if not d.has_departed(now, tz)][:limit]
    first = deps[0] if deps else None
    return {
        "configured": stop_cfg.configured,
        "name": snap.stop.name or "",
        "label": snap.stop.label or (first.direction if first else ""),
        "platform": stop_cfg.platform,
        "favourites": sorted(stop_cfg.favourites_set),
        "error": snap.error,
        "error_kind": snap.error_kind,
        "updated_at": snap.updated_at,
        "walk_minutes": stop_cfg.walk_minutes,
        "leave_lines": stop_cfg.leave_lines,
        "departures": [_departure_payload(d, tz) for d in deps],
    }


# Live times older than this are replaced by the timetable (with a notice).
LIVE_DATA_MAX_AGE_SECONDS = 120

# Plain-language reasons for the outage notice, by poller error kind.
_OUTAGE_REASONS = {
    "quota": "mobiliteit.lu request quota reached",
    "limit": "request budget used up",
    "auth": "API key rejected - check the API Key page",
    "transient": "mobiliteit.lu not responding",
    "bad_request": "stop settings rejected by mobiliteit.lu",
}


def _disruptions(stops: list[dict], limit: int = 5) -> list[dict]:
    """Unique (line, text) service messages from the departures shown."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for stop in stops:
        for dep in stop["departures"]:
            for text in dep["notices"]:
                key = (dep["line"], text)
                if key not in seen:
                    seen.add(key)
                    out.append({"line": dep["line"], "text": text})
    return out[:limit]


def build_board_payload(config, snapshot, report) -> dict:
    """Everything the kiosk page needs, as plain JSON. The page renders it
    and counts down by itself, so it only has to fetch this small document
    instead of reloading the whole page."""
    tz = get_timezone(config.timezone)
    now = datetime.now(tz)
    limit = max(1, min(config.api.max_journeys, BOARD_MAX_ROWS))
    status = report.status
    ap_password = _running_hotspot_password(config, report) if status.ap_active else ""
    warning = network.connectivity_warning_message(
        status,
        ap_ssid=report.ap_ssid or config.wifi_ap.ssid,
        ap_password=ap_password,
        ap_ip=report.ap_ip,
        saved_networks=report.saved_networks,
        ap_error=report.ap_error,
    )
    ip_address = None
    if config.display.show_ip_address:
        ip_address = report.ap_ip if status.ap_active else network.get_local_ip_address()
    stop_a = _stop_payload(snapshot.stop_a, config.stop_a, now, tz, limit)
    stop_b = _stop_payload(snapshot.stop_b, config.stop_b, now, tz, limit)
    reason_kind = snapshot.pause_reason or next(
        (s["error_kind"] for s in (stop_a, stop_b) if s["configured"] and s["error_kind"]), None
    )
    if not status.ok:
        outage_reason = "no internet connection"
    else:
        outage_reason = _OUTAGE_REASONS.get(reason_kind or "", "")
    return {
        "app_version": APP_VERSION,
        "generated_at": time.time(),
        "timezone": config.timezone,
        "theme": _resolve_board_theme(config),
        "rotate_180": config.display.rotate_180,
        "time_mode": config.display.time_display_mode,
        "show_accessibility": config.display.show_accessibility,
        "clock_format": config.display.clock_format,
        "leave_style": config.display.leave_now_style,
        "leave_margin": config.display.leave_now_margin_minutes,
        # Rows per column: each row gets exactly 1/rows of the column height,
        # so the rows fill the space (with or without the notice bar).
        "rows": limit,
        "display_on": config.schedule.is_active(now),
        "ip_address": ip_address,
        "warning": warning,
        "network": {
            "ok": status.ok,
            "offline_since": report.offline_since,
            "note": report.note,
        },
        "poll_interval": snapshot.poll_interval_seconds,
        # Live data older than this counts as an outage -> timetable mode.
        # At least 2 poll intervals, so a slow (budget-stretched) interval
        # alone never triggers it.
        "live_max_age": max(LIVE_DATA_MAX_AGE_SECONDS, 2 * snapshot.poll_interval_seconds + 15),
        "outage_reason": outage_reason,
        "stop_a": stop_a,
        "stop_b": stop_b,
        "disruptions": _disruptions([stop_a, stop_b]),
        "hotspot": _hotspot_payload(config, report) if status.ap_active else None,
        "admin_notice": _admin_notice(config, report),
    }


def _admin_notice(config, report) -> Optional[str]:
    """While the admin password is still the generated one, the board shows
    it (only someone in front of the screen can read it), so setting up
    never needs SSH. Gone as soon as it's changed on /admin/account."""
    pw = config.admin.initial_password
    if not pw:
        return None
    ip = (report.ap_ip or network.DEFAULT_AP_IP) if report.status.ap_active else network.get_local_ip_address()
    where = f"http://{ip}:8080/admin" if ip else "/admin"
    return f"Admin login: {config.admin.username} / {pw} at {where} - change the password there to hide this"


def _running_hotspot_password(config, report) -> str:
    """Password of the hotspot that's actually on: the WPA2 (hostapd) one
    needs the password, the open (NetworkManager) one none - whatever the
    setting says (e.g. during `busboard-hotspot-test --wpa2`)."""
    return config.wifi_ap.password if report.ap_secure else ""


def _hotspot_payload(config, report) -> dict:
    """Shown full-size on the board while the setup hotspot is on: scan
    one QR code to join the hotspot, the other to open the admin page."""
    url = f"http://{report.ap_ip or network.DEFAULT_AP_IP}:8080/admin"
    return {
        "ssid": config.wifi_ap.ssid,
        "password": _running_hotspot_password(config, report),
        "url": url,
        "wifi_qr": hotspot_qr_data_uri(config, _running_hotspot_password(config, report)),
        # Shown under the setup-page QR code while the generated admin
        # password is still in use (the bottom bar is busy with the outage).
        "admin_user": config.admin.username if config.admin.initial_password else "",
        "admin_password": config.admin.initial_password,
        "url_qr": setup_url_qr_data_uri(url),
    }


_LINE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .\-]{0,9}$")


def _normalize_lines(raw: str) -> str:
    """Comma-separated line list from the tag pickers: trimmed, valid
    tokens only, no duplicates, order kept."""
    out: list[str] = []
    for token in (t.strip() for t in raw.split(",")):
        if token and _LINE_TOKEN.match(token) and token not in out:
            out.append(token)
    return ",".join(out)


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = get_or_create_flask_secret_key()

    # First run: generate the admin and hotspot passwords. busboard-web is
    # the only process that does this (netwatch used to as well, racing it
    # and logging the password to whichever journal won). The admin password
    # is never logged: the installer prints it, and the board shows it until
    # it's changed on /admin/account.
    config = load_config()
    config, generated_password = ensure_admin_password(config)
    changed = bool(generated_password)
    if not config.wifi_ap.password:
        config.wifi_ap.password = generate_password(12)
        changed = True
    if changed:
        save_config(config)
    if generated_password:
        logger.warning("Generated the initial admin password - it is shown on the board until changed at /admin/account")

    # The departure poller and the connectivity view live for the whole
    # process, started up front so the board has data as soon as Chromium
    # opens (not only after the first page request).
    client_factory = CachedClientFactory()
    poller = DeparturePoller(config, client_factory=lambda: client_factory.get(load_config()))
    poller.start()
    monitor = network.ConnectivityMonitor()
    app.extensions["busboard_poller"] = poller
    app.extensions["busboard_monitor"] = monitor

    @app.before_request
    def _load_request_config():
        g.config = load_config()

    @app.context_processor
    def _inject_theme():
        # Makes `default_theme` available in every template without passing
        # it explicitly from each route. The client-side JS in base.html reads
        # this as the fallback when no localStorage override has been set.
        return {"default_theme": g.config.theme, "auto_theme": day_night_theme(g.config)}

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect(url_for("admin_login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    def _csrf_token() -> str:
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    app.jinja_env.globals["csrf_token"] = _csrf_token

    @app.context_processor
    def _inject_login_state():
        return {"logged_in": bool(session.get("logged_in"))}

    def _check_csrf() -> bool:
        submitted = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        return bool(expected) and secrets.compare_digest(submitted, expected)

    @app.before_request
    def _enforce_csrf():
        # Every POST, including the login form (prevents login CSRF).
        if request.method == "POST" and not _check_csrf():
            flash("Your session expired, please try again.", "error")
            return redirect(request.path)

    def _safe_next(target: Optional[str]) -> str:
        """Only redirect back into the admin area (no open redirects)."""
        if target and target.startswith("/admin") and not target.startswith("//"):
            return target
        return url_for("status")

    # ---- auth ----

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if session.get("logged_in"):
            return redirect(_safe_next(request.args.get("next")))
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if username == g.config.admin.username and g.config.admin.verify(password):
                session["logged_in"] = True
                session.permanent = True
                if g.config.admin.initial_password:
                    flash("You're using the generated password. Choose your own below - "
                          "the board stops showing it once you do.", "error")
                    return redirect(url_for("admin_account"))
                return redirect(_safe_next(request.args.get("next")))
            flash("Invalid username or password.", "error")
        return render_template("login.html")

    @app.route("/admin/logout", methods=["POST"])
    @login_required
    def admin_logout():
        session.clear()
        return redirect(url_for("status"))

    # ---- departure board display (for kiosk/HDMI browser) ----
    def _board_payload() -> dict:
        poller.update_config(g.config)
        return build_board_payload(g.config, poller.snapshot(), monitor.current())

    @app.route("/")
    def board():
        """Full-screen departure board - the root URL, no login required.

        Loaded once by the Chromium kiosk; after that the page fetches
        ``/api/board`` in the background and updates itself, so a slow or
        restarting server never leaves Chromium on an error page.
        """
        return render_template("board.html", initial=_board_payload())

    @app.route("/api/screen")
    def api_screen():
        """Tiny endpoint for the screen power helper (busboard-screen), which
        runs as the kiosk user and can't read config.yaml itself."""
        tz = get_timezone(g.config.timezone)
        on = g.config.schedule.is_active(datetime.now(tz))
        resp = app.response_class(app.json.dumps({"display_on": on}), mimetype="application/json")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/api/board")
    def api_board():
        resp = app.response_class(
            app.json.dumps(_board_payload()),
            mimetype="application/json",
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---- public status + admin overview ----

    def _status_context() -> dict:
        report = monitor.current()
        limiter = get_shared_limiter(
            hourly_limit=g.config.api.hourly_limit,
            daily_limit=g.config.api.daily_limit,
        )
        snap = poller.snapshot()
        now = time.time()
        live_max_age = max(LIVE_DATA_MAX_AGE_SECONDS, 2 * snap.poll_interval_seconds + 15)

        def stop_health(stop_snap, stop_cfg) -> dict:
            age = (now - stop_snap.updated_at) if stop_snap.updated_at else None
            return {
                "cfg": stop_cfg,
                "live": age is not None and age < live_max_age,
                "age": age,
                "error": stop_snap.error if stop_cfg.configured else None,
            }

        return {
            "config": g.config,
            "report": report,
            "connectivity": report.status,
            "usage": limiter.usage(),
            "api_key_set": api_key_configured(),
            "poll_interval": snap.poll_interval_seconds,
            "pause_reason": snap.pause_reason,
            "stop_a": stop_health(snap.stop_a, g.config.stop_a),
            "stop_b": stop_health(snap.stop_b, g.config.stop_b),
            "display_on": g.config.schedule.is_active(datetime.now(get_timezone(g.config.timezone))),
            "watchdog_running": report.source == "netwatch",
            "initial_password_in_use": bool(g.config.admin.initial_password),
        }

    @app.route("/status")
    def status():
        """Read-only health page, no login: network, hotspot (no password),
        live data freshness, API usage, configured stops. No departures."""
        return render_template("status.html", **_status_context())

    @app.route("/admin")
    @login_required
    def admin_home():
        """No separate overview page: /status shows everything, and the
        admin pages are in the menu once logged in."""
        return redirect(url_for("status"))

    # ---- stop setup with API-backed, cached autocomplete ----

    @app.route("/admin/stops", methods=["GET", "POST"])
    @login_required
    def admin_stops():
        config = g.config
        if request.method == "POST":
            try:
                config.stop_a.id = request.form.get("stop_a_id", "").strip()
                config.stop_a.name = request.form.get("stop_a_name", "").strip()
                config.stop_a.label = request.form.get("stop_a_label", "").strip()
                config.stop_a.lines = request.form.get("stop_a_lines", "").strip()
                config.stop_a.platform = request.form.get("stop_a_platform", "").strip()
                config.stop_a.favourites = request.form.get("stop_a_favourites", "").strip()
                config.stop_b.id = request.form.get("stop_b_id", "").strip()
                config.stop_b.name = request.form.get("stop_b_name", "").strip()
                config.stop_b.label = request.form.get("stop_b_label", "").strip()
                config.stop_b.lines = request.form.get("stop_b_lines", "").strip()
                config.stop_b.platform = request.form.get("stop_b_platform", "").strip()
                config.stop_b.favourites = request.form.get("stop_b_favourites", "").strip()
                for stop, prefix in ((config.stop_a, "stop_a"), (config.stop_b, "stop_b")):
                    stop.lines = _normalize_lines(stop.lines)
                    stop.favourites = _normalize_lines(stop.favourites)
                    mode = request.form.get(f"{prefix}_leave_lines", stop.leave_lines)
                    stop.leave_lines = mode if mode in LEAVE_LINE_MODES else "all"
                for stop, field_name in ((config.stop_a, "stop_a_walk"), (config.stop_b, "stop_b_walk")):
                    raw_walk = request.form.get(field_name, "").strip()
                    if raw_walk:
                        try:
                            stop.walk_minutes = int(raw_walk)
                        except ValueError as exc:
                            raise ConfigError("walking time must be a whole number of minutes") from exc
                save_config(config)
                flash("Stop configuration saved.", "success")
                return redirect(url_for("admin_stops"))
            except ConfigError as exc:
                flash(str(exc), "error")
        return render_template("admin_stops.html", config=config)

    @app.route("/admin/api/stops/search")
    @login_required
    def admin_api_stops_search():
        """Autocomplete endpoint — returns de-duplicated stop names. When
        multiple stop IDs share the same name (different platforms at the
        same location), they are grouped into a single result entry with all
        their IDs. The client uses the ID list to fetch quai info in Stage 2.
        Costs at most one API call total (the stops cache), never per
        keystroke."""
        query = request.args.get("q", "").strip()
        if len(query) < 2:
            return {"results": []}
        try:
            cache = get_or_refresh_cache(lambda: build_client(g.config))
        except ApiKeyNotConfigured as exc:
            return {"results": [], "error": str(exc)}, 400
        except StopsCacheError as exc:
            return {"results": [], "error": str(exc)}, 502

        matches = search_stops(cache, query, limit=40)

        # Group by name — same name = same physical location, different platforms.
        # Preserve ranking order (first occurrence wins for position).
        seen_names: dict[str, dict] = {}
        for s in matches:
            if s.name not in seen_names:
                seen_names[s.name] = {
                    "name": s.name,
                    "ids": [s.id],
                    "has_multiple": False,
                }
            else:
                seen_names[s.name]["ids"].append(s.id)
                seen_names[s.name]["has_multiple"] = True

        return {"results": list(seen_names.values())}

    @app.route("/admin/api/stops/platforms")
    @login_required
    def admin_api_stops_platforms():
        """Return distinct platform/quai numbers for a stop, aggregated
        across all stop IDs that share the same name (different physical
        platforms at the same location).

        Accepts: ?ids=ID1,ID2,ID3 (comma-separated, all IDs for one stop name)

        Results are cached server-side for 6 hours so repeated clicks on the
        same stop never cost more than one API call per ID per 6 hours.
        """
        ids_param = request.args.get("ids", "").strip()
        if not ids_param:
            return {"platforms": [], "error": "ids required"}, 400

        stop_ids = [i.strip() for i in ids_param.split(",") if i.strip()]
        if not stop_ids:
            return {"platforms": [], "error": "ids required"}, 400

        try:
            client = build_client(g.config)
        except ApiKeyNotConfigured as exc:
            return {"platforms": [], "error": str(exc)}, 400

        # quai -> {"quai", "stop_id", "example"}; every quai remembers which
        # stop ID it belongs to, so the right ID is saved with it.
        found: dict[str, dict] = {}
        failures = 0
        for stop_id in stop_ids:
            platforms = _get_cached_platforms(stop_id)
            if platforms is None:
                try:
                    departures = client.departure_board(stop_id=stop_id, duration_minutes=120, max_journeys=30)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("platform fetch failed for %s: %s", stop_id, exc)
                    failures += 1
                    continue
                per_id: dict[str, str] = {}
                for dep in departures:
                    if dep.platform and dep.platform not in per_id:
                        per_id[dep.platform] = dep.direction
                platforms = [{"quai": q, "example": d} for q, d in per_id.items()]
                _set_cached_platforms(stop_id, platforms)
            for p in platforms:
                found.setdefault(p["quai"], {"quai": p["quai"], "stop_id": stop_id, "example": p["example"]})

        if failures and failures == len(stop_ids):
            return {"platforms": [], "error": "Couldn't load quais from mobiliteit.lu - try again in a moment."}, 502
        platforms = sorted(found.values(), key=lambda p: (len(p["quai"]), p["quai"]))
        return {"platforms": platforms}

    _LINES_CACHE: dict[tuple, tuple[float, list]] = {}

    @app.route("/admin/api/stops/lines")
    @login_required
    def admin_api_stops_lines():
        """Distinct lines (with an example destination) serving a stop/quai,
        for the line pickers on the Stops page. Cached 6 hours per stop+quai,
        so it costs at most one API call per stop per 6 hours."""
        stop_id = request.args.get("id", "").strip()
        platform = request.args.get("platform", "").strip()
        if not stop_id:
            return {"lines": []}
        key = (stop_id, platform)
        hit = _LINES_CACHE.get(key)
        if hit and time.time() - hit[0] < _PLATFORM_CACHE_TTL:
            return {"lines": hit[1]}
        try:
            client = build_client(g.config)
            departures = client.departure_board(stop_id=stop_id, platform=platform, duration_minutes=240, max_journeys=80)
        except ApiKeyNotConfigured as exc:
            return {"lines": [], "error": str(exc)}, 400
        except Exception as exc:  # noqa: BLE001
            logger.warning("line lookup failed for %s: %s", stop_id, exc)
            return {"lines": [], "error": "Couldn't load lines from mobiliteit.lu"}, 502
        seen: dict[str, str] = {}
        for dep in departures:
            if dep.line and dep.line not in seen:
                seen[dep.line] = dep.direction

        def order(line: str):
            digits = "".join(ch for ch in line if ch.isdigit())
            return (0, int(digits), line) if digits and line[0].isdigit() else (1, 0, line)

        lines = [{"line": ln, "direction": seen[ln]} for ln in sorted(seen, key=order)]
        _LINES_CACHE[key] = (time.time(), lines)
        return {"lines": lines}

    # ---- API key ----

    @app.route("/admin/api-key", methods=["GET", "POST"])
    @login_required
    def admin_api_key():
        if request.method == "POST":
            key = request.form.get("api_key", "").strip()
            try:
                set_api_key(key)
                flash("API key saved.", "success")
            except ConfigError as exc:
                flash(str(exc), "error")
            return redirect(url_for("admin_api_key"))
        return render_template("admin_api_key.html", api_key_set=api_key_configured())

    # ---- schedule ----

    @app.route("/admin/schedule", methods=["GET", "POST"])
    @login_required
    def admin_schedule():
        config = g.config
        if request.method == "POST":
            try:
                for i, zone in enumerate(config.schedule.zones):
                    zone.enabled = f"zone_{i}_enabled" in request.form
                    zone.on_time = request.form.get(f"zone_{i}_on", zone.on_time)
                    zone.off_time = request.form.get(f"zone_{i}_off", zone.off_time)
                    zone.days = sorted({int(d) for d in request.form.getlist(f"zone_{i}_days") if d in "0123456" and len(d) == 1})
                    if not zone.enabled and not zone.days:
                        zone.days = list(ALL_DAYS)  # so switching it on later starts from "every day"
                save_config(config)
                flash("Display schedule saved.", "success")
                return redirect(url_for("admin_schedule"))
            except ConfigError as exc:
                flash(str(exc), "error")
        return render_template("admin_schedule.html", config=config, day_names=DAY_NAMES)

    # ---- polling / API options ----

    @app.route("/admin/options", methods=["GET", "POST"])
    @login_required
    def admin_options():
        config = g.config
        if request.method == "POST":
            try:
                config.api.poll_interval_seconds = int(request.form.get("poll_interval_seconds", config.api.poll_interval_seconds))
                config.api.duration_minutes = int(request.form.get("duration_minutes", config.api.duration_minutes))
                config.api.max_journeys = int(request.form.get("max_journeys", config.api.max_journeys))
                config.api.hourly_limit = int(request.form.get("hourly_limit", config.api.hourly_limit))
                config.api.daily_limit = int(request.form.get("daily_limit", config.api.daily_limit))
                raw_theme = request.form.get("theme", config.theme).strip().lower()
                config.theme = raw_theme if raw_theme in ("light", "dark", "auto") else config.theme
                d = config.display
                raw_mode = request.form.get("time_display_mode", d.time_display_mode).strip().lower()
                if raw_mode in TIME_DISPLAY_MODES:
                    d.time_display_mode = raw_mode
                raw_clock = request.form.get("clock_format", d.clock_format).strip().lower()
                if raw_clock in CLOCK_FORMATS:
                    d.clock_format = raw_clock
                raw_leave = request.form.get("leave_now_style", d.leave_now_style).strip().lower()
                if raw_leave in LEAVE_NOW_STYLES:
                    d.leave_now_style = raw_leave
                d.leave_now_margin_minutes = int(request.form.get("leave_now_margin_minutes", d.leave_now_margin_minutes))
                d.rotate_180 = "rotate_180" in request.form
                d.show_ip_address = "show_ip_address" in request.form
                d.show_accessibility = "show_accessibility" in request.form
                save_config(config)
                flash("Options saved.", "success")
                return redirect(url_for("admin_options"))
            except (ConfigError, ValueError) as exc:
                flash(str(exc), "error")
        return render_template("admin_options.html", config=config, board_auto_theme=day_night_theme(config))

    # ---- wifi ----

    def _activate_in_background(name: str) -> None:
        def _run() -> None:
            # Let the browser receive the response first: if you're on the
            # setup hotspot, switching the radio drops that connection.
            time.sleep(3)
            try:
                network.activate_wifi_profile(name)
            except network.NmcliError as exc:
                logger.warning("could not activate wifi %r now (NetworkManager keeps retrying): %s", name, exc)

        threading.Thread(target=_run, daemon=True).start()

    @app.route("/admin/wifi", methods=["GET", "POST"])
    @login_required
    def admin_wifi():
        if request.method == "POST":
            ssid = request.form.get("ssid", "").strip()
            password = request.form.get("password", "")
            country = request.form.get("wifi_country", g.config.wifi_country).strip().upper()
            config = g.config
            if country != config.wifi_country:
                config.wifi_country = country
                try:
                    save_config(config)
                except ConfigError as exc:
                    flash(str(exc), "error")
                    return redirect(url_for("admin_wifi"))
            try:
                network.add_or_update_wifi_profile(ssid, password)
            except (network.NmcliError, ValueError) as exc:
                flash(f"Could not save network: {exc}", "error")
                return redirect(url_for("admin_wifi"))
            _activate_in_background(ssid)
            flash(
                f'Saved "{ssid}" (country {country}). Connecting now - if you are on the setup '
                "hotspot it will disappear; rejoin your normal WiFi and use the IP shown on the board.",
                "success",
            )
            return redirect(url_for("admin_wifi"))

        report = monitor.current()
        current_profile = report.status.wifi_profile if report.status.wifi_connected else ""
        saved = [n for n in network.list_saved_wifi_networks() if n.name != current_profile]
        return render_template(
            "admin_wifi.html",
            report=report,
            saved=saved,
            ap_ssid=g.config.wifi_ap.ssid,
            ap_password=g.config.wifi_ap.effective_password,
            ap_wpa2_password=g.config.wifi_ap.password,
            ap_security=g.config.wifi_ap.security,
            hotspot_qr=hotspot_qr_data_uri(g.config),
            wifi_country=g.config.wifi_country,
            wifi_countries=WIFI_COUNTRIES,
        )

    @app.route("/admin/wifi/hotspot", methods=["POST"])
    @login_required
    def admin_wifi_hotspot():
        config = g.config
        security = request.form.get("security", "")
        if security in HOTSPOT_SECURITY:
            config.wifi_ap.security = security
            try:
                save_config(config)
                flash("Setup hotspot: " + ("WPA2 with password." if security == "wpa2" else "open network (no password)."), "success")
            except ConfigError as exc:
                flash(str(exc), "error")
        return redirect(url_for("admin_wifi"))

    def _saved_profile(name: str):
        return next((n for n in network.list_saved_wifi_networks() if n.name == name), None)

    @app.route("/admin/wifi/connect", methods=["POST"])
    @login_required
    def admin_wifi_connect():
        net = _saved_profile(request.form.get("profile", ""))
        if not net:
            flash("That network is no longer saved.", "error")
        else:
            _activate_in_background(net.name)
            flash(f'Switching to "{net.ssid}"...', "success")
        return redirect(url_for("admin_wifi"))

    @app.route("/admin/wifi/forget", methods=["POST"])
    @login_required
    def admin_wifi_forget():
        net = _saved_profile(request.form.get("profile", ""))
        report = monitor.current()
        if not net:
            flash("That network is no longer saved.", "error")
        elif report.status.wifi_connected and net.name == report.status.wifi_profile:
            flash("You can't forget the network the board is connected to.", "error")
        else:
            try:
                network.forget_wifi_profile(net.name)
                flash(f'Forgot "{net.ssid}".', "success")
            except (network.NmcliError, ValueError) as exc:
                flash(f"Could not forget network: {exc}", "error")
        return redirect(url_for("admin_wifi"))

    @app.route("/admin/api/wifi/scan")
    @login_required
    def admin_api_wifi_scan():
        """Nearby networks for the SSID suggestions. Empty while the setup
        hotspot is on (the radio can't scan properly then)."""
        if monitor.current().status.ap_active:
            return {"networks": [], "hotspot": True}
        return {
            "networks": [{"ssid": n.ssid, "signal": n.signal, "secure": n.secure} for n in network.scan_networks()],
            "hotspot": False,
        }

    # ---- admin password ----

    @app.route("/admin/account", methods=["GET", "POST"])
    @login_required
    def admin_account():
        config = g.config
        if request.method == "POST":
            current = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            confirm = request.form.get("confirm_password", "")
            if not config.admin.verify(current):
                flash("Current password is incorrect.", "error")
            elif new_password != confirm:
                flash("New passwords do not match.", "error")
            else:
                try:
                    updated = set_admin_password(config, new_password)
                    save_config(updated)
                    flash("Password updated.", "success")
                except ConfigError as exc:
                    flash(str(exc), "error")
            return redirect(url_for("admin_account"))
        return render_template("admin_account.html", config=config)

    return app
