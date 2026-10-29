# BusBoard Setup Guide

## 1. Prerequisites

- Raspberry Pi 3B+ (or newer) running Raspberry Pi OS **Trixie Lite**
  (64-bit), no desktop. When flashing with Raspberry Pi Imager, set the
  user to `planner`, enter your WiFi, set the WiFi country to **LU**, and
  enable SSH.
- HDMI display configured for 800x600 (via `/boot/firmware/config.txt` - see
  the Raspberry Pi OS docs for the `hdmi_cvt`/`hdmi_group`/`hdmi_mode`
  overrides if EDID auto-detection doesn't offer 800x600).
- A mobiliteit.lu (ATP) API key. Request one by emailing
  `opendata-api@atp.etat.lu` (see the "API mobiliteit.lu (ATP)" dataset page
  on [data.public.lu](https://data.public.lu/en/datasets/api-mobiliteit-lu/)).
  Default quota is 500 requests/hour and 5,000/day.
- A network connection for the install (WiFi or an Ethernet cable).

## 2. Install

```bash
git clone <repository-url> ~/busboard-src
cd ~/busboard-src
sudo ./scripts/install.sh
sudo reboot
```

Optional overrides: `DISPLAY_USER=<user>` (default `planner`) and
`TIMEZONE=<zone>` (default `Europe/Luxembourg`) and `UV_VERSION=<version>`,
e.g.
`sudo DISPLAY_USER=pi ./scripts/install.sh`. The WiFi country is set on the
admin **WiFi** page (default LU).

The installer:

1. Installs apt packages (Python, NetworkManager, polkit, `iw`, `ping`, SSH
   server, `cage`, `seatd`, `wlr-randr`, Chromium, curl, rsync) and a pinned,
   checksum-verified release of [uv](https://docs.astral.sh/uv/) to
   `/usr/local/bin/uv`.
2. Creates the unprivileged `busboard` system user (no login shell, no
   sudo) and gives the display user access to the `video` group.
3. Copies the app to `/opt/busboard` and installs the exact dependency
   versions from `uv.lock` (`uv sync --locked --no-dev`) into
   `/opt/busboard/.venv`, using the system's Python 3.13.
4. Creates `/etc/busboard` and `/var/lib/busboard` (mode `0700`, owned by
   `busboard`).
5. Installs the polkit rule (NetworkManager actions, NetworkManager restart,
   reboot - nothing else).
6. Hardens networking: WiFi country helper, WiFi power saving off, saved
   WiFi profiles reconnect forever, an auto-DHCP Ethernet profile, SSH on,
   and the boot-partition WiFi importer.
7. Sets timezone and NTP, enables the hardware watchdog, and keeps a
   persistent (64 MB-capped) journal.
8. Installs and starts `busboard-web`, `busboard-netwatch`,
   `busboard-display`, `busboard-screen`, and enables
   `busboard-wifi-import`, `busboard-wifi-country` and
   `busboard-reset-password` (runs only when its boot-partition flag file
   exists). Installs `busboard-hotspot.service` (never enabled; started
   on demand for a WPA2 hotspot) and the root helpers
   `busboard-reset-password`, `busboard-hotspot-test` and
   `busboard-wifi-fix` in `/usr/local/sbin`. Disables the text console on
   tty1 so the board owns the screen; tty2 stays available.

Re-running `sudo ./scripts/install.sh` is safe; it updates code,
dependencies and units in place without touching your config or API key. Reboot once after
the first install so the WiFi country and watchdog take effect.

## 3. First login

The admin login (username `admin` and a generated password) is printed at
the end of the install, and shown in the board's bottom bar until you
change it at `/admin/account`. Forgot it later? `sudo busboard-reset-password`
over SSH, or create an empty file named `busboard-reset-password` on the SD
card's boot partition and restart; the new one appears on the board.

Browse to `http://<pi-ip>:8080/admin` from a device on the same network
(the IP is shown on the board under the clock), log in as `admin`, and
change the password on the **Account** page.

Two addresses matter:

| Address | Login | What it's for |
|---|---|---|
| `/status` | no | Read-only health: display on/off, configured stops and quais, whether live data is flowing, network (WiFi name, Ethernet, internet, hotspot), API key present, requests used. No departures, no passwords. |
| `/admin` | yes | All settings: **Stops**, **API Key**, **Schedule**, **Options**, **WiFi**, **Account**. After logging in you land on `/status`, and the settings pages appear in the menu (the current page is highlighted). |

## 4. Configure your stops

On the **Stops** page, search by name (e.g. "Steinfort") and pick a stop
for Direction A and one for Direction B. If the stop has several quais, a
second list shows each quai with an example destination - pick the one for
your side of the road. If quais can't be loaded, the list says so and offers
"Try again"; the form won't save a name that wasn't picked from the list. In Luxembourg, the two directions
of a route are usually different physical stops/platforms, so pick the one
for each side of the road. Optionally set a display label (e.g. "Towards
Eischen"), **Show only these lines** and **Highlight these lines** (two
separate tag pickers listing the lines that serve the chosen stop and quai;
you can also type a line and press Enter), and the **walking
time** to that stop (default 5 minutes) and whether "Leave now" is shown
for **all lines** or **highlighted lines only** (all lines if none are
highlighted). For each line, the next bus you can still reach gets the
badge from walking time + margin until walking time before it leaves, using
real-time delays; it's never shown in timetable mode or on cancelled buses.
The badge style (**Off / Icon / Icon and text**) and the margin (default 2
minutes, same for both stops) are on **Options**. Walking time 0 turns it
off for that stop.

The autocomplete uses a locally cached nationwide stop list (the API has no
text search), fetched once and cached for a week; typing never calls the API.

## 5. Add your API key

On the **API Key** page, paste the key. It's written to
`/etc/busboard/api_key` (`0600`) and never displayed again.

## 6. Optional: schedule and options

- **Schedule**: up to five on/off windows, each on chosen days of the week
  (day chips, with Weekdays / Weekends / Every day shortcuts; windows that
  are off collapse to one line). Outside them the HDMI
  output is switched off (the panel goes to standby) and no API requests are
  made. The board fetches fresh times 30 seconds before each on-time and
  skips the last fetch just before an off-time. If no window is enabled, the
  board is always on.
- **Options**: a live preview row shows how the board will look.
  Departure time (clock, minutes, or both), clock format (24-hour or
  12-hour AM/PM, applied to every clock time), 180° rotation, IP
  address under the clock, wheelchair-accessible icon (off by default, since
  nearly all buses are accessible), colour theme (light, dark, or auto: light by
  day and dark at night, by sunrise and sunset at your stop - the admin
  pages' theme button offers the same three choices), minimum poll interval, look-ahead
  window, rows shown, and the hourly/daily request budgets.

The actual poll interval adapts: it slows down when nothing departs within
20 minutes and when needed to make the daily budget last. A board that's on
24 hours polls about every 45 s with the default budget; limiting it with a
schedule lets it poll every 30 s. The current interval is shown on the
**Status** page (`/status`).

## Network: WiFi, Ethernet and recovery

**The WiFi page** (`/admin/wifi`):

- **Join a WiFi network**: network name, password and country in one form.
  During normal operation the name field opens a list of nearby networks
  (with signal strength and a lock for secured ones) (you can
  still type one, e.g. a hidden network). While the setup hotspot is on it's
  a plain text field, because the radio can't scan then.
- **Country** (default Luxembourg; every country, sorted by name) is applied within a few seconds and kept
  across reboots. With the wrong country the board may not see a router on
  channel 12 or 13.
- **Other saved networks**: every saved network except the one currently
  connected, with **Connect** (switch to it) and **Forget** (delete it,
  after a confirmation). Networks are shown by their name, never by the
  internal connection name (e.g. `netplan-wlan0-Home`).
- **Setup hotspot**: name, password hidden behind **Show password**, and
  **Show QR code** (scan with a phone camera to join).

**WiFi and Ethernet are interchangeable.** Plug in a cable any time: the
board uses it while it's connected and goes back to WiFi when it's
unplugged. Add or change WiFi networks on the **WiFi** page; saved networks
are retried forever.

**What happens when the network goes away** (details in
`docs/ARCHITECTURE.md`):

- *Internet down, WiFi fine* (ISP or router uplink problem): the board shows
  "no internet access" and waits. It doesn't touch WiFi.
- *WiFi gone* (router off, WiFi chip stuck): after 1 minute it reconnects,
  after 2.5 minutes it resets the radio, after 4 minutes it restarts
  NetworkManager.
- *Still no link after 5 minutes*: it starts the **setup hotspot** and shows
  its name, password and address on the board. Every 5 minutes it briefly
  drops the hotspot to try your saved networks again, so it returns to your
  WiFi within about 5 minutes of the router coming back. It doesn't do this
  while a phone/laptop is connected to the hotspot (for up to 20 min).
- *Still broken after 30 minutes*: it reboots. If that doesn't help, the next
  reboot waits twice as long (1 h, 2 h ... at most once a day). After a
  reboot the saved networks are always tried first.

These timings are in `config.yaml` under `wifi_ap`
(`grace_period_seconds`, `retry_interval_seconds`, `reboot_after_seconds`;
`0` disables reboots). The hotspot name and password are also set there
(`ssid`, `password`); a random password is generated on first run and can be
revealed on the **WiFi** page.

### Using the setup hotspot

While the hotspot is on, the board replaces the departures with two QR
codes:

1. Scan the first to join the setup WiFi (or join it by hand: name and
   password are printed under the code).
2. Scan the second to open `http://10.42.0.1:8080/admin`, and log in.
3. On the **WiFi** page, enter your network. The hotspot disappears as the
   board connects; rejoin your normal WiFi.

### If you can't reach the board at all

In order of effort, none of these needs a reformat:

1. **Ethernet**: plug the Pi into your router with a cable. It gets an
   address by DHCP (shown on the board) and you can use `/admin` or
   `ssh planner@<ip>`.
2. **Edit a file on the SD card**: power off, put the SD card in any
   computer, and create `busboard-wifi.txt` on the `bootfs` partition:

   ```
   ssid=Your WiFi name
   password=your wifi password
   ```

   Put the card back and power on. The network is saved (and preferred) and
   the file is deleted, since it contains a plaintext password. Add
   `hidden=yes` for a hidden network.
3. **Keyboard**: plug a USB keyboard into the Pi and press Ctrl+Alt+F2,
   which should give a text login on tty2 (the kiosk runs cage with VT
   switching allowed), then use `sudo nmtui`. Ctrl+Alt+F1 goes back to the
   board.

### Testing the setup hotspot

Over SSH:

```bash
sudo busboard-hotspot-test          # 2 minutes; add a number for another duration
sudo busboard-hotspot-test --open   # same, without a password (for troubleshooting)
```

**Hotspot security** is set on the admin WiFi page:

- **Open** (default): no password, run by NetworkManager. Works on the Pi 3.
  Anyone nearby can join during an outage, but every settings page still
  needs your admin login.
- **WPA2 password**: run by hostapd (`busboard-hotspot.service`), because
  NetworkManager's WPA2 hotspot rejects phones on the Pi 3's WiFi chip.
  Test it first with `sudo busboard-hotspot-test --wpa2`, and only switch
  once your phone joins.

It runs in the background, so it's fine that SSH over WiFi drops when the
hotspot starts. Join the hotspot from a phone (the board shows its QR
codes); after 2 minutes the Pi switches back to your WiFi. Then read the
result with `journalctl -u busboard-hotspot-test --no-pager`.

Logs survive reboots, so afterwards you can see what happened:

```bash
journalctl -b -1 -u busboard-netwatch     # previous boot
journalctl -u busboard-netwatch --since "2 hours ago"
```

## Uninstalling

```bash
sudo ./scripts/uninstall.sh          # keeps /etc/busboard and /var/lib/busboard
sudo ./scripts/uninstall.sh --purge  # also removes config, API key, and the busboard user
```

Uninstall removes the NetworkManager, watchdog and journald drop-ins and
the hotspot profile. It leaves the WiFi country, timezone, SSH and your
saved networks as they are.

## Troubleshooting

- `systemctl status busboard-web busboard-netwatch busboard-display` -
  are the services running?
- `journalctl -u busboard-netwatch -f` - network decisions (lost, reconnect,
  hotspot, reboot) as they happen.
- `journalctl -u busboard-web -f` - API errors and poll interval changes.
- `journalctl -u busboard-display -f` - if the screen stays black
  (cage/Chromium errors).
- `cat /run/busboard/network_status.json` - what netwatch currently sees.
- `/status` shows how many WiFi networks are saved. "None" means the board
  can only come back via Ethernet or the hotspot; add one on the admin WiFi
  page.
- "No upcoming departures" for a stop you know has service: check the stop
  ID on the **Stops** page and that any line filter matches the line numbers
  mobiliteit.lu reports.
- The bar at the bottom of the board:
  - **red** - something is wrong on our side: no network (with hotspot
    details if it's on), board service not answering, or "Live times
    unavailable since HH:MM - showing timetable" (live data older than
    2 minutes; planned times are shown without delays until it recovers,
    with the reason - no internet, quota reached, API key rejected, API not
    responding).
  - **amber** - a service message from the operator for a line on screen
    (that line's badge also gets a "!").
- Departures that have left are removed automatically.
- The screen doesn't switch off at night: check
  `journalctl -u busboard-screen`. If it says hardware power-off was
  disabled, cage didn't cope with switching the output off, and the board
  shows a black page instead.
