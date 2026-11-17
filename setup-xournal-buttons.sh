#!/bin/bash
# 把 Xournal++ 笔侧键配置写入 settings.xml：
#   stylus  (BTN_STYLUS,  笔下侧键) → eraser      橡皮（按住临时切换，松开还原）
#   stylus2 (BTN_STYLUS2, 笔上侧键) → highlighter 荧光笔
# 前提：Xournal++ 必须已完全退出，否则退出时会覆盖本修改。
set -e
CONF=~/.config/xournalpp/settings.xml

if pgrep -x xournalpp >/dev/null; then
    echo "!! Xournal++ 还在运行。请先保存笔记并完全退出（所有窗口），再重跑本脚本。"
    pgrep -ax xournalpp | head -3
    exit 1
fi

cp "$CONF" "$CONF.bak-penfix-$(date +%s)"
python3 - "$CONF" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()

def set_btn(s, name, tool):
    # 只改该按钮条目的 tool 值，保留其它属性
    pat = re.compile(
        r'(<data name="%s">\s*<attribute name="tool" type="string" value=")[^"]*(")' % name)
    s2, n = pat.subn(r'\g<1>%s\2' % tool, s)
    if n == 0:
        raise SystemExit(f'!! 未找到按钮条目 {name}，格式可能变化')
    return s2

s = set_btn(s, 'stylus',  'eraser')       # 下键 → 橡皮
s = set_btn(s, 'stylus2', 'highlighter')  # 上键 → 荧光笔
open(p, 'w', encoding='utf-8').write(s)
print('已写入：stylus=eraser, stylus2=highlighter')
PY
echo "完成。备份：$(ls ~/.config/xournalpp/settings.xml.bak-penfix-* | tail -1)"
echo "现在启动 Xournal++：按住笔下键=橡皮（松开还原），按住上键=荧光笔。"
