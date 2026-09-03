#!/usr/bin/env python3
"""bot_thread.py 并发写入测试: 5 进程 × 20 消息, 断言零丢失零重复。"""
import multiprocessing as mp
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bot_thread import DB_PATH, append, db  # noqa: E402


def worker(thread_id: int, n: int, proc: int):
    conn = db()
    for i in range(n):
        append(conn, thread_id, f"p{proc}", "human", f"msg-{proc}-{i}")
    conn.close()


def main():
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO threads(name,members,created_at) VALUES(?,?,?)",
        ("cas-test", "default", time.time()),
    )
    conn.commit()
    tid = conn.execute(
        "SELECT id FROM threads WHERE name='cas-test'"
    ).fetchone()[0]
    conn.execute("DELETE FROM messages WHERE thread_id=?", (tid,))
    conn.commit()

    procs, n = 5, 20
    t0 = time.time()
    ps = [mp.Process(target=worker, args=(tid, n, i)) for i in range(procs)]
    [p.start() for p in ps]
    [p.join() for p in ps]

    c2 = sqlite3.connect(DB_PATH)
    total = c2.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id=?", (tid,)
    ).fetchone()[0]
    seqs = [r[0] for r in c2.execute(
        "SELECT seq FROM messages WHERE thread_id=? ORDER BY seq", (tid,)
    ).fetchall()]
    dup = len(seqs) - len(set(seqs))
    contiguous = seqs == list(range(1, total + 1))
    print(f"进程={procs} 每进程={n} 期望={procs*n} 实际={total} "
          f"重复seq={dup} 连续={contiguous} 耗时={time.time()-t0:.1f}s")
    assert total == procs * n, f"消息丢失: {total} != {procs*n}"
    assert dup == 0, f"seq 重复: {dup}"
    assert contiguous, "seq 不连续"
    print("PASS: 并发写入零丢失零重复")


if __name__ == "__main__":
    main()
