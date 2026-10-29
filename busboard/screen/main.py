"""Screen power helper: switches the HDMI output off outside the display
schedule and back on when it starts, so the panel goes into standby
instead of showing a black page all night.

Runs as the kiosk user (``busboard-screen.service``) inside cage's Wayland
session and uses ``wlr-randr`` (cage implements the wlr-output-management
protocol). It asks ``busboard-web`` whether the display should be on
(``/api/screen``), because the kiosk user can't read config.yaml.

Fail-safe rules:

- If the web service can't be reached, the screen is switched ON (a lit
  board with a warning beats a dark one nobody knows is broken).
- If switching the output off makes cage exit (some compositor versions
  quit when no output is left), hardware power-off is disabled until the
  next reboot and the board's black overlay is used instead.

Uses only the standard library.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger("busboard.screen")

SCREEN_URL = os.environ.get("BUSBOARD_SCREEN_URL", "http://127.0.0.1:8080/api/screen")
CHECK_EVERY_SECONDS = 20
CRASH_WATCH_SECONDS = 15

_running = True


def _handle_signal(signum, frame) -> None:
    global _running
    _running = False


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))


def disabled_marker() -> Path:
    return runtime_dir() / "busboard-screen-disabled"


def find_wayland_display() -> Optional[str]:
    """cage's socket name (usually wayland-0) in XDG_RUNTIME_DIR."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return os.environ["WAYLAND_DISPLAY"]
    try:
        names = sorted(p.name for p in runtime_dir().glob("wayland-*") if not p.name.endswith(".lock"))
    except OSError:
        return None
    return names[0] if names else None


def parse_outputs(stdout: str) -> dict[str, bool]:
    """Parse ``wlr-randr`` output into {output name: enabled}.

    Output headers start in column 0 (``HDMI-A-1 "Manufacturer ..."``);
    details are indented and include ``Enabled: yes|no``.
    """
    outputs: dict[str, bool] = {}
    current = None
    for line in stdout.splitlines():
        if line and not line[0].isspace():
            current = line.split()[0]
            outputs[current] = True
        elif current and line.strip().lower().startswith("enabled:"):
            outputs[current] = line.split(":", 1)[1].strip().lower() == "yes"
    return outputs


def _wlr_randr(args: list[str], display: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, WAYLAND_DISPLAY=display)
    return subprocess.run(["wlr-randr", *args], capture_output=True, text=True, timeout=15, env=env, check=False)


def display_wanted() -> bool:
    try:
        with urllib.request.urlopen(SCREEN_URL, timeout=5) as resp:
            return bool(json.load(resp).get("display_on", True))
    except (OSError, ValueError):
        return True  # fail safe: keep the board visible


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if not shutil.which("wlr-randr"):
        logger.warning("wlr-randr not installed - screen stays on, the board shows a black page when off")
        _sleep_forever()
        return

    while _running:
        if disabled_marker().exists():
            logger.info("hardware screen power-off disabled for this boot - using the black overlay")
            _sleep_forever()
            return
        display = find_wayland_display()
        if not display:
            _sleep(CHECK_EVERY_SECONDS)
            continue
        result = _wlr_randr([], display)
        outputs = parse_outputs(result.stdout) if result.returncode == 0 else {}
        if not outputs:
            _sleep(CHECK_EVERY_SECONDS)
            continue

        want_on = display_wanted()
        for name, enabled in outputs.items():
            if enabled == want_on:
                continue
            logger.info("turning screen %s (%s)", "on" if want_on else "off", name)
            res = _wlr_randr(["--output", name, "--on" if want_on else "--off"], display)
            if res.returncode != 0:
                logger.warning("wlr-randr failed: %s", res.stderr.strip())
                continue
            if not want_on and not _compositor_survived(display):
                logger.error("cage exited after the output was switched off - disabling screen power-off until reboot")
                try:
                    disabled_marker().touch()
                except OSError:
                    pass
        _sleep(CHECK_EVERY_SECONDS)


def _compositor_survived(display: str) -> bool:
    deadline = time.monotonic() + CRASH_WATCH_SECONDS
    sock = runtime_dir() / display
    while time.monotonic() < deadline:
        if not sock.exists():
            return False
        time.sleep(1)
    return _wlr_randr([], display).returncode == 0


def _sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while _running and time.monotonic() < end:
        time.sleep(1)


def _sleep_forever() -> None:
    while _running:
        time.sleep(5)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(0)
