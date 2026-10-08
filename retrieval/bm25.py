"""BM25 稀疏检索。

为什么中文技术文档的向量检索不够、必须补一路关键词检索:
    向量模型对「专有名词」和「精确字面」天然不敏感。用户问
    「CMD_PROCESS_START 是哪个命令码」，embedding 会把这句话编码成
    「询问某个命令」的泛化语义，而文档里 `0x30`、`CMD_PROCESS_START`
    这类字面 token 恰恰是判别性最强的信号，却容易在向量空间里被抹平。
    BM25 按字面精确匹配，正好补这个短板。

标题加权:
    本语料的标题本身就是强语义信号 —— 一个块标题路径是
    「上位机通信协议 > 命令码 > 工艺控制」，它命中"命令"这个词的
    重要程度远高于正文里偶然出现一次"命令"。
    做法是把标题路径的 token 在文档词频里重复 title_boost 次，
    让 BM25 的 tf 分量自然放大它。这比事后乘一个系数更符合 BM25 的
    概率模型，也少一个需要调的参数。
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter, defaultdict
from dataclasses import dataclass

from .tokenizer import tokenize


@dataclass
class Hit:
    """一条检索结果。"""

    doc_id: int
    score: float


class BM25Index:
    """标准 BM25 (Okapi) 索引。

    参数取业界常用默认值 k1=1.5 / b=0.75 —— 在中文短文档上这两个值
    已经足够稳，本项目不打算把它们当作调参对象（调了也说不清收益从哪来）。
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75, title_boost: int = 3) -> None:
        self.k1 = k1
        self.b = b
        self.title_boost = title_boost

        self.n_docs: int = 0
        self.avgdl: float = 0.0
        self.doc_len: list[int] = []
        self.tf: list[Counter[str]] = []              # 每篇文档的词频
        self.inverted: dict[str, dict[int, int]] = defaultdict(dict)
        self.df: dict[str, int] = {}                  # 词 → 出现该词的文档数
        self._idf_cache: dict[str, float] = {}

    # ------------------------------------------------------------ 构建

    def build(self, docs: list[tuple[str, str]]) -> None:
        """构建索引。

        Args:
            docs: [(正文, 标题路径文本), ...]，顺序即 doc_id 顺序。
        """
        self.n_docs = len(docs)
        self.doc_len = []
        self.tf = []
        self.inverted = defaultdict(dict)
        self.df = {}
        self._idf_cache = {}

        for doc_id, (body, title) in enumerate(docs):
            counter: Counter[str] = Counter(tokenize(body))
            # 标题 token 重复计入，放大 tf 分量
            if title:
                for tok in tokenize(title):
                    counter[tok] += self.title_boost

            self.tf.append(counter)
            self.doc_len.append(sum(counter.values()))

            for term, freq in counter.items():
                self.inverted[term][doc_id] = freq
                self.df[term] = self.df.get(term, 0) + 1

        self.avgdl = (sum(self.doc_len) / self.n_docs) if self.n_docs else 0.0

    # ------------------------------------------------------------ 打分

    def _idf(self, term: str) -> float:
        """逆文档频率。采用 log(1 + ...) 形式保证非负 ——

        经典 BM25 的 idf 在词出现在超过半数文档时会变负值，导致
        「出现该词反而扣分」。加 1 的平滑形式消除了这个反直觉行为。
        """
        cached = self._idf_cache.get(term)
        if cached is not None:
            return cached

        df = self.df.get(term, 0)
        if df == 0:
            val = 0.0
        else:
            val = math.log(1.0 + (self.n_docs - df + 0.5) / (df + 0.5))
        self._idf_cache[term] = val
        return val

    def search(self, query_tokens: list[str], top_k: int = 10) -> list[Hit]:
        """检索。

        只遍历查询词命中的倒排列表，不扫全库 —— 这是 BM25 相对于
        暴力向量检索的效率优势所在，也是两路检索各自的取舍点。
        """
        if not self.n_docs or not query_tokens:
            return []

        scores: dict[int, float] = defaultdict(float)
        avgdl = self.avgdl or 1.0

        for term in query_tokens:
            postings = self.inverted.get(term)
            if not postings:
                continue
            idf = self._idf(term)
            if idf <= 0.0:
                continue

            for doc_id, freq in postings.items():
                dl = self.doc_len[doc_id] or 1
                denom = freq + self.k1 * (1.0 - self.b + self.b * dl / avgdl)
                scores[doc_id] += idf * freq * (self.k1 + 1.0) / denom

        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]
        return [Hit(doc_id=d, score=s) for d, s in ranked]

    # ------------------------------------------------------------ 持久化

    def save(self, path: str) -> None:
        """存盘。用 JSON 而非 pickle —— 便于人工检查词表与倒排结构。"""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        payload = {
            "params": {"k1": self.k1, "b": self.b, "title_boost": self.title_boost},
            "n_docs": self.n_docs,
            "avgdl": self.avgdl,
            "doc_len": self.doc_len,
            "tf": [{k: v for k, v in c.items()} for c in self.tf],
            "df": self.df,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> "BM25Index":
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)

        idx = cls(**payload["params"])
        idx.n_docs = payload["n_docs"]
        idx.avgdl = payload["avgdl"]
        idx.doc_len = payload["doc_len"]
        idx.tf = [Counter(d) for d in payload["tf"]]
        idx.df = payload["df"]

        idx.inverted = defaultdict(dict)
        for doc_id, counter in enumerate(idx.tf):
            for term, freq in counter.items():
                idx.inverted[term][doc_id] = freq

        return idx

    # ------------------------------------------------------------ 诊断

    def stats(self) -> dict:
        """索引概况，用于确认索引没建歪。"""
        vocab = len(self.df)
        hapax = sum(1 for t, c in self.df.items() if c == 1)
        return {
            "文档数": self.n_docs,
            "词表大小": vocab,
            "平均文档长度": round(self.avgdl, 1),
            "只出现一次的词": hapax,
            "只出现一次占比": f"{hapax / vocab * 100:.1f}%" if vocab else "0%",
            "最长文档": max(self.doc_len) if self.doc_len else 0,
        }


if __name__ == "__main__":
    # 自检: 用真实语料建索引，跑几个查询看排序是否合理
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from chunker import build_chunks

    chunks = build_chunks()
    print(f"载入 {len(chunks)} 个块，开始建索引 ...")

    index = BM25Index()
    index.build([(c.text, c.title_str) for c in chunks])

    print()
    for k, v in index.stats().items():
        print(f"  {k:16s} {v}")

    queries = [
        "启动工艺的命令是什么",
        "丝杆超时怎么处理",
        "限位开关失效",
        "CMD_PROCESS_START",
        "波特率 115200",
    ]
    print()
    print("=" * 74)
    for q in queries:
        toks = tokenize(q)
        hits = index.search(toks, top_k=3)
        print(f"\n查询: {q}")
        print(f"  分词: {toks}")
        for h in hits:
            c = chunks[h.doc_id]
            print(f"  [{h.score:6.2f}] {c.title_str[:56]}")
            print(f"           {c.source}")
