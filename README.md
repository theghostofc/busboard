# BusBoard

A real-time bus/tram/train departure board for a Raspberry Pi 3B+ with an
800x600 HDMI display, showing the next departures in two directions from
chosen stops, using the Luxembourg `mobiliteit.lu` (ATP) open data API.

The Pi boots straight into a full-screen Chromium kiosk showing the board -
no desktop, no login prompt on the screen. Everything is configured from a
browser on the same network.

## Features

- Two columns, one per direction, each from its own stop and quai.
- Live delays and cancellations, with an automatic fallback to the
  timetable (and a notice) when live data is unavailable.
- "Leave now" badges based on the walking time to each stop.
- Per stop: show only chosen lines, and highlight chosen lines.
- Display schedule with up to five on/off windows; the HDMI output is
  switched off outside them and no API requests are made.
- Light, dark or auto (sunrise/sunset) theme; 24-hour or 12-hour clock.
- Self-healing WiFi, Ethernet support, and a setup hotspot with QR codes
  when there is no network at all.
- Public read-only `/status` page and a password-protected `/admin` area.
- Adaptive polling that stays within the API's hourly and daily quota.

## Hardware and software

- Raspberry Pi 3B+ (1 GB) or newer, with a microSD card.
- An HDMI display running at 800x600.
- Raspberry Pi OS Trixie Lite (64-bit), no desktop.
- A mobiliteit.lu (ATP) API key - see
  [docs/SETUP.md](docs/SETUP.md#1-prerequisites) for how to request one.

## Quick start (on the Pi)

```bash
git clone <repository-url> ~/busboard-src
cd ~/busboard-src
sudo ./scripts/install.sh
sudo reboot
```

The admin login (username `admin` and a generated password) is printed at
the end of the install, and shown in the board's bottom bar until you
change it at `/admin/account`. Forgot it later? `sudo busboard-reset-password`
over SSH, or create an empty file named `busboard-reset-password` on the SD
card's boot partition and restart; the new one appears on the board.

Then browse to `http://<pi-ip>:8080/admin` (the IP is shown on the board
under the clock) to configure stops and the API key. `http://<pi-ip>:8080/status`
shows the board's health without logging in.

## Repository layout

- `busboard/web` - Flask app served by waitress. Serves the kiosk board
  (`/`, updated in the background from `/api/board`), a public read-only
  status page (`/status`) and the password-protected admin area (`/admin`:
  stops, API key, WiFi, schedule, options, account). Also runs
  the background departure poller.
- `busboard/netwatch` - connectivity watchdog. Self-heals WiFi (reconnect,
  radio reset, NetworkManager restart, reboot as a last resort) and brings
  up a temporary setup hotspot (`BusBoard-Setup`) only when there is no
  network link at all. Always returns to the saved networks.
- `busboard/core` - shared config, API client, rate limiter, poller, stop
  search cache and network helpers.
- `busboard/screen` - switches the HDMI output off and on with the display
  schedule.
- `systemd/` - `busboard-web`, `busboard-netwatch`, `busboard-display`
  (cage + Chromium kiosk), `busboard-screen`, `busboard-wifi-import` (WiFi
  from the SD card's boot partition), `busboard-wifi-country`,
  `busboard-hotspot` (WPA2 setup hotspot via hostapd) and
  `busboard-reset-password` (password reset via a boot-partition flag
  file), plus the polkit rule.
- `scripts/` - installer, uninstaller and the root helpers they install
  (Pi only).
- `docs/` - setup guide and architecture notes.

## Development

Dependencies are managed with [uv](https://docs.astral.sh/uv/):
`pyproject.toml` lists the direct dependencies, `uv.lock` pins every package
with hashes, and `.python-version` pins Python 3.13 (what Raspberry Pi OS
Trixie ships). `busboard.core` and `busboard.web` have no
Raspberry-Pi-specific dependencies, so the web app runs on any machine:

```bash
uv sync                      # creates .venv with Python 3.13 and the locked dependencies
BUSBOARD_CONFIG_DIR=/tmp/busboard/etc BUSBOARD_STATE_DIR=/tmp/busboard/var \
  BUSBOARD_RUNTIME_DIR=/tmp/busboard/run \
  uv run python -m busboard.web.main
```

Open `http://localhost:8080/` for the board. Without `nmcli` the board
shows "No network connection" - that's expected on a dev machine.

Checks (there is no CI):

```bash
uv run --with ruff ruff check .            # lint + import sorting
uv run python -m compileall -q busboard    # syntax check
uv lock --check                            # uv.lock matches pyproject.toml
```

To change a dependency: edit `pyproject.toml`, run `uv lock`, and commit
both files. The scripts in `scripts/` change system configuration and are
meant for the Pi only; don't run them on a development machine.

## Documentation

- [docs/SETUP.md](docs/SETUP.md) - installation, configuration, network
  recovery and troubleshooting.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - processes, shared state,
  polling, rate limiting and network resilience.
- [CLAUDE.md](CLAUDE.md) - developer and coding-agent reference: design
  decisions to respect when changing the code.

## Data and security

Departure data comes from the mobiliteit.lu (ATP) open data API, published
on [data.public.lu](https://data.public.lu/en/datasets/api-mobiliteit-lu/).
You need your own API key; none is included.

BusBoard is meant for a private home network. Don't expose port 8080 to
the internet. The API key, admin password hash and session key live in
`/etc/busboard` on the Pi and never belong in this repository.

## License

MIT - see [LICENSE](LICENSE).
