"""MCP server —— 把设备文档检索能力暴露成 Claude Code 可直接调用的工具。

为什么要有这一层:
    检索脚本只能人跑。做成 MCP 工具之后，Claude Code 在对话中就能
    自己决定"该查文档了"，把排查过程从「我搜完念给你听」变成
    「它自己搜、自己读、自己组织答案，并把来源标出来」。

6 个工具的分工:
    知识侧（回答"文档里怎么写的"）
        search_doc      —— 通用混合检索，不确定答案在哪时用它
        search_command  —— 命令码 / 协议帧，只在表格块里找
        get_wiring      —— 接线与引脚，只在表格块里找
        list_faults     —— 文档里记录了哪些故障章节（知识地图）
    证据侧（回答"我们实际遇到过什么"）
        get_case        —— 检索历史故障案例库
        record_case     —— 把新排查结果记进去

为什么 search_command / get_wiring 要单独拆出来:
    技术上它们就是 search_doc + 预设的表格过滤，没有新的检索能力。
    拆开的理由是 MCP 的工具描述本身就是给模型的提示 —— 用户问
    "PA9 接什么"，模型看到一个叫 get_wiring 的工具会直奔接线表，
    而只有一个 search_doc 时它得自己猜该搜什么词。
    这个假设**没有实测过**，评估阶段要验证：同一批问题下，
    6 工具与单工具的选对率差多少。如果没差别就该合并。

关于工作目录:
    MCP server 由客户端拉起，cwd 不确定，所以索引与数据库路径
    一律用 __file__ 定位，不用相对路径。
"""

from __future__ import annotations

import os
import threading

from mcp.server.mcpserver import MCPServer

from case_store import KIND_CHECKLIST, KIND_ROOT_CAUSE, CaseStore
from chunker import doc_outline
from retrieval.embedder import EmbeddingError
from retrieval.hybrid import HybridRetriever, SearchOutcome, evidence_note

SERVER_NAME = "equipment-diagnosis"
SERVER_VERSION = "0.1.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_DIR = os.path.join(BASE_DIR, "index")
CASE_DB = os.path.join(BASE_DIR, "data", "cases.db")
CORPUS_DIR = os.path.join(BASE_DIR, "corpus")

# 正文预览上限。表格给更长预算 —— 命令表和接线表的价值就在完整性，
# 截到一半等于没给。普通段落截断损失较小。
PREVIEW_CHARS = 420
TABLE_PREVIEW_CHARS = 1100

TABLE_KIND = "table"
# 单路取多少候选再做过滤。取得少会把表格块筛没，取得多则白算。
FILTER_POOL = 60

# 标题里出现这些词的小节，就是文档自带的故障内容。
#
# 为什么用标题匹配而不是语义检索: 检索器并不知道"我在找故障章节"这件事，
# 拿一个固定查询串去搜，结果会偏向某一个文档 —— 实测 4 条里 3 条来自同一份
# 继电保护设计，还混进了「添加:」这种被误当作标题的垃圾。
# 扫标题是确定性的，能列全，也不会漏。
#
# 为什么扫 corpus 而不是 index 里的 chunks: chunks 是**检索单元**，不是目录。
# 短于分块器阈值的叶子小节会被归并，title_path 按设计收敛到共同前缀，
# 小节名随之丢失 —— 实测该做法让「固件README > 8. 常见问题」等三整章故障
# 内容不可见，而它们是 10 条案例的来源章节。详见 chunker.doc_outline()。
FAULT_TITLE_MARKERS = ("故障", "排查", "常见问题", "异常", "注意事项", "错误", "问题")

# ---------------------------------------------------------------- 惰性单例
# 索引加载要读盘 + 建 BM25，放在模块顶层会让 MCP 客户端每次握手都等一遍。
#
# 为什么两个单例都必须加锁: MCP SDK 用线程池执行工具函数，"第一次调用"
# 完全可能同时来自几个线程。没有锁的话每个线程都看到 None，于是各建一个
# 实例 —— 而 CaseStore 的锁是**实例私有**的，多建一个实例就等于多一把
# 互不相干的锁，串行化当场失效。实测 6 线程并发首次调用 get_store()，
# 6/6 全部在 load_seed 里抛 UNIQUE 冲突 / database is locked。
# 详见 .backups/20261008_getstore竞态/BUG_DESCRIPTION.md。

_retriever: HybridRetriever | None = None
_store: CaseStore | None = None
_retriever_lock = threading.Lock()
_store_lock = threading.Lock()


def get_retriever() -> HybridRetriever:
    global _retriever
    with _retriever_lock:
        if _retriever is None:
            _retriever = HybridRetriever.load(INDEX_DIR)
        return _retriever


def get_store() -> CaseStore:
    global _store
    with _store_lock:
        if _store is None:
            # 用局部变量承载，初始化全部完成后再发布到全局。这样正确性
            # 就不依赖锁本身了 —— 即使将来锁被误删，也不会有人拿到
            # load_seed 只跑了一半的实例。
            store = CaseStore(CASE_DB)
            store.load_seed(os.path.join(BASE_DIR, "cases_seed.json"))
            _store = store
        return _store


# ---------------------------------------------------------------- 渲染

def _clip(text: str, limit: int) -> str:
    body = text.strip()
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + "\n…（已截断）"


def render_hits(results, title: str, note: str = "") -> str:
    """把检索结果渲染成 Markdown。

    每条都带来源路径 —— 这是本项目和"把文档塞给大模型"的核心区别:
    用户能顺着路径翻到原文验证，而不是只能选择信或不信。
    """
    if not results:
        return f"## {title}\n\n无结果。可以换个说法再试，或用 search_doc 做通用检索。"

    parts = [f"## {title}（{len(results)} 条）"]
    if note:
        parts.append(f"> {note}")

    for i, r in enumerate(results, 1):
        c = r.chunk
        kinds = "/".join(c.kinds) if c.kinds else "文本"
        limit = TABLE_PREVIEW_CHARS if TABLE_KIND in c.kinds else PREVIEW_CHARS
        weak = "　⚠ 弱证据（仅语义召回）" if r.is_weak else ""
        parts.append(
            f"\n### [{i}] {c.title_str}\n"
            f"- 来源：`{c.source}`\n"
            f"- 召回：{r.sources}　类型：{kinds}　{c.char_count} 字符{weak}\n"
            f"\n```\n{_clip(c.text, limit)}\n```"
        )
    return "\n".join(parts)


def render_gated(query: str, note: str) -> str:
    """门控拦截时的输出 —— 明说「没找到」。

    而不是把向量的最近邻包装成"检索到 N 条"递出去。带来源引用的幻觉
    比没有引用更危险: 用户看到「《流程汇报》3.1 节」会默认它被核实过，
    而它恰恰是编的。
    """
    return (
        f"## 检索「{query}」—— 无结果\n\n"
        f"{note}\n\n"
        "建议换一种说法再试。若确认文档里应该有，可以先用 list_faults "
        "看看有哪些相关章节，或改用 search_command / get_wiring 查"
        "命令码与接线。"
    )


def render_cases(hits, title: str) -> str:
    if not hits:
        return (
            f"## {title}\n\n"
            "案例库里没有匹配记录。这不代表没发生过 —— 只代表没人记录过。\n"
            "可以改用 search_doc 在技术文档里找设计层面的说明。"
        )

    parts = [f"## {title}（{len(hits)} 条）"]
    for i, (case, score) in enumerate(hits, 1):
        kind_label = "有确定根因" if case.is_root_cause else "排查清单"
        parts.append(
            f"\n### [{i}] {case.id}　{case.symptom}\n"
            f"- 模块：{case.module or '未分类'}　类型：{kind_label}　"
            f"匹配度：{score}　{'已校对' if case.verified else '待校对'}\n"
            f"- 来源：`{case.source}`\n"
            f"- **原因**：{case.cause}\n"
            f"- **解决**：{case.solution}"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------- 知识侧工具

server = MCPServer(
    name=SERVER_NAME,
    version=SERVER_VERSION,
    instructions=(
        "叶菜切割-清洗-打包一体机 与 3D 打印自动化系统 的设备技术知识库。\n"
        "包含两套设备的自有技术文档，以及从中提取的历史故障案例。\n\n"
        "建议的排查顺序：\n"
        "1. 先 get_case 查有没有遇到过类似故障（经验优先于文档）\n"
        "2. 再 search_doc / search_command / get_wiring 找文档依据\n"
        "3. 综合给出建议时，务必带上来源路径，让用户能自己核对原文"
    ),
)


@server.tool(
    description=(
        "在设备技术文档库中做混合检索（关键词 BM25 + 向量语义）。"
        "返回文档片段正文、来源文件与章节路径。"
        "当你不确定答案在哪份文档里时用它。"
        "若问题明确是查命令码或接线，用 search_command / get_wiring 更准。"
    )
)
def search_doc(query: str, top_k: int = 5) -> str:
    """通用文档检索。"""
    try:
        outcome = get_retriever().search_checked(query, top_k=top_k)
    except EmbeddingError as e:
        return f"检索失败：向量编码不可用。\n{e}"
    if outcome.gated:
        return render_gated(query, outcome.note)
    return render_hits(outcome.results, f"文档检索「{query}」", outcome.note)


def _search_tables(query: str, top_k: int) -> tuple[SearchOutcome, bool]:
    """只保留表格块。返回 (检索结论, 是否真的过滤过)。

    过滤后为空时降级返回未过滤结果，但同时告诉调用方"这不是表格"——
    静默降级会让模型以为拿到的是权威表格，那比空手更糟。
    """
    outcome = get_retriever().search_checked(query, top_k=FILTER_POOL)
    if outcome.gated:
        return outcome, False

    kept = [r for r in outcome.results if TABLE_KIND in r.chunk.kinds]
    final = (kept if kept else outcome.results)[:top_k]
    # 证据说明必须在裁剪之后重算 —— 拿 60 条算出来的计数去描述
    # 最终 2 条结果，模型会以为还有 58 条是它没看见的。
    return SearchOutcome(final, note=evidence_note(final)), bool(kept)


@server.tool(
    description=(
        "查询串口/通信命令码。已知命令名查十六进制值，或已知命令值查功能。"
        "例：「启动工艺的命令」「0x30 是什么」「OFFSET_RESET 的数据格式」。"
        "返回串口命令表中的原始行。"
    )
)
def search_command(query: str, top_k: int = 5) -> str:
    """查命令码，只在表格块中检索。"""
    try:
        outcome, filtered = _search_tables(query, top_k)
    except EmbeddingError as e:
        return f"检索失败：向量编码不可用。\n{e}"

    if outcome.gated:
        return render_gated(query, outcome.note)

    notes = []
    if not filtered:
        notes.append("未命中表格块，以下为普通段落结果 —— 命令码请以文档表格为准。")
    if outcome.note:
        notes.append(outcome.note)
    return render_hits(outcome.results, f"命令码检索「{query}」", " ".join(notes))


@server.tool(
    description=(
        "查询硬件接线：引脚分配、电机与驱动器连接、传感器接线、串口引脚。"
        "例：「步进电机 丝杆接哪个脚」「PA9」「限位开关」「HX711」。"
        "返回接线表中的原始行。"
    )
)
def get_wiring(query: str, top_k: int = 5) -> str:
    """查接线，只在表格块中检索。"""
    try:
        outcome, filtered = _search_tables(query, top_k)
    except EmbeddingError as e:
        return f"检索失败：向量编码不可用。\n{e}"

    if outcome.gated:
        return render_gated(query, outcome.note)

    notes = []
    if not filtered:
        notes.append("未命中表格块，以下为普通段落结果 —— 引脚请以接线表原文为准。")
    if outcome.note:
        notes.append(outcome.note)
    return render_hits(outcome.results, f"接线检索「{query}」", " ".join(notes))


@server.tool(
    description=(
        "列出文档中所有讲故障/排查/常见问题的小节，以及案例库的模块分布。"
        "用于了解「这台设备有哪些已知的坑」，不返回具体内容。"
        "拿到章节名后再用 search_doc 深入。module 可留空。"
    )
)
def list_faults(module: str = "", limit: int = 20) -> str:
    """故障知识地图 —— 扫源文档标题，不做检索。"""
    if not os.path.isdir(CORPUS_DIR):
        return (
            "## 文档目录不可用\n\n"
            f"找不到语料目录 `{CORPUS_DIR}`。\n"
            "本工具需要读源文档标题 —— 索引里的标题路径会因碎块归并丢掉"
            "小节名，拿它反推目录会把「确实没有故障章节」和「读不到源文件」"
            "混成同一个结果。"
        )

    sections: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for source, title in doc_outline(CORPUS_DIR):
        if not any(marker in title for marker in FAULT_TITLE_MARKERS):
            continue
        if module and module not in title and module not in source:
            continue
        key = (source, title)
        if key in seen:
            continue
        seen.add(key)
        sections.append((source, title))

    header = f"## 文档中的故障相关小节（{len(sections)} 个）"
    if not sections:
        header += "\n\n没有匹配的小节。可能 module 传得过窄，换个词或留空再试。"
    elif len(sections) > limit:
        header += f"\n> 只显示前 {limit} 个（共 {len(sections)} 个），按文档分组。"

    lines = [header]
    current = None
    for source, title in sections[:limit]:
        if source != current:
            lines.append(f"\n**{source}**")
            current = source
        lines.append(f"- {title}")

    store = get_store()
    stats = store.stats()
    dist = "　".join(f"{name} {n} 条" for name, n in store.modules())

    lines.append(f"\n## 案例库模块分布\n{dist or '（空）'}")
    lines.append(
        f"\n统计：{stats['总案例数']} 条案例，{stats['有确定根因']} 条有确定根因，"
        f"{stats['待校对']} 条待人工校对。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- 证据侧工具

@server.tool(
    description=(
        "检索历史故障案例库 —— 这些是从真实工程文档里提取的故障记录，"
        "含现象、原因、解决办法。用于回答「以前遇到过类似问题吗」。"
        "结果按症状匹配度排序，「有确定根因」的案例比「排查清单」更值得优先采信。"
    )
)
def get_case(query: str, module: str = "", limit: int = 5) -> str:
    """查历史案例。"""
    hits = get_store().search(query, module=module, limit=limit)
    title = f"历史案例「{query}」" + (f"（仅 {module} 模块）" if module else "")
    return render_cases(hits, title)


@server.tool(
    description=(
        "把一次新的故障排查结果记入案例库。"
        "请在用户确认了根因和解决办法之后再调用 —— 未经验证的猜测不该进库。"
        "kind 传 root_cause 表示已确定根因，传 checklist 表示只是排查方向。"
    )
)
def record_case(
    symptom: str,
    cause: str,
    solution: str,
    module: str = "",
    kind: str = KIND_CHECKLIST,
) -> str:
    """记录新案例。"""
    if kind not in (KIND_ROOT_CAUSE, KIND_CHECKLIST):
        kind = KIND_CHECKLIST

    try:
        case = get_store().add(
            symptom=symptom,
            cause=cause,
            solution=solution,
            module=module,
            kind=kind,
        )
    except ValueError as e:
        return f"记录失败：{e}"

    return (
        f"已记录为 **{case.id}**（模块：{case.module or '未分类'}）。\n"
        f"- 现象：{case.symptom}\n"
        f"- 原因：{case.cause}\n"
        f"- 解决：{case.solution}\n\n"
        f"标记为「待校对」—— 如果这条已经现场验证过，告诉我，我把它标成已校对。"
    )


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
