"""Connectivity checks and NetworkManager (nmcli)-based WiFi management.

Targets Raspberry Pi OS Trixie, where NetworkManager is the default network
stack, so all WiFi/Ethernet operations here shell out to ``nmcli`` rather
than touching ``wpa_supplicant``/``dhcpcd`` configuration directly.

This module is deliberately side-effect-heavy (it runs real system commands)
so it's kept thin; logic that can be unit tested without a real Pi (parsing,
decisions) is split into small pure functions.

Key rules (see docs/ARCHITECTURE.md, "Network resilience"):

- The fallback access point (``busboard-ap``) occupies ``wlan0`` while it is
  up - the Pi 3B+ has a single radio. An AP connection is therefore never
  counted as "WiFi connected".
- Ethernet and WiFi are both first-class uplinks. NetworkManager prefers
  Ethernet automatically (lower route metric) whenever a cable is plugged
  in, and falls back to WiFi when it's unplugged.
- Connectivity state is probed once, by ``busboard-netwatch``, and shared
  with the web/board process through a small JSON file on tmpfs
  (:func:`write_status_file` / :class:`ConnectivityMonitor`), so rendering
  the board never blocks on an ``nmcli`` call or an HTTP probe.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import requests

from busboard.core.config import runtime_dir

logger = logging.getLogger(__name__)

# Captive-portal style probe first (must return exactly HTTP 204 - a captive
# portal or a router's "no internet" page answers with a 200/30x instead),
# then the API's own host as a second opinion.
_GENERATE_204_URL = "http://connectivitycheck.gstatic.com/generate_204"
_SECONDARY_CHECK_URL = "https://www.mobiliteit.lu"

AP_CONNECTION_NAME = "busboard-ap"
# NetworkManager's "shared" IPv4 method always hands out 10.42.0.0/24 with
# the device itself on .1, unless ipv4.addresses is set on the profile.
DEFAULT_AP_IP = "10.42.0.1"

WIFI_TYPE = "wifi"
ETHERNET_TYPE = "ethernet"

STATUS_FILENAME = "network_status.json"


class NmcliError(Exception):
    """Raised when an nmcli invocation fails."""


def _run(args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise NmcliError(f"command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        # Don't echo args: some nmcli calls carry a WiFi password.
        raise NmcliError(f"command timed out: {args[0]} {args[1] if len(args) > 1 else ''}") from exc


def nmcli_available() -> bool:
    return shutil.which("nmcli") is not None


# ---------------------------------------------------------------------------
# nmcli terse-output parsing (pure, testable)
# ---------------------------------------------------------------------------

_UNESCAPED_COLON = re.compile(r"(?<!\\):")


def split_terse(line: str) -> list[str]:
    """Split one line of ``nmcli -t`` output into fields.

    nmcli escapes literal ``:`` and ``\\`` inside values with a backslash
    (e.g. an SSID of ``Cafe:5G`` is printed as ``Cafe\\:5G``), so a plain
    ``str.split(":")`` breaks on such names.
    """
    return [p.replace("\\:", ":").replace("\\\\", "\\") for p in _UNESCAPED_COLON.split(line)]


@dataclass(frozen=True)
class DeviceState:
    device: str
    type: str
    state: str
    connection: str

    @property
    def connected(self) -> bool:
        # "connected" or "connected (externally)"; deliberately not
        # "connecting (...)" which is still in progress.
        return self.state.startswith("connected")


def parse_device_status(stdout: str) -> list[DeviceState]:
    """Parse ``nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status``."""
    devices = []
    for line in stdout.splitlines():
        parts = split_terse(line)
        if len(parts) < 4:
            continue
        devices.append(DeviceState(device=parts[0], type=parts[1], state=parts[2], connection=parts[3]))
    return devices


def list_devices() -> list[DeviceState]:
    if not nmcli_available():
        return []
    result = _run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"])
    if result.returncode != 0:
        logger.warning("nmcli device status failed: %s", result.stderr.strip())
        return []
    return parse_device_status(result.stdout)


# ---------------------------------------------------------------------------
# Connectivity status
# ---------------------------------------------------------------------------


@dataclass
class ConnectivityStatus:
    # WiFi *client* connection (the fallback AP never counts as this).
    wifi_connected: bool
    internet_reachable: bool
    ethernet_connected: bool = False
    ap_active: bool = False
    # Network name (SSID) of the WiFi connection - what users recognise.
    wifi_ssid: str = ""
    # NetworkManager profile name (e.g. "netplan-wlan0-Home"); internal only.
    wifi_profile: str = ""

    @property
    def link_up(self) -> bool:
        """Whether the Pi is attached to some network (WiFi or cable),
        regardless of whether that network has internet."""
        return self.wifi_connected or self.ethernet_connected

    @property
    def ok(self) -> bool:
        return self.link_up and self.internet_reachable


def status_from_devices(devices: list[DeviceState], wifi_interface: str = "wlan0") -> ConnectivityStatus:
    """Derive link state from parsed device status (pure, no I/O)."""
    wifi_connected = False
    ethernet_connected = False
    ap_active = False
    wifi_ssid = ""
    for d in devices:
        if d.type == WIFI_TYPE and d.device == wifi_interface and d.connected:
            if d.connection == AP_CONNECTION_NAME:
                ap_active = True
            else:
                wifi_connected = True
                wifi_ssid = d.connection  # profile name; resolved to the SSID later
        elif d.type == ETHERNET_TYPE and d.connected:
            ethernet_connected = True
    return ConnectivityStatus(
        wifi_connected=wifi_connected,
        internet_reachable=False,
        ethernet_connected=ethernet_connected,
        ap_active=ap_active,
        wifi_ssid=wifi_ssid,
        wifi_profile=wifi_ssid,
    )


def check_wifi_connected(interface: str = "wlan0") -> bool:
    """Whether ``interface`` has an active WiFi *client* connection (the
    fallback AP does not count)."""
    return status_from_devices(list_devices(), interface).wifi_connected


def check_internet_reachable(timeout_seconds: int = 5) -> bool:
    """Best-effort check that we actually have working internet, not just an
    associated link (captive portals and routers without an uplink fail)."""
    try:
        resp = requests.get(_GENERATE_204_URL, timeout=timeout_seconds, allow_redirects=False)
        if resp.status_code == 204:
            return True
    except requests.RequestException:
        pass
    try:
        resp = requests.get(_SECONDARY_CHECK_URL, timeout=timeout_seconds, allow_redirects=True)
        return resp.status_code < 400
    except requests.RequestException:
        return False


def get_connectivity_status(interface: str = "wlan0", timeout_seconds: int = 5) -> ConnectivityStatus:
    status = status_from_devices(list_devices(), interface)
    if not status.ap_active and hostapd_active():
        # wlan0 is unmanaged by NetworkManager while hostapd runs it.
        status.ap_active = True
        status.wifi_connected = False
    if status.wifi_connected:
        status.wifi_ssid = ssid_for_profile(status.wifi_profile)
    if status.link_up:
        status.internet_reachable = check_internet_reachable(timeout_seconds)
    return status


# ---------------------------------------------------------------------------
# Gateway reachability (distinguishes "WiFi link broken" from "ISP down")
# ---------------------------------------------------------------------------


def parse_default_gateway(route_table: str, device: str) -> Optional[str]:
    """Return the IPv4 default gateway for ``device`` from the text of
    ``/proc/net/route`` (little-endian hex addresses), or ``None``."""
    for line in route_table.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 3 or cols[0] != device or cols[1] != "00000000":
            continue
        try:
            return socket.inet_ntoa(struct.pack("<L", int(cols[2], 16)))
        except (ValueError, struct.error, OSError):
            return None
    return None


def default_gateway(device: str) -> Optional[str]:
    try:
        return parse_default_gateway(Path("/proc/net/route").read_text(), device)
    except OSError:
        return None


def _arp_entry_complete(ip: str, device: str) -> bool:
    try:
        lines = Path("/proc/net/arp").read_text().splitlines()[1:]
    except OSError:
        return False
    for line in lines:
        cols = line.split()
        # IP address, HW type, Flags, HW address, Mask, Device
        if len(cols) >= 6 and cols[0] == ip and cols[5] == device:
            return cols[2] == "0x2"
    return False


def gateway_reachable(device: str) -> Optional[bool]:
    """Whether the default gateway on ``device`` answers.

    Returns ``None`` if there is no default route on that device (can't
    tell). Tries ``ping`` first; if ping isn't usable (missing, or no
    permission under the service sandbox), falls back to poking the
    gateway with a UDP datagram and checking whether ARP resolved it -
    which proves the local WiFi link passes traffic, without any privilege.
    """
    gw = default_gateway(device)
    if not gw:
        return None
    if shutil.which("ping"):
        try:
            result = subprocess.run(
                ["ping", "-c", "2", "-W", "2", "-I", device, gw],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                return True
            if result.returncode == 1:
                return False
            # returncode 2 = error (e.g. permission) -> fall through to ARP.
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1)
            sock.sendto(b"", (gw, 9))  # discard port; only the ARP matters
    except OSError:
        pass
    for _ in range(6):
        if _arp_entry_complete(gw, device):
            return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# Saved WiFi profiles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SavedWifi:
    name: str  # NetworkManager connection profile name (e.g. "preconfigured")
    ssid: str


def _connection_list() -> list[tuple[str, str]]:
    result = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    if result.returncode != 0:
        return []
    rows = []
    for line in result.stdout.splitlines():
        parts = split_terse(line)
        if len(parts) >= 2:
            rows.append((parts[0], parts[1]))
    return rows


def _connection_names() -> set[str]:
    return {name for name, _ in _connection_list()}


def list_saved_wifi_profiles() -> list[str]:
    """Names of saved WiFi *client* profiles (never the fallback AP)."""
    if not nmcli_available():
        return []
    return [n for n, t in _connection_list() if t == "802-11-wireless" and n != AP_CONNECTION_NAME]


def _get_fields(name: str, fields: str) -> list[str]:
    result = _run(["nmcli", "-t", "-g", fields, "connection", "show", name])
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def list_saved_wifi_networks() -> list[SavedWifi]:
    """Saved WiFi client profiles with their SSIDs. The profile name often
    differs from the SSID (Raspberry Pi Imager names its profile
    "preconfigured"), so the SSID is looked up per profile."""
    networks = []
    for name in list_saved_wifi_profiles():
        values = _get_fields(name, "802-11-wireless.ssid")
        ssid = values[0].strip() if values else ""
        networks.append(SavedWifi(name=name, ssid=ssid or name))
    return networks


_SSID_CACHE: dict[str, str] = {}


def ssid_for_profile(name: str) -> str:
    """SSID of a saved profile (cached; profile names rarely change)."""
    if not name:
        return ""
    if name not in _SSID_CACHE:
        try:
            values = _get_fields(name, "802-11-wireless.ssid")
        except NmcliError:
            values = []
        _SSID_CACHE[name] = (values[0].strip() if values else "") or name
    return _SSID_CACHE[name]


@dataclass(frozen=True)
class VisibleNetwork:
    ssid: str
    signal: int  # 0-100
    secure: bool
    in_use: bool


def parse_wifi_scan(stdout: str) -> list[VisibleNetwork]:
    """Parse ``nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list``:
    one entry per SSID (strongest access point), hidden networks skipped,
    strongest first."""
    best: dict[str, VisibleNetwork] = {}
    for line in stdout.splitlines():
        parts = split_terse(line)
        if len(parts) < 4 or not parts[1] or parts[1] == "--":
            continue
        try:
            signal = int(parts[2])
        except ValueError:
            signal = 0
        net = VisibleNetwork(
            ssid=parts[1],
            signal=signal,
            secure=bool(parts[3].strip()) and parts[3].strip() != "--",
            in_use=parts[0].strip() == "*",
        )
        old = best.get(net.ssid)
        if old is None or net.signal > old.signal or net.in_use:
            best[net.ssid] = VisibleNetwork(net.ssid, max(net.signal, old.signal if old else 0), net.secure, net.in_use or bool(old and old.in_use))
    return sorted(best.values(), key=lambda n: -n.signal)


def scan_networks(interface: str = "wlan0") -> list[VisibleNetwork]:
    """Networks in range. ``--rescan auto`` only rescans if the last scan is
    old, so this usually returns within a second."""
    if not nmcli_available():
        return []
    try:
        result = _run(
            ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
             "ifname", interface, "--rescan", "auto"],
            timeout=30,
        )
    except NmcliError:
        return []
    return parse_wifi_scan(result.stdout) if result.returncode == 0 else []


def forget_wifi_profile(name: str) -> None:
    """Delete a saved WiFi profile. Refuses the setup hotspot's profile."""
    if not name or name == AP_CONNECTION_NAME:
        raise ValueError("that profile can't be removed")
    result = _run(["nmcli", "connection", "delete", "id", name])
    if result.returncode != 0:
        raise NmcliError(f"failed to forget network: {result.stderr.strip()}")
    _SSID_CACHE.pop(name, None)


def parse_wifi_list(stdout: str) -> set[str]:
    """Parse ``nmcli -t -f SSID device wifi list`` into a set of SSIDs."""
    return {split_terse(line)[0] for line in stdout.splitlines() if line.strip() and split_terse(line)[0]}


def visible_ssids(interface: str = "wlan0", rescan: bool = True) -> set[str]:
    """SSIDs currently visible to ``interface``. A fresh scan is only
    possible while the radio isn't busy running the fallback AP."""
    result = _run(
        [
            "nmcli", "-t", "-f", "SSID", "device", "wifi", "list",
            "ifname", interface, "--rescan", "yes" if rescan else "no",
        ],
        timeout=30,
    )
    if result.returncode != 0:
        logger.info("wifi scan failed: %s", result.stderr.strip())
        return set()
    return parse_wifi_list(result.stdout)


KEYFILE_DIR = "/etc/NetworkManager/system-connections/"


def profile_filename(name: str) -> str:
    """Where NetworkManager stores a profile. Native profiles live in
    KEYFILE_DIR; profiles created by Raspberry Pi Imager are generated from
    netplan YAML (/etc/netplan -> /run/NetworkManager/...)."""
    try:
        result = _run(["nmcli", "-t", "-f", "NAME,FILENAME", "connection", "show"])
    except NmcliError:
        return ""
    for line in result.stdout.splitlines():
        parts = split_terse(line)
        if len(parts) >= 2 and parts[0] == name:
            return parts[1]
    return ""


def is_keyfile_profile(name: str) -> bool:
    return profile_filename(name).startswith(KEYFILE_DIR)


def harden_wifi_profile(name: str) -> bool:
    """Make a saved WiFi profile reconnect forever and disable WiFi power
    saving on it (the Pi 3B+'s brcmfmac driver is known to drop links with
    power saving on). Only writes if something actually differs, so calling
    this repeatedly doesn't rewrite NetworkManager's files on the SD card.
    Returns True if the profile was changed.

    Only native (keyfile) profiles are touched. Modifying a netplan-backed
    profile makes NetworkManager rewrite netplan YAML, which is the likely
    way the board's Imager-created WiFi profile vanished after a reboot;
    install.sh migrates those to keyfiles instead.
    """
    if not is_keyfile_profile(name):
        logger.info("not modifying wifi profile %r: it is not a NetworkManager keyfile (netplan-backed)", name)
        return False
    wanted = {
        "connection.autoconnect": "yes",
        "connection.autoconnect-retries": "0",  # 0 = retry forever
        "802-11-wireless.powersave": "2",  # 2 = disable
    }
    current = _get_fields(name, ",".join(wanted))
    current_map = dict(zip(wanted, (v.strip() for v in current)))

    def _normalize(key: str, value: str) -> str:
        # nmcli -g prints enum-ish values as words on some versions.
        aliases = {"disable": "2", "enable": "3", "default": "0", "ignore": "1"}
        return aliases.get(value, value) if key.endswith("powersave") else value

    changes: list[str] = []
    for key, value in wanted.items():
        if _normalize(key, current_map.get(key, "")) != value:
            changes += [key, value]
    if not changes:
        return False
    result = _run(["nmcli", "connection", "modify", name, *changes])
    if result.returncode != 0:
        logger.warning("could not harden wifi profile %r: %s", name, result.stderr.strip())
        return False
    logger.info("hardened wifi profile %r (%s)", name, ", ".join(changes[::2]))
    return True


def add_or_update_wifi_profile(ssid: str, password: str, interface: str = "wlan0") -> None:
    """Create or update a saved WiFi connection profile via nmcli.

    Uses the SSID as the connection profile name. If a profile with that
    name already exists, its password is updated instead of creating a
    duplicate. New profiles retry forever and have WiFi power saving off.
    """
    if not ssid:
        raise ValueError("ssid must not be empty")
    if ssid == AP_CONNECTION_NAME:
        raise ValueError("that name is reserved for the setup hotspot")
    if ssid in _connection_names():
        args = ["nmcli", "connection", "modify", ssid]
        if password:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        else:
            args += ["wifi-sec.key-mgmt", ""]
        result = _run(args)
        if result.returncode != 0:
            raise NmcliError(f"failed to update wifi profile {ssid!r}: {result.stderr.strip()}")
    else:
        args = [
            "nmcli", "connection", "add", "type", "wifi",
            "con-name", ssid, "ifname", interface, "ssid", ssid,
            "connection.autoconnect", "yes",
            "connection.autoconnect-retries", "0",
            "802-11-wireless.powersave", "2",
        ]
        if password:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        result = _run(args)
        if result.returncode != 0:
            raise NmcliError(f"failed to add wifi profile {ssid!r}: {result.stderr.strip()}")
    _SSID_CACHE.pop(ssid, None)
    harden_wifi_profile(ssid)


def activate_wifi_profile(name: str, wait_seconds: int = 45) -> None:
    result = _run(["nmcli", "--wait", str(wait_seconds), "connection", "up", name], timeout=wait_seconds + 10)
    if result.returncode != 0:
        raise NmcliError(f"failed to activate wifi profile {name!r}: {result.stderr.strip()}")


def try_saved_networks(interface: str = "wlan0", wait_seconds: int = 45) -> Optional[str]:
    """Scan, then try to activate each saved WiFi profile whose SSID is in
    range. Returns the profile name that connected, or ``None``."""
    saved = list_saved_wifi_networks()
    if not saved:
        return None
    in_range = visible_ssids(interface, rescan=True)
    candidates = [s for s in saved if s.ssid in in_range] if in_range else saved
    for net in candidates:
        try:
            activate_wifi_profile(net.name, wait_seconds=wait_seconds)
            logger.info("connected to saved wifi profile %r", net.name)
            return net.name
        except NmcliError as exc:
            logger.info("saved wifi profile %r did not connect: %s", net.name, exc)
    return None


# ---------------------------------------------------------------------------
# Self-healing actions (each needs a matching polkit action, see
# systemd/polkit-busboard-nm.rules)
# ---------------------------------------------------------------------------


def cycle_wifi_radio(off_seconds: float = 3.0) -> None:
    """Turn the WiFi radio off and on again; often enough to recover a
    wedged brcmfmac firmware without a reboot."""
    _run(["nmcli", "radio", "wifi", "off"])
    time.sleep(off_seconds)
    result = _run(["nmcli", "radio", "wifi", "on"])
    if result.returncode != 0:
        raise NmcliError(f"failed to re-enable wifi radio: {result.stderr.strip()}")


def restart_network_manager() -> None:
    result = _run(["systemctl", "--no-ask-password", "restart", "NetworkManager.service"], timeout=60)
    if result.returncode != 0:
        raise NmcliError(f"failed to restart NetworkManager: {result.stderr.strip()}")


def request_reboot() -> None:
    result = _run(["systemctl", "--no-ask-password", "reboot"], timeout=30)
    if result.returncode != 0:
        raise NmcliError(f"failed to request reboot: {result.stderr.strip()}")


# ---------------------------------------------------------------------------
# Fallback access point
# ---------------------------------------------------------------------------


# Hotspot settings the Pi 3 WiFi chip (brcmfmac, 2.4 GHz only) can actually
# run. With NetworkManager's defaults (automatic band/channel, PMF
# "optional", WPA/WPA2 mixed ciphers) activation timed out on the Pi with
# "Hotspot network creation took too long" / "supplicant-timeout".
AP_CHANNEL = 6
_AP_RADIO_SETTINGS = {
    "connection.autoconnect": "no",
    "802-11-wireless.mode": "ap",
    "802-11-wireless.band": "bg",
    "802-11-wireless.channel": str(AP_CHANNEL),
    "802-11-wireless.powersave": "2",
    "ipv4.method": "shared",
}
_AP_WPA2_SETTINGS = {
    "802-11-wireless-security.key-mgmt": "wpa-psk",
    "802-11-wireless-security.proto": "rsn",
    "802-11-wireless-security.pairwise": "ccmp",
    "802-11-wireless-security.group": "ccmp",
    "802-11-wireless-security.pmf": "1",  # 1 = disable (not supported in AP mode)
}
_ENUM_ALIASES = {
    "802-11-wireless.powersave": {"disable": "2"},
    "802-11-wireless-security.pmf": {"disable": "1"},
}


def _ap_settings(ssid: str, password: str) -> dict[str, str]:
    wanted = {"802-11-wireless.ssid": ssid, **_AP_RADIO_SETTINGS}
    if password:
        wanted.update(_AP_WPA2_SETTINGS)
        wanted["802-11-wireless-security.psk"] = password
    return wanted


def ensure_ap_profile(ssid: str, password: str, interface: str = "wlan0") -> bool:
    """Create or update the hotspot profile. Writes to disk only when a
    setting differs (it used to be rewritten on every attempt, i.e. every
    46 s while failing). Returns True if anything was written."""
    wanted = _ap_settings(ssid, password)
    if AP_CONNECTION_NAME not in _connection_names():
        args = ["nmcli", "connection", "add", "type", "wifi", "ifname", interface,
                "con-name", AP_CONNECTION_NAME, "ssid", ssid]
        for k, v in wanted.items():
            args += [k, v]
        result = _run(args)
        if result.returncode != 0:
            raise NmcliError(f"failed to create AP profile: {result.stderr.strip()}")
        return True

    # Always read key-mgmt too, so switching to an open hotspot notices
    # that security is still set (it used to be missed: the "open" test
    # silently stayed WPA2).
    keys = [k for k in wanted if k != "802-11-wireless-security.psk"]
    if "802-11-wireless-security.key-mgmt" not in keys:
        keys.append("802-11-wireless-security.key-mgmt")
    current = dict(zip(keys, (v.strip() for v in _run_get_fields(AP_CONNECTION_NAME, keys))))
    current_psk = ""
    if password:
        psk_vals = _run(["nmcli", "-s", "-t", "-g", "802-11-wireless-security.psk", "connection", "show", AP_CONNECTION_NAME])
        current_psk = psk_vals.stdout.strip() if psk_vals.returncode == 0 else ""

    def norm(k: str, v: str) -> str:
        return _ENUM_ALIASES.get(k, {}).get(v, v)

    changes: list[str] = []
    for k, v in wanted.items():
        have = current_psk if k == "802-11-wireless-security.psk" else norm(k, current.get(k, ""))
        if have != v:
            changes += [k, v]
    if not password and current.get("802-11-wireless-security.key-mgmt"):
        changes += ["remove", "802-11-wireless-security"]
    if not changes:
        return False
    if changes[-2] == "remove":
        result = _run(["nmcli", "connection", "modify", AP_CONNECTION_NAME, "remove", "802-11-wireless-security"])
        changes = changes[:-2]
        if result.returncode != 0:
            raise NmcliError(f"failed to configure AP profile: {result.stderr.strip()}")
    if changes:
        result = _run(["nmcli", "connection", "modify", AP_CONNECTION_NAME, *changes])
        if result.returncode != 0:
            raise NmcliError(f"failed to configure AP profile: {result.stderr.strip()}")
    return True


def _run_get_fields(name: str, keys: list[str]) -> list[str]:
    result = _run(["nmcli", "-t", "-g", ",".join(keys), "connection", "show", name])
    return result.stdout.splitlines() if result.returncode == 0 else []


# WPA2 hotspot via hostapd. NetworkManager's WPA2 hotspot starts on the
# Pi 3 but its WiFi chip rejects every phone ("authentication error"); open
# NM hotspots work. hostapd is what Raspberry Pi hotspot projects use for WPA2
# on this chip. busboard-hotspot.service (root) takes wlan0 from
# NetworkManager, runs hostapd + a DHCP server on 10.42.0.1, and hands wlan0
# back when stopped. netwatch only passes it SSID + password, through a small
# file the root helper validates (never a config it would run as-is).
HOSTAPD_UNIT = "busboard-hotspot.service"
HOTSPOT_REQUEST_FILENAME = "hotspot.env"


def hotspot_request_path() -> Path:
    return runtime_dir() / HOTSPOT_REQUEST_FILENAME


def _write_hotspot_request(ssid: str, password: str, debug: bool = False) -> None:
    if not ssid or len(ssid.encode()) > 32 or any(c in ssid for c in "\r\n"):
        raise NmcliError("invalid hotspot name")
    if not (8 <= len(password) <= 63) or not password.isprintable():
        raise NmcliError("hotspot password must be 8-63 printable characters")
    path = hotspot_request_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".hotspot.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"ssid={ssid}\npassword={password}\n" + ("debug=1\n" if debug else ""))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def hostapd_active() -> bool:
    try:
        result = _run(["systemctl", "is-active", "--quiet", HOSTAPD_UNIT], timeout=10)
    except NmcliError:
        return False
    return result.returncode == 0


def _start_hostapd_ap(ssid: str, password: str, wait_seconds: int = 20, debug: bool = False) -> None:
    _write_hotspot_request(ssid, password, debug)
    result = _run(["systemctl", "--no-ask-password", "start", HOSTAPD_UNIT], timeout=40)
    if result.returncode != 0:
        raise NmcliError(f"hostapd hotspot failed to start: {result.stderr.strip()} (see journalctl -u {HOSTAPD_UNIT})")
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not hostapd_active():
            raise NmcliError(f"hostapd hotspot stopped right after starting (see journalctl -u {HOSTAPD_UNIT})")
        info = _run(["iw", "dev", "wlan0", "info"], timeout=10) if shutil.which("iw") else None
        if info is None or "type AP" in info.stdout:
            return
        time.sleep(1)
    raise NmcliError(f"hostapd hotspot did not come up within {wait_seconds}s (see journalctl -u {HOSTAPD_UNIT})")


def start_fallback_ap(ssid: str, password: str, interface: str = "wlan0", debug: bool = False) -> None:
    """Bring up the setup hotspot: WPA2 via hostapd when ``password`` is set,
    otherwise an open hotspot via NetworkManager. Neither comes up on its own
    after a reboot - the saved networks are always tried first."""
    if password:
        stop_nm_ap()
        _start_hostapd_ap(ssid, password, debug=debug)
        return
    if hostapd_active():
        _stop_hostapd_ap()
    ensure_ap_profile(ssid, "", interface)
    result = _run(["nmcli", "--wait", "40", "connection", "up", AP_CONNECTION_NAME], timeout=50)
    if result.returncode != 0:
        raise NmcliError(f"failed to activate AP: {result.stderr.strip()}")


def stop_nm_ap() -> None:
    result = _run(["nmcli", "connection", "down", AP_CONNECTION_NAME])
    if result.returncode != 0 and "not an active connection" not in result.stderr.lower():
        logger.warning("failed to stop fallback AP (may already be down): %s", result.stderr.strip())


def _stop_hostapd_ap() -> None:
    result = _run(["systemctl", "--no-ask-password", "stop", HOSTAPD_UNIT], timeout=40)
    if result.returncode != 0:
        logger.warning("failed to stop %s: %s", HOSTAPD_UNIT, result.stderr.strip())
    try:
        hotspot_request_path().unlink()
    except OSError:
        pass


def stop_fallback_ap() -> None:
    if hostapd_active():
        _stop_hostapd_ap()
    stop_nm_ap()


def is_fallback_ap_active() -> bool:
    if hostapd_active():
        return True
    if not nmcli_available():
        return False
    result = _run(["nmcli", "-t", "-f", "NAME", "connection", "show", "--active"])
    return AP_CONNECTION_NAME in {split_terse(line)[0] for line in result.stdout.splitlines()}


def ap_client_count(interface: str = "wlan0") -> int:
    """Number of devices currently associated with the fallback AP, so the
    watchdog never pulls the AP out from under someone reconfiguring it."""
    if shutil.which("iw"):
        try:
            result = _run(["iw", "dev", interface, "station", "dump"], timeout=10)
            if result.returncode == 0:
                return sum(1 for line in result.stdout.splitlines() if line.startswith("Station "))
        except NmcliError:
            pass
    # Fallback: resolved ARP neighbours on the AP interface.
    try:
        lines = Path("/proc/net/arp").read_text().splitlines()[1:]
    except OSError:
        return 0
    return sum(1 for line in lines if len(line.split()) >= 6 and line.split()[5] == interface and line.split()[2] == "0x2")


def get_device_ipv4(device: str) -> Optional[str]:
    if not nmcli_available():
        return None
    try:
        result = _run(["nmcli", "-t", "-g", "IP4.ADDRESS", "device", "show", device], timeout=10)
    except NmcliError:
        return None
    for line in result.stdout.splitlines():
        addr = line.split("|")[0].strip()
        if addr:
            return addr.split("/")[0]
    return None


def get_local_ip_address() -> Optional[str]:
    """Best-effort local IP address for display on the board.

    Uses the "connect a UDP socket, read the local endpoint" trick - this
    never sends a packet, it only asks the kernel which interface/address
    would be used, so it's instant and needs no ``nmcli`` call. Returns the
    address of the preferred uplink (Ethernet when plugged in, else WiFi).
    """
    for probe_host in ("8.8.8.8", "1.1.1.1"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(1)
                sock.connect((probe_host, 80))
                ip = sock.getsockname()[0]
            if ip and ip != "0.0.0.0":
                return ip
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Shared status file (netwatch writes, web/board reads)
# ---------------------------------------------------------------------------


def status_file_path() -> Path:
    return runtime_dir() / STATUS_FILENAME


@dataclass
class NetworkReport:
    """Everything the board/web UI needs to know about the network, as
    last observed by netwatch."""

    status: ConnectivityStatus
    written_at: float = 0.0
    offline_since: Optional[float] = None  # wall clock, None when online
    ap_ssid: str = ""
    ap_ip: str = ""
    ap_clients: int = 0
    note: str = ""  # what netwatch is currently doing, e.g. "Reconnecting"
    source: str = "netwatch"
    # Saved WiFi client profiles (-1 = unknown). 0 means the board can only
    # come back via Ethernet or the hotspot.
    saved_networks: int = -1
    # Last hotspot start error, empty when it started fine (or wasn't tried).
    ap_error: str = ""
    # True when the running hotspot is the WPA2 one (hostapd), so the board
    # shows the password and a WPA QR code; False for the open hotspot.
    ap_secure: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = asdict(self.status)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "NetworkReport":
        s = d.get("status") or {}
        return cls(
            status=ConnectivityStatus(
                wifi_connected=bool(s.get("wifi_connected")),
                internet_reachable=bool(s.get("internet_reachable")),
                ethernet_connected=bool(s.get("ethernet_connected")),
                ap_active=bool(s.get("ap_active")),
                wifi_ssid=str(s.get("wifi_ssid") or ""),
                wifi_profile=str(s.get("wifi_profile") or ""),
            ),
            written_at=float(d.get("written_at") or 0.0),
            offline_since=(float(d["offline_since"]) if d.get("offline_since") else None),
            ap_ssid=str(d.get("ap_ssid") or ""),
            ap_ip=str(d.get("ap_ip") or ""),
            ap_clients=int(d.get("ap_clients") or 0),
            note=str(d.get("note") or ""),
            source=str(d.get("source") or "netwatch"),
            saved_networks=int(d["saved_networks"]) if d.get("saved_networks") is not None else -1,
            ap_error=str(d.get("ap_error") or ""),
            ap_secure=bool(d.get("ap_secure")),
        )


def write_status_file(report: NetworkReport, path: Optional[Path] = None) -> None:
    p = path or status_file_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
    except OSError as exc:
        logger.warning("could not write network status file %s: %s", p, exc)


def read_status_file(path: Optional[Path] = None) -> Optional[NetworkReport]:
    p = path or status_file_path()
    try:
        return NetworkReport.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


class ConnectivityMonitor:
    """Non-blocking view of network state for the web/board process.

    Normally just reads netwatch's status file (a few hundred bytes on
    tmpfs). If that file is missing or stale - netwatch not running, or
    local development - it falls back to probing by itself in a background
    thread, so callers (e.g. the board route) never wait on nmcli or an
    HTTP probe.
    """

    def __init__(self, stale_after_seconds: float = 90.0, fallback_interval_seconds: float = 30.0):
        self._stale_after = stale_after_seconds
        self._fallback_interval = fallback_interval_seconds
        self._lock = threading.Lock()
        self._fallback: Optional[NetworkReport] = None
        self._fallback_thread: Optional[threading.Thread] = None
        self._fallback_offline_since: Optional[float] = None

    def current(self) -> NetworkReport:
        report = read_status_file()
        if report and (time.time() - report.written_at) <= self._stale_after:
            return report
        self._ensure_fallback_thread()
        with self._lock:
            if self._fallback is not None:
                return self._fallback
        # Nothing observed yet: assume fine rather than flashing a scary
        # warning for the first few seconds after boot.
        return NetworkReport(
            status=ConnectivityStatus(wifi_connected=True, internet_reachable=True),
            written_at=time.time(),
            source="unknown",
        )

    def _ensure_fallback_thread(self) -> None:
        with self._lock:
            if self._fallback_thread and self._fallback_thread.is_alive():
                return
            self._fallback_thread = threading.Thread(target=self._fallback_loop, name="connectivity-fallback", daemon=True)
            self._fallback_thread.start()

    def _fallback_loop(self) -> None:
        while True:
            fresh = read_status_file()
            if fresh and (time.time() - fresh.written_at) <= self._stale_after:
                return  # netwatch is back; stop probing ourselves
            try:
                status = get_connectivity_status()
            except Exception:  # noqa: BLE001
                logger.exception("fallback connectivity check failed")
                status = ConnectivityStatus(wifi_connected=False, internet_reachable=False)
            now = time.time()
            if status.ok:
                self._fallback_offline_since = None
            elif self._fallback_offline_since is None:
                self._fallback_offline_since = now
            with self._lock:
                self._fallback = NetworkReport(
                    status=status,
                    written_at=now,
                    offline_since=self._fallback_offline_since,
                    source="web",
                )
            time.sleep(self._fallback_interval)


def connectivity_warning_message(
    connectivity: ConnectivityStatus,
    ap_ssid: str = "",
    ap_password: str = "",
    ap_ip: str = "",
    saved_networks: int = -1,
    ap_error: str = "",
) -> str | None:
    """Human-readable warning for the board, or None when everything is fine.

    Says *why* there's no network where netwatch knows it (no saved WiFi,
    hotspot failing), so nobody has to SSH in to find out.
    """
    if connectivity.ap_active:
        ssid = ap_ssid or "BusBoard-Setup"
        pw = f' password "{ap_password}"' if ap_password else " (open network)"
        return (
            f'No network. Setup WiFi "{ssid}"{pw}, then open '
            f"http://{ap_ip or DEFAULT_AP_IP}:8080/admin - still retrying saved networks"
        )
    if not connectivity.link_up:
        if saved_networks == 0:
            msg = "No WiFi network saved"
        else:
            msg = "No network connection - reconnecting"
        if ap_error:
            return f"{msg}. Setup hotspot could not start - plug in an Ethernet cable to fix"
        if saved_networks == 0:
            return f"{msg} - starting the setup hotspot, or plug in an Ethernet cable"
        return msg + "..."
    if not connectivity.internet_reachable:
        via = "Ethernet" if connectivity.ethernet_connected else "WiFi"
        return f"Connected via {via} but no internet access - retrying"
    return None
