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


class WarehouseFake:
    """给 HTTP 测试用的假 worker 池，只要 status() 够用。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    def status(self) -> list:
        return [{"worker": "fake", "task": None}]


if __name__ == "__main__":
    unittest.main()
