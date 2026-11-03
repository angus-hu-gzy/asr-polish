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

from pathlib import Path
from typing import Callable, Optional

from .. import export
from ..bili import (BiliError, expand_pages, extract_bvid, extract_mid, fetch,
                   get_video_info, list_season, list_up_videos, search_videos)

Progress = Callable[[float, str], None]

#: 单条视频抓取失败的容忍度：全失败才判 job 失败
_MAX_ERRORS_KEPT = 5


def _noop(_frac: float, _msg: str) -> None:
    pass


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
    targets = resolve_targets(target, limit=limit,
                              pages=int(payload.get("search_pages") or 1))
    if not targets:
        raise ValueError("没有找到可处理的视频")
    if limit and len(targets) > limit:
        targets = targets[:limit]

    total = len(targets)
    items: list[dict] = []
    errors: list[str] = []
    n_ok = n_nosub = n_fail = 0

    for i, bvid in enumerate(targets, 1):
        row = {"bvid": bvid, "title": "", "parts": 0, "nosub": 0, "error": ""}
        try:
            info = get_video_info(bvid)
            row["title"] = info.get("title") or bvid
            total_pages = len(info.get("pages") or []) or 1
            pages = expand_pages(info)
            if want is not None:
                pages = [p for p in pages if p["index"] in want]

            for p in pages:
                if payload.get("stop"):
                    break
                try:
                    res = fetch(bvid, page=p["index"],
                                allow_asr=bool(payload.get("asr")))
                except BiliError as e:
                    n_fail += 1
                    errors.append("%s P%s: %s" % (bvid, p["index"], e))
                    continue
                if not res.segments:
                    row["nosub"] += 1
                    n_nosub += 1
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
        except BiliError as e:
            row["error"] = str(e)
            n_fail += 1
            errors.append("%s: %s" % (bvid, e))
        items.append(row)

        frac = 0.05 + 0.93 * (i / total)
        progress(frac, "已处理 %d/%d：%s" % (i, total, row["title"][:28]))

    result = {
        "target": target,
        "videos": total,
        "parts_written": n_ok,
        "no_subtitle": n_nosub,
        "failed": n_fail,
        "outdir": str(outdir.resolve()),
        "formats": formats,
        "items": items[:200],
        "errors": errors[:_MAX_ERRORS_KEPT],
    }
    if n_ok == 0 and n_fail > 0:
        raise RuntimeError("全部失败：%s" % ("; ".join(errors[:3]) or "未知原因"))
    progress(1.0, "完成")
    return result


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
