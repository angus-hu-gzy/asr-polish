"""评测：把「订正准不准」变成可复现的数字。

评测集是 `eval/cases.jsonl`，每行一条用例：

    {"id": "daily-01", "group": "daily", "input": "我看了一部电影",
     "expect": "我看了一部电影", "note": "日常用词，不应订正"}
    {"id": "asr-01", "group": "asr", "input": "今天讲色考和PASS",
     "expect": "今天讲SQL和Python", "edits": [["色考", "SQL"], ["PASS", "Python"]]}

`expect` 是「正确引擎应该输出的完整文本」——不该改的用例，expect 与 input 相同。
正例必须显式声明 `edits`（[原文, 订正后] 列表，按出现顺序）：用它定位「标准答案改了
哪几处」，进而算出精确率/召回率。不能用字符级 diff 反推——`PASS → Python` 会被对齐成
`PASS → ython`，把正确的改动误判成误伤。

指标：
  用例级   整段输出与期望完全一致的用例占比（最严格，任何一处多改/少改都算错）
  改动级   精确率 = 正确改动 / 引擎改动总数（衡量「改错」）
           召回率 = 正确改动 / 标注应改总数（衡量「漏改」）
           误伤 FP 与 漏改 FN 会分别列出——误伤代价远高于漏改，优先看 FP

用法：
    python tools/eval_corrections.py
    python tools/eval_corrections.py --group daily
    python tools/eval_corrections.py -v                      # 逐条打印
    python tools/eval_corrections.py --min-precision 0.98    # 低于阈值退出 1（供 CI）
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asr_polish import correct_text  # noqa: E402

CASES_PATH = Path(__file__).resolve().parent.parent / "eval" / "cases.jsonl"


@dataclass
class Edit:
    """一处文本改动（输入区间 → 新文本）。"""
    start: int
    end: int
    text: str

    def __str__(self) -> str:
        return f"({self.start},{self.end}){self.text!r}"


@dataclass
class CaseResult:
    case: dict
    actual: str
    gold: List[Edit] = field(default_factory=list)
    engine: List[Edit] = field(default_factory=list)
    tp: int = 0
    fp: List[Edit] = field(default_factory=list)
    fn: List[Edit] = field(default_factory=list)

    @property
    def case_ok(self) -> bool:
        return self.actual == self.case["expect"]


def gold_edits(case: dict) -> List[Edit]:
    """按用例显式声明的 edits 定位「标准答案改了哪几处」。

    不用字符级 diff 反推：`PASS → Python` 会被对齐成 `PASS → ython`（共用开头 P），
    把正确的订正误判成误伤。评测集的答案必须显式给出，不能猜。
    """
    src = case["input"]
    edits: List[Edit] = []
    pos = 0
    for pair in case.get("edits") or []:
        frm, to = pair
        idx = src.find(frm, pos)
        if idx < 0:
            raise SystemExit(f"用例 {case['id']} 声明的 edits 里找不到 {frm!r}")
        edits.append(Edit(start=idx, end=idx + len(frm), text=to))
        pos = idx + len(frm)
    return edits


def score_case(case: dict) -> CaseResult:
    src = case["input"]
    result = correct_text(src)
    engine = [Edit(c.start, c.end, c.corrected) for c in result.corrections]
    gold = gold_edits(case)
    res = CaseResult(case=case, actual=result.text, gold=gold, engine=engine)

    remaining = list(engine)          # 尚未匹配的引擎改动
    for g in gold:
        for e in remaining:
            if e.start == g.start and e.end == g.end and e.text == g.text:
                remaining.remove(e)
                res.tp += 1
                break
        else:
            res.fn.append(g)
    res.fp = remaining
    return res


def _check_edits_consistency(case: dict, name: str, lineno: int) -> None:
    """校验「按 edits 改完 == expect」，防止评测集的答案自己写错。"""
    edits = case.get("edits") or []
    if not edits:
        return
    out = case["input"]
    pos = 0
    for frm, to in edits:
        idx = out.find(frm, pos)
        if idx < 0:
            raise SystemExit(f"{name}:{lineno} 用例 {case['id']} 的 edits 里找不到 {frm!r}")
        out = out[:idx] + to + out[idx + len(frm):]
        pos = idx + len(to)
    if out != case["expect"]:
        raise SystemExit(
            f"{name}:{lineno} 用例 {case['id']} 的 edits 应用后为 {out!r}，"
            f"与 expect {case['expect']!r} 不符")


def load_cases(path: Path = CASES_PATH) -> List[dict]:
    if not path.exists():
        raise SystemExit(f"评测集不存在：{path}")
    cases: List[dict] = []
    with path.open(encoding="utf-8-sig") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path.name}:{lineno} JSON 解析失败：{exc}")
            for key in ("id", "input", "expect"):
                if key not in case:
                    raise SystemExit(f"{path.name}:{lineno} 缺少字段 {key}")
            case.setdefault("group", "default")
            if case["expect"] != case["input"] and not case.get("edits"):
                raise SystemExit(f"{path.name}:{lineno} 正例 {case['id']} 缺少 edits 字段")
            _check_edits_consistency(case, path.name, lineno)
            cases.append(case)
    return cases


def _pct(part: float, whole: float) -> str:
    return f"{part / whole:.1%}" if whole else "—"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval_corrections",
        description="在标注评测集上量化术语订正的精确率/召回率",
    )
    parser.add_argument("--cases", default=str(CASES_PATH), help="评测集路径")
    parser.add_argument("--group", default="", help="只跑某一组（daily/asr/boundary…）")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印结果")
    parser.add_argument("--min-precision", type=float, default=0.0,
                        help="改动级精确率低于该值则退出码 1（供 CI 用）")
    parser.add_argument("--min-recall", type=float, default=0.0,
                        help="改动级召回率低于该值则退出码 1")
    args = parser.parse_args(argv)

    cases = load_cases(Path(args.cases))
    if args.group:
        cases = [c for c in cases if c["group"] == args.group]
    if not cases:
        print("没有匹配的用例", file=sys.stderr)
        return 2

    results = [score_case(c) for c in cases]

    tp = sum(r.tp for r in results)
    fp_all: List[Tuple[CaseResult, Edit]] = [(r, e) for r in results for e in r.fp]
    fn_all: List[Tuple[CaseResult, Edit]] = [(r, g) for r in results for g in r.fn]
    engine_total = sum(len(r.engine) for r in results)
    gold_total = sum(len(r.gold) for r in results)
    case_ok = sum(r.case_ok for r in results)

    precision = tp / engine_total if engine_total else 1.0
    recall = tp / gold_total if gold_total else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    groups: Dict[str, List[CaseResult]] = {}
    for r in results:
        groups.setdefault(r.case["group"], []).append(r)

    print(f"评测集：{len(cases)} 条用例，{len(groups)} 组"
          f"（{' / '.join(f'{k} {len(v)}' for k, v in groups.items())}）")
    print(f"用例级：{case_ok}/{len(cases)} 完全正确  {_pct(case_ok, len(cases))}")
    print(f"改动级：精确率 {precision:.1%}（{tp}/{engine_total} 处改动是对的）"
          f"  召回率 {recall:.1%}（{tp}/{gold_total} 处该改的改到了）  F1 {f1:.1%}")
    print(f"        误伤 {len(fp_all)} 处（不该改却改了）  漏改 {len(fn_all)} 处")
    print()

    for name, rs in groups.items():
        g_tp = sum(r.tp for r in rs)
        g_eng = sum(len(r.engine) for r in rs)
        g_gold = sum(len(r.gold) for r in rs)
        g_ok = sum(r.case_ok for r in rs)
        print(f"  {name:<10} 用例 {g_ok}/{len(rs)}"
              f"  精确率 {_pct(g_tp, g_eng)}  召回率 {_pct(g_tp, g_gold)}"
              f"  误伤 {sum(len(r.fp) for r in rs)}  漏改 {sum(len(r.fn) for r in rs)}")
    print()

    if fp_all:
        print(f"误伤（FP，优先级最高）{len(fp_all)} 处：")
        for r, e in fp_all:
            before = r.case["input"]
            print(f"  ✗ {r.case['id']}  「{before[max(0, e.start - 8):e.end + 8]}」"
                  f"  {before[e.start:e.end]!r} → {e.text!r}")
        print()

    if fn_all:
        print(f"漏改（FN）{len(fn_all)} 处：")
        for r, g in fn_all:
            before = r.case["input"]
            print(f"  · {r.case['id']}  「{before[max(0, g.start - 8):g.end + 8]}」"
                  f"  应改为 {g.text!r}")
        print()

    if args.verbose:
        for r in results:
            mark = "OK  " if r.case_ok else "FAIL"
            print(f"[{mark}] {r.case['id']}")
            print(f"       输入 {r.case['input']}")
            print(f"       期望 {r.case['expect']}")
            print(f"       实际 {r.actual}")
        print()

    failed = precision < args.min_precision or recall < args.min_recall
    if failed:
        print(f"未达到阈值（精确率 ≥{args.min_precision:.0%}，召回率 ≥{args.min_recall:.0%}）",
              file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
