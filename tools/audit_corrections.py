"""语料审计：在真实文本上跑一遍术语订正，把每一处改动连同上下文列出来。

用途：词表是「按变体字面」匹配的，最危险的误伤是——变体只是某个更长正常词的
一部分（`全占` 之于「完全占领」、`给特` 之于「给特别」、`病发` 之于「疾病发生」）。
这类问题靠读词表看不出来，只有拿真实语料跑一遍才会暴露。

用法：
    python tools/audit_corrections.py 语料.txt 另一个目录/ [--lexicon 词表.json]
    python tools/audit_corrections.py 语料.txt --only 全占 --contexts 5
    python tools/audit_corrections.py 语料.txt --fail-on-embedded   # 有疑似误伤就退出 1

输出按「变体 → 正确词」聚合；"⚠ 嵌入" 表示变体前后都是汉字，即被夹在一个
更长的中文词/词组里——最像误伤，值得人工复核。注意它是启发式：
真实错别字（如「色考数据库」）也会被判为嵌入，需要人看一眼上下文。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asr_polish import Lexicon, correct_text  # noqa: E402

_CJK_START, _CJK_END = "\u4e00", "\u9fff"
_CONTEXT_CHARS = 12


def _is_cjk(ch: str) -> bool:
    return bool(ch) and _CJK_START <= ch <= _CJK_END


@dataclass
class Record:
    """一处订正及其上下文。"""
    original: str
    corrected: str
    confidence: str
    context: str
    embedded: bool          # 前后都是汉字 → 很可能属于更长的正常词
    source: str = ""


@dataclass
class Group:
    """按 (变体, 正确词) 聚合后的统计。"""
    original: str
    corrected: str
    confidence: str
    count: int = 0
    embedded: int = 0
    samples: List[str] = field(default_factory=list)


def audit_text(text: str, lexicon: Optional[Lexicon] = None,
               source: str = "") -> List[Record]:
    """对一段文本做订正审计，返回所有订正记录（含上下文与嵌入标记）。"""
    result = correct_text(text, lexicon)
    records: List[Record] = []
    for c in result.corrections:
        before = text[c.start - 1] if c.start > 0 else ""
        after = text[c.end] if c.end < len(text) else ""
        start = max(0, c.start - _CONTEXT_CHARS)
        end = min(len(text), c.end + _CONTEXT_CHARS)
        records.append(Record(
            original=c.original,
            corrected=c.corrected,
            confidence=c.confidence,
            context=text[start:end].replace("\n", " "),
            embedded=_is_cjk(before) and _is_cjk(after),
            source=source,
        ))
    return records


def collect(files: Iterable[Path], lexicon: Optional[Lexicon] = None,
            only: str = "") -> List[Record]:
    records: List[Record] = []
    for path in files:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        for rec in audit_text(text, lexicon, source=path.name):
            if only and only not in (rec.original, rec.corrected):
                continue
            records.append(rec)
    return records


def group_records(records: List[Record], contexts: int = 2) -> List[Group]:
    groups: dict[Tuple[str, str], Group] = {}
    for rec in records:
        key = (rec.original, rec.corrected)
        group = groups.get(key)
        if group is None:
            group = groups[key] = Group(rec.original, rec.corrected, rec.confidence)
        group.count += 1
        group.embedded += int(rec.embedded)
        if len(group.samples) < contexts:
            group.samples.append(rec.context)
    return sorted(groups.values(), key=lambda g: (-g.embedded, -g.count, g.original))


def _iter_files(paths: List[str]) -> List[Path]:
    files: List[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(q for q in p.rglob("*")
                                if q.is_file() and q.suffix.lower() in (".txt", ".md")))
        elif p.is_file():
            files.append(p)
        else:
            print(f"跳过（不存在）：{raw}", file=sys.stderr)
    return files


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="audit_corrections",
        description="在语料上审计术语订正结果，暴露「变体是更长正常词一部分」的误伤",
    )
    parser.add_argument("paths", nargs="+", help="语料文件或目录（目录会递归收 .txt/.md）")
    parser.add_argument("--lexicon", default=None, help="词表路径（默认用包内词表 + 用户词表）")
    parser.add_argument("--only", default="", help="只看包含该变体/正确词的记录")
    parser.add_argument("--contexts", type=int, default=2, help="每组展示的上下文样本数")
    parser.add_argument("--fail-on-embedded", action="store_true",
                        help="存在「嵌入汉字」的订正时退出码为 1（供 CI/脚本判断）")
    args = parser.parse_args(argv)

    files = _iter_files(args.paths)
    if not files:
        print("没有可读的语料文件", file=sys.stderr)
        return 2

    lexicon = Lexicon(args.lexicon) if args.lexicon else Lexicon()
    records = collect(files, lexicon, only=args.only)
    total_chars = sum(len(p.read_text(encoding="utf-8-sig", errors="replace")) for p in files)

    print(f"语料：{len(files)} 个文件，{total_chars} 字符")
    print(f"订正：{len(records)} 处，其中疑似误伤（嵌入汉字）{sum(r.embedded for r in records)} 处\n")

    if not records:
        print("没有发生任何订正。")
        return 0

    for group in group_records(records, args.contexts):
        mark = f"  ⚠ 嵌入 {group.embedded}/{group.count}" if group.embedded else ""
        print(f"{group.count:>3}× {group.original} → {group.corrected}"
              f"  [{group.confidence}]{mark}")
        for sample in group.samples:
            print(f"      …{sample}…")
    return 1 if (args.fail_on_embedded and any(r.embedded for r in records)) else 0


if __name__ == "__main__":
    sys.exit(main())
