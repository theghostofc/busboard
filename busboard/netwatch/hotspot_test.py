"""Start the setup hotspot for a short time, to check it works on this Pi.

Don't run this directly - use ``sudo busboard-hotspot-test`` (see
scripts/busboard-hotspot-test.sh), which pauses netwatch, runs this from
/opt/busboard as the busboard user, and keeps going if SSH drops.

Uses the same profile settings as netwatch, prints the hotspot's name and
password, keeps it up for 2 minutes (or --seconds N), then switches back to
the saved WiFi.
"""

from __future__ import annotations

import argparse
import sys
import time

from busboard.core import network
from busboard.core.config import generate_password, load_config, save_config


def _publish(ap_active: bool, ap_ssid: str = "", ap_ip: str = "", wifi: bool = False, ssid: str = "", secure: bool = False) -> None:
    network.write_status_file(network.NetworkReport(
        status=network.ConnectivityStatus(
            wifi_connected=wifi, internet_reachable=wifi, ap_active=ap_active,
            wifi_ssid=network.ssid_for_profile(ssid) if ssid else "", wifi_profile=ssid,
        ),
        written_at=time.time(),
        ap_ssid=ap_ssid,
        ap_ip=ap_ip,
        note="Hotspot test",
        source="hotspot-test",
        ap_secure=secure,
    ))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=int, default=120)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--open", action="store_true", help="test an open hotspot (NetworkManager)")
    mode.add_argument("--wpa2", action="store_true", help="test a WPA2 hotspot (hostapd)")
    parser.add_argument("--new-password", action="store_true",
                        help="generate and save a new hotspot password first (letters/digits that are hard to confuse)")
    args = parser.parse_args()

    config = load_config()
    if args.new_password:
        config.wifi_ap.password = generate_password(12)
        save_config(config)
        print("Saved a new hotspot password.")
    cfg = config.wifi_ap
    if args.open:
        password = ""
    elif args.wpa2:
        password = cfg.password
    else:
        password = cfg.effective_password  # what netwatch would use
    kind = "WPA2 via hostapd, password: " + password if password else "OPEN via NetworkManager, no password"
    print(f"Starting hotspot '{cfg.ssid}' ({kind}) for {args.seconds}s ...")
    try:
        network.start_fallback_ap(cfg.ssid, password, debug=bool(password))
    except network.NmcliError as exc:
        print(f"FAILED: {exc}")
        print("RESULT: hotspot FAILED")
        print("Kernel messages:   journalctl -k -n 30 --no-pager | grep -i brcm")
        print("NetworkManager:    journalctl -u NetworkManager -n 60 --no-pager")
        return 1

    ap_ip = network.get_device_ipv4("wlan0") or network.DEFAULT_AP_IP
    # netwatch is paused, so tell the board directly (it shows the QR codes
    # on its next refresh instead of after the status file goes stale).
    _publish(ap_active=True, ap_ssid=cfg.ssid, ap_ip=ap_ip, secure=bool(password))
    print(f"Hotspot is ON at http://{ap_ip}:8080/admin")
    print("RESULT: hotspot started")
    print("Join it from a phone now.")
    max_clients = 0
    try:
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            clients = network.ap_client_count()
            max_clients = max(max_clients, clients)
            print(f"  {int(end - time.monotonic()):4d}s left, {clients} device(s) connected", flush=True)
            time.sleep(15)
    except KeyboardInterrupt:
        pass
    print(f"Devices that joined: {max_clients}")
    print("Stopping hotspot and reconnecting to saved WiFi ...")
    network.stop_fallback_ap()
    connected = network.try_saved_networks()
    _publish(ap_active=False, wifi=bool(connected), ssid=connected or "")
    print(f"Reconnected to {connected!r}." if connected else "Saved WiFi didn't connect right away; netwatch will keep trying.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
