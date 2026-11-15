#!/bin/bash
# 一键恢复官方 GAOMON 驱动：停用 gaomon-penfix 接管，恢复官方自启动。
# 用法：sudo ./restore-official.sh
set -u

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 sudo 运行： sudo $0" >&2
  exit 1
fi

echo "==> 1/5 停止并禁用 gaomon-penfix 服务"
systemctl disable --now gaomon-penfix 2>/dev/null || true
pkill -f 'gaomon-penfix' 2>/dev/null || true
sleep 1
pkill -9 -f 'gaomon-penfix' 2>/dev/null || true

echo "==> 2/5 删除 udev 屏蔽规则（LIBINPUT_IGNORE_DEVICE / 关自动挂起）"
rm -f /etc/udev/rules.d/71-gaomon-penfix.rules

echo "==> 3/5 重新加载 udev 规则"
udevadm control --reload
# 让 libinput 重新识别原生笔节点：对 input 子系统触发 change
udevadm trigger --action=change --subsystem-match=input 2>/dev/null || true

echo "==> 4/5 恢复官方开机自启动项"
if [ -f /etc/xdg/autostart/gaomontablet.desktop.disabled ]; then
  mv -f /etc/xdg/autostart/gaomontablet.desktop.disabled \
        /etc/xdg/autostart/gaomontablet.desktop
  echo "    已恢复 /etc/xdg/autostart/gaomontablet.desktop"
else
  echo "    自启动项已是启用状态（或不存在），跳过"
fi

echo "==> 5/5 汇总"
echo "--- 残留服务检查（应为空）---"
systemctl is-active gaomon-penfix 2>/dev/null || true
pgrep -a -f gaomon-penfix || true
echo "--- 原生笔节点 ---"
for d in /sys/class/input/event*/device; do
  n=$(cat "$d/name" 2>/dev/null) || continue
  case "$n" in
    *GAOMON*Pen*)
      ev="/dev/input/$(basename "$(dirname "$d")")"
      echo "  $ev  <-- $n"
      ;;
  esac
done

cat <<'EOF'

完成。接下来：
  1) 拔插一次数位板 USB 线（让原生笔节点重新以「未被屏蔽」状态加入）
  2) 启动官方驱动： /usr/lib/gaomontablet/gaomontablet.sh
     （或直接注销重登，自启动项会拉起它）
  3) 托盘会出现 GAOMON 图标，笔即可用。

若想换回 penfix：
  sudo systemctl enable --now gaomon-penfix
  sudo cp /path/to/71-gaomon-penfix.rules /etc/udev/rules.d/ && sudo udevadm control --reload
  sudo mv /etc/xdg/autostart/gaomontablet.desktop /etc/xdg/autostart/gaomontablet.desktop.disabled
EOF
