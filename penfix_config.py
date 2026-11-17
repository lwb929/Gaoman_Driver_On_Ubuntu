#!/usr/bin/env python3
"""配置存储与压感曲线模型。

配置文件：~/.config/gaomon-penfix/config.json（用户级）
         /etc/gaomon-penfix/config.json（系统级，守护进程 root 读取）

设计要点
--------
1. 压感曲线用「三控制点 + 上限 + gamma」描述，对应官方驱动
   `UI_GetPressureSetting(QPoint&, QPoint&, QPoint&, int)` 与
   `calibrate_press_low/mid/high` 的三段结构。
2. 曲线映射必须单调，且 f(0)=0、f(raw_max)=raw_max（无上限时），
   避免笔迹跳变。
3. 所有参数都做范围钳制，坏配置不能让守护进程崩溃。
"""

import json
import os
import tempfile

# ---------------------------------------------------------------- 默认配置

DEFAULT_CONFIG = {
    "version": 1,

    # 压感 -----------------------------------------------------------------
    "pressure": {
        "enabled": True,
        # 三控制点：归一化 (输入力度, 输出压力)，x 必须严格递增
        # 默认线性；"soft" 适合轻笔，"hard" 适合重笔
        "points": [[0.0, 0.0], [0.333, 0.333], [0.666, 0.666], [1.0, 1.0]],
        # 达到满压所需的力度百分比。100 = 用尽全行程
        "max_pressure_pct": 100.0,
        # 伽马：<1 更灵敏，>1 更迟钝
        "gamma": 1.0,
        # 抬笔阈值：原始压力低于此值视为未接触，过滤抖动
        "deadzone": 0,
    },

    # 物理按键（板子上的 express keys + 笔侧键）-------------------------------
    "buttons": {
        # 全局开关：关掉后按键事件原样透传（不拦截、不重映射）
        "enabled": True,
        # 是否屏蔽固件自带的键盘上报（现在按侧键会打出 B/E 字母）
        "suppress_firmware_keys": True,
        # 按键映射。key 是物理键标识，value 是动作。
        # pen_b1 / pen_b2 是笔的两个侧键（下/上），动作经虚拟键盘发出。
        # "dual" = 两种模式共存：
        #   短按 → 切到橡皮，再短按 → 换回笔（交替）；
        #   从笔状态按住不放 → 保持橡皮，松开自动回笔。
        # 默认用 Xournal++ 内置工具快捷键。（焦点需在 Xournal++；
        # 删除映射项即恢复原样透传。）
        "map": {
            "pen_b1": {"type": "dual", "combo": "ctrl+shift+e",
                       "combo2": "ctrl+shift+p"},   # 下键：橡皮 ↔ 笔
            "pen_b2": {"type": "dual", "combo": "ctrl+shift+h",
                       "combo2": "ctrl+shift+p"},   # 上键：荧光笔 ↔ 笔
        },
    },

    # 多屏映射（板面 -> 哪块显示器）-------------------------------------------
    "map": {
        # "*" = KDE 默认；否则填 connector 名（如 "HDMI-A-1"）。
        # 生效方式：写入 ~/.config/kcminputrc 的 OutputUuid 并重建设备，
        # 由 KWin 自己做屏幕映射（实测 penfix 直接改 ABS 几何 KWin 不认）。
        "output": "*",
        # 板面旋转 0/90/180/270 → 写 KWin 的 Orientation=rotate/90。
        # 竖屏选 90 或 270（方向反了就换另一个）。
        "rotate": 0,
        # 板面使用区域 [x0,y0,x1,y1]（归一化），区域内拉伸铺满目标区域
        "region": [0.0, 0.0, 1.0, 1.0],
        # 目标屏内投影区域 [x0,y0,x1,y1]（归一化，相对目标屏）。
        # 竖屏上半=[0,0,1,0.5]，下半=[0,0.5,1,1]；默认整屏。
        "dst_region": [0.0, 0.0, 1.0, 1.0],
    },

    # 界面上展示的按键顺序与别名（由探测填充，也可手工改）
    "labels": {
        "pen_b1": "笔·下侧键",
        "pen_b2": "笔·上侧键",
    },
}


# ---------------------------------------------------------------- 读写

def config_paths():
    """返回 (用户配置路径, 系统配置路径)。"""
    home = os.path.expanduser('~')
    return (
        os.path.join(home, '.config', 'gaomon-penfix', 'config.json'),
        '/etc/gaomon-penfix/config.json',
    )


def write_path():
    """配置文件写入目标。

    守护进程以 root 运行，其 ~ 是 /root —— 若照原样写入，用户手工编辑的
    ~/.config/gaomon-penfix/config.json 与 WebUI 保存的位置就会分叉，
    改哪个都不生效、还以为配置没保存。因此 root 下统一写系统级配置，
    非 root 下写用户级配置（PENFIX_CONFIG 可显式覆盖，主要用于测试）。
    """
    env = os.environ.get('PENFIX_CONFIG')
    if env:
        return env
    user, system = config_paths()
    return system if os.geteuid() == 0 else user


def deep_merge(base, override):
    """把 override 递归合并进 base 的副本。list 直接替换。"""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def clamp(v, lo, hi, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:                       # NaN
        return default
    return max(lo, min(hi, f))


def normalize_pressure(p):
    """把任意输入整理成合法压感配置。坏值回落到默认，不抛异常。"""
    d = DEFAULT_CONFIG['pressure']
    if not isinstance(p, dict):
        return dict(d)

    out = {}
    out['enabled'] = bool(p.get('enabled', d['enabled']))
    out['max_pressure_pct'] = clamp(p.get('max_pressure_pct', d['max_pressure_pct']),
                                    1.0, 100.0, d['max_pressure_pct'])
    out['gamma'] = clamp(p.get('gamma', d['gamma']), 0.1, 5.0, d['gamma'])
    out['deadzone'] = int(clamp(p.get('deadzone', d['deadzone']), 0, 4096, 0))

    pts = p.get('points')
    clean = []
    if isinstance(pts, list):
        for item in pts:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                x = clamp(item[0], 0.0, 1.0, None)
                y = clamp(item[1], 0.0, 1.0, None)
                if x is not None and y is not None:
                    clean.append([x, y])
    clean.sort(key=lambda q: q[0])

    # 去掉 x 重复的点（重复会导致除零），至少保留 2 个点
    dedup = []
    for q in clean:
        if dedup and abs(q[0] - dedup[-1][0]) < 1e-6:
            dedup[-1] = q
        else:
            dedup.append(q)
    if len(dedup) < 2:
        dedup = [list(map(float, q)) for q in d['points']]

    # 端点补齐：确保覆盖 0 和 1，否则曲线两端会外推失控
    if dedup[0][0] > 0.0:
        dedup.insert(0, [0.0, 0.0])
    if dedup[-1][0] < 1.0:
        dedup.append([1.0, 1.0])

    # 两端点固定：f(0) 必须为 0（否则悬停时就有压力），
    # f(1) 必须为 1（否则永远画不到最粗）。中间点才是用户可调的部分，
    # 与官方驱动 calibrate_press_low/mid/high 的三控制点模型一致。
    dedup[0][0], dedup[0][1] = 0.0, 0.0
    dedup[-1][0], dedup[-1][1] = 1.0, 1.0

    # 输出必须单调不减：按压越重，压力不能反而变小（物理上不成立，
    # 且会让笔迹粗细跳动）。
    # 用「向后压平」而不是「向前抬平」：越界点向后一个点看齐，即只降低
    # 输出、不虚增输出。向前抬平会把整段曲线抬成满压平台，轻触就画出最粗
    # 的线，那是比倒挂更糟的后果。
    for i in range(len(dedup) - 2, -1, -1):
        if dedup[i][1] > dedup[i + 1][1]:
            dedup[i][1] = dedup[i + 1][1]

    out['points'] = dedup
    return out


def normalize_buttons(b):
    d = DEFAULT_CONFIG['buttons']
    if not isinstance(b, dict):
        return dict(d)
    out = {
        'enabled': bool(b.get('enabled', d['enabled'])),
        'suppress_firmware_keys': bool(b.get('suppress_firmware_keys',
                                             d['suppress_firmware_keys'])),
        'map': {},
    }
    m = b.get('map')
    if isinstance(m, dict):
        for k, v in m.items():
            if isinstance(v, dict):
                out['map'][str(k)] = v
    return out


def normalize_map(m):
    """多屏映射配置。output="*" 或 connector 名/uuid；rotate ∈ {0,90,180,270}；
    region = 板面使用区域 [x0,y0,x1,y1]（归一化）。"""
    d = DEFAULT_CONFIG['map']
    if not isinstance(m, dict):
        return dict(d)
    out = {
        'output': str(m.get('output', d['output'])).strip() or '*',
        'rotate': int(clamp(m.get('rotate', d['rotate']), 0, 270, 0)),
    }
    if out['rotate'] % 90 != 0:
        out['rotate'] = 0
    try:
        region = [float(v) for v in m.get('region', d['region'])]
    except (TypeError, ValueError):
        region = list(d['region'])
    if len(region) != 4:
        region = list(d['region'])
    region = [min(1.0, max(0.0, v)) for v in region]
    if region[0] >= region[2] or region[1] >= region[3]:
        region = list(d['region'])
    out['region'] = region
    try:
        dst = [float(v) for v in m.get('dst_region', d['dst_region'])]
    except (TypeError, ValueError):
        dst = list(d['dst_region'])
    if len(dst) != 4:
        dst = list(d['dst_region'])
    dst = [min(1.0, max(0.0, v)) for v in dst]
    if dst[0] >= dst[2] or dst[1] >= dst[3]:
        dst = list(d['dst_region'])
    out['dst_region'] = dst
    return out


def normalize(cfg):
    if not isinstance(cfg, dict):
        cfg = {}
    out = deep_merge(DEFAULT_CONFIG, {})
    out['version'] = int(clamp(cfg.get('version', 1), 1, 999, 1))
    out['pressure'] = normalize_pressure(cfg.get('pressure'))
    out['buttons'] = normalize_buttons(cfg.get('buttons'))
    out['map'] = normalize_map(cfg.get('map'))
    labels = cfg.get('labels')
    out['labels'] = {str(k): str(v) for k, v in labels.items()} \
        if isinstance(labels, dict) else {}
    return out


def load():
    """读配置：系统级打底，用户级覆盖。任何一步失败都用默认值。"""
    user, system = config_paths()
    merged = normalize({})
    for path in (system, user):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
        except (OSError, ValueError):
            continue
        merged = normalize(deep_merge(merged, raw) if isinstance(raw, dict) else merged)
    return merged


def save(cfg, path=None, atomic=True):
    """保存配置。atomic=True 时先写临时文件再 rename，避免写一半断电。"""
    cfg = normalize(cfg)
    if path is None:
        path = write_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not atomic:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return path
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.cfg-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


# ---------------------------------------------------------------- 曲线模型

class PressureCurve:
    """把原始压力值映射成输出压力值。

    raw_max 由虚拟设备的 ABS_PRESSURE 上限决定（本机 16383）。
    """

    def __init__(self, cfg, raw_max=16383):
        self.raw_max = raw_max
        self.enabled = bool(cfg.get('enabled', True))
        self.deadzone = int(cfg.get('deadzone', 0))
        self.ceiling = max(1.0, raw_max * (float(cfg.get('max_pressure_pct', 100.0)) / 100.0))
        self.gamma = float(cfg.get('gamma', 1.0))
        self.points = [(float(x), float(y)) for x, y in cfg.get('points', [])]
        if len(self.points) < 2:
            self.points = [(0.0, 0.0), (1.0, 1.0)]
        self.points.sort()

    def __call__(self, raw):
        if not self.enabled:
            return max(0, min(self.raw_max, int(raw)))
        if raw <= self.deadzone:
            return 0
        t = raw / self.ceiling
        if t <= 0.0:
            return 0
        if t >= 1.0:
            t = 1.0
        # 分段线性插值
        pts = self.points
        y = pts[-1][1]
        for i in range(len(pts) - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            if t <= x1:
                y = y0 if x1 <= x0 else y0 + (y1 - y0) * (t - x0) / (x1 - x0)
                break
        if self.gamma != 1.0:
            y = max(0.0, y) ** self.gamma
        return int(max(0.0, min(1.0, y)) * self.raw_max)

    def curve_points(self, n=12):
        """给前端预览用：返回等间距采样点。"""
        return [
            {"input": round(i / (n - 1), 4),
             "output": round(self(int(i / (n - 1) * self.raw_max)) / self.raw_max, 4)}
            for i in range(n)
        ]


# ---------------------------------------------------------------- 预设

PRESETS = {
    "linear": [[0.0, 0.0], [0.333, 0.333], [0.666, 0.666], [1.0, 1.0]],
    "soft":   [[0.0, 0.0], [0.333, 0.62], [0.666, 0.85], [1.0, 1.0]],
    "hard":   [[0.0, 0.0], [0.333, 0.10], [0.666, 0.38], [1.0, 1.0]],
    "s":      [[0.0, 0.0], [0.25, 0.08], [0.75, 0.92], [1.0, 1.0]],
}


if __name__ == '__main__':
    # 自测：曲线模型与配置往返
    cfg = normalize(DEFAULT_CONFIG)
    for name, pts in PRESETS.items():
        c = cfg['pressure']
        c['points'] = [list(map(float, p)) for p in pts]
        curve = PressureCurve(c)
        vals = [curve(r) for r in range(0, 16384)]
        assert all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1)), name
        assert vals[0] == 0 and vals[-1] == 16383, (name, vals[0], vals[-1])
        print(f'{name:8s} 单调 ✓  f(0)=0  f(16383)=16383  '
              f'f(4000)={curve(4000):5d}  f(8000)={curve(8000):5d}')

    # 坏配置鲁棒性
    for bad in ({'points': 'x'}, {'points': [[0.5, 0.5]]}, {'gamma': 'abc'},
                {'max_pressure_pct': -5}, None, []):
        n = normalize_pressure(bad if isinstance(bad, dict) else {})
        PressureCurve(n)
    print('坏配置全部安全回落 ✓')

    # 上限/伽马
    c = normalize_pressure({'max_pressure_pct': 50, 'gamma': 0.7})
    cv = PressureCurve(c)
    print(f'max=50% gamma=0.7: f(8192)={cv(8192)} (应=16383 附近满压)')
    print('config 模块自测通过')
