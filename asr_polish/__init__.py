"""asr-polish —— 中文语音转写（ASR）质量打磨工具。

把「转写出来的文字」变得可信：订正术语错字、交叉验证多引擎结果、自动打分。

三个核心能力：
- correction : 术语订正引擎 —— 修正 ASR 把 SQL/Flask/Python 等术语转成的同音错字
- crosscheck : 多引擎交叉验证 —— 对齐多个转写结果，标出低置信片段
- quality    : 转写质量自评 —— 五维打分，一眼看出这段文字能不能信

纯标准库实现，零第三方依赖。
"""
from .lexicon import Lexicon
from .correction import (
    Corrector,
    Correction,
    CorrectionResult,
    correct_text,
)
from .crosscheck import (
    cross_check,
    CrossCheckResult,
    Segment,
    confidence_report,
)
from .quality import (
    assess,
    QualityReport,
    Dimension,
)

__version__ = "0.1.0"

__all__ = [
    "Lexicon",
    "Corrector",
    "Correction",
    "CorrectionResult",
    "correct_text",
    "cross_check",
    "CrossCheckResult",
    "Segment",
    "confidence_report",
    "assess",
    "QualityReport",
    "Dimension",
    "__version__",
]
