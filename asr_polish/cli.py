"""asr-polish 命令行入口。

用法：
    asr-polish correct "今天讲色考和PASS"           # 术语订正
    asr-polish assess "转写文本" --duration 1316    # 质量打分
    asr-polish crosscheck a.txt b.txt               # 交叉验证两个转写
    asr-polish lexicon import 词表.txt             # 批量导入词表
    asr-polish lexicon list                        # 列出词表
    asr-polish lexicon export --out out.json       # 导出词表
    asr-polish fetch <B站链接>                     # 从 B站视频取文本（字幕轨→必剪ASR）
    asr-polish subs <链接> -f txt,srt -o ./subs    # 批量抓字幕导出文件
    asr-polish subs --up 12345 --limit 20          # 抓 UP 主最新 20 条
    asr-polish subs --search "Python教程" --limit 10  # 搜关键词再抓
    asr-polish serve --port 8040                   # 起 HTTP 服务 + 任务队列
    asr-polish submit up:12345 --wait              # 把任务交给服务去跑
    asr-polish status                              # 看队列和最近的任务
    asr-polish version                             # 显示版本

核心能力纯标准库实现（argparse），无第三方依赖；
仅 fetch 命令在「无字幕 → 必剪 ASR」时需要可选依赖 yt-dlp。
"""
from __future__ import annotations

import argparse
import sys
import unicodedata
import warnings
from pathlib import Path
from typing import Optional

from . import __version__
from .correction import correct_text
from .crosscheck import cross_check, confidence_report
from .quality import assess
from .lexicon import Lexicon, DEFAULT_LEXICON_PATH, USER_LEXICON_PATH
from . import lexicon_io

# 任务状态的展示名。API 里是英文常量，给人看的一律用中文，
# 免得同一个输出里表头是「状态」、值是 done。
_STATE_LABEL = {
    "pending": "排队",
    "running": "进行中",
    "done": "完成",
    "failed": "失败",
}


def _pad(text: str, width: int) -> str:
    """按显示宽度补齐。

    中文在终端里占两列，`%-10s` 按字符数算，会导致中文列整体左偏。
    """
    shown = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(0, width - shown)


def _cmd_correct(args: argparse.Namespace) -> None:
    result = correct_text(args.text)
    print(result.text)
    if args.verbose:
        print("\n" + result.summary(), file=sys.stderr)


def _cmd_assess(args: argparse.Namespace) -> None:
    report = assess(args.text, duration_seconds=args.duration)
    print(report.summary())


def _cmd_crosscheck(args: argparse.Namespace) -> None:
    a = Path(args.file1).read_text(encoding="utf-8")
    b = Path(args.file2).read_text(encoding="utf-8")
    print(confidence_report(cross_check(a, b)))


def _cmd_lexicon_import(args: argparse.Namespace) -> None:
    incoming = lexicon_io.load_entries_from_file(args.file, args.format)
    if not incoming:
        print(f"未从 {args.file} 解析到任何有效条目", file=sys.stderr)
        sys.exit(1)
    out = args.out or str(USER_LEXICON_PATH)
    added, merged = lexicon_io.import_into_lexicon_file(out, incoming)
    total_in_file = len(lexicon_io.load_lexicon_data(out).get("entries", []))
    print(f"解析到 {len(incoming)} 条 → 新增 {added} 条，合并 {merged} 条")
    print(f"词表文件现有 {total_in_file} 条，已写入：{out}")
    if not args.out:
        # 默认写入用户词表时，运行时按「内置词表 + 用户词表」合并后生效。
        # 统计条数时抑制告警，避免用户词表损坏的 warning 混进「导入成功」的输出。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            print(f"（合并内置词表后共 {len(Lexicon().entries)} 条，运行时按此生效）")


def _cmd_lexicon_export(args: argparse.Namespace) -> None:
    out = lexicon_io.export_lexicon(DEFAULT_LEXICON_PATH, args.out or "corrections.json")
    print(f"词表已导出到 {out}")


def _cmd_lexicon_list(args: argparse.Namespace) -> None:
    lex = Lexicon()
    print(f"共 {len(lex.entries)} 条，{len(lex.categories())} 个类别：{lex.categories()}\n")
    for i, e in enumerate(lex.entries, 1):
        variants = "；".join(e.get("variants", []))
        print(f"{i:>3}. {e['correct']:<20} [{e.get('category', '未分类')}]  {variants}")


def _cmd_fetch(args: argparse.Namespace) -> None:
    from .bili import fetch, BiliError

    try:
        result = fetch(args.url, cookie_path=args.cookie, page=args.page)
    except BiliError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)

    print(f"标题：{result.title}", file=sys.stderr)
    print(f"UP主：{result.owner}", file=sys.stderr)
    print(f"时长：{result.duration // 60}分{result.duration % 60}秒  "
          f"来源：{result.source}", file=sys.stderr)
    for w in result.warnings:
        print(f"提示：{w}", file=sys.stderr)

    text = result.text
    if not args.no_correct:
        cr = correct_text(text)
        text = cr.text
        if args.verbose:
            print(cr.summary(), file=sys.stderr)
    if not args.no_assess:
        print(file=sys.stderr)
        print(assess(text, duration_seconds=result.duration).summary(), file=sys.stderr)

    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"文本已写入：{args.out}", file=sys.stderr)
    else:
        print(text)


def _cmd_subs(args: argparse.Namespace) -> None:
    """批量抓字幕到文件：单视频 / 多 P / UP 主 / 合集。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from . import export
    from .bili import (BiliError, expand_pages, extract_bvid, extract_mid,
                       fetch, get_video_info, list_season, list_up_videos,
                       search_videos)

    try:
        formats = export.parse_formats(args.format)
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(2)

    targets: list[str] = []
    try:
        if args.up:
            mid = extract_mid(args.up)
            if not mid:
                print(f"错误：无法识别 UP 主 mid：{args.up}", file=sys.stderr)
                sys.exit(2)
            targets = [x["bvid"] for x in
                       list_up_videos(mid, args.cookie, args.limit)]
            print(f"UP {mid}：发现 {len(targets)} 个投稿", file=sys.stderr)
        elif args.season:
            mid, sid = args.season
            targets = [x["bvid"] for x in
                       list_season(int(mid), int(sid), args.cookie, args.limit)]
            print(f"合集 {sid}：发现 {len(targets)} 个视频", file=sys.stderr)
        elif args.search:
            found = search_videos(args.search, args.cookie, args.search_pages)
            targets = [x["bvid"] for x in found]
            print(f"搜索「{args.search}」：发现 {len(targets)} 个视频",
                  file=sys.stderr)
        else:
            for t in args.targets:
                bv = extract_bvid(t)
                if bv:
                    targets.append(bv)
                else:
                    print(f"跳过无法识别：{t}", file=sys.stderr)
    except (BiliError, ValueError) as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)

    if not targets:
        print("没有可处理的视频", file=sys.stderr)
        sys.exit(2)

    if args.limit and len(targets) > args.limit:
        # --up / --season 已在取列表时截断，--search 与显式 targets 在这里截
        targets = targets[:args.limit]

    outdir = Path(args.out)
    want: Optional[set] = None
    if args.pages.strip().lower() != "all":
        want = {int(x) for x in args.pages.split(",") if x.strip().isdigit()}

    def grab(bvid: str) -> dict:
        row = {"bvid": bvid, "title": "", "ok": 0, "nosub": 0, "fail": 0,
               "err": ""}
        try:
            info = get_video_info(bvid, cookie_path=args.cookie)
        except BiliError as e:
            row["fail"], row["err"] = 1, str(e)
            return row
        row["title"] = info["title"]
        total_pages = len(info.get("pages") or []) or 1
        pages = expand_pages(info)
        if want is not None:
            pages = [p for p in pages if p["index"] in want]
        for p in pages:
            try:
                res = fetch(bvid, cookie_path=args.cookie, page=p["index"],
                            allow_asr=args.asr)
            except BiliError as e:
                row["fail"] += 1
                row["err"] = str(e)
                continue
            if not res.segments:
                row["nosub"] += 1
                continue
            if args.correct:
                fixed = [correct_text(s["text"]).text for s in res.segments]
                for seg, txt in zip(res.segments, fixed):
                    seg["text"] = txt
                res.text = "\n".join(fixed)
            part = p["title"] if total_pages > 1 else ""
            stem = export.base_name(res, p["title"], total_pages)
            export.write_all(outdir, stem, formats, res, part,
                             skip_existing=args.skip_existing)
            row["ok"] += 1
        return row

    def show(i: int, total: int, row: dict) -> None:
        if args.quiet:
            return
        if row["ok"]:
            mark = "✓"
        elif row["fail"]:
            mark = "✗"
        else:
            mark = "○"
        extra = (f"{row['ok']} 段" if row["ok"]
                 else (row["err"][:60] or "无字幕轨"))
        print(f"[{i}/{total}] {mark} {row['bvid']} {row['title'][:34]}  {extra}",
              file=sys.stderr, flush=True)

    rows: list[dict] = []
    total = len(targets)
    if args.jobs > 1 and total > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futs = {pool.submit(grab, bv): bv for bv in targets}
            for i, fut in enumerate(as_completed(futs), 1):
                row = fut.result()
                rows.append(row)
                show(i, total, row)
    else:
        for i, bv in enumerate(targets, 1):
            row = grab(bv)
            rows.append(row)
            show(i, total, row)

    ok = sum(r["ok"] for r in rows)
    nosub = sum(r["nosub"] for r in rows)
    fail = sum(r["fail"] for r in rows)
    done = sum(1 for r in rows if r["ok"])
    print(f"\n视频 {total} 个：有字幕 {done}，无字幕 {nosub} 段，失败 {fail} 次",
          file=sys.stderr)
    print(f"输出目录：{outdir.resolve()}", file=sys.stderr)
    if fail and not ok:
        sys.exit(2)


def _api(server: str, path: str, payload: Optional[dict] = None,
         timeout: float = 20.0) -> dict:
    """调用本地服务。只依赖 urllib，保持零依赖。"""
    import json
    import urllib.error
    import urllib.request

    url = server.rstrip("/") + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(body).get("error") or body
        except ValueError:
            msg = body
        raise SystemExit(f"服务返回 {e.code}：{msg}")
    except urllib.error.URLError as e:
        raise SystemExit(f"连不上 {server}（{e.reason}）。先跑 asr-polish serve")


def _cmd_serve(args: argparse.Namespace) -> None:
    from .server import serve
    serve(host=args.host, port=args.port, db=args.db,
          workers=args.workers, stale_after=args.stale_after)


def _cmd_submit(args: argparse.Namespace) -> None:
    import json
    import time

    payload = {
        "kind": "subs",
        "target": args.target,
        "formats": [x.strip() for x in args.format.split(",") if x.strip()],
        "outdir": args.out,
        "asr": args.asr,
        "correct": args.correct,
    }
    if args.limit:
        payload["limit"] = args.limit
    if args.pages.strip().lower() != "all":
        payload["pages"] = args.pages

    task = _api(args.server, "/api/tasks", payload)
    print(f"已提交 {task['id']}（{task['status']}）", file=sys.stderr)
    if not args.wait:
        print(task["id"])
        return

    seen = -1
    while True:
        t = _api(args.server, "/api/tasks/" + task["id"])
        pct = int(t["progress"] * 100)
        if not args.quiet and pct != seen:
            label = _STATE_LABEL.get(t["status"], t["status"])
            print(f"  {pct:3d}%  {label:<6} {t['message'][:56]}",
                  file=sys.stderr, flush=True)
            seen = pct
        if t["status"] in ("done", "failed"):
            break
        time.sleep(1.0)

    if t["status"] == "failed":
        print(f"失败：{t['error']}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(t["result"], ensure_ascii=False, indent=2))


def _cmd_status(args: argparse.Namespace) -> None:
    import json

    if args.task_id:
        print(json.dumps(_api(args.server, "/api/tasks/" + args.task_id),
                         ensure_ascii=False, indent=2))
        return

    q = f"/api/tasks?limit={args.limit}"
    if args.state:
        q += "&status=" + args.state
    d = _api(args.server, q)
    s = d["stats"]
    print(f"排队 {s['pending']}  进行中 {s['running']}  "
          f"完成 {s['done']}  失败 {s['failed']}  共 {s['total']}")
    if not d["tasks"]:
        return
    print(_pad("任务", 14) + _pad("状态", 10) + _pad("进度", 7) + "目标")
    for t in d["tasks"]:
        target = (t.get("payload") or {}).get("target") or ""
        label = _STATE_LABEL.get(t["status"], t["status"])
        pct = "%4.0f%%" % (t["progress"] * 100)
        print(_pad(t["id"], 14) + _pad(label, 10) + _pad(pct, 7) + target[:44])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asr-polish",
        description="中文语音转写（ASR）质量打磨工具：术语订正 / 交叉验证 / 质量自评 / 词表管理",
    )
    parser.add_argument("--version", action="version", version=f"asr-polish {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_correct = sub.add_parser("correct", help="订正一段文本的术语错字")
    p_correct.add_argument("text", help="待订正的转写文本")
    p_correct.add_argument("-v", "--verbose", action="store_true", help="在 stderr 输出订正明细")
    p_correct.set_defaults(func=_cmd_correct)

    p_assess = sub.add_parser("assess", help="给一段转写文本打分（0~100）")
    p_assess.add_argument("text", help="待评估的转写文本")
    p_assess.add_argument("--duration", type=float, default=None, help="音频时长（秒），用于计算字符密度")
    p_assess.set_defaults(func=_cmd_assess)

    p_cc = sub.add_parser("crosscheck", help="交叉验证两个转写文本文件")
    p_cc.add_argument("file1", help="基准转写文本文件")
    p_cc.add_argument("file2", help="另一引擎的转写文本文件")
    p_cc.set_defaults(func=_cmd_crosscheck)

    p_lex = sub.add_parser("lexicon", help="词表管理")
    lex_sub = p_lex.add_subparsers(dest="lex_cmd")

    p_imp = lex_sub.add_parser("import", help="从文件批量导入词表（JSON/CSV/TXT）")
    p_imp.add_argument("file", help="待导入的文件路径")
    # 注意：argparse 会对 help 做「help % params」格式化，字面 % 必须写成 %%
    p_imp.add_argument("--out", default=None, help="输出词表文件（默认写入用户词表：Windows 为 %%APPDATA%%/asr-polish，Linux/macOS 为 $XDG_CONFIG_HOME 或 ~/.config）")
    p_imp.add_argument("--format", choices=["auto", "json", "csv", "txt"], default="auto", help="输入格式（默认按扩展名/内容自动识别）")
    p_imp.set_defaults(func=_cmd_lexicon_import)

    p_exp = lex_sub.add_parser("export", help="导出当前内置词表为 JSON")
    p_exp.add_argument("--out", default=None, help="输出文件路径（默认 corrections.json）")
    p_exp.set_defaults(func=_cmd_lexicon_export)

    p_ls = lex_sub.add_parser("list", help="列出内置词表全部条目")
    p_ls.set_defaults(func=_cmd_lexicon_list)

    p_fetch = sub.add_parser("fetch", help="从 B站视频链接获取文本（字幕轨优先，无字幕走必剪 ASR）")
    p_fetch.add_argument("url", help="B站视频链接或 bvid")
    p_fetch.add_argument("--cookie", default=None, help="cookies.txt 路径（可选，用于取 AI 字幕）")
    p_fetch.add_argument("-p", "--page", type=int, default=1,
                         help="多 P 视频取第几分 P（默认 1）")
    p_fetch.add_argument("--no-correct", action="store_true", help="不做术语订正")
    p_fetch.add_argument("--no-assess", action="store_true", help="不做质量打分")
    p_fetch.add_argument("-v", "--verbose", action="store_true", help="输出订正明细")
    p_fetch.add_argument("--out", default=None, help="把文本写入文件而非 stdout")
    p_fetch.set_defaults(func=_cmd_fetch)

    p_subs = sub.add_parser(
        "subs", help="批量抓 B站字幕并导出文件（txt/srt/json/md）")
    p_subs.add_argument("targets", nargs="*", metavar="链接或BV号",
                        help="视频链接或 bvid，可多个")
    p_subs.add_argument("--up", metavar="MID",
                        help="抓该 UP 主的全部投稿（mid 或空间链接）")
    p_subs.add_argument("--season", nargs=2, metavar=("MID", "SID"),
                        help="抓合集/系列：--season <mid> <season_id>")
    p_subs.add_argument("--search", metavar="关键词",
                        help="按关键词搜视频后抓（与 --limit 搭配使用）")
    p_subs.add_argument("--search-pages", type=int, default=1,
                        help="搜索取前几页，每页约 42 条（默认 1）")
    p_subs.add_argument("--limit", type=int, default=0,
                        help="批量模式最多抓几个视频（0 = 不限）")
    p_subs.add_argument("-o", "--out", default="./subs",
                        help="输出目录（默认 ./subs）")
    p_subs.add_argument("-f", "--format", default="txt",
                        help="导出格式，逗号分隔：txt,srt,json,md（默认 txt）")
    p_subs.add_argument("--pages", default="all",
                        help="多 P 视频取哪几集，如 1,2,5 或 all（默认 all）")
    p_subs.add_argument("-j", "--jobs", type=int, default=3,
                        help="并发数（默认 3，过高易触发风控）")
    p_subs.add_argument("--cookie", default=None,
                        help="cookies.txt 路径（默认自动查找）")
    p_subs.add_argument("--asr", action="store_true",
                        help="无字幕时上传音频走必剪 ASR 兜底（慢，默认关闭）")
    p_subs.add_argument("--correct", action="store_true",
                        help="对抓到的文本做术语订正（默认保留字幕原文）")
    p_subs.add_argument("--skip-existing", action="store_true",
                        help="输出文件已存在则跳过（增量抓取）")
    p_subs.add_argument("-q", "--quiet", action="store_true", help="不打印逐条进度")
    p_subs.set_defaults(func=_cmd_subs)

    p_serve = sub.add_parser(
        "serve", help="启动 HTTP 服务 + 任务队列（零依赖，长任务走异步）")
    p_serve.add_argument("--host", default="127.0.0.1",
                         help="监听地址（默认只监听本机，对外请自己套反向代理）")
    p_serve.add_argument("-p", "--port", type=int, default=8040,
                         help="端口（默认 8040）")
    p_serve.add_argument("--db", default="tasks.db",
                         help="任务库路径（默认 tasks.db）")
    p_serve.add_argument("-w", "--workers", type=int, default=2,
                         help="worker 数量（默认 2）")
    p_serve.add_argument("--stale-after", type=float, default=600.0,
                         help="任务多久没心跳就判定僵尸并回收，单位秒（默认 600）")
    p_serve.set_defaults(func=_cmd_serve)

    p_submit = sub.add_parser(
        "submit", help="把抓取任务提交给运行中的服务（不进命令行直接跑）")
    p_submit.add_argument("target",
                          help="BV 号/链接，或 up:123、season:mid:sid、search:关键词")
    p_submit.add_argument("--server", default="http://127.0.0.1:8040",
                          help="服务地址（默认 http://127.0.0.1:8040）")
    p_submit.add_argument("-o", "--out", default="./subs", help="输出目录")
    p_submit.add_argument("-f", "--format", default="txt",
                          help="导出格式，逗号分隔（默认 txt）")
    p_submit.add_argument("--limit", type=int, default=0,
                          help="批量模式最多几个视频（0 = 不限）")
    p_submit.add_argument("--pages", default="all",
                          help="多 P 视频取哪几集，如 1,2,5 或 all")
    p_submit.add_argument("--asr", action="store_true",
                          help="无字幕时走必剪 ASR 兜底")
    p_submit.add_argument("--correct", action="store_true",
                          help="对文本做术语订正（默认保留原文）")
    p_submit.add_argument("--wait", action="store_true",
                          help="阻塞到任务结束，打印进度与结果")
    p_submit.add_argument("-q", "--quiet", action="store_true",
                          help="不打印进度")
    p_submit.set_defaults(func=_cmd_submit)

    p_status = sub.add_parser(
        "status", help="查看任务状态（不给 id 就列最近的）")
    p_status.add_argument("task_id", nargs="?", default=None, help="任务 id")
    p_status.add_argument("--server", default="http://127.0.0.1:8040",
                          help="服务地址（默认 http://127.0.0.1:8040）")
    p_status.add_argument("--state", default=None,
                          choices=["pending", "running", "done", "failed"],
                          help="只看某个状态的")
    p_status.add_argument("--limit", type=int, default=15, help="列多少条")
    p_status.set_defaults(func=_cmd_status)

    return parser


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
