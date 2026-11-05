# -*- coding: utf-8 -*-
"""阅读页与只读正文接口。

分三层测：srt 解析（纯函数）、从 result 组装（碰磁盘）、HTTP 路由。
不联网——正文接口只读已经落盘的文件，这些测试正好走的是那条路径。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from asr_polish.server import App, Store, make_server
from asr_polish.server import transcript as T

SRT = (
    "1\n"
    "00:00:01,000 --> 00:00:03,500\n"
    "继承呢除了有单继承\n"
    "还有多继承\n"
    "\n"
    "2\n"
    "00:00:03,500 --> 00:00:05,200\n"
    "通过继承我们知道现有的类\n"
)


def _opener():
    """直连 127.0.0.1，不走系统代理。

    这台机器上 `http_proxy` 是设着的，走代理会让「服务没起来」变成 502，
    排查时容易误以为代码有问题。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class TestSrtParsing(unittest.TestCase):
    def test_basic(self) -> None:
        segs = T.parse_srt(SRT)
        self.assertEqual(len(segs), 2)
        self.assertAlmostEqual(segs[0]["start"], 1.0)
        self.assertAlmostEqual(segs[0]["end"], 3.5)
        self.assertEqual(segs[0]["text"], "继承呢除了有单继承\n还有多继承")

    def test_crlf_and_bom(self) -> None:
        """Windows 上写文件会把 \\n 变成 \\r\\n；BOM 是记事本留的。"""
        raw = "\ufeff" + SRT.replace("\n", "\r\n")
        self.assertEqual(len(T.parse_srt(raw)), 2)

    def test_doubled_carriage_return(self) -> None:
        """`\\r\\r\\n` 会出现两个换行，把「序号/时间轴/正文」拆进三个空行块。

        按空行切块的写法在这里会解析出 0 段——整篇白页。所以解析器按 `-->`
        扫行。这条用例是为了锁住那个决定。
        """
        raw = SRT.replace("\n", "\r\r\n")
        segs = T.parse_srt(raw)
        self.assertEqual(len(segs), 2)
        self.assertIn("多继承", segs[0]["text"])

    def test_no_index_line(self) -> None:
        segs = T.parse_srt("00:00:01,000 --> 00:00:02,000\n只有正文\n")
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["text"], "只有正文")

    def test_bad_block_is_skipped_not_fatal(self) -> None:
        raw = ("1\n00:00:01,000 --> 00:00:02,000\n好的一块\n\n"
               "2\n00:0x --> 坏\n这块没有有效时间轴\n\n"
               "3\n00:00:05,000 --> 00:00:06,000\n后面这块要还在\n")
        segs = T.parse_srt(raw)
        self.assertEqual([s["text"] for s in segs], ["好的一块", "后面这块要还在"])

    def test_empty_body_skipped(self) -> None:
        raw = "1\n00:00:01,000 --> 00:00:02,000\n\n2\n00:00:03,000 --> 00:00:04,000\n有字\n"
        self.assertEqual([s["text"] for s in T.parse_srt(raw)], ["有字"])

    def test_trailing_position_flags(self) -> None:
        raw = "1\n00:00:01,000 --> 00:00:02,500  align:start position:0%\n正文\n"
        seg = T.parse_srt(raw)[0]
        self.assertAlmostEqual(seg["end"], 2.5)

    def test_dot_as_decimal_separator(self) -> None:
        self.assertAlmostEqual(T.parse_srt("00:00:01.500 --> 00:00:02.000\nx\n")[0]["start"], 1.5)

    def test_placeholder_block_is_split_by_line(self) -> None:
        """必剪 ASR 那条路没有时间戳，导出 srt 会写成 0 → 99:59:59 一整块。"""
        raw = "1\n00:00:00,000 --> 99:59:59,000\n第一行\n第二行\n第三行\n"
        segs = T._split_placeholder(T.parse_srt(raw))
        self.assertEqual([s["text"] for s in segs], ["第一行", "第二行", "第三行"])
        self.assertTrue(all(s["start"] is None for s in segs))

    def test_real_block_with_long_end_is_not_split(self) -> None:
        """结束了但正文只有一行，不该被当成占位块拆开。"""
        raw = "1\n00:00:00,000 --> 99:59:59,000\n只有一行\n"
        segs = T._split_placeholder(T.parse_srt(raw))
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["start"], 0.0)


class TestBuild(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asrpolish_tr_")
        self.out = os.path.join(self.tmp, "subs")
        os.makedirs(self.out)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, text: str) -> str:
        path = os.path.join(self.out, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def _result(self, files, **extra):
        base = {"outcome": "complete", "outdir": self.out,
                "files": list(files), "parts_written": 1,
                "no_subtitle": 0, "failed": 0}
        base.update(extra)
        return base

    def test_prefers_srt_over_txt_for_same_stem(self) -> None:
        srt = self._write("BV1aa-标题.srt", SRT)
        txt = self._write("BV1aa-标题.txt", "重复的纯文本")
        out = T.build("T1", self._result([txt, srt]))
        self.assertEqual(len(out["parts"]), 1)
        self.assertEqual(out["parts"][0]["source"], "srt")
        self.assertEqual(len(out["parts"][0]["segments"]), 2)

    def test_json_fallback(self) -> None:
        payload = {"segments": [{"start": 0.5, "end": 1.5, "text": "从 json 来"}]}
        path = self._write("BV1bb.json", json.dumps(payload, ensure_ascii=False))
        out = T.build("T1", self._result([path]))
        self.assertEqual(out["parts"][0]["source"], "json")
        self.assertEqual(out["parts"][0]["segments"][0]["text"], "从 json 来")

    def test_txt_fallback_has_no_timestamps(self) -> None:
        path = self._write("BV1cc.txt", "第一行\n第二行\n")
        out = T.build("T1", self._result([path]))
        segs = out["parts"][0]["segments"]
        self.assertEqual(len(segs), 2)
        self.assertIsNone(segs[0]["start"])

    def test_file_outside_outdir_is_refused(self) -> None:
        """`files` 是库里的数据。照着它读磁盘、把内容发给浏览器之前，
        必须校验它确实在当前任务的输出目录里，否则就是任意文件读取。"""
        secret = os.path.join(self.tmp, "secret.txt")
        with open(secret, "w", encoding="utf-8") as fh:
            fh.write("不该被读到")
        out = T.build("T1", self._result([secret]))
        self.assertEqual(out["parts"], [])
        self.assertTrue(any("输出目录" in s["reason"] for s in out["skipped"]))

    def test_missing_file_is_reported_not_crashed(self) -> None:
        out = T.build("T1", self._result([os.path.join(self.out, "gone.srt")]))
        self.assertEqual(out["parts"], [])
        self.assertTrue(out["skipped"])

    def test_multipart_sorted_naturally(self) -> None:
        files = [self._write("BV1aa-P10-后.srt", SRT),
                 self._write("BV1aa-P2-前.srt", SRT)]
        out = T.build("T1", self._result(files))
        self.assertEqual([p["name"] for p in out["parts"]],
                         ["BV1aa-P2-前.srt", "BV1aa-P10-后.srt"])

    def test_hint_when_only_no_subtitle(self) -> None:
        out = T.build("T1", {"outcome": "empty", "outdir": self.out,
                             "files": [], "no_subtitle": 3, "failed": 0})
        self.assertIn("没有字幕轨", out["hint"])

    def test_hint_when_files_vanished(self) -> None:
        out = T.build("T1", {"outcome": "complete", "outdir": self.out,
                             "files": [], "parts_written": 2})
        self.assertIn("不在原处", out["hint"])

    def test_hint_when_no_result(self) -> None:
        self.assertTrue(T.build("T1", None)["hint"])


class _ServerBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="asrpolish_reader_")
        cls.out = os.path.join(cls.tmp, "subs")
        os.makedirs(cls.out)
        cls.store = Store(os.path.join(cls.tmp, "t.db"))
        cls.app = App(cls.store, None)
        cls.srv = make_server("127.0.0.1", 0, cls.app)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      kwargs={"poll_interval": 0.1}, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _get(self, path: str):
        try:
            with _opener().open(self.base + path, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def _drain(self, want: str):
        """把队列里排在 want 前面的任务领掉。"""
        while True:
            got = self.store.claim("w-test")
            if got is None or got.id == want:
                return got

    def _done(self, result: dict) -> str:
        task = self.store.create("subs", {"target": "BV1test"})
        claimed = self._drain(task.id)
        self.assertIsNotNone(claimed)
        self.store.finish(task.id, result, attempt=claimed.attempt)
        return task.id


class TestTranscriptEndpoint(_ServerBase):
    def test_reader_page(self) -> None:
        code, html = self._get("/reader")
        self.assertEqual(code, 200)
        self.assertIn('id="target"', html)
        self.assertIn("字幕阅读", html)
        # 页面本身仍然零外链：样式和脚本都内联，断网打开也是完整的。
        # 唯一的外部地址是 B站官方播放器——而且只在用户点了句子之后才加载
        # （iframe 初始没有 src），所以离线时它安静地不出现，不会拖白页。
        self.assertNotIn("<link", html)
        self.assertNotIn("<script src", html)
        urls = set(re.findall(r"https?://([^/\s\"'<>)]+)", html))
        self.assertEqual(urls, {"player.bilibili.com", "www.bilibili.com"})

    def test_unknown_task_404(self) -> None:
        code, body = self._get("/api/tasks/NOPE/transcript")
        self.assertEqual(code, 404)
        self.assertIn("error", json.loads(body))

    def test_pending_task_409(self) -> None:
        task = self.store.create("subs", {"target": "BV1pending"})
        code, body = self._get("/api/tasks/%s/transcript" % task.id)
        self.assertEqual(code, 409)
        data = json.loads(body)
        self.assertEqual(data["status"], "pending")
        self.assertIn("还没跑完", data["error"])

    def test_done_task_returns_segments(self) -> None:
        path = os.path.join(self.out, "BV1t1-标题.srt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(SRT)
        tid = self._done({"outcome": "complete", "outdir": self.out,
                          "files": [path], "parts_written": 1,
                          "no_subtitle": 0, "failed": 0})
        code, body = self._get("/api/tasks/%s/transcript" % tid)
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data["task"], tid)
        self.assertEqual(len(data["parts"]), 1)
        self.assertEqual(len(data["parts"][0]["segments"]), 2)
        self.assertAlmostEqual(data["parts"][0]["segments"][0]["start"], 1.0)

    def test_failed_task_still_readable(self) -> None:
        """全失败的任务没有正文，但接口要给出「为什么没有」，而不是 500。"""
        task = self.store.create("subs", {"target": "BV1bad"}, max_retries=0)
        claimed = self._drain(task.id)
        self.store.fail(task.id, "全部失败（1 处错误）：风控",
                        attempt=claimed.attempt,
                        result={"outcome": "failed", "outdir": self.out,
                                "files": [], "no_subtitle": 0, "failed": 1})
        code, body = self._get("/api/tasks/%s/transcript" % task.id)
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["parts"], [])
        self.assertTrue(data["hint"])

    def test_existing_panel_still_works(self) -> None:
        """加路由别把原来的面板弄坏。"""
        code, html = self._get("/")
        self.assertEqual(code, 200)
        self.assertIn("任务面板", html)


if __name__ == "__main__":
    unittest.main()
