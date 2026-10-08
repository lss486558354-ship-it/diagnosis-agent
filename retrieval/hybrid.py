"""混合检索 —— BM25 与向量两路融合。

为什么用 RRF 而不是加权求和分数:
    BM25 的分数是无界的（本语料实测到 17.56，取决于文档长度与词频），
    余弦相似度固定在 [-1, 1]。把两者归一化后加权求和看似合理，实则脆弱:
    归一化依赖当次查询的结果分布，同一文档在不同查询下权重会漂移，
    而且 min-max 归一化会被单个极端值支配。

    RRF (Reciprocal Rank Fusion) 只用**排名**不用分数:
        score(d) = Σ_r  weight_r / (k + rank_r(d))
    排名是跨检索器可比的，分数不是。代价是丢掉了分数里"领先多少"的
    信息 —— 但在两路检索器性质差异这么大的场景下，这个代价换来的
    稳定性是划算的。k=60 是文献里的常用取值，作用是压平头部差异，
    避免单路的第一名过度主导。

保留每路的原始排名:
    结果里同时给出 bm25_rank 与 dense_rank，便于回答"这条为什么被排在
    这里"。评估阶段要靠它判断两路各自的贡献，没有这个就只能猜。

为什么还要做门控（search_checked）:
    上面说 RRF 的代价是"丢掉领先多少的信息"，但那句话只考虑了
    **两路都有结果**的情形，漏了"一路全空"。BM25 是全库精确匹配，
    没有一个共同词就是空结果 —— 它天然具备"我不匹配"的能力。
    向量检索没有这个能力: 软排序，无论多远的邻居它都会返回。
    RRF 只看排名，于是当 BM25 空手而归，向量路的噪声就**独占**了
    结果列表，而且渲染出来与正常命中毫无区别。
    实测查询「冰箱不制冷」: 纯 BM25 返回 0 条（正确），混合检索
    返回 3 条毫不相关的内容（打包工艺、继电保护设计）。模型照着念
    就会产出一段**带来源引用的幻觉** —— 比没有引用更危险，
    因为引用本身就是可信度信号。
    "我不知道"这件事只能由外层显式补上，见 search_checked()。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import numpy as np

from chunker import Chunk

from .bm25 import BM25Index
from .dense import DenseIndex
from .embedder import EmbeddingError, OllamaEmbedder
from .tokenizer import tokenize_query

DEFAULT_RRF_K = 60
INDEX_DIR = "index"

# 关键词路全库零命中时的门控结论。
# 措辞刻意说死"可能没有这个概念"，而不是含糊的"未找到相关结果"——
# 后者是向量检索的常用话术，但它对"确实没有"和"用词不同"不作区分。
GATE_NOTE = (
    "查询里的关键词在整个语料库中一次都没有出现 —— 文档库里可能没有这个概念。"
    "向量检索仍会返回若干「最近邻」，但那与查询无关，已丢弃。"
)


@dataclass
class RetrievedChunk:
    """一条检索结果，含融合分与两路各自的排名。"""

    chunk: Chunk
    score: float
    bm25_rank: int | None = None
    dense_rank: int | None = None
    # 关键词路本次是否启用。纯向量检索时 bm25_rank 必然全为 None，
    # 但那是"根本没跑"，不是"没匹配上" —— is_weak 必须区分这两者，
    # 否则纯向量基线会被统计成 100% 弱证据。
    bm25_used: bool = True

    @property
    def sources(self) -> str:
        """标出这条结果由哪几路召回，便于快速判断检索器行为。"""
        marks = []
        if self.bm25_rank is not None:
            marks.append(f"BM25#{self.bm25_rank}")
        if self.dense_rank is not None:
            marks.append(f"VEC#{self.dense_rank}")
        return " + ".join(marks) if marks else "-"

    @property
    def is_weak(self) -> bool:
        """是否只有向量路召回。

        判据是"是否只有一路"，不是相似度阈值 —— 阈值该设多少现在
        没人知道，拍脑袋定一个数只是换一种拍脑袋。而这个判据是确定的:
        关键词路前 pool 名里根本没有它，说明字面上没有一个词对上，
        纯靠语义最近邻进来。

        关键词路本次没启用时一律不算弱 —— 纯向量检索里每条都"只有
        向量路召回"，标出来等于没标，还会污染评估统计。
        """
        return self.bm25_used and self.bm25_rank is None


@dataclass
class SearchOutcome:
    """一次检索的结果 + 可信度判断。

    为什么不直接改 search() 的返回值: demo 与评测脚本只想要结果列表，
    改签名会把它们全部牵连进来。拆开之后，关心可信度的调用方
    （MCP 工具）用 search_checked()，只想要结果的继续用 search()。
    """

    results: list[RetrievedChunk]
    gated: bool = False
    note: str = ""


def evidence_note(results: list[RetrievedChunk]) -> str:
    """一句话说明这批结果的证据强度，供调用方原样转述给模型。

    只报"有几条仅靠向量召回"这个事实，不给置信度打分 —— 打分需要
    阈值，而阈值需要评估数据。现在能诚实说的只有计数。

    调用方**裁剪结果之后必须重算**。计数和分母对不上实际返回的条数，
    模型就会拿这个数字去解读一批它根本没看到的条目 —— 实测
    `_search_tables` 先按 60 条算再裁到 2 条，输出「19/60 条仅由向量
    召回」配 2 条结果，这个提示反而变成了噪声。
    """
    weak = sum(1 for r in results if r.is_weak)
    if not weak:
        return ""
    return (
        f"其中 {weak}/{len(results)} 条仅由向量路召回（关键词路未匹配上），"
        "可靠性较低，引用前建议核对原文。"
    )


class HybridRetriever:
    """两路检索 + RRF 融合。

    构造后即可 search。索引构建较慢（要过一遍 embedding），
    因此支持 save/load —— 二次启动直接读盘。
    """

    def __init__(
        self,
        chunks: list[Chunk],
        bm25: BM25Index,
        dense: DenseIndex,
        embedder: OllamaEmbedder | None = None,
        rrf_k: int = DEFAULT_RRF_K,
    ) -> None:
        self.chunks = chunks
        self.bm25 = bm25
        self.dense = dense
        self.embedder = embedder or OllamaEmbedder()
        self.rrf_k = rrf_k

    # ------------------------------------------------------------ 构建

    @classmethod
    def build(
        cls,
        chunks: list[Chunk],
        embedder: OllamaEmbedder | None = None,
        *,
        verbose: bool = True,
    ) -> "HybridRetriever":
        """从块列表构建两路索引。embedding 是耗时步骤，会打印进度。"""
        emb = embedder or OllamaEmbedder()

        if verbose:
            print(f"构建 BM25 索引（{len(chunks)} 块）...")
        t0 = time.time()
        bm25 = BM25Index()
        bm25.build([(c.text, c.title_str) for c in chunks])
        if verbose:
            print(f"  完成，用时 {time.time() - t0:.1f}s")

        if verbose:
            print(f"构建向量索引（{emb.model}）...")
        t1 = time.time()
        # 编码文本 = 标题路径 + 正文。标题参与编码能让向量带上层级语义，
        # 与 BM25 侧的 title_boost 是同一个动机。
        payload = [f"{c.title_str}\n{c.text}" for c in chunks]
        vectors = emb.embed(payload, verbose=verbose)
        dense = DenseIndex(vectors)
        if verbose:
            print(f"  完成，用时 {time.time() - t1:.1f}s")

        return cls(chunks, bm25, dense, emb)

    # ------------------------------------------------------------ 检索

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        bm25_weight: float = 1.0,
        dense_weight: float = 1.0,
        pool: int = 50,
    ) -> list[RetrievedChunk]:
        """混合检索，只返回结果列表。

        需要知道"这次检索有多可信"的调用方请用 search_checked()。
        本方法同样受门控约束，只是门控原因不外传 —— 想要一个
        "绕过门控"的开关就走 search_checked(gate=False)。
        """
        return self.search_checked(
            query,
            top_k,
            bm25_weight=bm25_weight,
            dense_weight=dense_weight,
            pool=pool,
        ).results

    def search_checked(
        self,
        query: str,
        top_k: int = 5,
        *,
        bm25_weight: float = 1.0,
        dense_weight: float = 1.0,
        pool: int = 50,
        gate: bool = True,
    ) -> SearchOutcome:
        """混合检索 + 可信度判断。

        Args:
            query: 自然语言查询
            top_k: 返回条数
            bm25_weight / dense_weight: 两路在 RRF 中的权重。
                评估阶段要靠调这两个值来验证"混合确实优于单路"。
            pool: 每路各取多少个候选进入融合。取值应显著大于 top_k，
                否则融合退化成两路 top_k 的简单并集。
            gate: 关键词路全库零命中时是否直接判空。评估阶段要对比
                开/关门控的差别，所以做成参数而不是写死。
        """
        query = (query or "").strip()
        if not query:
            return SearchOutcome([], gated=True, note="查询为空。")

        pool = max(pool, top_k)

        # --- 路 1: BM25（字面匹配）---
        # 权重为 0 意味着这一路完全不参与融合，那就连它的排名都不该计算
        # 和展示 —— 否则「纯 BM25 检索」的结果里会标注出 VEC#3 这种
        # 让人误以为向量路参与了排序的信息。
        bm25_rank: dict[int, int] = {}
        toks: list[str] = []
        if bm25_weight > 0:
            toks = tokenize_query(query)
            bm25_hits = self.bm25.search(toks, top_k=pool) if toks else []
            bm25_rank = {h.doc_id: i + 1 for i, h in enumerate(bm25_hits)}

            # --- 门控 ---
            # 判据: **多字词**在全库零命中。单字 token 不算数 ——
            # 中文单字在语料里的命中率极高而区分度为零。实测
            # 「打包膜卡住」被切成「打 / 包膜 / 卡住」，靠一个「打」字
            # 就绕过了门控，返回一堆与打包膜无关的块。
            #
            # 单字仍参与上面的检索（用户搜「屏」该能查到 OLED），
            # 只是没资格否决"库里没有这个概念"。
            #
            # 不在多字词为空时判断: 查询本来就全是单字时无从判断。
            # 也不在 bm25_weight == 0 时判断 —— 那是用户主动要纯向量
            # 检索，零命中是预期行为，不该拦。
            content_toks = [t for t in toks if len(t) > 1]
            if gate and content_toks and not self.bm25.search(content_toks, top_k=1):
                return SearchOutcome([], gated=True, note=GATE_NOTE)

        # --- 路 2: 向量（语义匹配）---
        dense_rank: dict[int, int] = {}
        if dense_weight > 0 and len(self.dense) and self.embedder is not None:
            qvec = self.embedder.embed_one(query)
            dense_hits = self.dense.search(qvec, top_k=pool)
            dense_rank = {doc_id: i + 1 for i, (doc_id, _) in enumerate(dense_hits)}

        # --- RRF 融合 ---
        k = self.rrf_k
        fused: dict[int, float] = {}
        for doc_id, rank in bm25_rank.items():
            fused[doc_id] = fused.get(doc_id, 0.0) + bm25_weight / (k + rank)
        for doc_id, rank in dense_rank.items():
            fused[doc_id] = fused.get(doc_id, 0.0) + dense_weight / (k + rank)

        ranked = sorted(fused.items(), key=lambda kv: -kv[1])[:top_k]

        results = [
            RetrievedChunk(
                chunk=self.chunks[doc_id],
                score=score,
                bm25_rank=bm25_rank.get(doc_id),
                dense_rank=dense_rank.get(doc_id),
                bm25_used=bm25_weight > 0,
            )
            for doc_id, score in ranked
        ]
        return SearchOutcome(results, gated=False, note=evidence_note(results))

    # ------------------------------------------------------------ 持久化

    def save(self, index_dir: str = INDEX_DIR) -> None:
        """把两路索引与块元数据落盘。"""
        os.makedirs(index_dir, exist_ok=True)
        self.bm25.save(os.path.join(index_dir, "bm25.json"))
        self.dense.save(os.path.join(index_dir, "dense.npy"))
        with open(os.path.join(index_dir, "chunks.jsonl"), "w", encoding="utf-8") as f:
            for c in self.chunks:
                f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")
        with open(os.path.join(index_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "model": self.embedder.model,
                    "n_chunks": len(self.chunks),
                    "dim": self.dense.dim,
                    "rrf_k": self.rrf_k,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    @classmethod
    def load(cls, index_dir: str = INDEX_DIR) -> "HybridRetriever":
        """从磁盘恢复索引，不做 embedding —— 秒级启动。"""
        meta_path = os.path.join(index_dir, "meta.json")
        if not os.path.exists(meta_path):
            raise FileNotFoundError(
                f"索引不存在: {index_dir}\n请先运行:  python build_index.py"
            )

        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)

        chunks: list[Chunk] = []
        with open(os.path.join(index_dir, "chunks.jsonl"), encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                chunks.append(
                    Chunk(
                        id=d["id"],
                        source=d["source"],
                        title_path=d["title_path"],
                        text=d["text"],
                        kinds=d.get("kinds", []),
                    )
                )

        bm25 = BM25Index.load(os.path.join(index_dir, "bm25.json"))
        dense = DenseIndex.load(os.path.join(index_dir, "dense.npy"))
        embedder = OllamaEmbedder(model=meta.get("model", "bge-m3"))

        return cls(chunks, bm25, dense, embedder, rrf_k=meta.get("rrf_k", DEFAULT_RRF_K))


if __name__ == "__main__":
    # 自检: 单路 vs 混合的对比 —— 这是验证"混合确实有用"的最小实验
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from chunker import build_chunks

    chunks = build_chunks()
    try:
        retriever = HybridRetriever.build(chunks, verbose=False)
    except EmbeddingError as e:
        print(f"[embedding 不可用] {e}")
        print("\n仅用 BM25 演示:")
        retriever = None

    queries = ["启动工艺的命令是什么", "丝杆反转超时怎么处理", "限位开关失效"]

    for q in queries:
        print()
        print("=" * 76)
        print(f"查询: {q}")
        print("=" * 76)
        if retriever is None:
            continue
        for i, r in enumerate(retriever.search(q, top_k=4), 1):
            print(f"  {i}. [{r.score:.5f}] ({r.sources})")
            print(f"     {r.chunk.title_str[:62]}")
            print(f"     {r.chunk.source}")
