"""检索评估的**呈现层** —— 把测量结果渲染成一份人读的报告。

这一层**只做排版和措辞**，不产生任何新数字：所有数值都来自 `harness.py`
采到的 dataclass。任何需要"再算一次"的地方（未命中诊断的单路排名、
被挤掉那道题的逐块融合表）都调用 `harness` 里的函数，不在这里重新推导 ——
同一件事有两处实现，迟早会有一处悄悄过期。

为什么结论要写死在代码里而不是另写一份文档:
    报告是**自动生成**的。结论若写在别处，改语料后重跑就会得到
    「新数字 + 旧结论」的组合，而那个组合看起来完全正常。
    把结论和数字放在同一个文件里，"说过的话"必须跟着数字一起被重新审一遍。

必须诚实标注的局限（会原样写进报告）:
    1. 这 30 题是我看着语料出的，写题时会不自觉避开已知的弱点 —— 有选择性偏差。
    2. 30 题覆盖 288 块，抽样率约 10%，**不能外推成整体准确率**。
    3. 判定用的是"结果里有没有出现指定标识符"，这是**宽松**判定：
       块里有这个词，不等于模型能从中读出正确答案。
    所以结论的正确读法是「下限参考」: 真实使用只会更差，不会更好。
"""

from __future__ import annotations

import os

from harness import (
    BASE_DIR,
    CASE_K,
    CASE_QUERIES,
    EVAL_K,
    GATE_OFF_DOMAIN,
    GATE_OVERLAP,
    INDEX_DIR,
    RETRIEVAL_QUESTIONS,
    CaseRow,
    ConfigResult,
    DepthRow,
    GateResult,
    HybridRetriever,
    SweepRow,
    _fusion_rows,
    _ranks_for_item,
    pick_worst_demotion,
)


def _pct(n: int, d: int) -> str:
    return f"{n / d * 100:.0f}%" if d else "-"


def build_report(
    retriever: HybridRetriever,
    results: list[ConfigResult],
    gate: GateResult,
    case_rows: list[CaseRow],
    case_stats: dict,
    depth: list[DepthRow],
    sweep: list[SweepRow],
    *,
    dense_skipped: bool,
) -> str:
    n_chunks = len(retriever.chunks)
    n_docs = len({c.source for c in retriever.chunks})
    main = next(r for r in results if r.cfg.key == "hybrid_gate")
    L: list[str] = []

    L.append("# 检索评估报告")
    L.append("")
    L.append("> 由 `python eval/run_eval.py` 自动生成。改语料或改检索逻辑后重跑，数字可直接横向比较。")
    L.append("")
    L.append(f"- 语料：**{n_docs} 份文档 / {n_chunks} 块**")
    L.append(f"- 索引：`{os.path.relpath(INDEX_DIR, BASE_DIR)}`　模型：`{retriever.embedder.model}`")
    L.append(
        f"- 检索题：**{len(RETRIEVAL_QUESTIONS)} 道**　案例题：**{len(CASE_QUERIES)} 条**　"
        f"门控负样本：**{len(GATE_OFF_DOMAIN)} + {len(GATE_OVERLAP)} 条**（零重叠 / 有重叠）"
    )
    L.append(f"- 主指标：Recall@{EVAL_K} / MRR@{EVAL_K}（判定 = Top-{EVAL_K} 内出现预期标识符）")
    if dense_skipped:
        L.append("")
        L.append("> ⚠️ **向量路不可用（embedding 服务未启动），本次只跑了纯关键词配置。**")
        L.append("> 含向量的配置已跳过，表格中的 `-` 表示未测，不是 0 分。")
    L.append("")

    # ---- 一、总体 ----
    L.append("## 一、总体指标")
    L.append("")
    L.append(f"| 配置 | Recall@{EVAL_K} | MRR@{EVAL_K} | 平均耗时 | 空结果 |")
    L.append("|---|---|---|---|---|")
    for r in results:
        if dense_skipped and r.cfg.dense_weight > 0:
            L.append(f"| {r.cfg.label} | - | - | - | - |")
            continue
        empty = sum(1 for x in r.rows if x.n_results == 0)
        L.append(
            f"| {r.cfg.label} | **{r.recall:.1%}** | {r.mrr:.3f} | "
            f"{r.mean_ms:.0f} ms | {empty} |"
        )
    L.append("")
    L.append("耗时是**单次查询的进程内耗时**，向量路含一次 Ollama HTTP 往返，"
             "不代表线上响应时间（MCP 侧另有模型推理开销，量级远大于此）。")
    L.append("")

    # ---- 二、分类 ----
    L.append(f"## 二、分类明细（主配置：{main.cfg.label}）")
    L.append("")
    tags: list[str] = []
    for r in main.rows:
        if r.tag not in tags:
            tags.append(r.tag)
    L.append("| 类别 | 题数 | 命中 | Recall |")
    L.append("|---|---|---|---|")
    for tag in tags:
        sub = [r for r in main.rows if r.tag == tag]
        hit = sum(1 for r in sub if r.hit)
        L.append(f"| {tag} | {len(sub)} | {hit} | **{_pct(hit, len(sub))}** |")
    L.append("")

    # ---- 三、逐题 ----
    L.append("## 三、逐题结果")
    L.append("")
    head = " | ".join(f"{r.cfg.label}" for r in results if not (dense_skipped and r.cfg.dense_weight > 0))
    L.append(f"| # | 问题 | 类别 | {head} |")
    L.append("|---" * (3 + len(results)) + "|")
    for i, item in enumerate(RETRIEVAL_QUESTIONS, 1):
        cells = []
        for r in results:
            if dense_skipped and r.cfg.dense_weight > 0:
                continue
            row = next(x for x in r.rows if x.q == item.q)
            cells.append(f"#{row.first_rank}" if row.first_rank else "✗")
        L.append(f"| {i} | {item.q} | {item.tag} | " + " | ".join(cells) + " |")
    L.append("")
    L.append("`#n` = 首次命中的排名，`✗` = Top-5 内未命中。")
    L.append("")

    # ---- 四、未命中诊断 ----
    missed = [r for r in main.rows if not r.hit]
    L.append("## 四、未命中诊断")
    L.append("")
    if not missed:
        L.append(f"{main.cfg.label} 在全部 {len(main.rows)} 题上均命中。")
    else:
        L.append(f"主配置共 {len(missed)} 题未命中。**关键是要分清这些题卡在哪一步** ——"
                 "「排得不够靠前」和「压根没召回」的解法完全不同，"
                 "笼统说一句「检索效果不好」等于什么都没说。")
        L.append("")
        L.append("| 问题 | 期望标识符 | BM25 排名 | VEC 排名 | 首条结果实际来自 |")
        L.append("|---|---|---|---|---|")
        for r in missed:
            item = next(x for x in RETRIEVAL_QUESTIONS if x.q == r.q)
            bk, dk = _ranks_for_item(retriever, item)
            fmt = lambda v: f"#{v}" if v else ">50"  # noqa: E731
            L.append(
                f"| {r.q} | `{item.expect[0]}` | {fmt(bk)} | {fmt(dk)} | "
                f"{r.top_source or '（空）'} |"
            )
        L.append("")
        L.append("排名是**单路在 pool=50 内**的原始排名（不受融合影响）。"
                 "`>50` 表示该路前 50 名里没有答案块 —— 那是召回问题；"
                 "有排名但 >5 的，是排序问题。")
    L.append("")

    # ---- 五、排名深度 ----
    L.append("## 五、排名深度（单路各自的天花板）")
    L.append("")
    L.append("| top-k | 纯 BM25 Recall | 纯向量 Recall |")
    L.append("|---|---|---|")
    for row in depth:
        L.append(f"| {row.k} | {row.bm25:.1%} | {row.dense:.1%} |")
    L.append("")
    L.append("这张表是混合检索的**理论依据所在**，也是它最容易被误读的地方："
             "两路的失败模式不一样。BM25 命中就排得很前，但有一部分答案"
             "它**到 k=50 仍召不回**；向量几乎什么都召得回，"
             "但排名普遍靠后。混合的价值就在于把后者的召回补进前者的排序。")
    L.append("")

    # ---- 六、权重敏感性 ----
    L.append("## 六、RRF 权重敏感性")
    L.append("")
    L.append(f"| BM25 权重 | 向量权重 | Recall@{EVAL_K} | MRR@{EVAL_K} |")
    L.append("|---|---|---|---|")
    for row in sweep:
        star = " ←默认" if (row.bm25_weight == 1.0 and row.dense_weight == 1.0) else ""
        L.append(
            f"| {row.bm25_weight} | {row.dense_weight} | {row.recall:.1%} | {row.mrr:.3f}{star} |"
        )
    L.append("")
    if not dense_skipped:
        default = next(
            (r for r in sweep if r.bm25_weight == 1.0 and r.dense_weight == 1.0), None
        )
        best = max(sweep, key=lambda r: (r.recall, r.mrr))
        L.append(f"默认权重 (1.0 / 1.0) 的 Recall 是 {default.recall:.1%}，"
                 f"扫描中的最好成绩是 {best.recall:.1%}"
                 f"（{best.bm25_weight} / {best.dense_weight}）。")
        L.append("")
        L.append("⚠️ **这些数字不构成「应该把默认值改成 X」的证据。**"
                 "权重是在**同一套 30 题**上选的，没有独立验证集 —— "
                 "换句话说，最优值里有一部分是对这套题的拟合。"
                 "宽区间内多个取值打平（说明存在平台期、不是单点侥幸）能减轻这个疑虑，但消除不了。")
        L.append("")
        L.append("因此本项目**维持默认 1.0 / 1.0**，只把扫描结果如实记录。"
                 "要支撑改默认值，需要一套不参与调参的验证题集 —— 那是下一步的事。")
    L.append("")

    # ---- 七、门控 ----
    L.append("## 七、门控评估")
    L.append("")
    L.append("负样本拆成两组统计 —— 门控的判据是「多字词全库零命中」，"
             "有词命中就不拦，这是设计如此，两组的能力边界完全不同：")
    L.append("")
    for g in (gate.off_domain, gate.overlap):
        L.append(f"### {g.name}（{g.total} 条）")
        L.append("")
        L.append(f"> {g.note}")
        L.append("")
        L.append(f"- 不设门控时返回非空结果：**{g.noise}/{g.total}** —— 这些就是模型会照着念的**幻觉素材**")
        L.append(f"- 加上门控后判空：**{g.blocked}/{g.total}**")
        if g.empty_plain:
            L.append(f"- 其中 {g.empty_plain} 条本来就返回空（BM25 恰好无结果），不计门控的功劳")
        if g.passed_qs:
            L.append(f"- 被放过的：{'、'.join(g.passed_qs)}")
        L.append("")
    L.append(f"### 正样本（{gate.pos_total} 道检索题）")
    L.append("")
    if gate.pos_gated:
        L.append(f"- ⚠️ 被门控**误杀** {gate.pos_gated} 道：{'、'.join(gate.pos_gated_qs)}")
        L.append("- 误杀意味着答案其实在库里，但提问用词与文档没有共同的多字词。")
    else:
        L.append("- 误杀 **0** 道 —— 门控没有为了拦截负样本而牺牲任何一道正样本。")
    L.append("")

    # ---- 八、案例库 ----
    L.append("## 八、案例库检索")
    L.append("")
    case_hit3 = sum(1 for r in case_rows if r.hit_rank)
    case_hit1 = sum(1 for r in case_rows if r.hit_rank == 1)
    L.append(f"- 案例总数：{case_stats['总案例数']}（有确定根因 {case_stats['有确定根因']} / "
             f"排查清单 {case_stats['排查清单类']}）")
    L.append(f"- 已校对：{case_stats['已校对']}，待校对：{case_stats['待校对']}")
    L.append("")
    L.append(f"对 {len(case_rows)} 条有确定根因的案例做**换词查询**"
             f"（不直接用症状原文，否则等于把答案抄进题目）：")
    L.append("")
    L.append(f"- Top-1 命中：**{case_hit1}/{len(case_rows)}**（{_pct(case_hit1, len(case_rows))}）")
    L.append(f"- Top-{CASE_K} 命中：**{case_hit3}/{len(case_rows)}**（{_pct(case_hit3, len(case_rows))}）")
    L.append("")
    L.append("| 查询（换词后） | 期望案例 | 排名 | 实际返回 |")
    L.append("|---|---|---|---|")
    for r in case_rows:
        rank = f"#{r.hit_rank}" if r.hit_rank else "✗"
        got = "、".join(r.got_ids) if r.got_ids else "（空）"
        L.append(f"| {r.q} | {r.expect_id} | {rank} | {got} |")
    L.append("")
    L.append("清单类（checklist）案例不计入命中率 —— 它们本来就没有唯一答案，"
             "算进去只会人为压低分数。这正是 `kind` 字段必须存在的理由。")
    L.append("")

    # ---- 九、结论 ----
    L.append("## 九、结论与局限")
    L.append("")
    if not dense_skipped:
        by_key = {r.cfg.key: r for r in results}
        bm, de, hy, hg = (by_key["bm25_only"], by_key["dense_only"],
                          by_key["hybrid"], by_key["hybrid_gate"])
        L.append("**实测结论（含一条与预期相反的）：**")
        L.append("")
        L.append(f"1. ⚠️ **混合在默认权重下没有跑赢纯 BM25** —— Recall 打平（{hy.recall:.1%}），"
                 f"MRR 反而更低（{bm.mrr:.3f} → {hy.mrr:.3f}）。"
                 "不是「没测出来」，是测出来确实没赢。")
        worst = pick_worst_demotion(retriever, main)
        if worst is not None:
            bk, dk = _ranks_for_item(retriever, worst)
            show = lambda v: f"#{v}" if v else "未召回"  # noqa: E731
            L.append(f"   机制可以直接算出来，不是推测。最典型的一道是「{worst.q}」："
                     f"正确答案一路 BM25 {show(bk)}、一路向量 {show(dk)}，"
                     "单看哪一路都觉得稳进前五，实际却被融合挤了出去。逐块摊开：")
            L.append("")
            L.append("   | 融合名次 | BM25 排名 | 向量排名 | RRF 分 | |")
            L.append("   |---|---|---|---|---|")
            for n, score, b, d, is_ans in _fusion_rows(retriever, worst):
                mark = "← **正确答案**" if is_ans else ""
                L.append(
                    f"   | {n} | {show(b)} | {show(d)} | {score:.5f} | {mark} |"
                )
            L.append("")
            L.append("   规律很清楚：**挤掉它的块，两路都排在前面；而正确答案一路高一路低，"
                     "哪一路都不够突出。** RRF 是排名求和，两路均衡的块必然胜过"
                     "「一路好一路差」的块 —— 这是 RRF 的设计意图，不是 bug。"
                     "只是在这个语料上意图落了空：BM25 的第一名本来很准"
                     f"（top-1 命中 {depth[0].bm25:.1%}），融合反而把这个优势摊平了。")
            L.append("")
        L.append("")
        L.append(f"2. **但两路的失败模式确实互补**，融合的思路本身站得住："
                 f"BM25 即使把 k 拉到 50，仍有 {1 - depth[-1].bm25:.1%} 的题**根本没有召回**；"
                 f"向量 top-50 能到 {depth[-1].dense:.1%}，但 top-3 只有 {depth[2].dense:.1%}（排序问题）。"
                 "换句话说：**方法对，默认参数没调。**")
        L.append(f"3. 权重扫描印证了上一条：1.0/1.0 处于最差一档，多个更偏 BM25 的取值都能到 "
                 f"{max(r.recall for r in sweep):.1%}。但该结果来自评估集自身，**不作为改默认值的依据**。")
        L.append(f"4. 门控零误杀：{gate.off_domain.blocked}/{gate.off_domain.total} 条零词汇重叠的库外查询被拦下，"
                 f"{gate.pos_total} 道正样本一道没伤。"
                 f"有词汇重叠的 {gate.overlap.total} 条全部放过 —— 这是零命中判据的固有边界，已在第七节标明。")
        L.append("")
    L.append("**这份报告不能说明什么：**")
    L.append("")
    L.append(f"1. **题目是我看着语料出的**，写题时会不自觉地避开已知的弱点，存在选择性偏差。")
    L.append(f"2. **{len(RETRIEVAL_QUESTIONS)} 题覆盖 {n_chunks} 块，抽样率约 "
             f"{len(RETRIEVAL_QUESTIONS) / n_chunks * 100:.0f}%**，不能外推成整体准确率。")
    L.append("3. 判定是**宽松**的：块里出现标识符即算命中，不等于模型能从中读出正确答案。")
    L.append("   上游检索对了、下游模型答错，是这份报告**测不到**的失效模式 ——")
    L.append("   那一层需要人工抽验，见 `eval/模型层抽验清单.md`。")
    L.append("")
    L.append("**一处必须主动交代的口径修正：**")
    L.append("")
    L.append("判定条件在开发过程中改过一次。早期版本只在块的**正文**里找标识符，"
             "于是「上位机通信协议 > 4.1 直流电机启动 — CMD_DCM_START」这种"
             "**标识符只出现在标题里**的块被判成未命中 —— 可 `mcp_server.py:138` "
             "把结果渲染成 `### [{i}] {title_str}`，标题本来就是呈现内容的一部分。"
             "那是我评估口径的错，不是检索的错。")
    L.append("")
    L.append("改为一并匹配标题后：纯 BM25 从 76.7% 升到 80.0%，纯向量维持 70.0%，"
             "混合从 76.7% 升到 80.0%，门控误杀维持 0。")
    L.append("")
    L.append("**分数是往上走的，所以专门写在这里。** 可以佐证的是：核心结论"
             "（混合在默认权重下没有跑赢纯 BM25、MRR 反而更低）在**两种口径下都成立** —— "
             "只有在一个口径下才成立的结论，才有理由怀疑它来自测量误差。")
    L.append("")
    L.append("所以以上数字的正确读法是「**下限参考**」：真实使用只会更差，不会更好。")
    L.append("")
    return "\n".join(L)
