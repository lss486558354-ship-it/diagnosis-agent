"""中文技术文本分词器。

为什么不能直接用 jieba 默认切分:
    本语料是嵌入式技术文档，检索词大量是符号型术语 —— 命令码
    `CMD_PROCESS_START`、十六进制帧 `0x30`、引脚 `PA9`、波特率 `115200`、
    时长 `6s`。这类 token 一旦被切碎就再也匹配不上: 用户搜 `0x30` 时，
    索引里若只剩 `0` 和 `x30` 两个碎片，检索必然失败。

策略 —— 位置映射法:
    先扫描出术语在原文中的字符区间，再让 jieba 分词并返回每个词的区间，
    凡是落在术语区间内的碎片一律丢弃，术语本身按位置插回。
    这样既不需要改写原文（改写会引入不可靠的占位符），又能保证术语完整。

    曾尝试过「把术语替换成占位符再还原」，已废弃: 占位符在跨工具传递时
    容易被剥掉，还原必然失败。位置映射不依赖任何魔法字符串。
"""

from __future__ import annotations

import re
from functools import lru_cache

# ---------------------------------------------------------------- 术语保护

# 顺序有意义: 长模式在前，避免 0x30 被纯数字规则先吃掉
_TERM_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b0[xX][0-9A-Fa-f]+\b"),                      # 0x30 / 0xAA
    re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"),          # CMD_PROCESS_START
    re.compile(r"\b[A-Z]{2,}\d+[A-Z0-9]*\b"),                  # STM32F103VE / HX711
    re.compile(r"\bP[A-G]\d{1,2}\b"),                          # PA9 / PC11
    re.compile(r"\b\d+\.?\d*(?:ms|us|ns|min|kHz|MHz|Hz|kg|mm|cm|mA|mV|V|A|W|s|g|m|h)\b"),
    re.compile(r"\b\w+\.(?:c|h|py|json|md|txt|sh|service|uvprojx)\b"),
    re.compile(r"\b\d{3,}\b"),                                 # 115200 之类长数字
]

# 中文技术同义词 —— 用户口语与文档书面语不一致时靠它兜底。
# 只列真正高频且有歧义风险的，不做大而全的同义词表（那是另一种拍脑袋）。
_SYNONYMS: dict[str, str] = {
    "第一层": "首层",
    "底层": "首层",
    "翘边": "翘曲",
    "变形": "翘曲",
    "堵头": "堵料",
    "卡死": "堵转",
    "死机": "卡死",
    "不转": "堵转",
    "没反应": "无响应",
    "点不亮": "无响应",
    "上位机": "主机",
    "单片机": "stm32",
    "串口屏": "tjc",
    "步进电机": "步进",
    "限位": "限位开关",
    "波特率": "baud",
}

# 检索无意义的词。技术文档里「的」「了」等高频虚词会污染 BM25 权重。
_STOPWORDS: frozenset[str] = frozenset(
    """
    的 了 是 在 和 与 或 及 等 中 上 下 前 后 对 从 到 由 为 以 之 其 该 此 这 那
    有 无 不 也 都 就 而 但 并 若 则 可 能 会 要 需 应 将 被 把 给 让 使 于 按
    一个 一些 什么 怎么 如何 哪些 哪个 这个 那个 我们 你们 他们 自己
    可以 需要 应该 如果 因为 所以 但是 而且 然后 以及 通过 进行 使用 采用
    问题 情况 时候 地方 东西 方面 方式 方法 内容 部分 相关 有关 如下 所示
    """.split()
)

# 否定词 —— 必须与其后的词合并，绝不能当停用词单独丢掉。
#
# 为什么: "不动作" 若只剩 "动作"，它在索引里就和 "动作" 完全无法区分，
# 而这两个词在故障排查场景里语义相反 —— 「限位不响应」和「限位响应」
# 会被同一套 token 命中。实测后果是查询「膜杆不动」返回 0 条: 索引里
# 存的是「动作」，而 '动作' 里找不到子串「不动」。
# 合并成「不动作」之后，口语化的「不动」靠子串匹配就能命中。
_NEGATION_PREFIXES: frozenset[str] = frozenset({"不", "无", "没", "未", "非", "别"})


def _is_cjk(ch: str) -> bool:
    """判断是否为 CJK 统一表意文字。用码点比较，避免任何转义歧义。"""
    return 0x4E00 <= ord(ch) <= 0x9FFF


@lru_cache(maxsize=1)
def _get_jieba():
    """延迟加载 jieba 并注入本领域词典。

    首次调用会加载词典（约 1 秒），之后走缓存。
    领域词按项目文档中的实际术语补充 —— 这些词 jieba 默认词典里没有，
    不加会导致「丝杆」「限位开关」被切成无意义的碎片。
    """
    import jieba

    domain_words = [
        "丝杆", "同步带", "限位开关", "切割刀", "叶轮", "总线舵机", "舵机",
        "步进电机", "直流电机", "串口屏", "上位机", "主控", "叶菜", "小葱",
        "称重", "标定", "零漂", "堵转", "回抽", "空行程", "急停", "互斥锁",
        "环形缓冲", "中断服务", "状态机", "跳转规则", "边沿触发", "电平触发",
        "内网穿透", "心跳包", "像素标定", "录制回放", "灰度发布", "路径穿越",
        "帧头", "帧尾", "校验位", "半双工", "差分信号", "共地", "上拉电阻",
        "继电保护", "强电", "弱电", "接触器", "断路器", "热继电器", "熔断器",
        "首层", "翘曲", "拉丝", "层纹", "堵料", "挤出", "热床", "喷嘴",
        "切片", "支撑结构", "填充率", "回抽距离", "打印农场",
        # 2026-10-08 补漏: 「膜杆」是打包模块的核心执行部件，此前漏登记，
        # 被切成「膜」+「杆」两个单字，导致"膜杆不动"这类查询完全检索不到。
        "膜杆", "光电开关", "超声波", "传送带", "称重传感器", "蓝牙",
        "调试串口", "微动开关", "对射式",
    ]
    for w in domain_words:
        jieba.add_word(w, freq=2000)

    return jieba


# ---------------------------------------------------------------- 术语区间


def _find_term_spans(text: str) -> list[tuple[int, int, str]]:
    """扫描出所有符号型术语的字符区间，返回按位置排序且互不重叠的列表。

    重叠时保留最长匹配 —— 例如 `0x30` 与 `30` 都命中时保留 `0x30`。
    这是位置映射法的第一步: 先划定「不可被切碎的区间」。
    """
    found: list[tuple[int, int, str]] = []
    for pat in _TERM_PATTERNS:
        for m in pat.finditer(text):
            found.append((m.start(), m.end(), m.group(0)))

    # 起点升序、长度降序，贪心保留互不重叠的最长匹配
    found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    kept: list[tuple[int, int, str]] = []
    last_end = -1
    for s, e, tok in found:
        if s >= last_end:
            kept.append((s, e, tok))
            last_end = e
    return kept


def _in_spans(pos_start: int, pos_end: int, spans: list[tuple[int, int, str]]) -> bool:
    """判断 [pos_start, pos_end) 是否完全落在某个术语区间内。"""
    for s, e, _ in spans:
        if s <= pos_start and pos_end <= e:
            return True
        if s >= pos_end:          # spans 有序，可提前退出
            break
    return False


# ---------------------------------------------------------------- 分词


def normalize(token: str) -> str:
    """归一化单个 token。

    小写化 + 同义词映射。同义词表在归一化 *之后* 查 —— 英文缩写
    （USART2 → usart2）与中文同义词（限位 → 限位开关）走同一条路径。
    """
    t = token.strip().lower()
    return _SYNONYMS.get(t, t)


def _merge_negations(tokens: list[str]) -> list[str]:
    """把否定词与其后的 token 粘成一个。

    必须在停用词过滤**之前**调用 —— 合并出的「不动作」不在停用词表里，
    会被保留；而后面没有词的孤立「不」仍然是停用词，照常过滤掉。

    只处理紧邻形式。「没有动作」这类被 jieba 切成「没有」+「动作」的，
    「没有」不在合并表里，暂不覆盖 —— 这是已知边界，不是遗漏。
    """
    if not tokens:
        return []

    out: list[str] = []
    i = 0
    while i < len(tokens):
        cur = tokens[i]
        if cur in _NEGATION_PREFIXES and i + 1 < len(tokens):
            out.append(cur + tokens[i + 1])
            i += 2
        else:
            out.append(cur)
            i += 1
    return out


def tokenize(text: str, *, drop_stopwords: bool = True) -> list[str]:
    """把一段文本切成检索用 token 序列。

    使用 jieba 搜索引擎模式（mode='search'）—— 它为长词额外产出子词
    （「限位开关」→ 限位/开关/限位开关），提升召回。代价是索引略大，
    在本语料规模下完全可接受。
    """
    if not text or not text.strip():
        return []

    spans = _find_term_spans(text)
    jieba = _get_jieba()

    # (start, end, token) 双通道合并
    merged: list[tuple[int, int, str]] = list(spans)
    for word, s, e in jieba.tokenize(text, mode="search"):
        # 落在术语区间内的碎片直接丢弃 —— 术语会由 spans 通道提供
        if spans and _in_spans(s, e, spans):
            continue
        merged.append((s, e, word))

    merged.sort(key=lambda x: (x[0], x[1]))

    raw: list[str] = []
    for _, _, tok in merged:
        norm = normalize(tok)
        if not norm:
            continue
        # 丢弃纯标点与纯空白
        if not any(ch.isalnum() or _is_cjk(ch) for ch in norm):
            continue
        raw.append(norm)

    # 合并否定词，再对合并结果补一次同义词归一 ——
    # "没"+"反应" 合并出「没反应」，这条同义词键只在合并后才存在，
    # 不重查就会漏掉归一，与文档侧的「无响应」对不上。
    out = [normalize(t) for t in _merge_negations(raw)]

    if drop_stopwords:
        out = [t for t in out if t not in _STOPWORDS]

    return out


def tokenize_query(query: str) -> list[str]:
    """查询侧分词。与索引侧共用同一套逻辑，保证词表对齐。"""
    return tokenize(query, drop_stopwords=True)


if __name__ == "__main__":
    samples = [
        "启动工艺的命令 CMD_PROCESS_START 是 0x30，实测帧为 AA 30 00 9A",
        "USART2 波特率 115200，接上位机香橙派",
        "PA9 和 PA10 是 USART1 的调试串口",
        "丝杆反转 6s 超时后触发限位开关 E1",
        "STM32F103VE 的 HX711 称重模块通过 main.c 初始化",
    ]
    print("分词自检")
    print("=" * 72)
    for s in samples:
        print(f"\n原文: {s}")
        print(f"分词: {tokenize_query(s)}")

    print()
    print("=" * 72)
    print("术语完整性检查（这些 token 必须整体出现）:")
    critical = ["cmd_process_start", "0x30", "usart2", "115200", "pa9",
                "stm32f103ve", "hx711", "main.c", "6s"]
    joined = set(tokenize_query(" ".join(samples)))
    ok = 0
    for c in critical:
        hit = c in joined
        ok += hit
        print(f"  [{'OK  ' if hit else 'FAIL'}] {c}")
    print(f"\n  {ok}/{len(critical)} 通过")

    print()
    print("=" * 72)
    print("否定词语义检查（不能被退化成肯定形式）:")
    neg_cases = [
        ("膜杆第二节不动作", "不动作"),
        ("限位开关无响应", "无响应"),
        ("急停未触发", "未触发"),
        ("刀片不转", "堵转"),           # 走同义词路径: 不转 → 堵转
        ("电机没有动作", None),         # 已知不覆盖，仅展示当前行为
    ]
    neg_ok = neg_total = 0
    for text, expect in neg_cases:
        toks = tokenize_query(text)
        if expect is None:
            print(f"  [--  ] {text!r:20s} -> {toks}   (已知不覆盖)")
            continue
        neg_total += 1
        hit = expect in toks
        neg_ok += hit
        print(f"  [{'OK  ' if hit else 'FAIL'}] {text!r:20s} -> {toks}  期望含 {expect!r}")
    print(f"\n  {neg_ok}/{neg_total} 通过")

    print()
    print("=" * 72)
    print("口语 → 书面语 匹配验证（案例库用 LIKE 子串匹配，靠这个兜底）:")
    spoken = tokenize_query("膜杆不动")
    written = tokenize_query("膜杆第二节不动作")
    print(f"  口语「膜杆不动」       -> {spoken}")
    print(f"  书面「膜杆第二节不动作」 -> {written}")
    matched = [f"{s} ⊂ {w}" for s in spoken for w in written if s in w]
    print(f"  可对上的: {matched}")
