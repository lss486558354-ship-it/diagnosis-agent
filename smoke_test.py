"""多线程回归测试 —— 补上前两层（demo_search / MCP 协议握手）测不到的并发盲区。

为什么必须有这个文件:
    第 1 层和第 2 层都是单线程的，全绿。但真实会话里 MCP SDK 用线程池执行
    工具函数，SQLite 连接绑死创建线程，6 个工具坏了 3 个（get_case /
    list_faults / record_case）。覆盖率的盲区不在行数，在并发 ——
    这件事只能靠"多线程跑一遍"发现，代码读得再仔细也读不出来。

它测什么:
    [1] 并发首次 get_store()，是否仍然只创建一个实例
    [2] 6 线程同时把 6 个工具各调一遍，任一异常即失败
    [3] 6 线程并发 record_case，新增条数正好是 6 且 ID 互不重复

    第 3 条是重点: 它测的是「算 next id → INSERT」两步之间的原子性。
    只加 check_same_thread=False 能过第 2 条，过不了第 3 条 ——
    两个线程会算出同一个 next id 然后互相覆盖。

刻意没测什么:
    并发首次 get_retriever()。同样是模块级单例，理论上也有重复加载的竞态，
    但 HybridRetriever 是只读的，多创建几个只是浪费内存、不会出错；
    而触发一次要读盘 + 建 BM25，6 线程并发的代价远大于它值当的收益。

怎么跑:
    python smoke_test.py
    退出码 0 = 全通过，1 = 有失败。

数据安全:
    全程在临时目录建库（patch 掉 mcp_server.CASE_DB），不碰 data/cases.db。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from typing import Callable

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import mcp_server  # noqa: E402  (必须在 sys.path 调整之后导入)

THREADS = 6

# 每个线程要调的工具。用闭包而不是 (函数, 参数) 元组，是因为各工具参数
# 长得不一样，摊平成一个调用列表比做一层通用参数映射更好读。
def _tool_calls(idx: int) -> list[tuple[str, Callable[[], str]]]:
    tag = f"并发测试-{idx}"
    return [
        ("search_doc", lambda: mcp_server.search_doc("限位开关 接哪个引脚")),
        ("search_command", lambda: mcp_server.search_command("CMD_SET_PARAM")),
        ("get_wiring", lambda: mcp_server.get_wiring("限位开关 引脚")),
        ("list_faults", lambda: mcp_server.list_faults()),
        ("get_case", lambda: mcp_server.get_case("膜杆不动")),
        (
            "record_case",
            lambda: mcp_server.record_case(
                symptom=f"{tag} 的现象",
                cause=f"{tag} 的原因",
                solution=f"{tag} 的办法",
                module="并发测试",
            ),
        ),
    ]


def _spawn(target: Callable[[threading.Barrier, int], None], threads: int = THREADS) -> None:
    """起 threads 个线程同时执行 target(barrier, idx)，等全部结束。

    用 Barrier 而不是直接 start: 直接 start 的话线程会被调度器错开，
    第一个可能已经跑完了最后一个才开始，"并发"就退化成了串行，
    测试会在有缺陷的代码上照样通过。
    """
    barrier = threading.Barrier(threads)
    workers = [
        threading.Thread(target=target, args=(barrier, i), name=f"T{i}") for i in range(threads)
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join()


def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="diag_concurrency_")
    mcp_server.CASE_DB = os.path.join(tmpdir, "cases.db")  # 单例指向临时库

    failures: list[str] = []
    print(f"临时库: {mcp_server.CASE_DB}")
    print(f"线程数: {THREADS}\n")

    # ---------------------------------------------------------- [1] 单例竞态
    print("[1/3] 并发首次 get_store()")
    mcp_server._store = None
    # 存对象本身而不是 id(): 只留 id 的话实例出了作用域就被回收，
    # 地址会被下一个对象复用，去重结果会假报"只有一个实例"。
    seen: list[object] = []
    grab_errors: list[str] = []
    seen_lock = threading.Lock()

    def grab(barrier: threading.Barrier, idx: int) -> None:
        barrier.wait()
        try:
            store = mcp_server.get_store()
        except Exception as e:  # noqa: BLE001
            # 必须在这里接住 —— 线程里漏出去的异常只会打到 stderr，
            # 主线程照样往下走，测试会在"有 5 个线程崩了"的情况下报 OK。
            with seen_lock:
                grab_errors.append(f"T{idx}: {type(e).__name__}: {e}")
            return
        with seen_lock:
            seen.append(store)

    _spawn(grab)
    distinct = len({id(s) for s in seen})
    if grab_errors:
        msg = f"get_store() 抛异常 {len(grab_errors)}/{THREADS} 次：{grab_errors[0]}"
        failures.append(msg)
        print(f"      FAIL  {msg}")
    elif distinct == 1:
        print(f"      OK    {THREADS} 个线程拿到同一个实例")
    else:
        msg = f"get_store() 有竞态：{THREADS} 个线程创建了 {distinct} 个实例"
        failures.append(msg)
        print(f"      FAIL  {msg}")

    # ------------------------------------------------------- [2] 并发调 6 工具
    print(f"\n[2/3] {THREADS} 线程 × 6 工具")
    print("      预热索引（不计入并发窗口）…")
    mcp_server.get_retriever()

    store = mcp_server.get_store()
    before_ids = {c.id for c in store.all_cases()}

    results: dict[tuple[int, str], tuple[str, str]] = {}
    results_lock = threading.Lock()

    def call_tools(barrier: threading.Barrier, idx: int) -> None:
        barrier.wait()
        for name, fn in _tool_calls(idx):
            try:
                fn()
                entry = ("OK", "")
            except Exception as e:  # noqa: BLE001  —— 测试要的是"任何异常都算失败"
                entry = ("FAIL", f"{type(e).__name__}: {e}")
            with results_lock:
                results[(idx, name)] = entry

    _spawn(call_tools)

    by_tool: dict[str, list[str]] = {}
    for (_idx, name), (status, _msg) in results.items():
        by_tool.setdefault(name, []).append(status)

    for name in sorted(by_tool):
        statuses = by_tool[name]
        n_ok = statuses.count("OK")
        mark = "OK  " if n_ok == len(statuses) else "FAIL"
        print(f"      {mark}  {name:<15} {n_ok}/{len(statuses)}")
        if n_ok != len(statuses):
            failures.append(f"{name}: {len(statuses) - n_ok}/{len(statuses)} 次调用抛异常")

    for key in sorted(results):
        status, msg = results[key]
        if status == "FAIL":
            print(f"            T{key[0]} {key[1]}: {msg}")

    # ------------------------------------------------- [3] 并发写入的 ID 唯一性
    print(f"\n[3/3] 并发 record_case 的 ID 唯一性")
    after_ids = {c.id for c in store.all_cases()}
    new_ids = after_ids - before_ids
    n_ok_record = by_tool.get("record_case", []).count("OK")

    if len(new_ids) == n_ok_record == THREADS:
        print(f"      OK    {THREADS} 次写入 → {len(new_ids)} 条新案例，ID 无重复")
    else:
        msg = (
            f"写入丢失或 ID 撞车：成功调用 {n_ok_record} 次，"
            f"实际新增 {len(new_ids)} 条（期望都是 {THREADS}）"
        )
        failures.append(msg)
        print(f"      FAIL  {msg}")

    # ------------------------------------------------------------------ 收尾
    store.close()
    shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    if failures:
        print(f"结果：失败 {len(failures)} 项")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
