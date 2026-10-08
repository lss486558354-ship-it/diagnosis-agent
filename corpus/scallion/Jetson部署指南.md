# Jetson Orin Nano 部署指南

大葱切割-清洗-打包一体机 - Jetson Orin Nano 版本

---

## 一、硬件连接

### 1.1 Jetson Orin Nano 与 STM32F103VE 连接方式

**方式一：使用USB转串口模块（推荐）**

```
Jetson Orin Nano                USB转TTL模块              STM32F103VE
┌─────────────┐                ┌─────────────┐          ┌─────────────┐
│   USB接口   │◄───USB线──────►│  USB转TTL   │          │             │
│             │                │  (3.3V电平)  │          │             │
│             │                │  TX   RX   │          │             │
└─────────────┘                │  RX   TX   │          │             │
                               │  GND  GND  │◄───线────►│  GND       │
                               └─────────────┘          │  PA2 (TX)   │◄──┐
                                                       │  PA3 (RX)   │───┘
                                                       └─────────────┘
```

**接线说明：**
- USB转TTL模块 3.3V -> 不接（只接GND、TX、RX）
- GND -> STM32 GND（必须共地）
- USB转TTL TX -> STM32 PA3 (USART2_RX)
- USB转TTL RX -> STM32 PA2 (USART2_TX)

**方式二：使用Jetson 40Pin扩展口UART**

```
Jetson Orin Nano 40Pin                          STM32F103VE
┌──────────────────┐                          ┌─────────────┐
│ Pin 8  (UART1_TX)│◄───────线───────────────►│ PA3 (RX)    │
│ Pin 10 (UART1_RX)│◄───────线───────────────►│ PA2 (TX)    │
│ Pin 6  (GND)     │◄───────线───────────────►│ GND         │
└──────────────────┘                          └─────────────┘
```

### 1.2 硬件参数

| 参数 | 值 |
|------|-----|
| 波特率 | 115200 |
| 数据位 | 8 |
| 停止位 | 1 |
| 校验位 | 无 |
| 电平 | 3.3V TTL |

---

## 二、软件安装

### 2.1 系统要求

- JetPack 5.x 或更高版本
- Python 3.8+
- 已安装 pyserial 库

### 2.2 安装步骤

**步骤1：连接Jetson Orin Nano**

通过SSH或显示器连接Jetson Orin Nano。

**步骤2：安装Python依赖**

```bash
# 安装pyserial（串口通信）
pip install pyserial

# 安装ultralytics（YOLOv8视觉）
pip install ultralytics

# 可选：安装OpenCV（图像处理）
pip install opencv-python
```

**步骤3：传输通信程序**

将 `OrangePi/stm32_comm.py` 复制到Jetson：

```bash
# 使用SCP传输（Windows PowerShell）
scp C:\Users\yolov8\Desktop\大葱切割-清洗-打包一体机\OrangePi\stm32_comm.py jetson@<jetson-ip>:/home/jetson/

# 或使用U盘直接复制
```

**步骤4：设置串口权限**

```bash
# 查看串口设备
ls -l /dev/ttyUSB*
# 或查看40Pin UART
ls -l /dev/ttyTHS*

# 设置权限（每次重启后需重新设置，或添加到永久规则）
sudo chmod 666 /dev/ttyUSB0      # USB转串口
# 或
sudo chmod 666 /dev/ttyTHS0      # 40Pin UART
```

**步骤5：创建永久权限规则（推荐）**

```bash
# 创建规则文件
sudo nano /etc/udev/rules.d/99-usb-serial.rules
```

添加以下内容：
```
# USB转串口权限
KERNEL=="ttyUSB*", MODE="0666", GROUP="dialout"

# Jetson 40Pin UART权限
KERNEL=="ttyTHS*", MODE="0666", GROUP="dialout"
```

保存后执行：
```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

---

## 三、使用方法

### 3.1 基本使用

```bash
# 进入程序目录
cd ~

# 运行通信程序
python3 stm32_comm.py
```

程序会自动检测可用的串口设备，输出示例：

```
==================================================
Jetson Orin Nano 串口配置
==================================================
发现以下UART设备:
  1. /dev/ttyUSB0 (USB转串口)
  2. /dev/ttyTHS0 (40Pin扩展口)
将使用的串口: /dev/ttyUSB0
==================================================

2026-03-23 10:30:00 - STM32Comm - INFO - 发现USB转串口设备: /dev/ttyUSB0
2026-03-23 10:30:00 - STM32Comm - INFO - Jetson串口 /dev/ttyUSB0 连接成功
```

### 3.2 指定串口运行

如果程序自动检测的串口不正确，可以手动指定：

```python
# 在程序中修改（约第130行）
controller = STM32Communicator(port='/dev/ttyUSB0')
```

或在命令行中调用：
```python
# 修改程序添加命令行参数支持
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--port', type=str, default=None, help='串口设备路径')
args = parser.parse_args()

controller = STM32Communicator(port=args.port)
```

### 3.3 集成YOLOv8视觉

程序已内置YOLOv8检测功能，按如下方式使用：

```python
from stm32_comm import ScallionController

controller = ScallionController()

# 加载YOLOv8模型
controller.load_yolo_model('yolov8n.pt')  # 使用轻量级模型

# 连接STM32
controller.start()

# 在实际使用时调用检测
# detections = controller.detect_scallion(camera_frame)
# for det in detections:
#     print(f"检测到大葱: 位置({det['x']:.1f}, {det['y']:.1f}), 置信度{det['confidence']:.2f}")
```

### 3.4 常用控制命令

```python
# 创建通信对象
stm32 = STM32Communicator()

# 连接
stm32.connect()

# 发送控制命令
stm32.send_start_task()           # 开始任务
stm32.send_stop_task()            # 停止任务
stm32.send_pause_task()           # 暂停任务
stm32.send_resume_task()          # 恢复任务
stm32.send_motor_speed(1, 1000)    # 设置电机1速度
stm32.send_motor_position(2, 500)  # 设置电机2位置
stm32.send_servo_angle(1, 90)     # 设置舵机1角度
stm32.send_reset()                # 系统复位

# 断开连接
stm32.disconnect()
```

### 3.5 使用回调函数处理数据

```python
def on_sensor(data):
    print(f"传感器数据: 电机1位置={data['motor1_position']}")

def on_error(code):
    print(f"错误: 0x{code:02X}")

stm32 = STM32Communicator()
stm32.sensor_callback = on_sensor
stm32.error_callback = on_error
stm32.connect()
```

---

## 四、故障排除

### 4.1 串口无法连接

**问题**：提示 "串口连接失败"

**解决方法**：
1. 检查USB转串口是否正确连接
2. 检查STM32是否上电
3. 确认串口权限：`ls -l /dev/ttyUSB*`
4. 添加权限：`sudo chmod 666 /dev/ttyUSB0`
5. 检查串口是否被其他程序占用：`lsof /dev/ttyUSB0`

### 4.2 通信正常但无数据

**问题**：串口连接成功，但收不到STM32数据

**解决方法**：
1. 确认STM32程序已正确烧录
2. 检查波特率是否匹配（115200）
3. 检查TX/RX是否接反
4. 确认共地连接正确

### 4.3 YOLOv8加载失败

**问题**：提示 "未安装ultralytics库"

**解决方法**：
```bash
pip install ultralytics
```

如果速度慢，使用国内镜像：
```bash
pip install ultralytics -i https://pypi.tuna.tsinghua.edu.cn/simple
```

---

## 五、性能优化建议

### 5.1 YOLOv8模型选择

| 模型 | 参数量 | 速度 | 适用场景 |
|------|--------|------|----------|
| yolov8n.pt | 3.2M | 最快 | 实时控制 |
| yolov8s.pt | 11.2M | 快 | 通用 |
| yolov8m.pt | 25.9M | 中等 | 高精度 |

### 5.2 图像分辨率

在 `detect_scallion` 函数中，可以缩小输入图像以提高速度：
```python
# 缩小图像尺寸
frame_small = cv2.resize(frame, (320, 320))
results = self.yolo_model(frame_small)
```

### 5.3 串口缓冲区

如需提高通信效率，可减小接收超时时间：
```python
self.serial = serial.Serial(
    port=self.port,
    baudrate=115200,
    timeout=0.1  # 减小到100ms
)
```

---

## 六、安全注意事项

1. **电平匹配**：Jetson UART和STM32都是3.3V电平，可以直接连接
2. **共地**：确保两设备共地，否则可能导致通信异常
3. **急停**：STM32有急停开关，发生异常时立即按下
4. **权限**：串口权限设置仅在开发环境使用，生产环境请使用更安全的方案

---

## 七、技术支持

如遇问题，请检查：
1. 所有接线是否牢固
2. 串口权限是否正确
3. STM32程序是否正常运行（通过调试串口查看日志）
4. Python依赖是否完整安装

---

*文档版本：V2.0*
*更新日期：2026-03-23*
*适用硬件：Jetson Orin Nano + STM32F103VE*
