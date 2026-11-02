# -*- coding: utf-8 -*-
"""字幕导出：txt / srt / json / md 四种格式。

只依赖 FetchResult（bili.fetch 的返回值）与纯标准库。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Iterable

from . import __version__
from .bili import FetchResult

FORMATS = ("txt", "srt", "json", "md")
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_name(text: str, limit: int = 40) -> str:
    s = _ILLEGAL.sub("_", (text or "").strip())
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"_+", "_", s).strip(" ._")
    return s[:limit].strip(" ._") or "untitled"


def hhmmss(seconds: float) -> str:
    s = int(round(seconds or 0))
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def _srt_ts(seconds: float) -> str:
    ms = int(round((seconds or 0) * 1000))
    return (f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:"
            f"{ms // 1000 % 60:02d},{ms % 1000:03d}")


def base_name(result: FetchResult, part_title: str = "",
              page_count: int = 1) -> str:
    """多 P 用分 P 标题命名，单 P 用视频标题。"""
    parts = [result.bvid]
    if page_count > 1:
        parts.append(f"P{result.page}")
        parts.append(safe_name(part_title or result.title))
    else:
        parts.append(safe_name(result.title))
    return "-".join(parts)


def parse_formats(raw: str) -> list[str]:
    items = [x.strip().lower() for x in (raw or "").split(",") if x.strip()]
    bad = [x for x in items if x not in FORMATS]
    if bad:
        raise ValueError(f"不支持的格式：{', '.join(bad)}"
                         f"（可选 {', '.join(FORMATS)}）")
    return items or ["txt"]


def _is_ai(result: FetchResult) -> bool:
    return result.subtitle_lan.startswith("ai-")


def render_txt(result: FetchResult) -> str:
    return result.text


def render_srt(result: FetchResult) -> str:
    if not result.segments:
        # 必剪 ASR 路径没有时间戳，退化为按行顺序编号的整段字幕
        return "1\n00:00:00,000 --> 99:59:59,000\n" + result.text
    blocks = []
    for i, s in enumerate(result.segments, 1):
        blocks.append(f"{i}\n{_srt_ts(s['start'])} --> {_srt_ts(s['end'])}\n"
                      f"{s['text']}")
    return "\n\n".join(blocks)


def render_json(result: FetchResult, part_title: str = "") -> str:
    payload = {
        "bvid": result.bvid,
        "page": result.page,
        "part_title": part_title,
        "title": result.title,
        "owner": result.owner,
        "duration": result.duration,
        "url": f"https://www.bilibili.com/video/{result.bvid}",
        "source": result.source,
        "track": result.subtitle_lan,
        "tool": f"asr-polish {__version__}",
        "fetched_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "segments": [{"start": round(s["start"], 2),
                      "end": round(s["end"], 2),
                      "text": s["text"]} for s in result.segments],
    }
    if not result.segments:
        payload["text"] = result.text
    return json.dumps(payload, ensure_ascii=False, indent=1)


def render_md(result: FetchResult, part_title: str = "") -> str:
    kind = "AI 生成，含识别错字" if _is_ai(result) else (
        "人工 / 上传轨" if result.subtitle_lan else result.source)
    head = [
        f"# {result.title}",
        "",
        f"- 视频：https://www.bilibili.com/video/{result.bvid}",
    ]
    if part_title:
        head.append(f"- 分 P：P{result.page} · {part_title}")
    head += [
        f"- UP 主：{result.owner}",
        f"- 时长：{hhmmss(result.duration)}",
        f"- 来源：{result.source}（`{result.subtitle_lan or '-'}` · {kind}）",
        f"- 抓取时间：{_dt.datetime.now():%Y-%m-%d %H:%M}",
        "",
        "---",
        "",
    ]
    return "\n".join(head) + result.text + "\n"


_RENDERERS = {
    "txt": render_txt,
    "srt": render_srt,
    "json": render_json,
    "md": render_md,
}


def render(fmt: str, result: FetchResult, part_title: str = "") -> str:
    fn = _RENDERERS.get(fmt)
    if fn is None:
        raise ValueError(f"未知格式：{fmt}")
    if fmt in ("json", "md"):
        return fn(result, part_title)  # type: ignore[operator]
    return fn(result)  # type: ignore[operator]


def write_all(outdir: Path, stem: str, formats: Iterable[str],
              result: FetchResult, part_title: str = "",
              skip_existing: bool = False) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for fmt in formats:
        path = outdir / f"{stem}.{fmt}"
        if skip_existing and path.exists() and path.stat().st_size > 0:
            continue
        path.write_text(render(fmt, result, part_title), encoding="utf-8")
        written.append(path)
    return written
