"""用「同一视频的人工字幕 vs AI 字幕」评估真实转写质量，并测量订正器的实际覆盖率。

为什么需要它：`eval/*.jsonl` 是手写用例，`benchmark_recall.py` 是人造扰动——
两者都只能回答「给定这个错法能不能改对」。真实问题是「字幕里到底错了多少、
我的工具改到了几成」，只有拿人工字幕当参照系才测得出来。

用法：
    python tools/eval_against_human.py --bvid BV1Sz4y1U77N
    python tools/eval_against_human.py --list ../bench/human_sub_pairs.json
    python tools/eval_against_human.py --bvid BV... --show 30 --json out.json

需要本机 B 站 Cookie（人工字幕轨通常要求登录）。
"""
import argparse
import difflib
import hashlib
import json
import re
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asr_polish import bili                      # noqa: E402
from asr_polish import correct_text              # noqa: E402

_CLEAN = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]")
_MIXIN: dict[str, str] = {}


def _mixin_key(cookie: str | None) -> str:
    if "k" not in _MIXIN:
        nav = bili._http_json("https://api.bilibili.com/x/web-interface/nav",
                              cookie_path=cookie) or {}
        w = (nav.get("data") or {}).get("wbi_img") or {}
        ik = Path(w.get("img_url") or "").stem
        sk = Path(w.get("sub_url") or "").stem
        if not ik or not sk:
            raise RuntimeError("取不到 wbi key，Cookie 可能已失效")
        _MIXIN["k"] = "".join((ik + sk)[i] for i in bili._WBI_MIXIN_TAB)[:32]
    return _MIXIN["k"]


def subtitle_tracks(video: dict, cookie: str | None) -> list[dict]:
    params = {"bvid": video["bvid"], "aid": video["aid"],
              "cid": video["cid"], "wts": int(time.time())}
    query = urllib.parse.urlencode(sorted(params.items()))
    w_rid = hashlib.md5((query + _mixin_key(cookie)).encode()).hexdigest()
    data = bili._http_json(
        f"https://api.bilibili.com/x/player/wbi/v2?{query}&w_rid={w_rid}",
        cookie_path=cookie)
    return ((data.get("data") or {}).get("subtitle") or {}).get("subtitles") or []


def track_cues(tracks: list[dict], lan: str, cookie: str | None) -> list[tuple]:
    """取一条轨的 (from, to, text)。

    必须保留时间戳：人工字幕常常只覆盖视频的一小段（UP 主只做了样本），
    直接整篇比对会把「一方没有这段」当成错字。
    """
    pick = next((t for t in tracks if t.get("lan") == lan), None)
    if not pick:
        return []
    url = pick["subtitle_url"]
    if url.startswith("//"):
        url = "https:" + url
    body = (bili._http_json(url, cookie_path=cookie) or {}).get("body") or []
    body.sort(key=lambda it: it.get("from", 0))
    return [(float(it.get("from") or 0), float(it.get("to") or 0),
             (it.get("content") or "").strip())
            for it in body if (it.get("content") or "").strip()]


def common_span(ref: list[tuple], hyp: list[tuple]) -> tuple[float, float]:
    """两条轨都有内容的时间区间（取起止的最大/最小）。"""
    return max(ref[0][0], hyp[0][0]), min(ref[-1][1], hyp[-1][1])


def within(cues: list[tuple], lo: float, hi: float, tol: float = 1.0) -> list[tuple]:
    """只保留完整落在 [lo, hi] 内的 cue，两端各留 tol 秒容差。"""
    return [c for c in cues if c[0] >= lo - tol and c[1] <= hi + tol]


def track_lines(tracks: list[dict], lan: str, cookie: str | None) -> list[str]:
    return [t for _, _, t in track_cues(tracks, lan, cookie)]


def _flat(lines: list[str]) -> str:
    return _CLEAN.sub("", "".join(lines))


def _is_han(s: str) -> bool:
    return bool(s) and all("\u4e00" <= c <= "\u9fff" for c in s)


def align(ref: str, hyp: str):
    """字符级对齐，返回 (错字数, ref 长度, 差异块)。"""
    sm = difflib.SequenceMatcher(None, ref, hyp, autojunk=False)
    errs, blocks = 0, []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        errs += max(i2 - i1, j2 - j1)
        blocks.append((tag, ref[i1:i2], hyp[j1:j2]))
    return errs, len(ref), blocks


def classify(blocks) -> Counter:
    """把等长替换分成四类，纯大小写差异不算错字。"""
    c: Counter = Counter()
    for tag, r, h in blocks:
        if tag != "replace":
            c["漏句/多句"] += 1
            continue
        if abs(len(r) - len(h)) > 1:
            c["长度不等"] += 1
        elif _is_han(r) and _is_han(h):
            c["中文同音/近音"] += 1
        elif r.lower() == h.lower():
            c["纯大小写"] += 1
        elif re.search(r"[A-Za-z]", r) or re.search(r"[A-Za-z]", h):
            c["英文拼错/音译"] += 1
        else:
            c["数字符号"] += 1
    return c


def apply_corrections(text: str) -> tuple[str, list]:
    res = correct_text(text)
    out = text
    for c in sorted(res.corrections, key=lambda x: -x.start):
        out = out[:c.start] + c.corrected + out[c.end:]
    return out, res.corrections


def evaluate(bvid: str, human_lan: str, cookie: str | None, show: int) -> dict:
    video = bili.get_video_info(bvid, cookie_path=cookie)
    tracks = subtitle_tracks(video, cookie)
    lans = [t.get("lan") for t in tracks]
    if "ai-zh" not in lans:
        raise RuntimeError(f"{bvid} 没有 AI 中文字幕轨：{lans}")
    if human_lan not in lans:
        # 自动挑人工中文轨：B 站语言码不统一（zh-CN / zh / zh-Hans 都有）
        human_lan = next((l for l in lans
                          if l and not str(l).startswith("ai-")
                          and str(l).lower().startswith("zh")), "")
        if not human_lan:
            raise RuntimeError(f"{bvid} 没有人工中文字幕轨：{lans}")

    ref_cues = track_cues(tracks, human_lan, cookie)
    hyp_cues = track_cues(tracks, "ai-zh", cookie)
    if not ref_cues or not hyp_cues:
        raise RuntimeError(f"{bvid} 字幕轨为空")

    # 只比对两条轨都覆盖的时间段：人工字幕常只做一小段，整篇比会把
    # 「这一方本来就没这段」算成差异。
    lo, hi = common_span(ref_cues, hyp_cues)
    ref_cues = within(ref_cues, lo, hi)
    hyp_cues = within(hyp_cues, lo, hi)
    if not ref_cues or not hyp_cues:
        raise RuntimeError(f"{bvid} 两条轨无重叠时间段（{lo:.0f}~{hi:.0f}s）")

    ref = _flat([t for _, _, t in ref_cues])
    hyp = _flat([t for _, _, t in hyp_cues])
    errs, n, blocks = align(ref, hyp)

    fixed, corrections = apply_corrections(hyp)
    errs_fixed, _, _ = align(ref, fixed)

    row = {
        "bvid": bvid,
        "title": video.get("title", "")[:60],
        "duration": video.get("duration", 0),
        "human_lan": human_lan,
        "chars": n,
        "span": [round(lo), round(hi)],
        "cer": round(errs / n, 4) if n else None,
        "errors": errs,
        "blocks": dict(classify(blocks)),
        "corrections": len(corrections),
        "errors_after": errs_fixed,
        "net_gain": errs - errs_fixed,
        "coverage": round((errs - errs_fixed) / errs, 4) if errs else None,
    }

    print(f"\n{bvid}  {row['title']}")
    print(f"  重叠区间 {lo:.0f}~{hi:.0f}s  人工 {n:,} 字 / AI {len(hyp):,} 字  "
          f"差异 {errs:,} 字 → 字错率 {100 * (row['cer'] or 0):.2f}%")
    print("  差异构成: " + "  ".join(f"{k} {v}" for k, v in row["blocks"].items()))
    print(f"  订正器：改动 {row['corrections']} 处 → 错字 {errs_fixed}，"
          f"净修正 {row['net_gain']:+d}（覆盖率 {100 * (row['coverage'] or 0):.1f}%）")
    for c in corrections:
        print(f"    {c.original} → {c.corrected}")

    if show:
        subs = [(r, h) for tag, r, h in blocks
                if tag == "replace" and abs(len(r) - len(h)) <= 1
                and r.lower() != h.lower()]
        subs = [(r, h) for r, h in subs if r or h]
        seen, shown = set(), 0
        print(f"\n  真实错例（人工 → AI），共 {len(subs)} 处，展示前 {show} 处：")
        for r, h in subs:
            if (r, h) in seen:
                continue
            seen.add((r, h))
            print(f"    {r:<12} → {h}")
            shown += 1
            if shown >= show:
                break
    return row


def main() -> None:
    ap = argparse.ArgumentParser(
        description="用人工字幕评估 AI 字幕真实错字率 + 订正器覆盖率")
    ap.add_argument("--bvid", default=None, help="单个 BV 号")
    ap.add_argument("--human-lan", default="zh-CN",
                    help="人工字幕轨语言码；给错或不给时自动挑第一条非 AI 中文轨")
    ap.add_argument("--list", default=None,
                    help="JSON 清单：[{bvid, human_lan}, ...]")
    ap.add_argument("--show", type=int, default=20, help="展示多少条错例（0 不展示）")
    ap.add_argument("--json", default=None, help="把汇总写成 JSON")
    args = ap.parse_args()

    cookie = next((str(p) for p in bili._DEFAULT_COOKIE_PATHS if p.exists()), None)
    if not cookie:
        print("需要 B 站 Cookie（人工字幕轨要求登录）：", file=sys.stderr)
        print("  " + " / ".join(str(p) for p in bili._DEFAULT_COOKIE_PATHS),
              file=sys.stderr)
        sys.exit(2)

    if args.list:
        items = json.loads(Path(args.list).read_text(encoding="utf-8"))
    elif args.bvid:
        items = [{"bvid": args.bvid, "human_lan": args.human_lan}]
    else:
        ap.error("需要 --bvid 或 --list")

    rows = []
    for it in items:
        try:
            rows.append(evaluate(it["bvid"], it.get("human_lan") or "zh-CN",
                                 cookie, args.show))
        except Exception as e:                  # noqa: BLE001
            print(f"\n{it['bvid']} 跳过：{type(e).__name__} {e}")

    if rows:
        errs = sum(r["errors"] for r in rows)
        chars = sum(r["chars"] for r in rows)
        gain = sum(r["net_gain"] for r in rows)
        print(f"\n{'=' * 62}")
        print(f"合计 {len(rows)} 个视频 / {chars:,} 字")
        if chars:
            print(f"  平均字错率 {100 * errs / chars:.2f}%"
                  f"（{errs:,} 处 / {chars:,} 字）")
            print(f"  订正器净修正 {gain} 字 → 覆盖率 {100 * gain / max(errs, 1):.1f}%")
        agg: Counter = Counter()
        for r in rows:
            agg.update(r["blocks"])
        print("  差异构成合计: " + "  ".join(f"{k} {v}" for k, v in agg.items()))

    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"\n已写入 {args.json}")


if __name__ == "__main__":
    main()
