"""Ollama embedding 客户端。

为什么直接用 urllib 而不是 `ollama` 包或 `requests`:
    Ollama 的 HTTP API 只有两个端点、请求体是普通 JSON。为这个引入一个
    依赖不划算 —— 本项目的新增依赖已经压到 jieba + langgraph 两个，
    能省一个是一个。标准库 urllib 足够，也少一层版本兼容风险。

    Ollama 服务地址固定为本机 11434，不做可配置化 —— 本项目就是
    本机跑，加一个环境变量只会多一处可能配错的地方。

关于批量:
    优先用新版 /api/embed（支持一次提交多条），失败再回退到
    单条 /api/embeddings。这个回退不是防御性编程 —— 老版本 Ollama
    确实没有 /api/embed，而用户机器上的版本我无法预设。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import numpy as np

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "bge-m3"


class EmbeddingError(RuntimeError):
    """embedding 调用失败。携带可操作的提示信息。"""


class OllamaEmbedder:
    """把文本编码成向量。

    输出向量做 L2 归一化 —— 归一化之后余弦相似度退化为点积，
    检索时可以直接用矩阵乘法，省掉每次算模长的开销。
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        batch_size: int = 32,
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.batch_size = max(1, batch_size)
        self.timeout = timeout

    # ------------------------------------------------------------ 连通性

    def available_models(self) -> list[str]:
        """列出本机已安装的模型。用于给出「请先 pull」这类可操作提示。"""
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=5) as r:
                data = json.load(r)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise EmbeddingError(
                f"连不上 Ollama 服务 ({self.host})。请确认 Ollama 正在运行。\n原因: {e}"
            ) from e
        return [m.get("name", "") for m in data.get("models", [])]

    def ensure_ready(self) -> None:
        """检查模型是否可用，不可用则抛出带修复命令的错误。"""
        names = self.available_models()
        # Ollama 的模型名可能带 :latest 后缀，做前缀匹配
        if not any(n == self.model or n.split(":")[0] == self.model for n in names):
            raise EmbeddingError(
                f"模型 {self.model} 未安装。\n"
                f"请执行:  ollama pull {self.model}\n"
                f"当前已安装: {', '.join(names) if names else '(无)'}"
            )

    # ------------------------------------------------------------ 编码

    def _post(self, path: str, payload: dict) -> dict:
        """发一个 JSON POST，统一错误处理。"""
        req = urllib.request.Request(
            f"{self.host}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="ignore")[:200]
            except Exception:  # noqa: BLE001 - 读取错误详情失败不应掩盖原始错误
                pass
            raise EmbeddingError(f"Ollama 返回 HTTP {e.code}: {body}") from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise EmbeddingError(f"请求 Ollama 失败: {e}") from e

    def _embed_batch_new(self, texts: list[str]) -> np.ndarray | None:
        """新版 /api/embed 批量接口。接口不存在时返回 None 交给回退路径。"""
        try:
            data = self._post("/api/embed", {"model": self.model, "input": texts})
        except EmbeddingError:
            return None
        vecs = data.get("embeddings")
        if not vecs:
            return None
        return np.asarray(vecs, dtype=np.float32)

    def _embed_one_legacy(self, text: str) -> np.ndarray:
        """旧版 /api/embeddings 单条接口。"""
        data = self._post("/api/embeddings", {"model": self.model, "prompt": text})
        vec = data.get("embedding")
        if not vec:
            raise EmbeddingError(f"Ollama 未返回向量，响应键: {list(data.keys())}")
        return np.asarray(vec, dtype=np.float32)

    def embed(self, texts: list[str], *, verbose: bool = False) -> np.ndarray:
        """把一批文本编码成 (n, dim) 的归一化向量矩阵。

        自动分片提交，避免一次塞太多导致超时或显存峰值。
        """
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)

        chunks: list[np.ndarray] = []
        total = len(texts)
        t0 = time.time()

        for start in range(0, total, self.batch_size):
            batch = texts[start : start + self.batch_size]

            mat = self._embed_batch_new(batch)
            if mat is None:
                # 回退: 逐条调用旧接口
                mat = np.stack([self._embed_one_legacy(t) for t in batch])

            chunks.append(mat)
            if verbose:
                done = min(start + len(batch), total)
                rate = done / max(time.time() - t0, 1e-6)
                print(f"  编码 {done}/{total}  ({rate:.1f} 条/秒)")

        out = np.concatenate(chunks, axis=0)

        # L2 归一化: 之后余弦相似度 = 点积
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (out / norms).astype(np.float32)

    def embed_one(self, text: str) -> np.ndarray:
        """编码单条文本，返回 (dim,) 向量。查询侧使用。"""
        return self.embed([text])[0]


if __name__ == "__main__":
    # 自检: 确认服务可用、模型就位、相似度语义合理
    import sys

    emb = OllamaEmbedder()
    print(f"Ollama: {emb.host}")
    try:
        names = emb.available_models()
        print(f"已安装模型: {', '.join(names) if names else '(无)'}")
        emb.ensure_ready()
        print(f"模型 {emb.model} 就绪\n")
    except EmbeddingError as e:
        print(f"\n[不可用] {e}")
        sys.exit(1)

    pairs = [
        ("丝杆反转超时", "丝杆堵转不动作"),
        ("丝杆反转超时", "波特率设置"),
        ("限位开关失效", "行程开关损坏"),
    ]
    print("语义相似度自检（前者应显著高于后者）:")
    texts = sorted({t for p in pairs for t in p})
    vecs = emb.embed(texts, verbose=True)
    idx = {t: i for i, t in enumerate(texts)}

    for a, b in pairs:
        sim = float(vecs[idx[a]] @ vecs[idx[b]])
        print(f"  {sim:+.4f}   {a}  <->  {b}")
