# -*- coding: utf-8 -*-
"""节流器测试。

全部用假时钟——测的是「该等多久」，不是「真的等」，所以不该产生任何真实延迟。
"""
from __future__ import annotations

import threading
import unittest

from asr_polish.throttle import DEFAULT_THROTTLED_HOSTS, Throttle, host_of

API = "https://api.bilibili.com/x/web-interface/view?bvid=BV1xx"
BCUT = "https://member.bilibili.com/x/bcut/rubick-interface/task"
CDN = "https://aisubtitle.hdslb.com/bfs/subtitle/abc.json"


class _FixedClock:
    """sleep 会把时间往前推，因此「等过了」这件事在假时间里是真实的。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class TestHostOf(unittest.TestCase):
    def test_extracts_host(self) -> None:
        self.assertEqual(host_of(API), "api.bilibili.com")

    def test_ignores_path_and_query(self) -> None:
        self.assertEqual(host_of("https://api.bilibili.com"), "api.bilibili.com")

    def test_unparsable(self) -> None:
        self.assertEqual(host_of("这不是一个 url"), "")


class TestThrottle(unittest.TestCase):
    def _make(self, min_interval: float = 1.0, jitter: float = 0.0) -> Throttle:
        self.clock = _FixedClock()
        return Throttle(min_interval=min_interval, jitter=jitter,
                        clock=self.clock, sleep=self.clock.sleep)

    def test_zero_interval_never_waits(self) -> None:
        t = self._make(min_interval=0.0)
        for _ in range(5):
            self.assertEqual(t.wait(API), 0.0)

    def test_first_request_never_waits(self) -> None:
        self.assertEqual(self._make().wait(API), 0.0)

    def test_second_request_waits_one_interval(self) -> None:
        t = self._make()
        t.wait(API)
        self.assertAlmostEqual(t.wait(API), 1.0)

    def test_every_following_request_waits(self) -> None:
        t = self._make()
        t.wait(API)
        self.assertAlmostEqual(t.wait(API), 1.0)
        self.assertAlmostEqual(t.wait(API), 1.0)
        self.assertAlmostEqual(t.waited, 2.0)

    def test_jitter_bounds(self) -> None:
        t = self._make(jitter=0.3)
        t._rng = lambda: 0.0                 # 抖到下限
        self.assertAlmostEqual(t.interval(), 0.7)
        t._rng = lambda: 1.0                 # 抖到上限
        self.assertAlmostEqual(t.interval(), 1.3)

    def test_bcut_is_not_throttled(self) -> None:
        """必剪是另一套服务，轮询本来就该是 1 秒一次，不该被 API 间隔拖慢。"""
        t = self._make()
        t.wait(BCUT)
        self.assertEqual(t.wait(BCUT), 0.0)

    def test_subtitle_cdn_is_not_throttled(self) -> None:
        t = self._make()
        t.wait(CDN)
        self.assertEqual(t.wait(CDN), 0.0)

    def test_hosts_are_independent(self) -> None:
        t = self._make()
        t.wait(API)
        self.assertEqual(t.wait(CDN), 0.0)
        self.assertAlmostEqual(t.wait(API), 1.0)

    def test_whitelist_is_api_gateway_only(self) -> None:
        self.assertIn("api.bilibili.com", DEFAULT_THROTTLED_HOSTS)
        self.assertNotIn("member.bilibili.com", DEFAULT_THROTTLED_HOSTS)

    def test_concurrent_callers_get_distinct_slots(self) -> None:
        """5 个线程同时进来，应当排成 1/2/3/4 秒，而不是一起放行。

        用不推进时间的 sleep，把「谁排到第几个位置」这件事暴露出来：
        若锁失效，所有线程都会看到 last=None 或同一个 target。
        """
        clock = _FixedClock()
        delays: list[float] = []
        lock = threading.Lock()

        def fake_sleep(seconds: float) -> None:
            with lock:
                delays.append(seconds)

        t = Throttle(min_interval=1.0, jitter=0.0,
                     clock=clock, sleep=fake_sleep)
        threads = [threading.Thread(target=t.wait, args=(API,)) for _ in range(5)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(sorted(delays), [1.0, 2.0, 3.0, 4.0])

    def test_configure_and_reset(self) -> None:
        t = self._make(min_interval=2.0)
        t.wait(API)
        self.assertAlmostEqual(t.wait(API), 2.0)
        t.reset()
        self.assertEqual(t.waited, 0.0)
        self.assertEqual(t.wait(API), 0.0)      # reset 后重新计时

    def test_jitter_clamped(self) -> None:
        t = self._make(jitter=0.0)
        t.configure(jitter=5.0)
        self.assertEqual(t.jitter, 1.0)
        t.configure(min_interval=-3)
        self.assertEqual(t.min_interval, 0.0)


if __name__ == "__main__":
    unittest.main()
