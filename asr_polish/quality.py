"""转写质量自评：对单个转写结果做五维打分。

维度：
1. 乱码检测   —— 不可打印字符 / 替换字符 U+FFFD / 控制字符占比（硬伤，权重最高）
2. 重复片段   —— 连续重复的短语（口吃式重复、引擎卡死式重复）
3. 可疑同音字 —— 词表中「错误变体」的出现次数（说明可能存在未订正的错字）
4. 术语可识别 —— 词表中「正确术语」的命中数（正向信号，越多越可信）
5. 字符密度   —— 字/秒（需提供时长，中文正常约 3~6 字/秒）

输出 0~100 总分 + 各维度明细 + 低置信片段位置。
纯标准库实现，无第三方依赖。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .lexicon import Lexicon


@dataclass
class Dimension:
    """一个评分维度。"""
    name: str            # 维度名
    metric: float        # 原始指标值
    unit: str            # 指标单位说明
    score: float         # 0~100
    weight: float        # 权重（0~1）

    def describe(self) -> str:
        return f"{self.name}: {self.metric:.2f}{self.unit} → {self.score:.0f}分"


@dataclass
class QualityReport:
    """质量评估结果。"""
    score: float                                   # 0~100 总分
    text: str = ""                                 # 被评估的原文（供摘要回显片段）
    dimensions: List[Dimension] = field(default_factory=list)
    low_confidence_spans: List[Tuple[int, int, str]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"转写质量评分：{self.score:.0f} / 100"]
        for d in self.dimensions:
            lines.append(f"  {d.describe()}")
        if self.low_confidence_spans:
            lines.append(f"  低置信片段 {len(self.low_confidence_spans)} 处：")
            for start, end, reason in self.low_confidence_spans[:10]:
                snippet = self.text[start:end]
                if len(snippet) > 24:
                    snippet = snippet[:24] + "…"
                lines.append(f"    [{reason}] ({start}-{end})「{snippet}」")
        return "\n".join(lines)


# ── 各维度检测函数 ──────────────────────────────

# 控制字符（除 \t \n \r）与替换字符
_GARBLED_RE = re.compile(r"[\ufffd\x00-\x08\x0b\x0c\x0e-\x1f]")

# 连续重复：任意 2+ 字符，重复 3 次及以上
_REPEAT_RE = re.compile(r"(.{2,})\1{2,}")


def _detect_garbled(text: str) -> Tuple[float, List[Tuple[int, int, str]]]:
    """返回 (乱码率, 乱码位置列表)。"""
    if not text:
        return 0.0, []
    spans = [(m.start(), m.end(), "乱码") for m in _GARBLED_RE.finditer(text)]
    ratio = sum(e - s for s, e, _ in spans) / len(text)
    return ratio, spans


def _detect_repetition(text: str) -> Tuple[float, List[Tuple[int, int, str]]]:
    """返回 (重复率, 重复片段位置)。"""
    if not text:
        return 0.0, []
    spans = [(m.start(), m.end(), "重复") for m in _REPEAT_RE.finditer(text)]
    ratio = sum(e - s for s, e, _ in spans) / len(text)
    return ratio, spans


def _detect_suspicious_variants(
    text: str, lexicon: Lexicon
) -> Tuple[int, List[Tuple[int, int, str]]]:
    """返回 (可疑变体次数, 位置列表)。"""
    spans: List[Tuple[int, int, str]] = []
    for variant, correct in lexicon.all_variants:
        flags = re.IGNORECASE if all(ord(c) < 128 for c in variant) else 0
        for m in re.finditer(re.escape(variant), text, flags):
            spans.append((m.start(), m.end(), f"疑似「{correct}」"))
    return len(spans), spans


def _count_correct_terms(text: str, lexicon: Lexicon) -> int:
    """统计正确术语命中数（不重叠计数）。"""
    count = 0
    for term in lexicon.correct_terms:
        count += text.count(term)
    return count


def _density_score(density: float) -> float:
    """字符密度 → 0~100 分。中文正常语速约 3~6 字/秒。"""
    if density <= 0:
        return 0.0
    if density < 1:
        return 20.0                      # 大量漏识
    if density < 2.5:
        return 60.0                      # 偏稀疏
    if density <= 6:
        return 100.0                     # 正常
    if density <= 8:
        return 70.0                      # 偏快/轻微重复
    if density <= 12:
        return 40.0                      # 明显重复
    return 10.0                          # 严重重复/错乱


def assess(
    text: str,
    duration_seconds: Optional[float] = None,
    lexicon: Lexicon | None = None,
) -> QualityReport:
    """评估转写质量，返回 0~100 总分与明细。"""
    if not text:
        return QualityReport(score=0.0, text=text)

    lexicon = lexicon or Lexicon()
    spans: List[Tuple[int, int, str]] = []

    # 1. 乱码（硬伤）
    garbled_ratio, g_spans = _detect_garbled(text)
    spans.extend(g_spans)
    garbled_score = max(0.0, 100.0 - garbled_ratio * 1000.0)

    # 2. 重复
    repeat_ratio, r_spans = _detect_repetition(text)
    spans.extend(r_spans)
    repeat_score = max(0.0, 100.0 - repeat_ratio * 400.0)

    # 3. 可疑同音字
    variant_count, v_spans = _detect_suspicious_variants(text, lexicon)
    spans.extend(v_spans)
    variant_score = max(0.0, 100.0 - variant_count * 15.0)

    # 4. 术语可识别度（正向）
    term_hits = _count_correct_terms(text, lexicon)
    term_score = min(100.0, term_hits * 8.0)

    # 组装维度与权重
    dimensions: List[Dimension] = []
    dimensions.append(Dimension("乱码检测", garbled_ratio, "%", garbled_score, 0.25))
    dimensions.append(Dimension("重复片段", repeat_ratio, "%", repeat_score, 0.25))
    dimensions.append(Dimension("可疑同音字", float(variant_count), "处", variant_score, 0.20))
    dimensions.append(Dimension("术语可识别", float(term_hits), "处", term_score, 0.15))

    # 5. 字符密度（需时长）
    if duration_seconds and duration_seconds > 0:
        density = len(text) / duration_seconds
        dimensions.append(Dimension("字符密度", density, "字/秒",
                                    _density_score(density), 0.15))
    else:
        # 无时长：把密度权重均摊到前四项
        for d in dimensions:
            d.weight += 0.15 / 4

    # 加权总分
    total_weight = sum(d.weight for d in dimensions)
    score = sum(d.score * d.weight for d in dimensions) / total_weight
    score = round(score, 1)

    # 硬伤封顶：乱码或重复严重时，总分不应及格
    if garbled_ratio >= 0.05 or repeat_ratio >= 0.3:
        score = min(score, 50.0)

    # 低置信片段按位置排序去重
    spans.sort(key=lambda x: x[0])
    return QualityReport(score=score, text=text, dimensions=dimensions, low_confidence_spans=spans)
