"""检索评估的**测量层** —— 只负责跑出数字，不负责解释数字。

这一层回答的是「四种配置各自的 Recall / MRR / 排序深度 / 门控拦截率分别是多少」，
是纯粹的采集代码。所有"这些数字意味着什么"的判断都在 `report.py` 里，
所有"先跑什么后跑什么"的顺序编排都在 `run_eval.py` 里。

为什么要把测量和呈现分开:
    混在一起时，改一句措辞就可能碰到判定逻辑，而**呈现层的改动不该
    影响测量结果**。分开之后，`report.py` 可以随便重写，只要重跑一次
    就能确认数字没变 —— 这是判断"改动是否只是换了说法"的唯一办法。

四种配置在比什么:
    bm25_only    纯关键词。字面命中就中，换个说法就废。
    dense_only   纯向量。换个说法也能中，但库里没有的东西它照样返最近邻。
    hybrid       RRF 融合，不设门控。
    hybrid+gate  在 hybrid 基础上，关键词路多字词全库零命中时直接判空。
    前两者的差距说明"为什么要混合"，后两者的差距说明"为什么要门控"。
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
# 项目里到处是相对路径（INDEX_DIR="index"、SEED_PATH="cases_seed.json"），
# 所以脚本必须在任何 cwd 下都能把项目根加进 sys.path。
# EVAL_DIR 是为了 `python -m eval.run_eval` 这种调用方式也能找到 dataset。
for _p in (BASE_DIR, EVAL_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from case_store import CaseStore  # noqa: E402
from dataset import (  # noqa: E402
    CASE_QUERIES,
    GATE_OFF_DOMAIN,
    GATE_OVERLAP,
    RETRIEVAL_QUESTIONS,
)
from retrieval.hybrid import HybridRetriever  # noqa: E402
from retrieval.tokenizer import tokenize_query  # noqa: E402

DEPTH_KS = (1, 3, 5, 10, 20, 50)
# 权重扫描的取值。刻意取得粗、取得宽 —— 这里要回答的是"默认值附近
# 是不是一片平台"，不是"最优值精确到小数点后几位"。后者在这点数据量上
# 没有意义。
WEIGHT_SWEEP = (0.5, 1.0, 1.5, 2.0, 3.0, 5.0)

EVAL_K = 5      # 检索主指标用 Recall@5
CASE_K = 3      # 案例库条数少，用 Top-3
INDEX_DIR = os.path.join(BASE_DIR, "index")
CASE_DB = os.path.join(BASE_DIR, "data", "cases.db")
SEED_PATH = os.path.join(BASE_DIR, "cases_seed.json")


# ---------------------------------------------------------------- 配置

@dataclass(frozen=True)
class Config:
    key: str
    label: str
    bm25_weight: float
    dense_weight: float
    gate: bool


CONFIGS: tuple[Config, ...] = (
    Config("bm25_only", "纯关键词 (BM25)", 1.0, 0.0, False),
    Config("dense_only", "纯向量 (语义)", 0.0, 1.0, False),
    Config("hybrid", "混合 (RRF)", 1.0, 1.0, False),
    Config("hybrid_gate", "混合 + 门控", 1.0, 1.0, True),
)


@dataclass
class QueryResult:
    """一题在一个配置下的结果。"""

    q: str
    tag: str
    hit: bool
    first_rank: int | None    # 首次命中的排名（1-based），未命中为 None
    n_results: int
    gated: bool
    elapsed_ms: float
    top_source: str           # 首条结果的来源文档，未命中时用来看"它跑偏到哪了"


@dataclass
class ConfigResult:
    cfg: Config
    rows: list[QueryResult]

    @property
    def recall(self) -> float:
        return sum(1 for r in self.rows if r.hit) / len(self.rows) if self.rows else 0.0

    @property
    def mrr(self) -> float:
        if not self.rows:
            return 0.0
        return sum(1.0 / r.first_rank for r in self.rows if r.first_rank) / len(self.rows)

    @property
    def mean_ms(self) -> float:
        return sum(r.elapsed_ms for r in self.rows) / len(self.rows) if self.rows else 0.0


# ---------------------------------------------------------------- 自检

def check_ground_truth(retriever: HybridRetriever) -> list[str]:
    """确认每个 expect 串真的存在于某个块里。

    为什么必须在**块**里搜而不是在原始文件里搜: 检索只能返回完整的块。
    如果某个标识符正好被块边界劈成两半，它在文件里存在、却永远不可能
    被检索到 —— 那种题是死题，不该算进分母。

    不做这一步的后果很隐蔽: ground truth 写错时，四种配置会**一起**低分，
    看起来像"检索器不行"，实际是题目本身有问题。
    """
    blob = "\n".join(_seen_text(c) for c in retriever.chunks)
    bad: list[str] = []
    for item in RETRIEVAL_QUESTIONS:
        if not any(e in blob for e in item.expect):
            bad.append(f"  [{item.tag}] {item.q}\n      找不到任何: {list(item.expect)}")
    return bad


# ---------------------------------------------------------------- 检索评估

def _seen_text(chunk) -> str:
    """一条结果**实际呈现出来**的文本 = 标题 + 正文。

    为什么判定必须包含标题 —— 这是本评估早期版本的一个真实错误:
        mcp_server.py:138 把结果渲染成 `### [{i}] {c.title_str}`，
        demo_search.py 也打印 title_str。标题是结果的一部分，模型和用户都看得到。
        早期只查正文，于是「上位机通信协议 > 4.1 直流电机启动 — CMD_DCM_START」
        这种**标识符只出现在标题里**的块被判成未命中 —— 明明是正确答案，
        却被记成失败。那是评估口径的错，不是检索的错。
    改口径后分数会**上升**，这一点在报告里如实标明了。
    """
    return f"{chunk.title_str}\n{chunk.text}"


def _matches(chunk, expect: tuple[str, ...]) -> bool:
    text = _seen_text(chunk)
    return any(e in text for e in expect)


def _first_hit_rank(results, expect: tuple[str, ...]) -> int | None:
    """Top-K 里第一个含任一 expect 串的位置（1-based）。"""
    for i, r in enumerate(results, 1):
        if _matches(r.chunk, expect):
            return i
    return None


def _fusion_rows(
    retriever: HybridRetriever, item, *, top: int = 5, pool: int = 50
) -> list[tuple[int, float, int | None, int | None, bool]]:
    """重算一次融合，逐块给出 (名次, RRF分, BM25排名, 向量排名, 是否答案)。

    用于把"融合为什么把正确答案挤下去"摊开成一张能复核的表。
    报告里那张表由这里生成，不写死数字 —— 写死的表会在语料变化后
    **静默**过期，继续贴在报告里看着像最新结论。
    """
    k = retriever.rrf_k
    toks = tokenize_query(item.q)
    bm = retriever.bm25.search(toks, top_k=pool) if toks else []
    de = retriever.dense.search(retriever.embedder.embed_one(item.q), top_k=pool)
    br = {h.doc_id: i + 1 for i, h in enumerate(bm)}
    dr = {d: i + 1 for i, (d, _) in enumerate(de)}

    fused: dict[int, float] = {}
    for d, rk in br.items():
        fused[d] = fused.get(d, 0.0) + 1.0 / (k + rk)
    for d, rk in dr.items():
        fused[d] = fused.get(d, 0.0) + 1.0 / (k + rk)

    ranked = sorted(fused.items(), key=lambda kv: -kv[1])
    answer_pos = next(
        (
            n
            for n, (d, _) in enumerate(ranked, 1)
            if _matches(retriever.chunks[d], item.expect)
        ),
        None,
    )
    # 答案在 top 之外时也把它那一行补上 —— 只说"掉出前五"而不给实际名次，
    # 读者没法判断是差一点还是差很远。
    show = list(range(1, top + 1))
    if answer_pos and answer_pos > top:
        show.append(answer_pos)

    rows = []
    for n in show:
        d, score = ranked[n - 1]
        rows.append(
            (n, score, br.get(d), dr.get(d), _matches(retriever.chunks[d], item.expect))
        )
    return rows


def pick_worst_demotion(retriever: HybridRetriever, main: ConfigResult):
    """挑出"单路排名很好、融合后却掉出前五"的那道题。

    判据是 min(两路排名) 最小的那道未命中题 —— 越小越冤。
    自动挑而不是写死题号: 语料一变，当初那道题可能就不复现了，
    报告会继续贴着一段对不上的叙述。
    """
    best: tuple[int, object] | None = None
    for r in main.rows:
        if r.hit:
            continue
        item = next(x for x in RETRIEVAL_QUESTIONS if x.q == r.q)
        ranks = [v for v in _ranks_for_item(retriever, item) if v]
        if not ranks:
            continue
        if best is None or min(ranks) < best[0]:
            best = (min(ranks), item)
    return best[1] if best else None


def _ranks_for_item(
    retriever: HybridRetriever, item, pool: int = 50
) -> tuple[int | None, int | None]:
    """答案块在两路**各自** pool 名内的原始排名（不含融合）。

    诊断"为什么没进 top-5"必须看单路原始排名，不能看融合后的 ——
    融合会把两路都认可的块抬上来，恰恰可能把某一路的**第一名**挤下去。
    那正是要查的现象本身，用融合结果去解释它就成了循环论证。
    """
    toks = tokenize_query(item.q)
    bm = retriever.bm25.search(toks, top_k=pool) if toks else []
    de = retriever.dense.search(retriever.embedder.embed_one(item.q), top_k=pool)
    tgt = {i for i, c in enumerate(retriever.chunks) if _matches(c, item.expect)}
    # 取**最好**的排名，不是下标最小的那个块 —— 同一段知识常常在语料里
    # 有多个近重复的块（命令表在三份文档里各有一份），按下标取会随机
    # 挑中一个排得很差的，把诊断引到错误的方向上。
    bk = min((i + 1 for i, h in enumerate(bm) if h.doc_id in tgt), default=None)
    dk = min((i + 1 for i, (d, _) in enumerate(de) if d in tgt), default=None)
    return bk, dk


def run_config(retriever: HybridRetriever, cfg: Config) -> ConfigResult:
    """跑完一整套题。"""

    def one(item) -> QueryResult:
        t0 = time.perf_counter()
        outcome = retriever.search_checked(
            item.q,
            top_k=EVAL_K,
            bm25_weight=cfg.bm25_weight,
            dense_weight=cfg.dense_weight,
            gate=cfg.gate,
        )
        ms = (time.perf_counter() - t0) * 1000
        rank = _first_hit_rank(outcome.results, item.expect)
        return QueryResult(
            q=item.q,
            tag=item.tag,
            hit=rank is not None,
            first_rank=rank,
            n_results=len(outcome.results),
            gated=outcome.gated,
            elapsed_ms=ms,
            top_source=outcome.results[0].chunk.source if outcome.results else "",
        )

    return ConfigResult(cfg, [one(item) for item in RETRIEVAL_QUESTIONS])


# ---------------------------------------------------------------- 门控评估

@dataclass
class GateGroup:
    """一组负样本的门控表现。"""

    name: str
    note: str
    total: int
    blocked: int        # 开了门控后被判空的条数
    empty_plain: int    # 不开门控时本来就没结果的条数（BM25 恰好为空）
    noise: int          # 不开门控时返回了非空结果的条数 = 幻觉素材条数
    passed_qs: list[str]  # 被放过的查询


@dataclass
class GateResult:
    off_domain: GateGroup
    overlap: GateGroup
    pos_total: int
    pos_gated: int               # 正样本被误杀的条数
    pos_gated_qs: list[str]


def _probe_gate(retriever: HybridRetriever, queries, name: str, note: str) -> GateGroup:
    """跑一组负样本。

    同时看两件事:
      开门控后被拦下多少（拦截率 —— 门控有没有用）
      开门控前返回了多少条（幻觉素材 —— 不设门控的代价）
    只看拦截率会漏掉"这些查询本来就返回空"的情况，那样门控是个摆设 ——
    所以 empty_plain 必须单独记，它衡量的是"没有门控时 BM25 自己也够用"
    的那部分，不算门控的功劳。
    """
    blocked = empty_plain = noise = 0
    passed: list[str] = []
    for q in queries:
        if retriever.search_checked(q, top_k=EVAL_K, gate=True).gated:
            blocked += 1
        else:
            passed.append(q)
        if retriever.search_checked(q, top_k=EVAL_K, gate=False).results:
            noise += 1
        else:
            empty_plain += 1
    return GateGroup(name, note, len(queries), blocked, empty_plain, noise, passed)


def run_gate(retriever: HybridRetriever) -> GateResult:
    """门控正负样本。"""
    off = _probe_gate(
        retriever, GATE_OFF_DOMAIN, "库外领域（零词汇重叠）",
        "门控**应该**拦下 —— 拦不住就是缺陷",
    )
    ovl = _probe_gate(
        retriever, GATE_OVERLAP, "库外领域（有词汇重叠）",
        "门控判据是「多字词全库零命中」，有词命中就不拦 —— 这是**已知能力边界**，不是 bug",
    )

    gated_qs: list[str] = []
    for item in RETRIEVAL_QUESTIONS:
        if retriever.search_checked(item.q, top_k=EVAL_K, gate=True).gated:
            gated_qs.append(item.q)

    return GateResult(
        off_domain=off,
        overlap=ovl,
        pos_total=len(RETRIEVAL_QUESTIONS),
        pos_gated=len(gated_qs),
        pos_gated_qs=gated_qs,
    )


# ---------------------------------------------------------------- 深度与权重

@dataclass
class DepthRow:
    """单路在 top-k 上的 Recall —— 看"答案排得靠后"还是"根本没有"。"""

    k: int
    bm25: float
    dense: float


def run_depth(retriever: HybridRetriever) -> list[DepthRow]:
    """把 top-k 从 1 拉到 50，看两路各自的天花板在哪。

    这个分析能区分两种失败:
      答案在 top-50 里但排不进 top-5  → 排序问题
      答案在 top-50 里根本没有        → 召回问题（词面/语义上就够不着）
    两者的解法完全不同，混在一起谈"检索效果不好"没有意义。
    """
    rows: list[DepthRow] = []
    for k in DEPTH_KS:
        hits = {}
        for key, bw, dw in (("bm25", 1.0, 0.0), ("dense", 0.0, 1.0)):
            n = 0
            for item in RETRIEVAL_QUESTIONS:
                res = retriever.search_checked(
                    item.q, top_k=k, bm25_weight=bw, dense_weight=dw, gate=False
                ).results
                if any(_matches(r.chunk, item.expect) for r in res):
                    n += 1
            hits[key] = n / len(RETRIEVAL_QUESTIONS)
        rows.append(DepthRow(k, hits["bm25"], hits["dense"]))
    return rows


@dataclass
class SweepRow:
    bm25_weight: float
    dense_weight: float
    recall: float
    mrr: float


def run_sweep(retriever: HybridRetriever) -> list[SweepRow]:
    """RRF 权重敏感性扫描。

    动机: 默认 1.0/1.0 从来没被验证过。如果它在扫描里是最差的一档，
    那"混合优于单路"这个说法就是被一个没调过的默认值给否掉的 ——
    那是**默认值的锅，不是融合方法的锅**，两者必须分开谈。

    扫描范围取得宽而粗: 要回答的是"最优值附近是不是一片平台"。
    如果只有某个精确取值能赢，那多半是拟合了这套题，不该采信。
    """
    rows: list[SweepRow] = []
    for bw, dw in [(w, 1.0) for w in WEIGHT_SWEEP] + [(1.0, w) for w in (0.3, 0.5)]:
        hit = 0
        mrr = 0.0
        for item in RETRIEVAL_QUESTIONS:
            res = retriever.search_checked(
                item.q, top_k=EVAL_K, bm25_weight=bw, dense_weight=dw, gate=False
            ).results
            rank = _first_hit_rank(res, item.expect)
            if rank:
                hit += 1
                mrr += 1.0 / rank
        n = len(RETRIEVAL_QUESTIONS)
        rows.append(SweepRow(bw, dw, hit / n, mrr / n))
    return rows


# ---------------------------------------------------------------- 案例库评估

@dataclass
class CaseRow:
    q: str
    expect_id: str
    hit_rank: int | None
    got_ids: list[str]


def run_cases() -> tuple[list[CaseRow], dict]:
    store = CaseStore(CASE_DB)
    try:
        stats = store.stats()
        if not stats["总案例数"]:
            store.load_seed(SEED_PATH)

        rows: list[CaseRow] = []
        for item in CASE_QUERIES:
            hits = store.search(item.q, limit=CASE_K)
            ids = [c.id for c, _ in hits]
            rank = ids.index(item.expect_id) + 1 if item.expect_id in ids else None
            rows.append(
                CaseRow(q=item.q, expect_id=item.expect_id, hit_rank=rank, got_ids=ids)
            )
        return rows, store.stats()
    finally:
        store.close()
