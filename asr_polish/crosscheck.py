"""多引擎交叉验证：对同一段音频的多个转写结果做序列对齐，输出片段级置信度。

原理：
- 以第一个转写为基准，用 difflib.SequenceMatcher 与其余每个转写做字符级对齐。
- 用差分数组统计基准文本每个字符被多少个引擎「支持」（即落在 equal 区间内）。
- 支持数 / 引擎总数 = 该片段的置信度。多个引擎一致 → 高置信；互相矛盾 → 低置信。

典型用法：云端 ASR（必剪）+ 本地 whisper 两个结果交叉验证，
把「两引擎都说对」的高置信段与「各说各话」的低置信段区分开，
供后续总结时对低置信片段谨慎处理。
纯标准库实现，无第三方依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import List, Optional


@dataclass
class Segment:
    """一个对齐后的片段。"""
    text: str            # 基准文本片段
    agree: int           # 同意该片段的引擎数（含基准自身）
    total: int           # 参与交叉验证的引擎总数
    confidence: float    # 0~1，同意占比

    @property
    def is_low_confidence(self) -> bool:
        return self.confidence < 0.5

    @property
    def is_conflict(self) -> bool:
        """是否「各引擎说法不一」（基准被部分引擎支持）。"""
        return self.agree < self.total


@dataclass
class CrossCheckResult:
    """交叉验证结果。"""
    merged: str                           # 合并文本（当前取基准文本）
    segments: List[Segment] = field(default_factory=list)

    @property
    def low_confidence_segments(self) -> List[Segment]:
        return [s for s in self.segments if s.is_low_confidence]

    @property
    def conflict_segments(self) -> List[Segment]:
        return [s for s in self.segments if s.is_conflict]

    @property
    def overall_confidence(self) -> float:
        """整体置信度 = 各段置信度的加权平均（按字符数加权）。"""
        total_chars = sum(len(s.text) for s in self.segments)
        if total_chars == 0:
            return 1.0
        weighted = sum(s.confidence * len(s.text) for s in self.segments)
        return round(weighted / total_chars, 4)


def cross_check(*texts: str) -> CrossCheckResult:
    """对 2 个及以上转写结果做交叉验证。第一个参数为基准文本。"""
    if not texts:
        raise ValueError("至少需要 1 个转写结果")
    base = texts[0]
    n = len(texts)

    if n == 1 or not base:
        seg = Segment(text=base, agree=n, total=n, confidence=1.0)
        return CrossCheckResult(merged=base, segments=[seg])

    # 差分数组：base 每个字符位置被多少个「其他引擎」支持
    length = len(base)
    diff = [0] * (length + 1)
    for t in texts[1:]:
        if not t:
            continue
        sm = SequenceMatcher(None, base, t, autojunk=False)
        for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
            if tag == "equal":
                diff[i1] += 1
                diff[i2] -= 1

    # 前缀和 → 每个位置的支持数（0 ~ n-1）
    support = [0] * length
    cur = 0
    for i in range(length):
        cur += diff[i]
        support[i] = cur

    # 扫描出支持数相同的连续段
    segments: List[Segment] = []
    i = 0
    while i < length:
        j = i
        s = support[i]
        while j < length and support[j] == s:
            j += 1
        agree = s + 1  # 加上基准自身
        segments.append(Segment(
            text=base[i:j],
            agree=agree,
            total=n,
            confidence=round(agree / n, 4),
        ))
        i = j

    return CrossCheckResult(merged=base, segments=segments)


def confidence_report(result: CrossCheckResult) -> str:
    """生成一段可读的交叉验证报告。"""
    lines = [f"整体置信度：{result.overall_confidence:.0%}"]
    lines.append(f"片段总数：{len(result.segments)}，"
                 f"低置信片段：{len(result.low_confidence_segments)} 个")
    for seg in result.conflict_segments:
        tag = "低置信" if seg.is_low_confidence else "有分歧"
        snippet = seg.text[:20] + ("…" if len(seg.text) > 20 else "")
        lines.append(f"  [{tag}] ({seg.agree}/{seg.total} 引擎一致) {snippet}")
    return "\n".join(lines)
