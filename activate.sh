#!/bin/bash
# 激活接管：装 udev 规则 + 启动服务（之后拔插 USB 线生效）
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
install -m 644 "$SRC/71-gaomon-penfix.rules" /etc/udev/rules.d/
udevadm control --reload
# 立即对当前实例关闭 USB 自动挂起
for d in /sys/bus/usb/devices/*/idVendor; do
  if [ "$(cat "$d" 2>/dev/null)" = "256c" ]; then
    b=$(dirname "$d")
    echo on > "$b/power/control" 2>/dev/null || true
    echo -1 > "$b/power/autosuspend" 2>/dev/null || true
  fi
done
systemctl enable gaomon-penfix
# 注意：不能只用 enable --now。对**已在运行**的服务 enable --now 是 no-op，
# 升级后它仍在内存里跑旧代码（新装的模块不生效、--web 也不生效）。
# 用 restart 保证无论首次激活还是升级，都加载当前磁盘上的版本。
if systemctl is-active --quiet gaomon-penfix; then
  echo "服务已在运行，重启以加载新版本…"
  systemctl restart gaomon-penfix
else
  systemctl start gaomon-penfix
fi
sleep 2
systemctl --no-pager -l status gaomon-penfix | head -12

# 稳定性核对：曾因 unit 用 python3 执行 sh 启动器而崩溃重启 2616 次，
# 而 systemctl status 只显示 "activating"，看不出是崩溃循环。
# 判据：5 秒后 NRestarts 不再增长，且 MainPID 非 0。
echo
echo "—— 稳定性核对 ——"
r1=$(systemctl show gaomon-penfix -p NRestarts --value 2>/dev/null || echo 0)
sleep 5
r2=$(systemctl show gaomon-penfix -p NRestarts --value 2>/dev/null || echo 0)
pid=$(systemctl show gaomon-penfix -p MainPID --value 2>/dev/null || echo 0)
if [ "$r1" != "$r2" ]; then
  echo "  !! 崩溃重启中：NRestarts $r1 -> $r2（5 秒内又重启了）"
  echo "     最近的错误："
  journalctl -u gaomon-penfix -n 12 --no-pager | sed 's/^/       /'
elif [ "$pid" = "0" ]; then
  echo "  !! 服务没有运行中的主进程（MainPID=0），可能正在启动或已退出"
  journalctl -u gaomon-penfix -n 12 --no-pager | sed 's/^/       /'
else
  echo "  NRestarts 稳定在 $r2，MainPID=$pid ✓"
  echo "  实际执行：$(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null)"
fi

# 核对虚拟节点权限：KWin/Xournal++ 必须以非 root 身份读得到，
# 否则会出现「能悬停、画不出线」。
echo
echo "—— 虚拟笔节点权限核对 ——"
found=0
for d in /sys/class/input/event*/device; do
  n=$(cat "$d/name" 2>/dev/null) || continue
  [ "$n" = "GAOMON 1060PRO Pen (penfix)" ] || continue
  ev="/dev/input/$(basename "$(dirname "$d")")"
  found=1
  mode=$(stat -c '%A %U:%G' "$ev" 2>/dev/null)
  echo "  $ev -> $mode"
  if [ -r "$ev" ]; then
    echo "    当前用户可读 ✓"
  else
    echo "    !! 当前用户不可读 —— KWin 读不到，会表现为「能悬停但画不出线」"
    echo "       请确认 71-gaomon-penfix.rules 已安装并 reload，然后拔插 USB 线"
  fi
done
[ "$found" = 0 ] && echo "  !! 未找到虚拟笔节点，查看 journalctl -u gaomon-penfix"

# 核对 WebUI 端口：--web 是新加的参数，若磁盘版本没更新或端口被占，
# 服务看着 active 但控制台打不开。
echo
echo "—— WebUI 核对 ——"
webport=$(systemctl show gaomon-penfix -p ExecStart --value 2>/dev/null \
          | grep -oP '(?<=--web-port )\d+' | head -1)
webport=${webport:-8848}
if ss -ltn 2>/dev/null | grep -q "127.0.0.1:$webport"; then
  echo "  http://127.0.0.1:$webport/ 已监听 ✓"
  curl -s -m 3 -o /dev/null -w "  HTTP %{http_code}\n" "http://127.0.0.1:$webport/" || true
else
  echo "  !! 端口 $webport 未监听"
  echo "     若 unit 里没有 --web，说明磁盘上的 service 文件是旧的，重跑 install.sh"
  echo "     查看日志：journalctl -u gaomon-penfix -n 20"
fi

echo
echo "✔ 已激活。请现在【拔插一次数位板 USB 线】"
echo "  （udev 规则只对新出现的设备生效），然后打开 Xournal++ 测试："
echo "  画几笔（压感应正常）+ 按 下键/上键。"
echo "  压感曲线与按键映射：http://127.0.0.1:$webport/"
