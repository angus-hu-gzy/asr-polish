"""基准：在真实 ASR 语料上量「术语召回率」——漏改比误伤更常见，但没人量过。

`tools/eval_corrections.py` 用的是手写用例，能测「给定这个错法，能不能改对」，
但回答不了「真实视频里 ASR 这么错时，我们漏了多少」。本工具补这一块：

1. 扫语料，定位每个**正确术语**的出现位置（语料是 ASR 原始输出，术语本身是对的，
   错字在术语周围）；
2. 在每个位置把正确术语**替换成它的某个变体**，模拟 ASR 听错；
3. 用 ±WINDOW 字的切片跑订正器，判定该处：
      hit    改回正确术语
      miss   一处没改（漏改）
      wrong  改成了别的词（改错）
4. 另拿一份**干净语料**（人写对了的文本）跑一遍，数同一变体在正常用法里被改坏的
   次数（误伤）——精确率 = 命中 /（命中 + 误伤）。ASR 语料自身带真错字，在上面
   数改动分不清真改和误改，所以误伤必须换一份干净文本量。

为什么用切片而不是整篇：整篇里多处扰动会互相干扰，无法归因到具体变体；
WINDOW 取 250 字足够覆盖 ±8 字的上下文判定窗口，又不会让相邻用例串味。

变体轮转采样：每个变体分到的样本数尽量均等，否则 `一部` 这种超高频变体
会主导总体召回率，掩盖真正抓不回来的低频变体。

用法：
    python tools/benchmark_recall.py ..\\corpus_asr
    python tools/benchmark_recall.py ..\\corpus_asr --per-entry 60 --seed 7
    python tools/benchmark_recall.py ..\\corpus_asr --json out.json --min-recall 0.85
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asr_polish.correction import Corrector  # noqa: E402
from asr_polish.lexicon import Lexicon  # noqa: E402

WINDOW = 250


# ── 语料 ────────────────────────────────────────────────────

def load_corpus(dirs: list[str]) -> list[tuple[str, str]]:
    """返回 [(文件名, 文本)]。"""
    docs: list[tuple[str, str]] = []
    for d in dirs:
        root = Path(d)
        if not root.exists():
            raise SystemExit(f"语料目录不存在：{root}")
        files = sorted(p for p in root.rglob("*.txt") if p.is_file())
        for p in files:
            text = p.read_text(encoding="utf-8", errors="ignore")
            if len(text) >= 200:
                docs.append((p.name, text))
    if not docs:
        raise SystemExit("没有读到可用语料（需要 >=200 字的 .txt）")
    return docs


# ── 术语定位 ─────────────────────────────────────────────────

def term_pattern(term: str) -> re.Pattern[str]:
    """定位「正确术语」。ASCII 术语加字母边界并忽略大小写（Python/python 都算正确）。"""
    if term.isascii():
        return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])",
                          re.IGNORECASE)
    return re.compile(re.escape(term))


def find_occurrences(docs, term: str, cap_per_doc: int = 6) -> list[tuple[int, int, int]]:
    """返回 [(doc_idx, start, end)]，每个文档最多取 cap_per_doc 处，避免单篇长文独占。"""
    pat = term_pattern(term)
    out: list[tuple[int, int, int]] = []
    for idx, (_, text) in enumerate(docs):
        n = 0
        for m in pat.finditer(text):
            out.append((idx, m.start(), m.end()))
            n += 1
            if n >= cap_per_doc:
                break
    return out


def make_slice(text: str, start: int, end: int, window: int = WINDOW):
    """切出 (左文, 右文)，替换时拼回 左文 + 变体 + 右文。向行边界对齐，避免切断句子。"""
    lo = max(0, start - window)
    hi = min(len(text), end + window)
    nl = text.rfind("\n", lo, start)
    if nl != -1 and start - nl <= 40:
        lo = nl + 1
    nl2 = text.find("\n", end, hi)
    if nl2 != -1 and nl2 - end <= 40:
        hi = nl2
    return text[lo:start], text[end:hi]


# ── 采样与回测 ───────────────────────────────────────────────

@dataclass
class Sample:
    variant: str
    correct: str
    category: str
    confidence: str
    left: str
    right: str


@dataclass
class Tally:
    samples: int = 0
    hit: int = 0
    miss: int = 0
    wrong: int = 0
    miss_examples: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.hit / self.samples if self.samples else 0.0


def build_samples(corrector: Corrector, docs, per_entry: int, seed: int) -> list[Sample]:
    rng = random.Random(seed)
    samples: list[Sample] = []
    for entry in corrector.lexicon.entries:
        correct = entry.get("correct", "")
        variants = [v for v in (entry.get("variants") or []) if v]
        if not correct or not variants:
            continue
        occ = find_occurrences(docs, correct)
        if not occ:
            continue
        rng.shuffle(occ)
        for i, (doc_idx, start, end) in enumerate(occ[:per_entry]):
            variant = variants[i % len(variants)]          # 变体轮转，样本均摊
            left, right = make_slice(docs[doc_idx][1], start, end)
            samples.append(Sample(
                variant=variant, correct=correct,
                category=entry.get("category", ""),
                confidence=corrector.lexicon.resolve_confidence(entry, variant),
                left=left, right=right))
    return samples


def judge(cr, s: Sample) -> str:
    """只判「该处变体是否被改回 s.correct」。

    必须看订正列表而不是输出文本——切片别处可能本来就出现同一个正确术语
    （例如左边那句已经说过 Python），用 `in text` 判断会把漏改误判成命中。
    """
    norm = s.variant.strip().lower()
    for c in cr.corrections:
        if c.original.strip().lower() == norm or norm in c.original.lower():
            return "hit" if c.corrected == s.correct else "wrong"
    return "miss"


def run_samples(corrector: Corrector, samples: list[Sample]):
    overall = Tally()
    by_variant: dict[tuple[str, str], Tally] = defaultdict(Tally)
    by_conf: dict[str, Tally] = defaultdict(Tally)
    by_cat: dict[str, Tally] = defaultdict(Tally)

    for s in samples:
        perturbed = s.left + s.variant + s.right
        res = corrector.correct(perturbed)
        verdict = judge(res, s)
        key = (s.variant, s.correct)
        for t in (overall, by_variant[key], by_conf[s.confidence], by_cat[s.category]):
            t.samples += 1
            setattr(t, verdict, getattr(t, verdict) + 1)
        if verdict != "hit" and len(by_variant[key].miss_examples) < 2:
            pos = len(s.left)
            ctx = perturbed[max(0, pos - 18):pos + len(s.variant) + 18].replace("\n", " ")
            by_variant[key].miss_examples.append(
                f"{verdict}: …{ctx}… → {res.text[max(0, pos - 18):pos + 18].replace(chr(10), ' ')}")
    return overall, by_variant, by_conf, by_cat


def count_false_positives(corrector: Corrector, clean_docs) -> dict[tuple[str, str], int]:
    """在干净语料（人写对了的文本）上跑一遍，统计每个变体被误改的次数。

    ASR 语料自身带真错字，在上面数「改动」分不清真改和误改；干净语料里出现的
    变体字符串都是正常用词（「一部电影」「孙子兵法」），被改就是误伤。
    """
    fp: dict[tuple[str, str], int] = defaultdict(int)
    for _, text in clean_docs:
        for c in corrector.correct(text).corrections:
            fp[(c.original, c.corrected)] += 1
    return fp


# ── 输出 ────────────────────────────────────────────────────

def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def report(docs, samples, overall, by_variant, by_conf, by_cat,
           fp, clean_chars: int, verbose: bool, top: int,
           entries_total: int = 0) -> None:
    chars = sum(len(t) for _, t in docs)
    covered = len({s.correct for s in samples})
    total_fp = sum(fp.values())
    precision = overall.hit / (overall.hit + total_fp) if (overall.hit + total_fp) else 0.0

    print(f"ASR 语料：{len(docs)} 篇 / {chars:,} 字")
    print(f"采样：{len(samples)} 处扰动，覆盖 {covered} 个术语"
          + (f"（词表共 {entries_total} 条，其余 {entries_total - covered} 条本轮语料未出现，未测）"
             if entries_total else ""))
    print(f"干净语料：{clean_chars:,} 字（用于数误伤）" if clean_chars else "干净语料：未提供（跳过误伤统计）")
    print()
    print(f"召回率：{pct(overall.recall)}   (hit {overall.hit} / miss {overall.miss} / wrong {overall.wrong})")
    if clean_chars:
        print(f"误伤：{total_fp} 处（干净语料里被改坏）→ 精确率 {pct(precision)}")
    print()

    print("按置信度：")
    for conf in ("high", "medium", "low"):
        t = by_conf.get(conf)
        if t and t.samples:
            print(f"  {conf:<7} 样本 {t.samples:>4}  召回 {pct(t.recall):>6}  "
                  f"(漏改 {t.miss}, 改错 {t.wrong})")
    print()
    print("按类别（召回率升序）：")
    for cat, t in sorted(by_cat.items(), key=lambda kv: kv[1].recall):
        if t.samples:
            print(f"  {cat:<12} 样本 {t.samples:>4}  召回 {pct(t.recall):>6}")
    print()

    rows = [(v, c, t) for (v, c), t in by_variant.items() if t.samples]
    rows.sort(key=lambda r: (r[2].recall, -r[2].samples))
    print(f"最该修的 {top} 个变体（召回率升序）：")
    header = f"  {'变体':<12}{'正确词':<14}{'置信':<8}{'样本':>5}{'召回率':>8}"
    print(header + (f"{'误伤':>6}" if clean_chars else ""))
    shown = 0
    for variant, correct, t in rows:
        if t.recall >= 1.0 and shown >= 5:
            break
        conf = "high" if any(s.confidence == "high"
                             for s in samples if s.variant == variant) else "medium"
        line = f"  {variant:<12}{correct:<14}{conf:<8}{t.samples:>5}{pct(t.recall):>8}"
        if clean_chars:
            line += f"{fp.get((variant, correct), 0):>6}"
        print(line)
        shown += 1
        if verbose:
            for ex in t.miss_examples:
                print(f"      {ex}")
    print()

    if fp:
        print("误伤明细（干净语料里被改坏的地方）：")
        for (orig, fixed), n in sorted(fp.items(), key=lambda kv: -kv[1])[:15]:
            print(f"  {orig} → {fixed}  ×{n}")


def main() -> None:
    ap = argparse.ArgumentParser(description="真实 ASR 语料上的术语召回率基准")
    ap.add_argument("corpus", nargs="+", help="语料目录（含 .txt）")
    ap.add_argument("--clean", nargs="*", default=None,
                    help="干净语料目录（人写对了的文本），用于统计误伤；不给则跳过")
    ap.add_argument("--lexicon", default=None, help="词表路径（默认内置）")
    ap.add_argument("--per-entry", type=int, default=40, help="每个术语最多采多少处（默认 40）")
    ap.add_argument("--seed", type=int, default=7, help="采样随机种子")
    ap.add_argument("--top", type=int, default=15, help="变体表列多少行")
    ap.add_argument("--min-recall", type=float, default=0.0,
                    help="低于该召回率退出码 1（供 CI）")
    ap.add_argument("--json", default=None, help="把结果写成 JSON")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印漏改样例")
    args = ap.parse_args()

    docs = load_corpus(args.corpus)
    lexicon = Lexicon(args.lexicon) if args.lexicon else Lexicon()
    corrector = Corrector(lexicon)
    samples = build_samples(corrector, docs, args.per_entry, args.seed)
    if not samples:
        raise SystemExit("没有采到样本：语料里没有出现词表中的正确术语")

    overall, by_variant, by_conf, by_cat = run_samples(corrector, samples)

    fp: dict = {}
    clean_chars = 0
    if args.clean:
        clean_docs = load_corpus(args.clean)
        clean_chars = sum(len(t) for _, t in clean_docs)
        fp = count_false_positives(corrector, clean_docs)

    entries_total = sum(1 for e in lexicon.entries if e.get("variants"))
    report(docs, samples, overall, by_variant, by_conf, by_cat,
           fp, clean_chars, args.verbose, args.top, entries_total)

    if args.json:
        total_fp = sum(fp.values())
        payload = {
            "corpus_files": len(docs),
            "corpus_chars": sum(len(t) for _, t in docs),
            "clean_chars": clean_chars,
            "samples": len(samples),
            "recall": round(overall.recall, 4),
            "precision": round(overall.hit / (overall.hit + total_fp), 4)
            if (overall.hit + total_fp) else None,
            "hit": overall.hit, "miss": overall.miss, "wrong": overall.wrong,
            "false_positives": total_fp,
            "false_positive_detail": [
                {"variant": o, "corrected": f, "count": n}
                for (o, f), n in sorted(fp.items(), key=lambda kv: -kv[1])],
            "by_variant": [
                {"variant": v, "correct": c, "samples": t.samples,
                 "hit": t.hit, "miss": t.miss, "wrong": t.wrong,
                 "recall": round(t.recall, 4),
                 "false_positives": fp.get((v, c), 0)}
                for (v, c), t in sorted(by_variant.items(), key=lambda kv: kv[1].recall)],
        }
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print(f"\nJSON 已写入：{args.json}")

    if args.min_recall and overall.recall < args.min_recall:
        print(f"\n召回率 {pct(overall.recall)} 低于阈值 {pct(args.min_recall)}",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
