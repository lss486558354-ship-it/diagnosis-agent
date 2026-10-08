# 外设接线表（硬件 PWM 步进优化版 · 按模块分册 · VET6）

|大葱切割-清洗-打包一体机 — 引脚分配（步进六路全部硬件定时器 PWM，非步进外设保持可用）

> **版本**：V6.2-HWPWM-Modular | **更新日期**：2026-04-15 | **适用范围**：STM32F103VET6（F103xE，LQFP100）  
> **参考**：ST RM0008《STM32F101/102/103 参考手册》— 通用定时器 PWM、高级定时器 TIM1/TIM8；[STM32F103xE 数据手册](https://www.st.com/resource/en/datasheet/stm32f103ve.pdf) 表 "Alternate function mapping"。  
> **与 V6.1 关系**：电气定义与《外设接线表_硬件PWM步进优化版.md》**一致**；本文件**仅改变排版**：**按外设模块分节**，同一模块引脚在表中**连续列出**，便于画 PCB / 分连接器；原版中的「按端口总览」长列表在此**省略**，请以本文件各模块表为准。

---

## 设计目标与资料要点

1. **六路步进 PUL 全部使用片上定时器 PWM 引脚**（TIM1 / TIM2 / TIM3 / TIM5），每路独立定时器通道，便于独立调频、走步与中断统计；**不占用 TIM8 的 PC6~PC9**（留给直流电机 PWM）。
2. **TIM1 三路 PUL（步进1/5/6）须使用 `GPIO_FullRemap_TIM1`**：手册规定 **无重映射时** `TIM1_CH3/CH4` 在 PA10/PA11；**仅 FullRemap** 才能把 **CH3→PE13、CH4→PE14**。FullRemap 后 **CH1 固定在 PE9（不再是 PA8）**，固件见 `bsp_stepper.c`；I2C 等模块初始化后工程内会 **再次应用 FullRemap 并配置 PE9/PE13/PE14**，避免 AFIO 被改写后丝杆无脉冲。
3. **TIM1 为高级定时器**：与 TIM8 一样支持 **RCR 重复计数器**，若后续要做"硬件定脉冲数停止"，可优先在 TIM1 相关通道上扩展（需另行固件设计）。
4. **非步进功能**：USART2（Jetson）、USART3（屏）、UART4（总线舵机）、I2C1（OLED/MPU6050）、TIM8（四路直流 PWM）、HX711、限位与光电、继电器、调试串口等 **与 V5/V6 接线表一致**，仅对 **与步进/超声波冲突的引脚** 按优化版收敛。

---

## 相对《外设接线表.md》V5.0 的核心变更

| 项目 | V5.0 / 旧表易错写法 | 本优化版（与当前 `bsp_stepper` 固件一致） | 原因 |
|------|---------------------|------------------------------------------|------|
| 步进1 PUL | PA8，`TIM1_CH1`（无重映射） | **PE9，`TIM1_CH1`（FullRemap）** | 步进5/6 须在 **PE14/PE13**输出 `TIM1_CH4/CH3`，须 **FullRemap**；CH1 仅 **PE9** |
| 步进3 PUL | PB0，`TIM3_CH3` | **PA6，`TIM3_CH1`** | PB0 专作超声 **TRIG** |
| 步进5 PUL | PA11，`TIM1_CH4` | **PE14，`TIM1_CH4`** | PA11 常为 **USB_DM** |
| 步进6 PUL | PE13（V5 已修正） | **PE13，`TIM1_CH3`** | 与 V5 一致 |
| 超声波 TRIG | PB0（与步进3 争用） | **PB0，仅 GPIO TRIG** | 步进3 已迁出 PB0 |

> **固件同步**：`bsp_stepper.c` 启用 **`GPIO_FullRemap_TIM1`**；上电后在 MPU6050 等可能改写 AFIO 的模块之后调用 **`Stepper_ReapplyTim1RemapAndPulGpio()`**。

---

## 一、按模块列出的引脚与接线

以下各小节为 **PCB 友好顺序**：同一外设的所有 MCU 引脚排在同一张表内，相邻行便于画原理图符号与接口封装。

---

### 1.1 模块 — 人机接口（按键、指示灯）

**固件**：`main.c`（`GPIO_Init_All`）、板载器件。

| 信号 | STM32 引脚 | 方向 / 模式 | 说明 |
|------|------------|-------------|------|
| 流程开始键 | **PC1** | 输入，内部上拉 | 按下接地，启动一次完整流程 |
| 电机3/4 自检复位键 | **PC5** | 输入，内部上拉 | 按下接地，清零自检标志，可重做每方向 2.5s 正/反转自检 |
| 板载 LED | **PC13** | 输出，推挽 | 低电平亮（常见蓝丸板） |

---

### 1.2 模块 — 六路步进电机（硬件 PWM）

**固件**：`bsp_stepper.c` / `bsp_stepper.h`。

| 电机 | 功能 | STM32 引脚 | 定时器 / 通道 | 驱动器（参考） | 备注 |
|------|------|------------|---------------|----------------|------|
| 步进1 同步带A | PUL | **PE9** | TIM1_CH1 | DMA860H | **须 FullRemap TIM1**；TIM1 三路 PUL 的 CC 输出由固件按通道切换 |
| 步进1 | DIR | **PC0** | — | — | |
| 步进2 同步带B | PUL | **PA0** | TIM2_CH1 | DMA860H | |
| 步进2 | DIR | **PC2** | — | — | |
| 步进3 传送带 | PUL | **PA6** | TIM3_CH1 | DM422 | 释放 PB0 |
| 步进3 | DIR | **PC4** | — | — | |
| 步进4 打包 | PUL | **PA1** | TIM5_CH2 | DM422 | |
| 步进4 | DIR | **PC12** | — | — | |
| 步进5 丝杆A | PUL | **PE14** | TIM1_CH4 | DM542 | 避免 PA11/USB_DM |
| 步进5 | DIR | **PD2** | — | — | |
| 步进6 丝杆B | PUL | **PE13** | TIM1_CH3 | DM542 | 无 FSMC 占用时推荐 |
| 步进6 | DIR | **PD10** | — | — | |

**PCB 建议**：每颗驱动器旁布置 **PUL、DIR、GND、信号地**；TIM1 三路 PUL（PE9/PE13/PE14）可共用同一排针区域。

---

### 1.3 模块 — USART1（调试串口 / HX711 命令）

**固件**：`bsp_usart_obj.c`（`UART_InitAll`）。

| 信号 | STM32 引脚 | 说明 |
|------|------------|------|
| TX | **PA9** | USART1_TX |
| RX | **PA10** | USART1_RX |

---

### 1.4 模块 — USART2（Jetson 等）

| 信号 | STM32 引脚 | 说明 |
|------|------------|------|
| TX | **PA2** | USART2_TX → 对端 RX |
| RX | **PA3** | USART2_RX ← 对端 TX |

---

### 1.5 模块 — USART3（串口屏）

| 信号 | STM32 引脚 | 说明 |
|------|------------|------|
| TX | **PB10** | USART3_TX |
| RX | **PB11** | USART3_RX |

---

### 1.6 模块 — UART4（飞特 SCS / URT-1）

| 信号 | STM32 引脚 | 说明 |
|------|------------|------|
| TX | **PC10** | UART4_TX；接 URT-1 时与丝印核对 |
| RX | **PC11** | UART4_RX；全双工时 `UART4_SCS_HALF_DUPLEX=0`（默认） |

---

### 1.7 模块 — HX711 称重

**固件**：`bsp_hx711.c`。

| 信号 | STM32 引脚 | 说明 |
|------|------------|------|
| SCK | **PA4** | 输出 |
| DOUT | **PA5** | 输入 |

---

### 1.8 模块 — I2C1（MPU6050 / OLED）

**固件**：`bsp_mpu6050.c` 上电 **先尝试** PB8/PB9（重映射），再试 PB6/PB7；**单板只应实现其中一套**，勿两套同时焊接造成总线冲突。

| 方案 | SCL | SDA | 说明 |
|------|-----|-----|------|
| A（软件优先探测） | **PB8** | **PB9** | `GPIO_Remap_I2C1` |
| B（备选） | **PB6** | **PB7** | I2C1 默认脚，与部分 OLED 接线表一致 |

---

### 1.9 模块 — 超声波 HC-SR04

| 功能 | STM32 引脚 | 说明 |
|------|------------|------|
| TRIG（两路共用） | **PB0** | GPIO 输出，分时触发 |
| ECHO 超声1 | **PD3** | 输入 |
| ECHO 超声2 | **PD4** | 输入 |

---

### 1.10 模块 — TIM8 直流减速电机 PWM

| 功能 | STM32 引脚 | 定时器 |
|------|------------|--------|
| DCM1 PWM（切割） | **PC9** | TIM8_CH4 |
| DCM2 PWM（水泵） | **PC8** | TIM8_CH3 |
| DCM3 PWM（空压机） | **PC7** | TIM8_CH2 |
| DCM4 PWM | **PC6** | TIM8_CH1 |

---

### 1.11 模块 — 继电器

| 信号 | STM32 引脚 | 说明 |
|------|------------|------|
| K_AIR | **PB12** | GPIO 输出 |
| K_PUMP | **PB13** | GPIO 输出 |
| K_S | **PA15** | GPIO 输出；须 **关闭 JTAG、仅保留 SWD** |

---

### 1.12 模块 — 限位 / 光电 / 急停

与《外设接线表.md》V5.0 一致：**PE0～PE7** 等用于限位、光电、急停；直流电机方向脚等见 `bsp_dc_motor_obj` / 原 V5 第三章，**勿与 PE9（步进1 PUL）冲突**。

---

### 1.13 定时器资源一览（步进 + 直流）

| 定时器 | 类型 | 本方案用途 |
|--------|------|------------|
| TIM1 | 高级 | 步进1（CH1@PE9）、步进6（CH3@PE13）、步进5（CH4@PE14），**FullRemap** |
| TIM2 | 通用 | 步进2（CH1@PA0） |
| TIM3 | 通用 | 步进3（CH1@PA6） |
| TIM5 | 通用 | 步进4（CH2@PA1） |
| TIM8 | 高级 | DCM1～4 PWM（PC6～PC9） |

---

## 二、快速检查清单（按模块）

- **人机**：PC1 开始、PC5 自检复位、PC13 LED  
- **步进1**：PE9 / PC0（**勿用 PA8**）  
- **步进2**：PA0 / PC2  
- **步进3**：PA6 / PC4（PB0 仅超声 TRIG）  
- **步进4**：PA1 / PC12  
- **步进5**：PE14 / PD2（**勿用 PA11 作 PUL**）  
- **步进6**：PE13 / PD10  
- **USART1**：PA9 / PA10  
- **USART2**：PA2 / PA3  
- **USART3**：PB10 / PB11  
- **UART4**：PC10 / PC11  
- **HX711**：PA4 / PA5  
- **I2C1**：仅焊 PB8/PB9 **或** PB6/PB7 一套  
- **超声**：PB0、PD3、PD4  
- **TIM8**：PC6～PC9  
- **继电器**：PB12、PB13、PA15（SWD-only）  

---

## 三、工程注意事项

1. **TIM1 FullRemap 与步进1**：步进5/6 使用 PE13/PE14 时，步进1 PUL **必须接 PE9**，不可接 PA8；丝杆正常而同步带 A 不动时先查 PE9。  
2. **I2C1 与 TIM4**：PB8/PB9 兼为 TIM4_CH3/CH4；I2C 重映射到 PB8/PB9 时，**勿再**在同脚启用 TIM4。  
3. **PE13/PE14 与 FSMC**：使用外部总线/大屏时核对是否占用 PE13、PE14。  
4. **PA15 JTAG**：K_S 使用 PA15 时需 `GPIO_Remap_SWJ_JTAGDisable`，仅保留 SWD。  
5. **ADC 与 PB0**：若将 PB0 作 ADC1_IN8，与 **PB0 TRIG** 冲突；本方案以超声与步进 PWM 为优先。

---

## 四、文档修订记录

| 日期 | 版本 | 说明 |
|------|------|------|
| 2026-04-15 | V6.2-HWPWM-Modular | **按模块重排**引脚表，与 V6.1 电气内容一致；增加 PC1/PC5 人机接口；便于 PCB 分组布线 |
| 2026-04-15 | V6.1-HWPWM | 见《外设接线表_硬件PWM步进优化版.md》：PE9 步进1、检查清单与 TIM1 说明 |
| 2026-04-14 | V6.0-HWPWM | 六路硬件 PWM、步进3→PA6、步进5→PE14、PB0 TRIG |

---

## 五、打包测试分区时序（参考固件 V3.0）

以下时序对应 `process_control.c` 中 **分区D 打包子状态机** `Process_Pack_Zone_SM_Run()` 的完整流程，参数来自 `process_control.h` 中的默认值定义。

### 5.1 时序图

```
时间轴 →        0s              2s (pack_align_reverse_ms)       打包阶段（动态）
               |                |                                  |
PACK_ZONE_IDLE    PACK_ZONE_ALIGN_REV (M3反转对齐)         PACK_ZONE_FEED_FWD
               |                |                                  |
               ├────────────────┼──────────────────────────────────┤
               |                |                                  |
               |                |       ┌── 缠膜 2圈（wrap_start）──┐
               |                |       │  M3 传送带 CW            │
               |                |       │  叶轮 ID3/8 夹入转动     │
               |                |       └─────────────────────────┘
               |                |                       ↓ (2×M34_BOOT_FWD_MS=416ms)
               |                |              ┌─── 膜杆抽离 (rod_pull) ──┐
               |                |              │   ID2/7 轮模式           │
               |                |              │   第一节 leg=1: 417ms    │
               |                |              │   第二节 leg=2: 417ms   │
               |                |              └─────────────────────────┘
               |                |                                  ↓
               |                |       ┌─── 切膜 + 缠葱尾 (wrap_tail) ──┐
               |                |       │  M3 CW + DCM4 切膜电机正2s反2s │
               |                |       │  超声+红外双重检测葱尾离开       │
               |                |       └───────────────────────────────┘
               |                |                                  ↓
               |                |                        PACK_ZONE_EJECT
               |                |              ┌─── 抛出 (eject) ──┐
               |                |              │  叶轮 ID3/8 抛出    │
               |                |              │  运行 4000ms       │
               |                |              └───────────────────┘
               |                |                                  ↓
               |                |                                 完成
```

> `M3` = 步进4（打包传送带，`PA1` TIM5_CH2 / `PC12` DIR）；`DCM4` = 切膜直流电机（`PC8` TIM8_CH3）。

### 5.2 子状态机状态流转

| 固件状态名 | 触发条件 | 执行动作 | 关键参数 |
|------------|----------|----------|----------|
| `PACK_ZONE_IDLE` | 进入分区D | 复位所有标志；叶轮 ID3/8 使能扭矩；M3/DCM4 停止 | — |
| `PACK_ZONE_ALIGN_REV` | 立即进入 | **M3 反转 2s** 对齐大葱位置 | `pack_align_reverse_ms` = 2000ms |
| `PACK_ZONE_FEED_FWD` | ALIGN_REV 到达 | **M3 正转 + 叶轮 ID3/8 夹入转动** | `conveyor_speed_hz` = 2000Hz |
| `PACK_ZONE_WAIT_PACK` | 立即进入 | 等超声波 / 红外检测葱头 | `pack_dist_head` = 55mm / 红外 |
| `PACK_ZONE_WRAP_START` | 检测到葱头 | **M3 CW + 叶轮夹入**；定时缠 **2圈** | `2 × pack_m3_ms_per_rev` = 2 × 208 = **416ms** |
| `PACK_ZONE_ROD_PULL` | 2圈到达 | **膜杆 ID2/7 轮模式**，分两节定时 | `pack_scs_rod_goal_time` = 417ms/节 × 2 |
| `PACK_ZONE_WRAP_TAIL` | 膜杆到位 | **M3 CW + DCM4 切膜电机正2s/反2s**；等葱尾超声+红外离开 | `pack_film_cw_ms`=2000, `pack_film_ccw_ms`=2000 |
| `PACK_ZONE_EJECT` | 葱尾离开 | **叶轮 ID3/8 抛出** | `pack_impeller_eject_ms` = **4000ms** |
| `PACK_ZONE_DONE` | 超时或完成 | 停止所有设备 | — |

### 5.3 核心参数速查

来源于 `process_control.h`：

```c
// 打包圈定时（以 M3 步进转一圈的 ms 数为基准）
#define M34_BOOT_FWD_MS            208u   // M3 一圈时长 ms（实测标定）
#define DFLT_PACK_M3_MS_PER_REV    M34_BOOT_FWD_MS

// 膜杆轮模式定时
#define DFLT_PACK_ROD_GOAL_TIME    834u   // 每节 0.834s，写 0 速后下一节
#define DFLT_PACK_ROD_GOAL_SPEED   4095u   // 轮速幅值（4095 = 最大速度）
#define DFLT_PACK_ROD_ID2_PULL     512u    // ID2 抽出端位置
#define DFLT_PACK_ROD_ID2_PUSH     2560u   // ID2 推入端位置
#define DFLT_PACK_ROD_ID7_PULL     512u
#define DFLT_PACK_ROD_ID7_PUSH     2560u

// 叶轮（ID3=叶轮A，ID8=叶轮B；ID8速度自动取反实现对转）
#define DFLT_PACK_IMPELLER_SPD     3000u   // 夹入速度
#define DFLT_PACK_IMPELLER_ACC     40u     // 加减速
#define DFLT_PACK_IMPELLER_EJECT_MS 6000u  // 抛出持续时间
```

### 5.4 舵机总线ID 与固件接口

| 固件ID | 功能 | 总线协议 | 调用接口 |
|--------|------|----------|----------|
| ID 3（`PACK_SCS_IMPELLER_ID_A`） | 叶轮A（夹入/抛出） | SCS，UART4 | `pack_scs_impeller_clamp_run()` / `pack_scs_impeller_eject_run()` |
| ID 8（`PACK_SCS_IMPELLER_ID_B`） | 叶轮B（对转） | SCS，UART4 | 同上，速度自动取反 |
| ID 2（`PACK_SCS_ROD_ID_A`） | 膜杆A（抽/推） | SCS，UART4 | `pack_scs_rod_wheel_run_both(sp2, sp7)` |
| ID 7（`PACK_SCS_ROD_ID_B`） | 膜杆B（对转） | SCS，UART4 | 同上 |

轮模式驱动核心（`bsp_servo_serial.c`）：

```c
// 进入轮模式：设置角限位为0，模式寄存器写1
void ServoBus_STS_EnterWheelMode(uint8_t scs_id);

// 设置轮速（int16_t，有符号16位）
void ServoBus_SetWheelSpeedId(uint8_t scs_id, int16_t speed);

// 设置扭矩开关（1=使能，0=关闭）
void ServoBus_SetTorqueSwitch(uint8_t scs_id, uint8_t on);
```

### 5.5 打包测试分区的固件入口

`process_control.h` 中定义了台架调试宏，置 1 时上电直接进入打包分区：

```c
#define PROCESS_TEST_BOOT_DIRECT_PACK  1   // 上电直接启动打包子状态机
```

调试时确保此宏为 1，完整产线应设为 0 走正常流程。

### 5.6 与原始描述的对应关系

| 原始描述 | 固件对应 |
|----------|----------|
| "上电" | `PACK_ZONE_IDLE`（初始化） |
| "舵机3和8转动" | `pack_scs_impeller_clamp_run()` / `pack_scs_impeller_eject_run()` |
| "电机4转" | `Stepper_RunAtSpeed(STEPPER_CONVEY, ...)` + `DCM4_Run()` |
| "舵机2和7半圈 × 2" | `pack_scs_rod_wheel_run_both()` × 2节，每节 417ms（总 ≈ 834ms） |
| "打包圈时间宏 × 2" | `PACK_ZONE_WRAP_START` 中 `2 × pack_m3_ms_per_rev`（默认 416ms） |
| "舵机3和8停" | `pack_scs_impeller_stop()` |
| "电机4停" | `Stepper_Stop(STEPPER_CONVEY)` + `DCM4_Stop()` |

> **注**：固件中「打包圈时间」由 `pack_m3_ms_per_rev`（M3 一圈实测 208ms）决定，`× 2` 圈 ≈ 416ms，并非固定 2s；`pack_wrap_time_ms`（默认 2000ms）在当前固件状态机中未被直接使用，仅供参考。

---

*作者：工程训练中心116 电控组（沿用 V5/V6 署名风格）*  
*固件版本：process_control.c V3.0 / 2026-04-18；总线舵机协议见《飞特总线舵机_单片机通讯方法.md》*
