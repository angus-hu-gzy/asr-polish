# -*- coding: utf-8 -*-
"""任务执行器：把 CLI 里那套抓取流程包装成可被 worker 调用的函数。

每个执行函数的签名统一为：

    func(payload: dict, progress: Callable[[float, str], None]) -> dict

- `payload` 是任务参数（来自 HTTP 请求体）
- `progress(比例 0~1, 说明文字)` 由 worker 注入，内部顺带刷新心跳
- 返回值写进任务的 result 字段

单个 job 内部不再开线程池：并发度由 worker 数量决定，
这样「谁在干活」在任务表里始终看得见。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

from .. import export
from ..bili import (BiliError, BiliLoginRequired, BiliRiskControl, explain_risk,
                    expand_pages, extract_bvid, extract_mid, fetch,
                    get_video_info, group_only_pages, list_season,
                    list_up_videos, parse_only_targets, search_videos)

Progress = Callable[[float, str], None]

#: 单条视频抓取失败的容忍度：全失败才判 job 失败
_MAX_ERRORS_KEPT = 5

#: 结果里最多保留多少条失败明细（面板要列出来，也不能无限长）
_MAX_FAILED_KEPT = 200


class JobFailure(RuntimeError):
    """执行失败，但已经跑出来的那部分结果要留着。

    以前全失败只抛一句话，`result` 是空的——面板上只剩一个错误字符串，
    用户看不到「哪几条失败、为什么」，也就没法只重跑失败的那几条。
    异常带上 result，worker 落库时会把它一起写进任务的 result 字段。
    """

    def __init__(self, message: str, result: Optional[dict] = None) -> None:
        super().__init__(message)
        self.result = result


#: 被风控后依次等待的秒数。取 60 / 180 / 300 的理由：
#: B站的临时限制多在几分钟内解除，等太短等于白等（还会加重判定），
#: 等太长用户会以为卡死。三档合计约 9 分钟，之后仍被拦就是策略问题了，
#: 该让任务失败并提示用户降速，而不是继续耗着。
_RISK_BACKOFF = (60.0, 180.0, 300.0)

#: 退避时把等待切成小段，每段都刷新心跳与进度说明。
#: 不分段的话，退避 300 秒期间任务没有任何心跳，会被回收器判成僵尸重新排队。
_BACKOFF_TICK = 15.0

#: 可替换的 sleep（测试用，避免真的等几分钟）
_sleep = time.sleep


def _noop(_frac: float, _msg: str) -> None:
    pass


def _sleep_with_heartbeat(total: float, progress: Progress,
                          frac: float, note: str) -> None:
    """分段睡眠，期间持续汇报进度——既刷新心跳，也让用户在面板上看见"在等"。"""
    remaining = float(total)
    while remaining > 0:
        step = min(_BACKOFF_TICK, remaining)
        progress(frac, "%s，还需 %ds" % (note, int(remaining + 0.999)))
        _sleep(step)
        remaining -= step


def _with_risk_backoff(fn: Callable, progress: Progress, frac: float,
                       what: str) -> object:
    """执行 fn()；遇到风控就退避后重试同一个调用。

    与「把这一条记成失败、接着打下一条」的区别很关键：风控是按 IP / 账号
    记的，不是按单条视频记的。继续打只会让限制延长，而且剩余几十条大概率
    全失败——用户拿到一堆假失败，还得自己去猜原因。

    ``BiliLoginRequired`` 直接上抛：Cookie 没换，等多久结果都一样。
    """
    last: Optional[BiliRiskControl] = None
    for attempt in range(len(_RISK_BACKOFF) + 1):
        try:
            return fn()
        except BiliLoginRequired:
            raise
        except BiliRiskControl as e:
            # -352 有两种成因：请求太快，或者压根没登录。后者退避多久都没用，
            # 所以先探一次登录态再决定是「等」还是「让用户换 Cookie」。
            explained = explain_risk(e)
            if isinstance(explained, BiliLoginRequired):
                raise explained from e
            last = explained
            if attempt >= len(_RISK_BACKOFF):
                break
            wait = _RISK_BACKOFF[attempt]
            _sleep_with_heartbeat(
                wait, progress, frac,
                "第 %d 次被风控（%s），退避 %ds 后重试"
                % (attempt + 1, what, int(wait)))
    raise RuntimeError(
        "连续 %d 次被风控（最后一次：%s）。已累计退避 %d 秒仍被拦，"
        "继续重试只会延长限制。建议：把 --min-interval 调大（例如 3）、"
        "降低单次任务条数，或更新 Cookie 后重试。"
        % (len(_RISK_BACKOFF) + 1, last,
           int(sum(_RISK_BACKOFF)))) from last


def resolve_targets(target: str, limit: Optional[int] = None,
                    pages: int = 1) -> list[str]:
    """把各种 target 写法解析成 bvid 列表。

    - `BV1xxxxxxxxx` 或任意含 BV 号的链接
    - `up:329954990`       UP 主投稿
    - `season:329954990:12345` 合集
    - `search:关键词`      站内搜索
    """
    raw = (target or "").strip()
    low = raw.lower()

    if low.startswith("up:"):
        mid = extract_mid(raw[3:])
        if not mid:
            raise ValueError("无法识别 UP 主 mid：%s" % raw)
        return [x["bvid"] for x in
                list_up_videos(mid, max_count=limit or 0)]

    if low.startswith("season:"):
        parts = raw.split(":")
        if len(parts) != 3:
            raise ValueError("合集写法应为 season:<mid>:<season_id>")
        return [x["bvid"] for x in
                list_season(int(parts[1]), int(parts[2]),
                            max_count=limit or 0)]

    if low.startswith("search:"):
        kw = raw.split(":", 1)[1].strip()
        if not kw:
            raise ValueError("搜索关键词为空")
        return [x["bvid"] for x in search_videos(kw, pages=pages)]

    bv = extract_bvid(raw)
    if not bv:
        raise ValueError("无法识别目标：%s" % raw)
    return [bv]


def run_subs(payload: dict, progress: Progress = _noop) -> dict:
    """抓字幕并落盘。payload 字段见模块文档。"""
    target = payload.get("target") or ""
    raw_formats = payload.get("formats") or "txt"
    if isinstance(raw_formats, (list, tuple)):          # HTTP 里常写成数组
        raw_formats = ",".join(str(x) for x in raw_formats)
    formats = export.parse_formats(raw_formats)
    outdir = Path(payload.get("outdir") or "subs")
    limit = payload.get("limit")
    limit = int(limit) if limit else None
    want_pages = str(payload.get("pages") or "all").strip().lower()
    want: Optional[set] = None
    if want_pages != "all":
        want = {int(x) for x in want_pages.split(",") if x.strip().isdigit()}

    progress(0.02, "解析目标")
    only = parse_only_targets(payload.get("only"))
    if only is not None:
        # 重跑指定条目：**不重新解析 target**。UP 主投稿会一直变，重新解析
        # 拿到的是「现在最新的 20 条」，不是「刚才失败的那 3 条」——那就等于
        # 整个任务重来了，正是这次要消灭的行为。
        only_pages = group_only_pages(only)
        targets = list(only_pages)                     # dict 保序、已去重
    else:
        only_pages = {}
        try:
            targets = _with_risk_backoff(
                lambda: resolve_targets(
                    target, limit=limit,
                    pages=int(payload.get("search_pages") or 1)),
                progress, 0.02, "解析目标")
        except BiliLoginRequired as e:
            raise RuntimeError(
                "需要登录态：%s。\n"
                "修复办法：重新导出 cookies.txt，放到 README「登录态」一节列出的"
                "任一位置（或用 --cookie 指定），再重试；"
                "只想抓单个视频的话可以改用 BV 号。" % e) from e
    if not targets:
        raise ValueError("没有找到可处理的视频")
    if only is None and limit and len(targets) > limit:
        targets = targets[:limit]

    total = len(targets)
    items: list[dict] = []
    errors: list[str] = []
    n_ok = n_nosub = n_fail = 0

    for i, bvid in enumerate(targets, 1):
        row = {"bvid": bvid, "title": "", "parts": 0, "nosub": 0,
               "error": "", "note": "", "pages": 1, "failed_pages": []}
        frac_hint = 0.05 + 0.93 * ((i - 1) / total)
        try:
            info = _with_risk_backoff(
                lambda bv=bvid: get_video_info(bv),
                progress, frac_hint, "取视频信息")
            row["title"] = info.get("title") or bvid
            total_pages = len(info.get("pages") or []) or 1
            row["pages"] = total_pages
            pages = expand_pages(info)
            if want is not None:
                pages = [p for p in pages if p["index"] in want]
            if bvid in only_pages and only_pages[bvid] is not None:
                # 重跑时限定到当初失败的那几个分 P，别把已经成功的重做一遍。
                pages = [p for p in pages if p["index"] in only_pages[bvid]]

            for p in pages:
                if payload.get("stop"):
                    break
                try:
                    res = _with_risk_backoff(
                        lambda bv=bvid, ix=p["index"]: fetch(
                            bv, page=ix, allow_asr=bool(payload.get("asr"))),
                        progress, frac_hint, "取第 %s P" % p["index"])
                except BiliError as e:
                    n_fail += 1
                    msg = str(e)
                    errors.append("%s P%s: %s" % (bvid, p["index"], msg))
                    # 分 P 级失败也要落到明细里：否则 20 条成了 18 条时，
                    # 用户只知道「差了 2」，不知道差在哪一条哪一个 P。
                    row["failed_pages"].append(
                        {"page": p["index"], "error": msg[:200]})
                    continue
                if not res.segments:
                    row["nosub"] += 1
                    n_nosub += 1
                    if res.warnings:
                        # 例如「未登录导致取不到 AI 字幕」——不带出来用户只会
                        # 以为这些视频本来就没字幕。
                        row["note"] = res.warnings[-1][:160]
                    continue
                if payload.get("correct"):
                    from ..correction import correct_text
                    fixed = [correct_text(s["text"]).text for s in res.segments]
                    for seg, txt in zip(res.segments, fixed):
                        seg["text"] = txt
                    res.text = "\n".join(fixed)
                part = p["title"] if total_pages > 1 else ""
                stem = export.base_name(res, p["title"], total_pages)
                export.write_all(outdir, stem, formats, res, part,
                                 skip_existing=bool(payload.get("skip_existing")))
                row["parts"] += 1
                n_ok += 1
        except BiliLoginRequired as e:
            # Cookie 没换，等多久都是同样的结果；整批都没戏，立刻停。
            raise RuntimeError(
                "需要登录态：%s。\n"
                "修复办法：重新导出 cookies.txt，放到 README「登录态」一节列出的"
                "任一位置（或用 --cookie 指定），再重试；"
                "只想抓单个视频的话可以改用 BV 号。" % e) from e
        except BiliError as e:
            row["error"] = str(e)
            n_fail += 1
            errors.append("%s: %s" % (bvid, e))
        items.append(row)

        frac = 0.05 + 0.93 * (i / total)
        progress(frac, "已处理 %d/%d：%s" % (i, total, row["title"][:28]))

    failed_items = _collect_failures(items)
    outcome = _judge(n_ok, n_nosub, n_fail)
    result = {
        "target": target,
        "outcome": outcome,
        "videos": total,
        "parts_written": n_ok,
        "no_subtitle": n_nosub,
        "failed": n_fail,
        "outdir": str(outdir.resolve()),
        "formats": formats,
        "items": items[:200],
        "failed_items": failed_items[:_MAX_FAILED_KEPT],
        "errors": errors[:_MAX_ERRORS_KEPT],
    }
    if only is not None:
        # 让结果自己能说清「这是重跑出来的那几条」，面板上不必去翻 payload。
        result["only"] = [str(x) for x in (payload.get("only") or [])]
        result["retry_of"] = payload.get("retry_of")
    if outcome == "failed":
        # 全失败仍然是「任务失败」（状态 failed），但这次把明细一起带出去：
        # 只留一句「全部失败」等于把已经拿到的错误线索扔掉。
        raise JobFailure(
            "全部失败（%d 处错误）：%s"
            % (n_fail, "; ".join(errors[:3]) or "未知原因"), result)
    progress(1.0, _done_note(outcome, n_ok, n_nosub, n_fail))
    return result


def _collect_failures(items: list[dict]) -> list[dict]:
    """把每个视频的失败点摊平成「可单独重跑」的条目。

    ``retry`` 字段就是重跑时要传给 ``only`` 的写法（``BV1xx`` 或
    ``BV1xx#3``），面板和 CLI 直接照抄，不用各自再拼一遍。
    """
    out: list[dict] = []
    for row in items:
        if row.get("error"):
            out.append({"bvid": row["bvid"], "title": row.get("title") or "",
                        "page": None, "error": row["error"][:200],
                        "retry": row["bvid"]})
        multi = int(row.get("pages") or 1) > 1
        for bad in row.get("failed_pages") or []:
            page = bad.get("page")
            # 单 P 视频不写 `#1`：那等于把「整条」写复杂了，重跑时也一样。
            token = "%s#%s" % (row["bvid"], page) if multi else row["bvid"]
            out.append({"bvid": row["bvid"], "title": row.get("title") or "",
                        "page": page, "error": bad.get("error", ""),
                        "retry": token})
    return out


def _judge(n_ok: int, n_nosub: int, n_fail: int) -> str:
    """四态结论：complete / partial / empty / failed。

    判的是「产出」，不是「有没有报错」：

    - 有产出且处处干净 → complete
    - 有产出但差了几条（失败或没字幕）→ partial
    - 零产出 → empty。**只要还有「没字幕」这个解释，就不算失败**：20 条全没字幕
      时 `failed` 是 0，判成「成功」是撒谎，判成「失败」也不对——它确实没出错。
    - 零产出且全是错误 → failed
    """
    if n_ok:
        return "partial" if (n_fail or n_nosub) else "complete"
    if n_nosub or not n_fail:
        return "empty"
    return "failed"


def _done_note(outcome: str, n_ok: int, n_nosub: int, n_fail: int) -> str:
    if outcome == "complete":
        return "完成：%d 段全部成功" % n_ok
    if outcome == "empty":
        if n_nosub:
            return ("完成，但没有产出：%d 段都没有字幕" % n_nosub
                    + ("，另有 %d 处失败" % n_fail if n_fail else ""))
        return "完成，但没有产出：没有可处理的条目"
    bits = ["成功 %d 段" % n_ok]
    if n_nosub:
        bits.append("无字幕 %d 段" % n_nosub)
    if n_fail:
        bits.append("失败 %d 处" % n_fail)
    return "部分成功：" + "，".join(bits)



#: 任务类型注册表。新增一种任务就在这里加一行。
HANDLERS = {
    "subs": run_subs,
}


def run(kind: str, payload: dict, progress: Progress = _noop) -> dict:
    handler = HANDLERS.get(kind)
    if handler is None:
        raise ValueError("未知任务类型：%s（可用：%s）"
                         % (kind, ", ".join(sorted(HANDLERS))))
    return handler(payload, progress)
