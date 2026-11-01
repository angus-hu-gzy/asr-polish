"""术语订正引擎：修正 ASR 把技术术语转成的同音/形近错字。

设计要点：
- 长词优先：先匹配长变体，避免「Agentic Coding」的变体被「Agent」的变体抢先误替换。
- 英文变体大小写不敏感、中文变体精确匹配：PASS/Pass/pass 都命中「Python」，
  而「派森」等中文变体只做精确匹配。
- 记录每一处订正的位置与原文，方便人工核对与回滚。
纯标准库实现，无第三方依赖。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List

from .lexicon import Lexicon


@dataclass
class Correction:
    """一处订正。"""
    original: str      # 原文片段（错误写法）
    corrected: str     # 订正后的术语
    start: int         # 原文中的起始位置
    end: int           # 原文中的结束位置
    category: str = ""  # 术语类别


@dataclass
class CorrectionResult:
    """订正结果。"""
    text: str                              # 订正后的文本
    corrections: List[Correction] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.corrections)

    def summary(self) -> str:
        if not self.corrections:
            return "未发现需要订正的术语错误"
        items = [f"{c.original} → {c.corrected}" for c in self.corrections]
        return f"订正 {len(items)} 处：{'；'.join(items)}"


class Corrector:
    """术语订正器。"""

    def __init__(self, lexicon: Lexicon | None = None):
        self.lexicon = lexicon or Lexicon()
        # (变体, 正确词, 类别)，按变体长度降序
        self._patterns: List[tuple[str, str, str]] = self._build_patterns()

    def _build_patterns(self) -> List[tuple[str, str, str]]:
        patterns: List[tuple[str, str, str]] = []
        for entry in self.lexicon.entries:
            correct = entry["correct"]
            category = entry.get("category", "")
            for variant in entry.get("variants", []):
                patterns.append((variant, correct, category))
        patterns.sort(key=lambda x: len(x[0]), reverse=True)
        return patterns

    @staticmethod
    def _is_ascii_variant(variant: str) -> bool:
        """变体是否为纯 ASCII（英文/缩写），用于决定是否大小写不敏感。"""
        return all(ord(ch) < 128 for ch in variant)

    def correct(self, text: str) -> CorrectionResult:
        """对文本执行订正，返回订正结果。"""
        hits: List[tuple[int, int, str, str, str]] = []  # (start, end, correct, original, category)
        occupied: List[tuple[int, int]] = []              # 已占用的区间，避免重叠替换

        for variant, correct, category in self._patterns:
            flags = re.IGNORECASE if self._is_ascii_variant(variant) else 0
            for m in re.finditer(re.escape(variant), text, flags):
                start, end = m.span()
                # 跳过与已订正区间重叠的匹配
                if any(not (end <= s or start >= e) for s, e in occupied):
                    continue
                hits.append((start, end, correct, m.group(0), category))
                occupied.append((start, end))

        # 从后往前应用替换，保证位置不因前段替换而偏移
        result = text
        for start, end, correct, original, category in sorted(hits, key=lambda x: -x[0]):
            result = result[:start] + correct + result[end:]

        corrections = [
            Correction(original=o, corrected=c, start=s, end=e, category=cat)
            for s, e, c, o, cat in sorted(hits, key=lambda x: x[0])
        ]
        return CorrectionResult(text=result, corrections=corrections)


def correct_text(text: str, lexicon: Lexicon | None = None) -> CorrectionResult:
    """便捷入口：一键订正。"""
    return Corrector(lexicon).correct(text)
