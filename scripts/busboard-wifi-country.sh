#!/usr/bin/env bash
# Apply the WiFi country (regulatory domain) chosen on the admin WiFi
# page (/admin/wifi) to the WiFi chip.
#
# Installed by install.sh as /usr/local/sbin/busboard-wifi-country (root-owned)
# and run by busboard-wifi-country.service: at boot, and whenever
# /etc/busboard/config.yaml changes (busboard-wifi-country.path). Setting the
# regulatory domain needs root, which the busboard services don't have.
#
# Writes to the SD card only when the country actually changes.
set -euo pipefail

CONFIG="${1:-/etc/busboard/config.yaml}"
FALLBACK="${2:-LU}"

log() { echo "busboard-wifi-country: $*"; }

country=""
if [[ -r "${CONFIG}" ]]; then
  # config.yaml is written by yaml.safe_dump: top-level "wifi_country: LU"
  country="$(sed -n 's/^wifi_country:[[:space:]]*["'\'']\{0,1\}\([A-Za-z][A-Za-z]\)["'\'']\{0,1\}[[:space:]]*$/\1/p' "${CONFIG}" | head -n1)"
fi
country="${country:-${FALLBACK}}"
country="${country^^}"
if [[ ! "${country}" =~ ^[A-Z]{2}$ ]]; then
  log "invalid country '${country}', ignoring"
  exit 1
fi

# 1. Apply now (no reboot needed).
current="$(iw reg get 2>/dev/null | sed -n 's/^country \([A-Z0-9]\{2\}\):.*/\1/p' | head -n1 || true)"
if [[ "${current}" != "${country}" ]]; then
  iw reg set "${country}" && log "regulatory domain set to ${country} (was ${current:-unset})"
fi

# 2. Persist for next boots, only if it differs (avoids SD card writes).
CMDLINE=/boot/firmware/cmdline.txt
if [[ -f "${CMDLINE}" ]] && ! grep -q "cfg80211.ieee80211_regdom=${country}\b" "${CMDLINE}"; then
  if command -v raspi-config >/dev/null 2>&1; then
    if raspi-config nonint do_wifi_country "${country}" >/dev/null 2>&1; then
      log "persisted ${country} via raspi-config"
    else
      log "raspi-config could not persist ${country}"
    fi
  else
    sed -i -E "s/[[:space:]]*cfg80211\.ieee80211_regdom=[A-Za-z0-9]{2}//; s/$/ cfg80211.ieee80211_regdom=${country}/" "${CMDLINE}"
    log "persisted ${country} in ${CMDLINE}"
  fi
fi
exit 0
