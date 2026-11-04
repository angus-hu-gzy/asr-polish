# -*- coding: utf-8 -*-
"""任务存储：SQLite 一张表 + 严格的状态机。

三条规矩，都在这个文件里落地：

1. **状态迁移必须合法**。每次改状态都过 `_check_transition`，
   非法迁移直接抛 `StateError`，而不是悄悄写坏数据。
2. **「查 + 改」必须原子**。`claim()` 用 `BEGIN IMMEDIATE` 先拿写锁再查，
   两个 worker 不可能抢到同一行。这是 CAS（比较并交换）。
3. **进程被杀不会留下中间态**。任务保持 running、心跳停在旧时间戳，
   由 `reclaim_stale()` 判定僵尸并放回队列。

零依赖：只用标准库 `sqlite3`。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Optional

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

#: 允许的状态迁移。这个表就是状态机的全部定义。
ALLOWED = {
    PENDING: {RUNNING, FAILED},
    RUNNING: {DONE, FAILED, PENDING},   # → PENDING 表示重试
    DONE: set(),                        # 终态，不许再动
    FAILED: {PENDING},                  # 允许人工重试
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL,
    progress    REAL NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    result      TEXT,
    error       TEXT,
    retries     INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 2,
    worker      TEXT,
    created_at  REAL NOT NULL,
    started_at  REAL,
    updated_at  REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
"""


class StateError(RuntimeError):
    """非法状态迁移。"""


@dataclass
class Task:
    id: str
    kind: str
    payload: dict = field(default_factory=dict)
    status: str = PENDING
    progress: float = 0.0
    message: str = ""
    result: Optional[dict] = None
    error: Optional[str] = None
    retries: int = 0
    max_retries: int = 2
    worker: Optional[str] = None
    created_at: float = 0.0
    started_at: Optional[float] = None
    updated_at: float = 0.0
    finished_at: Optional[float] = None

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "kind": self.kind,
            "payload": self.payload,
            "status": self.status,
            "progress": round(self.progress, 4),
            "message": self.message,
            "result": self.result,
            "error": self.error,
            "retries": self.retries,
            "worker": self.worker,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }
        if self.started_at and self.finished_at:
            d["duration"] = round(self.finished_at - self.started_at, 2)
        return d

    @property
    def terminal(self) -> bool:
        return self.status in (DONE, FAILED)


def _row_to_task(row: sqlite3.Row) -> Task:
    def loads(raw):
        return json.loads(raw) if raw else None
    return Task(
        id=row["id"],
        kind=row["kind"],
        payload=loads(row["payload"]) or {},
        status=row["status"],
        progress=row["progress"],
        message=row["message"],
        result=loads(row["result"]),
        error=row["error"],
        retries=row["retries"],
        max_retries=row["max_retries"],
        worker=row["worker"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
    )


def _check_transition(src: str, dst: str) -> None:
    if src == dst:
        # 终态是终点：重复 finish / 重复 fail 必须报错，不能静默放过，
        # 否则「同一个任务被完成两次」这种 bug 永远查不出来。
        if src in (DONE, FAILED):
            raise StateError("任务已处于终态 %s，不能再次设置为 %s" % (src, dst))
        return
    if dst not in ALLOWED.get(src, set()):
        raise StateError("非法状态迁移：%s → %s" % (src, dst))


class Store:
    """任务表。方法全部线程安全（每次操作独立连接 + 写锁）。"""

    def __init__(self, path: str = "tasks.db") -> None:
        self.path = str(path)
        self._local = threading.local()
        #: 所有线程建过的连接。线程本地存储没法枚举，只能自己记账。
        self._all: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")   # 持久属性，设一次即可
            conn.executescript(_SCHEMA)
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        """每个线程复用一条连接。

        两个坑都踩过：
        - 不要每次连接都执行 `PRAGMA journal_mode=WAL`，并发下会把写事务
          搞成 readonly。WAL 是持久属性，初始化时设一次就够。
        - 不要每次操作都 open/close，高频开关在 Windows + WAL 下同样会
          撞上 readonly，顺便还慢。线程本地缓存最省事。
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # check_same_thread=False 是为了 close()：连接本身仍是线程私有的
            # （一条线程一条），但关闭时是在主线程上统一关，默认的线程检查
            # 会直接拒绝，导致 worker 线程的连接全部泄漏。
            conn = sqlite3.connect(self.path, timeout=15.0,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=15000")
            self._local.conn = conn
            with self._all_lock:
                self._all.append(conn)
        return conn

    def _reset_conn(self) -> None:
        """连接状态不可靠时直接丢弃，下次重建。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            with self._all_lock:
                if conn in self._all:
                    self._all.remove(conn)
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def close(self) -> None:
        """关闭**所有线程**创建过的连接。

        只关当前线程那条是不够的：worker 线程退出后它的连接没人负责，
        在 Windows 上会把库文件锁住（测试里表现为删不掉临时目录、以及
        GC 时报 ``unclosed database``）。
        """
        with self._all_lock:
            conns, self._all = self._all, []
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        self._local = threading.local()

    @contextmanager
    def _db(self):
        """一个会真正关闭的连接。sqlite 的 `with conn` 只提交，不关闭。"""
        conn = self._conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                self._reset_conn()
            raise

    # ── 写入 ────────────────────────────────────────────────

    def create(self, kind: str, payload: Optional[dict] = None,
               max_retries: int = 2) -> Task:
        now = time.time()
        task_id = "T" + uuid.uuid4().hex[:10]
        with self._db() as conn:
            conn.execute(
                "INSERT INTO tasks (id, kind, payload, status, max_retries,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (task_id, kind, json.dumps(payload or {}, ensure_ascii=False),
                 PENDING, max_retries, now, now))
            conn.commit()
        task = self.get(task_id)
        assert task is not None
        return task

    def claim(self, worker: str) -> Optional[Task]:
        """原子地取走最早的一个 pending 任务。

        先 BEGIN IMMEDIATE 拿写锁，再 SELECT + UPDATE，
        所以不存在「两个 worker 同时查到同一行」的窗口。
        """
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM tasks WHERE status=? ORDER BY created_at LIMIT 1",
                (PENDING,)).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            now = time.time()
            cur = conn.execute(
                "UPDATE tasks SET status=?, worker=?, started_at=?, updated_at=?"
                " WHERE id=? AND status=?",
                (RUNNING, worker, now, now, row["id"], PENDING))
            if cur.rowcount != 1:      # 写锁在手上时不该发生，防御性处理
                conn.execute("ROLLBACK")
                return None
            conn.execute("COMMIT")
            return self.get(row["id"])
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                self._reset_conn()
            raise

    def heartbeat(self, task_id: str, progress: Optional[float] = None,
                  message: Optional[str] = None) -> None:
        """刷新时间戳。worker 活着但任务很长时靠它证明自己没死。"""
        sets = ["updated_at=?"]
        args: list[Any] = [time.time()]
        if progress is not None:
            sets.append("progress=?")
            args.append(max(0.0, min(1.0, float(progress))))
        if message is not None:
            sets.append("message=?")
            args.append(message)
        args.append(task_id)
        with self._db() as conn:
            conn.execute("UPDATE tasks SET %s WHERE id=?" % ", ".join(sets), args)
            conn.commit()

    def finish(self, task_id: str, result: Optional[dict] = None) -> None:
        self._transition(task_id, DONE, result=result, progress=1.0,
                         message="完成")

    def fail(self, task_id: str, error: str) -> str:
        """失败。未超过重试上限就回队列，否则落成 failed。

        返回最终状态（pending 或 failed）。
        """
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.terminal:
            return task.status
        if task.retries < task.max_retries:
            with self._db() as conn:
                conn.execute(
                    "UPDATE tasks SET status=?, retries=retries+1, worker=NULL,"
                    " started_at=NULL, error=?, message=?, updated_at=?"
                    " WHERE id=?",
                    (PENDING, error[:500], "失败，等待重试", time.time(), task_id))
                conn.commit()
            return PENDING
        self._transition(task_id, FAILED, error=error[:500],
                         message="失败", )
        return FAILED

    def requeue(self, task_id: str, reason: str = "手动重试") -> bool:
        task = self.get(task_id)
        if task is None:
            return False
        if RUNNING == task.status:
            _check_transition(RUNNING, PENDING)
        elif FAILED == task.status:
            _check_transition(FAILED, PENDING)
        else:
            return False
        with self._db() as conn:
            conn.execute(
                "UPDATE tasks SET status=?, worker=NULL, started_at=NULL,"
                " finished_at=NULL, progress=0, message=?, error=NULL, updated_at=?"
                " WHERE id=?",
                (PENDING, reason, time.time(), task_id))
            conn.commit()
        return True

    def reclaim_stale(self, stale_after: float = 600.0) -> int:
        """把卡在 running 太久的任务放回队列（僵尸任务回收）。

        进程被 kill -9 时来不及写任何东西，任务会永远停在 running——
        不报错、也不结束，用户永远等不到。只能靠心跳超时判定。

        必须守住重试上限：否则一个「一跑就把 worker 搞死」的任务
        （比如内存爆掉）会被无限回收、无限重跑，永远落不到 failed，
        用户看到的是「一直在排队」，和僵尸任务一样查不出原因。
        """
        cutoff = time.time() - stale_after
        now = time.time()
        with self._db() as conn:
            # 还有重试预算：回队列，让下一个 worker 接着跑
            cur = conn.execute(
                "UPDATE tasks SET status=?, retries=retries+1, worker=NULL,"
                " started_at=NULL, message=?, updated_at=?"
                " WHERE status=? AND updated_at < ? AND retries < max_retries",
                (PENDING, "心跳超时，已回收重新排队", now, RUNNING, cutoff))
            requeued = cur.rowcount
            # 预算用完：判定失败，把原因摆到台面上
            conn.execute(
                "UPDATE tasks SET status=?, worker=NULL, started_at=NULL,"
                " error=?, message=?, finished_at=?, updated_at=?"
                " WHERE status=? AND updated_at < ? AND retries >= max_retries",
                (FAILED, "心跳超时且重试次数已用尽",
                 "心跳超时且重试已用尽", now, now, RUNNING, cutoff))
            conn.commit()
            return requeued

    def _transition(self, task_id: str, dst: str, result: Optional[dict] = None,
                    error: Optional[str] = None, progress: Optional[float] = None,
                    message: Optional[str] = None) -> None:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        _check_transition(task.status, dst)
        now = time.time()
        sets = ["status=?", "updated_at=?"]
        args: list[Any] = [dst, now]
        if result is not None:
            sets.append("result=?")
            args.append(json.dumps(result, ensure_ascii=False))
        if error is not None:
            sets.append("error=?")
            args.append(error)
        elif dst == DONE:
            # 重试后成功的任务不该继续显示上一次的报错——否则面板里一条
            # 「完成」的任务底下挂着一行红字，看的人只会以为它失败了。
            sets.append("error=?")
            args.append(None)
        if progress is not None:
            sets.append("progress=?")
            args.append(progress)
        if message is not None:
            sets.append("message=?")
            args.append(message)
        if dst in (DONE, FAILED):
            sets.append("finished_at=?")
            args.append(now)
        args.append(task_id)
        with self._db() as conn:
            conn.execute("UPDATE tasks SET %s WHERE id=?" % ", ".join(sets), args)
            conn.commit()

    # ── 读取 ────────────────────────────────────────────────

    def get(self, task_id: str) -> Optional[Task]:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def list(self, status: Optional[str] = None, limit: int = 50,
             offset: int = 0) -> list[Task]:
        sql = "SELECT * FROM tasks"
        args: list[Any] = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        with self._db() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [_row_to_task(r) for r in rows]

    def stats(self) -> dict:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
        out = {PENDING: 0, RUNNING: 0, DONE: 0, FAILED: 0}
        for r in rows:
            out[r["status"]] = r["n"]
        out["total"] = sum(out.values())
        return out

    def purge(self, older_than: float = 86400.0) -> int:
        """删掉 N 秒前结束的任务，避免表无限增长。"""
        cutoff = time.time() - older_than
        with self._db() as conn:
            cur = conn.execute(
                "DELETE FROM tasks WHERE status IN (?,?) AND finished_at IS NOT NULL"
                " AND finished_at < ?", (DONE, FAILED, cutoff))
            conn.commit()
            return cur.rowcount
