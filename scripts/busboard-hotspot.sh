#!/usr/bin/env bash
# WPA2 setup hotspot with hostapd, run by busboard-hotspot.service (root).
#
#   busboard-hotspot run      take wlan0 from NetworkManager, start DHCP + hostapd
#   busboard-hotspot cleanup  stop DHCP, hand wlan0 back to NetworkManager
#
# netwatch (unprivileged) requests the hotspot by writing SSID + password to
# /run/busboard/hotspot.env and starting the unit through polkit. This script
# validates those two values and writes the hostapd config itself, so the
# busboard user can never inject other hostapd/dnsmasq options into a root
# process. Installed by install.sh as /usr/local/sbin/busboard-hotspot.
set -euo pipefail

IFACE=wlan0
AP_IP=10.42.0.1
REQUEST=/run/busboard/hotspot.env
RUNDIR=/run/busboard-hotspot
CHANNEL=6

log() { echo "busboard-hotspot: $*"; }

cleanup() {
  if [[ -f "${RUNDIR}/dnsmasq.pid" ]]; then
    kill "$(cat "${RUNDIR}/dnsmasq.pid")" 2>/dev/null || true
    rm -f "${RUNDIR}/dnsmasq.pid"
  fi
  ip addr flush dev "${IFACE}" 2>/dev/null || true
  nmcli device set "${IFACE}" managed yes 2>/dev/null || true
  log "wlan0 handed back to NetworkManager"
}

read_request() {
  [[ -f "${REQUEST}" ]] || { log "no hotspot request at ${REQUEST}"; exit 1; }
  # The file must belong to the busboard service user (or root).
  local owner
  owner="$(stat -c %U "${REQUEST}")"
  [[ "${owner}" == "busboard" || "${owner}" == "root" ]] || { log "refusing request owned by ${owner}"; exit 1; }
  SSID=""; PASSWORD=""; DEBUG=""
  while IFS= read -r line || [[ -n "${line}" ]]; do
    case "${line}" in
      ssid=*) SSID="${line#ssid=}" ;;
      password=*) PASSWORD="${line#password=}" ;;
      debug=1) DEBUG="-d" ;;
    esac
  done < "${REQUEST}"
  local n=${#SSID}
  (( n >= 1 && n <= 32 )) || { log "invalid SSID length"; exit 1; }
  (( ${#PASSWORD} >= 8 && ${#PASSWORD} <= 63 )) || { log "invalid password length"; exit 1; }
  [[ "${PASSWORD}" =~ ^[[:print:]]+$ ]] || { log "password must be printable ASCII"; exit 1; }
}

country() {
  local c
  c="$(iw reg get 2>/dev/null | sed -n 's/^country \([A-Z]\{2\}\):.*/\1/p' | head -n1)"
  echo "${c:-LU}"
}

run() {
  read_request
  umask 077
  mkdir -p "${RUNDIR}"
  # SSID as hex (ssid2=) so any characters are safe in the config file.
  local ssid_hex
  ssid_hex="$(printf '%s' "${SSID}" | od -An -tx1 | tr -d ' \n')"
  cat > "${RUNDIR}/hostapd.conf" <<CONF
interface=${IFACE}
driver=nl80211
ssid2=${ssid_hex}
country_code=$(country)
hw_mode=g
channel=${CHANNEL}
ieee80211n=1
wmm_enabled=1
auth_algs=1
wpa=2
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
wpa_passphrase=${PASSWORD}
CONF

  trap cleanup EXIT
  log "taking ${IFACE} from NetworkManager"
  nmcli device disconnect "${IFACE}" 2>/dev/null || true
  nmcli device set "${IFACE}" managed no
  sleep 1
  ip link set "${IFACE}" up
  ip addr flush dev "${IFACE}"
  ip addr add "${AP_IP}/24" dev "${IFACE}"

  # dnsmasq-base (no system dnsmasq service): DHCP only, no DNS.
  dnsmasq --conf-file=/dev/null --port=0 --interface="${IFACE}" --bind-interfaces \
    --except-interface=lo --dhcp-range=10.42.0.10,10.42.0.254,1h \
    --dhcp-option=option:router,"${AP_IP}" --dhcp-leasefile="${RUNDIR}/leases" \
    --pid-file="${RUNDIR}/dnsmasq.pid" --log-dhcp

  log "starting hostapd on ${IFACE} (channel ${CHANNEL}, WPA2-PSK/CCMP)"
  # -d (test runs only): log every association/handshake step. Doesn't log
  # the passphrase (that would need -K).
  # shellcheck disable=SC2086
  hostapd ${DEBUG} "${RUNDIR}/hostapd.conf"
}

case "${1:-}" in
  run) run ;;
  cleanup) cleanup ;;
  *) echo "usage: busboard-hotspot run|cleanup" >&2; exit 1 ;;
esac
