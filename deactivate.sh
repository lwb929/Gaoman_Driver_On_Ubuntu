#!/bin/bash
# 停用接管，恢复原生 hid-generic 行为（侧键回到修复前状态）
set -e
systemctl disable --now gaomon-penfix 2>/dev/null || true
rm -f /etc/udev/rules.d/71-gaomon-penfix.rules
udevadm control --reload
echo "✔ 已停用。请拔插一次数位板 USB 线恢复原生设备。"
