# -*- coding: utf-8 -*-
"""MCP server：把「取字幕」变成 agent 能直接调用的工具。

为什么要有它
------------
装完 asr-polish 之后的使用姿势是：打开终端、回忆命令、敲参数、再去目录里
翻文件。但真实场景里人是这么说话的——「帮我把这条视频的字幕取下来，顺手
订正一下」。中间那一层（记命令、记参数、记输出到哪）没有产生任何价值。

所以这里把同一个 :func:`asr_polish.server.jobs.run_subs` 包一层 MCP
（Model Context Protocol）工具。**不新增任何抓取逻辑**：工具调用的就是
CLI 和本地服务在用的那个函数，四态判定、失败明细、「只重跑失败条目」
的行为完全一致。

实现取舍
--------
1. **零第三方依赖。** MCP 的 stdio 传输就是「一行一条 JSON-RPC 消息」，
   标准库足够。为此引入官方 SDK（会带 pydantic / httpx / anyio 一串）与
   这个包「零依赖」的定位直接冲突——那是这个包最实在的一条卖点。
2. **stdout 只能有协议消息。** 任何 ``print`` / 未捕获的 traceback 落到
   stdout，对端就会解析失败，整个 server 一起失联。所以诊断一律走 stderr，
   工具里的异常转成 ``isError`` 的结果而不是往上抛。
3. **同步执行 + 进度通知。** 单条视频几秒，二十条几分钟。客户端给了
   ``progressToken`` 就发 ``notifications/progress``，让「还在动」看得见。
   没做成「提交任务 + 轮询」：那要再引入后台线程和一张任务表，而批量的
   正路本来就是 ``asr-polish serve``（有队列、断点、面板）。
"""

from __future__ import annotations

import json
import sys
import time
from typing import Callable, Optional

from . import __version__
from .bili import (BiliError, BiliRiskControl, cookie_status, extract_mid,
                   list_season, list_up_videos, search_videos)
from .correction import correct_text as _correct
from .server.jobs import JobFailure as _JobFailure
from .server.jobs import run_subs

#: 我们实现的协议版本。客户端会带自己的版本过来，认识就照着回，不认识就
#: 用自己的——只用到 tools 这一层，这几个版本的差异不影响本 server。
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
PROTOCOL_VERSION = PROTOCOL_VERSIONS[-1]
SERVER_NAME = "asr-polish"

#: 进度通知最多每秒一条。抓取过程中 progress 回调可能一秒钟触发几十次，
#: 每条都发会把管道刷满，而且对端也只是拿它画个进度条。
_PROGRESS_INTERVAL = 1.0

_TARGET_DOC = (
    "目标，四种写法：BV 号或任意含 BV 号的链接（如 BV1xx411c7mD / "
    "https://www.bilibili.com/video/BV1xx411c7mD）；"
    "up:<mid 或主页链接> 取该 UP 主的投稿；"
    "season:<mid>:<season_id> 取合集；search:<关键词> 取站内搜索结果"
)

_TOOLS = [
    {
        "name": "fetch_subtitles",
        "description": (
            "把 B站视频的字幕取下来并存到本地（txt/srt/json/md）。"
            "有字幕轨就直接取（含 B站 AI 字幕），没有可选走云端 ASR 转写。"
            "返回四态结果：complete 全部成功 / partial 部分成功 / "
            "empty 视频本来没字幕 / failed 全部失败，并逐条列出失败的条目和原因。"
            "返回里带 files 字段（真实落盘路径）与 outdir。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": _TARGET_DOC},
                "outdir": {"type": "string",
                           "description": "输出目录，默认 subs"},
                "formats": {"type": "string",
                            "description": "逗号分隔，可选 txt,srt,json,md；默认 txt"},
                "limit": {"type": "integer",
                          "description": "最多处理多少条视频。批量抓时建议给 10"},
                "pages": {"type": "string",
                          "description": "分P，如 \"1,3\"；默认 all"},
                "max_parts": {"type": "integer",
                              "description": "单个视频最多取几个分P，默认 20（多P合集常有几十上百 P）。"
                                             "要全取传 0"},
                "asr": {"type": "boolean",
                        "description": "无字幕时是否走必剪云端 ASR 转写（需要 yt-dlp，慢）"},
                "correct": {"type": "boolean",
                            "description": "是否顺手做中文术语订正"},
                "only": {"type": "array", "items": {"type": "string"},
                         "description": "只重跑这些条目（已成功的不会重做），"
                                        "如 [\"BV1xx411c7mD\", \"BV1yy#3\"]"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "list_videos",
        "description": (
            "只列清单不下载：给定 UP 主 / 合集 / 关键词，返回视频的 bvid、"
            "标题、时长。用来先看看有什么，再决定抓哪些——避免一次性抓几十条"
            "触发 B站风控。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": _TARGET_DOC},
                "limit": {"type": "integer",
                          "description": "最多返回多少条，默认 20"},
                "pages": {"type": "integer",
                          "description": "search: 时搜几页，默认 1"},
            },
            "required": ["target"],
        },
    },
    {
        "name": "correct_text",
        "description": (
            "对一段中文 ASR 转写文本做术语订正（把「接生」改成 JSON 这类）。"
            "不需要联网，也不改动任何文件。返回订正后的文本和逐处改动。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "待订正的文本"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "check_login",
        "description": (
            "检查 B站 Cookie 是否可用。会真的请求一次 nav 接口验证 SESSDATA，"
            "这是判断 Cookie 过期的唯一可靠办法（文件存在、字段齐全都说明不了）。"
            "UP 主投稿 / 合集 / 搜索这几个接口必须登录，单条 BV 号不用。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ── 工具实现 ────────────────────────────────────────────────────


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _summary_text(result: dict) -> str:
    """给模型看的一句话结论，比让它自己去读 JSON 里四个数字更省事。"""
    outcome = result.get("outcome")
    ok = result.get("parts_written") or 0
    nosub = result.get("no_subtitle") or 0
    fail = result.get("failed") or 0
    head = {
        "complete": "全部成功",
        "partial": "部分成功",
        "empty": "没有拿到字幕",
        "failed": "全部失败",
    }.get(outcome, "完成")
    line = "%s：取到 %d 段，%d 段本来没字幕，%d 处失败。" % (head, ok, nosub, fail)
    files = result.get("files") or []
    if files:
        line += "\n输出目录 %s（%d 个文件）。" % (result.get("outdir"), len(files))
    dropped = result.get("parts_truncated") or 0
    if dropped:
        capped = [x for x in (result.get("items") or []) if x.get("parts_capped")]
        detail = "；".join(
            "%s 共 %d P 只取了 %d 个" % (x["bvid"], x["parts_capped"]["total"],
                                       x["parts_capped"]["taken"])
            for x in capped[:3])
        line += ("\n**有分P没取**：%s。要全取请传 max_parts=0（或分批用 "
                 "asr-polish subs）。" % (detail or "共 %d 个分P未取" % dropped))
    items = result.get("failed_items") or []
    if items:
        line += "\n失败明细：" + "；".join(
            "%s%s %s" % (x.get("bvid"), "#%s" % x["page"] if x.get("page") else "",
                         x.get("error", "")[:60])
            for x in items[:5])
        more = len(items) - 5
        if more > 0:
            line += "（还有 %d 条）" % more
        retry = [x["retry"] for x in items if x.get("retry")]
        if retry:
            line += ("\n可以只重跑这些，不用整批重来：fetch_subtitles(target=…, "
                     "only=%s)" % json.dumps(retry[:20], ensure_ascii=False))
    return line


def _tool_fetch(args: dict, progress: Callable[[dict], None]) -> tuple:
    payload = {
        "target": args.get("target") or "",
        "outdir": args.get("outdir") or "subs",
        "formats": args.get("formats") or "txt",
        "limit": args.get("limit"),
        "pages": args.get("pages") or "all",
        # 多P合集在这里最容易变成「看起来一条、实际几十条」。默认压到 20，
        # 传 0 才放开——截断了会在返回里明说，不静默。
        "max_parts": args.get("max_parts", 20),
        "asr": bool(args.get("asr")),
        "correct": bool(args.get("correct")),
        "only": args.get("only"),
    }
    try:
        result = run_subs(payload, progress)
    except _JobFailure as e:                       # 全失败：仍有明细，照样返回
        result = e.result or {}
        return _summary_text(result) + "\n\n" + str(e), _trim(result), True
    return _summary_text(result), _trim(result), False


#: 返回给模型的 JSON 里最多列几个文件路径。全列出来会把上下文吃满，
#: 而模型真正需要的是「东西在哪」这个目录，不是一个 166 行的清单。
_MAX_FILES_IN_BODY = 20


def _trim(result: dict) -> dict:
    """把结果压到「模型够用、上下文不炸」的大小。"""
    out = dict(result)
    files = out.get("files") or []
    if len(files) > _MAX_FILES_IN_BODY:
        out["files"] = files[:_MAX_FILES_IN_BODY]
        out["files_total"] = len(files)
        out["files_note"] = ("只列了前 %d 个，全部 %d 个都在 outdir 里，"
                             "用 basename 就能对上" % (_MAX_FILES_IN_BODY, len(files)))
    out.pop("items", None)                         # 逐视频明细里有 parts_capped，保留摘要即可
    return out


def _tool_list(args: dict, progress: Callable[[dict], None]) -> tuple:
    target = (args.get("target") or "").strip()
    limit = int(args.get("limit") or 20)
    low = target.lower()

    if low.startswith("up:"):
        mid = extract_mid(target[3:])
        if not mid:
            raise ValueError("认不出 UP 主 mid：%s" % target)
        rows = list_up_videos(mid, max_count=limit)
    elif low.startswith("season:"):
        parts = target.split(":")
        if len(parts) != 3:
            raise ValueError("合集写法应为 season:<mid>:<season_id>")
        rows = list_season(int(parts[1]), int(parts[2]), max_count=limit)
    elif low.startswith("search:"):
        rows = search_videos(target.split(":", 1)[1].strip(),
                             pages=int(args.get("pages") or 1))
        rows = rows[:limit]
    else:
        raise ValueError("list_videos 需要一个 up: / season: / search: 开头的目标；"
                         "单个视频用 fetch_subtitles（这里是 BV 号）")

    body = {
        "count": len(rows),
        "videos": [{"bvid": r.get("bvid"), "title": r.get("title"),
                    "duration": r.get("duration")} for r in rows],
    }
    head = "共 %d 条。" % len(rows)
    if len(rows) > 10:
        head += "一次全抓容易触发风控，建议先取前 10 条。"
    return head, body, False


def _tool_correct(args: dict, progress: Callable[[dict], None]) -> tuple:
    text = args.get("text") or ""
    res = _correct(text)
    body = {
        "text": res.text,
        "count": res.count,
        "corrections": [
            {"original": c.original, "corrected": c.corrected,
             "confidence": c.confidence, "category": c.category}
            for c in res.corrections
        ],
    }
    return res.summary(), body, False


def _tool_check_login(args: dict, progress: Callable[[dict], None]) -> tuple:
    st = cookie_status(verify=True)
    level = st.get("level")
    head = {"ok": "Cookie 可用。",
            "warn": "Cookie 快过期了，建议尽快换。",
            "error": "Cookie 不可用。"}.get(level, "Cookie 状态未知。")
    if st.get("note"):
        head += "\n" + str(st["note"])
    return head, st, level == "error"


_TOOL_IMPL = {
    "fetch_subtitles": _tool_fetch,
    "list_videos": _tool_list,
    "correct_text": _tool_correct,
    "check_login": _tool_check_login,
}


# ── JSON-RPC 层 ────────────────────────────────────────────────


def _write(stream, obj: dict) -> None:
    """写一条消息。**必须**单行 + 立即 flush：stdio 传输是按行读的。"""
    stream.write(json.dumps(obj, ensure_ascii=False) + "\n")
    stream.flush()


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": code, "message": message}}


def _ok(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _content(text: str, body) -> list:
    """两个内容块：一句人话 + 一份 JSON。

    只给 JSON 的话模型容易漏掉「为什么失败」这类只有人能读懂的信息；
    只给一句话的话，它又要去猜文件在哪。
    """
    blocks = [{"type": "text", "text": text}]
    if body is not None:
        blocks.append({"type": "text", "text": _dump(body)})
    return blocks


def _handle_call(msg: dict, emit: Callable[[dict], None]) -> dict:
    mid = msg.get("id")
    params = msg.get("params") or {}
    name = params.get("name")
    args = params.get("arguments") or {}
    meta = params.get("_meta") or {}
    token = meta.get("progressToken")

    impl = _TOOL_IMPL.get(name)
    if impl is None:
        return _error(mid, -32602, "没有这个工具：%s" % name)

    last_sent = [0.0]

    def progress(frac: float, note: str) -> None:
        if token is None:
            return
        now = time.monotonic()
        if now - last_sent[0] < _PROGRESS_INTERVAL and frac < 1.0:
            return
        last_sent[0] = now
        emit({"jsonrpc": "2.0", "method": "notifications/progress",
              "params": {"progressToken": token,
                         "progress": round(max(0.0, min(1.0, float(frac))), 3),
                         "total": 1.0, "message": note}})

    try:
        head, body, is_error = impl(args, progress)
        return _ok(mid, {"content": _content(head, body), "isError": bool(is_error)})
    except Exception as e:                          # noqa: BLE001 —— 工具错误不该杀 server
        text = "%s: %s" % (type(e).__name__, e)
        if isinstance(e, (BiliError, BiliRiskControl)):
            # 这两类是可预期错误，把原话给模型，它才好向用户解释该怎么处理
            text = str(e)
        return _ok(mid, {"content": _content(text, None), "isError": True})


def _handle(msg: dict, emit: Callable[[dict], None]) -> Optional[dict]:
    """返回响应；返回 None 表示这是通知，不该回。"""
    method = msg.get("method")
    mid = msg.get("id")
    is_notification = "id" not in msg

    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion")
        version = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSION
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": (
                "B站字幕抓取。抓单个视频用 fetch_subtitles(target=\"BV…\")；"
                "要批量先 list_videos 看清单再决定，一次别超过 10 条——"
                "B站按 IP/账号记风控，打太密会连坐。"
            ),
        }
        return None if is_notification else _ok(mid, result)

    if is_notification:
        # notifications/initialized、cancelled 等：按规范一律不回。
        return None

    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": _TOOLS})
    if method == "tools/call":
        return _handle_call(msg, emit)
    if method == "prompts/list":
        return _ok(mid, {"prompts": []})
    if method == "resources/list":
        return _ok(mid, {"resources": []})
    return _error(mid, -32601, "不支持的方法：%s" % method)


def serve(stdin=None, stdout=None, stderr=None) -> None:
    """读 stdin 的行、把响应写 stdout，直到 stdin 关闭。

    故意不捕获 ``KeyboardInterrupt`` 之外的任何东西：协议解析失败只回一条
    错误消息然后继续读下一行——一条坏消息不该让整个 server 退出。
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr

    def emit(obj: dict) -> None:
        _write(stdout, obj)

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            _write(stdout, _error(None, -32700, "不是合法 JSON：%s" % e))
            continue
        if not isinstance(msg, dict):
            _write(stdout, _error(None, -32600, "期望一个 JSON 对象"))
            continue
        try:
            reply = _handle(msg, emit)
        except Exception as e:                      # noqa: BLE001
            # 走到这里说明是我们自己的 bug。回一条错误让对端知道这次调用
            # 失败了，但进程要活着——否则用户只看到「server 挂了」。
            reply = _error(msg.get("id"), -32603, "内部错误：%s: %s"
                           % (type(e).__name__, e))
            print("asr-polish mcp: %s" % reply["error"]["message"], file=stderr)
        if reply is not None:
            emit(reply)


def main(argv: Optional[list] = None) -> int:
    from .cli import _prepare_stdio

    _prepare_stdio()
    try:
        serve()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
