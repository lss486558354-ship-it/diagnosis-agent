"""混合检索层。

两路互补的检索器 + RRF 融合:
    bm25   —— 字面精确匹配，擅长命令码、引脚名、十六进制帧这类符号 token
    dense  —— 语义相似匹配，擅长「丝杆不动」≈「丝杆堵转」这类换词表达
    hybrid —— 用 RRF 融合两路排名，不依赖两路分数可比
"""

from .bm25 import BM25Index, Hit
from .tokenizer import tokenize, tokenize_query

__all__ = ["BM25Index", "Hit", "tokenize", "tokenize_query"]
