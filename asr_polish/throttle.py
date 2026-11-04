# -*- coding: utf-8 -*-
"""按域名的请求间隔控制。

为什么需要它：批量抓一个 UP 主几十条视频时，每条要打 3～4 个 B站接口
（view → nav → player/wbi/v2 → 字幕 CDN），全速打就是几百个请求连着发出。
B站对这类模式的处理是「风控校验失败」，之后一段时间内**所有**接口都拒绝，
不是只拒被拦的那一条。

设计取舍
--------
1. **放到底层（``_http_json``）而不是批量循环里。** 循环里加 sleep 只能管
   主流程，管不到 ``list_up_videos`` 翻页、``_wbi_mixin_key`` 取密钥这些旁路
   请求，风控照样会来。
2. **按 host 分组。** 必剪（``member.bilibili.com``）是另一套服务，它的轮询
   本来就该是 1 秒一次；字幕 CDN 是静态资源。这两类不该被 API 的间隔拖慢，
   否则只是白白变慢，换不来任何风控上的好处。
3. **加抖动。** 固定 1000ms 的等间隔本身就是机器特征。默认 ±30%。

测试友好：``clock`` / ``sleep`` / ``rng`` 都可注入，测试里用假时钟推进，
不需要真的等。
"""
from __future__ import annotations

import random
import threading
import time
import urllib.parse
from typing import Callable, Iterable, Optional

#: 需要节流的域名。刻意写成白名单而不是「所有 bilibili.com」——
#: 风控发生在 API 网关（api.bilibili.com），字幕文件走的是对象存储。
DEFAULT_THROTTLED_HOSTS = ("api.bilibili.com",)


def host_of(url: str) -> str:
    """取 url 的 host；解析不出返回空串。"""
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


class Throttle:
    """保证同一 host 上两次请求之间有间隔。

    间隔不是固定值：目标值为 ``min_interval``，再按 ``jitter`` 上下浮动
    （默认 ±30%，即 1.0 秒会落在 0.7～1.3 秒之间）。别把它当硬下限用。

    线程安全。锁只保护时间账本的读写，等待在锁外进行——否则一个 worker 在等
    api.bilibili.com 的间隔时，会把要访问必剪的另一个 worker 一起堵住。

    >>> t = Throttle(min_interval=1.0, jitter=0.0)
    >>> t.min_interval
    1.0
    """

    def __init__(self, min_interval: float = 0.0, jitter: float = 0.3,
                 hosts: Iterable[str] = DEFAULT_THROTTLED_HOSTS,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 rng: Optional[Callable[[], float]] = None) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self.jitter = min(1.0, max(0.0, float(jitter)))
        self.hosts = tuple(h.lower() for h in hosts)
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.random
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self.waited = 0.0            # 累计等待秒数，便于观测与测试

    # ── 配置 ────────────────────────────────────────────────

    def configure(self, min_interval: Optional[float] = None,
                  jitter: Optional[float] = None) -> None:
        with self._lock:
            if min_interval is not None:
                self.min_interval = max(0.0, float(min_interval))
            if jitter is not None:
                self.jitter = min(1.0, max(0.0, float(jitter)))

    def applies_to(self, url: str) -> bool:
        if self.min_interval <= 0:
            return False
        host = host_of(url)
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    # ── 主逻辑 ──────────────────────────────────────────────

    def interval(self) -> float:
        """本次应等待的目标间隔（含抖动）。"""
        if self.min_interval <= 0:
            return 0.0
        span = self.min_interval * self.jitter
        return self.min_interval + span * (2.0 * self._rng() - 1.0)

    def wait(self, url: str) -> float:
        """必要时阻塞，返回实际等待的秒数。

        不在白名单里的 url 立即返回 0，不记账——省掉一次锁。
        """
        if not self.applies_to(url):
            return 0.0
        host = host_of(url)
        with self._lock:
            now = self._clock()
            last = self._last.get(host)
            if last is None:
                self._last[host] = now
                return 0.0
            target = last + self.interval()
            if target <= now:
                self._last[host] = now
                return 0.0
            # 先把账本推进到「租约结束时刻」，这样并发线程会依次排队，
            # 而不是一起算出同一个 target 然后同时放行。
            self._last[host] = target
            delay = target - now
        self._sleep(delay)
        with self._lock:
            self.waited += delay
        return delay

    def reset(self) -> None:
        """清空账本（测试与配置变更后重新计时用）。"""
        with self._lock:
            self._last.clear()
            self.waited = 0.0
