# -*- coding: utf-8 -*-
"""把已经落盘的字幕文件读成「可以展示的分段」。

**只读**：不触发抓取、不改任务状态、不碰导出流程。它是 API 层的展示适配，
所以放在 server/ 下面而不是顶层——CLI 不需要它。

为什么不让抓取时顺便把分段塞进 result
--------------------------------------
result 要能存进 SQLite 一行，一段一小时的视频约一千个时间戳，塞进去会让
任务表变胖；而且老任务没有这些字段，前端还得兼容两种形状。改成「读已经
写在磁盘上的文件」之后，历史任务自动就能看，不需要任何数据迁移。

为什么按 stem 分组后再挑格式
----------------------------
同一个视频会同时写出 `BV1xx-标题.txt` / `.srt` / `.json`（用户可能勾了多个
格式）。三个文件内容重复，全都返回会让正文出现三遍。所以按文件名主干分组，
每组只取信息量最大的那个：srt（带时间戳）> json（带时间戳且字段更全）>
txt（纯文本，退化成整块）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

#: 各格式的信息量排序，数字小的优先。
_FORMAT_RANK = {"srt": 0, "json": 1, "txt": 2}

#: 单个文件最多吐多少段。一小时视频约 1000 段，两万段够覆盖 20 小时了；
#: 设上限是为了挡住「误把整个语料目录当成一个任务产出」这种情况。
_MAX_SEGMENTS = 20000

#: 一次最多返回几个文件（多 P 合集）。超过的只回前 N 个并说明。
_MAX_PARTS = 60

#: 单文件读取上限。超过直接跳过并给出原因——把 20MB 文本塞进浏览器，
#: 页面会卡死，而且用户其实想看的是「怎么变成这样」，不是这 20MB。
_MAX_BYTES = 8 << 20

_ARROW = "-->"
_TS = re.compile(r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")

#: 必剪 ASR 那条路没有时间戳，导出 srt 时会写成整块占位时间。
#: 解析出来只有一个 0 → 99:59:59 的块，前端不能把它当成「一句话」。
_PLACEHOLDER_END = 99 * 3600 + 59 * 60 + 59


def _ts(raw: str) -> Optional[float]:
    """`00:01:02,500` → 62.5。解析不了就返回 None（上层跳过这一块）。"""
    m = _TS.search(raw or "")
    if not m:
        return None
    h, mnt, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mnt * 60 + s + ms / 1000.0


def parse_srt(text: str) -> list[dict]:
    """把 srt 解析成 ``[{start, end, text}]``（秒，float）。

    按行扫、不按空行切块。看起来绕，但这是被真实文件逼出来的：

    - **坏块只跳过自己**。手工编辑过的 srt 经常有个别时间轴写错，整篇报错
      比少一段更糟——用户看到的是一张白页。
    - **不依赖序号行**。有些工具导出的 srt 没有 `1 2 3` 这些行；靠 `-->`
      定位比靠序号稳。
    - **空行不一定是块边界**。Windows 上写文件会把 `\\n` 变成 `\\r\\n`，
      如果某一行本来就以 `\\r` 结尾，落盘就是 `\\r\\r\\n`，读回来多出一个
      空行——于是「序号」「时间轴」「正文」被拆进三个块，整篇解析成 0 段。
      按 `-->` 扫行不受这个影响。
    - **正文允许多行**。B站字幕一行的字不多，一句话常被拆成两行。

    代价是正文里若出现字面量 `-->` 会被当成新块开始。字幕正文里没有这种
    东西，不值得为它加转义规则。
    """
    if not text:
        return []
    # 先把连续的回车折成一个，再统一换行符。`\r\r\n` 是「本来就是 CRLF 的文本
    # 又被文本模式写了一遍」的产物，语义上仍是一个换行；不折的话它会变成
    # 一个空行，把时间轴和正文分开，整块解析失败。
    text = re.sub(r"\r+", "\r", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    lines = text.split("\n")
    out: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _ARROW not in line:
            i += 1
            continue
        left, _, right = line.partition(_ARROW)
        start, end = _ts(left), _ts(right)
        i += 1
        body: list[str] = []
        while i < len(lines) and lines[i].strip() and _ARROW not in lines[i]:
            body.append(lines[i].strip())
            i += 1
        if start is None or end is None or not body:
            continue
        out.append({"start": start, "end": end, "text": "\n".join(body)})
    return out


def _split_placeholder(segs: list[dict]) -> list[dict]:
    """占位块（0 → 99:59:59 包着全文）拆成按行的小段。

    必剪 ASR 兜底路径没有时间戳，`export.render_srt` 会写成一块。如果不拆，
    阅读页会把一整篇字幕画成一个段落——正是「不要把全文压成一整坨」要避免的。
    拆出来的段没有时间，`start`/`end` 置 None，前端据此隐藏时间轴。
    """
    if len(segs) != 1:
        return segs
    only = segs[0]
    if only["start"] > 0 or only["end"] < _PLACEHOLDER_END or "\n" not in only["text"]:
        return segs
    return [{"start": None, "end": None, "text": ln.strip()}
            for ln in only["text"].split("\n") if ln.strip()]


def _inside(path: Path, base: Path) -> bool:
    """路径必须落在任务自己的输出目录里。

    `files` 是任务执行时写下来的，理论上都是我们自己写的路径；但它是存在
    SQLite 里的**数据**，而这个接口会照着它去读磁盘、把内容发给浏览器。
    一旦有人手工改库、或者将来某个 handler 把用户输入拼进 files，这里就会
    变成「读任意文件」。所以按解析后的真实路径做一次归属校验，越界就不读。
    """
    try:
        path.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return False
    return True


def _natural_key(name: str) -> list:
    """让 `P2` 排在 `P10` 前面——字符串排序会把 P10 提到 P2 之前。"""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", name)]


def _pick(files: list[str], base: Path,
          skipped: list[dict]) -> list[Path]:
    """按 stem 分组、组内挑信息量最大的格式。"""
    chosen: dict[str, Path] = {}
    for raw in files:
        path = Path(str(raw))
        ext = path.suffix.lower().lstrip(".")
        if ext not in _FORMAT_RANK:
            continue
        if not _inside(path, base):
            skipped.append({"file": path.name,
                            "reason": "不在任务输出目录内，已忽略"})
            continue
        if not path.exists():
            skipped.append({"file": path.name, "reason": "文件已不存在"})
            continue
        old = chosen.get(path.stem)
        if old is None:
            chosen[path.stem] = path
            continue
        if _FORMAT_RANK[ext] < _FORMAT_RANK[old.suffix.lower().lstrip(".")]:
            # 换掉更好的一份；被换下来的不算 skip，它只是重复内容
            chosen[path.stem] = path
    return [chosen[k] for k in sorted(chosen, key=_natural_key)]


def _read_part(path: Path) -> dict:
    ext = path.suffix.lower().lstrip(".")
    size = path.stat().st_size
    if size > _MAX_BYTES:
        raise ValueError("文件 %.1f MB，超过 %d MB 上限"
                         % (size / 1048576.0, _MAX_BYTES >> 20))
    raw = path.read_text(encoding="utf-8", errors="replace")
    if ext == "srt":
        segs = _split_placeholder(parse_srt(raw))
    elif ext == "json":
        data = json.loads(raw)
        segs = []
        for s in data.get("segments") or []:
            txt = (s.get("text") or "").strip()
            if not txt:
                continue
            start = s.get("start")
            end = s.get("end")
            segs.append({"start": float(start) if start is not None else None,
                         "end": float(end) if end is not None else None,
                         "text": txt})
        if not segs and data.get("text"):
            segs = _txt_segments(data["text"])
    else:
        segs = _txt_segments(raw)
    truncated = len(segs) > _MAX_SEGMENTS
    if truncated:
        segs = segs[:_MAX_SEGMENTS]
    return {"name": path.name, "source": ext, "segments": segs,
            "truncated": truncated}


def _txt_segments(text: str) -> list[dict]:
    """纯文本没有时间，一行一段。"""
    return [{"start": None, "end": None, "text": ln.strip()}
            for ln in (text or "").split("\n") if ln.strip()]


def _hint(result: Optional[dict], parts: int) -> str:
    """没有正文时，说清「为什么没有」——空白页是最差的答案。"""
    if parts:
        return ""
    if not result:
        return "这个任务没有留下结果记录"
    nosub = result.get("no_subtitle") or 0
    failed = result.get("failed") or 0
    written = result.get("parts_written") or 0
    if written:
        # 抓到了但文件读不出来：多半是被手工删了，或者换了输出目录
        return "任务报告写了 %d 段，但输出文件已经不在原处（被移动或删除？）" % written
    if nosub and not failed:
        return ("这 %d 段都没有字幕轨。B站上并非每个视频都有 AI 字幕，"
                "这不是抓取失败——要转写音频得用 --asr 走必剪 ASR。" % nosub)
    bits = []
    if nosub:
        bits.append("%d 段无字幕" % nosub)
    if failed:
        bits.append("%d 处失败" % failed)
    return "没有拿到任何字幕" + ("（%s）" % "，".join(bits) if bits else "")


def build(task_id: str, result: Optional[dict]) -> dict:
    """把任务的 result 变成阅读页要的结构。

    返回值里 **不含绝对路径以外的隐私信息**，`outdir` 是本地自用，留着方便
    「文件在哪」这个常见追问。
    """
    result = result or {}
    outdir = result.get("outdir") or "."
    base = Path(outdir)
    skipped: list[dict] = []
    paths = _pick([str(x) for x in (result.get("files") or [])], base, skipped)

    parts: list[dict] = []
    for path in paths[:_MAX_PARTS]:
        try:
            part = _read_part(path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            skipped.append({"file": path.name, "reason": str(e)[:120]})
            continue
        if not part["segments"]:
            skipped.append({"file": path.name, "reason": "文件里没有可显示的内容"})
            continue
        parts.append(part)
    if len(paths) > _MAX_PARTS:
        skipped.append({"file": "…",
                        "reason": "文件太多，只显示了前 %d 个" % _MAX_PARTS})

    return {
        "task": task_id,
        "outcome": result.get("outcome"),
        "outdir": str(base),
        "parts": parts,
        "skipped": skipped[:10],
        "hint": _hint(result, len(parts)),
    }
