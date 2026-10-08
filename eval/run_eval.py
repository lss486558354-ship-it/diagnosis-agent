"""检索评估 —— 四种配置跑同一套题，产出可复核的数字。

用法:
    cd _portfolio_work/diagnosis-agent
    python eval/run_eval.py

产出:
    eval/report.md     人读的报告
    eval/results.json  原始数据（每题的命中/排名/耗时），便于二次分析

三层分工（改哪一层看哪一层，不要混着改）:
    harness.py    测量层 —— 跑出数字。判定口径在这里，改它数字会变。
    report.py     呈现层 —— 把数字排版成报告。改它数字**不该**变。
    run_eval.py   编排层 —— 本文件。决定先跑什么后跑什么、缓存装在哪。

    分开的理由很实际: 只有把呈现层单独拎出来，"我这次只改了措辞"
    才是一个**可验证**的说法 —— 重跑一遍，`results.json` 逐位相同即可。

为什么要有这个脚本:
    README 里想写"混合检索优于单路"，但这句话在此之前**没有任何数据支撑** ——
    只有几个手挑的查询看起来还行。手挑的查询永远看起来还行。
    这里要做的是把"看起来"换成"测出来"，并且把测的方法一并交出去，
    让别人能证伪。

必须诚实标注的局限（会原样写进报告）:
    1. 这 30 题是我看着语料出的，写题时会不自觉避开已知的弱点 —— 有选择性偏差。
    2. 30 题覆盖 288 块，抽样率约 10%，**不能外推成整体准确率**。
    3. 判定用的是"结果里有没有出现指定标识符"，这是**宽松**判定：
       块里有这个词，不等于模型能从中读出正确答案。
    所以结论的正确读法是「下限参考」: 真实使用只会更差，不会更好。
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict

# harness 是同目录下的模块，两种调用方式（`python eval/run_eval.py` 与
# `python -m eval.run_eval`）的 sys.path 不同，这里先统一补上。
# 项目根目录由 harness 负责补 —— 它自己要 import retrieval / case_store。
_EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
if _EVAL_DIR not in sys.path:
    sys.path.insert(0, _EVAL_DIR)

from harness import (  # noqa: E402
    BASE_DIR,
    CASE_DB,
    CASE_K,
    CONFIGS,
    EVAL_DIR,
    EVAL_K,
    INDEX_DIR,
    ConfigResult,
    check_ground_truth,
    run_cases,
    run_config,
    run_depth,
    run_gate,
    run_sweep,
)
from report import build_report  # noqa: E402
from retrieval.embedder import EmbeddingError  # noqa: E402
from retrieval.hybrid import HybridRetriever  # noqa: E402


# ---------------------------------------------------------------- 主流程

def main() -> int:
    print("加载索引 ...")
    try:
        retriever = HybridRetriever.load(INDEX_DIR)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"  {len(retriever.chunks)} 块 / {len({c.source for c in retriever.chunks})} 份文档")

    # ---- 自检 ----
    print("自检 ground truth ...")
    bad = check_ground_truth(retriever)
    if bad:
        print("\n以下 expect 串在整个语料里都找不到，说明题目写错了，评估结果无意义：",
              file=sys.stderr)
        print("\n".join(bad), file=sys.stderr)
        print("\n请修正 eval/dataset.py 后重跑。", file=sys.stderr)
        return 2
    print("  通过")

    # ---- 向量路可用性 ----
    dense_skipped = False
    try:
        retriever.embedder.embed_one("预热")
    except EmbeddingError as e:
        dense_skipped = True
        print(f"\n[向量路不可用] {e}", file=sys.stderr)
        print("只跑纯关键词配置。启动 embedding 服务后重跑可得完整对比。\n", file=sys.stderr)

    # ---- 四配置 ----
    # 计时必须在装缓存**之前**跑完 —— 下面的扫描会让同一条查询被反复编码，
    # 一旦缓存提前生效，混合配置的耗时就会显示成 0 ms（第一次跑出来就是这样）。
    results: list[ConfigResult] = []
    for cfg in CONFIGS:
        if dense_skipped and cfg.dense_weight > 0:
            continue
        t0 = time.time()
        res = run_config(retriever, cfg)
        results.append(res)
        print(f"[{cfg.key}] Recall@{EVAL_K}={res.recall:.1%}  MRR={res.mrr:.3f}  "
              f"({time.time() - t0:.1f}s)")

    # 同一条查询在后面几个扫描里会被重复编码，缓存掉让权重扫描能秒级跑完。
    # 只作用于本次评估持有的这个 embedder 实例，不碰任何共享状态。
    _cache: dict[str, object] = {}
    _orig_embed = retriever.embedder.embed_one
    if not dense_skipped:
        def _cached(q: str):
            if q not in _cache:
                _cache[q] = _orig_embed(q)
            return _cache[q]

        retriever.embedder.embed_one = _cached  # type: ignore[method-assign]

    # ---- 门控 / 深度 / 权重 / 案例库 ----
    gate = run_gate(retriever)
    print(f"[门控] 零重叠组拦下 {gate.off_domain.blocked}/{gate.off_domain.total}，"
          f"有重叠组拦下 {gate.overlap.blocked}/{gate.overlap.total}，"
          f"正样本误杀 {gate.pos_gated}/{gate.pos_total}")

    depth = run_depth(retriever) if not dense_skipped else []
    if depth:
        print(f"[深度] k=50 时 BM25={depth[-1].bm25:.1%}  VEC={depth[-1].dense:.1%}")

    sweep = run_sweep(retriever) if not dense_skipped else []
    if sweep:
        best = max(sweep, key=lambda r: (r.recall, r.mrr))
        dflt = next(r for r in sweep if r.bm25_weight == 1.0 and r.dense_weight == 1.0)
        print(f"[权重] 默认(1.0/1.0)={dflt.recall:.1%}  最好="
              f"{best.recall:.1%} @ {best.bm25_weight}/{best.dense_weight}")

    case_rows, case_stats = run_cases()
    # CaseStore.stats() 的「数据库」字段是本机绝对路径。results.json 会随
    # 仓库一起公开，这里统一换成相对路径 —— 否则等于把开发者的目录结构
    # （含用户名和文件夹命名）一并发布出去。报告不用这个字段，但数据文件用。
    case_stats = {**case_stats, "数据库": os.path.relpath(CASE_DB, BASE_DIR)}
    case_hit3 = sum(1 for r in case_rows if r.hit_rank)
    print(f"[案例库] Top-{CASE_K} 命中 {case_hit3}/{len(case_rows)}")

    # ---- 落盘 ----
    report = build_report(
        retriever, results, gate, case_rows, case_stats, depth, sweep,
        dense_skipped=dense_skipped,
    )
    report_path = os.path.join(EVAL_DIR, "report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    payload = {
        "corpus": {
            "n_chunks": len(retriever.chunks),
            "n_docs": len({c.source for c in retriever.chunks}),
            "model": retriever.embedder.model,
        },
        "dense_skipped": dense_skipped,
        "configs": [
            {
                "key": r.cfg.key,
                "label": r.cfg.label,
                "recall_at_k": round(r.recall, 4),
                "mrr": round(r.mrr, 4),
                "mean_ms": round(r.mean_ms, 1),
                "rows": [asdict(x) for x in r.rows],
            }
            for r in results
        ],
        "gate": asdict(gate),
        "depth": [asdict(r) for r in depth],
        "weight_sweep": [asdict(r) for r in sweep],
        "cases": {
            "stats": case_stats,
            "top1": sum(1 for r in case_rows if r.hit_rank == 1),
            "top3": case_hit3,
            "rows": [asdict(r) for r in case_rows],
        },
    }
    results_path = os.path.join(EVAL_DIR, "results.json")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n报告: {report_path}")
    print(f"数据: {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
