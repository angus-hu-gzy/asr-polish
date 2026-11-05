# -*- coding: utf-8 -*-
"""风控退避：服务端说等多久，就等多久。

原来 ``BiliRiskControl`` 有个 ``retry_after`` 字段，赋过值、从没被读过——
退避永远按硬编码的 60/180/300 走。对方说「30 秒后可以再来」，我们等 60；
对方说「一小时」，我们 300 秒后继续撞，三次撞完报一个「继续重试只会延长
限制」。这两件事都不算错，但都不是最优。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
from unittest import mock

from asr_polish import bili
from asr_polish.bili import BiliRiskControl
from asr_polish.server import jobs


def _headers(**kw) -> Message:
    m = Message()
    for k, v in kw.items():
        m[k.replace("_", "-")] = v
    return m


class ParseRetryAfterTest(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(bili.parse_retry_after(_headers(Retry_After="120")), 120.0)

    def test_seconds_with_spaces_and_float(self):
        self.assertEqual(bili.parse_retry_after(_headers(Retry_After=" 7.5 ")), 7.5)

    def test_http_date_in_the_future(self):
        when = datetime.now(timezone.utc) + timedelta(seconds=90)
        got = bili.parse_retry_after(_headers(Retry_After=format_datetime(when, usegmt=True)))
        self.assertIsNotNone(got)
        self.assertGreater(got, 80)
        self.assertLessEqual(got, 91)

    def test_http_date_already_passed_is_zero(self):
        when = datetime.now(timezone.utc) - timedelta(hours=1)
        self.assertEqual(
            bili.parse_retry_after(_headers(Retry_After=format_datetime(when, usegmt=True))), 0.0)

    def test_garbage_is_none_not_an_exception(self):
        """读不懂就当没给——这是加速信息，不能因为它把退避逻辑带崩。"""
        self.assertIsNone(bili.parse_retry_after(_headers(Retry_After="soon-ish")))
        self.assertIsNone(bili.parse_retry_after(_headers(Retry_After="")))

    def test_missing_header_and_none(self):
        self.assertIsNone(bili.parse_retry_after(_headers(Other="1")))
        self.assertIsNone(bili.parse_retry_after(None))

    def test_lowercase_dict_key(self):
        """普通 dict 的键是大小写敏感的，代理层改写大小写很常见。"""
        self.assertEqual(bili.parse_retry_after({"retry-after": "30"}), 30.0)

    def test_negative_is_clamped(self):
        self.assertEqual(bili.parse_retry_after(_headers(Retry_After="-5")), 0.0)


class BackoffChoiceTest(unittest.TestCase):
    def test_falls_back_to_fixed_ladder(self):
        self.assertEqual([jobs._backoff_for(i) for i in range(3)], [60.0, 180.0, 300.0])

    def test_server_hint_wins(self):
        self.assertEqual(jobs._backoff_for(0, 45.0), 45.0)

    def test_hint_cannot_go_below_floor(self):
        """对方给 0 秒等于「立刻再来」——刚被拦完马上打，判定只会更重。"""
        self.assertEqual(jobs._backoff_for(0, 0.0), jobs._MIN_BACKOFF)

    def test_hint_above_ceiling_means_give_up(self):
        """对方说一小时后再来，重试三次只是把失败往后推。"""
        self.assertIsNone(jobs._backoff_for(0, 3600.0))
        self.assertEqual(jobs._backoff_for(0, jobs._MAX_BACKOFF), jobs._MAX_BACKOFF)


class BackoffLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.slept: list[float] = []
        p = mock.patch.object(jobs, "_sleep", self.slept.append)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, exc_factory, times=1):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] <= times:
                raise exc_factory()
            return "done"

        return jobs._with_risk_backoff(fn, lambda *a: None, 0.0, "取字幕"), calls["n"]

    def test_uses_server_hint(self):
        notes: list[str] = []
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] == 1:
                raise BiliRiskControl("被拦", code=429, retry_after=45.0)
            return "done"

        ok = jobs._with_risk_backoff(
            fn, lambda frac, msg: notes.append(msg), 0.0, "取字幕")
        self.assertEqual(ok, "done")
        self.assertEqual(sum(self.slept), 45.0)
        self.assertTrue(any("B站建议等 45s" in n for n in notes), notes)

    def test_falls_back_when_no_hint(self):
        ok, n = self._run(lambda: BiliRiskControl("被拦", code=-352), times=1)
        self.assertEqual(ok, "done")
        self.assertEqual(n, 2)
        self.assertEqual(sum(self.slept), 60.0)

    def test_absurd_hint_fails_fast_without_sleeping(self):
        def boom():
            raise BiliRiskControl("被拦", code=429, retry_after=3600.0)

        with self.assertRaises(RuntimeError) as cm:
            jobs._with_risk_backoff(boom, lambda *a: None, 0.0, "取字幕")
        self.assertIn("3600", str(cm.exception))
        self.assertIn("上限", str(cm.exception))
        self.assertEqual(self.slept, [])           # 一秒都没等

    def test_exhausted_message_reports_actual_waited(self):
        ok_calls = {"n": 0}

        def boom():
            ok_calls["n"] += 1
            raise BiliRiskControl("被拦", code=-352)

        with self.assertRaises(RuntimeError) as cm:
            jobs._with_risk_backoff(boom, lambda *a: None, 0.0, "取字幕")
        # 60 + 180 + 300 = 540，报错里应该是这个实际数字
        self.assertIn("540", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
