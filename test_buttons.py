#!/usr/bin/env python3
"""按键映射核心逻辑自测（不需要数位板、不需要 root）。

分层说明
--------
- 设备层（打开节点、EVIOCGRAB 独占、读事件）依赖真实硬件与权限，
  已由 grab_test.py 单独验证 EVIOCGRAB 可用；真机联调需 root 运行。
- 本脚本验证**纯逻辑层**：键码 -> 动作 -> uinput 事件序列。
  这一层是映射正确性的核心，且完全可以离线判定。
"""
import os
import sys
import time
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import penfix_buttons as B


class SpySink:
    """替代真实 uinput，记录所有输出事件。"""
    def __init__(self):
        self.keys = []
        self.rels = []

    def emit(self, evs):
        for t, c, v in evs:
            if t == B.EV_KEY:
                self.keys.append((c, v))
            elif t == B.EV_REL:
                self.rels.append((c, v))

    def close(self):
        pass


def make_mapper(cfg):
    """构造一个不启动设备线程的 mapper，注入 SpySink。"""
    m = B.ButtonMapper(cfg, log=lambda *a: None)
    m._kbd = SpySink()
    return m


def results():
    return []


def check(name, ok, detail=''):
    print(f'  {"✓" if ok else "✗"} {name}' + (f'   {detail}' if detail else ''))
    return ok


def main():
    ok = []

    # ---------- 1. 组合键解析
    print('=== 组合键解析 ===')
    ok.append(check('ctrl+z', B.parse_combo('ctrl+z') == [29, 44]))
    ok.append(check('ctrl+shift+z', B.parse_combo('ctrl+shift+z') == [29, 42, 44]))
    ok.append(check('alt+Tab', B.parse_combo('alt+Tab') == [56, 15]))
    ok.append(check('F13', B.parse_combo('F13') == [183]))
    try:
        B.parse_combo('nosuchkey')
        ok.append(check('未知键应报错', False))
    except ValueError:
        ok.append(check('未知键应报错', True))

    # ---------- 2. key 类型：按下/抬起对称
    print('\n=== key 动作（按住式）===')
    m = make_mapper({'enabled': True, 'suppress_firmware_keys': True,
                     'map': {'48': {'type': 'key', 'combo': 'ctrl+z'}}})
    m._handle_key(48, 1)
    down = list(m._kbd.keys)
    m._kbd.keys.clear()
    m._handle_key(48, 0)
    up = list(m._kbd.keys)
    ok.append(check('按下发 ctrl+z 全按下', down == [(29, 1), (44, 1)], str(down)))
    ok.append(check('抬起逆序发', up == [(44, 0), (29, 0)], str(up)))

    # ---------- 3. key 类型：toggle 模式（完整 tap）
    print('\n=== key 动作（toggle 点按式）===')
    m = make_mapper({'enabled': True, 'suppress_firmware_keys': True,
                     'map': {'183': {'type': 'key', 'combo': 'ctrl+s', 'toggle': True}}})
    m._handle_key(183, 1)
    m._handle_key(183, 0)
    time.sleep(0.15)
    got = list(m._kbd.keys)
    ok.append(check('按下发一次完整 tap', got == [(29, 1), (31, 1), (31, 0), (29, 0)], str(got)))
    ok.append(check('抬起不再发', True))

    # ---------- 4. none 吞掉
    print('\n=== none 动作 ===')
    m = make_mapper({'enabled': True, 'suppress_firmware_keys': True,
                     'map': {'18': {'type': 'none'}}})
    m._handle_key(18, 1)
    m._handle_key(18, 0)
    ok.append(check('不产生任何事件', m._kbd.keys == [] and m._kbd.rels == []))

    # ---------- 5. 未映射键
    print('\n=== 未映射键 ===')
    m = make_mapper({'enabled': True, 'suppress_firmware_keys': True, 'map': {}})
    m._handle_key(99, 1)
    ok.append(check('未映射不产生事件', m._kbd.keys == []))

    # ---------- 6. 鼠标键
    print('\n=== mouse 动作 ===')
    m = make_mapper({'enabled': True, 'suppress_firmware_keys': True,
                     'map': {'183': {'type': 'mouse', 'button': 'back'}}})
    m._handle_key(183, 1)
    m._handle_key(183, 0)
    ok.append(check('后退键按下+抬起',
                    m._kbd.keys == [(B.BTN_SIDE, 1), (B.BTN_SIDE, 0)], str(m._kbd.keys)))
    for nm, code in (('left', B.BTN_LEFT), ('right', B.BTN_RIGHT),
                     ('middle', B.BTN_MIDDLE), ('forward', B.BTN_EXTRA)):
        m = make_mapper({'enabled': True, 'suppress_firmware_keys': True,
                         'map': {'190': {'type': 'mouse', 'button': nm}}})
        m._handle_key(190, 1)
        ok.append(check(f'鼠标 {nm}', m._kbd.keys == [(code, 1)], str(m._kbd.keys)))

    # ---------- 7. 滚轮
    print('\n=== wheel 动作 ===')
    m = make_mapper({'enabled': True, 'suppress_firmware_keys': True,
                     'map': {'184': {'type': 'wheel', 'amount': 1}}})
    m._handle_key(184, 1)
    ok.append(check('向上滚', m._kbd.rels == [(B.REL_WHEEL, 1)], str(m._kbd.rels)))
    m = make_mapper({'enabled': True, 'suppress_firmware_keys': True,
                     'map': {'184': {'type': 'wheel', 'amount': -1}}})
    m._handle_key(184, 1)
    ok.append(check('向下滚', m._kbd.rels == [(B.REL_WHEEL, -1)], str(m._kbd.rels)))

    # ---------- 8. 全局禁用
    print('\n=== 全局禁用 ===')
    m = make_mapper({'enabled': False, 'suppress_firmware_keys': True,
                     'map': {'48': {'type': 'key', 'combo': 'ctrl+z'}}})
    m._handle_key(48, 1)
    ok.append(check('enabled=false 不产生事件', m._kbd.keys == []))

    # ---------- 9. 学习模式
    print('\n=== 学习模式 ===')
    m = make_mapper({'enabled': True, 'suppress_firmware_keys': True,
                     'map': {'48': {'type': 'key', 'combo': 'ctrl+z'}}})
    box = {}

    def learner():
        box['v'] = m.learn(timeout=5)

    t = threading.Thread(target=learner)
    t.start()
    time.sleep(0.25)
    m._kbd.keys.clear()
    m._handle_key(184, 1)          # 学习中按下 F14
    t.join(timeout=6)
    ok.append(check('学习返回正确键码', box.get('v', {}).get('code') == 184, str(box.get('v'))))
    ok.append(check('学习时不该触发映射', m._kbd.keys == [], str(m._kbd.keys)))
    # 学习结束后恢复映射
    m._handle_key(48, 1)
    ok.append(check('学习结束后映射恢复',
                    m._kbd.keys == [(29, 1), (44, 1)], str(m._kbd.keys)))

    # ---------- 10. 学习超时
    print('\n=== 学习超时 ===')
    m = make_mapper({'enabled': True, 'suppress_firmware_keys': True, 'map': {}})
    t0 = time.time()
    r = m.learn(timeout=0.6)
    dt = time.time() - t0
    ok.append(check('超时返回 None', r is None, f'{dt:.2f}s'))

    # ---------- 11. 坏配置鲁棒性
    print('\n=== 坏配置 ===')
    for bad in ({'48': {'type': 'key'}},                 # 缺 combo
                {'48': {'type': 'key', 'combo': 'badkey'}},
                {'48': {'type': 'mouse', 'button': 'nosuch'}},
                {'48': {'type': 'unknown'}},
                {'48': 'notadict'}):
        m = make_mapper({'enabled': True, 'suppress_firmware_keys': True, 'map': bad})
        try:
            m._handle_key(48, 1)
            m._handle_key(48, 0)
            good = True
        except Exception as e:
            good = False
            print(f'    异常: {e}')
        ok.append(check(f'坏映射安全处理 {bad}', good))

    print('\n=== 结果 ===')
    print(f'  {sum(ok)}/{len(ok)} 通过')
    allok = all(ok)
    print('\n== ' + ('PASS：按键映射逻辑完整正确 ==' if allok else 'FAIL =='))
    return 0 if allok else 1


if __name__ == '__main__':
    sys.exit(main())
