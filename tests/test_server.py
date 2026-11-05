# -*- coding: utf-8 -*-
"""服务层测试：状态机、并发抢任务、重试、僵尸回收、HTTP 路由。

这些测试不联网：只验证「任务调度」这套骨架本身。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from asr_polish.server import App, Store, make_server
from asr_polish.server.store import (DONE, FAILED, PENDING, RUNNING,
                                     StateError)


class _TempDB(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asrpolish_")
        self.db = os.path.join(self.tmp, "tasks.db")
        self.store = Store(self.db)

    def tearDown(self) -> None:
        self.store.close()
        time.sleep(0.05)                       # 让 WAL 落盘，Windows 才删得掉
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestStateMachine(_TempDB):
    def test_new_task_is_pending(self) -> None:
        t = self.store.create("subs", {"target": "BV1xx"})
        self.assertEqual(t.status, PENDING)
        self.assertEqual(t.progress, 0.0)
        self.assertEqual(t.payload["target"], "BV1xx")

    def test_happy_path(self) -> None:
        t = self.store.create("subs", {"target": "BV1xx"})
        got = self.store.claim("w1")
        self.assertIsNotNone(got)
        self.assertEqual(got.status, RUNNING)
        self.assertEqual(got.worker, "w1")
        self.store.finish(t.id, {"videos": 1})
        done = self.store.get(t.id)
        self.assertEqual(done.status, DONE)
        self.assertEqual(done.progress, 1.0)
        self.assertIsNotNone(done.finished_at)
        self.assertEqual(done.result["videos"], 1)

    def test_done_is_terminal(self) -> None:
        """重复 finish 必须报错，不能静默放过。"""
        t = self.store.create("subs", {})
        self.store.claim("w1")
        self.store.finish(t.id, {})
        with self.assertRaises(StateError):
            self.store.finish(t.id, {})

    def test_cannot_finish_pending_task(self) -> None:
        """没被领取的任务不能直接标完成（跳过 running 是非法迁移）。"""
        t = self.store.create("subs", {})
        with self.assertRaises(StateError):
            self.store.finish(t.id, {})


class TestClaim(_TempDB):
    def test_claim_is_exclusive(self) -> None:
        """多个线程并发抢同一个队列，不能有任何一个任务被领两次。"""
        ids = [self.store.create("subs", {"target": "BV%d" % i}).id
               for i in range(20)]
        claimed: list[str] = []
        lock = threading.Lock()

        def worker(name: str) -> None:
            while True:
                task = self.store.claim(name)
                if task is None:
                    return
                with lock:
                    claimed.append(task.id)
                self.store.finish(task.id, {})

        threads = [threading.Thread(target=worker, args=("w%d" % i,))
                   for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(claimed), len(ids))
        self.assertEqual(len(set(claimed)), len(ids), "有任务被重复领取")
        self.assertEqual(self.store.stats()[DONE], len(ids))

    def test_claim_empty_returns_none(self) -> None:
        self.assertIsNone(self.store.claim("w1"))


class TestRetry(_TempDB):
    def test_retry_then_failed(self) -> None:
        t = self.store.create("subs", {}, max_retries=2)
        self.assertEqual(self.store.claim("w1").id, t.id)
        self.assertEqual(self.store.fail(t.id, "网络错误"), PENDING)
        self.assertEqual(self.store.claim("w1").id, t.id)
        self.assertEqual(self.store.fail(t.id, "网络错误"), PENDING)
        self.assertEqual(self.store.claim("w1").id, t.id)
        self.assertEqual(self.store.fail(t.id, "网络错误"), FAILED)
        final = self.store.get(t.id)
        self.assertEqual(final.retries, 2)
        self.assertEqual(final.status, FAILED)

    def test_no_retry_budget(self) -> None:
        t = self.store.create("subs", {}, max_retries=0)
        self.store.claim("w1")
        self.assertEqual(self.store.fail(t.id, "挂了"), FAILED)

    def test_success_after_retry_clears_error(self) -> None:
        """重试后成功的任务不该保留上一次的报错——界面上会看起来像失败。"""
        t = self.store.create("subs", {}, max_retries=2)
        self.store.claim("w1")
        self.store.fail(t.id, "第一次风控")
        self.assertEqual(self.store.get(t.id).error, "第一次风控")
        self.store.claim("w1")
        self.store.finish(t.id, {"ok": True})
        final = self.store.get(t.id)
        self.assertEqual(final.status, DONE)
        self.assertIsNone(final.error)
        self.assertEqual(final.retries, 1)

    def test_requeue_failed(self) -> None:
        t = self.store.create("subs", {}, max_retries=0)
        self.store.claim("w1")
        self.store.fail(t.id, "挂了")
        self.assertTrue(self.store.requeue(t.id))
        self.assertEqual(self.store.get(t.id).status, PENDING)

    def test_requeue_pending_rejected(self) -> None:
        t = self.store.create("subs", {})
        self.assertFalse(self.store.requeue(t.id))

    def test_requeue_done_rejected(self) -> None:
        t = self.store.create("subs", {})
        self.store.claim("w1")
        self.store.finish(t.id, {})
        self.assertFalse(self.store.requeue(t.id))


class TestReclaim(_TempDB):
    def test_does_not_touch_live_task(self) -> None:
        t = self.store.create("subs", {})
        self.store.claim("w1")
        self.assertEqual(self.store.reclaim_stale(stale_after=9999), 0)
        self.assertEqual(self.store.get(t.id).status, RUNNING)

    def test_reclaims_zombie(self) -> None:
        """模拟 kill -9：任务停在 running，心跳不再更新。"""
        t = self.store.create("subs", {})
        self.store.claim("w1")
        with self.store._db() as conn:
            conn.execute("UPDATE tasks SET updated_at=? WHERE id=?",
                         (time.time() - 5000, t.id))
        self.assertEqual(self.store.reclaim_stale(stale_after=600), 1)
        after = self.store.get(t.id)
        self.assertEqual(after.status, PENDING)
        self.assertEqual(after.retries, 1)

    def test_heartbeat_keeps_task_alive(self) -> None:
        t = self.store.create("subs", {})
        self.store.claim("w1")
        with self.store._db() as conn:
            conn.execute("UPDATE tasks SET updated_at=? WHERE id=?",
                         (time.time() - 5000, t.id))
        self.store.heartbeat(t.id, 0.5, "还在跑")
        self.assertEqual(self.store.reclaim_stale(stale_after=600), 0)
        self.assertEqual(self.store.get(t.id).progress, 0.5)

    def test_reclaim_respects_retry_limit(self) -> None:
        """僵尸回收必须守住重试上限。

        否则「一跑就把 worker 搞死」的任务会无限回收、无限重跑，
        永远落不到 failed——用户看到的是「一直在排队」。
        """
        t = self.store.create("subs", {}, max_retries=2)
        for _ in range(5):
            claimed = self.store.claim("w1")
            if claimed is None:
                break
            with self.store._db() as conn:
                conn.execute("UPDATE tasks SET updated_at=? WHERE id=?",
                             (time.time() - 5000, t.id))
            self.store.reclaim_stale(stale_after=600)

        after = self.store.get(t.id)
        self.assertEqual(after.status, FAILED)
        self.assertEqual(after.retries, 2)              # 不再往上加
        self.assertIn("重试", after.error or "")

    def test_reclaim_with_zero_retry_budget_fails_at_once(self) -> None:
        t = self.store.create("subs", {}, max_retries=0)
        self.store.claim("w1")
        with self.store._db() as conn:
            conn.execute("UPDATE tasks SET updated_at=? WHERE id=?",
                         (time.time() - 5000, t.id))
        self.assertEqual(self.store.reclaim_stale(stale_after=600), 0)
        self.assertEqual(self.store.get(t.id).status, FAILED)


class TestOwnership(_TempDB):
    """所有权令牌（fencing token）：任务被接管后，旧执行的写入必须全部落空。

    没有这一层，「被判僵尸又复活的 worker」会和接管者同时写同一行——它甚至
    能把接管者还在跑的任务抢先标成完成，而接管者最后收尾时反而撞上「已处于
    终态 done」，日志里还会打出一句假的「已重新排队」。
    """

    def test_claim_bumps_attempt(self) -> None:
        t = self.store.create("subs", {"target": "BV1xx"})
        self.assertEqual(t.attempt, 0)              # 建任务不算持有
        first = self.store.claim("w1")
        self.assertEqual(first.attempt, 1)
        self.store.fail(t.id, "挂了")
        second = self.store.claim("w1")
        self.assertEqual(second.attempt, 2)         # 失败重排后再领是新的一轮

    def test_retry_running_is_restart_not_duplicate(self) -> None:
        t = self.store.create("subs", {"target": "BV1xx"})
        old = self.store.claim("w1")
        self.assertTrue(self.store.requeue(t.id))
        new = self.store.claim("w2")
        self.assertGreater(new.attempt, old.attempt)

        # 旧执行的每一次写回都被拒
        self.assertFalse(self.store.heartbeat(t.id, 0.99, "旧进度",
                                              attempt=old.attempt))
        self.assertFalse(self.store.finish(t.id, {"by": "w1"},
                                           attempt=old.attempt))
        self.assertIsNone(self.store.fail(t.id, "旧错误", attempt=old.attempt))

        task = self.store.get(t.id)
        self.assertEqual(task.status, RUNNING)      # 没被旧执行抢先标成完成
        self.assertIsNone(task.result)
        self.assertEqual(task.progress, 0.0)

        # 新执行一切正常
        self.assertTrue(self.store.finish(t.id, {"by": "w2"}, attempt=new.attempt))
        self.assertEqual(self.store.get(t.id).result, {"by": "w2"})

    def test_reclaim_invalidates_old_owner(self) -> None:
        """没人点重试也会撞车：回收器接管后，僵尸 worker 醒来照样想写。"""
        t = self.store.create("subs", {"target": "BV1xx"})
        old = self.store.claim("w1")
        with self.store._db() as conn:
            conn.execute("UPDATE tasks SET updated_at=? WHERE id=?",
                         (time.time() - 700, t.id))
            conn.commit()
        self.assertEqual(self.store.reclaim_stale(600), 1)
        new = self.store.claim("w2")
        self.assertGreater(new.attempt, old.attempt)
        self.assertFalse(self.store.finish(t.id, {"by": "w1"},
                                          attempt=old.attempt))
        self.assertEqual(self.store.get(t.id).status, RUNNING)
        self.assertTrue(self.store.finish(t.id, {"by": "w2"}, attempt=new.attempt))

    def test_exhausted_reclaim_also_bumps_attempt(self) -> None:
        t = self.store.create("subs", {}, max_retries=0)
        old = self.store.claim("w1")
        with self.store._db() as conn:
            conn.execute("UPDATE tasks SET updated_at=? WHERE id=?",
                         (time.time() - 5000, t.id))
        self.store.reclaim_stale(stale_after=600)
        task = self.store.get(t.id)
        self.assertEqual(task.status, FAILED)
        self.assertGreater(task.attempt, old.attempt)
        self.assertFalse(self.store.finish(t.id, {}, attempt=old.attempt))

    def test_manual_retry_of_failed_gets_fresh_token(self) -> None:
        t = self.store.create("subs", {}, max_retries=0)
        old = self.store.claim("w1")
        self.assertEqual(self.store.fail(t.id, "挂了", attempt=old.attempt), FAILED)
        self.assertTrue(self.store.requeue(t.id))
        new = self.store.claim("w2")
        self.assertGreater(new.attempt, old.attempt)
        # 旧令牌作废：不能把新执行的任务改回 failed
        self.assertIsNone(self.store.fail(t.id, "旧错误", attempt=old.attempt))
        self.assertEqual(self.store.get(t.id).status, RUNNING)

    def test_done_task_cannot_be_resurrected_by_stale_token(self) -> None:
        t = self.store.create("subs", {}, max_retries=0)
        old = self.store.claim("w1")
        self.store.fail(t.id, "挂了", attempt=old.attempt)
        self.store.requeue(t.id)
        new = self.store.claim("w2")
        self.assertTrue(self.store.finish(t.id, {"ok": 1}, attempt=new.attempt))
        self.assertFalse(self.store.finish(t.id, {"ok": 2}, attempt=old.attempt))
        self.assertEqual(self.store.get(t.id).result, {"ok": 1})

    def test_without_token_no_check(self) -> None:
        """不传 attempt 等于不校验，管理员路径与旧调用方不受影响。"""
        t = self.store.create("subs", {"target": "BV1xx"})
        self.store.claim("w1")
        self.assertTrue(self.store.finish(t.id, {}))
        self.assertEqual(self.store.get(t.id).status, DONE)


class TestMigration(_TempDB):
    def test_old_table_gets_attempt_column(self) -> None:
        """0.9.0 之前的库没有 attempt 列，打开时必须自动补上。"""
        import sqlite3
        path = os.path.join(self.tmp, "old.db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '', result TEXT, error TEXT,
                retries INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 2, worker TEXT,
                created_at REAL NOT NULL, started_at REAL,
                updated_at REAL NOT NULL, finished_at REAL);
            CREATE INDEX idx_tasks_status ON tasks(status, created_at);
        """)
        conn.execute("INSERT INTO tasks (id,kind,payload,status,created_at,"
                     "updated_at) VALUES ('Told','subs','{}','pending',1,1)")
        conn.commit()
        conn.close()

        store = Store(path)
        try:
            task = store.get("Told")
            self.assertIsNotNone(task)
            self.assertEqual(task.attempt, 0)       # 历史任务没持有过所有权
            claimed = store.claim("w1")
            self.assertEqual(claimed.id, "Told")
            self.assertEqual(claimed.attempt, 1)
        finally:
            store.close()

    def test_migration_is_idempotent(self) -> None:
        """反复打开同一个库不能重复 ALTER。"""
        path = os.path.join(self.tmp, "twice.db")
        Store(path).close()
        store = Store(path)
        try:
            self.assertEqual(store.create("subs", {}).attempt, 0)
        finally:
            store.close()


class TestWorkerOwnership(_TempDB):
    def _pool(self, events: list) -> "object":
        from asr_polish.server.worker import WorkerPool
        pool = WorkerPool(self.store, workers=1, on_event=events.append)
        pool.heartbeat = 0.0        # 关掉进度节流，让每次 progress 都真写库
        return pool

    def test_result_dropped_after_takeover(self) -> None:
        """worker 跑完才发现自己被接管：结果丢弃，不能盖掉新执行。"""
        from asr_polish.server import jobs
        events: list[str] = []
        pool = self._pool(events)
        t = self.store.create("subs", {"target": "BV1xx"})
        task = self.store.claim("worker-1")

        def fake_run(_kind, _payload, progress):
            progress(0.5, "跑一半")
            self.store.requeue(t.id)        # 用户在跑的过程中点了重试
            progress(0.8, "继续跑")          # 这次心跳会被拒
            return {"by": "old"}

        with mock.patch.object(jobs, "run", fake_run):
            pool._run("worker-1", task)

        after = self.store.get(t.id)
        self.assertEqual(after.status, PENDING)     # 没有被标成完成
        self.assertIsNone(after.result)
        self.assertTrue(any("丢弃" in e for e in events), events)

    def test_failure_dropped_after_takeover(self) -> None:
        """被接管后即使业务报错，也不该把接管者的任务记成失败。"""
        from asr_polish.server import jobs
        events: list[str] = []
        pool = self._pool(events)
        t = self.store.create("subs", {"target": "BV1xx"}, max_retries=0)
        task = self.store.claim("worker-1")

        def fake_run(_kind, _payload, progress):
            self.store.requeue(t.id)
            progress(0.1, "第一步")
            raise RuntimeError("抓取失败")

        with mock.patch.object(jobs, "run", fake_run):
            pool._run("worker-1", task)

        after = self.store.get(t.id)
        self.assertEqual(after.status, PENDING)     # 不是 FAILED
        self.assertIsNone(after.error)
        self.assertTrue(any("丢弃" in e for e in events), events)

    def test_normal_run_still_finishes(self) -> None:
        """没被接管时行为不变，别把正常路径改坏。"""
        from asr_polish.server import jobs
        events: list[str] = []
        pool = self._pool(events)
        t = self.store.create("subs", {"target": "BV1xx"})
        task = self.store.claim("worker-1")

        with mock.patch.object(jobs, "run", lambda *_: {"ok": 1}):
            pool._run("worker-1", task)

        after = self.store.get(t.id)
        self.assertEqual(after.status, DONE)
        self.assertEqual(after.result, {"ok": 1})
        self.assertTrue(any("完成" in e for e in events), events)

    def test_job_failure_keeps_details(self) -> None:
        """全失败仍是 failed，但明细要跟着落库——不能只剩一句话。"""
        from asr_polish.server import jobs
        events: list[str] = []
        pool = self._pool(events)
        t = self.store.create("subs", {"target": "BV1xx"}, max_retries=0)
        task = self.store.claim("worker-1")
        detail = {"outcome": "failed",
                  "failed_items": [{"retry": "BV1a", "error": "风控"}]}

        def fake_run(_kind, _payload, _progress):
            raise jobs.JobFailure("全部失败（1 处错误）：风控", detail)

        with mock.patch.object(jobs, "run", fake_run):
            pool._run("worker-1", task)

        after = self.store.get(t.id)
        self.assertEqual(after.status, FAILED)
        self.assertEqual(after.result["failed_items"][0]["retry"], "BV1a")
        # 错误文案不该带 "JobFailure:" 这种类名前缀，那是给意外异常用的
        self.assertEqual(after.error, "全部失败（1 处错误）：风控")
        self.assertTrue(any("失败" in e for e in events), events)


class TestFailureKeepsResult(_TempDB):
    """失败也要留下明细：否则面板只剩一行错误，没法只重跑失败的那几条。"""

    def test_failed_task_keeps_partial_result(self) -> None:
        t = self.store.create("subs", {}, max_retries=0)
        claimed = self.store.claim("w1")
        detail = {"outcome": "failed",
                  "failed_items": [{"retry": "BV1a"}, {"retry": "BV1b#3"}]}
        self.assertEqual(
            self.store.fail(t.id, "全部失败", attempt=claimed.attempt,
                            result=detail), FAILED)
        got = self.store.get(t.id)
        self.assertEqual(got.result["outcome"], "failed")
        self.assertEqual([x["retry"] for x in got.result["failed_items"]],
                         ["BV1a", "BV1b#3"])

    def test_requeue_branch_does_not_pin_stale_result(self) -> None:
        """回队列那一支不写 result：下一次执行会覆盖它，留着只会误导。"""
        t = self.store.create("subs", {}, max_retries=2)
        claimed = self.store.claim("w1")
        self.assertEqual(
            self.store.fail(t.id, "boom", attempt=claimed.attempt,
                            result={"outcome": "failed"}), PENDING)
        self.assertIsNone(self.store.get(t.id).result)


class TestJobs(unittest.TestCase):
    def test_parse_targets(self) -> None:
        from asr_polish.server.jobs import resolve_targets
        self.assertEqual(resolve_targets("BV1Mshg6REo4"), ["BV1Mshg6REo4"])
        self.assertEqual(
            resolve_targets("https://www.bilibili.com/video/BV1Mshg6REo4?p=2"),
            ["BV1Mshg6REo4"])

    def test_bad_target(self) -> None:
        from asr_polish.server.jobs import resolve_targets
        with self.assertRaises(ValueError):
            resolve_targets("这不是一个链接")

    def test_unknown_kind(self) -> None:
        from asr_polish.server import jobs
        with self.assertRaises(ValueError) as ctx:
            jobs.run("nope", {})
        self.assertIn("未知任务类型", str(ctx.exception))

    def test_formats_accept_list(self) -> None:
        """HTTP 里 formats 常写成数组，必须能接受，不能只认字符串。"""
        from asr_polish import export
        self.assertEqual(export.parse_formats(",".join(["txt", "srt"])),
                         ["txt", "srt"])


class TestHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="asrpolish_http_")
        cls.store = Store(os.path.join(cls.tmp, "t.db"))
        cls.pool = WarehouseFake(cls.store)
        cls.app = App(cls.store, cls.pool)
        cls.srv = make_server("127.0.0.1", 0, cls.app)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      kwargs={"poll_interval": 0.1},
                                      daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path: str, method: str = "GET", payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_health(self) -> None:
        code, body = self._get("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertIn("stats", body)
        # 面板靠这两个字段提示「Cookie 是否可用」「有没有在限速」
        self.assertIn("cookie", body)
        self.assertIn("level", body["cookie"])
        self.assertIn("note", body["cookie"])
        self.assertIn("throttle", body)
        self.assertIn("min_interval", body["throttle"])

    def test_create_and_get(self) -> None:
        code, body = self._get("/api/tasks", "POST",
                               {"kind": "subs", "target": "BV1test"})
        self.assertEqual(code, 202)
        task_id = body["id"]
        self.assertEqual(body["payload"]["target"], "BV1test")
        code, got = self._get("/api/tasks/" + task_id)
        self.assertEqual(code, 200)
        self.assertEqual(got["id"], task_id)

    def test_create_requires_target(self) -> None:
        code, body = self._get("/api/tasks", "POST", {"kind": "subs"})
        self.assertEqual(code, 400)
        self.assertIn("target", body["error"])

    def test_missing_task_404(self) -> None:
        code, body = self._get("/api/tasks/NOPE")
        self.assertEqual(code, 404)
        self.assertIn("error", body)

    def test_bad_json_400(self) -> None:
        req = urllib.request.Request(self.base + "/api/tasks", data=b"{oops",
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)

    def test_list_with_filter(self) -> None:
        self._get("/api/tasks", "POST", {"kind": "subs", "target": "BVlist"})
        code, body = self._get("/api/tasks?status=pending&limit=5")
        self.assertEqual(code, 200)
        self.assertLessEqual(len(body["tasks"]), 5)
        self.assertEqual(body["tasks"][0]["status"], PENDING)

    def test_index_page(self) -> None:
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            html = r.read().decode()
        self.assertIn("任务面板", html)
        # 面板上要有进阅读页的入口，否则抓完字幕还得自己拼 URL
        self.assertIn("/reader?task=", html)

    def test_retry_running_task(self) -> None:
        code, body = self._get("/api/tasks", "POST",
                               {"kind": "subs", "target": "BVretry"})
        task_id = body["id"]
        while True:                         # 队列是 FIFO，前面可能还有别的任务
            claimed = self.store.claim("w1")
            if claimed is None or claimed.id == task_id:
                break
        code, body = self._get("/api/tasks/%s/retry" % task_id, "POST")
        self.assertEqual(code, 202)
        self.assertEqual(body["status"], PENDING)

    def _finished(self, target: str, result: dict) -> str:
        """建一个任务、跑完、写入指定 result，返回任务 id。"""
        _, body = self._get("/api/tasks", "POST",
                            {"kind": "subs", "target": target, "limit": 20,
                             "outdir": "./subs"})
        task_id = body["id"]
        while True:
            claimed = self.store.claim("w1")
            if claimed is None or claimed.id == task_id:
                break
        self.store.finish(task_id, result)
        return task_id

    def test_retry_failed_creates_scoped_task(self) -> None:
        """只重跑失败的那几条：新建任务，带 only，其余参数沿用原任务。"""
        parent = self._finished("BVpartial", {
            "outcome": "partial", "failed": 2,
            "failed_items": [{"bvid": "BV1a", "retry": "BV1a"},
                             {"bvid": "BV1b", "page": 3,
                              "retry": "BV1b#3"}],
        })
        code, child = self._get("/api/tasks/%s/retry-failed" % parent, "POST")
        self.assertEqual(code, 202)
        self.assertEqual(child["payload"]["only"], ["BV1a", "BV1b#3"])
        self.assertEqual(child["payload"]["retry_of"], parent)
        self.assertEqual(child["payload"]["target"], "BVpartial")
        self.assertEqual(child["payload"]["limit"], 20)
        self.assertEqual(child["status"], PENDING)
        # 原任务不能被改动：它的 result 是「当时到底发生了什么」的记录
        self.assertEqual(self.store.get(parent).status, DONE)
        self.assertEqual(self.store.get(parent).result["outcome"], "partial")

    def test_retry_failed_without_failures_409(self) -> None:
        parent = self._finished("BVclean",
                                {"outcome": "complete", "failed_items": []})
        code, body = self._get("/api/tasks/%s/retry-failed" % parent, "POST")
        self.assertEqual(code, 409)
        self.assertIn("没有可重跑", body["error"])


class WarehouseFake:
    """给 HTTP 测试用的假 worker 池，只要 status() 够用。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    def status(self) -> list:
        return [{"worker": "fake", "task": None}]


if __name__ == "__main__":
    unittest.main()
