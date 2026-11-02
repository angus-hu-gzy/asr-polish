# -*- coding: utf-8 -*-
"""采集「真实 ASR 字幕」语料，用于跑召回基准（tools/benchmark_recall.py）。

只收 `lan == ai-zh` 的字幕轨：人工字幕文本本身是对的，混进来会污染召回统计
（拿干净文本当"待改"输入，等于自欺）。多 P 合集取单集时长最接近 20 分钟的那一 P，
避免只拿到几十字的预告。

语料不进仓库，原因两条：体积（百万字纯文本）、版权（内容是别人的视频字幕）。
所以这里只保留**采集口径**，语料在本地生成。

用法：
    # 按关键词搜索采集（推荐）
    python tools/collect_corpus.py -o ./corpus_asr --keywords "Python教程,算法讲解"

    # 用已有的候选清单（[{bvid:...}, ...] 或 ["BV...", ...]）
    python tools/collect_corpus.py -o ./corpus_asr --candidates bench/bv_candidates.json

    # 只看会抓哪些、不落盘
    python tools/collect_corpus.py --keywords "Python教程" --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asr_polish.bili import (  # noqa: E402
    BiliError, fetch, get_video_info, search_videos,
)

DEFAULT_KEYWORDS = [
    "Python教程", "Java后端", "算法讲解", "数据库原理", "大模型原理",
    "深度学习", "前端开发", "微服务架构", "Docker容器", "Linux运维",
    "数据结构", "网络爬虫", "网络安全", "Rust编程", "Go语言",
    "机器学习", "数据分析", "Redis", "MySQL优化", "Kubernetes",
    "Git使用", "操作系统原理", "计算机网络", "云原生", "软件测试",
    "分布式系统", "编译原理", "Python数据分析", "架构设计", "程序员",
]

IDEAL_SEC = 1200      # 理想单集时长：20 分钟
MIN_SEC, MAX_SEC = 180, 5400
MIN_CHARS = 400


def pick_page(info: dict) -> int:
    """多 P 里挑时长最接近 IDEAL_SEC 的一 P。"""
    pages = info.get("pages") or []
    if len(pages) <= 1:
        return 1
    best, best_gap = 1, float("inf")
    for i, p in enumerate(pages, 1):
        dur = p.get("duration") or 0
        if MIN_SEC <= dur <= MAX_SEC:
            gap = abs(dur - IDEAL_SEC)
            if gap < best_gap:
                best, best_gap = i, gap
    return best


def load_candidates(args) -> list[dict]:
    """候选来源：显式清单优先，否则按关键词搜。"""
    if args.candidates:
        raw = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
        out = []
        for item in raw:
            if isinstance(item, str):
                out.append({"bvid": item, "kw": ""})
            elif isinstance(item, dict) and item.get("bvid"):
                out.append({"bvid": item["bvid"], "kw": item.get("kw", "")})
        return out

    keywords = ([x.strip() for x in args.keywords.split(",") if x.strip()]
                if args.keywords else DEFAULT_KEYWORDS)
    out, seen = [], set()
    for kw in keywords:
        try:
            found = search_videos(kw, args.cookie, pages=args.search_pages)
        except BiliError as e:
            print(f"  {kw}: {e}", file=sys.stderr)
            continue
        new = 0
        for v in found:
            if v["bvid"] in seen:
                continue
            seen.add(v["bvid"])
            out.append({"bvid": v["bvid"], "kw": kw})
            new += 1
        print(f"  {kw}: +{new}（累计 {len(out)}）", file=sys.stderr)
        time.sleep(args.delay)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="采集 B站 AI 字幕（ASR 原始输出）作为评测语料")
    ap.add_argument("-o", "--out", default="./corpus_asr", help="语料目录")
    ap.add_argument("--keywords", help="逗号分隔的关键词；默认 30 个技术词")
    ap.add_argument("--search-pages", type=int, default=1,
                    help="每个关键词搜几页（每页约 42 条）")
    ap.add_argument("--candidates", help="候选清单 JSON（bvid 列表或带 kw 的对象列表）")
    ap.add_argument("--max-items", type=int, default=200, help="最多收几篇（默认 200）")
    ap.add_argument("--delay", type=float, default=0.6, help="请求间隔秒数（默认 0.6）")
    ap.add_argument("--cookie", default=None, help="cookies.txt 路径（默认自动查找）")
    ap.add_argument("--dry-run", action="store_true", help="只列出候选，不抓字幕")
    args = ap.parse_args(argv)

    outdir = Path(args.out)
    manifest_path = outdir / "manifest.json"

    print("收集候选…", file=sys.stderr)
    cands = load_candidates(args)
    if not cands:
        print("没有候选，退出", file=sys.stderr)
        return 1
    print(f"候选 {len(cands)} 个", file=sys.stderr)

    manifest = []
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    done = {m["bvid"] for m in manifest}

    if args.dry_run:
        for c in cands[:20]:
            print(f"  {c['bvid']}  {c['kw']}")
        print(f"…共 {len(cands)} 个（--dry-run 不落盘）", file=sys.stderr)
        return 0

    outdir.mkdir(parents=True, exist_ok=True)
    stat = {"no_sub": 0, "human_sub": 0, "short": 0, "err": 0, "skip": 0}
    kept = 0

    for i, c in enumerate(cands, 1):
        bvid = c["bvid"]
        if bvid in done:
            stat["skip"] += 1
            continue
        if len(manifest) >= args.max_items:
            break
        try:
            info = get_video_info(bvid, args.cookie)
            page = pick_page(info)
            r = fetch(bvid, cookie_path=args.cookie, page=page, allow_asr=False)
        except BiliError as e:
            # 无字幕时 fetch 会抛带 yt-dlp 字样的错误，这类记 no_sub
            stat["no_sub" if "yt-dlp" in str(e) else "err"] += 1
            continue
        except Exception:
            stat["err"] += 1
            continue

        if r.source != "字幕轨":
            stat["no_sub"] += 1
            continue
        if r.subtitle_lan != "ai-zh":
            stat["human_sub"] += 1       # 人工轨：文本本身是对的，不能当语料
            continue
        if r.duration < MIN_SEC or len(r.text) < MIN_CHARS:
            stat["short"] += 1
            continue

        name = f"{len(manifest):03d}-{bvid}.txt"
        (outdir / name).write_text(r.text, encoding="utf-8")
        manifest.append({
            "file": name, "bvid": bvid, "title": r.title, "owner": r.owner,
            "duration": r.duration, "page": r.page, "chars": len(r.text),
            "lan": r.subtitle_lan, "kw": c.get("kw", ""),
        })
        kept += 1
        if kept % 5 == 0:
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[{i}/{len(cands)}] 已收 {len(manifest)} 篇 {stat}", file=sys.stderr)
        time.sleep(args.delay)

    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    total_chars = sum(m["chars"] for m in manifest)
    print(f"完成：{len(manifest)} 篇 / {total_chars} 字 {stat}", file=sys.stderr)
    print(f"清单：{manifest_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
