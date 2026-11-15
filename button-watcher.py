#!/usr/bin/env python3
"""监听 penfix 虚拟笔节点的按键事件（验证 BTN_STYLUS / BTN_STYLUS2）。"""
import struct, glob, os, sys, select, time

KEY_NAMES = {
    0x140: 'BTN_TOOL_PEN', 0x141: 'BTN_TOOL_RUBBER', 0x14a: 'BTN_TOUCH',
    0x14b: 'BTN_STYLUS', 0x14c: 'BTN_STYLUS2',
}
FMT = 'iiHHi'
dev = None
for d in sorted(glob.glob('/sys/class/input/event*/device')):
    try:
        name = open(d + '/name').read().strip()
    except OSError:
        continue
    if 'penfix' in name:
        dev = '/dev/input/' + d.split('/')[-2]
        print('监听:', dev, '(', name, ')')
        break
if not dev:
    sys.exit('未找到 penfix 虚拟节点——守护进程在跑吗？')
seen = set()
t0 = time.time()
fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
print('现在按 下侧键 / 上侧键 各两次（30 秒）…')
while time.time() - t0 < 30:
    r, _, _ = select.select([fd], [], [], 0.2)
    if not r:
        continue
    data = os.read(fd, 16 * 64)
    for off in range(0, len(data) - 15, 16):
        _, _, etype, code, value = struct.unpack(FMT, data[off:off + 16])
        if etype == 1 and code in KEY_NAMES:
            print(f'{KEY_NAMES[code]} = {value} {"DOWN" if value else "UP"}')
            if value:
                seen.add(KEY_NAMES[code])
print('见到的按键:', sorted(seen) if seen else '无')
