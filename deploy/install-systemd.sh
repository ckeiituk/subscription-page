#!/bin/sh
set -eu

repository_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ "$(id -u)" != 0 ] || [ "$repository_dir" != /opt/remnawave/subscription-source ]; then
    echo 'Run as root from /opt/remnawave/subscription-source.' >&2
    exit 1
fi
install -d -m 700 /var/lib/remnawave-subscription-update
install -D -m 755 "$repository_dir/scripts/deploy-x-out.py" /usr/local/libexec/remnawave-subscription-update.py
install -m 644 "$repository_dir/deploy/remnawave-subscription-update.service" /etc/systemd/system/
install -m 644 "$repository_dir/deploy/remnawave-subscription-update.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now remnawave-subscription-update.timer
echo 'Timer enabled. Start remnawave-subscription-update.service for an immediate update.'
