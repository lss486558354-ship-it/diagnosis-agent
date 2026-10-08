# 淘晶驰 T1 串口屏 ↔ STM32 事件与 0xAA 协议对照

本文档说明各控件应绑定的**事件**及发往单片机的**完整帧**（十六进制）。协议与 [`User/protocol/protocol.h`](User/protocol/protocol.h)、[`串口命令汇总表.md`](串口命令汇总表.md) 一致。屏端书写语法见 [淘晶驰资料中心](http://wiki.tjc1688.com/index.html)。

**物理连接**：屏 TTL 串口接 STM32 **USART3（PB10=TX屏RX，PB11=RX屏TX）**，波特率 **115200**，8N1。

**帧格式**：`[0xAA][CMD][LEN][DATA…][CHK]`  
**校验**：`CHK = 0xAA ^ CMD ^ LEN ^ DATA[0] ^ … ^ DATA[N-1]`（逐字节异或）。

**屏上发送十六进制**：在按钮/页面/滑块的用户事件中用 `printh`（或当前上位机版本等效的十六进制发送指令）按顺序打出下列各字节。以下表格中「完整帧」已含校验字节。

---

## 1. 常用完整帧速查（可直接对照 `printh` 顺序）

| 说明 | 十六进制（空格分隔） |
|------|----------------------|
| 启动工艺 CMD_PROCESS_START，LEN=0 | `AA 30 00 9A` |
| 停止工艺 CMD_PROCESS_STOP | `AA 31 00 9B` |
| 暂停 CMD_PROCESS_PAUSE | `AA 32 00 9C` |
| 继续 CMD_PROCESS_RESUME | `AA 33 00 9D` |
| 急停 CMD_ALL_STOP | `AA 2F 00 85` |
| 清洗仅气泵 `CMD_SET_CLEAN_MODE` data=`01` | `AA 34 01 01 9E` |
| 清洗仅水泵 data=`02` | `AA 34 01 02 9D` |
| 清洗气+水 data=`03` | `AA 34 01 03 9C` |
| 打包全部 `CMD_SET_PACK_MODE` data=`01` | `AA 35 01 01 9F` |
| 打包中间 data=`02` | `AA 35 01 02 9C` |
| 打包头尾 data=`03` | `AA 35 01 03 9D` |
| 测高+丝杆 **开** `SET_PARAM 0x2A` val=1 | `AA 36 03 2A 00 01 B4` |
| 测高+丝杆 **关** val=0 | `AA 36 03 2A 00 00 B5` |
| 方案=称重 `SET_PARAM 0x2B` val=0 | `AA 36 03 2B 00 00 B4` |
| 方案=数量 `SET_PARAM 0x2B` val=1 | `AA 36 03 2B 00 01 B5` |
| 目标根数=3 `SET_PARAM 0x2C` val=3 | `AA 36 03 2C 00 03 B0` |
| 称重目标=250g `SET_PARAM 0x0A` val=250 | `AA 36 03 0A 00 FA 6F` |

校验计算示例（启动帧）：`0xAA ^ 0x30 ^ 0x00 = 0x9A`。

---

## 2. 控件与事件绑定（建议命名仅供参考）

| 建议控件名 | 类型 | 事件 | 发送内容 |
|------------|------|------|----------|
| `btn_start` | 按钮 | **弹起（Touch Release）** | 帧：`AA 30 00 9A` |
| `btn_pause` | 按钮 | **按下** | `AA 32 00 9C` |
| `btn_resume` | 按钮 | **按下** | `AA 33 00 9D` |
| 或使用双态 `sw_pause` | 双态开关 | **按下** | `if(sw_pause.val==0){发暂停}` `else{发继续}`（逻辑以你屏语法为准） |
| `btn_estop` | 按钮 | **按下** | `AA 2F 00 85` |
| `sw_height_screw` | 双态（默认=开） | **状态改变**或弹起 | val=1 发 `AA 36 03 2A 00 01 B4`；val=0 发 `AA 36 03 2A 00 00 B5` |
| `btn_clean_air` | 按钮 | 按下 | `AA 34 01 01 9E` |
| `btn_clean_water` | 按钮 | 按下 | `AA 34 01 02 9D` |
| `btn_clean_both` | 按钮 | 按下 | `AA 34 01 03 9C` |
| `btn_pack_full` | 按钮 | 按下 | `AA 35 01 01 9F` |
| `btn_pack_mid` | 按钮 | 按下 | `AA 35 01 02 9C` |
| `btn_pack_ht` | 按钮 | 按下 | `AA 35 01 03 9D` |

---

## 3. 称重方案页（静态变量 + 三次下发）

**需求**：进入页面发一次、滑块调整后发一次、离开页面再发一次，保证 MCU 收到最新目标重量。

1. **全局/跨页变量**：在淘晶驰编辑器中将滑块数值或中间变量设为**全局**（跨页保留，具体菜单以官方「控件详解」为准），例如 `va_weight_slot` 保存档位 0~6。
2. **档位 → 目标克数（中值）**：`slot0→150g … slot 6→750g`，公式：`target_g = 150 + slot * 100`（对应区间 100–200 … 700–800 的中值）。
3. **页面装入（Page Load / 页面打开）**：`printh` 发送 `SET_PARAM 0x2B` 称重方案（`2B 00 00`）；再发 `SET_PARAM 0x0A` 当前档位对应 `target_g`（16 位大端）。
4. **滑块 Touch Release（或值改变事件）**：同上两条帧（方案 + 0x0A）。
5. **页面离开（Leave / 切换页面前）**：再次发送同上两条帧。

单条 `CMD_SET_PARAM` 格式：`AA 36 03 [ID] [VH] [VL] [CHK]`，其中 `CHK = 0xAA ^ 0x36 ^ 0x03 ^ ID ^ VH ^ VL`。

---

## 4. 数量方案页（1～7 根）

1. 页面装入 / 滑块弹起 / 页面离开：**均建议发送两条**：  
   - `SET_PARAM 0x2B` val=**1**（数量方案）：`AA 36 03 2B 00 01 B5`  
   - `SET_PARAM 0x2C` val=**N**（1~7）：例 N=5：`AA 36 03 2C 00 05 B6`（其它 N 请用 `0xAA^0x36^0x03^0x2C^0x00^N` 计算末字节）。
2. 根数由滑块 1～7 映射；若屏上为 0～6，事件里 `cov n0.val+1,1` 再组帧。

---

## 5. ACK（屏端可选解析）

部分命令应答：`CMD' = 原命令 + 0x80`，LEN=2，数据 `[状态][上下文]`。例如 `0x30` 成功应答头为 `AA B0 02 ...`。调试阶段可忽略，仅用发帧确认。

---

## 6. 与固件行为对应摘要

| 参数 ID | 含义 |
|---------|------|
| 0x2A | 非 0：测高 + 丝杆；0：两阶段跳过，进带后直接切割准备 |
| 0x2B | 0：称重达标进打包；1：传送带上超声 **近→远** 脉冲计数达标进打包 |
| 0x2C | 数量方案目标根数 1～7（与 `pack_dist_head`/`pack_dist_tail` 阈值配合） |

---

## 7. 淘晶驰书写语法与可直接粘贴的事件代码

以下写法符合官方文档：[书写语法目录](http://wiki.tjc1688.com/grammar/index.html)、[printh（十六进制发送）](http://wiki.tjc1688.com/commands/printh.html)、[跨页/全局变量](http://wiki.tjc1688.com/grammar/global_variable.html)、[if逻辑](http://wiki.tjc1688.com/grammar/hmi_logic.html)。

**语法要点（务必遵守，否则编译报错）**

- `printh` 每项为**两位**十六进制，**空格分隔**，可大小写：`printh aa 30 00 9a`（见 [printh 说明](http://wiki.tjc1688.com/commands/printh.html)）。
- 事件里**不要**在运算符两侧、`if` 括号内等处加**多余空格**（见 [赋值操作](http://wiki.tjc1688.com/grammar/assignment_operation.html)）。
- `if` 内**不要**写运算表达式，先赋给中间变量再判断（见 [HMI 逻辑语句](http://wiki.tjc1688.com/grammar/hmi_logic.html)）。
- 跨页保留滑块值：把数字控件 **`vscope` 设为「全局」**，名字会变黑；跨页读写用 `页面名.控件.属性`（见 [全局变量](http://wiki.tjc1688.com/grammar/global_variable.html)）。

**约定页面名（可在上位机里改成你的实际页面名）**

| 页面 ID | 用途 |
|---------|------|
| `main` | 主操作：启动/暂停继续/急停/测高丝杆/清洗/跳转子页 |
| `weight` | 称重档滑块 `h0`，范围建议 0～6 |
| `qty` | 根数滑块 `h1`，范围建议 1～7 |
| `pack` | 打包方式三按钮 |

---

### 7.1 主页面 `main`

**控件 `m_btn_start`（按钮）— 弹起事件（Touch Release）**

```text
printh aa 30 00 9a
```

**控件 `m_btn_estop`（按钮）— 按下事件**

```text
printh aa 2f 00 85
```

**控件 `m_sw_pause`（双态按钮，0=暂停态 / 1=继续态 可按你布局反相）— 按下事件**

```text
if(m_sw_pause.val==1)
{
printh aa 33 00 9d
}else
{
printh aa 32 00 9c
}
```

**控件 `m_sw_height`（双态，1=测高+丝杆开，0=关）— 状态变化或弹起**

```text
if(m_sw_height.val==1)
{
printh aa 36 03 2a 00 01 b4
}else
{
printh aa 36 03 2a 00 00 b5
}
```

**清洗三键 — 按下**

```text
// 仅气泵 m_btn_clean_air
printh aa 34 01 01 9e
```

```text
// 仅水泵 m_btn_clean_water
printh aa 34 01 02 9d
```

```text
// 气+水 m_btn_clean_both
printh aa 34 01 03 9c
```

**跳转称重页 / 数量页 / 打包页（按钮按下）**

```text
page weight
```

```text
page qty
```

```text
page pack
```

---

### 7.2 打包页 `pack`（三键按下）

```text
// 全部打包 b_pack_full
printh aa 35 01 01 9f
```

```text
// 打包中间 b_pack_mid
printh aa 35 01 02 9c
```

```text
// 打包头尾 b_pack_ht
printh aa 35 01 03 9d
```

---

### 7.3 称重页 `weight`：滑块 `h0`（0～6 档，目标克数 150～750）

在 **`h0` 的「页面加载」「弹起」「页面离开」** 三个事件里调用同一段（或复制下面整段）。  
先发方案=称重，再发 `0x0A` 目标重量（已与固件校验一致）。

```text
printh aa 36 03 2b 00 00 b4
if(h0.val==0)
{
printh aa 36 03 0a 00 96 03
}else if(h0.val==1)
{
printh aa 36 03 0a 00 fa 6f
}else if(h0.val==2)
{
printh aa 36 03 0a 01 5e ca
}else if(h0.val==3)
{
printh aa 36 03 0a 01 c2 56
}else if(h0.val==4)
{
printh aa 36 03 0a 02 26 b1
}else if(h0.val==5)
{
printh aa 36 03 0a 02 8a 1d
}else if(h0.val==6)
{
printh aa 36 03 0a 02 ee 79
}
```

---

### 7.4 数量页 `qty`：滑块 `h1`（1～7 根）

在 **`h1` 的「页面加载」「弹起」「页面离开」** 中复用下列整段。

```text
printh aa 36 03 2b 00 01 b5
if(h1.val==1)
{
printh aa 36 03 2c 00 01 b2
}else if(h1.val==2)
{
printh aa 36 03 2c 00 02 b1
}else if(h1.val==3)
{
printh aa 36 03 2c 00 03 b0
}else if(h1.val==4)
{
printh aa 36 03 2c 00 04 b7
}else if(h1.val==5)
{
printh aa 36 03 2c 00 05 b6
}else if(h1.val==6)
{
printh aa 36 03 2c 00 06 b5
}else if(h1.val==7)
{
printh aa 36 03 2c 00 07 b4
}
```

若滑块硬件只能是 0～6表示 1～7 根，可在事件开头增加：`h1.val++`（注意最大值勿超过 7，可先 `if(h1.val<7)` 再自增，逻辑按你控件调整）。

---

### 7.5 跨页保存滑块（可选）

将 `weight.h0`、`qty.h1` 设为**全局**后，在 `main` 上显示可写：

```text
covx weight.h0.val,t_main_slot.txt,0,0
```

（需增加文本控件 `t_main_slot`；`covx` 见 [赋值操作](http://wiki.tjc1688.com/grammar/assignment_operation.html) 与指令说明。）

---

## 8. 与你当前工程对应的代码（页面名随意，按功能对齐）

下列页面、控件名来自你提供的上位机截图；若个别对象名不一致，只改**对象名**即可，**帧内容不要改**。

| 页面名 | 功能 |
|--------|------|
| `button` | 启动 / 暂停·继续 / 急停 |
| `key` | 测高+丝杆双态、跳转清洗/打包菜单 |
| `text` | 清洗：仅气 / 仅水 / 气+水 |
| `slide` | 入口：称重设置、计数设置、打包选择 |
| `guide` | 称重区间1～7（100～200g … 700～800g） |
| `box` | 数量 1～7 根 |
| `other` | 打包：全部 / 中间 / 头尾 |

---

### 8.1 页面 `button`

**`b0`「启动」— 弹起事件**

```text
printh aa 30 00 9a
```

**`b1`「暂停」— 弹起事件（单按钮交替：暂停 / 继续）**  
请先放一个**数字/变量类控件**（例如 `va0`），`vscope` 设为**全局**，初值 `0`。若用别的名字，把下面 `va0` 全部换成你的控件名。

```text
if(va0.val==0)
{
va0.val=1
printh aa 32 00 9c
}else
{
va0.val=0
printh aa 33 00 9d
}
```

若你更希望**两个独立按钮**分别暂停、继续，则一个按钮只写 `printh aa 32 00 9c`，另一个只写 `printh aa 33 00 9d`。

**`b2`「急停」— 弹起事件（或按下事件均可）**

```text
printh aa 2f 00 85
```

---

### 8.2 页面 `key`

**`bt0`「测高设置」双态 — 弹起事件（或状态变化事件）**  
`val==1`：测高+丝杆开；`val==0`：关（与固件 `0x2A` 一致）。

```text
if(bt0.val==1)
{
printh aa 36 03 2a 00 01 b4
}else
{
printh aa 36 03 2a 00 00 b5
}
```

**`b0`「清洗设置」— 弹起事件 →进清洗页**

```text
page text
```

**`b1`「打包设置」— 弹起事件 → 进打包菜单页**

```text
page slide
```

**`b2`「了解更多」—** 按需 `page` 到说明页或留空。

---

### 8.3 页面 `text`（清洗三键）

均在 **弹起事件**中写（与截图一致）。

**`b0` 仅气泵**

```text
printh aa 34 01 01 9e
```

**`b1` 仅水泵**

```text
printh aa 34 01 02 9d
```

**`b2` 气泵加水泵**

```text
printh aa 34 01 03 9c
```

---

### 8.4 页面 `slide`（三个入口）

**`b0`「称重设置」— 弹起事件**

```text
page guide
```

**`b1`「计数设置」— 弹起事件**

```text
page box
```

**`b2`「打包选择」— 弹起事件**

```text
page other
```

---

### 8.5 页面 `guide`（重量选择，滑块假设为 `h0`，刻度1～7）

要求与固件一致：**进入发一次、拖动后弹起发一次、离开页面再发一次**。  
下列整段复制到 **`guide` 的「后初始化事件」**、**`h0` 的弹起事件**、**`guide` 的「页面离开事件」**（三处代码相同）。

若你的称重滑块**不是** `h0`，把下面所有 `h0` 改成实际对象名。  
若滑块实际是 **0～6**，请改用本文 **§7.3** 那一组 `if(h0.val==0)`…`6` 的分支。

```text
printh aa 36 03 2b 00 00 b4
if(h0.val==1)
{
printh aa 36 03 0a 00 96 03
}else if(h0.val==2)
{
printh aa 36 03 0a 00 fa 6f
}else if(h0.val==3)
{
printh aa 36 03 0a 01 5e ca
}else if(h0.val==4)
{
printh aa 36 03 0a 01 c2 56
}else if(h0.val==5)
{
printh aa 36 03 0a 02 26 b1
}else if(h0.val==6)
{
printh aa 36 03 0a 02 8a 1d
}else if(h0.val==7)
{
printh aa 36 03 0a 02 ee 79
}
```

**跨页保留档位**：把 `guide.h0` 的 `vscope` 设为**全局**（参见 [全局变量](http://wiki.tjc1688.com/grammar/global_variable.html)）；你画面上的 `ba0`/`ba1` 若用于存数，也可在滑块事件里 `ba0.val=h0.val` 再配合全局属性使用。

---

### 8.6 页面 `box`（数量 1～7，滑块 `h0`）

同样三处：**`box` 后初始化**、**`h0` 弹起**、**`box` 页面离开**。

```text
printh aa 36 03 2b 00 01 b5
if(h0.val==1)
{
printh aa 36 03 2c 00 01 b2
}else if(h0.val==2)
{
printh aa 36 03 2c 00 02 b1
}else if(h0.val==3)
{
printh aa 36 03 2c 00 03 b0
}else if(h0.val==4)
{
printh aa 36 03 2c 00 04 b7
}else if(h0.val==5)
{
printh aa 36 03 2c 00 05 b6
}else if(h0.val==6)
{
printh aa 36 03 2c 00 06 b5
}else if(h0.val==7)
{
printh aa 36 03 2c 00 07 b4
}
```

---

### 8.7 页面 `other`（打包方式三键，弹起事件）

**`b0` 全部打包**

```text
printh aa 35 01 01 9f
```

**`b1` 仅打包中间**

```text
printh aa 35 01 02 9c
```

**`b2` 仅打包头尾**

```text
printh aa 35 01 03 9d
```

---

*若 `printh` 语法随上位机版本变化，以字节序列为准在串口助手验证后再写入屏事件。*
