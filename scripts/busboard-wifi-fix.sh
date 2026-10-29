#!/usr/bin/env bash
# Turn the Pi WiFi chip's firmware handshake offload off (or back on).
#
#   sudo busboard-wifi-fix on       # write the setting; takes effect after a reboot
#   sudo busboard-wifi-fix off      # remove it again; takes effect after a reboot
#   sudo busboard-wifi-fix status
#
# Why: the brcmfmac chip can run the WPA2 password exchange in its own
# firmware. With a password-protected setup hotspot, phones fail with
# "authentication error" while an open hotspot works - the known symptom of
# that offload misbehaving. feature_disable=0x82000 makes the normal software
# (wpa_supplicant) do the exchange instead, for the hotspot and for normal
# WiFi alike.
#
# The driver can't be reloaded while running (it holds its vendor module),
# so the setting goes into /etc/modprobe.d and applies at the next boot.
# Installed by install.sh as /usr/local/sbin/busboard-wifi-fix.
set -euo pipefail
CONF=/etc/modprobe.d/busboard-brcmfmac.conf
LINE="options brcmfmac feature_disable=0x82000"

[[ "${EUID}" -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }

case "${1:-status}" in
  on)
    printf '# Installed by busboard-wifi-fix. Remove with: sudo busboard-wifi-fix off\n%s\n' "${LINE}" > "${CONF}"
    echo "Written ${CONF}. Reboot to apply: sudo reboot"
    ;;
  off)
    rm -f "${CONF}"
    echo "Removed ${CONF}. Reboot to apply: sudo reboot"
    ;;
  status)
    if [[ -f "${CONF}" ]]; then
      boot=$(( $(date +%s) - $(cut -d. -f1 /proc/uptime) ))
      if (( $(stat -c %Y "${CONF}") < boot )); then
        echo "on (active since this boot)"
      else
        echo "on (written after this boot - reboot to apply)"
      fi
    else
      echo "off"
    fi
    ;;
  *) echo "usage: sudo busboard-wifi-fix on|off|status" >&2; exit 1 ;;
esac
