"""术语订正引擎：修正 ASR 把技术术语转成的同音/形近错字。

设计要点：
- 长词优先：先匹配长变体，避免「Agentic Coding」的变体被「Agent」的变体抢先误替换。
- 英文变体加字母边界、中文变体精确匹配：纯 ASCII 变体用 `(?<![A-Za-z])...(?![A-Za-z])`
  包裹，避免「pass」命中「compass」「passport」；全大写变体（如 PASS）改为大小写敏感，
  避免把英文常用词「pass」误判成 Python。
- 变体级置信分级：置信度标在「变体」而非「条目」上。规则默认 ASCII 变体 high、中文变体
  medium（`Lexicon.resolve_confidence` 负责），「不可能独立成词」的安全中文变体用
  `high_variants` 白名单升级。high 变体无条件订正；medium 变体（如「一部」→「异步」这类
  本身是合法中文常用词的）只有在命中位置 ±8 字内出现足够上下文佐证时才订正，
  从而避免「我们看一部电影」被改成「我们看异步电影」。
- 上下文分强弱：条目可配 `strong_context`（如「虚拟机」「序列化」这类强信号，命中 1 个
  即可）与 `context`（弱信号词，需命中 ≥2 个）。避免「我们讲爪哇的虚拟机」因只有 1 个
  强词而被漏判。
- 邻接保护（guards）：条目可用 `guards: {变体: 正则}` 给「会被更长正常词包含」的中文
  变体加负向前瞻，例如 `全占(?!满)` 让「全占满了」不被误判为「全栈」。
- 记录每一处订正的位置、原文与置信度，方便人工核对与回滚（尤其只审 medium 改动）。
纯标准库实现，无第三方依赖。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Tuple

from .lexicon import Lexicon

# medium 变体做上下文佐证时，向前后各扩展的字符窗口大小
_CONTEXT_WINDOW = 8
# medium 变体在窗口内至少命中的「弱」上下文词数量（强词命中 1 个即可）
_MIN_CONTEXT_HITS = 2


def is_ascii_variant(variant: str) -> bool:
    """变体是否为纯 ASCII（英文/缩写）。quality 与 correction 共用同一判定。"""
    return all(ord(ch) < 128 for ch in variant)


def has_context(text: str, start: int, end: int,
                strong: List[str], weak: List[str]) -> bool:
    """medium 变体的上下文佐证：±8 字窗口内强词命中 1 个、或弱词命中 ≥2 个。

    correction 与 quality 共用同一判定，保证「订正」与「打分」对同一处变体
    给出同一个结论，不会出现「correct 不动、assess 却扣分」的分裂。
    """
    window = text[max(0, start - _CONTEXT_WINDOW): end + _CONTEXT_WINDOW]
    if any(w and w in window for w in strong):
        return True
    return sum(1 for w in weak if w and w in window) >= _MIN_CONTEXT_HITS


def variant_pattern(variant: str, guard: str = "") -> Tuple[str, int]:
    """返回 (正则, flags)。correction 与 quality 共用，避免两侧判定分裂。

    - 纯 ASCII 变体：加字母边界，避免子串误伤；全大写（含字母）则大小写敏感。
    - 中文变体：精确匹配（不加 IGNORECASE）。
    - guard：可选的负向前瞻内容（不含 `(?!...)` 包裹），用于中文变体的邻接保护，
      例如 guard="满" 让「全占满了」不命中「全占」。
    """
    if is_ascii_variant(variant) and any(c.isalpha() for c in variant):
        pattern = r"(?<![A-Za-z0-9])" + re.escape(variant) + r"(?![A-Za-z0-9])"
        has_lower = any(c.islower() for c in variant)
        has_upper = any(c.isupper() for c in variant)
        # 全大写（如 PASS）→ 大小写敏感，避免把小写英文词「pass」误判
        flags = 0 if (has_upper and not has_lower) else re.IGNORECASE
    else:
        pattern = re.escape(variant)
        flags = 0
    if guard:
        pattern += "(?!" + guard + ")"
    return pattern, flags


@dataclass
class Correction:
    """一处订正。"""
    original: str      # 原文片段（错误写法）
    corrected: str     # 订正后的术语
    start: int         # 原文中的起始位置
    end: int           # 原文中的结束位置
    category: str = ""  # 术语类别
    confidence: str = "high"  # 该变体的置信度（high 直接采信；medium 由上下文佐证，建议人工复核）


@dataclass
class CorrectionResult:
    """订正结果。"""
    text: str                              # 订正后的文本
    corrections: List[Correction] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.corrections)

    @property
    def medium_corrections(self) -> List[Correction]:
        """medium 置信的订正（由上下文佐证触发，建议人工复核）。"""
        return [c for c in self.corrections if c.confidence != "high"]

    def summary(self) -> str:
        if not self.corrections:
            return "未发现需要订正的术语错误"
        items = [
            f"{c.original} → {c.corrected}" + ("" if c.confidence == "high" else "（medium·待确认）")
            for c in self.corrections
        ]
        return f"订正 {len(items)} 处：{'；'.join(items)}"


class Corrector:
    """术语订正器。"""

    def __init__(self, lexicon: Lexicon | None = None):
        self.lexicon = lexicon or Lexicon()
        # 类别 → 该类别下的所有正确术语（medium 变体弱上下文佐证的回退来源）
        self._category_terms: dict[str, List[str]] = self._build_category_terms()
        # (变体, 正确词, 类别, 置信度, 强上下文词, 弱上下文词, guard)，按变体长度降序
        self._patterns = self._build_patterns()

    def _build_patterns(self):
        patterns = []
        for entry in self.lexicon.entries:
            correct = entry["correct"]
            category = entry.get("category", "")
            strong, weak = self._context_words_for(entry)
            guards = entry.get("guards") or {}
            for variant in entry.get("variants", []):
                confidence = self.lexicon.resolve_confidence(entry, variant)
                guard = guards.get(variant, "")
                patterns.append((variant, correct, category, confidence, strong, weak, guard))
        patterns.sort(key=lambda x: len(x[0]), reverse=True)
        return patterns

    def _context_words_for(self, entry: dict) -> Tuple[List[str], List[str]]:
        """返回 (强上下文词, 弱上下文词)。

        强词来自 `strong_context`（如「虚拟机」「序列化」），命中 1 个即可佐证；
        弱词来自 `context`，未配置时回退到同类别术语，需命中 ≥2 个。
        """
        strong = [str(w) for w in (entry.get("strong_context") or []) if str(w)]
        weak = self.lexicon.context_words(entry)
        if not weak:
            weak = self._category_terms.get(entry.get("category", ""), [])
        return strong, weak

    def _build_category_terms(self) -> dict[str, List[str]]:
        """类别 → 正确术语列表（去重）。用于 medium 变体的弱上下文佐证。"""
        mapping: dict[str, List[str]] = {}
        for entry in self.lexicon.entries:
            cat = entry.get("category", "")
            if not cat:
                continue
            mapping.setdefault(cat, [])
            if entry["correct"] not in mapping[cat]:
                mapping[cat].append(entry["correct"])
        return mapping

    def _has_context(self, text: str, start: int, end: int,
                     strong: List[str], weak: List[str]) -> bool:
        """medium 变体的上下文佐证（实现见模块级 has_context）。"""
        return has_context(text, start, end, strong, weak)

    def correct(self, text: str) -> CorrectionResult:
        """对文本执行订正，返回订正结果。"""
        # (start, end, correct, original, category, confidence)
        hits: List[Tuple[int, int, str, str, str, str]] = []
        occupied: List[Tuple[int, int]] = []              # 已占用的区间，避免重叠替换

        for variant, correct, category, confidence, strong, weak, guard in self._patterns:
            pattern, flags = variant_pattern(variant, guard)
            for m in re.finditer(pattern, text, flags):
                start, end = m.span()
                # 跳过与已订正区间重叠的匹配
                if any(not (end <= s or start >= e) for s, e in occupied):
                    continue
                # medium 变体需要上下文佐证，否则视为正常用词跳过；low/未知不自动订正
                if confidence == "medium" and not self._has_context(
                        text, start, end, strong, weak):
                    continue
                if confidence not in ("high", "medium"):
                    continue
                hits.append((start, end, correct, m.group(0), category, confidence))
                occupied.append((start, end))

        # 从后往前应用替换，保证位置不因前段替换而偏移
        result = text
        for start, end, correct, original, category, confidence in sorted(hits, key=lambda x: -x[0]):
            result = result[:start] + correct + result[end:]

        corrections = [
            Correction(original=o, corrected=c, start=s, end=e, category=cat, confidence=conf)
            for s, e, c, o, cat, conf in sorted(hits, key=lambda x: x[0])
        ]
        return CorrectionResult(text=result, corrections=corrections)


def correct_text(text: str, lexicon: Lexicon | None = None) -> CorrectionResult:
    """便捷入口：一键订正。"""
    return Corrector(lexicon).correct(text)
