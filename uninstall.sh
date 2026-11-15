#!/bin/bash
# 完全卸载
set -e
"$(dirname "$0")/deactivate.sh" || true
rm -f /usr/local/bin/gaomon-penfix /usr/local/bin/gaomon-penfix-button-watcher
rm -rf /usr/local/lib/gaomon-penfix
rm -f /etc/systemd/system/gaomon-penfix.service
systemctl daemon-reload
echo "✔ 已卸载"
echo "  配置保留在 /etc/gaomon-penfix/config.json 与 ~/.config/gaomon-penfix/config.json"
echo "  如需一并删除：sudo rm -rf /etc/gaomon-penfix ~/.config/gaomon-penfix"
