"""稠密向量检索。

为什么用 numpy 暴力计算而不是上向量数据库:
    本语料 293 个块，向量维度 1024。一次全库打分是一个 (293, 1024) @
    (1024,) 的矩阵乘法 —— 在普通 CPU 上是微秒级。引入 Chroma / FAISS
    这类向量库，换来的是毫秒级降到微秒级，代价是多一个依赖、多一层
    持久化、多一处可能出错的配置。在这个规模下那是纯粹的过度设计。

    这个判断有前提: 块数在数千以内。所以下面把阈值写进代码并在超出时
    明确提示 —— 而不是让它悄悄变慢。

关于归一化:
    向量在 embedder 里已做 L2 归一化，因此余弦相似度 = 点积，
    检索时不需要再算模长。
"""

from __future__ import annotations

import os

import numpy as np

# 超过这个块数，暴力检索开始变得不划算，应换用向量索引
BRUTE_FORCE_LIMIT = 20_000


class DenseIndex:
    """向量索引。持有全部块的归一化向量，支持点积检索。"""

    def __init__(self, vectors: np.ndarray) -> None:
        if vectors.ndim != 2:
            raise ValueError(f"向量矩阵应为二维 (n, dim)，实际 {vectors.shape}")
        self.vectors = vectors.astype(np.float32)

    def __len__(self) -> int:
        return int(self.vectors.shape[0])

    @property
    def dim(self) -> int:
        return int(self.vectors.shape[1]) if self.vectors.size else 0

    def search(self, query_vec: np.ndarray, top_k: int = 10) -> list[tuple[int, float]]:
        """检索最相似的 top_k 个块，返回 [(doc_id, 相似度), ...] 按分数降序。

        用 argpartition 先做 O(n) 的部分排序选出候选，再对候选精确排序。
        全量 argsort 是 O(n log n)，在只需要前 10 个时是浪费。
        """
        if not len(self) or query_vec.size == 0:
            return []

        scores = self.vectors @ query_vec.astype(np.float32)
        k = min(top_k, len(scores))

        if k < len(scores):
            cand = np.argpartition(-scores, k - 1)[:k]
        else:
            cand = np.arange(len(scores))

        order = cand[np.argsort(-scores[cand])]
        return [(int(i), float(scores[i])) for i in order]

    # ------------------------------------------------------------ 持久化

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        np.save(path, self.vectors)

    @classmethod
    def load(cls, path: str) -> "DenseIndex":
        # np.save 会自动补 .npy 后缀，加载时容错处理
        if not os.path.exists(path) and os.path.exists(path + ".npy"):
            path = path + ".npy"
        return cls(np.load(path))

    def stats(self) -> dict:
        return {
            "块数": len(self),
            "向量维度": self.dim,
            "占用内存": f"{self.vectors.nbytes / 1024 / 1024:.2f} MB",
            "检索方式": "numpy 暴力点积" + (
                "（已接近建议上限，考虑换向量索引）" if len(self) > BRUTE_FORCE_LIMIT else ""
            ),
        }


if __name__ == "__main__":
    # 自检: 用随机向量验证检索逻辑与 top_k 边界
    rng = np.random.default_rng(42)
    n, dim = 100, 32

    mat = rng.standard_normal((n, dim)).astype(np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)

    idx = DenseIndex(mat)
    for k, v in idx.stats().items():
        print(f"  {k:10s} {v}")

    # 用库中第 7 条自身作查询，它应当以相似度 1.0 排第一
    q = mat[7]
    hits = idx.search(q, top_k=3)
    print("\n用第 7 条向量自查询（Top-1 应为 7 且相似度≈1.0）:")
    for doc_id, score in hits:
        print(f"  doc {doc_id:3d}   相似度 {score:+.6f}")

    assert hits[0][0] == 7, "自查询未命中自身，检索逻辑有误"
    assert abs(hits[0][1] - 1.0) < 1e-5, "归一化向量自相似度应为 1.0"

    # 边界: top_k 超过库容量不应报错
    assert len(idx.search(q, top_k=999)) == n, "top_k 超界处理有误"
    # 边界: 空查询
    assert idx.search(np.zeros(dim, dtype=np.float32), top_k=3) is not None

    print("\n  全部断言通过")
