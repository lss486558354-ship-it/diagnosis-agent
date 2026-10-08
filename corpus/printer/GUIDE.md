# 3D 打印机自动化控制系统 — 完整部署与使用指南

本指南覆盖从零开始部署到日常使用的完整流程：Ubuntu 22.04 下载 → 香橙派/PC 部署 → 项目安装 → 功能详解 → 故障排查。

---

## 目录

1. [Ubuntu 22.04 下载与烧录](#1-ubuntu-2204-下载与烧录)
2. [系统初始化与基础配置](#2-系统初始化与基础配置)
3. [项目部署](#3-项目部署)
4. [功能模块详解](#4-功能模块详解)
5. [自动化控制器 (main.py)](#5-自动化控制器-mainpy)
6. [GUI 工具 (mouse_recorder.py)](#6-gui-工具-mouse_recorderpy)
7. [配置说明](#7-配置说明)
8. [开机自启动](#8-开机自启动)
9. [故障排查](#9-故障排查)

---

## 1. Ubuntu 22.04 下载与烧录

### 1.1 下载 Ubuntu 22.04 Desktop

| 硬件平台 | 下载地址 | 说明 |
|---------|---------|------|
| **x86_64 虚拟机** | <https://releases.ubuntu.com/22.04/ubuntu-22.04.5-desktop-amd64.iso> | VMware/VirtualBox/实体 PC |
| **香橙派 5** | <https://github.com/Joshua-Riek/ubuntu-rockchip/releases> | 推荐 `ubuntu-22.04.3-preinstalled-desktop-arm64-orangepi-5.img.xz` |
| **香橙派 3B** | 同上（选择对应型号） | ARM 架构 |
| **树莓派 4** | <https://ubuntu.com/download/raspberry-pi> | 选择 22.04.3 LTS (64-bit) |

### 1.2 烧录到香橙派

**工具**: [balenaEtcher](https://www.balenaetcher.net/) （Windows/macOS/Linux 均有）

```
步骤：
1. 安装并打开 balenaEtcher
2. 点击 "Flash from file" → 选择下载的 .img.xz 文件（无需解压）
3. 点击 "Select target" → 选择你的 TF 卡/优盘
4. 点击 "Flash!" → 等待完成
5. 将 TF 卡插入香橙派，接通电源
```

### 1.3 创建 Ubuntu 虚拟机（x86_64 PC）

```
VMware Workstation / VirtualBox:
1. 新建虚拟机 → 选择 ubuntu-22.04.5-desktop-amd64.iso
2. 内存: 4GB 以上（推荐 8GB）
3. CPU: 4 核以上
4. 磁盘: 30GB 以上
5. 安装完成后重启
```

---

## 2. 系统初始化与基础配置

### 2.1 首次登录

香橙派默认用户名/密码（Joshua-Riek 镜像）:
```
用户名: ubuntu
密码:   ubuntu
```

首次登录会要求修改密码。

### 2.2 切换为 Xorg 会话（重要！）

项目依赖 X11 进行键鼠捕获，Ubuntu 22.04 默认 Wayland 不兼容。

```
方法 1 — 登录界面切换（每次登录手动选）:
  1. 注销/重启，到输入密码界面
  2. 点击用户名 → 屏幕右下角出现齿轮图标 ⚙️ → 点击
  3. 选择 "Ubuntu on Xorg" → 输入密码登录

方法 2 — 永久设置:
  sudo nano /etc/gdm3/custom.conf
  找到 #WaylandEnable=false → 删除前面的 # → 保存退出
  sudo reboot
```

验证是否生效:
```bash
echo $XDG_SESSION_TYPE
# 输出 x11 则正确，输出 wayland 则需重来
```

### 2.3 基础软件安装

```bash
# 更新系统
sudo apt update && sudo apt upgrade -y

# 安装必备工具
sudo apt install -y git curl wget htop net-tools openssh-server

# 启用 SSH（便于远程管理）
sudo systemctl enable ssh
sudo systemctl start ssh

# 查看 IP（方便后续 scp）
ip addr show | grep "inet " | grep -v 127
```

### 2.4 设置固定 IP（可选，推荐）

```bash
# 编辑 netplan 配置
sudo nano /etc/netplan/01-network-manager-all.yaml
```

```yaml
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    eth0:
      dhcp4: no
      addresses:
        - 192.168.1.100/24
      routes:
        - to: default
          via: 192.168.1.1
      nameservers:
        addresses: [8.8.8.8, 114.114.114.114]
```

```bash
sudo netplan apply
```

---

## 3. 项目部署

### 3.1 将项目传到香橙派

**方法 1 — scp（需宿主机开启 SSH）**:
```bash
# 在 Windows 宿主机上（PowerShell）
scp -r "D:\auto - 副本" ubuntu@192.168.x.x:~/printer-controller

# 或者在 Ubuntu 虚拟机上
scp -r user@宿主机IP:/path/to/auto-副本 ~/printer-controller
```

**方法 2 — U盘**:
```
把项目文件夹拷到 U 盘 → 插上香橙派 → 自动挂载到 /media/ubuntu/
cp -r /media/ubuntu/U盘名/auto-副本 ~/printer-controller
```

**方法 3 — Git（如已上传到 GitHub）**:
```bash
git clone https://github.com/你的用户名/你的仓库.git ~/printer-controller
```

### 3.2 安装项目依赖

```bash
cd ~/printer-controller

# 安装系统包
sudo apt install -y python3 python3-pip python3-tk python3-pil xdotool

# 安装 Python 包
pip install pynput pillow

# 可选：MCU 串口通信
pip install pyserial

# 可选：Xlib Python 绑定
pip install python3-xlib
```

### 3.3 验证安装

```bash
cd ~/printer-controller
python3 -c "
from mouse_recorder import IS_LINUX, _find_xdotool
print(f'平台: Linux={IS_LINUX}')
print(f'xdotool: {_find_xdotool()}')
from pixel_monitor import _capture_region_rgb
print('像素监测: OK')
from state_machine import StateMachineEngine
print('状态机: OK')
from cloud_service import CloudService
print('云端服务: OK')
"
# 全部输出 OK 且 xdotool 路径不为 None 则安装成功
```

---

## 4. 功能模块详解

### 整体架构

```
main.py  ← 自动化控制器（headless 入口）
  ├─ app_launcher.py   → 启动 BambuLab Studio + 像素确认
  ├─ cloud_service.py  → 云端双向通信（接收任务/发送状态）
  ├─ print_manager.py  → 打印任务生命周期管理
  ├─ heartbeat.py      → 心跳包（30s 间隔）
  ├─ mcu_bridge.py     → 单片机串口通信
  └─ pixel_monitor.py  → 屏幕像素检测引擎

mouse_recorder.py  ← GUI 工具（录制/回放/监测/状态机）
  ├─ 录制回放 (pynput + xdotool)
  ├─ 像素监测 (PIL/GDI)
  ├─ 状态机 (pixel_monitor + state_machine)
  └─ 云端配置 (cloud_service)
```

---

## 5. 自动化控制器 (main.py)

### 5.1 工作原理

```
上电 → 初始化 → 启动 BambuLab Studio → 像素确认启动成功
  → 向云端发送「空闲」状态 → 进入主状态机循环

主状态机:
  空闲  ←─────────────────────────┐
    ├─ 收到打印任务 A/B            │
    │   ├─ 校验：是否已有任务？     │
    │   │   ├─ 是 → 发送「任务忙」 ─┘
    │   │   └─ 否 → 发送「确认」
    │   ├─ 启动打印 + 后台计时线程
    │   └─ 主线程立即切回空闲 ─────┘
    │
    └─ 收到取消指令 → 终止打印 → 发送「已取消」

后台计时线程:
  ├─ 计时: 预估时长 + 缓冲时间
  ├─ 定时查询像素 → 判断打印是否完成
  │   └─ 完成 → MCU 发送指令 → 云端「完成」→ 结束
  ├─ 超时 → 追加 10 分钟（最多 3 次）
  └─ 收到取消 → 终止 → 云端「已取消」→ 结束

心跳线程 (独立):
  每 30s 发送: 设备状态(空闲/打印中/故障), 当前任务ID, 打印进度, 运行时长
```

### 5.1.1 多线程架构

自动化控制器和 GUI 工具均采用多线程架构，各组件在独立线程中并发运行：

| 组件 | 线程名 | 职责 |
|------|--------|------|
| **主状态机** | `main` (主线程) | 轮询云端消息，分派任务 |
| **云端服务** | `cloud-service` (daemon) | 轮询云命令 (可配置间隔) |
| **心跳服务** | `heartbeat` (daemon) | 定期发送心跳包 (默认 30s) |
| **打印计时器** | 每任务一个 (daemon) | 后台监控打印进度/超时 |
| **像素监测** | `region-monitor` (daemon) | 截图 → 哈希比对 → 规则触发 |
| **状态机引擎** | `sm-engine` (daemon) | 轮询条件 → 状态转移 → 回放动作 |

**GUI 工具 (mouse_recorder.py) 多状态机并发**:

像素监测引擎和每个状态机实例各自运行在独立 daemon 线程中，因此：
- 可同时启动多个状态机实例，每个评估不同区域的像素条件
- 各实例状态转移和回放动作互不干扰
- 例如：「主流程」状态机处理正常操作，「异常监控」状态机同时检测错误弹窗

### 5.2 配置文件

编辑 `config.json`:

```json
{
  "device_id": "printer-01",
  "debug": true,
  "cloud": {
    "enabled": true,
    "server_url": "https://你的云端服务器.com",
    "api_key": "你的API密钥",
    "poll_interval_seconds": 10,
    "heartbeat_interval_seconds": 30
  },
  "bambu": {
    "appimage_path": "./bambu stdio/BambuStudio_ubuntu-22.04-v02.07.00.55-20260514170313.AppImage",
    "launch_recording": "打开bambu stdio",
    "cancel_update_recording": "取消更新",
    "confirm_region": "启动确认",
    "confirm_color": "#00AE42",
    "startup_timeout_s": 120
  },
  "print": {
    "upload_task_recording": "上传打印任务",
    "start_print_recording": "daying",
    "default_timeout_minutes": 120,
    "extra_timeout_minutes": 10,
    "status_check_interval_s": 5,
    "complete_region": "",
    "complete_color": "#00FF00"
  },
  "mcu": {
    "enabled": false,
    "port": "/dev/ttyUSB0",
    "baudrate": 115200
  }
}
```

### 5.3 运行

```bash
cd ~/printer-controller

# 前台运行（用于调试）
python3 main.py

# 后台运行
nohup python3 main.py > controller.log 2>&1 &
```

**日志查看**:
```bash
tail -f controller.log     # 实时日志
cat controller.log | grep ERROR  # 查看错误
```

### 5.4 云端 API 接口规范

自动化控制器期望云端提供以下接口：

#### 设备就绪
```
POST /api/v1/agent/ready
Body: { "hostname": "...", "version": "2.0.0", "status": "idle", "timestamp": ... }
```

#### 拉取命令
```
GET /api/v1/commands?status=pending
Response: { "commands": [ { "type": "print_task_a", "data": { "task_id": "...", ... } } ] }
```

#### 云端消息类型

| type | 说明 | data 字段 |
|------|------|-----------|
| `print_task_a` | 打印任务 A | `task_id`, `estimated_minutes`, `buffer_minutes` |
| `print_task_b` | 打印任务 B | 同上 |
| `cancel` | 取消当前任务 | `task_id` |
| `restart` | 重启控制器 | — |
| `ping` | 心跳检测 | — |

#### 状态上报
```
POST /api/v1/agent/status
Body: { "status": "printing", "task_id": "...", "message": "...", ... }
```

#### 心跳
```
POST /api/v1/agent/heartbeat
Body: { "hostname": "...", "status": "printing", "current_task_id": "...",
        "print_progress": 45.5, "uptime_seconds": 1800, "timestamp": ... }
```

### 5.5 云端升级机制

自动化控制器预留了完整的远程升级接口，支持通过云端下发更新。

#### 升级类型

| 升级类型 | 说明 | 触发消息 type |
|---------|------|---------------|
| **软件更新** | 更新 Python 脚本/模块 | `update_software` |
| **配置更新** | 更新 config.json / 状态机配置 | `update_config` |
| **AppImage 更新** | 更新 BambuLab Studio | `update_appimage` |
| **录制更新** | 更新录制文件 | `update_recording` |

#### 云端消息格式

```json
{
  "type": "update_software",
  "data": {
    "version": "2.1.0",
    "file_url": "https://云端服务器/updates/printer-controller-v2.1.0.tar.gz",
    "sha256": "abc123...",
    "restart_after": true
  }
}
```

```json
{
  "type": "update_config",
  "data": {
    "file_url": "https://云端服务器/updates/config-v2.json",
    "sha256": "def456...",
    "config_type": "state_machine"
  }
}
```

```json
{
  "type": "update_appimage",
  "data": {
    "version": "v02.08.00.00",
    "file_url": "https://云端服务器/updates/BambuStudio-v02.08.AppImage",
    "sha256": "ghi789..."
  }
}
```

```json
{
  "type": "update_recording",
  "data": {
    "name": "上传打印任务",
    "file_url": "https://云端服务器/recordings/上传打印任务.json",
    "sha256": "jkl012..."
  }
}
```

#### 升级流程

```
云端下发升级消息
  → 控制器校验版本号
  → 下载更新包（支持 SHA256 校验）
  → 备份当前文件到 backup_YYYYMMDD_HHMMSS/
  → 解压/替换文件
  → (可选) 重启控制器
  → 向云端发送升级结果
```

#### 状态机配置导出/导入（支持多实例）

状态机配置支持手动导出导入，用于：

- **日常保存**：在 GUI「状态机」页点击「保存」→ 写入 `state_machine.json`
- **多实例导出**：每个实例可单独「导出...」为独立 JSON 文件
- **批量导入**：点击「加载...」支持多选，一次性导入多个状态机 JSON
- **升级前备份**：导出各实例配置到安全位置
- **云端下发**：将配置上传到云端，通过 `update_config` 下发到设备
- **跨设备迁移**：将配置 JSON 复制到其他打印机控制器，批量导入

```
导出: GUI → 状态机 → 下拉选择实例 → 导出... → 选择路径
导入: GUI → 状态机 → 加载... → 多选 JSON 文件 → 自动创建对应实例
云端: 将配置上传到云端 → 下发 update_config → 设备自动加载
```

#### 备份目录结构

升级时自动创建备份：
```
backup_20260520_143000/
├── config.json
├── state_machine.json
├── pixel_config.json
├── mouse_recorder.py
├── main.py
├── ... (所有被替换的文件)
```

---

## 6. GUI 工具 (mouse_recorder.py)

### 6.1 启动

```bash
cd ~/printer-controller
python3 mouse_recorder.py
```

### 6.2 标签页功能

| 标签页 | 功能 | 快捷键/操作 |
|--------|------|-------------|
| **录制回放** | 录制鼠标键盘操作，保存并回放 | F9 录制, F10 停止, Esc 中断 |
| **像素监测** | 屏幕区域颜色监测 + 自动触发 | F6/F7 取坐标, F8 取颜色, 双击规则编辑 |
| **状态机** | PLC 风格自动化，**支持多实例并发** | 顶部下拉切换实例,「全部运行」启动所有 |
| **云端配置** | 云端服务开关与配置 | 填写服务器地址后启用 |
| **使用说明** | 完整操作指南 | 随时查阅 |

### 6.3 像素监测 — 详细操作

```
1. 打开「像素监测」标签页
2. 实时鼠标坐标会显示在顶部（约 10 fps）
3. 选择监测区域:
   a. 鼠标移到目标区域左上角 → 点「→P1」或按 F6
   b. 鼠标移到目标区域右下角 → 点「→P2」或按 F7
   c. 或直接手动输入坐标到 P1/P2 输入框
4. 输入名称 → 点「添加区域」
5. 选中左侧区域 → 编辑触发规则（颜色/容差/录制名）→ 点「添加规则」
6. 双击规则行 → 值自动填入编辑表单 → 修改后点「更新规则」保存
7. 点「开始监测」
```

### 6.4 录制回放 — 详细操作

```
1. 「录制回放」标签页
2. 可选: 勾选「录制键盘」采集键盘事件
3. F9 → 执行操作 → F10 → 命名保存
4. 双击列表中的录制 → 播放
5. 调速: 在速度下拉框中选择 (0.25x ~ 5x)
```

### 6.5 状态机 — 详细操作（多实例并发）

状态机标签页支持创建和管理多个状态机实例，每个实例可独立配置条件并在独立的线程中并发运行。

#### 实例管理

```
• 顶部下拉框 → 选择要编辑的状态机实例
• 「新建」→ 创建新的空状态机实例
• 「删除」→ 删除当前实例（含所有规则文件）
• 「加载...」→ 从文件导入，支持多选，一次性导入多个状态机 JSON
• 「保存」→ 将当前实例写入 state_machine.json
• 「导出...」→ 导出到用户指定路径
```

#### 并发运行

```
• 「▶ 全部运行」→ 一键启动所有状态机实例
  - 每个实例运行在独立 daemon 线程中
  - 各实例条件独立评估，互不干扰
  - 按钮变为「■ 全部停止」→ 一键停止所有
• 「⏸ 暂停」/「⏭ 单步」→ 对当前选中的实例操作
```

#### 配置状态

```
1. 选中一个状态机实例（下拉框切换）
2. 左侧「状态列表」中「新增状态」→ 命名
3. 设置转移规则:
   a. 在「转移规则」区点「添加规则」→ 新建规则文件
   b. 下拉框切换规则 → 编辑条件
   c. 选区域/颜色/容差/触发方式 → 点「+」添加条件
   d. 双击条件行 → 值填入编辑表单 → 修改后点「更新」
   e. 设置「条件达成→」跳转的目标状态（或留空=顺序下一状态）
4. 设置进入/退出动作（回放录制名称）
5. 设置超时和循环
6. 点「保存」
```

#### 条件触发方式

| 方式 | 说明 |
|------|------|
| **颜色一致 (match)** | 区域像素与目标颜色匹配即触发（电平触发） |
| **变化后一致 (changed)** | 颜色从不匹配变为匹配才触发（边沿触发，防重复触发） |

#### 典型多实例场景

```
实例 "主流程":  state_1(检测登录) → state_2(点击报表) → state_3(导出数据) → 循环
实例 "异常监控": state_1(检测错误弹窗) → 回放"关闭弹窗" → 循环

两个实例同时运行，主流程执行正常操作，异常监控随时处理弹窗。
```

---

## 7. 配置说明

### 7.1 配置文件一览

| 文件 | 用途 | 格式 |
|------|------|------|
| `config.json` | **统一配置**（云端/Bambu/打印/MCU） | JSON |
| `pixel_config.json` | 像素监测区域和触发规则 | JSON |
| `state_machine.json` | 状态机配置（状态/条件/动作） | JSON |
| `cloud_config.json` | 云端配置（向后兼容） | JSON |

### 7.2 录制文件

存储在 `recordings/` 目录:
- `名称.json` — 录制数据（含所有鼠标/键盘事件）
- `名称.ahk` — 生成的 AutoHotkey 脚本（Windows 用）

### 7.3 云端文件

- `cloud_history/` — 所有下载过的文件（防重复覆盖）
- `cloud_exec/` — 当前任务文件（每次清空）

---

## 8. 开机自启动

### 8.1 systemd 服务（推荐）

创建服务文件:
```bash
sudo nano /etc/systemd/system/printer-controller.service
```

```ini
[Unit]
Description=3D Printer Automation Controller
After=network.target graphical.target
Wants=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/printer-controller
ExecStart=/usr/bin/python3 /home/ubuntu/printer-controller/main.py
Restart=always
RestartSec=10
StandardOutput=append:/home/ubuntu/printer-controller/controller.log
StandardError=append:/home/ubuntu/printer-controller/controller.log

# 环境变量
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/ubuntu/.Xauthority

[Install]
WantedBy=multi-user.target
```

启用服务:
```bash
sudo systemctl daemon-reload
sudo systemctl enable printer-controller
sudo systemctl start printer-controller

# 查看状态
sudo systemctl status printer-controller

# 查看日志
journalctl -u printer-controller -f
```

### 8.2 crontab 方式（备选）

```bash
crontab -e
# 添加:
@reboot sleep 30 && cd /home/ubuntu/printer-controller && python3 main.py >> controller.log 2>&1
```

---

## 9. 故障排查

### 9.1 鼠标坐标不显示

**症状**: 像素监测页鼠标坐标始终显示 "(请安装 xdotool)"

**解决**:
```bash
sudo apt install xdotool -y
# 验证
xdotool getmouselocation
```

如果 xdotool 已安装但仍不显示:
```bash
# 确认 Xorg 会话
echo $XDG_SESSION_TYPE  # 必须是 x11
# 如果不是 x11，参考 §2.2 切换
```

### 9.2 录制/回放不工作

**症状**: F9 无反应，或回放无声

**解决**:
```bash
# 确认 pynput 已安装
python3 -c "from pynput import mouse; print('OK')"

# 确认 xdotool 已安装
which xdotool

# 确认在 Xorg 下
echo $XDG_SESSION_TYPE
```

### 9.3 像素监测截图黑屏

**症状**: 添加区域后检测不到颜色变化

**解决**:
```bash
# 确认 Pillow 已安装
python3 -c "from PIL import ImageGrab; print('OK')"

# Wayland 下截图受限 → 切换到 Xorg (§2.2)
```

### 9.4 云端无法连接

**症状**: 日志显示 "连接失败"

**排查步骤**:
```bash
# 1. 检查网络
ping 你的云端服务器地址

# 2. 测试 API
curl -v http://你的服务器/api/v1/agent/ready

# 3. 检查配置
cat ~/printer-controller/config.json | grep server_url

# 4. 确认云服务已启用
# config.json 中 cloud.enabled = true
```

### 9.5 MCU 无法通信

**症状**: 日志显示 "MCU 连接失败"

**解决**:
```bash
# 1. 检查设备
ls -la /dev/ttyUSB* /dev/ttyACM*

# 2. 权限
sudo usermod -a -G dialout $USER
# 重新登录后生效

# 3. 测试串口
python3 -c "import serial; s=serial.Serial('/dev/ttyUSB0',115200); print('OK'); s.close()"
```

### 9.6 常见错误速查

| 错误信息 | 原因 | 解决 |
|---------|------|------|
| `pynput not found` | pynput 未安装 | `pip install pynput` |
| `xdotool: not found` | xdotool 未安装 | `sudo apt install xdotool` |
| `No module named 'PIL'` | Pillow 未安装 | `pip install pillow` 或 `sudo apt install python3-pil` |
| `cannot open display` | 没有 X11 会话 | 确认在图形界面下运行 |
| `Permission denied /dev/ttyUSB0` | 串口权限不足 | `sudo usermod -aG dialout $USER` |
| `wayland detected` | 在 Wayland 下运行 | 切换到 Xorg（§2.2） |

---

## 附录 A: 开发模式

```bash
# 启用调试日志
python3 main.py --debug

# 或在 config.json 中设置 "debug": true
```

## 附录 B: 目录结构

```
printer-controller/
├── main.py                # 自动化控制器入口
├── mouse_recorder.py      # GUI 工具（录制/回放/监测/状态机）
├── config.py              # 统一配置模块
├── config.json            # 统一配置文件
├── app_launcher.py        # Studio 启动器
├── print_manager.py       # 打印任务管理器
├── heartbeat.py           # 心跳服务
├── mcu_bridge.py          # MCU 串口通信
├── cloud_service.py       # 云端通信服务
├── cloud_client.py        # HTTP 客户端
├── cloud_config.py        # 云端配置（旧版）
├── cloud_file_manager.py  # 文件管理
├── pixel_monitor.py       # 像素监测引擎
├── state_machine.py       # 状态机引擎
├── bambu stdio/           # BambuLab Studio AppImage 目录
│   └── BambuStudio_ubuntu-22.04-v02.07.00.55-20260514170313.AppImage
├── recordings/            # 录制文件目录
├── cloud_history/         # 云端下载历史
├── cloud_exec/            # 云端下载及更新临时文件
├── backup_YYYYMMDD_HHMMSS/ # 升级自动备份目录
├── sm_rules/              # 状态机转移规则文件（每个状态独立存储）
├── pixel_config.json      # 像素区域配置
├── state_machine.json     # 状态机配置
├── cloud_config.json      # 云端配置（旧版）
├── controller.log         # 运行日志
└── GUIDE.md               # 本文档
```

## 附录 C: BambuLab Studio 安装说明

项目已内置 BambuLab Studio AppImage，位于 `bambu stdio/` 目录下，`config.json` 已配置好路径，无需额外下载。

如需在其他位置运行或更新版本：

```bash
# x86_64 (虚拟机/PC) — 直接运行
chmod +x "bambu stdio/BambuStudio_ubuntu-22.04-*.AppImage"
./bambu\ stdio/BambuStudio_ubuntu-22.04-*.AppImage

# 或从 GitHub 下载最新版
wget https://github.com/bambulab/BambuStudio/releases/download/v02.07.00.55/BambuStudio_ubuntu-22.04-v02.07.00.55-20260514170313.AppImage
chmod +x BambuStudio_ubuntu-22.04-*.AppImage
./BambuStudio_ubuntu-22.04-*.AppImage

# ARM (香橙派) — 需要 box64 转译
sudo apt install box64 -y
box64 ./BambuStudio_ubuntu-22.04-*.AppImage
```

建议在 x86 虚拟机中运行 BambuLab Studio，通过 scp 与香橙派传输 gcode 文件。

AppImage 可通过云端升级接口 (`update_appimage`) 远程更新，升级前会自动备份旧版本。
