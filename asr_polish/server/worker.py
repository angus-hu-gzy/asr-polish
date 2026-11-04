# -*- coding: utf-8 -*-
"""Worker 池：从任务表里领活、干活、汇报。

三个角色在一个进程里跑：

- **worker 线程**：`claim()` 抢任务 → 执行 → `finish()` / `fail()`
- **心跳**：长任务执行期间定期刷新 `updated_at`，证明自己没死
- **janitor 线程**：扫「running 但心跳停了」的任务，判定僵尸并放回队列

为什么必须有 janitor：worker 被 `kill -9` 或机器断电时，进程来不及写任何东西，
任务会永远停在 running——不报错、不结束、用户永远等不到。这类故障只能靠
「心跳超时」间接发现，没有别的办法。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from . import jobs
from .store import FAILED, Task, Store

Event = Callable[[str], None]


class WorkerPool:
    def __init__(self, store: Store, workers: int = 2,
                 stale_after: float = 600.0, poll: float = 0.4,
                 heartbeat: float = 1.0,
                 on_event: Optional[Event] = None) -> None:
        self.store = store
        self.n = max(1, int(workers))
        self.stale_after = stale_after
        self.poll = poll
        self.heartbeat = heartbeat
        self._on_event = on_event or (lambda _m: None)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._janitor: Optional[threading.Thread] = None
        self.busy: dict[str, Optional[str]] = {}

    # ── 生命周期 ────────────────────────────────────────────

    def start(self) -> None:
        for i in range(self.n):
            name = "worker-%d" % (i + 1)
            self.busy[name] = None
            t = threading.Thread(target=self._loop, args=(name,),
                                 name=name, daemon=True)
            t.start()
            self._threads.append(t)
        self._janitor = threading.Thread(target=self._janitor_loop,
                                         name="janitor", daemon=True)
        self._janitor.start()
        self._on_event("worker 池已启动：%d 个 worker，僵尸判定 %ds"
                       % (self.n, int(self.stale_after)))

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)
        if self._janitor:
            self._janitor.join(timeout=timeout)
        self._on_event("worker 池已停止")

    def status(self) -> list[dict]:
        return [{"worker": k, "task": v} for k, v in sorted(self.busy.items())]

    # ── 主循环 ──────────────────────────────────────────────

    def _loop(self, name: str) -> None:
        while not self._stop.is_set():
            try:
                task = self.store.claim(name)
            except Exception as e:                       # 数据库暂时不可用
                self._on_event("%s 领任务出错：%s" % (name, e))
                self._stop.wait(1.0)
                continue
            if task is None:
                self._stop.wait(self.poll)
                continue
            self.busy[name] = task.id
            try:
                self._run(name, task)
            finally:
                self.busy[name] = None

    def _run(self, name: str, task: Task) -> None:
        self._on_event("%s 领走 %s（%s，第 %d 轮）"
                       % (name, task.id, task.kind, task.attempt))
        last = [0.0]
        lost = [False]

        def progress(frac: float, msg: str) -> None:
            if lost[0]:
                return                                    # 已经知道没戏了，别再写
            now = time.monotonic()
            if now - last[0] < self.heartbeat and frac < 1.0:
                return
            last[0] = now
            try:
                owned = self.store.heartbeat(task.id, frac, msg,
                                             attempt=task.attempt)
            except Exception:
                return                                    # 心跳失败不能中断业务
            if not owned:
                # 任务被人工重试或回收器接管了。此刻起它的进度、结果、
                # 错误信息都不该由我写——否则会盖掉接管者的真实状态。
                lost[0] = True
                self._on_event("%s 失去 %s 的所有权（已被接管），结果将被丢弃"
                               % (name, task.id))

        try:
            result = jobs.run(task.kind, task.payload, progress)
        except Exception as e:
            # 全失败的任务带着「已经跑出来的明细」抛出。它是失败记录的一部分，
            # 不像普通异常那样只有一句话可写。
            partial = getattr(e, "result", None) if isinstance(
                e, jobs.JobFailure) else None
            reason = (str(e) if isinstance(e, jobs.JobFailure)
                      else "%s: %s" % (type(e).__name__, e))
            if lost[0]:
                self._on_event("%s 丢弃 %s 的失败记录：任务已被接管"
                               % (name, task.id))
                return
            try:
                final = self.store.fail(task.id, reason, attempt=task.attempt,
                                        result=partial)
            except Exception as e2:                       # 连状态都写不进去
                self._on_event("%s 失败且无法落库 %s：%s" % (name, task.id, e2))
                return
            if final is None:                             # 令牌过期，不是我的事
                self._on_event("%s 丢弃 %s 的失败记录：任务已被接管"
                               % (name, task.id))
            elif final == FAILED:
                self._on_event("%s 失败 %s：%s" % (name, task.id, reason[:120]))
            else:
                self._on_event("%s 失败 %s，已重新排队（重试 %d/%d）"
                               % (name, task.id, task.retries + 1,
                                  task.max_retries))
            return

        if lost[0]:
            # 业务层已经跑完了才走到这里——结果作废。要做到「立刻停手」得给
            # jobs.run 传一个中断信号，那是另一件事；先把错误的写入堵住。
            self._on_event("%s 丢弃 %s 的结果：任务已被接管" % (name, task.id))
            return
        try:
            owned = self.store.finish(task.id, result, attempt=task.attempt)
        except Exception as e:
            self._on_event("%s 收尾失败 %s：%s" % (name, task.id, e))
            return
        if not owned:
            self._on_event("%s 丢弃 %s 的结果：任务已被接管" % (name, task.id))
            return
        self._on_event("%s 完成 %s" % (name, task.id))

    # ── 僵尸回收 ────────────────────────────────────────────

    def _janitor_loop(self) -> None:
        interval = max(2.0, min(30.0, self.stale_after / 4))
        while not self._stop.wait(interval):
            try:
                n = self.store.reclaim_stale(self.stale_after)
            except Exception as e:
                self._on_event("回收器出错：%s" % e)
                continue
            if n:
                self._on_event("回收器放回 %d 个心跳超时的任务" % n)
