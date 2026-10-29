"""Generate a new admin password (forgotten password).

Run via ``sudo busboard-reset-password`` (scripts/busboard-reset-password.sh),
which runs this as the busboard user from /opt/busboard. Also used by
``--show``, which prints the current generated password, if one is in use,
for the installer's final message.

The new password is shown on the board until it's changed on /admin/account.
"""

from __future__ import annotations

import sys

from busboard.core.config import load_config, reset_admin_password, save_config


def main() -> int:
    config = load_config()
    if "--show" in sys.argv:
        if config.admin.initial_password:
            print(f"{config.admin.username} {config.admin.initial_password}")
            return 0
        return 1 if config.admin.configured else 2
    config, password = reset_admin_password(config)
    save_config(config)
    print(f"New admin login: {config.admin.username} / {password}")
    print("It's also shown on the board until you change it at /admin/account.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
