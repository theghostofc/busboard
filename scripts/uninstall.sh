#!/usr/bin/env bash
# Removes BusBoard's systemd services, polkit rule, and installed code.
#
# Usage:
#   sudo bash scripts/uninstall.sh          # remove services + code, keep config
#   sudo bash scripts/uninstall.sh --purge  # also remove config, state, and service account
#
# Without --purge:
#   - /etc/busboard (API key, admin password hash, config) is preserved.
#   - /var/lib/busboard (stops cache, rate limiter state) is preserved.
#   - The `busboard` system account is preserved.
#   This lets you reinstall (re-run install.sh) without losing your setup.
#
# With --purge:
#   - The above directories and the `busboard` system account are also removed.
#   - getty@tty1 is re-enabled so the console is usable again.
#   - The `video` group membership added to the display user is NOT removed
#     (it's harmless to leave and could break other programs if removed).

set -euo pipefail

log()  { echo "==> $*"; }

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script must be run as root (e.g. sudo bash scripts/uninstall.sh)." >&2
  exit 1
fi

PURGE=false
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=true ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

if [[ "${PURGE}" == "true" ]]; then
  echo "WARNING: --purge will delete /etc/busboard (API key, config) and"
  echo "         /var/lib/busboard (cache, state), and remove the busboard user."
  read -r -p "Type 'yes' to confirm: " confirm
  if [[ "${confirm}" != "yes" ]]; then
    echo "Aborted."
    exit 0
  fi
fi

# ── Stop and disable services ─────────────────────────────────────────────────
log "Stopping and disabling BusBoard services"
systemctl disable --now busboard-display.service  2>/dev/null || true
systemctl disable --now busboard-web.service      2>/dev/null || true
systemctl disable --now busboard-netwatch.service 2>/dev/null || true
systemctl disable busboard-wifi-import.service    2>/dev/null || true
systemctl disable --now busboard-screen.service   2>/dev/null || true
systemctl disable --now busboard-wifi-country.path 2>/dev/null || true
systemctl disable busboard-wifi-country.service   2>/dev/null || true

# ── Remove systemd unit files ─────────────────────────────────────────────────
log "Removing systemd unit files"
rm -f /etc/systemd/system/busboard-display.service
rm -f /etc/systemd/system/busboard-web.service
rm -f /etc/systemd/system/busboard-netwatch.service
rm -f /etc/systemd/system/busboard-wifi-import.service
rm -f /usr/local/sbin/busboard-wifi-import
rm -f /usr/local/sbin/busboard-hotspot-test
rm -f /usr/local/sbin/busboard-wifi-fix
systemctl disable busboard-reset-password.service 2>/dev/null || true
rm -f /etc/systemd/system/busboard-reset-password.service /usr/local/sbin/busboard-reset-password
systemctl stop busboard-hotspot.service 2>/dev/null || true
rm -f /etc/systemd/system/busboard-hotspot.service /usr/local/sbin/busboard-hotspot
# /etc/modprobe.d/busboard-brcmfmac.conf is kept: it also helps normal WiFi.
rm -f /etc/systemd/system/busboard-screen.service
rm -f /etc/systemd/system/busboard-wifi-country.service /etc/systemd/system/busboard-wifi-country.path
rm -f /usr/local/sbin/busboard-wifi-country
systemctl daemon-reload

# ── Remove system tweaks installed by install.sh ─────────────────────────────
# Kept on purpose (harmless, generally useful): WiFi country, timezone/NTP,
# SSH, WiFi profile hardening, the Ethernet profile.
log "Removing NetworkManager, watchdog and journald drop-ins"
rm -f /etc/NetworkManager/conf.d/90-busboard.conf
systemctl reload NetworkManager 2>/dev/null || true
rm -f /etc/systemd/system.conf.d/90-busboard-watchdog.conf
rm -f /etc/systemd/journald.conf.d/90-busboard.conf
systemctl daemon-reexec
systemctl restart systemd-journald 2>/dev/null || true
# The fallback AP profile (never auto-connects, but no reason to keep it)
nmcli connection delete id busboard-ap >/dev/null 2>&1 || true

# ── Remove polkit rule ────────────────────────────────────────────────────────
log "Removing polkit rule"
rm -f /etc/polkit-1/rules.d/50-busboard-nm.rules

# ── Remove application code ───────────────────────────────────────────────────
log "Removing application code from /opt/busboard"
rm -rf /opt/busboard
rm -rf /var/cache/busboard   # uv download cache

# ── Purge: config, state, service account, re-enable getty ───────────────────
if [[ "${PURGE}" == "true" ]]; then
  log "Purging config: /etc/busboard"
  rm -rf /etc/busboard

  log "Purging state: /var/lib/busboard"
  rm -rf /var/lib/busboard
  log "Removing uv (installed by install.sh)"
  rm -f /usr/local/bin/uv

  log "Removing busboard system account"
  userdel busboard 2>/dev/null || true

  log "Re-enabling getty@tty1"
  systemctl enable --now getty@tty1 2>/dev/null || true
else
  echo
  echo "Config and state preserved:"
  echo "  /etc/busboard        (API key, admin hash, config.yaml)"
  echo "  /var/lib/busboard    (stops cache, rate limiter counters)"
  echo
  echo "To remove everything, re-run with --purge."
fi

echo
echo "==> Uninstall complete."
