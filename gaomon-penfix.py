#!/usr/bin/env python3
"""gaomon-penfix — GAOMON 1060PRO (USB 256c:2004) 笔侧键修复（Linux 用户空间代理）

原理：内核 hid-generic 只按设备"宣传"的 HID 描述符解码，该固件把
上侧键放在笔尖位（bit0），内核永远无法暴露 BTN_STYLUS2。
本守护进程直接读 hidraw 原始报告，用 uinput 合成一只完整虚拟笔
（含 BTN_STYLUS / BTN_STYLUS2），配合 udev 规则屏蔽原节点。

实测原始协议：10 字节帧，report id 0x0a
  byte1    : bit0=笔尖 bit1=下侧键 bit2=上侧键 bit3=?(未知,待观测) bits6,7=在板标志
  bytes2-3 : X (LE, 15bit)
  bytes4-5 : Y (LE)
  bytes6-7 : 压力 (LE)
  bytes8-9 : tilt_x, tilt_y (S8)
"""
import struct, glob, os, sys, time, select, argparse, fcntl, signal, json
import ctypes

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import penfix_config as C
import penfix_buttons as B
import penfix_map as M
import penfix_web as W

_libc = ctypes.CDLL(None, use_errno=True)

VENDOR, PRODUCT = 0x256C, 0x2004
FRAME_ID, FRAME_LEN = 0x0a, 10

# --- ioctl helpers (linux/uinput.h, linux/input.h) -------------------------
def _IOC(d, t, nr, size): return (d << 30) | (size << 16) | (t << 8) | nr
def _IOW(t, nr, size): return _IOC(1, t, nr, size)
def _IOR(t, nr, size): return _IOC(2, t, nr, size)
def _IO(t, nr): return _IOC(0, t, nr, 0)

UI_SET_EVBIT  = _IOW(0x55, 100, 4)
UI_SET_KEYBIT = _IOW(0x55, 101, 4)
UI_SET_ABSBIT = _IOW(0x55, 103, 4)
UI_ABS_SETUP  = _IOW(0x55, 4, 28)     # struct uinput_abs_setup {pad2,u16 code,absinfo24}
UI_DEV_SETUP  = _IOW(0x55, 3, 92)     # struct uinput_setup {input_id8,name80,u32} = 92
UI_DEV_CREATE  = _IO(0x55, 1)
UI_DEV_DESTROY = _IO(0x55, 2)

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
ABS_X, ABS_Y = 0, 1
ABS_PRESSURE, ABS_TILT_X, ABS_TILT_Y = 0x18, 0x1a, 0x1b
SEND_AXES = (ABS_X, ABS_Y, ABS_PRESSURE, ABS_TILT_X, ABS_TILT_Y)
BTN_TOOL_PEN, BTN_TOOL_RUBBER = 0x140, 0x141
BTN_TOUCH, BTN_STYLUS, BTN_STYLUS2 = 0x14a, 0x14b, 0x14c
WANT_KEYS = (BTN_TOOL_PEN, BTN_TOOL_RUBBER, BTN_TOUCH, BTN_STYLUS, BTN_STYLUS2)

log = lambda *a: print(*a, flush=True)

def ioc(fd, req, arg=0):
    return fcntl.ioctl(fd, req, arg)

def find_hidraw():
    for hw in sorted(glob.glob('/sys/class/hidraw/hidraw*')):
        try:
            uev_raw = open(hw + '/device/uevent').read()
        except OSError:
            continue
        hid_id, phys = None, ''
        for ln in uev_raw.splitlines():
            if ln.startswith('HID_ID='):
                hid_id = ln[7:].upper()
            elif ln.startswith('HID_PHYS='):
                phys = ln[9:]
        if not hid_id:
            continue
        try:
            bus, vid, pid = [int(x, 16) for x in hid_id.split(':')]
        except ValueError:
            continue
        if bus != 3 or vid != VENDOR or pid != PRODUCT:
            continue
        if not phys.endswith('/input1'):    # 只接管笔接口（iface 1）
            continue
        return '/dev/' + hw.split('/')[-1]
    return None

VIRT_NAME = 'GAOMON 1060PRO Pen (penfix)'


def find_real_node():
    """找内核原生的笔 evdev 节点。

    必须排除自己创建的虚拟设备（名字同样含 GAOMON/Pen），否则重启时会把
    虚拟设备当成镜像源，导致轴范围自我覆盖、甚至读到自己而打不开。
    """
    for d in sorted(glob.glob('/sys/class/input/event*/device')):
        try:
            n = open(d + '/name').read().strip()
        except OSError:
            continue
        if n == VIRT_NAME:
            continue
        if 'GAOMON' in n and 'Pen' in n:
            return '/dev/input/' + d.split('/')[-2], n
    return None, None

def mirror_axes(real_dev):
    """从真实 evdev 节点读取 ABS 轴范围（EVIOCGBIT/EVIOCGABS，走 ctypes 直调）。"""
    axes = {}
    if not real_dev:
        log('!! 找不到真实 pen evdev 节点，使用兜底范围')
        return {ABS_X: (0, 50800, 0, 0, 200), ABS_Y: (0, 31760, 0, 0, 200),
                ABS_PRESSURE: (0, 16383, 0, 0, 0),
                ABS_TILT_X: (-90, 90, 0, 0, 0), ABS_TILT_Y: (-90, 90, 0, 0, 0)}
    fd = os.open(real_dev, os.O_RDONLY)
    try:
        def ioctl_buf(req, n):
            buf = ctypes.create_string_buffer(n)
            r = _libc.ioctl(fd, ctypes.c_uint(req), buf)
            if r < 0:
                raise OSError(ctypes.get_errno(), f'ioctl {req:#x} failed')
            return buf.raw                       # EVIOCGABS 成功时 ret=0 但数据有效
        bitbuf = ioctl_buf(_IOC(2, 0x45, 0x20 + EV_ABS, 64), 64)   # EVIOCGBIT(EV_ABS)
        for code in SEND_AXES:
            if not (bitbuf[code // 8] >> (code % 8)) & 1:
                continue
            vals = struct.unpack('<6i', ioctl_buf(_IOR(0x45, 0x40 + code, 24), 24)[:24])
            _, mn, mx, fz, fl, res = vals
            if mx <= mn:
                continue
            axes[code] = (mn, mx, fz, fl, res)
    finally:
        os.close(fd)
    return axes

def uinput_create(name, keys, axes):
    fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
    for ev in (EV_SYN, EV_KEY, EV_ABS):
        fcntl.ioctl(fd, UI_SET_EVBIT, ev)
    for k in keys:
        fcntl.ioctl(fd, UI_SET_KEYBIT, k)
    for a in axes:
        fcntl.ioctl(fd, UI_SET_ABSBIT, a)
    for a, (mn, mx, fz, fl, res) in axes.items():     # UI_ABS_SETUP: pad2+code+absinfo
        s = struct.pack('<H2x6i', a, 0, mn, mx, fz, fl, res)  # [code][pad2][absinfo]
        b = ctypes.create_string_buffer(s, len(s))
        if _libc.ioctl(fd, ctypes.c_uint(UI_ABS_SETUP), b) < 0:
            raise OSError(ctypes.get_errno(), 'UI_ABS_SETUP failed')
    setup = struct.pack('<HHHH', 0x03, VENDOR, PRODUCT, 1)
    setup += name.encode()[:79].ljust(80, b'\0') + struct.pack('<I', 0)
    b = ctypes.create_string_buffer(setup, len(setup))
    if _libc.ioctl(fd, ctypes.c_uint(UI_DEV_SETUP), b) < 0:
        raise OSError(ctypes.get_errno(), 'UI_DEV_SETUP failed')
    fcntl.ioctl(fd, UI_DEV_CREATE)
    _fix_node_perms(name)
    return fd


def _fix_node_perms(name, timeout=3.0):
    """uinput 设备挂在 virtual 总线上，udev 的 uaccess 规则不覆盖，默认落成
    root:input 0660 —— 非 root 的 KWin / Xournal++ 读不到，表现为「能侦测到笔
    但画不出线」。设备创建后把它放开成 0666。以 root 运行时才有效。"""
    if os.geteuid() != 0:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        for d in glob.glob('/sys/class/input/event*/device/name'):
            try:
                if open(d).read().strip() != name:
                    continue
            except OSError:
                continue
            node = '/dev/input/' + d.split('/')[-3]
            if not os.path.exists(node):
                continue
            try:
                os.chmod(node, 0o666)
                log(f'已放开 {node} 权限为 0666（供 libinput/KWin 读取）')
            except OSError as e:
                log(f'!! 无法修改 {node} 权限: {e}')
            return
        time.sleep(0.05)

class EventWriter:
    """input_event 写入。

    本内核 struct input_event 为 24 字节（64 位 timeval）。16 字节格式会被内核
    按 24 字节边界重新切分，导致坐标/压力错位 —— 且 os.write 只返回「接受的字
    节数」，短写不抛 OSError，所以不能靠异常探测，必须比对返回字节数。

    探测法：写入 2 条「16 字节格式」记录 = 32 字节。
      · 内核 16B → 32/16=2 条完整 → 返回 32
      · 内核 24B → 32/24=1 条完整，余 8 字节 → 返回 24（短写）
    故以返回 32 判定 16B，返回 24 判定 24B。用 24B 格式探测则两种内核都返回
    48，无法区分（勿改回）。
    """
    def __init__(self, fd):
        self.fd = fd
        probe = '<iiHHi'                       # 16B 格式，用于区分内核记录长度
        try:
            n = os.write(self.fd, struct.pack(probe, 0, 0, EV_SYN, 0, 0) * 2)
        except OSError:
            n = -1
        # 24B 内核返回 24；16B 内核返回 32
        self.fmt = probe if n == 32 else '<qqHHi'
        log(f'内核 input_event 记录长度: {struct.calcsize(self.fmt)} 字节'
            f'（探测写入返回 {n}/32）')
        if n not in (24, 32):
            log('!! 探测结果异常，按 24 字节记录继续（本机 64 位内核默认）')

    def pack(self, evs):
        return b''.join(struct.pack(self.fmt, 0, 0, t, c, v) for t, c, v in evs)

    def write_events(self, evs):
        evs = list(evs) + [(EV_SYN, 0, 0)]
        buf = self.pack(evs)
        n = os.write(self.fd, buf)
        if n != len(buf):
            log(f'!! uinput 短写: {n}/{len(buf)}（格式 {self.fmt}）')
        return n

class PenProxy:
    def __init__(self, no_motion, curve=None, mapfn=None):
        self.no_motion = no_motion
        self.curve = curve          # 压感曲线（None = 原样透传）
        self.mapfn = mapfn          # 多屏映射 (x,y,rng_x,rng_y)->(x,y)；None=透传
        self.w = None
        self.reset_state()

    def reset_state(self):
        self.tool = 0                      # 当前按下的 TOOL 键 (0=无)
        self.touch = 0
        self.b1 = 0
        self.b2 = 0
        self.last_frame = 0.0
        self.unknown = {}

    def start(self, axes):
        keys = set(WANT_KEYS)
        if self.no_motion:
            axes = {}
            keys = {BTN_STYLUS, BTN_STYLUS2}
        self.w = EventWriter(uinput_create(VIRT_NAME, sorted(keys), axes))
        self.axes = axes
        self.limits = {c: (axes[c][0], axes[c][1]) for c in axes}
        # 诊断计数：区分「没读到帧」与「读到但没写出」
        self.n_raw = self.n_board = self.n_write = 0
        log('虚拟设备已创建 (motion=' + ('off' if self.no_motion else 'on') + ')')
        log(f'事件记录格式: {self.w.fmt} ({struct.calcsize(self.w.fmt)} 字节)')

    def stop(self):
        if self.w:
            try:
                ioc(self.w.fd, UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(self.w.fd)
            self.w = None

    def key(self, code, down):
        self.w.write_events([(EV_KEY, code, int(bool(down)))])

    def force_release(self):
        if not self.w:
            return
        evs = []
        if self.touch:
            evs.append((EV_KEY, BTN_TOUCH, 0))
        if self.tool:
            evs.append((EV_KEY, self.tool, 0))
        if self.b1:
            evs.append((EV_KEY, BTN_STYLUS, 0))
        if self.b2:
            evs.append((EV_KEY, BTN_STYLUS2, 0))
        self.touch = self.b1 = self.b2 = 0
        self.tool = 0
        if evs:
            self.w.write_events(evs)

    def handle_frame(self, data):
        self.n_raw += 1
        st = data[1]
        if st & 0xC0 != 0xC0:              # 非在板帧
            return
        self.n_board += 1
        tip, b1, b2, inv = bool(st & 1), bool(st & 2), bool(st & 4), bool(st & 8)
        if inv:
            log('观测到 bit3=1（疑似橡皮头）——暂按 bit3=橡皮处理')
        x = data[2] | (data[3] << 8)
        y = data[4] | (data[5] << 8)
        p = data[6] | (data[7] << 8)
        tx = struct.unpack('<b', data[8:9])[0]
        ty = struct.unpack('<b', data[9:10])[0]
        evs = []
        if not self.no_motion:
            want_tool = BTN_TOOL_RUBBER if inv else BTN_TOOL_PEN
            if self.tool and self.tool != want_tool:
                evs.append((EV_KEY, self.tool, 0))
                self.tool = 0
            if not self.tool:
                self.tool = want_tool
                evs.append((EV_KEY, self.tool, 1))
            want_touch = 1 if (tip and not inv) else 0
            if want_touch != self.touch:
                self.touch = want_touch
                evs.append((EV_KEY, BTN_TOUCH, want_touch))
            lo, hi = self.limits.get(ABS_X, (0, 0x7fff))
            x = max(lo, min(hi, x))
            lo, hi = self.limits.get(ABS_Y, (0, 0x5fff))
            y = max(lo, min(hi, y))
            if self.mapfn is not None:
                x, y = self.mapfn(x, y,
                                  self.limits.get(ABS_X, (0, 0x7fff)),
                                  self.limits.get(ABS_Y, (0, 0x5fff)))
            lo, hi = self.limits.get(ABS_PRESSURE, (0, 0x1fff))
            p = max(lo, min(hi, p))
            if self.curve is not None:
                p = max(lo, min(hi, self.curve(p)))
            lo, hi = self.limits.get(ABS_TILT_X, (-64, 63))
            tx = max(lo, min(hi, tx))
            lo, hi = self.limits.get(ABS_TILT_Y, (-64, 63))
            ty = max(lo, min(hi, ty))
            if self.mapfn is not None:
                lox, hix = self.limits.get(ABS_X, (0, 0x7fff))
                loy, hiy = self.limits.get(ABS_Y, (0, 0x5fff))
                x, y = self.mapfn(x, y, (lox, hix), (loy, hiy))
                x = max(lox, min(hix, int(round(x))))
                y = max(loy, min(hiy, int(round(y))))
            evs += [(EV_ABS, ABS_X, int(x)), (EV_ABS, ABS_Y, int(y)),
                    (EV_ABS, ABS_PRESSURE, int(round(p))),
                    (EV_ABS, ABS_TILT_X, int(round(tx))), (EV_ABS, ABS_TILT_Y, int(round(ty)))]
        if b1 != self.b1 or b2 != self.b2:
            # 侧键映射：配置了 pen_b1/pen_b2 时拦截侧键，转为虚拟键盘动作
            # （默认 ctrl+shift+e / ctrl+shift+h，即 Xournal++ 的工具切换快捷键）
            pmap = getattr(self, 'pen_map', None)
            trigger = getattr(self, 'pen_trigger', None)
            if pmap and trigger:
                if b1 != self.b1 and 'pen_b1' in pmap:
                    trigger('pen_b1', b1)
                if b2 != self.b2 and 'pen_b2' in pmap:
                    trigger('pen_b2', b2)
            else:
                if b1 != self.b1:
                    evs.append((EV_KEY, BTN_STYLUS, int(b1)))
                if b2 != self.b2:
                    evs.append((EV_KEY, BTN_STYLUS2, int(b2)))
            self.b1, self.b2 = b1, b2
        if evs:
            self.w.write_events(evs)
            self.n_write += 1
        self.last_frame = time.time()

    def release_if_gone(self):
        if self.no_motion or not self.tool:
            if self.no_motion and (self.b1 or self.b2):
                return
            return
        if time.time() - self.last_frame > 0.35:
            evs = [(EV_KEY, BTN_TOUCH, 0)] if self.touch else []
            evs += [(EV_KEY, self.tool, 0)]
            if self.b1:
                evs.append((EV_KEY, BTN_STYLUS, 0)); self.b1 = 0
            if self.b2:
                evs.append((EV_KEY, BTN_STYLUS2, 0)); self.b2 = 0
            self.tool = 0
            self.touch = 0
            self.w.write_events(evs)

def selftest():
    """合成帧走 handle_frame 全管线，再用 EVIOCGABS/EVIOCGKEY 读回，
    验证「虚拟设备参数」与「事件是否真正抵达内核」两件事。
    不需要笔在板上，因此可独立判定 EventWriter 是否正常。"""
    real, rname = find_real_node()
    axes = mirror_axes(real) if real else mirror_axes(None)
    log(f'真实 pen 节点: {real} ({rname})')
    log('轴范围: ' + ', '.join(f'{hex(c)}:{v[0]}..{v[1]}(res={v[4]})'
                               for c, v in sorted(axes.items())))

    proxy = PenProxy(no_motion=False)
    proxy.start(axes)
    fd, node = proxy.w.fd, find_own_node()

    def read_abs(code):
        b = ctypes.create_string_buffer(24)
        f = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        try:
            if _libc.ioctl(ctypes.c_int(f), ctypes.c_uint(_IOR(0x45, 0x40 + code, 24)), b) < 0:
                return None
            return struct.unpack('<6i', b.raw)
        finally:
            os.close(f)

    def read_keys():
        f = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
        try:
            b = ctypes.create_string_buffer(96)
            if _libc.ioctl(ctypes.c_int(f), ctypes.c_uint(_IOR(0x45, 0x18, 96)), b) < 0:
                return set()
            return {c for c in (BTN_TOOL_PEN, BTN_TOOL_RUBBER, BTN_TOUCH,
                                BTN_STYLUS, BTN_STYLUS2)
                    if (b.raw[c // 8] >> (c % 8)) & 1}
        finally:
            os.close(f)

    if not node:
        log('!! 找不到自建虚拟设备节点，无法读回（可能需要 root 或在 input 组）')
        proxy.stop()
        return 1

    log(f'虚拟节点: {node}')
    readable = False
    if node:
        try:
            os.close(os.open(node, os.O_RDONLY | os.O_NONBLOCK))
            readable = True
        except OSError as e:
            log(f'（无读权限 {e.strerror}，将仅用写入完整性判定）')
    if readable:
        log('写入前: ' + ', '.join(f'{hex(c)}={read_abs(c)[0] if read_abs(c) else "?"}'
                                  for c in sorted(axes)))

    # 合成帧：byte0=0x0a, byte1=0xC0|flags, X/Y/P/tilt
    def frame(tip=False, b1=False, b2=False, x=12345, y=6789, p=4096, tx=30, ty=-20):
        st = 0xC0 | (1 if tip else 0) | (2 if b1 else 0) | (4 if b2 else 0)
        return bytes([0x0a, st, x & 0xFF, x >> 8, y & 0xFF, y >> 8,
                      p & 0xFF, p >> 8, tx & 0xFF, ty & 0xFF])

    # 首要判据：每条写调用都必须被内核完整接收（短写=格式错）
    short = []
    orig_write = proxy.w.write_events

    def checked_write(evs):
        n = orig_write(evs)
        want = struct.calcsize(proxy.w.fmt) * (len(list(evs)) + 1)
        if n != want:
            short.append((n, want))
        return n
    proxy.w.write_events = checked_write

    for label, f in [('悬停', frame()),
                     ('笔尖按下', frame(tip=True)),
                     ('下侧键', frame(tip=True, b1=True)),
                     ('上侧键', frame(tip=True, b1=True, b2=True))]:
        proxy.handle_frame(f)
    time.sleep(0.3)

    fmt_ok = not short
    log(f'格式 {proxy.w.fmt} ({struct.calcsize(proxy.w.fmt)}B/记录)：'
        f'{"全部写入完整 ✓" if fmt_ok else f"短写 {short} ✗"}')

    ok = fmt_ok
    if readable:
        log('写入后 absinfo:')
        for c, exp in {ABS_X: 12345, ABS_Y: 6789, ABS_PRESSURE: 4096,
                       ABS_TILT_X: 30, ABS_TILT_Y: -20}.items():
            got = read_abs(c)
            v = got[0] if got else None
            good = (v == exp)
            ok &= good
            log(f'  ABS {hex(c):5s} value={v!s:<8} 期望={exp:<8} {"✓" if good else "✗"}')

        pressed = read_keys()
        want = {BTN_TOOL_PEN, BTN_TOUCH, BTN_STYLUS, BTN_STYLUS2}
        keys_ok = want <= pressed
        ok &= keys_ok
        log(f'  按下键: {sorted(hex(x) for x in pressed)} 期望⊇'
            f'{sorted(hex(x) for x in want)} {"✓" if keys_ok else "✗"}')

    log(f'  摄取: raw={proxy.n_raw} board={proxy.n_board} write={proxy.n_write}')

    proxy.stop()
    log('== 自检 ' + ('PASS：uinput 通路完好 ==' if ok else 'FAIL：事件未正确抵达内核 =='))
    return 0 if ok else 1


def find_own_node():
    """找到本进程创建的虚拟笔 evdev 节点（按设备名精确匹配）。"""
    for d in glob.glob('/sys/class/input/event*/device/name'):
        try:
            if open(d).read().strip() == VIRT_NAME:
                return '/dev/input/' + d.split('/')[-3]
        except OSError:
            continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-motion', action='store_true',
                    help='仅转发两个侧键（用于与真实设备并存的验证测试）')
    ap.add_argument('--selftest', action='store_true',
                    help='合成帧走完整管线并读回校验，不需要笔在板上')
    ap.add_argument('--stats', action='store_true',
                    help='每 5 秒打印摄取/写出发送计数，用于定位断点')
    ap.add_argument('--web', action='store_true',
                    help='启动 Web 控制台（默认端口 8848）')
    ap.add_argument('--web-port', type=int, default=8848,
                    help='Web 控制台端口（仅 127.0.0.1）')
    ap.add_argument('--no-buttons', action='store_true',
                    help='不接管物理按键（保留固件原始行为）')
    ap.add_argument('--curve-debug', action='store_true',
                    help='打印压感曲线映射样本后退出')
    ap.add_argument('--config', metavar='PATH',
                    help='使用指定的配置文件（默认按用户/系统两级合并）')
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    # ---- 载入配置
    # --config 同时决定写入目标，否则「用 A 启动、保存却写进 B」，
    # 用户会以为保存失效。
    if args.config:
        os.environ['PENFIX_CONFIG'] = os.path.abspath(args.config)
    cfg = C.load() if not args.config else C.normalize(
        json.load(open(args.config, encoding='utf-8')) if os.path.exists(args.config) else C.DEFAULT_CONFIG)
    preset = cfg['pressure']
    log(f'配置：满压阈值 {preset["max_pressure_pct"]:.0f}%、γ={preset["gamma"]:.2f}、'
        f'死区 {preset["deadzone"]}、控制点 {len(preset["points"])} 个')

    if args.curve_debug:
        curve = C.PressureCurve(preset)
        log('raw -> mapped')
        for r in (0, 500, 1000, 2000, 4000, 8000, 12000, 16383):
            log(f'  {r:6d} -> {curve(r):6d}')
        return 0

    if args.no_motion:
        real, rname = None, None
        axes = {}
        log('=== 测试模式：只创建带侧键的虚拟设备，不接管笔迹 ===')
    else:
        real, rname = find_real_node()
        axes = mirror_axes(real)
        log(f'真实 pen 节点: {real} ({rname})')
        log('镜像轴范围: ' + ', '.join(
            f'{hex(c)}:{v[0]}..{v[1]}' for c, v in sorted(axes.items())))

    raw_max = axes.get(ABS_PRESSURE, (0, 16383))[1] if axes else 16383
    curve = C.PressureCurve(preset, raw_max) if preset['enabled'] else None
    if curve is None:
        log('压感曲线已禁用（原样透传）')

    proxy = PenProxy(no_motion=args.no_motion, curve=curve)
    # 多屏映射：region 裁剪在这里做；屏幕选择写 KWin 配置（见 penfix_map docstring）
    outmap = M.OutputMapper(cfg['map'])
    proxy.mapfn = outmap.map

    def recreate_device():
        """销毁并重建虚拟笔设备。KWin 只在设备出现时读 kcminputrc，
        改映射后必须重建才能让 KWin 按新配置映射。"""
        nonlocal proxy
        proxy.stop()
        proxy = PenProxy(no_motion=args.no_motion, curve=curve,
                         mapfn=outmap.map)
        proxy.pen_map = (cfg['buttons'].get('map') or {}) if mapper else {}
        proxy.pen_trigger = mapper.trigger if mapper else None
        proxy.start(axes)

    # 启动时把配置落到的 kcminputrc 状态对齐（幂等；重启后仍有效）
    if cfg['map']['output'] != '*':
        o = M.pick_output(outmap.status()['outputs'], cfg['map']['output'])
        if o:
            M.write_kcm_output(o['uuid'], cfg['map']['rotate'] // 90)
            log(f'多屏映射：{cfg["map"]["output"]}（KWin Orientation='
                f'{cfg["map"]["rotate"] // 90}），region='
                f'{[round(v, 2) for v in cfg["map"]["region"]]}')
        else:
            log(f'!! 配置的映射目标 {cfg["map"]["output"]} 不在当前输出里')
    elif cfg['map']['region'] != [0.0, 0.0, 1.0, 1.0]:
        log(f'板面区域：{[round(v, 2) for v in cfg["map"]["region"]]}')
    running = True

    def bye(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)

    proxy.start(axes)                      # 虚拟设备只建一次，跨 hidraw 重连保持

    # ---- 物理按键映射
    mapper = None
    if not args.no_buttons and cfg['buttons'].get('enabled', True):
        mapper = B.ButtonMapper(cfg['buttons'], log=log)
        if mapper.start():
            proxy.pen_map = cfg['buttons'].get('map') or {}
            proxy.pen_trigger = mapper.trigger
        else:
            log('按键映射未启动（不影响笔功能）')
            mapper = None

    # ---- Web 控制台
    web = None
    if args.web:
        def get_status():
            st = {
                'pen_connected': (time.time() - getattr(proxy, 'last_frame', 0)) < 1.0,
                'frame_total': getattr(proxy, 'n_raw', 0),
                'board_frames': getattr(proxy, 'n_board', 0),
                'writes': getattr(proxy, 'n_write', 0),
                'event_fmt_bytes': struct.calcsize(proxy.w.fmt) if proxy.w else None,
                'raw_max': raw_max,
                'device_path': getattr(proxy, 'dev_path', None),
                'buttons_grabbed': bool(mapper and mapper.status()['grabbed']),
                'button_event_count': mapper.status()['event_count'] if mapper else 0,
                'button_last': mapper.status()['last_event'] if mapper else None,
                'map': outmap.status(),
            }
            return st

        def reload_cfg(new_cfg):
            """热重载：压感即时生效；按键映射需重建（因为要重新 grab）。"""
            nonlocal curve, mapper
            p = new_cfg['pressure']
            curve = C.PressureCurve(p, raw_max) if p['enabled'] else None
            proxy.curve = curve
            log('已热重载压感曲线')
            # 多屏映射：output/rotate 变化 → 写 KWin 的 kcminputrc + 重建虚拟
            # 设备（KWin 只在设备出现时读配置）；region 变化 → 只换引用即生效。
            outmap.cfg = new_cfg['map']
            nm, om = new_cfg['map'], cfg['map']
            if (nm['output'], nm['rotate']) != (om['output'], om['rotate']):
                if nm['output'] != '*':
                    outs = outmap.status()['outputs']
                    o = M.pick_output(outs, nm['output'])
                    if o:
                        M.write_kcm_output(o['uuid'], nm['rotate'] // 90)
                        recreate_device()
                        log(f'多屏映射：{nm["output"]} 已写入 KWin 配置并重建设备 '
                            f'(Orientation={nm["rotate"] // 90})')
                    else:
                        log(f'!! 找不到输出 {nm["output"]}，映射未改')
                else:
                    M.write_kcm_output(None)
                    recreate_device()
                    log('多屏映射：已清除（KDE 默认），设备已重建')
            cfg['map'] = dict(nm)
            if mapper:
                mapper.stop()
                mapper = None
            if new_cfg['buttons'].get('enabled', True) and not args.no_buttons:
                m = B.ButtonMapper(new_cfg['buttons'], log=log)
                if m.start():
                    mapper = m
                else:
                    mapper = None
            proxy.pen_map = (new_cfg['buttons'].get('map') or {}) if mapper else {}
            proxy.pen_trigger = mapper.trigger if mapper else None
            log('已热重载按键映射')

        W.STATE['get_status'] = get_status
        W.STATE['reload'] = reload_cfg
        W.STATE['learn'] = (lambda t: mapper.learn(t)) if mapper else None
        W.STATE['log'] = log
        web = W.Server('127.0.0.1', args.web_port)
        port = web.start()
        log(f'Web 控制台: http://127.0.0.1:{port}/')

    hfd = None
    next_stats = time.time() + 5
    while running:
        dev = find_hidraw()
        if not dev:
            log('未找到 GAOMON hidraw，2 秒后重试…')
            time.sleep(2)
            continue
        try:
            hfd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
            proxy.dev_path = dev
            log(f'已连接 {dev}，开始转发')
            while running:
                r, _, _ = select.select([hfd], [], [], 0.2)
                if r:
                    data = os.read(hfd, 64)
                    for off in range(0, len(data) - FRAME_LEN + 1, FRAME_LEN):
                        frame = data[off:off + FRAME_LEN]
                        if frame[0] != FRAME_ID:
                            continue
                        proxy.handle_frame(frame)
                proxy.release_if_gone()
                if args.stats and time.time() >= next_stats:
                    next_stats = time.time() + 5
                    log(f'[stats] raw={proxy.n_raw} board={proxy.n_board} '
                        f'write={proxy.n_write}')
        except OSError as e:
            proxy.force_release()
            log(f'连接中断（{e}），1 秒后重连（虚拟设备保持不变）')
        finally:
            if hfd is not None:
                try:
                    os.close(hfd)
                except OSError:
                    pass
                hfd = None
        time.sleep(1)
    proxy.stop()
    if mapper:
        mapper.stop()
    if web:
        web.stop()
    log('退出')

if __name__ == '__main__':
    main()
