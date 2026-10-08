# 3D 打印机自动化控制系统

> 用「数字员工」的方式驱动**没有开放 API** 的桌面软件：鼠标键盘录制回放 + 像素级界面识别 + PLC 风格状态机 + 云端指令下发。

[![Platform](https://img.shields.io/badge/platform-Ubuntu%2022.04%20%2B%20Xorg-orange)](https://releases.ubuntu.com/22.04/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Lines](https://img.shields.io/badge/code-8.5k%20lines-informational)](#项目规模)

---

## 这个项目解决什么问题

拓竹（BambuLab）的切片软件 **Bambu Studio 没有提供任何自动化接口**——没有 CLI，没有本地 HTTP API，没有插件机制。这意味着在多机 3D 打印农场场景下，**无法批量下发打印任务**，只能人守在每台机器前重复点击。

商用打印农场方案要么买厂商的整机管理套件（贵、绑定硬件），要么自己改固件（失去保修、风险高）。

本项目走第三条路：**把「人在 GUI 上点鼠标」这件事本身自动化掉。**

```
云端下发任务  →  设备侧 Agent 自主执行  →  回报状态
     │                    │                    │
  任务队列          录制回放 + 像素识别        心跳 / 进度
```

设备侧不关心切片软件内部怎么实现，它只做三件事：**看屏幕、动鼠标、报状态**。所以这个方案对任何 GUI 软件都通用——把 `recordings/` 里换成别的软件的操作录制，就能驱动别的程序。

---

## 现场实录

![现场运行](docs/demo/field-test.gif)

真机运行记录，手机拍摄、未剪辑。完整视频见 **[Release v2.0.0](https://github.com/lss486558354-ship-it/printer-controller/releases/tag/v2.0.0)**。

<table>
<tr>
<td width="50%">
<img src="docs/demo/rig-overview.jpg" alt="整机部署形态">
<p align="center"><sub><b>设备侧部署形态</b><br>单板机（Ubuntu 22.04 + Xorg）经 VNC 输出 Bambu Studio；旁侧为 USB 转串口与 STM32 控制板</sub></p>
</td>
<td width="50%">
<img src="docs/demo/bambu-studio-slice.jpg" alt="Bambu Studio 切片界面">
<p align="center"><sub><b>被驱动的目标：Bambu Studio</b><br>模型已排布在热床上，右下角绿色按钮即像素监控检测的 <code>#00AE42</code></sub></p>
</td>
</tr>
<tr>
<td width="50%">
<img src="docs/demo/servo-debug-tool.jpg" alt="舵机调试工具">
<p align="center"><sub><b>GUI 编排工具链</b><br>舵机调试窗口——录制、像素标定、状态机编辑都在这一层的工具里完成</sub></p>
</td>
<td width="50%">
<img src="docs/demo/touchscreen-command.jpg" alt="触屏下发指令">
<p align="center"><sub><b>触屏下发指令</b><br>现场调试时的操作入口</sub></p>
</td>
</tr>
</table>

> ⚠️ **如实说明**：这些是**未经剪辑的现场记录**，画面里有杂乱的桌面、反光的显示器和来回调整的镜头。它们证明的是「这套系统真的搭起来并跑过」，而不是「它很好看」。录屏级的演示需要 Xorg + 真实打印机，目前没有条件重录。

---

## 核心特性

| 特性 | 说明 |
|------|------|
| **无侵入驱动** | 不改固件、不逆向协议，对 Bambu Studio 零依赖改动 |
| **像素级状态识别** | 屏幕区块截图 → RGB 欧氏距离颜色匹配，判断界面处于什么状态 |
| **PLC 风格状态机** | 两级规则系统（jump_rules + transitions），支持**电平触发**与**边沿触发** |
| **跨平台截图抽象** | Windows 走 GDI `BitBlt`，Linux 走 PIL `ImageGrab`，同一接口 |
| **云端指令通道** | 轮询拉取指令 + 30s 心跳 + 状态回报，支持远程取消/重启 |
| **OTA 全链路升级** | 软件 / 配置 / AppImage / 单个录制文件，四类热更新 |
| **失败可恢复** | 打印超时可延长 3 轮，任务单并发保护（`TASK_BUSY`） |
| **GUI 编排工具** | 5 个标签页：录制回放 / 像素监控 / 状态机编辑 / 云端配置 / 帮助 |

---

## 架构

### 双入口设计

| 入口 | 类 | 用途 |
|------|-----|------|
| `main.py` | `PrinterController` | 无头守护进程，产线运行 |
| `mouse_recorder.py` | `MouseRecorderApp` (tkinter) | 图形工具，用于编排录制、标定像素区域、编辑状态机 |

编排时用 GUI，运行时用无头进程——**生产环境不需要 X11 桌面交互，只需要 X11 存在（`xdotool` 和 PIL 截图依赖）**。

### 启动时序

```
_init_components()        # 装配各模块回调（setter 依赖注入）
    │
    ├─ _launch_studio()   # 回放「打开bambu stdio」录制
    │                     # → 用像素确认 Studio 真的起来了（不是盲等）
    ├─ _connect_cloud()   # POST /ready → 启动 30s 心跳 → 上报 idle
    └─ _run_main_loop()   # 主循环睡眠，云端消息经回调异步到达
```

### 线程模型

全部后台服务为 **daemon 线程**，按需创建：

| 线程名 | 创建者 | 职责 |
|--------|--------|------|
| `cloud-service` | `CloudService.start()` | 轮询 `GET /api/v1/commands` |
| `heartbeat` | `HeartbeatService.start()` | 每 30s 上报心跳 |
| `print-timer` | `PrintManager.submit()` | 单任务计时、超时、取消 |
| `sm-engine` | `StateMachineEngine.start()` | 轮询像素条件 → 状态转移 |
| `region-monitor` | `PixelMonitor` | 截图 → 哈希比对 → 触发规则 |

> 回调在**调用方线程**触发（非主线程），`PrinterController` 用 `threading.Lock` 保证回调线程安全。

### 状态机：两级规则 + 两种触发模式

条件按顺序求值，**首个命中即生效**：

1. **Jump Rules**（`jump_rules/`，一规则一文件）— 跨状态复用，由 `state.jump_rules` 引用
2. **Transitions**（`state_machine.json` 内联）— 单状态私有转移

单个规则内多条件为 **AND** 逻辑。条件支持两种模式：

| 模式 | 语义 | 适用 |
|------|------|------|
| `match` | **电平触发**：颜色当前存在即成立 | 「按钮是绿色的 → 可以点」 |
| `changed` | **边沿触发**：颜色由不存在变为存在才成立 | 「打印完成弹窗刚弹出 → 抓一次」 |

边沿触发是这里的**关键设计**——没有它，状态机会在弹窗持续存在的每一轮轮询里反复触发同一个动作。

### 像素监控

```python
_capture_region_rgb(x1, y1, x2, y2) -> bytes   # R,G,B,R,G,B,... 扁平字节流
_scan_rgb_for_color(data, r, g, b, tolerance)  # RGB 空间欧氏距离匹配
```

Windows 分支走 GDI：`CreateCompatibleBitmap` → `BitBlt` → `GetDIBits` 拿 BGRA → 手工重排为 RGB。
Linux 分支走 PIL `ImageGrab`。

**不落盘、不解码完整图像**——只在内存里比对字节流，单次判定成本极低，因此能以 200ms 间隔常驻轮询。

---

## 云端协议

```
POST /api/v1/agent/ready       — {hostname, version, status, timestamp}
GET  /api/v1/commands?status=pending
                               — 返回 {commands: [{type, data}]}
POST /api/v1/agent/status      — {hostname, status, task_id, message, timestamp}
POST /api/v1/agent/heartbeat   — {hostname, status, current_task_id,
                                  print_progress, uptime_seconds, timestamp}
```

支持的指令类型：

| 类型 | 动作 |
|------|------|
| `print_task_a` / `print_task_b` | 执行打印任务（两条不同流程分支） |
| `cancel` | 取消当前打印 |
| `restart` | 重启流程 |
| `ping` | 探活 |
| `update_software` | 拉取 tar.gz 覆盖项目目录 |
| `update_config` | 替换 `config.json` / `state_machine.json` / `pixel_config.json` |
| `update_appimage` | 替换 BambuStudio AppImage 二进制 |
| `update_recording` | 替换单个 `recordings/<name>.json` |

---

## OTA 升级的安全设计

远程升级是这类系统里**风险最高**的一环——一旦被投毒或解包出错，设备就变砖。本项目做了三层防护：

1. **SHA256 强制校验** — 摘要缺失直接拒绝升级，不提供「跳过校验」开关
2. **路径穿越防护** — 解包前对每个 `tarfile` 成员做 `member_path.resolve()` 校验，阻断 `../../` 逃逸
3. **升级前自动备份** — 失败可回滚

---

## 快速开始

### 环境要求

- **Ubuntu 22.04 + Xorg**（必须 `echo $XDG_SESSION_TYPE` 输出 `x11`）
- Python 3.10+
- 硬件：香橙派 5 / 香橙派 3B / 树莓派 4 / x86 PC 或虚拟机

> ⚠️ **不支持 Wayland**。`xdotool` 注入和 PIL 截图都依赖 X11。

### 安装

```bash
sudo apt install -y python3 python3-pip python3-tk python3-pil xdotool
pip install pynput pillow pyserial
```

### 运行

```bash
# 产线：无头守护进程
python3 main.py

# 编排：图形工具（录制 / 标定像素 / 编辑状态机）
python3 mouse_recorder.py

# 冒烟测试：确认各模块可导入
python3 -c "
from mouse_recorder import IS_LINUX, PYNPUT_OK
from pixel_monitor import _capture_region_rgb
from state_machine import StateMachineEngine
from cloud_service import CloudService
print('All OK')
"
```

### 命令行参数

```bash
python3 main.py --init            # 执行 Setup 钩子后正常运行
python3 main.py --init-only       # 仅执行 Setup 钩子后退出
python3 main.py --maintenance     # 执行维护类钩子后正常运行
python3 main.py --skip-onboarding # 跳过首次引导流程
```

### 开机自启

```bash
sudo ./install_service.sh    # 安装 mouse_cloud.service 到 systemd
```

---

## 配置体系

**四层配置加载**，后者覆盖前者：

```
内置默认值  →  managed（云端下发）  →  用户配置  →  本地覆盖  →  环境变量
```

`config.json` 是所有模块设置的单一事实来源，`config.py` 用 dataclass 定义 schema 并校验。`cloud_config.json` 是向后兼容的历史遗留。

`.gitignore` 刻意排除了 `.device_state.json`、`config.local.json`、`cloud_history/`、`backup_*/` —— **敏感配置和运行时产物从不进仓库**。

---

## 项目规模

| 模块 | 行数 | 职责 |
|------|------|------|
| `mouse_recorder.py` | 4600 | 录制回放引擎 + 5 标签页 GUI |
| `main.py` | 931 | 主控制器、生命周期、云端消息分派 |
| `state_machine.py` | 806 | PLC 风格状态机引擎 |
| `pixel_monitor.py` | 418 | 跨平台截图 + 颜色匹配 |
| `config.py` | 311 | 配置 schema 与四层加载 |
| `print_manager.py` | 307 | 打印任务生命周期与超时 |
| 其余 9 个模块 | 1199 | 云端通信、心跳、MCU 桥接、设备状态、Setup 钩子 |
| **合计** | **8572** | 15 个 Python 模块 |

---

## 已知限制与技术债

如实记录当前状态，避免后来者踩坑：

- **`mouse_recorder.py` 4600 行**，GUI 与录制回放引擎耦合在单文件里。已知问题，重构成 `recorder/` + `gui/` 两个包是下一个主要工作项。
- **强依赖 Xorg**，无法在 Wayland 或纯 headless 环境运行。
- **像素坐标非可移植**：`pixel_config.json` 里是绝对屏幕坐标，换分辨率需重新标定。理想方案是归一化坐标或模板匹配。
- **颜色匹配对主题/DPI 敏感**，Bambu Studio 大版本升级可能需重新标定。
- **单并发**：一台设备同时只跑一个打印任务（`print_mgr.is_busy` 时新任务返回 `TASK_BUSY`）。

---

## 目录结构

```
printer-controller/
├── main.py                  # 无头守护进程入口
├── mouse_recorder.py        # GUI 编排工具（录制/像素/状态机/云端/帮助）
├── pixel_monitor.py         # 跨平台截图 + 颜色规则引擎
├── state_machine.py         # PLC 状态机引擎
├── print_manager.py         # 打印任务生命周期
├── cloud_service.py         # 云端指令轮询
├── cloud_client.py          # HTTP 客户端
├── cloud_file_manager.py    # 云端文件下载与校验
├── heartbeat.py             # 心跳服务
├── mcu_bridge.py            # MCU 串口桥接
├── device_state.py          # 设备级持久化状态
├── config.py                # 配置 schema 与四层加载
├── setup_hooks.py           # Setup 钩子框架
├── app_launcher.py          # 应用启动
├── config.json              # 主配置
├── pixel_config.json        # 像素监控区域标定
├── recordings/              # 鼠标键盘操作录制（JSON）
├── sm_rules/                # 状态机跳转规则
├── setup_hooks/             # 首次运行 / 维护钩子
├── install_service.sh       # systemd 服务安装
├── mouse_cloud.service      # systemd 单元文件
├── docs/demo/               # 现场实录静帧与 GIF
├── GUIDE.md                 # 完整部署与故障排查指南
└── CLAUDE.md                # 架构说明（供 AI 协作工具读取）
```

---

## 部署文档

完整部署流程（Ubuntu 烧录 → 香橙派/PC 部署 → 功能详解 → 故障排查）见 **[GUIDE.md](GUIDE.md)**。

架构细节见 **[CLAUDE.md](CLAUDE.md)**。

---

## License

[MIT](LICENSE) © 2026 刘水生
