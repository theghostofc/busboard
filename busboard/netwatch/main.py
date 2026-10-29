"""BusBoard connectivity watchdog.

Runs as its own systemd service, independent from the web/board process.
It is the one thing that must keep working even if everything else about
the board is broken, so it has a single loop and no shared state beyond the
config file, a tiny persisted reboot counter, and the status file it writes
to tmpfs for the board to read.

What it does every ``check_interval_seconds``:

1. Probe the network once (NetworkManager device state + an HTTP probe) and
   publish the result to ``/run/busboard/network_status.json``.
2. Classify the situation:

   - **online** - any uplink (WiFi or Ethernet) with working internet.
     Tear down the fallback AP if it's up.
   - **upstream down** - the Pi is attached to a working local network
     (Ethernet, or WiFi whose gateway answers) but there's no internet.
     That's the router's/ISP's problem; touching WiFi can't fix it, and
     starting the AP would *disconnect* a perfectly good WiFi link, so
     netwatch only reports it and waits.
   - **link broken** - no uplink at all, or WiFi "connected" but the
     gateway doesn't answer (typical of a wedged brcmfmac chip). Run the
     healing ladder below.

3. Healing ladder, measured from when the link broke:

   ======================  ==============================================
   after 60 s              scan and explicitly re-activate saved networks
   after 150 s             turn the WiFi radio off and on, re-activate
   after 240 s             restart NetworkManager
   after grace (300 s)     start the fallback AP (only if no link at all)
   every retry (300 s)     drop the AP (if nobody is using it), try the
                           saved networks again, bring the AP back if not
   after 30 min            reboot (backs off x2 per consecutive reboot,
                           capped at 24 h; never while someone is on the AP)
   ======================  ==============================================

The AP profile is ``autoconnect=no`` and netwatch takes it down at startup,
so after any reboot or restart the saved networks are always tried first.
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from busboard.core import network
from busboard.core.config import (
    Config,
    _atomic_write,
    load_config,
    state_dir,
)

logger = logging.getLogger(__name__)

WIFI_INTERFACE = "wlan0"

STAGE_RECONNECT_AFTER = 60
STAGE_RADIO_AFTER = 150
STAGE_NM_RESTART_AFTER = 240
# Don't yank the AP from under a connected client for longer than this.
AP_CLIENT_PATIENCE_SECONDS = 20 * 60
MAX_REBOOT_BACKOFF_SECONDS = 24 * 3600
HARDEN_PROFILES_EVERY_SECONDS = 30 * 60
# After a failed hotspot start, leave the radio alone this long (for the
# saved networks) before trying again - it used to retry every ~46 s, which
# kept the radio busy and stopped WiFi from ever reconnecting.
AP_FAILURE_BACKOFF_SECONDS = 300
# A hotspot that fails this many times in a row can't be used to recover the
# board, so a reboot is allowed even without a saved network.
AP_FAILURES_BEFORE_REBOOT = 2

STATE_FILENAME = "netwatch_state.json"

_running = True


def _handle_signal(signum, frame) -> None:
    global _running
    _running = False


# ---------------------------------------------------------------------------
# Persisted reboot backoff state
# ---------------------------------------------------------------------------


@dataclass
class RebootState:
    consecutive_reboots: int = 0
    last_reboot_at: float = 0.0  # wall clock


def _state_path() -> Path:
    return state_dir() / STATE_FILENAME


def load_reboot_state(path: Optional[Path] = None) -> RebootState:
    p = path or _state_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return RebootState(
            consecutive_reboots=int(raw.get("consecutive_reboots", 0)),
            last_reboot_at=float(raw.get("last_reboot_at", 0.0)),
        )
    except (OSError, ValueError, TypeError):
        return RebootState()


def save_reboot_state(state: RebootState, path: Optional[Path] = None) -> None:
    p = path or _state_path()
    data = json.dumps({"consecutive_reboots": state.consecutive_reboots, "last_reboot_at": state.last_reboot_at})
    try:
        _atomic_write(p, data.encode("utf-8"), mode=0o600)
    except OSError as exc:
        logger.warning("could not persist netwatch state: %s", exc)


def reboot_delay_seconds(base_seconds: int, consecutive_reboots: int) -> float:
    """How long the link must be broken before the next reboot."""
    return min(base_seconds * (2 ** max(consecutive_reboots, 0)), MAX_REBOOT_BACKOFF_SECONDS)


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------


class Watchdog:
    """One iteration of the watchdog per :meth:`tick`. All side effects go
    through ``net`` (the :mod:`busboard.core.network` module by default) so
    the decision logic can be exercised with a fake."""

    def __init__(
        self,
        config: Config,
        net=network,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        reboot_state_path: Optional[Path] = None,
    ):
        self.config = config
        self.net = net
        self._mono = monotonic
        self._wall = wall
        self._reboot_state_path = reboot_state_path
        self.reboot_state = load_reboot_state(reboot_state_path)

        self.offline_since_wall: Optional[float] = None
        self.broken_since: Optional[float] = None  # monotonic
        self.stages_done: set[str] = set()
        self.ap_active = False
        self.ap_started_at: Optional[float] = None
        self.ap_clients_since: Optional[float] = None
        self.last_harden: Optional[float] = None
        self.note = ""
        self._upstream_logged = False
        self._acted = False
        self.ap_failures = 0
        self.ap_next_try_at: Optional[float] = None
        self.ap_error = ""
        self.saved_count = -1

    # -- helpers -----------------------------------------------------------

    def _saved_networks(self) -> list:
        try:
            saved = self.net.list_saved_wifi_networks()
        except Exception:  # noqa: BLE001
            logger.exception("could not list saved wifi networks")
            return []
        if len(saved) != self.saved_count:
            if self.saved_count > 0 and not saved:
                logger.error("all saved WiFi networks have disappeared - add one at /admin/wifi (via Ethernet or the hotspot)")
            elif self.saved_count == -1 and not saved:
                logger.error("no saved WiFi network - add one at /admin/wifi (via Ethernet or the hotspot)")
            else:
                logger.info("saved WiFi networks: %s", ", ".join(n.ssid for n in saved) or "none")
            self.saved_count = len(saved)
        return saved

    def _harden_profiles(self) -> None:
        for net_ in self._saved_networks():
            try:
                self.net.harden_wifi_profile(net_.name)
            except Exception:  # noqa: BLE001
                logger.exception("could not harden profile %r", net_.name)
        self.last_harden = self._mono()

    def _probe(self) -> "network.ConnectivityStatus":
        try:
            return self.net.get_connectivity_status(WIFI_INTERFACE)
        except Exception:  # noqa: BLE001 - a check failure must not crash the watchdog
            logger.exception("connectivity check failed")
            return self.net.ConnectivityStatus(wifi_connected=False, internet_reachable=False)

    def _stop_ap(self, reason: str) -> None:
        logger.info("stopping fallback AP: %s", reason)
        try:
            self.net.stop_fallback_ap()
        except Exception:  # noqa: BLE001
            logger.exception("failed to stop fallback AP")
        self.ap_active = False
        self.ap_started_at = None
        self.ap_clients_since = None

    def _start_ap(self) -> None:
        now = self._mono()
        if self.ap_next_try_at is not None and now < self.ap_next_try_at:
            return  # backing off after a failure
        cfg = self.config.wifi_ap
        logger.warning("no network link, starting fallback AP %r", cfg.ssid)
        self._acted = True
        try:
            self.net.start_fallback_ap(cfg.ssid, cfg.effective_password, WIFI_INTERFACE)
        except Exception as exc:  # noqa: BLE001
            self.ap_failures += 1
            self.ap_error = str(exc)[:200]
            self.ap_next_try_at = now + AP_FAILURE_BACKOFF_SECONDS
            logger.error(
                "setup hotspot failed to start (%d in a row), next try in %ds: %s",
                self.ap_failures, AP_FAILURE_BACKOFF_SECONDS, exc,
            )
            self.note = "Setup hotspot could not start"
            # Give the saved networks the radio in the meantime.
            if self._saved_networks():
                try:
                    self.net.try_saved_networks(WIFI_INTERFACE)
                except Exception:  # noqa: BLE001
                    logger.exception("reconnect attempt failed")
            return
        self.ap_active = True
        self.ap_started_at = now
        self.ap_clients_since = None
        self.ap_failures = 0
        self.ap_error = ""
        self.ap_next_try_at = None
        self.note = "Setup hotspot active"

    def _publish(self, status) -> None:
        ap_ip = ""
        ap_clients = 0
        if status.ap_active:
            ap_ip = self.net.get_device_ipv4(WIFI_INTERFACE) or self.net.DEFAULT_AP_IP
            ap_clients = self._ap_clients()
        self.net.write_status_file(
            self.net.NetworkReport(
                status=status,
                written_at=self._wall(),
                offline_since=self.offline_since_wall,
                ap_ssid=self.config.wifi_ap.ssid,
                ap_ip=ap_ip,
                ap_clients=ap_clients,
                note=self.note,
                saved_networks=self.saved_count,
                ap_error=self.ap_error,
                ap_secure=bool(status.ap_active and getattr(self.net, "hostapd_active", lambda: False)()),
            )
        )

    def _ap_clients(self) -> int:
        try:
            return self.net.ap_client_count(WIFI_INTERFACE)
        except Exception:  # noqa: BLE001
            return 0

    # -- startup -----------------------------------------------------------

    def startup(self) -> None:
        """Always prefer the saved networks after a (re)start: if the AP is
        up (e.g. netwatch was restarted while it was running), take it down
        and let NetworkManager reconnect normally."""
        if self.net.is_fallback_ap_active():
            self._stop_ap("netwatch (re)started - trying saved networks first")
        self._harden_profiles()

    # -- main decision -----------------------------------------------------

    def classify(self, status) -> str:
        """'online', 'upstream_down' or 'link_broken'."""
        if status.ok:
            return "online"
        if status.ethernet_connected:
            return "upstream_down"
        if status.wifi_connected:
            gw_ok = self.net.gateway_reachable(WIFI_INTERFACE)
            # None = no default route on wlan0 (DHCP not finished / lost):
            # the link is not usable.
            return "upstream_down" if gw_ok else "link_broken"
        return "link_broken"

    def tick(self) -> None:
        now = self._mono()
        if self.last_harden is None or now - self.last_harden >= HARDEN_PROFILES_EVERY_SECONDS:
            self._harden_profiles()

        status = self._probe()
        self.ap_active = status.ap_active
        situation = self.classify(status)

        if situation == "online":
            self._on_online(status)
        else:
            if self.offline_since_wall is None:
                self.offline_since_wall = self._wall()
                logger.warning("connectivity lost (%s)", situation)
            if situation == "upstream_down":
                self._on_upstream_down(status)
            else:
                self._acted = False
                self._on_link_broken(status)
                if self._acted:
                    # The ladder changed something - publish the new state.
                    status = self._probe()
        self._publish(status)

    def _on_online(self, status) -> None:
        if self.offline_since_wall is not None:
            logger.info(
                "connectivity restored after %.0fs (%s)",
                self._wall() - self.offline_since_wall,
                "ethernet" if status.ethernet_connected else f"wifi {status.wifi_ssid!r}",
            )
        if status.ap_active:
            self._stop_ap("connectivity restored")
        self.offline_since_wall = None
        self.broken_since = None
        self.stages_done.clear()
        self.note = ""
        self.ap_failures = 0
        self.ap_error = ""
        self.ap_next_try_at = None
        self._upstream_logged = False
        if self.reboot_state.consecutive_reboots:
            self.reboot_state = RebootState()
            save_reboot_state(self.reboot_state, self._reboot_state_path)

    def _on_upstream_down(self, status) -> None:
        # The local network works; the AP would only make things worse.
        if status.ap_active:
            self._stop_ap("a network link is available")
        self.broken_since = None
        self.stages_done.clear()
        self.note = "Waiting for internet"
        if not self._upstream_logged:
            logger.warning("local network is up but internet is unreachable - waiting (not touching WiFi)")
            self._upstream_logged = True

    def _on_link_broken(self, status) -> None:
        now = self._mono()
        if self.broken_since is None:
            self.broken_since = now
            self.stages_done.clear()
        elapsed = now - self.broken_since
        cfg = self.config.wifi_ap
        saved = self._saved_networks()

        if self.ap_active:
            self._maybe_retry_from_ap(saved)
            # `status` predates the retry; don't act on it this tick (it
            # would restart the AP right after a successful reconnect).
            self._maybe_reboot(elapsed, saved)
            return
        if saved:
            self._run_ladder(elapsed)

        if not self.ap_active and not status.wifi_connected and not status.ethernet_connected:
            # With no saved network there's nothing to wait for.
            ap_after = cfg.grace_period_seconds if saved else min(cfg.grace_period_seconds, 60)
            if elapsed >= ap_after:
                self._start_ap()

        self._maybe_reboot(elapsed, saved)

    def _run_ladder(self, elapsed: float) -> None:
        if elapsed >= STAGE_NM_RESTART_AFTER and "nm" not in self.stages_done:
            self.stages_done.update({"nm", "radio", "reconnect"})
            logger.warning("link still broken after %.0fs - restarting NetworkManager", elapsed)
            self.note = "Restarting network"
            self._acted = True
            try:
                self.net.restart_network_manager()
            except Exception:  # noqa: BLE001
                logger.exception("NetworkManager restart failed")
        elif elapsed >= STAGE_RADIO_AFTER and "radio" not in self.stages_done:
            self.stages_done.update({"radio", "reconnect"})
            logger.warning("link still broken after %.0fs - cycling WiFi radio", elapsed)
            self.note = "Reconnecting (radio reset)"
            self._acted = True
            try:
                self.net.cycle_wifi_radio()
                self.net.try_saved_networks(WIFI_INTERFACE)
            except Exception:  # noqa: BLE001
                logger.exception("radio cycle failed")
        elif elapsed >= STAGE_RECONNECT_AFTER and "reconnect" not in self.stages_done:
            self.stages_done.add("reconnect")
            logger.info("link broken for %.0fs - re-activating saved networks", elapsed)
            self.note = "Reconnecting"
            self._acted = True
            try:
                self.net.try_saved_networks(WIFI_INTERFACE)
            except Exception:  # noqa: BLE001
                logger.exception("reconnect attempt failed")

    def _maybe_retry_from_ap(self, saved: list) -> None:
        now = self._mono()
        if not saved or self.ap_started_at is None:
            if self.ap_started_at is None:
                self.ap_started_at = now
            return
        if now - self.ap_started_at < self.config.wifi_ap.retry_interval_seconds:
            return
        clients = self._ap_clients()
        if clients:
            if self.ap_clients_since is None:
                self.ap_clients_since = now
            if now - self.ap_clients_since < AP_CLIENT_PATIENCE_SECONDS:
                logger.info("%d client(s) on the setup hotspot - postponing retry", clients)
                return
        self.ap_clients_since = None
        logger.info("taking setup hotspot down briefly to retry saved networks")
        self._stop_ap("periodic retry of saved networks")
        connected = None
        try:
            connected = self.net.try_saved_networks(WIFI_INTERFACE)
        except Exception:  # noqa: BLE001
            logger.exception("retry of saved networks failed")
        self.note = "Retried saved networks"
        self._acted = True
        if connected:
            return
        self._start_ap()

    def _maybe_reboot(self, elapsed: float, saved: list) -> None:
        base = self.config.wifi_ap.reboot_after_seconds
        if not base:
            return
        if not saved and self.ap_failures < AP_FAILURES_BEFORE_REBOOT:
            return  # nothing to reconnect to, and the hotspot (if tried) may still work
        needed = reboot_delay_seconds(base, self.reboot_state.consecutive_reboots)
        if elapsed < needed:
            return
        if self.ap_active and self._ap_clients():
            return  # someone is reconfiguring the board right now
        self.reboot_state = RebootState(
            consecutive_reboots=self.reboot_state.consecutive_reboots + 1,
            last_reboot_at=self._wall(),
        )
        save_reboot_state(self.reboot_state, self._reboot_state_path)
        logger.error(
            "network link broken for %.0fs - rebooting (consecutive reboot #%d, next one would wait %.0fs)",
            elapsed,
            self.reboot_state.consecutive_reboots,
            reboot_delay_seconds(base, self.reboot_state.consecutive_reboots),
        )
        self.note = "Rebooting to recover network"
        try:
            self.net.request_reboot()
        except Exception:  # noqa: BLE001
            logger.exception("reboot request failed")


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Passwords (admin, hotspot) are generated by busboard-web only.
    config = load_config()
    watchdog = Watchdog(config)
    watchdog.startup()
    logger.info(
        "netwatch started (check every %ss, hotspot after %ss, retry every %ss, reboot after %ss)",
        config.wifi_ap.check_interval_seconds,
        config.wifi_ap.grace_period_seconds,
        config.wifi_ap.retry_interval_seconds,
        config.wifi_ap.reboot_after_seconds,
    )

    while _running:
        try:
            watchdog.config = load_config()  # pick up SSID/password/timing changes without a restart
        except Exception:  # noqa: BLE001 - keep the last good config
            logger.exception("could not reload config")
        try:
            watchdog.tick()
        except Exception:  # noqa: BLE001 - the watchdog must never die
            logger.exception("watchdog iteration failed")
        deadline = time.monotonic() + watchdog.config.wifi_ap.check_interval_seconds
        while _running and time.monotonic() < deadline:
            time.sleep(1)

    logger.info("netwatch shutting down")


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(0)
