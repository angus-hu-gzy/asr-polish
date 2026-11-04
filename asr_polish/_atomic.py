# -*- coding: utf-8 -*-
"""原子替换：先写同目录临时文件，再 ``os.replace``。

单独放一个模块，是为了让「撞上瞬时占用怎么办」只写一遍。

``os.replace`` 在 Windows 上会因为杀软正在扫描刚生成的文件、或别的程序正开着
目标文件，而报 ``PermissionError: [WinError 5] 拒绝访问``。这不是写入失败——
重试几十毫秒就过去了。实测过一次：同一份 Cookie 导入两次，第二次一个字节
都没变，重写却在 CI 的 Windows runner 上炸了。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Optional

#: 重试间隔（秒）。累计约 0.75 秒：短到用户察觉不到，长到够杀软扫完一个小文件。
REPLACE_DELAYS = (0.05, 0.1, 0.2, 0.4)


def replace_with_retry(
    tmp: Path,
    target: Path,
    on_error: Optional[Callable[[OSError], BaseException]] = None,
) -> None:
    """把 ``tmp`` 替换成 ``target``，遇到瞬时占用就退避重试。

    ``on_error`` 用来把最终失败翻译成调用方自己的异常类型（比如 ``BiliError``
    带上一句能照做的提示）；不给就原样抛出 ``PermissionError``。

    最终失败时会清理临时文件——调用方不必再管，也不会留下半份内容。
    """
    for delay in REPLACE_DELAYS:
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            time.sleep(delay)
    try:
        os.replace(tmp, target)
    except PermissionError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        if on_error is not None:
            raise on_error(exc) from exc
        raise
