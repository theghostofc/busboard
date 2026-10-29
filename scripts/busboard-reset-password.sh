#!/usr/bin/env bash
# Reset the BusBoard admin password (forgotten password).
#
#   sudo busboard-reset-password            over SSH; prints the new password
#   (boot) /boot/firmware/busboard-reset-password exists
#                                           without SSH: create an empty file
#                                           with that name on the SD card's
#                                           boot partition from any computer.
#
# Either way the new password is shown on the board until it's changed at
# /admin/account. Installed by install.sh as /usr/local/sbin/busboard-reset-password.
set -euo pipefail
BOOT_FLAG=/boot/firmware/busboard-reset-password

[[ "${EUID}" -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }

cd /opt/busboard
runuser -u busboard -- env BUSBOARD_CONFIG_DIR=/etc/busboard BUSBOARD_STATE_DIR=/var/lib/busboard \
  /opt/busboard/.venv/bin/python -m busboard.web.reset_password "$@"

if [[ "${1:-}" == "--from-boot" || -e "${BOOT_FLAG}" ]]; then
  rm -f "${BOOT_FLAG}" && sync
fi
