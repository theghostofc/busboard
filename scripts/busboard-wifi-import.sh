#!/usr/bin/env bash
# Import WiFi credentials from a plain-text file on the SD card's boot
# partition, so a board that can't reach any network can be fixed from any
# laptop (put the SD card in, edit one file, put it back) instead of being
# reformatted.
#
# Installed by install.sh as /usr/local/sbin/busboard-wifi-import (root-owned,
# so the unprivileged busboard user can't change what runs as root) and run at
# boot by busboard-wifi-import.service when the file exists.
#
# File format (/boot/firmware/busboard-wifi.txt), one key=value per line:
#
#   ssid=My Home WiFi
#   password=my wifi password
#   # optional:
#   hidden=yes
#
# The file holds a plaintext password on a FAT partition, so it is deleted
# after a successful import. Blank lines and lines starting with # are ignored;
# Windows (CRLF) line endings are fine.
set -euo pipefail

FILE="${1:-/boot/firmware/busboard-wifi.txt}"

log() { echo "busboard-wifi-import: $*"; }

if [[ ! -f "${FILE}" ]]; then
  log "no ${FILE}, nothing to do"
  exit 0
fi

ssid=""
password=""
hidden="no"
while IFS= read -r line || [[ -n "${line}" ]]; do
  line="${line%$'\r'}"
  [[ -z "${line//[[:space:]]/}" || "${line}" =~ ^[[:space:]]*# ]] && continue
  key="${line%%=*}"
  value="${line#*=}"
  [[ "${line}" != *=* ]] && continue
  # trim whitespace around the key only; values (passwords) are taken verbatim
  key="$(echo -n "${key}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
  case "${key}" in
    ssid) ssid="${value}" ;;
    password|psk) password="${value}" ;;
    hidden) hidden="${value,,}" ;;
    *) log "ignoring unknown key '${key}'" ;;
  esac
done < "${FILE}"

if [[ -z "${ssid}" ]]; then
  log "no ssid= line in ${FILE}; leaving the file in place"
  exit 1
fi
if [[ -n "${password}" && ${#password} -lt 8 ]]; then
  log "password is shorter than 8 characters (WPA2 minimum); leaving the file in place"
  exit 1
fi
if [[ "${ssid}" == "busboard-ap" ]]; then
  log "'busboard-ap' is reserved for the setup hotspot"
  exit 1
fi

hidden_flag="no"
[[ "${hidden}" == "yes" || "${hidden}" == "true" || "${hidden}" == "1" ]] && hidden_flag="yes"

common=(
  connection.autoconnect yes
  connection.autoconnect-retries 0
  connection.autoconnect-priority 10
  802-11-wireless.powersave 2
  802-11-wireless.hidden "${hidden_flag}"
)
if [[ -n "${password}" ]]; then
  security=(wifi-sec.key-mgmt wpa-psk wifi-sec.psk "${password}")
else
  security=(wifi-sec.key-mgmt "")
fi

if nmcli -t -f NAME connection show | grep -Fxq -- "${ssid}"; then
  log "updating existing profile '${ssid}'"
  nmcli connection modify id "${ssid}" 802-11-wireless.ssid "${ssid}" "${common[@]}" "${security[@]}"
else
  log "creating profile '${ssid}'"
  nmcli connection add type wifi ifname wlan0 con-name "${ssid}" ssid "${ssid}" "${common[@]}" "${security[@]}"
fi

# Remove the plaintext credentials from the boot partition.
rm -f -- "${FILE}"
sync
log "imported WiFi '${ssid}' and removed ${FILE}"

# Best effort: connect now (take the setup hotspot down if it's somehow up).
nmcli connection down busboard-ap >/dev/null 2>&1 || true
if nmcli --wait 45 connection up id "${ssid}" >/dev/null 2>&1; then
  log "connected to '${ssid}'"
else
  log "could not connect to '${ssid}' right now; NetworkManager will keep retrying"
fi
exit 0
