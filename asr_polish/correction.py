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
- 文档级主题证据：中文同音错（`酸法`/`算法`、`还存`/`缓存`）在 ±8 字内根本分不开，再补一条
  证据——整篇在讲什么。一篇讲算法的字幕里「算法」出现几十次、「酸法」只出现一两次，那几次
  几乎必然是转写错误；反过来讲腌菜的视频里「算法」不出现，就不会改。判定口径：正确写法
  ≥2 次且 ≥2×(变体次数+1)。只在输入为整篇文本时生效，传单句会自然退化为纯上下文判定。
  本身是正常词的变体（`一部`/`爱的`）用 `ordinary_variants` 排除，只认局部上下文。
- 上下文分强弱：条目可配 `strong_context`（如「虚拟机」「序列化」这类强信号，命中 1 个
  即可）与 `context`（弱信号词，需命中 ≥2 个）。避免「我们讲爪哇的虚拟机」因只有 1 个
  强词而被漏判。
- 邻接保护（guards）：条目可用 `guards: {变体: 正则}` 给「会被更长正常词包含」的中文
  变体加负向前瞻，例如 `全占(?!满)` 让「全占满了」不被误判为「全栈」；`left_guards` 则是
  负向后顾（左邻），例如 `PASS` 配 `\\s` 让测试日志里的「6/6 PASS」不被改成 Python。
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
# medium 变体在窗口内至少命中的「弱」上下文词数量（强词命中 1 个即可）。
# 曾试过降到 1 个：真实 ASR 语料召回 +1.8pt，259k 干净语料与 842 处正确术语位置都
# 测不出代价；但 `我们讲一部和并发` 这类「量词 + 技术词」句子会被误改成语义无关的
# 术语，单元测试直接失败。单个弱词不足以佐证，维持 2。
_MIN_CONTEXT_HITS = 2

# 文档级证据：一篇字幕本身的用词分布就是它的主题。`酸法`/`算法` 同音，±8 字内看不出
# 该用哪个；但一篇讲算法的视频里「算法」出现几十次、「酸法」只出现一两次，那几次几乎
# 必然是转写错误；反过来讲腌菜的视频里「算法」根本不出现，就不会改。这条证据不需要
# 任何外部语料，代价是必须先拿到整篇文本（单句调用没有这个信息，退化为原判定）。
# 要求正确写法出现 ≥2 次（只有 1 次时无法确认这不是别的词），且不少于变体自身次数的
# 2 倍——「一部」在一篇里出现 75 次时不会被「异步」的 12 次翻过去，正是靠这一条。
_DOC_MIN_OCCURRENCES = 2
_DOC_DOMINANCE = 2


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


def term_count(text: str, term: str, cache: dict | None = None) -> int:
    """数某个词在文本里出现几次（英文加词边界，避免 MySQL 里的 SQL 被计入）。"""
    if not term:
        return 0
    if cache is not None and term in cache:
        return cache[term]
    pattern, flags = variant_pattern(term)
    n = len(re.findall(pattern, text, flags)) if is_ascii_variant(term) \
        else text.count(term)
    if cache is not None:
        cache[term] = n
    return n


def has_doc_evidence(text: str, variant: str, correct: str,
                     cache: dict | None = None) -> bool:
    """文档级主题证据：本篇里正确写法够多、且明显盖过这个变体。

    只对 medium 变体生效，作为 ±8 字上下文之外的第二条证据。它解决的是同音词
    在局部无从区分的问题——判断依据从「旁边的字」换成「整篇在讲什么」。
    `variant` 是当前这一处（调用方保证），数次数时扣掉它。

    领先倍数按「变体出现次数 + 1」算：本篇已经出现过一次该写法时，要求正确写法
    的次数翻倍以对——「这个兵法书里也有类似思想」出现在并发讲座里，正是靠这一条
    挡住的（兵法 ×1 → 需要「并发」≥4 次才算证据）。
    """
    n_correct = term_count(text, correct, cache)
    if n_correct < _DOC_MIN_OCCURRENCES:
        return False
    n_variant = max(term_count(text, variant, cache) - 1, 0)
    return n_correct >= _DOC_DOMINANCE * (n_variant + 1)


def has_evidence(text: str, start: int, end: int,
                 strong: List[str], weak: List[str],
                 variant: str = "", correct: str = "",
                 cache: dict | None = None,
                 allow_doc: bool = True) -> bool:
    """medium 变体的完整佐证：局部上下文 **或** 文档级主题证据。

    两者取或：局部有强信号时直接成立（快、准）；局部无从判断时再看整篇的用词分布。
    `allow_doc=False` 用于条目声明了 `ordinary_variants` 的同形词（如「一部」）——
    它在任何一篇里都可能是正常用词，主题证据不足以翻案，只认局部上下文。
    correction 与 quality 都走这里，保持判定一致。
    """
    if has_context(text, start, end, strong, weak):
        return True
    if allow_doc and variant and correct:
        return has_doc_evidence(text, variant, correct, cache)
    return False


def variant_pattern(variant: str, guard: str = "", left_guard: str = "") -> Tuple[str, int]:
    r"""返回 (正则, flags)。correction 与 quality 共用，避免两侧判定分裂。

    - 纯 ASCII 变体：加字母边界，避免子串误伤；全大写（含字母）则大小写敏感。
    - 中文变体：精确匹配（不加 IGNORECASE）。
    - guard：可选的负向前瞻内容（不含 `(?!...)` 包裹），用于右邻保护，
      例如 guard="满" 让「全占满了」不命中「全占」。
    - left_guard：可选的负向后顾内容（不含 `(?<!...)` 包裹），用于左邻保护，
      例如 left_guard=r"\s" 让测试日志里的「6/6 PASS」不命中「PASS」。
      必须是定长正则（Python 的 lookbehind 不支持变长）。
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
    if left_guard:
        pattern = "(?<!" + left_guard + ")" + pattern
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
        # (变体, 正确词, 类别, 置信度, 强上下文词, 弱上下文词, guard, left_guard)，按变体长度降序
        self._patterns = self._build_patterns()

    def _build_patterns(self):
        patterns = []
        for entry in self.lexicon.entries:
            correct = entry["correct"]
            category = entry.get("category", "")
            strong, weak = self._context_words_for(entry)
            guards = entry.get("guards") or {}
            left_guards = entry.get("left_guards") or {}
            for variant in entry.get("variants", []):
                confidence = self.lexicon.resolve_confidence(entry, variant)
                guard = guards.get(variant, "")
                left_guard = left_guards.get(variant, "")
                allow_doc = not self.lexicon.is_ordinary_variant(entry, variant)
                patterns.append((variant, correct, category, confidence,
                                 strong, weak, guard, left_guard, allow_doc))
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
                     strong: List[str], weak: List[str],
                     variant: str = "", correct: str = "",
                     cache: dict | None = None,
                     allow_doc: bool = True) -> bool:
        """medium 变体的佐证（实现见模块级 has_evidence）。"""
        return has_evidence(text, start, end, strong, weak, variant, correct,
                            cache, allow_doc)

    def correct(self, text: str) -> CorrectionResult:
        """对文本执行订正，返回订正结果。

        传入的 `text` 应当是一整篇转写文本。文档级证据依赖它——只传单句时该证据
        自动失效（正文里出现的正确写法次数必然不足 2），行为退化为纯上下文判定。
        """
        # (start, end, correct, original, category, confidence)
        hits: List[Tuple[int, int, str, str, str, str]] = []
        occupied: List[Tuple[int, int]] = []              # 已占用的区间，避免重叠替换
        freq: dict[str, int] = {}                         # 词频缓存，一次调用内复用

        for (variant, correct, category, confidence, strong, weak,
             guard, left_guard, allow_doc) in self._patterns:
            pattern, flags = variant_pattern(variant, guard, left_guard)
            for m in re.finditer(pattern, text, flags):
                start, end = m.span()
                # 跳过与已订正区间重叠的匹配
                if any(not (end <= s or start >= e) for s, e in occupied):
                    continue
                # medium 变体需要佐证（局部上下文或整篇用词分布），否则视为正常用词跳过
                if confidence == "medium" and not self._has_context(
                        text, start, end, strong, weak, variant, correct,
                        freq, allow_doc):
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
