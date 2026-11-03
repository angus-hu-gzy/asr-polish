# -*- coding: utf-8 -*-
"""服务化：把抓字幕变成一个带任务队列的 HTTP 服务。

```
asr-polish serve --port 8040
curl -X POST localhost:8040/api/tasks \
     -H 'Content-Type: application/json' \
     -d '{"kind":"subs","target":"BV1xxxxxxxxx"}'
```

三个部件：`store`（SQLite 任务表 + 状态机）、`worker`（干活与僵尸回收）、
`app`（HTTP 接口）。全部零依赖。
"""
from __future__ import annotations

import sys


from .app import App, Handler, make_server
from .store import (DONE, FAILED, PENDING, RUNNING, StateError, Store, Task)
from .worker import WorkerPool

__all__ = [
    "App", "Handler", "Store", "Task", "StateError", "WorkerPool",
    "PENDING", "RUNNING", "DONE", "FAILED", "serve",
]


def serve(host: str = "127.0.0.1", port: int = 8040, db: str = "tasks.db",
          workers: int = 2, stale_after: float = 600.0,
          quiet: bool = False) -> None:
    """启动 HTTP 服务 + worker 池，阻塞直到 Ctrl-C。"""
    def note(msg: str) -> None:
        if not quiet:
            print("[serve] " + msg, file=sys.stderr, flush=True)

    store = Store(db)
    pool = WorkerPool(store, workers=workers, stale_after=stale_after,
                      on_event=note)
    app = App(store, pool)
    srv = make_server(host, port, app)

    stats = store.stats()
    note("任务库 %s（已有 %d 个任务：排队 %d / 进行中 %d）"
         % (db, stats["total"], stats[PENDING], stats[RUNNING]))
    pool.start()
    note("监听 http://%s:%d  面板直接打开这个地址" % (host, port))
    try:
        srv.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        note("收到中断，正在停止")
    finally:
        srv.server_close()
        pool.stop()
