"""Entry point for the BusBoard web server.

Runs the Flask app from :mod:`busboard.web.app` behind Waitress, a pure
Python WSGI server with no native dependencies - a good fit for a Pi 3B+
where every extra compiled dependency is friction, and simpler to reason
about here than gunicorn's worker/signal model for a single-user local
setup tool.

Binds on all interfaces (0.0.0.0) since this must be reachable both from
the normal LAN and from the fallback WiFi access point.
"""

from __future__ import annotations

import logging
import os
import signal

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from waitress import serve

    from busboard.web.app import create_app

    app = create_app()
    host = os.environ.get("BUSBOARD_WEB_HOST", DEFAULT_HOST)
    port = int(os.environ.get("BUSBOARD_WEB_PORT", DEFAULT_PORT))
    # systemd stops the service with SIGTERM; turn it into a normal exit so
    # atexit handlers run (the rate limiter flushes its counters there).
    def _on_sigterm(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)

    logger.info("BusBoard setup UI listening on %s:%s", host, port)
    serve(app, host=host, port=port, threads=4)


if __name__ == "__main__":
    run()
