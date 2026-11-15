# 排障笔记

记录本项目最花时间的几处故障：**现象 → 排查 → 根因 → 方案**，附实测数据。
故障一～三（`input_event` 记录长度、虚拟节点权限、自我覆盖）与故障四～七
（配置写入分叉、曲线倒挂、控制点定位、按键通路）都是实测确认的；
其中故障七的根因是反汇编推断，标注了未验证部分。

---

## 故障一：设备全对，但 Xournal++ 画不出线

### 现象

- `gaomon-penfix.service` 正常运行，日志无报错
- 虚拟设备已创建，读回轴范围正确：
  `ABS_X 0..50800 res=200`、`ABS_Y 0..31760 res=200`、`PRESSURE 0..16383`、`TILT ±90`
- 笔悬停时**光标跟着动**
- 但 Xournal++ 里画不出任何笔迹，两个侧键也无反应

### 排查

先确认「事件到底有没有写进内核」。写了一个独立脚本：创建与 penfix 同参数的
uinput 设备，写入已知值的合成事件，再用 `EVIOCGABS` 读回 value 字段。这一步
不需要笔在板上，也不受权限影响（读回走 ioctl）。

对比两种记录格式，同一组事件：

| 格式 | 记录长度 | `os.write` 返回 | 读回结果 |
|---|---|---|---|
| `<iiHHi` | 16 字节 | **48 / 64（短写）** | X=0, Y=0, PRESSURE=4096 ✗ |
| `<qqHHi` | 24 字节 | 96 / 96 | X=12345, Y=6789, PRESSURE=4096 ✓ |

注意 16 字节格式下压力值"看起来对"，那是因为压力是第 3 个事件，恰好落在一个
24 字节边界之后；X/Y 则被整体错位吞掉了。这种部分正确最容易误导排查。

### 根因

本机内核 `struct input_event` 是 **24 字节**（64 位 `timeval`：`__kernel_time64_t
tv_sec; long tv_usec; __u16 type; __u16 code; __s32 value`）。代码一直按 16 字节
`<iiHHi` 打包。

关键陷阱：**短写不抛 `OSError`**。`os.write` 到 uinput 只返回被接受的字节数，
内核会把 32 字节按 24 字节边界切，默默丢掉 8 字节。原代码的格式探测是
`try/except OSError`，因此永远"成功"，永远选中错误的 16 字节格式。

### 方案

改成按返回字节数判定，而非异常：

```python
n = os.write(fd, struct.pack('<iiHHi', 0, 0, EV_SYN, 0, 0) * 2)   # 32 字节
fmt = '<iiHHi' if n == 32 else '<qqHHi'
```

- 16 字节内核：32/16 = 2 条完整 → 返回 **32**
- 24 字节内核：32/24 = 1 条完整、余 8 → 返回 **24**

反过来的探测方式不可用：用 24 字节格式写 2 条（48 字节），16 字节内核
48/16=3 条完整同样返回 48，两种内核无法区分。

同时在每次 `write_events` 里比对返回值，短写就打日志，避免同类问题再次静默。

---

## 故障二：事件已进内核，但没人能读

### 现象

修好故障一之后，`--selftest` 报告"全部写入完整 ✓"，坐标读回也正确，
但屏幕上依然没有笔迹。

### 排查

`--selftest` 里读回那一步报 `Permission denied`，但当时以为是沙箱限制忽略了。
直接对比两个节点：

```
event22  (penfix 虚拟笔)  root:input  crw-rw----    → 当前用户不可读
event258 (内核原生笔)      root:input  crw-rw-rw-    → 当前用户可读
```

再看 udev 属性：

```
event22: E: DEVPATH=/devices/virtual/input/input316/event22      ← virtual 总线
event258: E: DEVPATH=/devices/pci0000:00/.../0003:256C:2004.0071/...  ← USB 设备
```

原生笔之所以是 0666，是因为官方 `20-gaomon.rules` 里有一条按 USB 父设备放行的
规则（`SUBSYSTEMS=="usb", ATTRS{idVendor}=="256c", MODE:="0666"`），虚拟设备
没有这个祖先，自然覆盖不到。

### 根因

uinput 设备挂在 **virtual 总线**（`/devices/virtual/input/`）。systemd 的
`73-seat-late.rules` 里 uaccess 逻辑依赖 `ID_SEAT`：

```
ENV{ID_SEAT}=="", IMPORT{parent}="ID_SEAT"
ENV{ID_SEAT}!="", TAG+="$env{ID_SEAT}"
TAG=="uaccess|xaccess-*", ENV{MAJOR}!="", RUN{builtin}+="uaccess"
```

virtual 设备既无 `ID_SEAT` 也无可继承的 USB 祖先，所以 `TAG+="uaccess"`
**不会**为它生成 ACL，最终只有默认的 `root:input 0660`。而 KWin 和 Xournal++
都是以登录用户身份运行，读不到这个节点。

这就是"光标能动、画不出线"：libinput 通过 root 侧通路仍能在某些路径感知设备，
但用户态合成器拿到的事件流是空的。

### 方案

两层兜底，都不依赖 udev 时序：

1. udev 规则显式给权限
   ```
   SUBSYSTEM=="input", KERNEL=="event*", ATTRS{name}=="GAOMON 1060PRO Pen (penfix)", MODE="0666", TAG+="uaccess"
   ```
   注意：这里真正生效的是 `MODE`，`TAG+="uaccess"` 只是尽力而为（对 virtual 设备无效）。

2. 守护进程自行修权限（`_fix_node_perms()`）：以 root 运行时，设备创建后
   轮询 sysfs 找到自己的 event 节点并 `chmod 0666`。这样即使 udev 规则没装、
   或 reload 不及时，也能恢复。

`activate.sh` 里加了一步权限核对，激活后立刻告诉用户节点是否对当前用户可读。

---

## 故障三：重启后轴范围被自己覆盖

### 现象

`--selftest` 在服务已运行时启动，直接抛：

```
PermissionError: [Errno 13] Permission denied: '/dev/input/event22'
```

而且 `mirror_axes()` 拿到的源是虚拟设备本身。

### 根因

`find_real_node()` 的匹配条件是：

```python
if 'GAOMON' in n and 'Pen' in n:
```

而虚拟设备名是 `GAOMON 1060PRO Pen (penfix)` —— **同样满足**。所以当服务已在
运行、又启动一次时，它会把自己的虚拟设备当成镜像源，自我覆盖轴范围；若该节点
不可读则直接崩。

### 方案

统一常量并在查找时显式排除：

```python
VIRT_NAME = 'GAOMON 1060PRO Pen (penfix)'

def find_real_node():
    ...
    if n == VIRT_NAME:
        continue
    if 'GAOMON' in n and 'Pen' in n:
        return ...
```

---

## 故障四：WebUI 保存了，但改动静止不生效

### 现象

在 WebUI 里拖了压感曲线、点了「应用并保存」，提示「已保存并生效」并给出路径，
但重新打开页面——或重启服务后——参数回到旧值。日志里两条 `已热重载…` 都打了。

### 排查

看提示里出现的路径，再和手工编辑的文件对比：

```
提示里:      /root/.config/gaomon-penfix/config.json
手工在改:    /home/xxx/.config/gaomon-penfix/config.json
```

两个文件都存在，内容不同，都在被写，但写的是不同的文件。

### 根因

服务以 **root** 身份运行（systemd unit 没有 `User=`），`os.path.expanduser('~')`
在 root 下返回 `/root`。所以「用户级配置」实际落在 `/root/.config/...`，而用户手工
编辑的是自己家目录下那份。两个位置分叉后，改哪个都只有一半效果。

### 方案

新增 `penfix_config.write_path()`，按**运行身份**而非「用户/系统」选写入目标：

```python
def write_path():
    env = os.environ.get('PENFIX_CONFIG')
    if env:
        return env
    user, system = config_paths()
    return system if os.geteuid() == 0 else user
```

- root 下写 `/etc/gaomon-penfix/config.json`
- 普通用户下写 `~/.config/gaomon-penfix/config.json`
- `PENFIX_CONFIG` / `--config PATH` 显式覆盖（后者会顺带设置前者，
  避免「用 A 启动、保存却写进 B」）

读取仍是两层合并，所以两种身份都能读到对方的配置。

`/api/status` 的 `config_path` 也改用 `write_path()`，前端提示与实际落盘一致。

---

## 故障五：拖控制点后，越用力画得越淡

### 现象

在 WebUI 里把中段控制点往上拖，曲线看着还"顺畅"，但实际书写时手感极差：
轻压出一条粗线，加重反而变细。实测映射值：

```
raw  6553 -> 9662
raw 11468 -> 9379      ← 输入更大，输出反而更小
```

### 排查

把落盘的控制点取出来算切线：

```
points = [[0,0], [0.333, 0.9], [0.667, 0.38], [1,1]]
```

中间点 `y=0.9` 高于右邻点 `y=0.38`，曲线在 `x≈0.333` 之后**向下走**。
拖拽时只限制了 `x` 在左右邻点之间，没有约束 `y`，所以能轻易拖出倒挂。

### 根因

控制点 `y` 没有单调约束。UI 允许任意拖，配置层也原样接受。

### 方案

两层都要管，只改一层会留下缺口：

1. **UI**（`webui.html` 的 `pointermove`）：拖拽时把 `y` 夹在左右邻点 `y` 之间；
   新增点（`pointerdown`）时同样夹一次，否则一插入就倒挂。
2. **配置层**（`normalize_pressure`）：对越界点**向后压平**：

```python
for i in range(len(dedup) - 2, -1, -1):
    if dedup[i][1] > dedup[i + 1][1]:
        dedup[i][1] = dedup[i + 1][1]
```

方向很关键。如果反过来「向前抬平」（`dedup[i][1] < dedup[i-1][1]` 时抬到前一点），
整段会被抬成满压平台——轻触就画最粗的线，比倒挂更糟。实测：

| 输入控制点 | 向前抬平（错误） | 向后压平（采用） |
|---|---|---|
| `[[0,0],[0.333,0.9],[0.667,0.38],[1,1]]` | `[0.667,0.9]`，f(8000)≈15180 | `[0.333,0.38]`，f(8000)=6225 |

同时**锁定首尾端点为 `(0,0)` 与 `(1,1)`**。原先端点只限制纵向移动，仍能被拖离 0/1，
造成"悬停即有压力"或"永远到不了满压"。端点在 UI 里画成方块表示不可拖。

随机 800 组乱序/倒挂控制点归一后：端点固定 ✓、`x` 严格递增 ✓、`y` 单调 ✓、
曲线单调违例 **0**。

---

## 故障六：WebUI 里改按键，页面报 JS 错误 / 点消失

### 现象

在曲线上双击删点、或在两点之间点一下新增控制点，偶尔页面卡住并抛：

```
Cannot read properties of undefined (reading '1')
```

新增点后也偶发"点没加进去"。

### 根因

新增点的定位用了浮点近似比较：

```js
best = pts.findIndex(q => Math.abs(q[0] - nx) < 1e-9);
```

但存入时做了 `+nx.toFixed(3)` 截断，`nx` 与存进去的值相差最多 5e-4，远大于 1e-9，
`findIndex` 返回 `-1`。接着用 `pts[-1][1]`、`pts[best+1]` 去钳制 `y`，就炸在下标 `-1` 上。

### 方案

先截断、再用**同一个值**定位：

```js
const qx = +nx.toFixed(3);
pts.push([qx, +ny.toFixed(3)]);
pts.sort((a,b)=>a[0]-b[0]);
best = pts.findIndex(q => q[0] === qx);
if(best <= 0 || best >= pts.length-1){ dragging = null; draw(); return; }
```

兜住 `best` 越界的情况，避免再次访问 `-1` 或 `length`。

另外给 `draw()` 包了一层 `try/catch`：绘图异常不该让整个渲染链断掉（地址栏标签、
滑块数值、曲线读数都在同一条链上，一个 `undefined` 会让它们全部停更）。

---

## 故障七：按键按了完全没反应，所有节点都是 0 字节

### 现象

板子 12 个 express 键、笔的两个侧键，无论按哪个，同时监听全部可读节点都是 **0 字节**：

```
/dev/hidraw1 (iface0, 64B IN)   0 字节
/dev/hidraw2 (iface1, 16B IN)   0 字节   ← 笔通道也是 0
/dev/hidraw3 (iface2, 16B IN)   0 字节
/dev/input/event23 (板键盘)      0 字节
```

连笔自己的通道都是 0 —— 这排除了"只是按键通路不对"。

### 排查与已排除项

| 怀疑 | 验证 | 结论 |
|---|---|---|
| hidraw 被 penfix 独占 | hidraw 允许多 reader | 排除 |
| usbhid 没绑上 | `3-9:1.{0,1,2}/driver` 均为 `usbhid` | 排除 |
| 设备掉电/挂起 | `power/runtime_status=active`、12 Mbps | 排除 |
| 接口被 libusb 抢 | `fuser` 无进程；pyusb claim 报 `Resource busy` | 说明被 usbhid 正常持有 |
| feature report 轮询 | `0x08` / `0x16` 恒返回 `1601030000000000`，`SETFEATURE` 后不变 | 按键不是 feature report |

USB 端点结构（pyusb 描述符，未 detach 时读出）：

| 接口 | 端点 | 最大包 | 用途 |
|---|---|---|---|
| 0 | `0x81` IN / `0x01` OUT | 64B / 8B | vendor（按键、配置下发） |
| 1 | `0x82` IN | 16B | 笔 |
| 2 | `0x83` IN | 16B | 键盘 |

### 根因（推断，真机未验证）

官方 `huionCore` 的反汇编给出了这一组符号，说明厂商固件有一份统一的报文结构：

```
parse_hbtn_num   读偏移 0xb
parse_sbtn_num   读偏移 0xc
report_hbtn_id / report_hbtn_state
report_touch_id / report_slider_id / report_dail_id
report_type / report_p / report_x / report_y / report_ax / report_ay
is_invalid_report
```

结论：按键是由 **iface0 的 vendor input report（`report 0x08`，11 字节）**主动上报的，
而且需要先向 OUT 端点（`ep 0x01`，8 字节）下发初始化命令，固件才开始推送。
官方驱动采用 libusb 直连 + uhid、并 detach `usbhid` 独占 iface0 —— 正好绕开了
「hidraw 被动等报文」这条路，这解释了为什么被动读取永远是 0。

### 待办

1. 停掉 `usbhid` 对 iface0 的绑定（unbind 或官方那种 detach）。
2. 用 libusb claim iface0，向 `ep 0x01` 下发初始化序列。
3. 从 `ep 0x81` 读 11 字节报文，按偏移 `0xb`/`0xc` 解析 hbtn/sbtn 编号。
4. 拿到真实键码后填入 `buttons.map`，此时 WebUI 的学习模式才真正可用。

侧键的重映射部分不依赖这一步：`EVIOCGRAB` 已实测非 root 可用，键盘节点也存在，
缺的只是"键码到编号"这张对照表。

---

## 附：诊断工具的输出怎么读

`--stats` 每 5 秒打印 `raw=N board=N write=N`，三个计数对应管线三段：

| 输出 | 含义 | 下一步 |
|---|---|---|
| `raw=0` | hidraw 没读到任何帧 | 笔不在板上？USB 接口被独占？ |
| `raw>0, board=0` | 读到帧但都不是"在板"帧 | 协议位判断有误，检查 `byte1 & 0xC0` |
| `board>0, write=0` | 帧正常但没产生事件 | 状态机问题，检查 `handle_frame` 的 diff 逻辑 |
| `write>0` 但无笔迹 | 事件没到用户态 | 查节点权限；确认 libinput 没忽略该设备 |
| uinput 短写日志 | 记录格式不对 | 回到故障一 |

`--selftest` 是一次性全链路验证，不需要笔在板上，适合装完先跑一遍。

WebUI 相关的三个新工具：

```bash
gaomon-penfix --curve-debug      # 打印 raw→mapped 样本，看曲线是否按预期映射
curl -s localhost:8848/api/state  # 一次性看配置 + 运行时状态
journalctl -u gaomon-penfix -n 20 -f   # 保存配置时盯「已热重载…」两行
```

`--curve-debug` 输出示例（为了验证 70% 满压 + γ + 死区）：

```
raw     0 ->     0
raw    19 ->     0      ← 死区生效
raw  4000 ->  1748
raw 11468 -> 16382      ← 70% 处达到满压
raw 16383 -> 16383
```

## 附：官方驱动的架构（逆向结论）

- `huionCore` / `gaomontablet` 均为 x86-64 PIE 用户态 ELF，无 systemd unit，
  由 `gaomontablet.sh` / `huionCore.sh` 启动（`huionCore` 带 `-d`）。
- 数据通路：**libusb 直连 USB**（持有 `/dev/bus/usb/003/048`）+ `/dev/uhid` +
  `/dev/uinput`，**完全不读 hidraw**。
- 它 detach 内核 `usbhid` 并独占首个接口：运行时 `3-9:1.0` 的 driver 显示为
  `usbfs`；进程退出后立即回到 `usbhid`。这就是两者不能并存的原因。
- 其 uhid 笔设备 `HUION 256C PEN STYLUS`：X `0..40000`、Y `0..25000`、
  PRESSURE `0..8191`、TILT `±60`，res 均为 `5080`。
- **缺陷**：`5080` 实为 LPI，被直接填进 `ABS` 的 resolution 字段，udev 因此算出
  `ID_INPUT_WIDTH_MM=7`、`HEIGHT_MM=4`（把 254mm 的板报成 7mm）。
  penfix 用 `res=200` → `WIDTH_MM=254 HEIGHT_MM=158`，正确。
- 其 `Huion Tablet`（event260/263）设备带 `BTN_STYLUS2`，可作为按键能力的参考模型。

## 附：本机内核/环境特性速记

排查时反复踩到的几个点：

- `struct input_event` 为 **24 字节** `'qqHHi'`（不是常见的 16 字节 `'iiHHi'`）。
- `fcntl.ioctl` 传可变缓冲区时返回内核 retval(int)，数据写回原 buffer；
  读结构体必须用 ctypes 直调 + `create_string_buffer`，否则拿不到数据。
- `EVIOCGABS` 成功时返回 **0**（不是结构体大小 24），因此不能拿返回值当字节数。
- `HID uevent` 的 `HID_ID` 是 4 位补零格式（`0003:256C:2004`）；
  `HID_PHYS` 结尾**没有**斜杠（`usb-0000:00:14.0-9/input1`）。
- sysfs 的 capabilities 位图是**高位字在左**，解析时要先反转。
- 设备映射：pen = `hidraw8`（iface input1）、板键盘 = `hidraw9`（iface input2）、
  原生笔 = `event258`、板键盘 = `event259`、虚拟笔 = `event22`（编号会变）。

## 附：本机验证过的模块自测

```bash
python3 penfix_config.py    # 配置 + 曲线模型
python3 test_buttons.py     # 按键映射纯逻辑
```

`penfix_config.py` 自测内容：四个预设均单调、`f(0)=0`、`f(16383)=16383`；
六种坏配置（`points` 为字符串、只有一个点、`gamma` 非数值、`max_pressure_pct` 为负、
`None`、`[]`）全部安全回落。实测输出示例：

```
linear   单调 ✓  f(0)=0  f(16383)=16383  f(4000)= 4000  f(8000)= 8000
soft     单调 ✓  f(0)=0  f(16383)=16383  f(4000)= 7447  f(8000)=11914
hard     单调 ✓  f(0)=0  f(16383)=16383  f(4000)= 1201  f(8000)= 3777
s        单调 ✓  f(0)=0  f(16383)=16383  f(4000)= 1280  f(8000)= 7869
```

`test_buttons.py` 是 **28/28 通过**的纯逻辑测试，不需要数位板也不需要 root。
分层说明写在了脚本头部：设备层（开节点、`EVIOCGRAB`、读事件）依赖真实硬件与权限，
由 `grab_test.py` 单独验证 `EVIOCGRAB` 可用；本脚本只验证**键码 → 动作 → uinput 事件序列**，
这一层才是映射正确性的核心。覆盖范围：

| 分组 | 项数 | 内容 |
|---|---|---|
| 组合键解析 | 5 | `ctrl+z`、`ctrl+shift+z`、`alt+Tab`、`F13`、未知键报错 |
| `key` 按住式 | 2 | 按下全按下、抬起逆序 |
| `key` 点按式 | 2 | `toggle` 发完整 tap、抬起不重发 |
| `none` / 未映射 | 2 | 吞掉、静默 |
| `mouse` | 5 | `left`/`right`/`middle`/`back`/`forward` |
| `wheel` | 2 | 向上 +1、向下 −1 |
| 全局禁用 | 1 | `enabled=false` 不产出事件 |
| 学习模式 | 3 | 返回正确键码、学习期间不触发、结束后恢复 |
| 学习超时 | 1 | 超时返回 `None` |
| 坏配置 | 5 | 缺 `combo`、`combo` 无效、`button` 无效、`type` 未知、映射值不是 dict |

最后一组曾抓到一个真实缺陷：映射值写成字符串（`{'48': 'notadict'}`）时会抛
`'str' object has no attribute 'get'`，因为只判了 `if not act` 没判类型。修法：

```python
if not act or not isinstance(act, dict):
    return
```
