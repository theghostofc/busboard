# BusBoard - Agent/Developer Reference

This file is a quick-reference for any AI agent or developer picking up
work on this project later. For user-facing setup instructions, see
`docs/SETUP.md`. For a deeper architectural explanation, see
`docs/ARCHITECTURE.md`. This file is the "how do I make a change safely"
doc.

## What this project is

A real-time bus/tram/train departure board for a Raspberry Pi 3B+ (1 GB)
with an 800x600 HDMI display. Shows next departures in two directions from
configured stops, using Luxembourg's `mobiliteit.lu` (ATP) open data API.
Raspberry Pi OS Trixie Lite, no desktop: the Pi boots into `cage` +
Chromium kiosk showing `http://localhost:8080/`.

## Repo layout

```
busboard/
  core/           # shared logic, no Pi-specific dependencies
    config.py         # Config dataclasses, atomic YAML read/write, secrets
    ratelimiter.py     # hourly/daily API budget; one shared instance per process
    api_client.py       # MobiliteitClient + Departure model (tz-aware times)
    countries.py        # WiFi country list (ISO 3166-1, generated once)
    client_factory.py    # builds MobiliteitClient from config + api_key file
    stops_cache.py         # nationwide stop list cache for autocomplete
    network.py                # nmcli wifi/ethernet, healing actions, status file
    poller.py                    # background thread polling both stops, adaptive interval
  web/            # Flask app: kiosk board, public /status, /admin area
    app.py          # create_app(): board, /api/board, /status, /admin/*, auth, CSRF
    main.py           # entrypoint: serves app.py via waitress
    reset_password.py  # CLI behind `sudo busboard-reset-password`
    templates/          # board.html (self-updating kiosk page) + login, status and admin pages
  netwatch/       # connectivity watchdog daemon
    main.py         # Watchdog: classify -> heal ladder -> hotspot -> reboot
    hotspot_test.py  # CLI behind `sudo busboard-hotspot-test`
  screen/         # HDMI output on/off with the schedule (runs as kiosk user)
    main.py         # polls /api/screen, calls wlr-randr; stdlib only
systemd/          # .service files + polkit rule
pyproject.toml    # direct dependencies (pinned); uv.lock pins everything with hashes
.python-version   # 3.13 - same as Raspberry Pi OS Trixie
scripts/          # install.sh / uninstall.sh and the root helpers installed to
                  # /usr/local/sbin: busboard-wifi-import.sh, busboard-wifi-country.sh,
                  # busboard-hotspot.sh, busboard-hotspot-test.sh,
                  # busboard-wifi-fix.sh, busboard-reset-password.sh (Pi only)
docs/             # SETUP.md (user guide), ARCHITECTURE.md (design rationale)
```

Services: `busboard-web` (board + status + admin + poller), `busboard-netwatch`
(watchdog), `busboard-display` (cage + Chromium, runs as `planner`),
`busboard-screen` (HDMI power, `planner`), `busboard-wifi-import` and
`busboard-wifi-country` (root one-shots; the latter also via a `.path` unit
on config.yaml), `busboard-hotspot` (root, WPA2 hotspot, started/stopped by
netwatch on demand) and `busboard-reset-password` (root one-shot at boot,
only when the boot-partition flag file exists). They share state only
through files, never by talking to each other directly.

There is exactly **one rendering path**: `templates/board.html`, fed by
`build_board_payload()` in `app.py`.

## Where things are stored at runtime

| Path | Contents | Perms |
|---|---|---|
| `/etc/busboard/config.yaml` | stops, schedule, options, hotspot/watchdog settings, admin password hash | 0600 |
| `/etc/busboard/api_key` | mobiliteit.lu API key (plaintext, separate file on purpose) | 0600 |
| `/etc/busboard/flask_secret_key` | Flask session signing key | 0600 |
| `/run/busboard/rate_limit_state.json` | live request counters (tmpfs) | - |
| `/var/lib/busboard/rate_limit_state.json` | request counters, written only at clean exit | - |
| `/var/lib/busboard/stops_cache.json` | cached nationwide stop list for autocomplete | - |
| `/var/lib/busboard/netwatch_state.json` | consecutive-reboot counter (reboot backoff) | 0600 |
| `/run/busboard/network_status.json` | netwatch's latest network observation (tmpfs) | 0644 |

Overridable via `BUSBOARD_CONFIG_DIR` / `BUSBOARD_STATE_DIR` /
`BUSBOARD_RUNTIME_DIR` - use these for local dev/testing instead of the real
`/etc`, `/var`, `/run` paths.

**The API key is set through the web UI** (`/admin/api-key` ->
`set_api_key()`), not by editing files directly.

URL layout:
`/` board, `/api/board`, `/api/screen` (public, for the kiosk);
`/status` (public, read-only: health + configured stop names, never
departures or secrets); `/admin` + `/admin/*` pages and `/admin/api/*` JSON
(login required via `login_required`). Admin endpoint names are
`admin_<page>`. `/admin` itself redirects to `/status` (there is no separate
overview page); the menu marks the current page via `request.endpoint` in
`base.html`'s `navlink` macro - add new pages there.

Themes are light / dark / auto everywhere. "auto" = light between
sunrise and sunset at the configured stop, else dark (`day_night_theme` in
`app.py`, Luxembourg City as fallback); the board uses it directly, the admin
pages get it as `auto_theme`. There is no OS-following "system" mode - the
kiosk has no OS preference to follow.

The WiFi country list is `busboard/core/countries.py` (all ISO 3166-1
countries, sorted by name, generated once from pycountry - not a runtime
dependency).

## Key design decisions (read before changing behavior)

- **Two separate stop IDs, not one stop + direction flag.** In Luxembourg,
  the two directions of a route are usually distinct physical
  stops/platforms. `Config` has `stop_a` and `stop_b`.
- **No bundled stop list in the repo.** The API has no text search
  (`location.name` 404s). Autocomplete fetches the entire nationwide stop
  list via `location.nearbystops` and caches it for a week
  (`stops_cache.py`). Never add a static `stops.json`.
- **Rate limiting is mandatory around every API call.** `MobiliteitClient`
  always calls `RateLimiter.try_acquire()` first. Get limiters through
  `get_shared_limiter()` (what `build_client` does) - separate instances in
  one process under-count and overwrite each other's state file. Defaults
  (400/h, 4000/day) are below the documented quota on purpose. Counters live
  on tmpfs and are written to the SD card only at exit (`flush()` via
  atexit), which is why `web/main.py` turns SIGTERM into `SystemExit`.
- **SD card writes are kept to a minimum.** Anything that changes often
  goes to `/run/busboard` (`config.runtime_dir()`) or stays in memory. Never
  persist departures - stale times are worthless; the board falls back to
  the timetable with an alert instead. The journal is persistent on purpose.
- **API errors are classified** (`classify_error` -> `ERR_*`): quota pauses
  polling until Retry-After / next hour, auth retries every 10 min,
  `SVC_NO_RESULT` means "no departures" (not an error), transient errors
  back off exponentially. Map new error codes there, not in the poller.
- **Polling follows the schedule**: fetch 30 s before an on-time
  (`WARM_UP_SECONDS`), skip polls < 60 s before an off-time, nothing while
  off (`DeparturePoller._schedule_wait`).
- **Poll interval is adaptive** (`compute_poll_interval` in `poller.py`):
  never below the configured value, relaxed when nothing departs within 20
  min, and stretched so the remaining daily budget lasts until reset
  (counting only schedule-on time). Don't replace it with a fixed interval -
  a fixed 30 s ran out of budget every evening.
- **Times are Luxembourg local time, always.** API times are local wall
  clock. Use `get_timezone(config.timezone)` and the `Departure` helpers
  (`departure_datetime`, `epoch`, `minutes_until`, `has_departed`,
  `sort_key`); never compare bare `"HH:MM"` strings or rely on the OS
  timezone. `realtime_date` exists because a delay can cross midnight.
- **Minutes-until is computed on demand**, not stored: server side via
  `Departure.minutes_until()`, and in the browser from `epoch` every second.
- **Schedule windows** (`ScheduleZone`, 5 of them) have `days` (Mon=0).
  `ScheduleConfig.is_active()` takes a local **datetime**, not "HH:MM";
  overnight windows belong to the day they start.
- **"Leave now"** lives in `board.html` (`leaveNowSet`): one badge per line
  + destination, window `[walk, walk + margin]` seconds before the
  real-time departure, only when live. Config: `StopConfig.walk_minutes`,
  `StopConfig.leave_lines`, `DisplayConfig.leave_now_style` /
  `leave_now_margin_minutes`. Keep the Options preview in sync.
- **Clock format** (`DisplayConfig.clock_format`) is applied in the board
  JS (`clockParts`); the server always sends "HH:MM".
- **Live data older than `live_max_age` (2 min, or 2 poll intervals) is
  not shown as live.** The column switches to timetable mode (planned times,
  no delays) and the notice bar shows an alert. Keep that rule in the JS
  (`isLive`) and the payload (`live_max_age`) in sync.
- **One notice bar, theme-aware**: alerts (our outages) beat operator
  notices (HAFAS notes type R/I, HIM `Messages`). Add new states there
  rather than new banners.
- **The board page never full-reloads on a timer.** It fetches `/api/board`
  and patches the DOM only when something visible changes (Chromium renders
  in software on the Pi). If the fetch fails it keeps the last data. Don't
  add `<meta http-equiv="refresh">` back: a reload that hits a restarting
  server leaves Chromium on its error page for good. New display options go
  into `build_board_payload()` + the JS in `board.html`. The page must fill
  the viewport (`100vw`/`100vh`) with no scrolling; ellipsise, don't widen.
  Build DOM with `textContent`, never `innerHTML` with API data.
- **Screen power** (`busboard/screen`): runs as the kiosk user, so it asks
  `/api/screen` instead of reading config.yaml. It must fail safe (screen
  on) and keep the black overlay as the fallback.
- **WiFi country** is `Config.wifi_country` (WiFi page); root applies it via
  `busboard-wifi-country`. The app never needs root for it.
- **Board routes must never block.** Connectivity comes from
  `ConnectivityMonitor.current()` (reads netwatch's status file); don't call
  `get_connectivity_status()`, `nmcli`, or HTTP probes from a request.
  Expensive derived data (e.g. stop coordinates for the auto theme) is
  memoized.
- **Network resilience rules** (`netwatch/main.py`, docs/ARCHITECTURE.md):
  - The single radio can't run the hotspot and a client link at once. The
    hotspot connection never counts as "WiFi connected"
    (`status_from_devices`).
  - Never start the hotspot while a link exists (WiFi with a reachable
    gateway, or Ethernet): internet outages are waited out, not "fixed".
  - While the hotspot is up, periodically drop it and retry saved networks;
    take it down at netwatch startup; its profile stays `autoconnect=no`.
  - Reboot only with backoff (`reboot_delay_seconds`), never while a client
    is on the hotspot.
  - `Watchdog` takes a `net` object - test decision changes with a fake,
    never against real `nmcli`.
- **Never `nmcli connection modify` a netplan-backed profile** (Imager's
  `netplan-wlan0-*`, stored under /run from /etc/netplan). On the board this
  is the likely way the only WiFi profile disappeared after a reboot.
  `harden_wifi_profile` only touches keyfiles (`is_keyfile_profile`);
  `install.sh` copies netplan WiFi profiles into keyfiles and removes the
  originals.
- **Hotspot on the Pi 3's brcmfmac chip** needs band bg, a fixed channel,
  WPA2 (proto rsn, ccmp) and PMF disabled (`_AP_RADIO_SETTINGS` /
  `_AP_WPA2_SETTINGS`); with NetworkManager defaults it times out.
  `ensure_ap_profile` writes only when something differs. After a failed
  start netwatch waits `AP_FAILURE_BACKOFF_SECONDS` (radio free for the
  saved networks); after `AP_FAILURES_BEFORE_REBOOT` failures it may reboot
  even with no saved network. Test on the Pi with `sudo busboard-hotspot-test` (runs the module from
  /opt/busboard as busboard; the venv is `package = false`, so `python -m
  busboard...` only works with /opt/busboard as the working directory).
- **Hotspot security** (`WifiApConfig.security`, default "open"; use
  `effective_password` everywhere): open = NetworkManager `busboard-ap`
  profile; wpa2 = `busboard-hotspot.service` (root, `scripts/busboard-hotspot.sh`:
  wlan0 unmanaged, hostapd + dnsmasq-base on 10.42.0.1), started/stopped by
  netwatch via polkit. netwatch only writes SSID + password to
  `/run/busboard/hotspot.env`; the root script validates them and builds the
  hostapd config itself - never let the busboard user pass config through.
- **Admin password**: generated only by busboard-web (`ensure_admin_password`
  / `reset_admin_password`), kept as `AdminConfig.initial_password` until
  changed, shown on the board (`admin_notice`) and by the installer
  (`python -m busboard.web.reset_password --show`). Never log passwords.
  Reset: `sudo busboard-reset-password` or the boot-partition flag file.
- **Profile names are not SSIDs.** Raspberry Pi Imager names its profile
  `preconfigured`; use `list_saved_wifi_networks()` when you need SSIDs.
- **`busboard-web` and `busboard-netwatch` do not run as root.** Privileged
  operations go through `systemd/polkit-busboard-nm.rules`, scoped to the
  exact NetworkManager actions, `restart` of `NetworkManager.service`, and
  reboot. Adding a new privileged call means adding exactly that action.
  Anything that must run as root (the boot-partition importer) is installed
  root-owned to `/usr/local/sbin`, never run from `/opt/busboard`.
- **CSRF is hand-rolled**, not Flask-WTF. It's enforced for every POST,
  including login. Every form needs the `csrf_token` hidden field pattern
  from the existing templates. Post-login redirects go through `_safe_next`
  (only `/admin...`).
- **Show SSIDs, never NetworkManager profile names** (Imager/netplan names
  them `netplan-wlan0-<SSID>` or `preconfigured`). Use
  `ConnectivityStatus.wifi_ssid` / `SavedWifi.ssid` for display and the
  profile name (`wifi_profile` / `SavedWifi.name`) only for nmcli calls.
  The WiFi page never lists (or lets you forget) the connected network.
- **Hotspot secrets**: the password appears only in `/admin/wifi` (masked
  until "Show password") and on the board while the hotspot is on, with QR
  codes generated server-side by `segno` (`hotspot_qr_data_uri`).
- **Stop picker**: `/admin/api/stops/platforms` returns each quai with the
  stop ID it was found under - save that ID, never pair quais with IDs by
  position.
- **`rtMode` must be `SERVER_DEFAULT`** - this HAFAS server rejects `FULL`
  with `API_PARAM`, even though some third-party docs list it.
- **Config writes are atomic** (temp file + `os.replace`, see
  `_atomic_write`). Frequently-changing state goes to `/run/busboard`
  (tmpfs), not the SD card.
- **Never let a raw `requests`/`urllib3` exception reach a log line, the
  board, or any user-facing string.** Connection errors embed the full
  request URL including `accessId` (the API key). Always go through
  `_describe_network_error()` in `api_client.py`. Likewise, never put WiFi
  passwords in log lines or exception messages (`network._run` doesn't echo
  its arguments on timeout for this reason).
- **Dependencies go through uv.** Add/upgrade with `uv add` / edit
  `pyproject.toml` + `uv lock`, and commit `uv.lock`. The installer runs
  `uv sync --locked --no-dev` with Debian's `/usr/bin/python3` and
  `UV_PYTHON_DOWNLOADS=never`: it refuses to install if the lock file is
  out of date, and never re-resolves or downloads a Python on the Pi. uv
  itself is a pinned release (`UV_VERSION` in install.sh), checksum-verified.
- **Avoid rebuilding expensive objects in hot paths.** `CachedClientFactory`
  keeps one HTTP session across polls; the poller and monitor are created
  once in `create_app()`.

## Local development / testing

```bash
uv sync                      # creates .venv with Python 3.13 and the locked dependencies
BUSBOARD_CONFIG_DIR=/tmp/busboard/etc BUSBOARD_STATE_DIR=/tmp/busboard/var \
  BUSBOARD_RUNTIME_DIR=/tmp/busboard/run \
  uv run python -m busboard.web.main
uv run --with ruff ruff check .            # lint + import sorting (rules in pyproject.toml)
uv run python -m compileall -q busboard    # quick syntax check
```

Without `nmcli` the board shows "No network connection" (the monitor falls
back to probing by itself) - expected on a dev machine. To see the board with
data, monkeypatch `MobiliteitClient.departure_board` to return fake
`Departure`s. A headless Chrome screenshot
(`--headless=new --window-size=800,600 --screenshot`) is a quick visual check
of `board.html`.

Always use `uv run` / the uv-managed `.venv` (Python 3.13). Don't test with
macOS's system Python 3.9: it lacks `hashlib.scrypt`, which werkzeug's
default password hashing needs.

## Testing conventions

There's no committed test suite yet. If you add one:

- Use `pytest`, added as a dev dependency (`uv add --dev pytest`); run with
  `uv run pytest`. The Pi installs with `--no-dev`, so it never gets it.
- Mock `subprocess`/`nmcli` in `network.py` tests; test `Watchdog` with a
  fake `net`. Never touch real network state.
- Point `BUSBOARD_CONFIG_DIR`/`BUSBOARD_STATE_DIR`/`BUSBOARD_RUNTIME_DIR` at a
  pytest `tmp_path`.

## Making changes safely

1. Read `docs/ARCHITECTURE.md` first if the change touches more than one
   process or shared state.
2. Check **Key design decisions** above before changing stops, rate
   limiting, polling, time handling, the board page or netwatch.
3. After any change, verify locally with `uv run` and
   `uv run --with ruff ruff check .` - there's no CI.
4. If you change which `nmcli`/`systemctl` operations are called, update
   `systemd/polkit-busboard-nm.rules` to match.
5. `scripts/*.sh` are for the Pi only; never run them on a dev machine.
6. Never commit `config.yaml`, `api_key`, `flask_secret_key`, captured API
   responses, or any real credentials (`.gitignore` covers the usual names).
