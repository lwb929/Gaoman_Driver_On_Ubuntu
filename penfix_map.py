#!/usr/bin/env python3
"""多屏映射辅助：板面区域裁剪 + KDE kcminputrc 写入。

职责划分（实测教训：penfix 自己算 ABS 几何行不通 —— KWin 对未配置的
tablet 设备只固定映射到某一块屏，不理会我们发出的坐标几何）：

  * 「笔映射到哪块屏」→ 写 kcminputrc 里虚拟设备的 OutputUuid，让 KWin
    自己映射（官方驱动就是这么生效的）；切换后重建虚拟设备逼 KWin 重读。
  * 「板子哪块区域生效 / 板面旋转之外的裁剪」→ penfix 在 ABS 值域内做
    归一化区域裁剪（region = [x0,y0,x1,y1] ∈ [0,1]）。

旋转不在这里做：单一旋钮走 KWin 的 Orientation（0/1/2/3 = rotate/90），
避免双重旋转。
"""
import os
import re
import shutil
import subprocess

# ------------------------------------------------------------------ kscreen

_OUT_RE = re.compile(
    r'Output:\s+(\d+)\s+(\S+)\s+([0-9a-f-]{36})')
_GEOM_RE = re.compile(r'Geometry:\s*(-?\d+),(-?\d+)\s+(\d+)x(\d+)')
_ROT_RE = re.compile(r'Rotation:\s*(\d+)')
_SCALE_RE = re.compile(r'Scale:\s*([\d.]+)')
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def parse_kscreen(text):
    """解析 `kscreen-doctor -o` 输出（先剥离 ANSI 色码 —— 色码会插在
    `Output: ` 与编号之间，如 'Output: \\x1b[0;0m1 eDP-1'）。

    返回 [{'id','name','uuid','x','y','w','h','rotation','scale'}, ...]
    """
    text = _ANSI_RE.sub('', text)
    outs = []
    cur = None
    for line in text.splitlines():
        m = _OUT_RE.search(line)
        if m:
            cur = {'id': int(m.group(1)), 'name': m.group(2), 'uuid': m.group(3),
                   'x': 0, 'y': 0, 'w': 0, 'h': 0, 'rotation': 1, 'scale': 1.0}
            outs.append(cur)
            continue
        if cur is None:
            continue
        if (m := _GEOM_RE.search(line)):
            cur['x'], cur['y'], cur['w'], cur['h'] = map(int, m.groups())
        elif (m := _ROT_RE.search(line)):
            cur['rotation'] = int(m.group(1))
        elif (m := _SCALE_RE.search(line)):
            cur['scale'] = float(m.group(1))
    return [o for o in outs if o['w'] > 0 and o['h'] > 0]


def read_outputs(timeout=2.0):
    """实时读当前输出布局。kscreen-doctor 不存在或失败返回 []。"""
    exe = shutil.which('kscreen-doctor')
    if not exe:
        return []
    try:
        r = subprocess.run([exe, '-o'], capture_output=True, text=True,
                           timeout=timeout)
        return parse_kscreen(r.stdout or '')
    except (OSError, subprocess.TimeoutExpired):
        return []


def pick_output(outs, name):
    """按 connector 名（如 HDMI-A-1）或 uuid 选屏；找不到返回 None。"""
    for o in outs:
        if o['name'] == name or o['uuid'] == name:
            return o
    return None


# ------------------------------------------------------------------ 区域裁剪

def _norm_region(region):
    """把任意输入整理成合法 [x0,y0,x1,y1]（0..1，x1>x0）。坏值回全板。"""
    try:
        vals = [float(v) for v in region]
    except (TypeError, ValueError, IndexError):
        return [0.0, 0.0, 1.0, 1.0]
    if len(vals) != 4:
        return [0.0, 0.0, 1.0, 1.0]
    v = [min(1.0, max(0.0, t)) for t in vals]
    if v[0] >= v[2] or v[1] >= v[3]:
        return [0.0, 0.0, 1.0, 1.0]
    return v


class OutputMapper:
    """板面区域裁剪器（屏幕选择不在这里 —— 见模块 docstring）。

    cfg 是配置里的 `map` 小节 {output, rotate, region}。
    """

    def __init__(self, cfg=None, outputs_provider=None, cache_sec=5.0):
        import time as _t
        self._time = _t.time
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self._provider = outputs_provider or read_outputs
        self._cache_sec = cache_sec
        self._outs = None
        self._ts = 0.0

    def _outputs(self):
        now = self._time()
        if self._outs is None or now - self._ts > self._cache_sec:
            self._outs = self._provider()
            self._ts = now
        return self._outs

    def map(self, x, y, rng_x, rng_y):
        """板面 region 裁剪 → 拉伸到目标屏 dst_region 投影。

        值域内的归一化坐标直接落在输出矩形的对应比例处（KWin 把设备
        坐标铺满目标屏），因此 dst_region 就是坐标值的子区间。
        任何异常/退化情形都退回恒等。"""
        try:
            region = _norm_region(self.cfg.get('region'))
            dst = _norm_region(self.cfg.get('dst_region'))
            if region == [0.0, 0.0, 1.0, 1.0] and dst == [0.0, 0.0, 1.0, 1.0]:
                return x, y
            x0, y0, x1, y1 = region
            u = (x - rng_x[0]) / max(1, rng_x[1] - rng_x[0])
            v = (y - rng_y[0]) / max(1, rng_y[1] - rng_y[0])
            # 板面裁剪 → 归一化 → 投影到目标屏子区域
            u = (x0 + u * (x1 - x0))
            v = (y0 + v * (y1 - y0))
            u = dst[0] + u * (dst[2] - dst[0])
            v = dst[1] + v * (dst[3] - dst[1])
            return (rng_x[0] + u * (rng_x[1] - rng_x[0]),
                    rng_y[0] + v * (rng_y[1] - rng_y[0]))
        except Exception:
            return x, y

    def status(self):
        outs = self._outputs()
        target = str(self.cfg.get('output') or '*').strip() or '*'
        rotate = int(self.cfg.get('rotate') or 0)
        o = pick_output(outs, target) if target != '*' else None
        return {'outputs': outs, 'target': target,
                'resolved': o['uuid'] if o else ('*' if target == '*' else None),
                'resolved_name': o['name'] if o else ('*' if target == '*' else None),
                'rotate': rotate,
                'region': _norm_region(self.cfg.get('region')),
                'dst_region': _norm_region(self.cfg.get('dst_region'))}


# ------------------------------------------------------------------ kcminputrc

# 虚拟设备的 libinput 标识（vendor/product 十进制，段名 = libinput 设备名）
KCM_VENDOR = 9580        # 0x256C
KCM_PRODUCT = 8196       # 0x2004
KCM_DEV_NAME = 'GAOMON 1060PRO Pen (penfix)'
KCM_SECTION = f'[Libinput][{KCM_VENDOR}][{KCM_PRODUCT}][{KCM_DEV_NAME}]'


def kcm_path():
    return os.path.expanduser('~/.config/kcminputrc')


def write_kcm_output(uuid, orientation=0, path=None):
    """把虚拟设备的屏幕映射写进 kcminputrc（保留其余所有内容）。

    uuid=None 表示删除该段（回到「未配置」状态）。
    返回写入的文件路径。原子替换，坏文件不抛（复制原样保留后追加）。
    """
    path = path or kcm_path()
    header = KCM_SECTION
    try:
        lines = open(path, encoding='utf-8', errors='replace').read().splitlines()
    except OSError:
        lines = []

    # 删除已有的我们的段（从段头到下一个段头之前）
    out, skip = [], False
    for ln in lines:
        if ln.strip() == header:
            skip = True
            continue
        if skip and ln.startswith('['):
            skip = False
        if not skip:
            out.append(ln)

    # 去掉文件尾部的 KConfig 落款注释，保证新段附加在有效内容之后
    while out and (out[-1].startswith('[$Version') or out[-1].startswith('%')
                   or not out[-1].strip()):
        out.pop()

    if uuid:
        out.append(header)
        out.append(f'Orientation={int(orientation) % 4}')
        out.append(f'OutputUuid={uuid}')
        out.append('')

    tmp = path + '.penfix.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    os.replace(tmp, path)
    return path


def read_kcm_output(path=None):
    """读当前 kcminputrc 里我们设备的 (uuid, orientation)；无段返回 (None, 0)。"""
    path = path or kcm_path()
    try:
        lines = open(path, encoding='utf-8', errors='replace').read().splitlines()
    except OSError:
        return None, 0
    in_sec = False
    uuid, orient = None, 0
    for ln in lines:
        if ln.strip() == KCM_SECTION:
            in_sec = True
            uuid, orient = None, 0
            continue
        if in_sec and ln.startswith('['):
            break
        if in_sec:
            if ln.startswith('OutputUuid='):
                uuid = ln.split('=', 1)[1].strip() or None
            elif ln.startswith('Orientation='):
                try:
                    orient = int(ln.split('=', 1)[1].strip()) % 4
                except ValueError:
                    pass
    return uuid, orient


# ------------------------------------------------------------------ 自测

def _selftest():
    # 区域裁剪
    m = OutputMapper({'output': '*', 'region': [0, 0, 1, 1]})
    assert m.map(12345, 6789, (0, 50800), (0, 31760)) == (12345, 6789)
    m2 = OutputMapper({'output': '*', 'region': [0.5, 0, 1, 1]})
    lo, hi = (0, 50800)
    ax, ay = m2.map(0, 0, (lo, hi), (0, 31760))          # 板左半被丢弃
    assert abs(ax / hi - 0.5) < 1e-9, ax
    ax, ay = m2.map(hi, 0, (lo, hi), (0, 31760))
    assert abs(ax / hi - 1.0) < 1e-9
    assert _norm_region([0.8, 0, 0.2, 1]) == [0, 0, 1, 1]  # 倒挂回全板
    assert _norm_region('bad') == [0, 0, 1, 1]

    # 竖屏半屏横向投影：板面全幅 → 目标屏上半 [0,0,1,0.5]
    m7 = OutputMapper({'output': '*', 'dst_region': [0, 0, 1, 0.5]})
    rx, ry = (0, 50800), (0, 31760)
    ax, ay = m7.map(rx[1] // 2, 0, rx, ry)               # 板上边中点
    assert abs(ax / rx[1] - 0.5) < 1e-9 and abs(ay / ry[1]) < 1e-9, (ax, ay)
    ax, ay = m7.map(rx[1] // 2, ry[1] // 2, rx, ry)      # 板中心 → 上半屏中心(y=25%)
    assert abs(ay / ry[1] - 0.25) < 1e-9, ay

    # kcminputrc 写入/读取/删除（在临时文件上）
    import tempfile
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.ini', delete=False)
    tmp.write('''[Libinput][10248][258][FTCS1000:01 2808:0102 Touchpad]
DisableWhileTyping=true

[Libinput][9580][61165][HUION 256C PEN STYLUS]
Orientation=1
OutputUuid=3ae4af0b-edcf-471d-845c-f44f238de860

[Mouse]
cursorSize=48
''')
    tmp.close()
    write_kcm_output('uuid-aaa', 1, path=tmp.name)
    body = open(tmp.name).read()
    assert '[Libinput][9580][8196][GAOMON 1060PRO Pen (penfix)]' in body
    assert 'cursorSize=48' in body and 'HUION 256C PEN STYLUS' in body
    assert read_kcm_output(tmp.name) == ('uuid-aaa', 1)
    # 覆盖写
    write_kcm_output('uuid-bbb', 2, path=tmp.name)
    assert read_kcm_output(tmp.name) == ('uuid-bbb', 2)
    assert body.count('GAOMON 1060PRO Pen (penfix)') == 1
    # 删除
    write_kcm_output(None, path=tmp.name)
    assert read_kcm_output(tmp.name) == (None, 0)
    body = open(tmp.name).read()
    assert 'cursorSize=48' in body and 'penfix' not in body
    # 空文件
    open(tmp.name, 'w').close()
    write_kcm_output('uuid-ccc', 0, path=tmp.name)
    assert read_kcm_output(tmp.name) == ('uuid-ccc', 0)
    os.unlink(tmp.name)

    print('映射模块自测通过：区域裁剪/kcminputrc 写读删 ✓')


if __name__ == '__main__':
    _selftest()
