# BusBoard Architecture

## Processes

| Service                 | Runs as    | Entry point                    | Responsibility |
|-------------------------|------------|--------------------------------|----------------|
| `busboard-web`          | `busboard` | `busboard.web.main`            | Serves the kiosk board (`/`, `/api/board`), public `/status`, and the password-protected `/admin` area. Runs the departure poller thread. |
| `busboard-netwatch`     | `busboard` | `busboard.netwatch.main`       | Probes the network, self-heals WiFi, runs the fallback hotspot, publishes network status. |
| `busboard-display`      | `planner`  | `cage` + `chromium --kiosk`    | Shows `http://localhost:8080/` full screen on HDMI. No desktop. |
| `busboard-screen`       | `planner`  | `busboard.screen.main`         | Switches the HDMI output off/on with the display schedule (`wlr-randr` in cage's session). |
| `busboard-wifi-import`  | root       | `/usr/local/sbin/busboard-wifi-import` | One-shot at boot, only if `/boot/firmware/busboard-wifi.txt` exists. |
| `busboard-wifi-country` | root       | `/usr/local/sbin/busboard-wifi-country` | Applies `wifi_country` from config.yaml; at boot and on every config change (`.path` unit). |
| `busboard-hotspot`      | root       | `/usr/local/sbin/busboard-hotspot` | WPA2 setup hotspot (hostapd + dnsmasq on wlan0). Never enabled; started and stopped by netwatch via polkit, only when hotspot security is WPA2. |
| `busboard-reset-password` | root     | `/usr/local/sbin/busboard-reset-password` | One-shot at boot, only if `/boot/firmware/busboard-reset-password` exists; generates a new admin password. |

They are separate so that:

- The kiosk browser can crash/restart without touching the web app or the
  watchdog.
- The watchdog - the one thing that must keep working when everything else
  is broken - has a single loop and minimal dependencies.

## Shared state

Processes never talk to each other directly; they share files:

| Path | Written by | Contents |
|---|---|---|
| `/etc/busboard/config.yaml` | web (admin forms; generates the admin and hotspot passwords on first run), `busboard-reset-password`, `busboard-hotspot-test --new-password` | stops, schedule, options, hotspot + watchdog settings, admin hash. Atomic writes, `0600`. |
| `/etc/busboard/api_key` | web | mobiliteit.lu API key, `0600`, separate so it's never shown/logged with config. |
| `/etc/busboard/flask_secret_key` | web | session signing key. |
| `/run/busboard/rate_limit_state.json` | web (every request) | live hourly/daily request counters (tmpfs). |
| `/var/lib/busboard/rate_limit_state.json` | web (clean exit only) | copy of the counters so they survive a reboot. |
| `/var/lib/busboard/stops_cache.json` | web | nationwide stop list for autocomplete (refreshed weekly). |
| `/var/lib/busboard/netwatch_state.json` | netwatch | consecutive-reboot counter for the reboot backoff. |
| `/run/busboard/network_status.json` | netwatch (every ~20 s) | link/internet/hotspot state. tmpfs, so frequent writes never touch the SD card. |
| `/run/busboard/hotspot.env` | netwatch (WPA2 hotspot only) | SSID and password for the root `busboard-hotspot` script, which validates them and builds the hostapd config itself. |

## The board page

`/` renders once, with the full board state embedded as JSON. After that
the page:

- fetches `/api/board` every 15 s (local only - costs no API quota);
- computes "minutes until" itself every second from each departure's
  absolute timestamp, so countdowns are exact rather than as old as the last
  page load;
- drops departures whose minute has passed;
- switches a column to **timetable mode** when its live data is older than
  2 minutes (or 2 poll intervals, if longer): planned times only, no delays,
  and an alert in the notice bar ("Live times unavailable since HH:MM -
  showing timetable") with the reason (no internet, quota, API down ...);
- shows "Leave now" on the next reachable bus of each line (line +
  destination): from walking time + margin down to walking time before the
  real-time departure, compared in seconds. Per stop: walking time and
  all/highlighted lines; global: style (off/icon/text) and margin (default
  2 min). Not shown in timetable mode or for cancelled buses;
- formats every clock time as 24-hour or 12-hour (small AM/PM);
- marks wheelchair-accessible journeys (only if enabled in Options), strikes through cancelled ones, and
  puts a "!" on the line badge when the operator has a message for it;
- only rebuilds the DOM when something visible changes (Chromium draws in
  software on the Pi 3B+, so repaints are the main CPU cost);
- keeps showing the last data if the server doesn't answer, with a notice
  after three failed fetches, instead of navigating to Chromium's error
  page (which has no way to recover by itself);
- reloads itself when the server restarts with new code (`app_version`) or
  after 24 h, but only right after a successful fetch;
- blacks out the page outside the display schedule (fallback if the HDMI
  output can't be switched off).

It fills the viewport (`100vw` x `100vh`, 800x600 today) and never scrolls;
long names are ellipsised. Each row is exactly 1/N of the column height
(N = rows shown, max 8), so a full column fills the screen with no empty
band at the bottom; when the notice bar appears, the rows shrink to make
room.

### Notice bar

One bar at the bottom, following the light/dark theme, showing the most
important thing first:

1. **alert** (red accent): board service not answering, network problem
   (including the setup hotspot's name/password/address), or live data
   unavailable (timetable mode).
2. **notice** (amber accent): the operator's service messages for the
   departures on screen - HAFAS notes of type R (realtime) and I (infotext),
   plus HIM `Messages` if present - as "Line 26: ...", rotating every 8 s
   when there are several.

Nothing is shown when there's nothing to say.

Times without realtime data are shown in the normal text colour; green means
realtime data confirms the departure is on time.

## Departure polling (`busboard/core/poller.py`)

The poller thread runs inside `busboard-web` and polls both stops
back-to-back. The interval adapts (`compute_poll_interval`):

1. never faster than the configured `poll_interval_seconds` (default 30 s);
2. at least 60 s when nothing departs within 20 minutes;
3. slow enough that the remaining daily budget (minus a 5 % reserve for the
   admin pages' stop/quai lookups) lasts until the counter resets, counting only time when the
   display schedule is on; same for the hourly budget.

Rule 3 is why a board that's on 24 hours polls about every 45 s with the
default 4,000/day budget, and every 30 s when the schedule turns it off
part of the day. The old fixed 30 s interval used 5,760 requests/day, ran out
of budget at about 18:40 local time and showed stale times all evening.

Polling follows the display schedule: it starts 30 s before a scheduled
on-time (so the screen wakes up with current times), skips any poll less
than 60 s before the off-time, and makes no API calls while the display is
off.

API errors are classified (`api_client.classify_error`, codes from the
mobiliteit.lu departureBoard docs):

| Kind | Examples | Poller behaviour |
|---|---|---|
| quota | `API_QUOTA`, HTTP 429 | stop calling until the server's `Retry-After`, else the next full hour |
| auth | `API_AUTH`, 401/403 | retry every 10 min; board says to check the API key |
| no result | `SVC_NO_RESULT`, `SVC_NO_MATCH` | not an error: nothing departs |
| bad request | `API_PARAM`, `SVC_LOC` ... | shown per stop (configuration problem) |
| transient | timeouts, DNS, 5xx, `INT_ERR` | exponential backoff 60 s -> 5 min, reset on success |

A quota or key error on the first stop skips the second stop's call.

It requests 4 more journeys than it displays, so rows that leave between
polls are backfilled. Departure times are parsed in `config.timezone`
(Europe/Luxembourg) regardless of the OS timezone, sorted by full date+time
(realtime if known), and a stop marked `reachable: false` (skipped, e.g. by a
diversion) is shown as cancelled.

## API usage and rate limiting

Every call through `MobiliteitClient` consults the process-wide
`RateLimiter` (`get_shared_limiter`) first; if the hourly or daily budget
is exhausted it raises before any HTTP request is made. Defaults (400/h,
4,000/day) sit below mobiliteit.lu's documented 500/h, 5,000/day.

The counters live on tmpfs and are written to the SD card only on a clean
exit (service restart, shutdown, reboot). After a power cut they restart
from the last clean shutdown; the margin below the real quota absorbs that,
and a real `API_QUOTA` answer is handled as above.

## SD card writes

Normal operation writes almost nothing to the SD card: departures, network
status and request counters are in RAM/tmpfs. What does get written: the
journal (persistent, 64 MB cap, kept on purpose for diagnosis), config when
you save an admin form, the stop list weekly, the request counters and the
reboot counter at shutdown/reboot, and the WiFi country only when it
changes. Chromium runs incognito (no profile or cache on disk).

## Screen power

`busboard-screen` runs in the kiosk user's cage session and asks
`/api/screen` once every 20 s whether the display should be on. At the
scheduled off-time it switches the HDMI output off with `wlr-randr` (cage
supports the wlr-output-management protocol), so the panel goes into
standby; at the on-time it switches it back on (the board already has data
from the warm-up poll). If the web service doesn't answer, the screen is
switched on. If switching the output off makes cage exit, hardware
power-off is disabled until the next reboot and the page's black overlay is
used instead.

## Network resilience (`busboard/netwatch/main.py`)

The Pi 3B+ has one WiFi radio. While the fallback hotspot is up, it can't
be connected to your WiFi, can't scan, and can't reconnect. The original
watchdog started the hotspot after 90 s offline and then never left it,
because its "back online" check could never succeed from hotspot mode. Any
router reboot or internet outage longer than 90 s took the board offline
until someone logged in. The watchdog now works like this:

Each check (every 20 s) classifies the situation:

- **online** - any uplink with working internet. Hotspot off.
- **upstream down** - Ethernet is connected, or WiFi is connected and the
  default gateway answers, but there's no internet. The router or ISP is at
  fault; netwatch waits and never touches WiFi (starting the hotspot here
  would disconnect a working WiFi link).
- **link broken** - no uplink at all, or WiFi "connected" but the gateway
  doesn't answer (typical of a wedged brcmfmac chip). Healing ladder:

| After the link broke | Action |
|---|---|
| 60 s | scan and re-activate saved networks in range |
| 150 s | WiFi radio off/on, re-activate |
| 240 s | restart NetworkManager |
| 300 s (`grace_period_seconds`) | start the setup hotspot - only if there's no link at all |
| every 300 s (`retry_interval_seconds`) | take the hotspot down, try saved networks, bring it back if that fails. Postponed while a device is connected to the hotspot, up to 20 min |
| 30 min (`reboot_after_seconds`) | reboot. Doubles after each consecutive reboot without recovery (1 h, 2 h ... max 24 h); never while someone is on the hotspot; reset once online |

After any reboot or netwatch restart, the hotspot is off (its profile is
`autoconnect=no` and netwatch takes it down at startup), so the saved
networks are always tried first.

At startup and every 30 min, netwatch sets every saved WiFi profile to
reconnect forever (`autoconnect-retries 0`) with power saving off; it only
writes when something differs.

Ethernet and WiFi are interchangeable: NetworkManager prefers Ethernet
(lower route metric) while a cable is plugged in and WiFi otherwise. Both
count as an uplink, and plugging in a cable while the hotspot is up turns
the hotspot off.

While the hotspot is on, the board shows its name, password and setup URL
(`http://10.42.0.1:8080`).

System-level settings applied by `install.sh`:

- WiFi country from the admin WiFi page (default `LU`), applied by the
  root-owned `busboard-wifi-country` helper, so 2.4 GHz channels 12-13 work;
- NetworkManager drop-in with WiFi power saving off and no scan MAC
  randomisation (both known to cause drops on the 3B+);
- hardware watchdog (`RuntimeWatchdogSec=15s`) for kernel hangs;
- persistent, 64 MB-capped journal, so an incident can be diagnosed after a
  reboot;
- SSH enabled; timezone and NTP set;
- `busboard-wifi-import`, which reads WiFi credentials from the boot
  partition.

## Security notes

- The only network-exposed service is `busboard-web` on port 8080. The
  board (`/`, `/api/board`, `/api/screen`) and `/status` are public on the
  LAN; `/status` shows health and configured stop names only (no departures,
  no API key, no hotspot password). Everything under `/admin` (pages and
  `/admin/api/*`) requires login, the login form itself is CSRF-protected,
  and redirects after login stay inside `/admin`, sessions are signed with a persisted random key, and all
  state-changing routes are CSRF-protected.
- `/api/board` includes the hotspot password (and its WiFi QR code) only
  while the hotspot is active - that's when the board shows it full-screen
  so the board can be recovered with a phone.
- `busboard-web` and `busboard-netwatch` don't run as root. The polkit rule
  grants the `busboard` user only: the NetworkManager actions used,
  `systemctl restart NetworkManager.service` (that unit and verb only),
  start/stop of `busboard-hotspot.service`, and reboot.
- The root helpers (boot-partition importer, WiFi country, WPA2 hotspot,
  password reset) run from root-owned copies in `/usr/local/sbin`, never
  from `/opt/busboard` (which the `busboard` user can write to). The
  importer deletes the plaintext WiFi file after importing it.
- Intended for a private home network, not for exposure to the internet.
