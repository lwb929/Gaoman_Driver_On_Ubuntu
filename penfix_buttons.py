#!/usr/bin/env python3
"""按键映射引擎：把数位板物理键重映射成任意动作。

设计要点
--------
1. 用 EVIOCGRAB 独占板子的键盘节点（实测非 root 可用），这样固件自带的
   字母上报（下侧键→KEY_B、上侧键→KEY_E）不会漏到应用里，同时能拿到
   原始键值做重映射。
2. 动作类型：
     key      —— 键盘组合键（"ctrl+z"、"alt+Tab"）
     mouse    —— 鼠标键（left/right/middle/back/forward）
     cmd      —— 执行命令（可选，默认关闭）
     none     —— 吞掉按键，不产生任何动作
3. 通过一个 uinput 键盘设备注入组合键，通过 uinput 鼠标设备注入鼠标键。
4. 学习模式：等待下一个按下事件，返回其键码，供 WebUI 绑定。
"""

import os
import select
import struct
import time
import fcntl
import ctypes
import threading

_libc = ctypes.CDLL(None, use_errno=True)


def _IOC(d, t, nr, size):
    return (d << 30) | (size << 16) | ((ord(t) if isinstance(t, str) else t) << 8) | nr


EVIOCGRAB = _IOC(1, 'E', 0x90, 4)

EV_SYN, EV_KEY, EV_REL = 0, 1, 2
UI_SET_EVBIT = _IOC(1, 'U', 100, 4)
UI_SET_KEYBIT = _IOC(1, 'U', 101, 4)
UI_SET_RELBIT = _IOC(1, 'U', 102, 4)   # 102 = RELBIT；104 是 MSCBIT（MSC_MAX=0x07）
UI_DEV_SETUP = _IOC(1, 'U', 3, 92)
UI_DEV_CREATE = _IOC(0, 'U', 1, 0)
UI_DEV_DESTROY = _IOC(0, 'U', 2, 0)

REL_X, REL_Y, REL_WHEEL = 0, 1, 8
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE, BTN_EXTRA = 0x110, 0x111, 0x112, 0x113, 0x114

# ------------------------------------------------------------------ 键名表

# 常用键名 -> Linux 键码
KEYCODES = {
    'esc': 1, 'escape': 1, 'tab': 15, 'backspace': 14, 'enter': 28, 'return': 28,
    'space': 57, 'capslock': 58,
    'leftctrl': 29, 'rightctrl': 97, 'ctrl': 29,
    'leftshift': 42, 'rightshift': 54, 'shift': 42,
    'leftalt': 56, 'rightalt': 100, 'alt': 56,
    'leftmeta': 125, 'rightmeta': 126, 'super': 125, 'meta': 125,
    'insert': 110, 'delete': 111, 'home': 102, 'end': 107,
    'pageup': 104, 'pagedown': 109, 'up': 103, 'down': 108,
    'left': 105, 'right': 106,
    'minus': 12, 'equal': 13, 'comma': 51, 'dot': 52, 'slash': 53,
    'semicolon': 39, 'apostrophe': 40, 'grave': 41, 'backslash': 43,
    'leftbrace': 26, 'rightbrace': 27,
    'print': 99, 'scrolllock': 70, 'pause': 119, 'sysrq': 99,
    'playpause': 164, 'nextsong': 163, 'previoussong': 165, 'stopcd': 166,
    'volumeup': 115, 'volumedown': 114, 'mute': 113,
    'brightnessup': 225, 'brightnessdown': 224,
    'www': 150, 'back': 158, 'forward': 159, 'refresh': 173,
    'calc': 140, 'mail': 155, 'homepage': 172,
}
for _c in 'abcdefghijklmnopqrstuvwxyz':
    KEYCODES[_c] = 30 + 'abcdefghijklmnopqrstuvwxyz'.index(_c) \
        if _c not in 'zyxwvutsrqponmlkjihgfedcba' else 0
# 上面映射不准，改用显式表
KEYCODES.update({
    'q': 16, 'w': 17, 'e': 18, 'r': 19, 't': 20, 'y': 21, 'u': 22, 'i': 23,
    'o': 24, 'p': 25, 'a': 30, 's': 31, 'd': 32, 'f': 33, 'g': 34, 'h': 35,
    'j': 36, 'k': 37, 'l': 38, 'z': 44, 'x': 45, 'c': 46, 'v': 47, 'b': 48,
    'n': 49, 'm': 50,
    '1': 2, '2': 3, '3': 4, '4': 5, '5': 6, '6': 7, '7': 8, '8': 9, '9': 10,
    '0': 11,
})
for _i in range(1, 25):
    KEYCODES[f'f{_i}'] = {1: 59, 2: 60, 3: 61, 4: 62, 5: 63, 6: 64, 7: 65, 8: 66,
                          9: 67, 10: 68, 11: 87, 12: 88, 13: 183, 14: 184,
                          15: 185, 16: 186, 17: 187, 18: 188, 19: 189,
                          20: 190, 21: 191, 22: 192, 23: 193, 24: 194}[_i]

# 反向表：键码 -> 可读名（用于学习模式回显）
CODE_NAMES = {}
for _n, _c in KEYCODES.items():
    CODE_NAMES.setdefault(_c, _n)

MOUSE_CODES = {
    'left': BTN_LEFT, 'right': BTN_RIGHT, 'middle': BTN_MIDDLE,
    'back': BTN_SIDE, 'forward': BTN_EXTRA,
}


def parse_combo(combo):
    """'ctrl+shift+z' -> [29, 42, 44]。未知键名抛 ValueError。"""
    parts = [p.strip().lower() for p in str(combo).split('+') if p.strip()]
    if not parts:
        raise ValueError('空组合键')
    codes = []
    for p in parts:
        if p not in KEYCODES:
            raise ValueError(f'未知键名: {p}')
        codes.append(KEYCODES[p])
    return codes


# ------------------------------------------------------------------ uinput

class UInputDev:
    """极简 uinput 设备。fmt 探测方式与 penfix 主程序一致（24B/16B）。"""

    def __init__(self, name, keys=(), rels=(), vendor=0x256C, product=0x2004):
        self.fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
        for ev in (EV_SYN, EV_KEY, EV_REL):
            fcntl.ioctl(self.fd, UI_SET_EVBIT, ev)
        for k in keys:
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, k)
        for r in rels:
            fcntl.ioctl(self.fd, UI_SET_RELBIT, r)
        setup = struct.pack('<HHHH', 0x03, vendor, product, 1)
        setup += name.encode()[:79].ljust(80, b'\0') + struct.pack('<I', 0)
        buf = ctypes.create_string_buffer(setup, len(setup))
        if _libc.ioctl(ctypes.c_int(self.fd), ctypes.c_uint(UI_DEV_SETUP), buf) < 0:
            raise OSError(ctypes.get_errno(), 'UI_DEV_SETUP failed')
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self.fmt = self._probe()

    def _probe(self):
        probe = '<iiHHi'
        try:
            n = os.write(self.fd, struct.pack(probe, 0, 0, EV_SYN, 0, 0) * 2)
        except OSError:
            n = -1
        return probe if n == 32 else '<qqHHi'

    def emit(self, evs):
        buf = b''.join(struct.pack(self.fmt, 0, 0, t, c, v) for t, c, v in evs)
        buf += struct.pack(self.fmt, 0, 0, EV_SYN, 0, 0)
        try:
            os.write(self.fd, buf)
        except OSError:
            pass

    def close(self):
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


# ------------------------------------------------------------------ 映射引擎

class ButtonMapper:
    """独占板子键盘节点 + 重映射。

    线程模型：start() 起一个读线程；stop() 置位并 join。
    """

    def __init__(self, config, log=print, device=None):
        self.cfg = config
        self.log = log
        self.device_override = device   # 显式指定输入节点（测试用）
        self._running = False
        self._thread = None
        self._fd = None
        self._kbd = None
        self._mouse = None
        self._lock = threading.Lock()
        self._held = set()            # 当前按下的源键码
        self._sw = {}                 # switch/dual 动作的交替状态（按 act 的 id 记）
        self._dual_t0 = {}            # dual：按下时刻
        self._dual_pending = {}       # dual：按下时已是橡皮态的挂起标记
        self._learn = None            # 学习模式回调
        self.last_event = None        # 最近一次按键，供状态查询
        self.event_count = 0

    # -------------------------------------------------- 设备定位

    @staticmethod
    def find_tablet_keyboard():
        """找板子的键盘节点（iface2 → 名字 GAOMON ... 且具备大量 KEY 能力）。"""
        import glob
        for d in sorted(glob.glob('/sys/class/input/event*/device')):
            try:
                n = open(d + '/name').read().strip()
            except OSError:
                continue
            if 'penfix' in n:
                continue
            if 'GAOMON' in n and 'Pen' not in n:
                return '/dev/input/' + os.path.basename(os.path.dirname(d))
        return None

    # -------------------------------------------------- 生命周期

    def start(self):
        if self._running:
            return True
        dev = self.device_override or self.find_tablet_keyboard()
        fd = None
        grabbed = False
        if dev:
            try:
                fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as e:
                self.log(f'按键映射：打开 {dev} 失败: {e}')
                fd = None
        if fd is not None and self.cfg.get('suppress_firmware_keys', True):
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 1)
                grabbed = True
            except OSError as e:
                self.log(f'按键映射：EVIOCGRAB 失败（{e}），固件字母会漏出')
        self._fd, self._dev, self._grabbed = fd, dev, grabbed

        # 输出设备
        all_keys = set()
        for act in (self.cfg.get('map') or {}).values():
            if isinstance(act, dict) and act.get('type') == 'key':
                try:
                    all_keys.update(parse_combo(act.get('combo', '')))
                except ValueError:
                    pass
        all_keys.update(MOUSE_CODES.values())
        try:
            self._kbd = UInputDev('GAOMON 1060PRO Keys (penfix)',
                                  keys=sorted(all_keys),
                                  rels=(REL_X, REL_Y, REL_WHEEL))
        except OSError as e:
            self.log(f'按键映射：创建 uinput 设备失败: {e}')
            self._cleanup_fd()
            return False

        self._running = True
        if fd is not None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            self.log(f'按键映射已启动：{dev} (grab={grabbed})，映射 '
                     f'{len(self.cfg.get("map") or {})} 项')
        else:
            # keyboard-less 模式：无板键线程，但仍创建虚拟键盘供笔侧键动作使用
            self.log('按键映射：未找到板子键盘节点（仅笔侧键映射可用）')
        return True

    def trigger(self, name, down):
        """由笔侧键等非键盘源触发的动作入口。name 是 map 里的键（如 pen_b1）。"""
        with self._lock:
            if not self.cfg.get('enabled', True):
                return False
            act = (self.cfg.get('map') or {}).get(name)
        if not act or not isinstance(act, dict):
            return False
        if self._kbd is None:
            self._ensure_kbd()
        if self._kbd is None:
            return False
        self.last_event = {'code': name, 'name': name, 'value': 1 if down else 0,
                           't': time.time()}
        self.event_count += 1
        self._run_action(act, down)
        return True

    def _ensure_kbd(self):
        """懒创建虚拟键盘（trigger 在 mapper 未 start 时也能用）。"""
        all_keys = set()
        for act in (self.cfg.get('map') or {}).values():
            if isinstance(act, dict) and act.get('type') == 'key':
                try:
                    all_keys.update(parse_combo(act.get('combo', '')))
                except ValueError:
                    pass
        all_keys.update(MOUSE_CODES.values())
        try:
            self._kbd = UInputDev('GAOMON 1060PRO Keys (penfix)',
                                  keys=sorted(all_keys),
                                  rels=(REL_X, REL_Y, REL_WHEEL))
        except OSError as e:
            self.log(f'按键映射：创建 uinput 设备失败: {e}')
            self._kbd = None

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        self._cleanup_fd()
        if self._kbd:
            self._kbd.close()
            self._kbd = None

    def _cleanup_fd(self):
        if self._fd is not None:
            try:
                if self.cfg.get('suppress_firmware_keys', True):
                    try:
                        fcntl.ioctl(self._fd, EVIOCGRAB, 0)
                    except OSError:
                        pass
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    # -------------------------------------------------- 事件循环

    def _loop(self):
        while self._running:
            try:
                r, _, _ = select.select([self._fd], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not r:
                continue
            try:
                data = os.read(self._fd, 4096)
            except OSError:
                continue
            if not data:
                continue
            step = 24 if self._kbd and self._kbd.fmt == '<qqHHi' else 16
            fmt = '<qqHHi' if step == 24 else '<iiHHi'
            for off in range(0, len(data) // step * step, step):
                _, _, typ, code, val = struct.unpack(fmt, data[off:off + step])
                if typ != EV_KEY:
                    continue
                self._handle_key(code, val)

    def _handle_key(self, code, val):
        with self._lock:
            self.event_count += 1
            name = CODE_NAMES.get(code, f'code{code}')
            self.last_event = {'code': code, 'name': name, 'value': val,
                               't': time.time()}
            if val:
                self._held.add(code)
            else:
                self._held.discard(code)

            # 学习模式优先
            if self._learn is not None and val:
                cb, self._learn = self._learn, None
                try:
                    cb({'code': code, 'name': name})
                except Exception as e:
                    self.log(f'学习回调异常: {e}')
                return

            if not self.cfg.get('enabled', True):
                return
            act = (self.cfg.get('map') or {}).get(str(code)) \
                or (self.cfg.get('map') or {}).get(name)
            if not act or not isinstance(act, dict):
                return
            self._run_action(act, val)

    def _run_action(self, act, down):
        typ = act.get('type')
        if typ == 'none' or typ is None:
            return
        if typ == 'dual':
            # 短按切换 + 按住临时使用的组合语义：
            #   down 且当前是笔态 → tap combo（切到橡皮）
            #   down 且已是橡皮态 → 暂不动作（等抬起时长判定）
            #   up 短按（<0.3s）且已是橡皮态 → tap combo2（换回笔）
            #   up 长按且本次按下时刚切过去 → tap combo2（按住松开还原）
            if down:
                self._dual_t0[id(act)] = time.time()
                if not self._sw.get(id(act)):
                    self._tap_by(act, 'combo')
                    self._sw[id(act)] = True
                else:
                    self._dual_pending[id(act)] = True
                return
            t0 = self._dual_t0.pop(id(act), None)
            pending = self._dual_pending.pop(id(act), False)
            long_held = t0 is not None and (time.time() - t0) >= 0.3
            if self._sw.get(id(act)):
                if pending and not long_held:
                    self._tap_by(act, 'combo2'); self._sw[id(act)] = False
                elif not pending and long_held:
                    self._tap_by(act, 'combo2'); self._sw[id(act)] = False
                # 其余情形：短按后保持橡皮 / 橡皮态长按保持橡皮
            return
        if typ == 'hold':
            # 按住语义：按下 tap combo（切到目标工具），松开 tap combo2（还原）
            combo = act.get('combo') if down else act.get('combo2')
            if not combo:
                return
            try:
                codes = parse_combo(combo)
            except ValueError as e:
                self.log(f'组合键无效: {combo} ({e})')
                return
            self._tap(codes)
            return
        if typ == 'switch':
            # 交替 tap：每次按下在 combo / combo2 之间切换（按住无重复）
            if not down:
                return
            combo = act.get('combo2') if self._sw.get(id(act)) else act.get('combo')
            self._sw[id(act)] = not self._sw.get(id(act), False)
            try:
                codes = parse_combo(combo or '')
            except ValueError as e:
                self.log(f'组合键无效: {combo} ({e})')
                return
            self._tap(codes)
            return
        if typ == 'key':
            try:
                codes = parse_combo(act.get('combo', ''))
            except ValueError as e:
                self.log(f'组合键无效: {act.get("combo")} ({e})')
                return
            if act.get('toggle'):
                # 切��式：按下发一次完整 tap，按下+抬起
                if down:
                    self._tap(codes)
                return
            if down:
                self._kbd.emit([(EV_KEY, c, 1) for c in codes])
            else:
                self._kbd.emit([(EV_KEY, c, 0) for c in reversed(codes)])
            return
        if typ == 'mouse':
            btn = MOUSE_CODES.get(str(act.get('button', 'left')).lower())
            if btn is None:
                return
            self._kbd.emit([(EV_KEY, btn, 1 if down else 0)])
            return
        if typ == 'wheel':
            if down:
                amount = int(act.get('amount', 1))
                self._kbd.emit([(EV_REL, REL_WHEEL, amount)])
            return
        if typ == 'cmd':
            if down and act.get('command'):
                import subprocess
                try:
                    subprocess.Popen(act['command'], shell=True,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                except Exception as e:
                    self.log(f'命令执行失败: {e}')

    def _tap_by(self, act, key):
        try:
            codes = parse_combo(act.get(key, ''))
        except ValueError as e:
            self.log(f'组合键无效: {act.get(key)} ({e})')
            return
        self._tap(codes)

    def _tap(self, codes):
        """完整按下+抬起，带 30ms 间隔，模拟真实敲击。"""
        def go():
            self._kbd.emit([(EV_KEY, c, 1) for c in codes])
            time.sleep(0.03)
            self._kbd.emit([(EV_KEY, c, 0) for c in reversed(codes)])
        threading.Thread(target=go, daemon=True).start()

    # -------------------------------------------------- 学习模式

    def learn(self, timeout=15.0):
        """阻塞等待下一个按键，返回 {'code','name'}；超时返回 None。"""
        done = threading.Event()
        box = {}

        def cb(info):
            box.update(info)
            done.set()

        with self._lock:
            self._learn = cb
        if not done.wait(timeout):
            with self._lock:
                self._learn = None
            return None
        return box

    def status(self):
        return {
            'running': bool(self._running),
            'device': getattr(self, '_dev', None),
            'grabbed': bool(getattr(self, '_grabbed', False)),
            'event_count': self.event_count,
            'last_event': self.last_event,
            'held': sorted(CODE_NAMES.get(c, f'code{c}') for c in self._held),
        }


if __name__ == '__main__':
    # 自测：键名解析
    assert parse_combo('ctrl+z') == [29, 44]
    assert parse_combo('alt+Tab') == [56, 15]
    assert parse_combo('F13') == [183]
    try:
        parse_combo('nosuchkey')
        raise SystemExit('应当抛异常')
    except ValueError:
        pass
    print('组合键解析 ✓')
    print('  ctrl+shift+z ->', parse_combo('ctrl+shift+z'))
    print('  F14 ->', parse_combo('f14'))

    # 真机 uinput 自测：只测纯逻辑的话，ioctl 常量写错（如 RELBIT 误用 MSCBIT 的 104）
    # 永远不会暴露 —— REL_WHEEL(8) 会报 EINVAL，滚轮动作在真机上静默失效。
    if os.access('/dev/uinput', os.W_OK):
        dev = UInputDev('penfix selftest', keys=(BTN_LEFT,), rels=(REL_WHEEL,))
        dev.emit([(EV_REL, REL_WHEEL, 1)])
        dev.close()
        print('uinput 真机创建 + REL_WHEEL 写入 ✓')
    else:
        print('uinput 真机自测跳过（/dev/uinput 不可写）')

    print('按键映射模块自测通过')
