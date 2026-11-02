"""变体审计：哪些「变体」其实是正常的中文常用词？

背景：中文 ASR 的同音错与正常用词在字面上完全一样（`酸法`/`算法`、`一部`/`异步`），
局部上下文分不开。订正引擎因此加了「文档级主题证据」——同篇里正确写法出现得足够多、
变体只出现在少数几处，才判为转写错误。

但有些变体本身就是高频正常词（`一部` 是量词，任何一篇里都可能有），主题证据对它们
不成立，必须登记进条目的 `ordinary_variants`，只认局部上下文。**哪些该登记不能靠猜**，
要在真实语料上量：这个变体出现在多少篇里。出现在很多篇 = 它本来就是个正常词。

用法：
    python tools/audit_variants.py ../corpus_asr
    python tools/audit_variants.py ../corpus_asr --min-df 5 --zh-only
    python tools/audit_variants.py ../corpus_asr --suggest   # 直接打印建议改动

建议的判定口径：`--min-df` 默认 5，即「196 篇语料里出现在 5 篇以上」。阈值不宜调低：
技术上不可能出现的写法（`刀客`/`多克`）恰好是我们要改的那些，它们只在个别篇里出现。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asr_polish import Lexicon  # noqa: E402
from asr_polish.correction import is_ascii_variant  # noqa: E402

_EXTS = (".txt", ".md")


def iter_docs(paths: Iterable[str]) -> List[Tuple[str, str]]:
    docs: List[Tuple[str, str]] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.suffix.lower() in _EXTS and f.is_file():
                    docs.append((str(f), f.read_text(encoding="utf-8", errors="ignore")))
        elif p.is_file():
            docs.append((str(p), p.read_text(encoding="utf-8", errors="ignore")))
    return [(n, t) for n, t in docs if t.strip()]


def variant_stats(lexicon: Lexicon, docs: List[Tuple[str, str]],
                  zh_only: bool = False) -> List[Tuple[str, str, str, int, int, bool]]:
    """返回 [(变体, 正确词, 置信度, 出现篇数, 正确写法出现篇数, 已登记 ordinary)]。"""
    rows = []
    for entry in lexicon.entries:
        correct = entry.get("correct", "")
        variants = [v for v in (entry.get("variants") or []) if v]
        if not correct or not variants:
            continue
        for variant in variants:
            if zh_only and is_ascii_variant(variant):
                continue
            df_v = sum(1 for _, t in docs if variant in t)
            df_c = sum(1 for _, t in docs if correct in t)
            rows.append((variant, correct, lexicon.resolve_confidence(entry, variant),
                         df_v, df_c, lexicon.is_ordinary_variant(entry, variant)))
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="审计哪些变体其实是正常中文词")
    parser.add_argument("paths", nargs="+", help="语料文件或目录（递归收 .txt/.md）")
    parser.add_argument("--lexicon", default=None, help="词表路径（默认包内词表）")
    parser.add_argument("--min-df", type=int, default=5,
                        help="出现在多少篇以上算「正常常用词」，默认 5")
    parser.add_argument("--zh-only", action="store_true", help="只看中文变体")
    parser.add_argument("--suggest", action="store_true",
                        help="按条目汇总，打印可直接抄进词表的 ordinary_variants")
    parser.add_argument("--top", type=int, default=0, help="只显示前 N 行，0 = 全部")
    args = parser.parse_args(argv)

    lexicon = Lexicon(args.lexicon) if args.lexicon else Lexicon()
    docs = iter_docs(args.paths)
    if not docs:
        print("没有读到语料，检查路径：" + " ".join(args.paths), file=sys.stderr)
        return 2

    total_chars = sum(len(t) for _, t in docs)
    print(f"语料：{len(docs)} 篇 / {total_chars:,} 字　阈值：出现在 ≥{args.min_df} 篇\n")

    rows = variant_stats(lexicon, docs, zh_only=args.zh_only)
    rows.sort(key=lambda r: (-r[3], r[0]))
    if args.top:
        rows = rows[:args.top]

    print(f"{'变体':<12}{'正确词':<14}{'置信':<8}{'篇数':>6}{'正确词篇数':>10}  备注")
    suggested: Counter = Counter()
    for variant, correct, conf, df_v, df_c, registered in rows:
        flag = ""
        if registered:
            flag = "已登记 ordinary"
        elif df_v >= args.min_df:
            flag = "⚠ 疑似正常词（需人工复核上下文）"
            suggested[correct] += 1
        print(f"{variant:<12}{correct:<14}{conf:<8}{df_v:>6}{df_c:>10}  {flag}")

    if args.suggest:
        print("\n建议改动（合并进对应条目）：")
        if not suggested:
            print("  无需改动。")
        for correct, _ in suggested.most_common():
            vs = [r[0] for r in rows
                  if r[1] == correct and r[3] >= args.min_df and not r[5]]
            print(f'  {correct}: ordinary_variants = {vs}   # 复核后合并进该条目')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
