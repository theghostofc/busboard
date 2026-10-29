#!/usr/bin/env bash
# BusBoard installer for Raspberry Pi OS Trixie (64-bit, NetworkManager-based).
#
# Run as root from the repository root, e.g.:
#   sudo bash scripts/install.sh
#
# Optional environment overrides:
#   DISPLAY_USER=planner          user that runs the Chromium kiosk
#   TIMEZONE=Europe/Luxembourg    system timezone (departure times are local)
#   UV_VERSION=0.12.17            uv release to install (checksum-verified)
#
# The WiFi country is chosen on the admin WiFi page (/admin/wifi) (default LU) and
# applied by busboard-wifi-country.service.
#
# What this does:
#   1. Installs required apt packages.
#   2. Creates the `busboard` system service account (web + netwatch processes).
#   3. Adds the display user to the `video` group for DRM/KMS access.
#   4. Creates /etc/busboard (config + secrets) and /var/lib/busboard (state).
#   5. Copies application code to /opt/busboard, installs uv, and installs the
#      exact dependency versions from uv.lock into /opt/busboard/.venv.
#   6. Installs the polkit rule (NetworkManager, NM restart, reboot).
#   7. Hardens networking: WiFi country, power saving off, reconnect forever,
#      Ethernet auto-DHCP, SSH, boot-partition WiFi import.
#   8. Hardens the system: timezone + NTP, hardware watchdog, persistent
#      (size-capped) journal.
#   9. Installs and enables the systemd services.
#  10. Disables getty@tty1 so the kiosk owns the screen (tty2 stays available).
#
# Safe to re-run: steps that already exist are skipped or updated in place.
# Does NOT modify /etc/busboard or /var/lib/busboard contents if they exist.

set -euo pipefail

log()  { echo "==> $*"; }
warn() { echo "WARN: $*" >&2; }

if [[ "${EUID}" -ne 0 ]]; then
  echo "This installer must be run as root (e.g. sudo bash scripts/install.sh)." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/busboard"
CONFIG_DIR="/etc/busboard"
STATE_DIR="/var/lib/busboard"
SERVICE_ACCOUNT="busboard"
DISPLAY_USER="${DISPLAY_USER:-planner}"
TIMEZONE="${TIMEZONE:-Europe/Luxembourg}"
VENV="${INSTALL_DIR}/.venv"
UV_VERSION="${UV_VERSION:-0.12.17}"
UV_BIN="/usr/local/bin/uv"
UV_CACHE="/var/cache/busboard/uv"
POLKIT_RULES_DIR="/etc/polkit-1/rules.d"

# ── 1. apt packages ────────────────────────────────────────────────────────────
log "Installing apt packages"
apt-get update -qq
apt-get install -y \
  python3 \
  curl \
  ca-certificates \
  rsync \
  network-manager \
  iw \
  iputils-ping \
  openssh-server \
  cage \
  seatd \
  wlr-randr \
  hostapd \
  dnsmasq-base \
  chromium
systemctl enable --now seatd 2>/dev/null || warn "could not enable seatd"

# polkitd was renamed from policykit-1 in Debian Trixie.
if apt-cache show polkitd >/dev/null 2>&1; then
  apt-get install -y polkitd pkexec
else
  apt-get install -y policykit-1
fi

# ── 2. busboard service account ────────────────────────────────────────────────
log "Ensuring '${SERVICE_ACCOUNT}' system account exists"
if ! id -u "${SERVICE_ACCOUNT}" >/dev/null 2>&1; then
  useradd \
    --system \
    --no-create-home \
    --home-dir "${INSTALL_DIR}" \
    --shell /usr/sbin/nologin \
    "${SERVICE_ACCOUNT}"
fi

# ── 3. display user — video group for KMS/DRM framebuffer access ───────────────
log "Adding '${DISPLAY_USER}' to the 'video' group (required for DRM display)"
if id -u "${DISPLAY_USER}" >/dev/null 2>&1; then
  usermod --append --groups video "${DISPLAY_USER}"
else
  warn "User '${DISPLAY_USER}' does not exist — skipping video group assignment."
  warn "Create the user and re-run, or set DISPLAY_USER=<your-username> before running."
fi

# ── 4. config and state directories ───────────────────────────────────────────
log "Setting up config directory: ${CONFIG_DIR}"
mkdir -p "${CONFIG_DIR}"
chown "${SERVICE_ACCOUNT}:${SERVICE_ACCOUNT}" "${CONFIG_DIR}"
chmod 700 "${CONFIG_DIR}"

log "Setting up state directory: ${STATE_DIR}"
mkdir -p "${STATE_DIR}"
chown "${SERVICE_ACCOUNT}:${SERVICE_ACCOUNT}" "${STATE_DIR}"
chmod 700 "${STATE_DIR}"

# ── 5. application code and Python venv ───────────────────────────────────────
log "Copying application code to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
rsync -a --delete \
  --exclude ".venv*" \
  --exclude ".git" \
  --exclude ".backup" \
  --exclude "__pycache__" \
  --exclude "*.pyc" \
  --exclude ".env" \
  --exclude "*.log" \
  --exclude "response.json" \
  "${REPO_DIR}/" "${INSTALL_DIR}/"
chown -R "${SERVICE_ACCOUNT}:${SERVICE_ACCOUNT}" "${INSTALL_DIR}"

# uv: a pinned release from GitHub, verified against the release's SHA-256
# before it's installed (no `curl | sh`).
install_uv() {
  local arch target tmp
  arch="$(uname -m)"
  case "${arch}" in
    aarch64|arm64) target="aarch64-unknown-linux-gnu" ;;
    armv7l)        target="armv7-unknown-linux-gnueabihf" ;;
    x86_64)        target="x86_64-unknown-linux-gnu" ;;
    *) echo "Unsupported CPU architecture for uv: ${arch}" >&2; exit 1 ;;
  esac
  tmp="$(mktemp -d)"
  local base="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}"
  curl -fsSL --retry 3 -o "${tmp}/uv.tar.gz"        "${base}/uv-${target}.tar.gz"
  curl -fsSL --retry 3 -o "${tmp}/uv.tar.gz.sha256" "${base}/uv-${target}.tar.gz.sha256"
  (cd "${tmp}" && echo "$(cut -d' ' -f1 uv.tar.gz.sha256)  uv.tar.gz" | sha256sum -c --quiet -) \
    || { echo "uv download failed checksum verification" >&2; rm -rf "${tmp}"; exit 1; }
  tar -xzf "${tmp}/uv.tar.gz" -C "${tmp}"
  install -m 0755 -o root -g root "${tmp}/uv-${target}/uv" "${UV_BIN}"
  rm -rf "${tmp}"
}

if [[ -x "${UV_BIN}" ]] && "${UV_BIN}" --version | grep -q " ${UV_VERSION}\b"; then
  log "uv ${UV_VERSION} already installed"
else
  log "Installing uv ${UV_VERSION}"
  install_uv
fi

log "Installing Python dependencies from uv.lock into ${VENV}"
mkdir -p "${UV_CACHE}"
chown -R "${SERVICE_ACCOUNT}:${SERVICE_ACCOUNT}" "$(dirname "${UV_CACHE}")"
# --locked:  fail if uv.lock doesn't match pyproject.toml (never re-resolve
#            on the device - that could silently pick different versions)
# --no-dev:  runtime dependencies only
# --python /usr/bin/python3 + UV_PYTHON_DOWNLOADS=never: use Debian's
#            Python 3.13 (security updates via apt), never a downloaded one
# --compile-bytecode: precompile, so the first start after an install is fast
(cd "${INSTALL_DIR}" && sudo -u "${SERVICE_ACCOUNT}" env \
    UV_CACHE_DIR="${UV_CACHE}" \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_PROGRESS=1 \
    "${UV_BIN}" sync --locked --no-dev --python /usr/bin/python3 --compile-bytecode)

# ── 6. polkit rule ────────────────────────────────────────────────────────────
log "Installing polkit rule: ${POLKIT_RULES_DIR}/50-busboard-nm.rules"
mkdir -p "${POLKIT_RULES_DIR}"
install -m 0644 \
  "${INSTALL_DIR}/systemd/polkit-busboard-nm.rules" \
  "${POLKIT_RULES_DIR}/50-busboard-nm.rules"

# ── 7. networking hardening ───────────────────────────────────────────────────
# 7a. WiFi regulatory domain. Without it the radio runs in the "world"
#     domain and can't join 2.4 GHz channels 12-13, which routers in
#     Luxembourg may pick on their own - the board would silently lose WiFi.
#     The country comes from config.yaml (WiFi page, default LU); the root-owned
#     helper re-applies it whenever config.yaml changes and at boot.
log "Installing WiFi country helper"
install -m 0755 -o root -g root "${INSTALL_DIR}/scripts/busboard-wifi-country.sh" /usr/local/sbin/busboard-wifi-country
install -m 0644 "${INSTALL_DIR}/systemd/busboard-wifi-country.service" /etc/systemd/system/
install -m 0644 "${INSTALL_DIR}/systemd/busboard-wifi-country.path"    /etc/systemd/system/
/usr/local/sbin/busboard-wifi-country "${CONFIG_DIR}/config.yaml" LU || warn "could not set WiFi country"
rfkill unblock wifi 2>/dev/null || true

# 7b. NetworkManager defaults: WiFi power saving off (the Pi 3B+ brcmfmac
#     driver is known to drop links with it on), and no MAC randomisation
#     while scanning (another source of flaky reconnects on this chip).
log "Installing NetworkManager config: /etc/NetworkManager/conf.d/90-busboard.conf"
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/90-busboard.conf <<'NMCONF'
# Installed by BusBoard (scripts/install.sh). Removed by uninstall.sh.
[connection-busboard-wifi]
match-device=type:wifi
wifi.powersave=2

[device-busboard-wifi]
match-device=type:wifi
wifi.scan-rand-mac-address=no
NMCONF
systemctl reload NetworkManager 2>/dev/null || true
iw dev wlan0 set power_save off 2>/dev/null || true

# 7c. Saved WiFi profiles: retry forever instead of giving up after 4 tries.
#     (netwatch re-applies this at every start, for profiles added later.)
# Raspberry Pi Imager stores WiFi through netplan (profiles named
# "netplan-wlan0-<SSID>", generated at boot from /etc/netplan). Modifying
# those with nmcli makes NetworkManager rewrite the netplan YAML, and on this
# board the WiFi profile was gone after a reboot. Copy each one into a native
# NetworkManager keyfile (/etc/NetworkManager/system-connections), check the
# copy landed there, and only then remove the netplan original.
KEYFILE_DIR=/etc/NetworkManager/system-connections/
migrate_netplan_wifi() {
  local uuid type file ssid keymgmt psk
  while IFS=: read -r uuid type file; do
    [[ "${type}" == "802-11-wireless" ]] || continue
    [[ "${file}" == ${KEYFILE_DIR}* ]] && continue
    ssid="$(nmcli -g 802-11-wireless.ssid connection show uuid "${uuid}")"
    [[ -n "${ssid}" && "${ssid}" != "busboard-ap" ]] || continue
    keymgmt="$(nmcli -g 802-11-wireless-security.key-mgmt connection show uuid "${uuid}")"
    psk="$(nmcli -s -g 802-11-wireless-security.psk connection show uuid "${uuid}")"
    case "${keymgmt}" in
      wpa-psk|sae|"") ;;
      *) warn "WiFi '${ssid}' uses ${keymgmt}; leaving its netplan profile as it is"; continue ;;
    esac
    if ! nmcli -t -f NAME,FILENAME connection show | grep -Fq "${ssid}:${KEYFILE_DIR}"; then
      log "Copying netplan WiFi profile for '${ssid}' to a NetworkManager keyfile"
      local sec=(wifi-sec.key-mgmt "")
      if [[ -n "${psk}" ]]; then sec=(wifi-sec.key-mgmt wpa-psk wifi-sec.psk "${psk}"); fi
      if ! nmcli connection add type wifi ifname wlan0 con-name "${ssid}" ssid "${ssid}" \
          connection.autoconnect yes connection.autoconnect-retries 0 \
          802-11-wireless.powersave 2 "${sec[@]}" >/dev/null; then
        warn "could not copy WiFi '${ssid}'; keeping the netplan profile"
        continue
      fi
    fi
    if nmcli -t -f NAME,FILENAME connection show | grep -Fq "${ssid}:${KEYFILE_DIR}"; then
      nmcli connection delete uuid "${uuid}" >/dev/null && log "Removed netplan profile for '${ssid}' (keyfile copy in place)"
    else
      warn "keyfile copy of '${ssid}' not found; keeping the netplan profile"
    fi
  done < <(nmcli -t -f UUID,TYPE,FILENAME connection show)
}
migrate_netplan_wifi

log "Hardening saved WiFi profiles (reconnect forever, power saving off)"
while IFS=: read -r uuid type file; do
  [[ "${type}" == "802-11-wireless" && "${file}" == ${KEYFILE_DIR}* ]] || continue
  [[ "$(nmcli -g connection.id connection show uuid "${uuid}")" == "busboard-ap" ]] && continue
  nmcli connection modify uuid "${uuid}" \
    connection.autoconnect yes \
    connection.autoconnect-retries 0 \
    802-11-wireless.powersave 2 || warn "could not harden WiFi profile ${uuid}"
done < <(nmcli -t -f UUID,TYPE,FILENAME connection show)

# 7d. Ethernet: make sure plugging in a cable just works (DHCP). NetworkManager
#     prefers Ethernet over WiFi automatically (lower route metric) and falls
#     back to WiFi when the cable is removed, so both can be used
#     interchangeably.
if ip link show eth0 >/dev/null 2>&1; then
  if ! nmcli -t -f TYPE connection show | grep -qx "802-3-ethernet"; then
    log "Creating an auto-DHCP Ethernet profile for eth0"
    nmcli connection add type ethernet ifname eth0 con-name busboard-ethernet \
      connection.autoconnect yes connection.autoconnect-retries 0 ipv4.method auto ipv6.method auto \
      || warn "could not create Ethernet profile"
  fi
fi

# 7e. SSH, so a board on the network can always be reached.
log "Enabling SSH"
systemctl enable --now ssh 2>/dev/null || warn "could not enable ssh"

# 7f. Boot-partition WiFi import (root-owned copy - never run as root from
#     /opt/busboard, which the busboard user can write to).
log "Installing boot-partition WiFi importer"
install -m 0755 -o root -g root "${INSTALL_DIR}/scripts/busboard-wifi-import.sh" /usr/local/sbin/busboard-wifi-import
install -m 0755 -o root -g root "${INSTALL_DIR}/scripts/busboard-hotspot-test.sh" /usr/local/sbin/busboard-hotspot-test
install -m 0755 -o root -g root "${INSTALL_DIR}/scripts/busboard-wifi-fix.sh" /usr/local/sbin/busboard-wifi-fix
install -m 0755 -o root -g root "${INSTALL_DIR}/scripts/busboard-hotspot.sh" /usr/local/sbin/busboard-hotspot
install -m 0755 -o root -g root "${INSTALL_DIR}/scripts/busboard-reset-password.sh" /usr/local/sbin/busboard-reset-password
install -m 0644 "${INSTALL_DIR}/systemd/busboard-reset-password.service" /etc/systemd/system/
install -m 0644 "${INSTALL_DIR}/systemd/busboard-hotspot.service" /etc/systemd/system/
# The distro hostapd service must never grab wlan0 on its own.
systemctl disable --now hostapd.service 2>/dev/null || true
systemctl mask hostapd.service 2>/dev/null || true
install -m 0644 "${INSTALL_DIR}/systemd/busboard-wifi-import.service" /etc/systemd/system/

# ── 8. system hardening ───────────────────────────────────────────────────────
# 8a. Timezone + NTP: mobiliteit.lu returns local Luxembourg times and the Pi
#     has no RTC, so both the zone and time sync matter for countdowns.
log "Setting timezone to ${TIMEZONE} and enabling NTP"
timedatectl set-timezone "${TIMEZONE}" || warn "could not set timezone"
timedatectl set-ntp true 2>/dev/null || warn "could not enable NTP"

# 8b. Hardware watchdog: if the kernel hangs, the SoC watchdog reboots the Pi
#     (the bcm2835 watchdog supports at most ~15 s).
log "Enabling hardware watchdog"
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/90-busboard-watchdog.conf <<'WDCONF'
# Installed by BusBoard (scripts/install.sh). Removed by uninstall.sh.
[Manager]
RuntimeWatchdogSec=15s
RebootWatchdogSec=2min
WDCONF

# 8c. Keep logs across reboots (size-capped) so the next incident can be
#     diagnosed instead of lost.
log "Enabling persistent, size-capped journal"
mkdir -p /etc/systemd/journald.conf.d /var/log/journal
cat > /etc/systemd/journald.conf.d/90-busboard.conf <<'JCONF'
# Installed by BusBoard (scripts/install.sh). Removed by uninstall.sh.
[Journal]
Storage=persistent
SystemMaxUse=64M
SystemKeepFree=256M
JCONF
systemd-tmpfiles --create --prefix /var/log/journal 2>/dev/null || true
systemctl restart systemd-journald || true

# ── 9. systemd services ───────────────────────────────────────────────────────
log "Installing systemd unit files"
# Kiosk units: substitute the display user's name, uid and home.
if id -u "${DISPLAY_USER}" >/dev/null 2>&1; then
  DISPLAY_UID="$(id -u "${DISPLAY_USER}")"
  DISPLAY_HOME="$(getent passwd "${DISPLAY_USER}" | cut -d: -f6)"
else
  DISPLAY_UID=1000
  DISPLAY_HOME="/home/${DISPLAY_USER}"
fi
for unit in busboard-display.service busboard-screen.service; do
  sed -e "s#/run/user/1000#/run/user/${DISPLAY_UID}#g" \
      -e "s#/home/planner#${DISPLAY_HOME}#g" \
      -e "s#planner:planner#${DISPLAY_USER}:${DISPLAY_USER}#g" \
      -e "s#^User=planner#User=${DISPLAY_USER}#" \
      "${INSTALL_DIR}/systemd/${unit}" > "/etc/systemd/system/${unit}"
  chmod 0644 "/etc/systemd/system/${unit}"
done
install -m 0644 "${INSTALL_DIR}/systemd/busboard-web.service"      /etc/systemd/system/
install -m 0644 "${INSTALL_DIR}/systemd/busboard-netwatch.service" /etc/systemd/system/

# ── 10. disable getty@tty1 so the kiosk can own the screen ────────────────────
# A USB keyboard can still reach a text login on tty2 with Ctrl+Alt+F2.
log "Disabling getty@tty1 (Chromium kiosk needs exclusive TTY1 access), keeping tty2"
systemctl disable getty@tty1 2>/dev/null || true
systemctl stop    getty@tty1 2>/dev/null || true
systemctl enable getty@tty2 2>/dev/null || true

# ── 11. enable linger for display user so XDG_RUNTIME_DIR exists at boot ──────
log "Enabling linger for '${DISPLAY_USER}' (required for cage Wayland socket at boot)"
if id -u "${DISPLAY_USER}" >/dev/null 2>&1; then
  loginctl enable-linger "${DISPLAY_USER}"
else
  warn "User '${DISPLAY_USER}' not found — skipping linger setup."
fi

# ── 12. enable and start services ─────────────────────────────────────────────
log "Enabling and starting services"
systemctl daemon-reexec   # picks up the watchdog setting
systemctl daemon-reload
systemctl enable busboard-wifi-import.service
systemctl enable busboard-reset-password.service
systemctl enable busboard-wifi-country.service
systemctl enable --now busboard-wifi-country.path
systemctl enable --now busboard-web.service
systemctl enable --now busboard-netwatch.service
systemctl enable --now busboard-display.service
systemctl enable --now busboard-screen.service
# Restart so updated code/units take effect on a re-run.
systemctl restart busboard-web.service busboard-netwatch.service

# ── Admin login ───────────────────────────────────────────────────────────────
# busboard-web generates the password on first start; wait for it.
ADMIN_LINE=""
for _ in $(seq 30); do
  if ADMIN_LINE="$(cd "${INSTALL_DIR}" && runuser -u "${SERVICE_ACCOUNT}" -- env BUSBOARD_CONFIG_DIR="${CONFIG_DIR}" \
        "${VENV}/bin/python" -m busboard.web.reset_password --show 2>/dev/null)"; then
    break
  elif [[ $? -eq 1 ]]; then
    ADMIN_LINE="custom"; break
  fi
  sleep 1
done

# ── Done ───────────────────────────────────────────────────────────────────────
echo
echo "╔══════════════════════════════════════════════════════════╗"
echo "║              BusBoard install complete                   ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  Service status:                                         ║"
echo "║    systemctl status busboard-web busboard-netwatch       ║"
echo "║    systemctl status busboard-display                     ║"
echo "║                                                          ║"
echo "║  Status: http://<pi-ip>:8080/status   Admin: .../admin    ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo
if [[ -n "${ADMIN_LINE}" && "${ADMIN_LINE}" != "custom" ]]; then
  echo "Admin login:  username: ${ADMIN_LINE%% *}   password: ${ADMIN_LINE#* }"
  echo "(Also shown on the board until you change it at /admin/account.)"
elif [[ "${ADMIN_LINE}" == "custom" ]]; then
  echo "Admin login: unchanged (you've already set your own password)."
  echo "Forgot it? sudo busboard-reset-password"
else
  echo "Admin login: not ready yet - it will appear on the board shortly."
fi
echo
echo "NOTE: reboot once after the first install so the WiFi country,"
echo "watchdog and 'video' group membership all take effect."
