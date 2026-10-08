"""构建检索索引并落盘。

分块 → BM25 → embedding → 向量索引 → 存盘。

为什么索引要落盘:
    BM25 建索引是纯 CPU 的，秒级完成；但 embedding 要过一遍模型。
    虽然实测只要 5 秒，落盘的意义在于**可复现**——评估阶段要反复
    跑同一索引对比不同检索策略，如果每次结果都因重新编码而有微小
    差异（浮点非确定性），对比就失去意义。

用法:
    python build_index.py              # 构建并保存
    python build_index.py --check      # 只检查已有索引
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from chunker import build_chunks
from retrieval.embedder import EmbeddingError, OllamaEmbedder
from retrieval.hybrid import INDEX_DIR, HybridRetriever


def main() -> int:
    ap = argparse.ArgumentParser(description="构建检索索引")
    ap.add_argument("--corpus", default="corpus", help="语料目录")
    ap.add_argument("--index", default=INDEX_DIR, help="索引输出目录")
    ap.add_argument("--check", action="store_true", help="只检查已有索引，不重建")
    args = ap.parse_args()

    if args.check:
        try:
            r = HybridRetriever.load(args.index)
        except FileNotFoundError as e:
            print(f"[未建索引] {e}")
            return 1
        print(f"索引就绪: {args.index}")
        print(f"  块数      {len(r.chunks)}")
        print(f"  向量维度  {r.dense.dim}")
        print(f"  模型      {r.embedder.model}")
        for k, v in r.bm25.stats().items():
            print(f"  {k:10s} {v}")
        return 0

    t_start = time.time()

    # --- 1. 分块 ---
    print("[1/4] 分块 ...")
    chunks = build_chunks(args.corpus)
    if not chunks:
        print(f"  语料目录为空: {args.corpus}", file=sys.stderr)
        return 1
    print(f"  {len(chunks)} 块，{sum(c.char_count for c in chunks):,} 字符")

    # --- 2. 检查 embedding 可用性（失败要早失败，别等建完 BM25 才报错）---
    print("[2/4] 检查 embedding 服务 ...")
    emb = OllamaEmbedder()
    try:
        emb.ensure_ready()
    except EmbeddingError as e:
        print(f"\n{e}", file=sys.stderr)
        return 1
    print(f"  {emb.model} 就绪")

    # --- 3. 构建两路索引 ---
    print("[3/4] 构建索引 ...")
    try:
        retriever = HybridRetriever.build(chunks, emb, verbose=True)
    except EmbeddingError as e:
        print(f"\n编码失败: {e}", file=sys.stderr)
        return 1

    # --- 4. 落盘 ---
    print("[4/4] 保存 ...")
    retriever.save(args.index)

    meta_path = os.path.join(args.index, "meta.json")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    print()
    print("=" * 70)
    print(f"索引已保存到 {args.index}/")
    print("=" * 70)
    for k, v in meta.items():
        print(f"  {k:12s} {v}")
    print()
    for k, v in retriever.bm25.stats().items():
        print(f"  {k:12s} {v}")
    print()
    print(f"总耗时 {time.time() - t_start:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
