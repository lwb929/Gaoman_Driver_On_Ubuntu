# Gaoman_Driver_On_Ubuntu

An unofficial driver and adapter for the Gaomon 1060 Pro tablet, featuring a built-in WebUI for configuring mapping, pressure curves, and button bindings. Includes portrait/vertical screen adaptation for subscreen. Open source and cross-platform.

一套让 GAOMON 1060PRO（USB `256c:2004`）在 Ubuntu + KDE Plasma Wayland 上
完整工作的用户态驱动：笔迹跟随、真实压感、笔侧键/板面物理按键可自定义映射，
并带一个零依赖的 WebUI 控制台（压感曲线 / 按键映射 / 多屏映射）。

## 思路

内核 `hid-uclogic` 不认识这块板（`256c:2004` 不在 ID 表），设备落在
`hid-generic` 上，固件的真实报告布局（笔侧键挤在"笔尖"位等）被错误解码。
与其跟内核搏斗（DKMS / 打补丁 / 修驱动 ID 表，内核一升级就全报废），
不如**让内核继续按它的方式解码，我们在用户态旁路接管**：

1. udev 规则把原生笔节点对 libinput 隐藏（`LIBINPUT_IGNORE_DEVICE`）；
2. 守护进程直接读 hidraw **原始报告**（10 字节帧：bit0=笔尖 bit1=下键 bit2=上键
   + X/Y/压力/倾斜），按帧重写后写入一只 uinput 合成的**虚拟笔**；
3. 压感曲线、按键映射、多屏坐标全部在这一层加工，KWin / Xournal++ 只看到虚拟笔。

不动内核、无 DKMS、不碰官方驱动的配置文件；卸载 = 删几个文件 + 拔插 USB。

### 两个必须知道的坑（也是本项目最脆弱的两处）

**1. `input_event` 记录长度**：本机内核是 24 字节（64 位 `timeval`），按 16 字节
打包写入会导致 X/Y/倾斜全部错位，且**短写不抛异常**（`os.write` 只返回被接受的
字节数）——必须用"写 2 条 16B 记录看返回 32 还是 24"来探测。

**2. 虚拟设备权限**：uinput 虚拟设备拿不到 udev `uaccess` 的 ACL，默认
`root:input 0660`，普通用户的 KWin 读不到——表现为"能悬停但画不出线"。
两层兜底：udev 规则 `MODE="0666"` + 守护进程创建设备后自行 `chmod 0666`。

## 适用场景

- Xournal++ / Krita / GIMP 等 Wayland 下需要**压感**和**笔侧键切换工具**（橡皮擦/荧光笔）的书写绘画；
- KDE Plasma **多显示器**：板子映射到指定屏幕，竖屏（Rotated90）直接用，
  或投影到竖屏的**上半/下半**做横向书写（半屏横向投影）；
- 板面物理按键（12 键）映射成键盘快捷键 / 鼠标键 / 滚轮 / 命令 / 禁用；
- 官方 Linux 驱动不可用或与其冲突时作为**替代品**（两者只能二选一，见下文）。

## 实测设备

| 项目 | 实测环境 |
|---|---|
| 数位板 | GAOMON 1060PRO（`lsusb`: `256c:2004 GAOMON_Tablet_1060PRO`）|
| 系统 | Ubuntu，内核 `7.0.0-43-generic`（24 字节 `input_event`）|
| 桌面 | KDE Plasma 6 / Wayland（KWin），libinput |
| 应用 | Xournal++（压感 + 侧键切换工具）|
| 显示器 | 笔记本 eDP-1 1920x1080 + 外接 HDMI-A-1 1920x1080（`Rotated90` 竖屏）|
| 多屏验证 | kcminputrc `OutputUuid` 映射、竖屏半屏横向投影（行为实测进行中）|

> 其它 GAOMON/Huion 系（256c:xxxx）大概率可用但未实测；内核若为 16 字节
> `input_event`，启动时的探测会自动适配。

## How to use

```bash
# 安装（不影响现状，末尾自动自检）
sudo bash install.sh
# 激活接管 + 拔插一次 USB 线
sudo bash activate.sh
# 打开控制台
http://127.0.0.1:8848/
```

WebUI 三张卡片：**压感曲线**（拖控制点，实时热生效）、**物理按键映射**
（点「学习」再按目标键，自动识别键码）、**多屏映射**（选目标屏 / 旋转 /
板面区域框选 / 目标屏内投影区域，保存即写入 KDE 配置并重建设备）。

卸载：`sudo bash uninstall.sh`，配置文件保留在 `~/.config/gaomon-penfix/`。

## 问题

1. 内核 `hid-uclogic` 驱动的 ID 表不含 `256c:2004`（无法手动绑定，现代内核拒绝 sysfs 强绑，亦无 `driver_override`），设备落在 `hid-generic` 上。
2. `hid-generic` 只按固件宣传的 HID 描述符解码；固件实际把**上侧键**放在"笔尖"位（bit0），所以内核永远暴露不出 `BTN_STYLUS2`——上键还会被误认为笔尖触碰。
3. 下键其实有 `BTN_STYLUS` 事件，但 Xournal++ 默认配置里笔按键动作是 `none`，所以表现为"按键不切换工具"。

## 原理

`gaomon-penfix.py` 直接读笔接口 hidraw 的原始报告（10 字节帧，report id `0x0a`：
byte1 bit0=笔尖 bit1=下键 bit2=上键，bytes2-7=X/Y/压力 小端，bytes8-9=倾斜 S8），
用 uinput 合成一只带完整按键的虚拟笔；udev 规则把原生笔节点对 libinput 隐藏
（`LIBINPUT_IGNORE_DEVICE`），KDE Plasma Wayland / Xournal++ 只看到虚拟笔。

不动内核、无 DKMS，内核升级不受影响。压感/笔迹由本进程逐帧转发（轴范围启动时
从真实 evdev 节点镜像）。

### 两个必须知道的坑

这两处曾导致"设备看起来都正常，但就是画不出线"，是本项目最脆弱的地方：

**1. `input_event` 记录长度**

本机内核 `struct input_event` 是 **24 字节**（64 位 `timeval`）。若按 16 字节
`<iiHHi` 打包写入，内核会按 24 字节边界重新切分，结果是 X/Y/倾斜全部错位、
按键事件丢失。

致命之处在于**短写不抛异常**：`os.write` 只返回被内核接受的字节数。所以不能靠
`try/except OSError` 探测格式，必须比对返回字节数。现在的探测方式：写入 2 条 16
字节格式记录（32 字节），返回 32 说明内核是 16 字节，返回 24 说明是 24 字节。
（若改用 24 字节格式探测，两种内核都返回 48，无法区分，不要改回去。）

**2. 虚拟设备权限**

uinput 设备挂在 `/devices/virtual/input/` 下。virtual 总线设备拿不到 `ID_SEAT`，
udev 的 `uaccess` 逻辑不会为它们生成 ACL，设备默认落成 `root:input 0660`。
当前用户不在 `input` 组，于是 KWin / Xournal++ **读不到**这个节点——事件已经进了
内核，但没有任何用户态程序能收到。表现为"笔能悬停、光标跟着动，但画不出线"。

两层兜底：udev 规则 `MODE="0666"`；守护进程以 root 运行时还会在设备创建后自行
`chmod 0666`（`_fix_node_perms()`），不依赖 udev 是否及时。

## 使用（详细）

```bash
sudo bash install.sh     # 安装（不影响现状），末尾自动跑 --selftest
# 可选：侧键验证测试（两个终端）
sudo /usr/local/bin/gaomon-penfix --no-motion
sudo /usr/local/bin/gaomon-penfix-button-watcher
# 激活接管 + 拔插 USB 线
sudo bash activate.sh
```

装好后浏览器打开 **<http://127.0.0.1:8848/>** 调压感曲线与按键映射。

Xournal++ 里：编辑 → 偏好设置 → 输入 → 笔按键动作：
按钮 1 = 橡皮擦，按钮 2 = 荧光笔（按住临时切换，松开恢复）。

### 安装布局

`install.sh` 把主程序与配套模块装进同一目录，因为主程序按 `__file__` 找同级模块：

```
/usr/local/lib/gaomon-penfix/
    gaomon-penfix.py    主程序（hidraw → uinput 代理）
    penfix_config.py    配置存储 + 压感曲线模型 + 预设
    penfix_buttons.py   按键映射引擎（EVIOCGRAB + uinput）
    penfix_web.py       零依赖 HTTP 服务与 API
    webui.html          单文件前端
/usr/local/bin/gaomon-penfix        → sh 启动器，指向上面目录
```

install 结束会打印文件清单（缺一个就会在导入时崩，提前暴露）并跑模块自检。

### WebUI 控制台

`penfix_web.py` 只用标准库 `http.server`，**零第三方依赖**（系统没有 flask 也能跑），
**只监听 `127.0.0.1`**，默认端口 8848，不会暴露到局域网。

| 路由 | 说明 |
|---|---|
| `GET /` | `webui.html` |
| `GET /api/state` | `{config, status}` 一次性取全量 |
| `GET /api/status` | 状态，前端每秒轮询 |
| `POST /api/config` | 归一 → 校验 → 原子落盘 → 热重载 |
| `POST /api/learn` | 等待按下一个物理键，返回 `{code, name}`；超时返回 408 |

相关命令行参数：

```bash
gaomon-penfix --web                # 启动控制台（默认 8848）
gaomon-penfix --web-port 9000      # 换端口
gaomon-penfix --no-buttons         # 不接管物理按键，保留固件原始行为
gaomon-penfix --curve-debug        # 打印若干 raw→mapped 样本后退出
gaomon-penfix --config PATH        # 指定配置文件（同时成为保存目标）
```

热重载行为：压感曲线即时替换（无需重启）；按键映射需要重建（重新 `EVIOCGRAB`）。
日志会分别打印 `已热重载压感曲线` / `已热重载按键映射`。

### 压感曲线

对标官方驱动的三控制点模型（`UI_GetPressureSetting(QPoint&, QPoint&, QPoint&, int)`
与 `calibrate_press_low/mid/high`），但用归一化控制点数组描述，点数可以多于三个。

| 配置项 | 范围 | 含义 |
|---|---|---|
| `pressure.points` | 归一化 `(力度, 输出)` 数组 | 分段线性插值曲线 |
| `pressure.max_pressure_pct` | 1–100 | 达到满压所需的力度百分比，越低越轻就画粗 |
| `pressure.gamma` | 0.1–5.0 | `<1` 更灵敏，`>1` 更需用力 |
| `pressure.deadzone` | 0–4096（原始压力值） | 低于此值视为未接触，过滤抖动 |
| `pressure.enabled` | bool | 关闭后原样透传 |

**不变量**（归一化时强制，坏配置改不动）：

- `f(0) = 0`、`f(1) = 1`
- `x` 严格递增
- 输出随输入**单调不减**

首尾两端点被锁定为 `(0,0)` 与 `(1,1)`，UI 里画成方块、不可拖动。理由：端点漂移会让
悬停时就有压力、或永远画不到最粗。**想「更轻就画粗」请调满压阈值，别拖端点。**

四个预设：

| 预设 | 效果 | 实测 `f(4000)` / `f(8000)` |
|---|---|---|
| `linear` 线性 | 原样 | 4000 / 8000 |
| `soft` 轻触 | 前段抬升快 | 7447 / 11914 |
| `hard` 重压 | 前段压低，需更用力 | 1201 / 3777 |
| `s` S 形 | 中间过渡陡 | 1280 / 7869 |

单调性是硬约束，因为倒挂曲线在物理上不成立：实测控制点
`[[0,0],[0.333,0.9],[0.667,0.38],[1,1]]` 会产生 raw 6553→9662 而 raw 11468→9379
（越用力越淡）。归一化会把越界点**向后压平**（`[0.333,0.9]` → `[0.333,0.38]`），
复测 raw 6553→4564、raw 11468→6225，恢复单调。

注意「向后压平」而不是「向前抬平」：向前抬平会把整段抬成满压平台，轻触就画出最粗的线，
那是比倒挂更糟的后果。

### 物理按键映射

覆盖板子的 12 个 express 键与笔的两个侧键。动作类型：

| type | 参数 | 行为 |
|---|---|---|
| `none` | — | 吞掉，不产生任何事件 |
| `key` | `combo`（如 `ctrl+z`）、`toggle` | 默认按住式；`toggle: true` 为按下即完整点按 |
| `mouse` | `button`: `left`/`right`/`middle`/`back`/`forward` | 鼠标键按下+抬起 |
| `wheel` | `amount`: `±1` | 单次滚轮 |
| `cmd` | `command` | 执行 shell 命令（异步，不阻塞） |

配置示例：

```json
{
  "buttons": {
    "enabled": true,
    "suppress_firmware_keys": true,
    "map": {
      "48":  { "type": "none" },
      "18":  { "type": "key", "combo": "ctrl+z" },
      "184": { "type": "wheel", "amount": -1 }
    }
  },
  "labels": { "48": "笔侧下键", "184": "板键1" }
}
```

实现手段：对板子的键盘节点做 `EVIOCGRAB` 独占（实测**非 root 可用**），顺带屏蔽固件
自带的字母上报（下侧键→`KEY_B`、上侧键→`KEY_E`）。

**「学习模式」**是获取键码的正常方式：点 UI 上的「学习」，然后按板子上的键，自动识别键码
并回填，不需要手工查 Linux 键码表。学习模式下按键**不触发任何动作**，避免边学边误触。

> ⚠️ **当前状态：按键映射的代码与纯逻辑测试已完成（28/28 通过），但真机键码尚未采集到。**
> 详见下文「已知边界」。

### 配置文件

两层递归合并，系统级打底、用户级覆盖：

```
/etc/gaomon-penfix/config.json         系统级（打底）
~/.config/gaomon-penfix/config.json    用户级（覆盖）
```

**但「写入」只看运行身份**（`penfix_config.write_path()`）：

| 运行身份 | 保存到 |
|---|---|
| root（systemd 服务） | `/etc/gaomon-penfix/config.json` |
| 普通用户 | `~/.config/gaomon-penfix/config.json` |
| 设了 `PENFIX_CONFIG` 或 `--config` | 该路径（优先） |

原因：root 的 `~` 是 `/root`。若不加区分，用户手改 `~/.config/...`、WebUI 却写进
`/root/.config/...`，改哪个都不生效、还以为保存失败。

写入是原子的（`tempfile` → `os.replace` + `fsync`），不会写一半断电留下坏 JSON。
`normalize()` 对任何坏配置都不抛异常，逐项回落默认值。

### 诊断工具

```bash
gaomon-penfix --selftest   # 合成帧走完整管线并读回校验，不需要笔在板上
gaomon-penfix --stats      # 每 5 秒打印 raw=N board=N write=N
gaomon-penfix --curve-debug  # 打印 raw→mapped 样本，验证曲线
python3 penfix_config.py   # 配置与曲线模型自测
python3 test_buttons.py    # 按键映射纯逻辑自测（28 项）
```

`--selftest` 会打印内核 `input_event` 记录长度、格式与短写检查、摄取计数、
`absinfo` 读回、按键位读回。判断 uinput 通路是否正常，用它最快。

`--stats` 用来区分是"没读到帧"还是"读到但没写出去"：

- `raw=0` —— hidraw 没数据（笔不在板上，或 USB 接口被别的驱动抢占）
- `raw>0, board=0` —— 读到帧但都判定为"不在板"，检查协议位
- `board>0, write=0` —— 帧正常但没产生事件，检查状态机
- `write>0` 但屏幕无反应 —— 多半是权限或 libinput 忽略问题

## 多屏映射

多显示器下把板子映射到指定屏幕。WebUI「多屏映射」卡片：选目标屏 + 板面旋转 + 板面区域框选，保存即生效（无需拔插 USB）。

**生效机制（重要）**：penfix 自己改 ABS 坐标几何对 KWin 无效（实测：KWin 对未配置的
tablet 设备只固定映射到某块屏，不理会驱动发出的坐标值）。所以：

- **选哪块屏 / 板面旋转** → 写 `~/.config/kcminputrc` 里虚拟设备
  `[Libinput][9580][8196][GAOMON 1060PRO Pen (penfix)]` 的 `OutputUuid` + `Orientation`，
  然后**重建虚拟设备**逼 KWin 按新配置重新映射（KWin 只在设备出现时读配置）。
  这与官方驱动生效的通路相同。
- **板面区域框选**（`map.region = [x0,y0,x1,y1]` 归一化）→ penfix 在值域内裁剪，
  选中区域拉伸铺满目标屏。
- 板面旋转写的是 KWin `Orientation = rotate/90`（0/1/2/3）；竖屏选 90°，方向反了换 270°。
- `map.output = "*"` 时清除 kcminputrc 段，回到 KDE 默认映射。

## 排障

按现象定位，详见 [TROUBLESHOOTING.md](TROUBLESHOOTING.md)。

| 现象 | 先查 |
|---|---|
| 完全没反应，光标都不动 | `--stats` 看 `raw`；`raw=0` 则查 hidraw 与 USB 接口归属 |
| 光标能悬停跟随，但画不出线 | 虚拟节点权限（`ls -l /dev/input/event*` 应为 `0666`） |
| 有笔迹但位置偏移/乱跳 | `input_event` 记录长度是否判成 24 字节 |
| 侧键无反应 | Xournal++ 的笔按键动作设置；`--no-motion` + `button-watcher` 验证 |
| 打不开 WebUI | 服务是否带 `--web`；`ss -ltnp | grep 8848`；端口被占 |
| WebUI 保存后不生效 | 确认写的是哪个文件（见「配置文件」的写入目标表） |
| 曲线调了没变化 | `--curve-debug` 看映射值；`pressure.enabled` 是否为 true |
| 按键按了没反应 | 先看日志是否 `grab=true`；键码是否已采集到（见「已知边界」） |
| 多屏下笔迹跑到别的屏幕 | WebUI「多屏映射」选目标屏（写 kcminputrc + 重建设备）；确认日志出现「已写入 KWin 配置并重建设备」 |
| 选了屏还是不对 | 若重建设备后 KWin 仍不认：拔插一次 USB 再试；`grep -A3 'penfix' ~/.config/kcminputrc` 看 OutputUuid 是否为目标屏 uuid |
| 竖屏方向反了（上下颠倒） | 板面旋转 90° ↔ 270° 对调（写的是 KWin Orientation，语义按 KWin 的来） |

## 回滚

```bash
sudo bash deactivate.sh   # 停用并拔插 USB 线，恢复原生
sudo bash uninstall.sh    # 彻底删除
```

## 与官方驱动的关系

官方驱动 `/usr/lib/gaomontablet/{huionCore,gaomontablet}` 是用户态 ELF，采用
**libusb 直连 USB + uhid + uinput**，完全不读 hidraw。它会 detach 内核 `usbhid`
并独占第一个 USB 接口（`3-9:1.0` 的 driver 变为 `usbfs`）；官方进程退出后接口
立即回到 `usbhid`。

因此**两者不能同时运行**：官方驱动在跑时，penfix 读 hidraw 会一直是 0 字节。
切换前请先彻底停掉另一方。

顺带记录官方驱动的一个缺陷：它把 `res=5080`（实为 LPI）直接填进 `ABS` 的
resolution 字段，导致 udev 算出 `ID_INPUT_WIDTH_MM=7`、`HEIGHT_MM=4`，把 254mm
宽的板报成 7mm。penfix 用 `res=200`，得到 `WIDTH_MM=254 HEIGHT_MM=158`，正确。

## 已知边界

- **板子的 12 个 express 键与笔侧键的真实键码尚未采集到，按键映射未在真机验证通过。**
  多轮采集（同时监听全部可读 `/dev/input/event*` 与全部 `hidraw`，含笔通道）在用户报告
  「按键已按下」的窗口内仍返回 0 字节。已排除：`hidraw` 允许多 reader（0 字节不是被
  penfix 独占）；三个接口的驱动归属均为 `usbhid` 正常；设备状态 `active`、12 Mbps。
  当前最可能的原因是按键数据走 iface0 的 vendor input report（`report 0x08`，11 字节，
  结构含 `parse_hbtn_num` 读偏移 `0xb`、`parse_sbtn_num` 读偏移 `0xc`，以及
  `report_hbtn_id/state`、`report_touch_id`、`report_slider_id`、`report_dail_id`），
  且需要先向 iface0 的 OUT 端点（`ep 0x01`，8 字节）下发初始化命令设备才开始上报。
  官方驱动正是 libusb 直连 + uhid，并且会 detach `usbhid` —— 这一点与上述推断吻合。
  下一步方向：主动 claim iface0（需停掉 usbhid 对该接口的绑定）后下发初始化序列，
  再用 `EVIOCGRAB` 接管键盘节点做重映射。
- 按侧键时，板子键盘节点仍会发一个字母（下键=`KEY_B`、上键=`KEY_E`，固件行为）。
  `suppress_firmware_keys: true` 时由 `EVIOCGRAB` 屏蔽；关掉该选项则字母会漏到应用。
- 橡皮头（bit3）未实测，出现时按橡皮处理并打日志。
- 该板不在 hid-uclogic 支持表内；若未来内核加入 256c:2004，可停用本方案改用原生驱动。
- 本方案依赖 hidraw 原始帧。若与官方驱动同时运行，或 USB 接口被其他驱动独占，
  hidraw 读不到数据。
- WebUI 无鉴权，因此只绑 `127.0.0.1`。**不要**改成 `0.0.0.0`。
- `cmd` 动作会以守护进程身份（root）执行 shell 命令。只在明确知道自己配了什么时使用。
