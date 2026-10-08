"""结构化 Markdown 分块器。

设计动机（为什么不按固定字数切）:
    常见的 512 字定长切分在这个语料上会碎掉两类关键知识:
      1. 命令表被拦腰切断 —— "CMD_PROCESS_START 是 0x30" 和帧格式
         "AA 30 00 9A" 落在两块里，任何一块单独看都答不出问题。
      2. 代码块与它的说明分离 —— 孤立的 "AA 30 00 9A" 零信息量。
    本语料 876 行表格、158 个代码块，这两类正是知识密度最高的部分。
    所以改为: 按标题层级切 + 表格/代码块原子保护。

分块规则:
    主切分   —— h1/h2/h3 标题是天然语义边界，按标题路径聚合
    原子保护 —— 表格整块保留（超长也不切）; 代码块与其所属段落绑定
    超长处理 —— 仅对纯段落部分二次切分，绝不动表格与代码块
    碎块合并 —— 小于 MIN_CHARS 的叶子块向上并入父级，减少检索碎片
    标题路径 —— 每块携带 ["上位机通信协议", "命令码", "工艺控制"] 这样的
                路径，使引用能精确指向文档位置，而非笼统说"文档里提到"

用法:
    python chunker.py                    # 分块并打印统计报告
    python chunker.py --dump 5           # 额外打印前 5 块内容供人工检查
    python chunker.py --out chunks/      # 输出 JSONL
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from typing import Literal

# ---------------------------------------------------------------- 参数

MIN_CHARS = 180     # 低于此值的叶子块尝试向上合并
TINY_CHARS = 40     # 低于此值视为碎片，即使无共同父级也强行归并
MAX_CHARS = 1400    # 高于此值的块做二次切分（表格代码块除外）
OVERLAP_CHARS = 80  # 二次切分时相邻片段保留的重叠长度

CORPUS_DIR = "corpus"

BlockKind = Literal["heading", "table", "code", "para"]

# ---------------------------------------------------------------- 数据结构


@dataclass
class Block:
    """文档中的一个原子结构单元。"""

    kind: BlockKind
    text: str
    level: int = 0          # 仅 heading 使用，1=h1 / 2=h2 / 3=h3
    atomic: bool = False    # True = 不可再切（表格 / 代码块）
    line_no: int = 0        # 起始行号（1-based）。仅 code 块填 —— 围栏告警要能指到具体行


@dataclass
class Chunk:
    """检索的最小单元。"""

    id: str
    source: str
    title_path: list[str]
    text: str
    kinds: list[str] = field(default_factory=list)  # 该块含哪些结构类型

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def title_str(self) -> str:
        return " > ".join(self.title_path) if self.title_path else "(无标题)"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["char_count"] = self.char_count
        d["title_str"] = self.title_str
        return d


# ---------------------------------------------------------------- 解析


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")   # 水平分隔线，无检索价值


def parse_blocks(text: str) -> list[Block]:
    """把 Markdown 文本解析成 Block 序列。

    识别四类结构: 标题 / 围栏代码块 / 表格 / 普通段落。
    表格与代码块标记为 atomic —— 后续任何切分都不会拆开它们。
    """
    lines = text.split("\n")
    blocks: list[Block] = []
    buf: list[str] = []
    i = 0

    def flush_para() -> None:
        """把累积的普通行收成一个段落块。"""
        if not buf:
            return
        body = "\n".join(buf).strip()
        if body:
            blocks.append(Block(kind="para", text=body))
        buf.clear()

    while i < len(lines):
        line = lines[i]

        # --- 围栏代码块: 从开围栏吃到闭围栏，整体原子 ---
        fence = _FENCE_RE.match(line)
        if fence:
            flush_para()
            marker = fence.group(1)
            start_line = i + 1
            code = [line]
            i += 1
            while i < len(lines):
                code.append(lines[i])
                if _FENCE_RE.match(lines[i]) and lines[i].strip().startswith(marker):
                    i += 1
                    break
                i += 1
            blocks.append(
                Block(kind="code", text="\n".join(code), atomic=True, line_no=start_line)
            )
            continue

        # --- 标题 ---
        h = _HEADING_RE.match(line)
        if h:
            flush_para()
            blocks.append(
                Block(kind="heading", text=h.group(2).strip(), level=len(h.group(1)))
            )
            i += 1
            continue

        # --- 表格: 连续以 | 开头的行视为一张表，整体原子 ---
        if line.lstrip().startswith("|"):
            flush_para()
            tbl = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                tbl.append(lines[i])
                i += 1
            blocks.append(Block(kind="table", text="\n".join(tbl), atomic=True))
            continue

        # --- 水平分隔线: 直接丢弃 ---
        # 本语料里 --- 用于分节装饰，本身零语义。若当成段落会产出
        # 一堆 3 字符的块，检索时白占 top-k 名额。
        if _HR_RE.match(line):
            flush_para()
            i += 1
            continue

        # --- 普通行 ---
        if line.strip():
            buf.append(line)
        else:
            flush_para()
        i += 1

    flush_para()
    return blocks


# ---------------------------------------------------------------- 围栏体检

# Markdown ATX 标题。只认 2 级及以上 —— shell 与 python 的注释都是单层 `#`，
# 连单层一起查会把每一个代码注释都报成问题，告警立刻变成噪音。
_ATX_IN_CODE_RE = re.compile(r"^#{2,6}\s")


def audit_code_blocks(blocks: list[Block]) -> list[str]:
    """检查代码块内容里是否混进了 markdown 标题，返回告警消息列表。

    这是围栏配对错位的**唯一可靠信号**。
    文档里只要多出一个孤立的 ``` ，配对就会从那里整体反转: 真标题被吞进
    代码块、代码注释漏到外面被当成标题，索引的引用路径随之全线失真。
    整个过程完全静默 —— 索引照建、检索照跑，只是指向错误的位置。
    实测 corpus/printer/GUIDE.md 因 L271 一个多余围栏触发 27 次，
    代价是「9. 故障排查」整章在索引里根本不存在。

    只告警，不自动修正: 判定"哪个围栏是多余的"必须依赖启发式，
    而启发式会误伤文档里合法内嵌 markdown 示例的场景。把问题暴露给人，
    比悄悄改写解析结果可靠 —— 静默失败正是这个 bug 最毒的地方。
    """
    msgs: list[str] = []
    for b in blocks:
        if b.kind != "code":
            continue
        inner = [l.strip() for l in b.text.split("\n") if _ATX_IN_CODE_RE.match(l)]
        if not inner:
            continue
        shown = "、".join(t[:24] for t in inner[:3])
        more = f" 等 {len(inner)} 处" if len(inner) > 3 else ""
        msgs.append(
            f"L{b.line_no} 起的代码块内含 markdown 标题（{shown}{more}）"
            f" —— 围栏可能配对错位，检查该处 ``` 是否成对"
        )
    return msgs


def audit_fences(text: str, source: str = "") -> list[str]:
    """对单份文档做围栏体检，告警带上文件名。"""
    prefix = f"{source}: " if source else ""
    return [prefix + m for m in audit_code_blocks(parse_blocks(text))]


# ---------------------------------------------------------------- 分块


def _split_long_text(text: str, limit: int, overlap: int) -> list[str]:
    """把过长的纯文本按段落边界切开，保留少量重叠。

    仅用于不含表格/代码块的段落文本 —— 调用方需保证这一点。
    """
    paras = [p for p in text.split("\n\n") if p.strip()]
    if len(paras) <= 1:
        # 没有空行分隔，退化为按行累积
        paras = [p for p in text.split("\n") if p.strip()]

    out: list[str] = []
    cur: list[str] = []
    cur_len = 0

    for p in paras:
        # 单个段落就超限且无法再分时，硬切（保证不丢内容）
        if len(p) > limit and not cur:
            for s in range(0, len(p), limit - overlap):
                out.append(p[s : s + limit])
            continue
        if cur_len + len(p) > limit and cur:
            out.append("\n\n".join(cur))
            # 保留尾部作为下一段的重叠上下文
            tail = cur[-1][-overlap:] if len(cur[-1]) > overlap else cur[-1]
            cur = [tail]
            cur_len = len(tail)
        cur.append(p)
        cur_len += len(p)

    if cur:
        out.append("\n\n".join(cur))
    return [c for c in out if c.strip()]


def _group_parts(parts: list[Block], limit: int) -> list[list[Block]]:
    """按顺序把 Block 分组成不超过 limit 的批次，原子块绝不拆开。

    这是替代「整块文本二次切分」的做法。区别很关键:
    表格与代码块作为整体被分配到某一组，组与组的边界只会落在它们
    *之间*，不会落在它们内部。单个原子块自身超过 limit 时独占一组
    —— 宁可超限，也不把一张命令表切成两半。
    """
    groups: list[list[Block]] = []
    cur: list[Block] = []
    cur_len = 0

    for b in parts:
        blen = len(b.text)
        if cur and cur_len + blen > limit:
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(b)
        cur_len += blen

    if cur:
        groups.append(cur)
    return groups


def _common_prefix(a: list[str], b: list[str]) -> list[str]:
    """两个标题路径的共同前缀。用于碎块向上归并时收敛引用路径。"""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return a[:n]


def chunk_document(text: str, source: str, start_index: int = 0) -> list[Chunk]:
    """把单份文档切成 Chunk 列表。

    线性扫描 Block 序列，遇到标题就结束当前块并更新标题路径栈。
    表格与代码块不参与二次切分 —— 这是本分块器的核心约束。
    """
    blocks = parse_blocks(text)
    chunks: list[Chunk] = []

    path: list[str] = []          # 标题路径栈（按 level 对齐）
    parts: list[Block] = []       # 当前累积的内容块
    kinds: list[str] = []
    idx = start_index

    def flush() -> None:
        """把当前累积内容落成一个或多个 Chunk。

        超长时按 Block 分组切开 —— 表格与代码块作为整体落在某一组内，
        组边界只出现在它们之间。纯段落组若仍超限，再按段落边界二次
        切分并保留重叠。这样既控制了块长，又不牺牲结构完整性。
        """
        nonlocal idx, parts, kinds
        if not parts:
            return

        def emit(piece: str, piece_kinds: list[str]) -> None:
            nonlocal idx
            if not piece.strip():
                return
            idx += 1
            chunks.append(
                Chunk(
                    id=f"{source}::{idx}",
                    source=source,
                    # 无标题文档（如纯文本厂商手册）没有层级可依，
                    # 退回用文件名作路径 —— 至少让引用有明确出处，
                    # 而不是含糊的「(无标题)」。
                    title_path=list(path) or [os.path.basename(source)],
                    text=piece,
                    kinds=piece_kinds,
                )
            )

        body = "\n\n".join(b.text for b in parts).strip()
        if not body:
            parts, kinds = [], []
            return

        if len(body) <= MAX_CHARS:
            emit(body, sorted(set(kinds)))
        else:
            for group in _group_parts(parts, MAX_CHARS):
                gtext = "\n\n".join(b.text for b in group).strip()
                gkinds = sorted({b.kind for b in group})
                if len(gtext) > MAX_CHARS and not any(b.atomic for b in group):
                    # 纯段落组仍超限 —— 按段落边界再切，保留重叠
                    for piece in _split_long_text(gtext, MAX_CHARS, OVERLAP_CHARS):
                        emit(piece, gkinds)
                else:
                    emit(gtext, gkinds)

        parts, kinds = [], []

    for b in blocks:
        if b.kind == "heading":
            flush()
            # 维护标题路径栈: 同级或更深层级的下标全部截断
            depth = min(b.level, 3)
            path = path[: depth - 1]
            path.append(b.text)
            continue

        parts.append(b)
        if b.kind not in kinds:
            kinds.append(b.kind)

    flush()

    # --- 碎块合并: 过小的块向前归并到共同父级 ---
    # 合并条件是共享至少一级标题，而非路径完全相同。这一点很关键:
    # 本语料里大量叶子小节只有一两句话（如「丝杆超时设为 6s」），
    # 要求路径相同会把它们全留成碎片，检索时 top-k 被碎片占满。
    # 归并后引用路径收敛到共同前缀，正好反映"这块内容横跨了几个子节"。
    merged: list[Chunk] = []
    for c in chunks:
        if not merged or c.char_count >= MIN_CHARS:
            merged.append(c)
            continue

        prev = merged[-1]
        if prev.char_count + c.char_count > MAX_CHARS:
            merged.append(c)
            continue

        prefix = _common_prefix(prev.title_path, c.title_path)
        if prefix:
            # 同源小节 —— 归并，引用路径收敛到共同前缀
            prev.text = prev.text + "\n\n" + c.text
            prev.kinds = sorted(set(prev.kinds) | set(c.kinds))
            prev.title_path = prefix
        elif c.char_count < TINY_CHARS:
            # 无共同父级但内容极短（如孤立的 `which xdotool`）——
            # 仍并入前一块，保留前一块更精确的路径。这类碎片单独成块
            # 没有任何检索价值，只会白占 top-k 名额。
            prev.text = prev.text + "\n\n" + c.text
            prev.kinds = sorted(set(prev.kinds) | set(c.kinds))
        else:
            merged.append(c)

    # 合并会留下 id 空洞，重新编号保持连续
    counters: dict[str, int] = {}
    for c in merged:
        counters[c.source] = counters.get(c.source, 0) + 1
        c.id = f"{c.source}::{counters[c.source]}"

    return merged


# ---------------------------------------------------------------- 报告


def _iter_corpus(root: str) -> list[tuple[str, str]]:
    """遍历语料目录，返回 (相对路径, 文本) 列表。"""
    out: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in sorted(filenames):
            if not fn.lower().endswith((".md", ".txt")):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            try:
                out.append((rel, open(full, encoding="utf-8", errors="ignore").read()))
            except OSError as e:
                print(f"  [WARN] 读取失败 {rel}: {e}", file=sys.stderr)
    return sorted(out)


def build_chunks(corpus_dir: str = CORPUS_DIR) -> list[Chunk]:
    """对整个语料目录分块，顺带做围栏体检。

    体检结果走 stderr，不混进正常报表 —— 语料有缺陷时用户该看见，
    但不该污染 stdout 的结构化输出。
    """
    chunks: list[Chunk] = []
    for rel, text in _iter_corpus(corpus_dir):
        for msg in audit_fences(text, rel):
            print(f"  [WARN] {msg}", file=sys.stderr)
        chunks.extend(chunk_document(text, rel))
    return chunks


def doc_outline(corpus_dir: str = CORPUS_DIR) -> list[tuple[str, str]]:
    """扫描语料目录，返回每份文档的完整标题目录。

    返回 [(源文件, "根 > 章 > 节"), ...]，按文档与标题出现顺序排列。

    为什么不复用 chunk_document 的 title_path:
        那是**检索单元**的路径，不是目录。为了控制粒度，短于 MIN_CHARS 的
        叶子小节会被归并，归并时 title_path 按设计收敛到共同前缀，小节名
        随之丢失。实测「固件README > 8. 常见问题」整章因此从索引里消失，
        而它正是 CASE-006~009 四条案例的来源章节。
        目录问题就该读目录 —— 直接扫标题，不受分块策略影响。
    """
    outline: list[tuple[str, str]] = []
    for rel, text in _iter_corpus(corpus_dir):
        path: list[str] = []
        for b in parse_blocks(text):
            if b.kind != "heading":
                continue
            depth = min(b.level, 3)
            path = path[: depth - 1] + [b.text]
            outline.append((rel, " > ".join(path)))
    return outline


def print_report(chunks: list[Chunk]) -> None:
    """打印分块质量报告 —— 这是判断分块器是否合格的主要依据。"""
    if not chunks:
        print("没有切出任何块，检查语料目录。")
        return

    n = len(chunks)
    sizes = sorted(c.char_count for c in chunks)
    total = sum(sizes)

    print("=" * 74)
    print("分块质量报告")
    print("=" * 74)
    print(f"  块总数        {n}")
    print(f"  总字符        {total:,}")
    print(f"  平均块长      {total / n:.0f} 字符")
    print(f"  中位块长      {sizes[n // 2]} 字符")
    print(f"  最短 / 最长   {sizes[0]} / {sizes[-1]} 字符")

    too_small = sum(1 for s in sizes if s < MIN_CHARS)
    too_big = sum(1 for s in sizes if s > MAX_CHARS)
    print(f"  偏小 (<{MIN_CHARS})   {too_small} 块 ({too_small / n * 100:.1f}%)")
    print(f"  偏大 (>{MAX_CHARS})   {too_big} 块 ({too_big / n * 100:.1f}%)")

    # 结构保留情况 —— 表格与代码块是否被完整保留
    with_table = sum(1 for c in chunks if "table" in c.kinds)
    with_code = sum(1 for c in chunks if "code" in c.kinds)
    print(f"  含表格的块    {with_table}")
    print(f"  含代码的块    {with_code}")

    print()
    print("  按来源分布:")
    per_source: dict[str, list[int]] = {}
    for c in chunks:
        per_source.setdefault(c.source, []).append(c.char_count)
    for src in sorted(per_source):
        cs = per_source[src]
        print(f"    {src:44s} {len(cs):3d} 块  {sum(cs):6d} 字符")

    print()
    print("  标题路径样例（前 12 条，检查层级是否正确）:")
    for c in chunks[:12]:
        print(f"    [{c.char_count:5d}] {c.title_str}")


# ---------------------------------------------------------------- 入口


def main() -> int:
    ap = argparse.ArgumentParser(description="结构化 Markdown 分块器")
    ap.add_argument("--corpus", default=CORPUS_DIR, help="语料目录")
    ap.add_argument("--dump", type=int, default=0, help="打印前 N 块的完整内容")
    ap.add_argument("--out", default="", help="输出 JSONL 路径（目录或文件）")
    ap.add_argument("--top", type=int, default=0, help="按字符数打印最大的 N 块")
    args = ap.parse_args()

    if not os.path.isdir(args.corpus):
        print(f"语料目录不存在: {args.corpus}", file=sys.stderr)
        return 1

    chunks = build_chunks(args.corpus)
    print_report(chunks)

    if args.top:
        print()
        print(f"  最大的 {args.top} 块（检查是否该切而未切）:")
        for c in sorted(chunks, key=lambda x: -x.char_count)[: args.top]:
            print(f"    [{c.char_count:5d}] {c.source}  |  {c.title_str}")

    if args.dump:
        print()
        print("=" * 74)
        print(f"前 {args.dump} 块完整内容")
        print("=" * 74)
        for c in chunks[: args.dump]:
            print(f"\n--- {c.id}")
            print(f"    路径: {c.title_str}")
            print(f"    类型: {','.join(c.kinds)}  字符: {c.char_count}")
            print("    " + "-" * 66)
            for line in c.text.split("\n")[:24]:
                print("    | " + line)
            if c.char_count > 800:
                print("    | ... (截断显示)")

    if args.out:
        path = args.out
        if os.path.isdir(path) or not path.lower().endswith(".jsonl"):
            os.makedirs(path, exist_ok=True)
            path = os.path.join(path, "chunks.jsonl")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for c in chunks:
                f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
        print(f"\n已写出 {len(chunks)} 块 -> {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
