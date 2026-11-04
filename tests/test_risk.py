# -*- coding: utf-8 -*-
"""风控识别、退避重试、Cookie 状态。

不联网：HTTP 层用假 opener，时间用可替换的 sleep。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from asr_polish import bili
from asr_polish.bili import BiliError, BiliLoginRequired, BiliRiskControl
from asr_polish.server import jobs
from asr_polish.throttle import Throttle

API = "https://api.bilibili.com/x/web-interface/view?bvid=BV1xx"

# 真实的 wbi_img / sub_url 文件名都是 32 位十六进制；总长 64 才能被重排表索引。
_WBI_IMG = "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png"
_WBI_SUB = "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png"


class _Resp:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.headers: dict = {}

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _Opener:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def open(self, req, timeout=None):        # noqa: ANN001, ANN201
        return _Resp(self._payload)


# ── 业务码识别 ──────────────────────────────────────────────

class TestCheckCode(unittest.TestCase):
    def test_ok_codes_pass(self) -> None:
        self.assertIsNone(bili._check_code({"code": 0}, "取视频信息"))
        self.assertIsNone(bili._check_code({}, "取视频信息"))
        self.assertIsNone(bili._check_code({"code": None}, "取视频信息"))

    def test_risk_codes(self) -> None:
        for code in (-352, -412, -799, -509):
            with self.assertRaises(BiliRiskControl) as ctx:
                bili._check_code({"code": code, "message": "风控校验失败"},
                                 "取视频信息")
            self.assertEqual(ctx.exception.code, code)

    def test_login_codes(self) -> None:
        for code in (-101, -111):
            with self.assertRaises(BiliLoginRequired) as ctx:
                bili._check_code({"code": code, "message": "账号未登录"},
                                 "取 UP 主投稿")
            self.assertEqual(ctx.exception.code, code)

    def test_exception_hierarchy(self) -> None:
        # 上层可以「只捕 BiliRiskControl」把风控一网打尽，
        # 同时又能单独先捕 BiliLoginRequired 走另一条提示。
        self.assertTrue(issubclass(BiliLoginRequired, BiliRiskControl))
        self.assertTrue(issubclass(BiliRiskControl, BiliError))

    def test_other_codes_are_plain_errors(self) -> None:
        with self.assertRaises(BiliError) as ctx:
            bili._check_code({"code": -404, "message": "稿件不存在"}, "取视频信息")
        self.assertNotIsInstance(ctx.exception, BiliRiskControl)
        self.assertIn("稿件不存在", str(ctx.exception))


# ── HTTP 层：412 / 429 / HTML 验证页 ────────────────────────

class TestHttpRiskDetection(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = bili._throttle
        bili._throttle = Throttle(min_interval=0.0)

    def tearDown(self) -> None:
        bili._throttle = self._saved

    def _call(self, opener) -> dict:               # noqa: ANN001
        with mock.patch.object(bili, "_opener", lambda *a, **k: opener):
            return bili._http_json(API)

    def test_normal_json(self) -> None:
        got = self._call(_Opener(b'{"code":0,"data":{"ok":1}}'))
        self.assertEqual(got["data"]["ok"], 1)

    def test_http_412_is_risk(self) -> None:
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            API, 412, "Precondition Failed", {}, None)
        with self.assertRaises(BiliRiskControl):
            self._call(opener)

    def test_http_429_is_risk(self) -> None:
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            API, 429, "Too Many Requests", {}, None)
        with self.assertRaises(BiliRiskControl):
            self._call(opener)

    def test_http_403_is_login(self) -> None:
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            API, 403, "Forbidden", {}, None)
        with self.assertRaises(BiliLoginRequired):
            self._call(opener)

    def test_http_500_is_plain_error(self) -> None:
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            API, 500, "Internal Server Error", {}, None)
        with self.assertRaises(BiliError) as ctx:
            self._call(opener)
        self.assertNotIsInstance(ctx.exception, BiliRiskControl)

    def test_html_body_is_risk(self) -> None:
        """正常接口不返回 HTML；出现 HTML 基本就是被导到人机验证页了。"""
        with self.assertRaises(BiliRiskControl):
            self._call(_Opener(b"<!DOCTYPE html><html>verify</html>"))

    def test_html_with_leading_space_is_risk(self) -> None:
        with self.assertRaises(BiliRiskControl):
            self._call(_Opener(b"\n  <html></html>"))

    def test_broken_json_is_plain_error(self) -> None:
        with self.assertRaises(BiliError) as ctx:
            self._call(_Opener(b"not json at all"))
        self.assertNotIsInstance(ctx.exception, BiliRiskControl)


# ── 接口层确实接上了识别 ────────────────────────────────────

class TestApiUsesCheckCode(unittest.TestCase):
    def setUp(self) -> None:
        bili._wbi_cache.clear()

    def test_get_video_info_raises_on_risk(self) -> None:
        with mock.patch.object(bili, "_http_json",
                               return_value={"code": -352,
                                             "message": "风控校验失败"}):
            with self.assertRaises(BiliRiskControl):
                bili.get_video_info("BV1xx")

    def test_get_video_info_missing_data_is_plain_error(self) -> None:
        with mock.patch.object(bili, "_http_json",
                               return_value={"code": 0, "data": None,
                                             "message": "啥都没有"}):
            with self.assertRaises(BiliError) as ctx:
                bili.get_video_info("BV1xx")
        self.assertNotIsInstance(ctx.exception, BiliRiskControl)

    def test_list_up_videos_raises_on_risk(self) -> None:
        def fake(url, cookie_path=None, timeout=60):    # noqa: ANN001
            if "nav" in url:
                return {"code": 0, "data": {"wbi_img": {
                    "img_url": _WBI_IMG, "sub_url": _WBI_SUB}}}
            return {"code": -352, "message": "风控校验失败"}

        with mock.patch.object(bili, "_http_json", side_effect=fake):
            with self.assertRaises(BiliRiskControl):
                bili.list_up_videos(329954990)

    def test_subtitle_no_longer_refetches_nav(self) -> None:
        """取字幕不该每条视频都重打一次 nav（原来那样等于白发请求）。"""
        calls: list[str] = []

        def fake(url, cookie_path=None, timeout=60):    # noqa: ANN001
            calls.append(url)
            if "nav" in url:
                return {"code": 0, "data": {"wbi_img": {
                    "img_url": _WBI_IMG, "sub_url": _WBI_SUB}}}
            if "player/wbi/v2" in url:
                return {"code": 0, "data": {"subtitle": {"subtitles": []}}}
            return {"code": 0, "body": []}

        video = {"bvid": "BV1xx", "aid": 1, "cid": 2}
        with mock.patch.object(bili, "_http_json", side_effect=fake):
            bili._fetch_subtitle_detail(video)
            bili._fetch_subtitle_detail(video)
        self.assertEqual(sum(1 for c in calls if "nav" in c), 1)


# ── 退避重试 ────────────────────────────────────────────────

class TestRiskBackoff(unittest.TestCase):
    def setUp(self) -> None:
        self.slept: list[float] = []
        self.notes: list[tuple[float, str]] = []
        self._patch = mock.patch.object(jobs, "_sleep", self.slept.append)
        self._patch.start()
        # 退避逻辑本身不该顺带联网探登录态——那是 explain_risk 的职责，
        # 单独在 TestExplainRisk 里测。
        self._no_explain = mock.patch.object(jobs, "explain_risk", lambda e: e)
        self._no_explain.start()

    def tearDown(self) -> None:
        self._no_explain.stop()
        self._patch.stop()

    def _progress(self, frac: float, msg: str) -> None:
        self.notes.append((frac, msg))

    def test_success_does_not_sleep(self) -> None:
        got = jobs._with_risk_backoff(lambda: 42, self._progress, 0.5, "取视频信息")
        self.assertEqual(got, 42)
        self.assertEqual(self.slept, [])

    def test_one_risk_then_success(self) -> None:
        calls = {"n": 0}

        def fn() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise BiliRiskControl("风控", code=-352)
            return "ok"

        got = jobs._with_risk_backoff(fn, self._progress, 0.5, "取视频信息")
        self.assertEqual(got, "ok")
        self.assertAlmostEqual(sum(self.slept), 60.0)

    def test_backoff_grows(self) -> None:
        """三段退避应当是 60 → 180 → 300，不是固定间隔。"""
        calls = {"n": 0}

        def fn() -> str:
            calls["n"] += 1
            if calls["n"] <= 3:
                raise BiliRiskControl("风控", code=-412)
            return "ok"

        jobs._with_risk_backoff(fn, self._progress, 0.5, "取视频信息")

        # 每轮退避都被切成 15 秒一片，按片数还原每轮总等待
        per_round = []
        idx = 0
        for total in (60.0, 180.0, 300.0):
            slices = int(round(total / jobs._BACKOFF_TICK))
            per_round.append(round(sum(self.slept[idx:idx + slices])))
            idx += slices
        self.assertEqual(per_round, [60, 180, 300])
        self.assertEqual(idx, len(self.slept))

    def test_gives_up_after_all_backoffs(self) -> None:
        def fn() -> str:
            raise BiliRiskControl("风控", code=-799)

        with self.assertRaises(RuntimeError) as ctx:
            jobs._with_risk_backoff(fn, self._progress, 0.5, "取视频信息")
        self.assertAlmostEqual(sum(self.slept), 540.0)     # 60 + 180 + 300
        message = str(ctx.exception)
        self.assertIn("连续 4 次", message)
        self.assertIn("540", message)
        self.assertIn("--min-interval", message)

    def test_login_error_is_not_retried(self) -> None:
        calls = {"n": 0}

        def fn() -> str:
            calls["n"] += 1
            raise BiliLoginRequired("需要登录", code=-101)

        with self.assertRaises(BiliLoginRequired):
            jobs._with_risk_backoff(fn, self._progress, 0.5, "取 UP 主投稿")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.slept, [])

    def test_heartbeat_during_backoff(self) -> None:
        """退避期间必须持续汇报——否则回收器会把任务判成僵尸。"""
        calls = {"n": 0}

        def fn() -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise BiliRiskControl("风控", code=-352)
            return "ok"

        jobs._with_risk_backoff(fn, self._progress, 0.5, "取视频信息")
        backoff_notes = [n for n in self.notes if "退避" in n[1]]
        self.assertEqual(len(backoff_notes), 4)            # 60s 切成 4 片
        self.assertEqual(len(self.slept), 4)
        self.assertAlmostEqual(sum(self.slept), 60.0)
        self.assertTrue(all(frac == 0.5 for frac, _ in self.notes))

    def test_no_single_sleep_exceeds_tick(self) -> None:
        def fn() -> str:
            raise BiliRiskControl("风控", code=-509)

        with self.assertRaises(RuntimeError):
            jobs._with_risk_backoff(fn, self._progress, 0.5, "取视频信息")
        self.assertTrue(all(s <= jobs._BACKOFF_TICK for s in self.slept))


# ── 风控原因细分 ────────────────────────────────────────────

class TestExplainRisk(unittest.TestCase):
    """-352 既可能是「太快」也可能是「没登录」，两者处置相反。"""

    def test_login_required_passes_through(self) -> None:
        exc = BiliLoginRequired("需要登录", code=-101)
        self.assertIs(bili.explain_risk(exc), exc)

    def test_non_352_passes_through(self) -> None:
        for code in (-799, -509):
            exc = BiliRiskControl("风控", code=code)
            self.assertIs(bili.explain_risk(exc), exc)

    def test_352_with_logged_out_becomes_login_required(self) -> None:
        with mock.patch.object(bili, "verify_login", return_value=False):
            got = bili.explain_risk(BiliRiskControl("风控", code=-352))
        self.assertIsInstance(got, BiliLoginRequired)
        # 说明「是什么」；「怎么办」留给上层（CLI / 任务执行器）去讲
        self.assertIn("未登录", str(got))
        self.assertIn("退避无效", str(got))

    def test_352_with_login_ok_stays_risk(self) -> None:
        with mock.patch.object(bili, "verify_login", return_value=True):
            got = bili.explain_risk(BiliRiskControl("风控", code=-352))
        self.assertIsInstance(got, BiliRiskControl)
        self.assertNotIsInstance(got, BiliLoginRequired)

    def test_unknown_probe_keeps_risk(self) -> None:
        """探测不出结论时保守当风控——宁可多等，也不误判用户 Cookie 失效。"""
        with mock.patch.object(bili, "verify_login", return_value=None):
            got = bili.explain_risk(BiliRiskControl("风控", code=-412))
        self.assertNotIsInstance(got, BiliLoginRequired)


# ── Cookie 状态 ─────────────────────────────────────────────

_GOOD_COOKIE = (
    "# Netscape HTTP Cookie File\n"
    ".bilibili.com\tTRUE\t/\tTRUE\t1999999999\tSESSDATA\tabc123\n"
)
_OTHER_COOKIE = (
    "# Netscape HTTP Cookie File\n"
    ".bilibili.com\tTRUE\t/\tTRUE\t1999999999\tbuvid3\ttrack-only\n"
)


class TestCookieStatus(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asrpolish_cookie_")
        self.path = os.path.join(self.tmp, "cookies.txt")
        self._p = mock.patch.object(bili, "_DEFAULT_COOKIE_PATHS", ())
        self._p.start()
        bili._reset_login_cache()

    def tearDown(self) -> None:
        self._p.stop()
        bili._reset_login_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, text: str) -> str:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)
        return self.path

    def test_missing_file(self) -> None:
        st = bili.cookie_status(os.path.join(self.tmp, "nope.txt"))
        self.assertEqual(st["level"], "error")
        self.assertFalse(st["found"])
        self.assertIsNone(st["path"])
        self.assertIn("UP 主", st["note"])

    def test_valid_cookie(self) -> None:
        st = bili.cookie_status(self._write(_GOOD_COOKIE))
        self.assertTrue(st["found"])
        self.assertTrue(st["parsable"])
        self.assertTrue(st["has_sessdata"])
        self.assertEqual(st["level"], "ok")

    def test_cookie_without_sessdata(self) -> None:
        st = bili.cookie_status(self._write(_OTHER_COOKIE))
        self.assertFalse(st["has_sessdata"])
        self.assertEqual(st["level"], "error")
        self.assertIn("SESSDATA", st["note"])

    def test_broken_file(self) -> None:
        st = bili.cookie_status(self._write("this is not a cookie file\n"))
        self.assertFalse(st["parsable"])
        self.assertEqual(st["level"], "error")

    def test_stale_cookie_is_only_a_warning(self) -> None:
        path = self._write(_GOOD_COOKIE)
        os.utime(path, (1, 1))                 # 文件时间设成很久以前
        st = bili.cookie_status(path)
        self.assertEqual(st["level"], "warn")
        self.assertGreater(st["age_days"], 30)

    def test_verify_detects_dead_sessdata(self) -> None:
        path = self._write(_GOOD_COOKIE)

        def fake(url, cookie_path=None, timeout=60):    # noqa: ANN001
            return {"code": 0, "data": {"isLogin": False}}

        with mock.patch.object(bili, "_http_json", side_effect=fake):
            st = bili.cookie_status(path, verify=True)
        self.assertIs(st["logged_in"], False)
        self.assertEqual(st["level"], "error")
        self.assertIn("失效", st["note"])

    def test_verify_confirms_login(self) -> None:
        path = self._write(_GOOD_COOKIE)

        def fake(url, cookie_path=None, timeout=60):    # noqa: ANN001
            return {"code": 0, "data": {"isLogin": True}}

        with mock.patch.object(bili, "_http_json", side_effect=fake):
            st = bili.cookie_status(path, verify=True)
        self.assertIs(st["logged_in"], True)
        self.assertEqual(st["level"], "ok")

    def test_verify_failure_is_unknown_not_dead(self) -> None:
        """探测本身被风控时不能把用户的 Cookie 判死。"""
        path = self._write(_GOOD_COOKIE)

        def fake(url, cookie_path=None, timeout=60):    # noqa: ANN001
            raise BiliRiskControl("风控", code=-352)

        with mock.patch.object(bili, "_http_json", side_effect=fake):
            st = bili.cookie_status(path, verify=True)
        self.assertIsNone(st["logged_in"])
        self.assertEqual(st["level"], "ok")

    def test_nav_101_means_logged_out(self) -> None:
        """SESSDATA 时间上没过期、服务端已注销——只有 nav 能发现。"""
        with mock.patch.object(bili, "_http_json",
                               return_value={"code": -101, "data": {}}):
            self.assertIs(bili.verify_login(None), False)

    def test_login_probe_is_cached(self) -> None:
        self._write(_GOOD_COOKIE)
        hits = {"n": 0}

        def fake(url, cookie_path=None, timeout=60):    # noqa: ANN001
            hits["n"] += 1
            return {"code": 0, "data": {"isLogin": True}}

        with mock.patch.object(bili, "_http_json", side_effect=fake):
            bili.verify_login(self.path)
            bili.verify_login(self.path)
            bili.verify_login(self.path)
        self.assertEqual(hits["n"], 1)

    def test_resolve_prefers_explicit_path(self) -> None:
        explicit = self._write(_GOOD_COOKIE)
        self.assertEqual(str(bili._resolve_cookie_path(explicit)), explicit)

    def test_resolve_falls_back_to_default_paths(self) -> None:
        self._write(_GOOD_COOKIE)
        with mock.patch.object(bili, "_DEFAULT_COOKIE_PATHS",
                               (Path(self.path),)):
            got = bili._resolve_cookie_path(None)
        self.assertEqual(str(got), self.path)


if __name__ == "__main__":
    unittest.main()
