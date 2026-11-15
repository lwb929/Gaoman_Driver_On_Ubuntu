#!/bin/bash
# 安装文件（不激活接管，不影响当前使用）
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/usr/local/lib/gaomon-penfix

# 主程序 + 三个配套模块 + WebUI 页面，必须装进同一目录（主程序按 __file__ 找同级模块）
install -d -m 755 "$DEST"
install -m 644 "$SRC/penfix_config.py"  "$DEST/"
install -m 644 "$SRC/penfix_buttons.py" "$DEST/"
install -m 644 "$SRC/penfix_map.py"     "$DEST/"
install -m 644 "$SRC/penfix_web.py"     "$DEST/"
install -m 644 "$SRC/webui.html"        "$DEST/"

# 启动器：把 DEST 加入 sys.path 后执行主程序，避免依赖 cwd
cat > /usr/local/bin/gaomon-penfix <<EOF
#!/bin/sh
exec /usr/bin/python3 $DEST/gaomon-penfix.py "\$@"
EOF
chmod 755 /usr/local/bin/gaomon-penfix
install -m 755 "$SRC/gaomon-penfix.py" "$DEST/gaomon-penfix.py"

install -m 755 "$SRC/button-watcher.py" /usr/local/bin/gaomon-penfix-button-watcher
install -m 644 "$SRC/gaomon-penfix.service" /etc/systemd/system/gaomon-penfix.service
systemctl daemon-reload

# 立即对该设备的当前实例关闭 USB 自动挂起（无需等拔插）
for d in /sys/bus/usb/devices/*/idVendor; do
  if [ "$(cat "$d" 2>/dev/null)" = "256c" ]; then
    b=$(dirname "$d")
    echo on > "$b/power/control" 2>/dev/null || true
    echo -1 > "$b/power/autosuspend" 2>/dev/null || true
  fi
done

# 已安装文件的清单核对：少一个模块就会在导入时崩，提前暴露
echo
echo "—— 文件清单 ——"
for f in gaomon-penfix.py penfix_config.py penfix_buttons.py penfix_map.py penfix_web.py webui.html; do
  if [ -f "$DEST/$f" ]; then
    printf '  ✓ %-20s %s\n' "$f" "$(stat -c '%s 字节' "$DEST/$f")"
  else
    printf '  ✗ %-20s 缺失\n' "$f"
  fi
done

# 安装后自检：验证 input_event 记录长度与 uinput 写入通路。
# 不需要笔放在板上，失败说明环境不匹配，先别激活。
echo
echo "—— 安装自检 ——"
if /usr/local/bin/gaomon-penfix --selftest; then
  echo "自检通过。"
else
  echo "!! 自检失败：请把上面的输出发出来，先不要激活接管。" >&2
fi

echo
echo "—— 模块自检 ——"
/usr/bin/python3 "$DEST/penfix_config.py" 2>&1 | tail -2
/usr/bin/python3 "$DEST/penfix_buttons.py" 2>&1 | tail -2
/usr/bin/python3 "$DEST/penfix_map.py" 2>&1 | tail -1

# unit 与安装布局一致性核对。
# 曾因 unit 写 `python3 /usr/local/bin/gaomon-penfix`（该路径现在是 sh 启动器）
# 而无限重启：python3 解析 shell 脚本 → SyntaxError → restart counter 涨到 2000+。
echo
echo "—— unit 一致性核对 ——"
EXEC=$(grep -m1 '^ExecStart=' /etc/systemd/system/gaomon-penfix.service | cut -d= -f2-)
TARGET=$(echo "$EXEC" | awk '{print $NF}' | sed 's/^--//')
PYFILE=$(echo "$EXEC" | grep -oE '/[^ ]*\.py' | head -1)
if [ -n "$PYFILE" ] && [ -f "$PYFILE" ]; then
  echo "  ExecStart 指向 python 文件: $PYFILE ✓"
  FIRST=$(head -1 "$PYFILE")
  case "$FIRST" in
    '#!'*)
      # shebang 存在时必须指向 python；指向 sh 就说明 unit 调错了目标
      if echo "$FIRST" | grep -qi python; then
        echo "  shebang 指向 python ✓"
      else
        echo "  !! 该文件是 shell 脚本而非 python：$FIRST"
        echo "     交给 python3 会 SyntaxError 并无限重启。"
      fi
      ;;
    *)
      echo "  首行无 shebang（正常 python 模块）✓"
      ;;
  esac
else
  echo "  !! ExecStart 里找不到可执行的 .py 路径"
  echo "     ExecStart=$EXEC"
fi

# 语法预检：用 compile() 而不是 py_compile —— 后者会往 __pycache__ 写字节码，
# 该目录属 root，以非 root 重跑 install 时会 Permission denied 造成假失败。
if [ -z "$PYFILE" ]; then
  echo "  跳过语法预检（ExecStart 里没有 .py）"
elif /usr/bin/python3 -c "import sys;compile(open(sys.argv[1]).read(), sys.argv[1], 'exec')" \
       "$PYFILE" 2>/dev/null; then
  echo "  语法预检 ✓"
else
  echo "  !! 语法预检失败，服务会崩溃重启。先修好再 activate。"
fi

# 关键提示：install 只换磁盘文件，不会重启动已在运行的服务。
# 已在运行时必须重跑 activate.sh（它会 restart），否则进程里还是旧代码。
if systemctl is-active --quiet gaomon-penfix 2>/dev/null; then
  echo
  echo "!! 服务当前正在运行。本次只更新了磁盘文件，进程里仍是旧代码。"
  echo "   必须执行下一步 activate.sh 来重启，否则新功能（--web、压感、按键）不会生效。"
fi

cat <<'EOF'

—— 侧键验证测试（可选，先跑这个最稳妥）——
  终端1:  sudo /usr/local/bin/gaomon-penfix --no-motion
  终端2:  sudo /usr/local/bin/gaomon-penfix-button-watcher
  在板上按 下键/上键 各两次，终端2 应出现 BTN_STYLUS / BTN_STYLUS2
  测完在终端1 Ctrl+C 退出。

—— 正式激活（接管笔设备）——
  sudo bash activate.sh   然后拔插一次数位板 USB 线
  装好后浏览器打开 http://127.0.0.1:8848/ 调整压感曲线与按键映射
EOF
