#!/usr/bin/env bash
# Test the setup hotspot:   sudo busboard-hotspot-test [seconds] [--open]
#   seconds  how long to keep it on (default 120)
#   --open   open hotspot (NetworkManager), whatever the setting on /admin/wifi
#   --wpa2   WPA2 hotspot (hostapd), whatever the setting on /admin/wifi
#
# Runs in the background as a systemd unit, so it finishes - and reconnects
# to your WiFi - even when your SSH session over WiFi drops as the hotspot
# comes up. netwatch is paused during the test and always restarted after.
# Installed by install.sh as /usr/local/sbin/busboard-hotspot-test.
set -euo pipefail
SECS=120
OPEN=""
NEWPW=""
for arg in "$@"; do
  case "${arg}" in
    --open) OPEN="--open" ;;
    --wpa2) OPEN="--wpa2" ;;
    --new-password) NEWPW="--new-password" ;;
    *[!0-9]*|"") echo "usage: sudo busboard-hotspot-test [seconds] [--open|--wpa2] [--new-password]" >&2; exit 1 ;;
    *) SECS="${arg}" ;;
  esac
done

if [[ "${BUSBOARD_HOTSPOT_TEST_INNER:-}" != "1" ]]; then
  [[ "${EUID}" -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
  systemctl reset-failed busboard-hotspot-test >/dev/null 2>&1 || true
  systemd-run --quiet --collect --unit=busboard-hotspot-test \
    -E BUSBOARD_HOTSPOT_TEST_INNER=1 "$(readlink -f "$0")" "${SECS}" ${OPEN} ${NEWPW}
  echo "Hotspot test started for ${SECS}s. If you're connected over WiFi, SSH will drop now."
  echo "On the phone, FORGET 'BusBoard-Setup' first if it was joined before (it remembers the old security type)."
  echo "Then join the hotspot; the board shows its QR codes."
  echo "Afterwards (back on WiFi): journalctl -u busboard-hotspot-test --no-pager"
  exit 0
fi

START="$(date '+%Y-%m-%d %H:%M:%S')"
trap 'systemctl start busboard-netwatch' EXIT
systemctl stop busboard-netwatch

echo "WiFi handshake offload fix: $(/usr/local/sbin/busboard-wifi-fix status 2>/dev/null || echo unknown)"
cd /opt/busboard
runuser -u busboard -- env \
  BUSBOARD_CONFIG_DIR=/etc/busboard BUSBOARD_STATE_DIR=/var/lib/busboard \
  BUSBOARD_RUNTIME_DIR=/run/busboard PYTHONUNBUFFERED=1 \
  /opt/busboard/.venv/bin/python -m busboard.netwatch.hotspot_test --seconds "${SECS}" ${OPEN} ${NEWPW} || true

# What the WiFi stack saw during the test (phones trying to join, handshake
# and DHCP), so one `journalctl -u busboard-hotspot-test` shows everything.
# Separate journalctl calls: -u and -k in one call are ANDed (no output).
FILTER='STA [0-9a-f:]{17}|WPA:|EAPOL|PSK|mismatch|deauth|disassoc|busboard-hotspot|hostapd|busboard-ap|wlan0|AP-STA|STA |sta_|assoc|auth|handshake|EAPOL|deauth|dnsmasq|DHCP|brcmf'
for src in "-u NetworkManager" "-u wpa_supplicant" "-u busboard-hotspot" "-k"; do
  echo "---- ${src} since ${START} ----"
  # shellcheck disable=SC2086
  journalctl --no-pager -o short --since "${START}" ${src} 2>/dev/null \
    | grep -iE "${FILTER}" | grep -v 'vndr ie set error' | grep -vi 'wpa_passphrase' | tail -n 120 || true
done
echo "---- dnsmasq leases ----"
cat /var/lib/NetworkManager/dnsmasq-wlan0.leases /run/busboard-hotspot/leases 2>/dev/null || echo "(none)"
exit 0
