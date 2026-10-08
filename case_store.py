"""案例库 —— 从工程文档提取的真实故障案例，SQLite 持久化。

为什么案例库要独立于文档检索:
    文档检索回答"文档里怎么写的"，案例库回答"我们实际遇到过什么"。
    这是两种不同性质的知识 —— 前者是设计意图，后者是现场事实。
    混在一起检索，结果里就分不清"这是规范要求"还是"这是踩过的坑"。

为什么不用 SQLite FTS5:
    本机 SQLite 未必编译了 FTS5；而 FTS5 自带的分词器对中文技术术语
    （CMD_PROCESS_START、0x30、PA9）处理很差，会把符号切碎成无意义碎片。
    改用 retrieval.tokenizer 预处理出 search_text 字段，好处是案例库与
    文档检索共享同一套术语保护与同义词归一化 —— 用户搜"丝杠"，文档侧
    和案例侧都能命中"丝杆"。代价是查不了模糊拼写，但技术术语场景下
    精确匹配本来就是更该要的行为。

kind 字段为什么必须存在:
    root_cause —— 文档给出了确定根因（如"急停按钮接成常开"）
    checklist  —— 文档给的是排查清单（如"电机不转"列了 4 个可能原因）
    这两类不能混着算命中率。清单类本来就没有唯一答案，拿它算 Top-3
    命中率会人为压低分数。评估时必须分开统计，否则数字没有意义。
"""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime

from retrieval.tokenizer import tokenize, tokenize_query

DEFAULT_DB = os.path.join("data", "cases.db")
SEED_PATH = "cases_seed.json"

KIND_ROOT_CAUSE = "root_cause"
KIND_CHECKLIST = "checklist"
VALID_KINDS = frozenset({KIND_ROOT_CAUSE, KIND_CHECKLIST})

MODULE_OTHER = "其他"

# 单字 token 区分度太低（搜"机"会命中一切含"机"的案例），
# 但如果查询切出来全是单字，那也只能用 —— 总比返回空好。
MIN_TOKEN_LEN = 2

# 症状字段的命中权重倍数。
#
# 原因: 搜「急停按了没反应」时，「按下急停按钮无法断电」和「电机不响应」
# 都能靠词命中 —— 但前者是症状本身，后者只是在解决办法里提了一句
# "检查急停按钮"。不加权的话两者同为 1 分，排序退化成按 id 排，
# 等于随机。症状是用户描述问题的入口，它命中就该比正文提及更重要。
SYMPTOM_WEIGHT = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id             TEXT PRIMARY KEY,
    symptom        TEXT NOT NULL,
    module         TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL DEFAULT 'checklist',
    cause          TEXT NOT NULL,
    solution       TEXT NOT NULL,
    source_doc     TEXT NOT NULL DEFAULT '',
    source_section TEXT NOT NULL DEFAULT '',
    verified       INTEGER NOT NULL DEFAULT 0,
    search_text    TEXT NOT NULL DEFAULT '',
    symptom_text   TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_module ON cases(module);
CREATE INDEX IF NOT EXISTS idx_cases_kind   ON cases(kind);
"""


@dataclass(frozen=True)
class Case:
    """一条故障案例。frozen 是刻意的 —— 案例一旦入库就不该被就地改写，
    修改应当走 add/update 产生新版本，这样 created_at 才有意义。"""

    id: str
    symptom: str
    module: str
    kind: str
    cause: str
    solution: str
    source_doc: str
    source_section: str
    verified: bool
    created_at: str

    @property
    def source(self) -> str:
        """来源的可读形式，供工具输出直接引用。"""
        if self.source_doc and self.source_section:
            return f"{self.source_doc} > {self.source_section}"
        return self.source_doc or "(未标注来源)"

    @property
    def is_root_cause(self) -> bool:
        """是否有确定根因。评估时只有这类能算 Top-3 命中率。"""
        return self.kind == KIND_ROOT_CAUSE

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "symptom": self.symptom,
            "module": self.module,
            "kind": self.kind,
            "cause": self.cause,
            "solution": self.solution,
            "source_doc": self.source_doc,
            "source_section": self.source_section,
            "verified": self.verified,
            "created_at": self.created_at,
        }


def build_search_text(*parts: str) -> str:
    """把若干字段拼成用于 LIKE 匹配的 token 串。

    分词而非原文直存，是为了让同义词归一化生效:
    用户搜"丝杠"，tokenizer 把它归一成"丝杆"，才能命中这里存的"丝杆"。
    """
    return " ".join(tokenize(" ".join(p for p in parts if p)))


def _query_tokens(query: str) -> list[str]:
    """查询侧分词。丢弃单字 token，除非切出来全是单字。"""
    tokens = tokenize_query(query)
    long_enough = [t for t in tokens if len(t) >= MIN_TOKEN_LEN]
    return long_enough or tokens


def _row_to_case(row: sqlite3.Row) -> Case:
    return Case(
        id=row["id"],
        symptom=row["symptom"],
        module=row["module"],
        kind=row["kind"],
        cause=row["cause"],
        solution=row["solution"],
        source_doc=row["source_doc"],
        source_section=row["source_section"],
        verified=bool(row["verified"]),
        created_at=row["created_at"],
    )


def _synchronized(method):
    """把方法体放进 self._lock 里执行。

    为什么需要锁，不只是为了绕开 check_same_thread:
        add() 内部是「算下一个 ID（_next_id）→ INSERT」两步。两个线程
        并发时，即使各自的连接都合法，也可能算出同一个 next id 然后
        互相覆盖。锁保证的是写入的原子性，跨线程只是顺带解决的。

    为什么用 RLock 而不是 Lock:
        add() 内部会调用同样上了锁的 _next_id() 和 get()，不可重入的
        锁会在这里自己锁死。
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class CaseStore:
    """故障案例库。构造即建表，可直接用于增删查。

    线程安全: MCP SDK 用线程池执行工具函数，同一个 CaseStore 实例会被
    多个线程同时使用。串行化由 _synchronized 负责。
    """

    def __init__(self, db_path: str = DEFAULT_DB) -> None:
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        # check_same_thread=False: 这个连接注定要被多个线程使用，
        # sqlite3 自带的线程归属检查在这里只会误伤。安全性改由
        # _synchronized 提供的串行化保证，不依赖那个检查。
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # 构造期间对象尚未逃逸到其他线程，建表与迁移无需持锁
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """补齐老库缺失的列并回填。

        SQLite 的 ADD COLUMN 不接受"非空且无默认值"，所以先给空串默认值
        再回填。回填用当前分词器重新切一遍，这顺带刷新了分词器升级前
        写入的旧 search_text —— 否则升级分词器后老数据会静默失配。
        """
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(cases)")}
        if "symptom_text" not in cols:
            self._conn.execute(
                "ALTER TABLE cases ADD COLUMN symptom_text TEXT NOT NULL DEFAULT ''"
            )

        stale = self._conn.execute(
            "SELECT id, symptom, module, cause, solution FROM cases WHERE symptom_text = ''"
        ).fetchall()
        for r in stale:
            self._conn.execute(
                "UPDATE cases SET symptom_text = ?, search_text = ? WHERE id = ?",
                (
                    build_search_text(r["symptom"]),
                    build_search_text(r["symptom"], r["module"], r["cause"], r["solution"]),
                    r["id"],
                ),
            )

    # ------------------------------------------------------------ 写入

    @_synchronized
    def add(
        self,
        *,
        symptom: str,
        cause: str,
        solution: str,
        module: str = "",
        kind: str = KIND_CHECKLIST,
        source_doc: str = "",
        source_section: str = "",
        verified: bool = False,
        case_id: str = "",
        created_at: str = "",
    ) -> Case:
        """新增一条案例。case_id 留空则自动编号。

        校验失败直接抛 ValueError 而不是静默修正 —— 案例库是要拿去
        评估和展示的，脏数据比缺数据更难发现。
        """
        symptom = (symptom or "").strip()
        cause = (cause or "").strip()
        solution = (solution or "").strip()

        if not symptom:
            raise ValueError("symptom（故障现象）不能为空")
        if not cause:
            raise ValueError("cause（原因）不能为空")
        if not solution:
            raise ValueError("solution（解决办法）不能为空")
        if kind not in VALID_KINDS:
            raise ValueError(f"kind 必须是 {sorted(VALID_KINDS)} 之一，收到 {kind!r}")

        cid = (case_id or "").strip() or self._next_id()
        ts = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self._conn.execute(
            """
            INSERT INTO cases (id, symptom, module, kind, cause, solution,
                               source_doc, source_section, verified,
                               search_text, symptom_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cid,
                symptom,
                (module or "").strip(),
                kind,
                cause,
                solution,
                (source_doc or "").strip(),
                (source_section or "").strip(),
                1 if verified else 0,
                build_search_text(symptom, module, cause, solution),
                build_search_text(symptom),
                ts,
            ),
        )
        self._conn.commit()
        return self.get(cid)  # type: ignore[return-value]

    @_synchronized
    def mark_verified(self, case_id: str) -> bool:
        """标记某条案例已经人工校对。返回是否真的改到了行。"""
        cur = self._conn.execute("UPDATE cases SET verified = 1 WHERE id = ?", (case_id,))
        self._conn.commit()
        return cur.rowcount > 0

    @_synchronized
    def delete(self, case_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))
        self._conn.commit()
        return cur.rowcount > 0

    @_synchronized
    def _next_id(self) -> str:
        """按数字部分取最大值再 +1。

        不能直接用 ORDER BY id DESC —— 'CASE-009' 的字符串序大于 'CASE-010'。
        """
        row = self._conn.execute(
            "SELECT MAX(CAST(SUBSTR(id, 6) AS INTEGER)) AS n "
            "FROM cases WHERE id LIKE 'CASE-%'"
        ).fetchone()
        n = row["n"] if row and row["n"] is not None else 0
        return f"CASE-{n + 1:03d}"

    # ------------------------------------------------------------ 读取

    @_synchronized
    def get(self, case_id: str) -> Case | None:
        row = self._conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        return _row_to_case(row) if row else None

    @_synchronized
    def search(
        self,
        query: str,
        *,
        module: str = "",
        kind: str = "",
        include_unverified: bool = True,
        limit: int = 5,
    ) -> list[tuple[Case, int]]:
        """按症状关键词检索案例。

        返回 (案例, 命中词数) 并按命中数降序。命中数是"这条案例与查询
        共享几个词"，不是相似度分数 —— 案例库只有几十条，用不上 BM25
        那套 tf-idf；共享词数在这个量级上已经足够区分。

        排序里 verified 优先于 id 只是打破平局，不参与主排序 ——
        已校对的案例不该因为"校对过"就压过命中更多的案例。
        """
        tokens = _query_tokens(query)
        if not tokens:
            return []

        like_params = [f"%{t}%" for t in tokens]
        # 症状命中记 SYMPTOM_WEIGHT 分，正文提及记 1 分
        score_expr = " + ".join(
            f"(symptom_text LIKE ?) * {SYMPTOM_WEIGHT} + (search_text LIKE ?)"
            for _ in tokens
        )
        score_params: list[str] = []
        for p in like_params:
            score_params.extend((p, p))

        where = ["(" + " OR ".join("search_text LIKE ?" for _ in tokens) + ")"]
        where_params = list(like_params)

        if module:
            where.append("module = ?")
            where_params.append(module)
        if kind:
            where.append("kind = ?")
            where_params.append(kind)
        if not include_unverified:
            where.append("verified = 1")

        sql = (
            f"SELECT *, ({score_expr}) AS hits FROM cases "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY hits DESC, verified DESC, id LIMIT ?"
        )
        # SELECT 子句里的占位符在 SQL 文本中更靠前，参数顺序必须对应
        rows = self._conn.execute(sql, score_params + where_params + [limit]).fetchall()
        return [(_row_to_case(r), int(r["hits"])) for r in rows]

    @_synchronized
    def modules(self) -> list[tuple[str, int]]:
        """模块名及其案例数，供概览使用。"""
        rows = self._conn.execute(
            "SELECT CASE WHEN module = '' THEN ? ELSE module END AS m, COUNT(*) AS n "
            "FROM cases GROUP BY m ORDER BY n DESC",
            (MODULE_OTHER,),
        ).fetchall()
        return [(r["m"], int(r["n"])) for r in rows]

    @_synchronized
    def all_cases(self, *, limit: int = 0) -> list[Case]:
        sql = "SELECT * FROM cases ORDER BY id"
        if limit > 0:
            sql += " LIMIT ?"
            rows = self._conn.execute(sql, (limit,)).fetchall()
        else:
            rows = self._conn.execute(sql).fetchall()
        return [_row_to_case(r) for r in rows]

    @_synchronized
    def stats(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"]
        verified = self._conn.execute(
            "SELECT COUNT(*) AS n FROM cases WHERE verified = 1"
        ).fetchone()["n"]
        by_kind = {
            r["kind"]: int(r["n"])
            for r in self._conn.execute("SELECT kind, COUNT(*) AS n FROM cases GROUP BY kind")
        }
        return {
            "总案例数": total,
            "已校对": verified,
            "待校对": total - verified,
            "有确定根因": by_kind.get(KIND_ROOT_CAUSE, 0),
            "排查清单类": by_kind.get(KIND_CHECKLIST, 0),
            "数据库": self.db_path,
        }

    # ------------------------------------------------------------ 种子数据

    @_synchronized
    def load_seed(self, path: str = SEED_PATH, *, overwrite: bool = False) -> tuple[int, int]:
        """导入种子案例，返回 (新增数, 跳过数)。

        overwrite 默认 False —— 种子文件只是原始提取结果，用户校对过的
        版本（verified=1）不应被重新导入冲掉。这正是把 verified 单独
        存一列而不是塞进备注的原因。
        """
        if not os.path.exists(path):
            raise FileNotFoundError(f"种子文件不存在: {path}")

        with open(path, encoding="utf-8") as f:
            seeds = json.load(f)

        if not isinstance(seeds, list):
            raise ValueError(f"种子文件应为 JSON 数组，实际是 {type(seeds).__name__}")

        added = skipped = 0
        for item in seeds:
            cid = str(item.get("id", "")).strip()
            if cid and self.get(cid) is not None:
                if not overwrite:
                    skipped += 1
                    continue
                self.delete(cid)

            self.add(
                case_id=cid,
                symptom=item.get("symptom", ""),
                cause=item.get("cause", ""),
                solution=item.get("solution", ""),
                module=item.get("module", ""),
                kind=item.get("kind", KIND_CHECKLIST),
                source_doc=item.get("source_doc", ""),
                source_section=item.get("source_section", ""),
                verified=bool(item.get("verified", False)),
                created_at=item.get("created_at", ""),
            )
            added += 1

        return added, skipped

    # ------------------------------------------------------------ 生命周期

    @_synchronized
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CaseStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


if __name__ == "__main__":
    # 自检：建库 → 导入种子 → 搜几条真实症状
    import sys

    store = CaseStore()
    added, skipped = store.load_seed()
    print(f"种子导入: 新增 {added} 条，跳过 {skipped} 条")
    print()
    for k, v in store.stats().items():
        print(f"  {k:12s} {v}")

    print()
    print("模块分布:")
    for name, n in store.modules():
        print(f"  {name:12s} {n} 条")

    print()
    for q in ["膜杆不动", "急停按了没反应", "电机速度自己变快", "舵机抖动", "串口连不上"]:
        hits = store.search(q, limit=3)
        print()
        print(f"查询「{q}」→ {len(hits)} 条")
        for case, score in hits:
            tag = "根因" if case.is_root_cause else "清单"
            print(f"   [{score}分/{tag}] {case.id}  {case.symptom[:44]}")
            print(f"            {case.source}")

    store.close()
    sys.exit(0)
