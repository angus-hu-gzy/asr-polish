"""多引擎交叉验证：对同一段音频的多个转写结果做序列对齐，输出片段级置信度。

原理：
- 以第一个转写为基准，用 difflib.SequenceMatcher 与其余每个转写做 token 级对齐
  （英文单词/缩写作为一个 token，中文逐字，避免英文单词被字符级对齐切碎）。
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
from typing import List, Tuple

# base token 数超过此阈值时才考虑 autojunk（避免正常长文本也损失对齐精度）
_AUTOJUNK_THRESHOLD = 1000
# token 去重占比低于此值视为「高度重复」（引擎卡死的典型特征）
_REPETITIVE_RATIO = 0.1


@dataclass
class Segment:
    """一个对齐后的片段。"""
    text: str            # 基准文本片段
    agree: int           # 同意该片段的引擎数（含基准自身）
    total: int           # 参与交叉验证的引擎总数
    confidence: float    # 0~1，同意占比

    @property
    def is_low_confidence(self) -> bool:
        # <= 0.5：两引擎各执一词时 confidence 恰为 0.5，应算低置信，
        # 避免「字符一致率 65%」与「低置信片段 0 个」互相矛盾。
        return self.confidence <= 0.5

    @property
    def is_conflict(self) -> bool:
        """是否「各引擎说法不一」（基准被部分引擎支持）。"""
        return self.agree < self.total


@dataclass
class CrossCheckResult:
    """交叉验证结果。"""
    base_text: str                        # 基准文本（第一个输入转写，作为对齐基准）
    segments: List[Segment] = field(default_factory=list)

    @property
    def merged(self) -> str:
        """兼容旧字段名：即基准文本（并非多引擎融合结果，命名仅为历史遗留）。"""
        return self.base_text

    @property
    def low_confidence_segments(self) -> List[Segment]:
        return [s for s in self.segments if s.is_low_confidence]

    @property
    def conflict_segments(self) -> List[Segment]:
        return [s for s in self.segments if s.is_conflict]

    @property
    def overall_confidence(self) -> float:
        """整体一致率 = 各段置信度的加权平均（按字符数加权）。

        注意：这是「按字符加权的片段平均一致度」，衡量多引擎有多一致，
        不是「内容正确率」——两个引擎可能同时错成同一个词。
        """
        total_chars = sum(len(s.text) for s in self.segments)
        if total_chars == 0:
            return 1.0
        weighted = sum(s.confidence * len(s.text) for s in self.segments)
        return round(weighted / total_chars, 4)


def _tokenize(s: str) -> List[Tuple[int, int, str]]:
    """把文本切分为 token，返回 (char_start, char_end, token_text)。

    - 连续 ASCII 字母/数字（英文单词、缩写、数字）作为一个 token
    - 其余（中文单字、标点、空格）每个字符一个 token

    这样英文单词作为一个整体参与对齐，不会被字符级对齐切成「ython」之类的碎片。
    """
    tokens: List[Tuple[int, int, str]] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c.isascii() and c.isalnum():
            j = i
            while j < n and s[j].isascii() and s[j].isalnum():
                j += 1
            tokens.append((i, j, s[i:j]))
            i = j
        else:
            tokens.append((i, i + 1, c))
            i += 1
    return tokens


def _should_use_autojunk(tokens: List[str]) -> bool:
    """仅在「文本既长又高度重复」时启用 autojunk。

    正常长文本（token 去重占比较高）即使不开 autojunk 也很快（实测 1 万字约 0.6s）
    且对齐更精确；只有「引擎卡死导致大段重复」这类文本才会触发近似平方级退化，
    此时用 autojunk 跳过高频 token 以避免卡死，代价是重复区域的对齐变粗——而这类
    文本本就会被 quality 的重复检测标记，可接受。
    """
    n = len(tokens)
    if n <= _AUTOJUNK_THRESHOLD:
        return False
    return len(set(tokens)) / n < _REPETITIVE_RATIO


def cross_check(*texts: str) -> CrossCheckResult:
    """对 2 个及以上转写结果做交叉验证。第一个参数为基准文本。

    空文本会被剔除：一个「没产出任何内容」的引擎不应被算成「反对一切」，
    否则 `cross_check("今天讲SQL", "")` 会把一致率腰斩成 0.5。
    """
    texts = [t for t in texts if t]
    if not texts:
        raise ValueError("至少需要 1 个非空转写结果")
    base = texts[0]
    n = len(texts)

    if n == 1:
        seg = Segment(text=base, agree=n, total=n, confidence=1.0)
        return CrossCheckResult(base_text=base, segments=[seg])

    # 差分数组：base 每个字符位置被多少个「其他引擎」支持
    length = len(base)
    diff = [0] * (length + 1)
    base_tokens = _tokenize(base)
    base_token_texts = [tok[2] for tok in base_tokens]
    for t in texts[1:]:
        if not t:
            continue
        other_token_texts = [tok[2] for tok in _tokenize(t)]
        autojunk = (_should_use_autojunk(base_token_texts)
                    or _should_use_autojunk(other_token_texts))
        sm = SequenceMatcher(None, base_token_texts, other_token_texts, autojunk=autojunk)
        for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
            if tag == "equal" and i2 > i1:
                # 这段 token 对应 base 的字符区间 [char_start, char_end)
                char_start = base_tokens[i1][0]
                char_end = base_tokens[i2 - 1][1]
                diff[char_start] += 1
                diff[char_end] -= 1

    # 前缀和 → 每个位置的支持数（0 ~ n-1）
    support = [0] * length
    cur = 0
    for i in range(length):
        cur += diff[i]
        support[i] = cur

    # 扫描出支持数相同的连续段（每段已是「最大同支持区间」，
    # 相邻段支持数必然不同，短分歧段保留独立，不再塞进前一段以避免定位失真）
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

    return CrossCheckResult(base_text=base, segments=segments)


def confidence_report(result: CrossCheckResult) -> str:
    """生成一段可读的交叉验证报告。"""
    lines = [f"字符一致率：{result.overall_confidence:.0%}"]
    lines.append(f"片段总数：{len(result.segments)}，"
                 f"低置信片段：{len(result.low_confidence_segments)} 个")
    for seg in result.conflict_segments:
        tag = "低置信" if seg.is_low_confidence else "有分歧"
        snippet = seg.text[:20] + ("…" if len(seg.text) > 20 else "")
        lines.append(f"  [{tag}] ({seg.agree}/{seg.total} 引擎一致) {snippet}")
    return "\n".join(lines)
