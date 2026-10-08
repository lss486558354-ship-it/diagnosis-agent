"""检索演示 —— 命令行查询你的设备技术文档。

用法:
    python demo_search.py "启动工艺的命令是什么"
    python demo_search.py                      # 进入交互模式
    python demo_search.py "丝杆超时" --single bm25   # 只看关键词检索
    python demo_search.py "丝杆超时" --compare      # 三路对比

输出里每条结果都标注了召回来源（BM25#3 表示关键词路第 3 名，
VEC#1 表示向量路第 1 名）—— 这不是装饰，是用来判断
「这条结果究竟是字面命中还是语义命中」的依据。
"""

from __future__ import annotations

import argparse
import sys
import textwrap

from retrieval.embedder import EmbeddingError
from retrieval.hybrid import INDEX_DIR, HybridRetriever
from retrieval.tokenizer import tokenize_query

PREVIEW_CHARS = 220
WIDTH = 78


def _preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    """把块正文压成适合终端阅读的预览。

    保留表格与代码块的原始形态 —— 这两类内容的换行和缩进本身就是信息，
    重排会破坏可读性（比如把命令表折行后就对不齐了）。
    """
    body = text.strip()
    is_structured = body.startswith("|") or body.startswith("```")

    if is_structured:
        lines = body.split("\n")[:8]
        width = min(len(ln) for ln in lines if ln.strip()) if any(ln.strip() for ln in lines) else 0
        out = []
        for ln in lines:
            out.append(ln if len(ln) <= 100 else ln[:100] + " ...")
        return "\n".join("      " + ln for ln in out)

    flat = " ".join(body.split())
    if len(flat) <= limit:
        return "      " + flat
    return "\n".join("      " + ln for ln in textwrap.wrap(flat[:limit], WIDTH - 8)) + "\n      ..."


def render(label: str, results, *, show_preview: bool = True) -> None:
    """渲染一组检索结果。"""
    print()
    print("=" * WIDTH)
    print(f"  {label}")
    print("=" * WIDTH)

    if not results:
        print("  （无结果）")
        return

    for i, r in enumerate(results, 1):
        c = r.chunk
        print()
        print(f"  [{i}]  {r.sources}")
        print(f"       {c.title_str}")
        print(f"       {c.source}  ·  {c.char_count} 字符")
        if show_preview:
            print()
            print(_preview(c.text))


def run_single(retriever: HybridRetriever, query: str, top_k: int, mode: str):
    """只跑单路，用于对比时看某一路的原始表现。

    复用 HybridRetriever 的两路索引，但把权重压到单侧 ——
    权重为 0 的那一路完全不参与融合，效果等价于纯单路检索。
    """
    if mode == "bm25":
        return retriever.search(query, top_k=top_k, bm25_weight=1.0, dense_weight=0.0)
    return retriever.search(query, top_k=top_k, bm25_weight=0.0, dense_weight=1.0)


def do_query(retriever: HybridRetriever, query: str, args) -> None:
    """执行一次查询并按参数渲染。"""
    toks = tokenize_query(query)
    print()
    print(f"查询: {query}")
    print(f"分词: {toks}")

    if args.compare:
        for mode, label in (("bm25", "纯关键词检索 (BM25)"), ("dense", "纯语义检索 (向量)")):
            render(label, run_single(retriever, query, args.top, mode), show_preview=False)
        render("混合检索 (RRF 融合)", retriever.search(query, top_k=args.top), show_preview=args.preview)
        return

    if args.single:
        results = run_single(retriever, query, args.top, args.single)
        render(f"检索结果（仅 {args.single}）", results, show_preview=args.preview)
        return

    render("检索结果（混合）", retriever.search(query, top_k=args.top), show_preview=args.preview)


def interactive(retriever: HybridRetriever, args) -> None:
    """交互模式。"""
    print()
    print("=" * WIDTH)
    print("  设备文档检索 —— 交互模式")
    print("=" * WIDTH)
    print(f"  索引: {len(retriever.chunks)} 块    模型: {retriever.embedder.model}")
    print("  输入查询回车执行，输入 q 退出")
    print()

    while True:
        try:
            query = input("查询> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query:
            continue
        if query.lower() in {"q", "quit", "exit"}:
            break
        try:
            do_query(retriever, query, args)
        except EmbeddingError as e:
            print(f"\n[编码失败] {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="设备文档检索演示",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("query", nargs="*", help="查询内容；省略则进入交互模式")
    ap.add_argument("--index", default=INDEX_DIR, help="索引目录")
    ap.add_argument("-k", "--top", type=int, default=5, help="返回条数")
    ap.add_argument("--single", choices=["bm25", "dense"], help="只用单路检索")
    ap.add_argument("--compare", action="store_true", help="对比 关键词 / 语义 / 混合 三路")
    ap.add_argument("--no-preview", dest="preview", action="store_false", help="不显示正文预览")
    args = ap.parse_args()

    try:
        retriever = HybridRetriever.load(args.index)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1

    query = " ".join(args.query).strip()
    if query:
        do_query(retriever, query, args)
    else:
        interactive(retriever, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
