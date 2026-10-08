"""评估数据集 —— 检索 30 题 / 案例库 14 条 / 门控负样本 10 条。

怎么判"答对了":
    每题带一组 `expect` 特征串，Top-K 里任一结果**包含其中任意一个**即算命中。
    特征串一律选**唯一性高的标识符**，不选泛词:
        用 `CMD_PROCESS_START`（枚举名，全文只出现在命令表那一行）
        不用 `0x30`（命令表里到处都是，命中了也说明不了任何事）
        用 `PE3` / `PARAM_PACK_MODE`，也不用「舵机」「参数」这类满篇都是的词。
    这样"对不对"就不依赖我的主观判断 —— 你逐条复核时，只要在语料里
    搜一下那个串在不在，就能验证 ground truth 本身有没有写错。

    `run_eval.py` 启动时会**自检**：任何 `expect` 串若在整个语料里搜不到，
    直接报错退出。防止我把 ground truth 写成语料里根本不存在的东西
    （那就成了"所有方案都答不对"的假失败）。

必须诚实标注的局限:
    这 30 题是我**看着语料**出的，而且我知道 BM25 怎么分词、门控怎么判。
    写题时会不自觉地避开已知的弱点 —— 存在选择性偏差。
    所以结果的正确读法是「**下限参考**」：真实使用中只会更差，不会更好。
"""

from __future__ import annotations

from typing import NamedTuple


class RetrievalQuestion(NamedTuple):
    """一道检索评估题。

    doc 字段是"答案应该在哪份文档"，只用于报告分组与人工复核，
    不参与判定 —— 判定只看 expect 串在不在结果里。
    """

    q: str
    expect: tuple[str, ...]
    doc: str
    tag: str


# ---------------------------------------------------------------- 检索 30 题
# 覆盖 16 份文档中的 14 份。分类配比按"实际会被问到什么"来定:
# 命令码和参数 ID 是查得最频繁的，故障排查是价值最高的。

RETRIEVAL_QUESTIONS: tuple[RetrievalQuestion, ...] = (
    # ---- 命令码（8）----
    RetrievalQuestion("启动工艺流程要发哪个命令", ("CMD_PROCESS_START",), "串口命令汇总表.md", "命令码"),
    RetrievalQuestion("急停用哪个命令", ("CMD_ALL_STOP",), "串口命令汇总表.md", "命令码"),
    RetrievalQuestion("改工艺参数用哪个命令", ("CMD_SET_PARAM",), "串口命令汇总表.md", "命令码"),
    RetrievalQuestion("怎么读当前工艺走到哪一步了", ("Process_GetPhase",), "串口命令汇总表.md", "命令码"),
    RetrievalQuestion("直流电机启动命令是什么", ("CMD_DCM_START",), "串口命令汇总表.md", "命令码"),
    RetrievalQuestion("步进电机怎么停下来", ("CMD_STEPPER_STOP",), "串口命令汇总表.md", "命令码"),
    RetrievalQuestion("带方向的调速命令", ("CMD_SET_SPEED_DIR",), "串口命令汇总表.md", "命令码"),
    RetrievalQuestion("超声波测距测试命令", ("CMD_ULTRASONIC_TEST",), "串口命令汇总表.md", "命令码"),

    # ---- 接线引脚（6）----
    RetrievalQuestion("丝杆下限位开关接哪个脚", ("screw_down_limit", "PE3"), "串口命令速查表.md", "接线"),
    RetrievalQuestion("急停按钮检测用哪个引脚", ("PE6",), "继电保护设计.md", "接线"),
    RetrievalQuestion("飞特总线舵机接哪两个脚", ("PC10",), "外设接线表.md", "接线"),
    RetrievalQuestion("超声波模块 TRIG 接哪个引脚", ("PB0",), "外设接线表.md", "接线"),
    RetrievalQuestion("HX711 称重模块接哪两个脚", ("PA4", "PA5"), "外设接线表.md", "接线"),
    RetrievalQuestion("OLED 屏走哪个 I2C", ("I2C1",), "外设接线表.md", "接线"),

    # ---- 故障排查（6）----
    RetrievalQuestion("膜杆第二节不动作怎么办", ("PACK_ROD_ON_2", "g_pack_test_seg_ms"), "流程汇报_20260423.md", "故障"),
    RetrievalQuestion("电机速度自己变得越来越快", ("OFFSET_ADJUST",), "上位机通信协议.md", "故障"),
    RetrievalQuestion("命令帧被拒绝说校验和不匹配", ("校验和",), "上位机通信协议.md", "故障"),
    RetrievalQuestion("串口收到的数据老丢帧", ("缓冲区",), "固件README.md", "故障"),
    RetrievalQuestion("USB-TTL 接 URT-1 之后舵机没反应", ("Micro USB",), "飞特总线舵机通讯方法.md", "故障"),
    RetrievalQuestion("舵机抖动是什么原因", ("50Hz",), "固件README.md", "故障"),

    # ---- 工艺参数（5）----
    RetrievalQuestion("打包切断舵机的角度是哪个参数", ("PARAM_PACK_SERVO_CUT",), "串口命令汇总表.md", "参数"),
    RetrievalQuestion("称重目标克数的参数 ID", ("PARAM_WEIGHT_TARGET_G",), "串口命令汇总表.md", "参数"),
    RetrievalQuestion("丝杆速度是哪个参数", ("PARAM_SCREW_SPEED_HZ",), "串口命令汇总表.md", "参数"),
    RetrievalQuestion("打包模式参数 ID 是多少", ("PARAM_PACK_MODE",), "串口命令汇总表.md", "参数"),
    RetrievalQuestion("称重达标要连续保持多久", ("PARAM_WEIGHT_STABLE_MS",), "串口命令汇总表.md", "参数"),

    # ---- 综合（5）----
    RetrievalQuestion("0xAA 帧的校验怎么算", ("0xAA ^ CMD",), "串口命令汇总表.md", "综合"),
    RetrievalQuestion("ACK 状态码 0x03 表示什么", ("ACK_BUSY",), "串口命令汇总表.md", "综合"),
    RetrievalQuestion("参数 0x2B 是干什么的", ("PARAM_PROCESS_PLAN",), "串口命令汇总表.md", "综合"),
    RetrievalQuestion("Jetson 通过哪个串口和单片机通信", ("USART2",), "外设接线表.md", "综合"),
    RetrievalQuestion("3D 打印机项目为什么必须切到 Xorg 会话", ("xdotool",), "GUIDE.md", "综合"),
)


# ---------------------------------------------------------------- 案例库 14 条
# 覆盖全部 14 条 `root_cause` 案例 —— 只取有确定根因的，清单类（checklist）
# 没有唯一答案，拿它算 Top-3 命中率会人为压低分数。
#
# 查询一律**换词**，不直接用案例的 symptom 原文 —— 用原文等于把答案
# 抄进题目里，测不出任何东西。换词能否命中，考验的是同义词表
# 和 search_text 的归一化是否真的管用。

class CaseQuestion(NamedTuple):
    q: str
    expect_id: str


CASE_QUERIES: tuple[CaseQuestion, ...] = (
    CaseQuestion("打包的时候膜杆后半截不动", "CASE-001"),
    CaseQuestion("电机转速越来越快，跟设定对不上", "CASE-002"),
    CaseQuestion("发的命令被退回来，说校验不对", "CASE-004"),
    CaseQuestion("按了急停但是机器没断电", "CASE-010"),
    CaseQuestion("手一松启动键设备就掉电", "CASE-011"),
    CaseQuestion("按启动键机器一点反应都没有", "CASE-012"),
    CaseQuestion("机器跑着跑着自己断电了", "CASE-014"),
    CaseQuestion("换了单独的 USB 转串口模块接 URT-1，舵机就不动了", "CASE-017"),
    CaseQuestion("串口收到的数据老是丢", "CASE-018"),
    CaseQuestion("急停之后舵机还在转", "CASE-019"),
    CaseQuestion("直流电机一点不动，PWM 没输出", "CASE-020"),
    CaseQuestion("像素监测里鼠标坐标显示要装 xdotool", "CASE-021"),
    CaseQuestion("像素监测框了区域但颜色一直是黑的", "CASE-023"),
    CaseQuestion("提示缺 ultralytics，YOLO 起不来", "CASE-028"),
)


# ---------------------------------------------------------------- 门控负样本
# 全是**这个知识库根本不覆盖**的领域。门控的作用就是让检索器在这些
# 问题上说"库里没有"，而不是拿向量路的最近邻硬凑。
#
# 为什么用这些领域: 与语料里的"机电设备"有一定语义距离，但又不是
# 完全无关（否则向量路也够不着，测不出门控有没有用）。冰箱、洗衣机
# 这类家电与"切割机/舵机"共享"电机""控制板"等概念，是最容易骗过
# 纯向量检索的那一类。
#
# 为什么必须拆成两组:
#   门控的判据是"多字词在全库零命中"。如果查询里**碰巧**有个词出现在
#   语料里（哪怕概念完全无关），门控按设计就**不会**拦 —— 它无法区分
#   "库里确实没这个概念"和"用户用词跟文档不一样"。
#   把这两类混在一起算拦截率，得到的是一个既不能说明门控有效、
#   也不能说明门控失效的混合数字。分开统计才看得清能力边界在哪。
#   分组依据是**实测**（逐词跑 BM25 零命中检查），不是我的直觉。

# A 组：与语料零词汇重叠 —— 门控**应该**拦下，拦不住才是缺陷。
GATE_OFF_DOMAIN: tuple[str, ...] = (
    "冰箱不制冷怎么修",
    "洗衣机脱水时剧烈晃动",
    "空调外机噪音大",
    "电梯困人怎么救援",
    "路由器连不上网",
    "热水器打不着火",
    "咖啡机不出水怎么办",
    "跑步机跑带打滑",
    "微波炉不加热",
    "共享单车锁打不开",
)

# B 组：概念不在库里，但**用词与语料有重叠** —— 门控按设计不拦，是已知边界。
# 这组恰恰是最危险的：系统会返回一堆带来源引用的无关内容，
# 而"打印机"三个字真的出现在语料里（corpus/printer 是一个 3D 打印机项目），
# 引用看起来完全站得住脚。
GATE_OVERLAP: tuple[str, ...] = (
    "汽车发动机异响",          # 「汽车」「发动」在库中命中
    "手机屏幕碎了换屏多少钱",   # 库中有"串口屏""触摸屏"，「屏幕」命中
    "打印机卡纸",              # 库中有 3D 打印机项目，「打印机」命中
    "笔记本电池鼓包",          # 「笔记」「电池」命中
)
