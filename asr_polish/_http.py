# -*- coding: utf-8 -*-
"""用完的 ``HTTPError`` 关掉——只做这一件事。

``urllib.error.HTTPError`` 自己就是一份打开着的响应（``addinfourl``，底下是一
个 socket 的 file object）。读完了不关，它就挂在 GC 上等着被回收：Python 3.13+
会为此打一条 ``ResourceWarning``，那行噪音长得像「我们自己漏了资源」，正好盖住
真正的失败原因——限流、key 失效、被风控拦下都走这条路，而这几种恰恰是需要看清
原因的时候；顺带每失败一次漏一个连接，412/429 那种反复重试的路径上会攒起来。

为什么不是直接 ``exc.close()``：3.9 的 ``addbase.__getattr__`` 是往 ``self.fp``
上转发的，fp 不是真文件对象时（测试里塞的替身、或者已经关过一次的对象）会抛
``KeyError`` / ``ValueError``。关不掉可以算了，但不能因为「顺手关一下」把真正的
异常换掉——那才是真的把原因埋了。所以这里只做一件事：尽力去关，什么都吞。
"""

from __future__ import annotations


def close_quietly(exc) -> None:
    """尽力关掉一个 HTTPError（或任何带 ``close`` 的响应对象）。"""
    try:
        exc.close()
    except Exception:          # 见模块注释：关不掉不是错，换掉原异常才是
        pass
