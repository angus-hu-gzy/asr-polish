"""asr-polish —— B站字幕批量抓取 + 本地任务队列服务，附带中文转写文本加工。

顶层是「把字幕拿下来」：单条取文本、按 UP 主 / 合集 / 搜索批量抓、起服务排队跑，
内置按域名限速，避免把账号抓进风控。

抓取链路：
- fetch      : 从 B站视频链接取文本（字幕轨优先，无字幕走必剪云端 ASR）
- subs       : 批量抓字幕，导出 txt / srt / json / md
- serve      : 把批量抓取变成任务队列，页面关掉任务也不丢
- mcp        : 同一套抓取包成 MCP 工具，agent 在对话里直接调（stdio，零依赖）

文本加工（可单独使用，不依赖抓取）：
- correction : 术语订正引擎 —— 修正 ASR 把 SQL/Flask/Python 等术语转成的同音错字
- crosscheck : 多引擎交叉验证 —— 对齐多个转写结果，标出低置信片段
- quality    : 转写质量自评 —— 四维打分，一眼看出这段文字能不能信

全部纯标准库实现，零第三方依赖；仅「无字幕 → 必剪 ASR」需要可选依赖 yt-dlp。
"""
from . import lexicon_io
from .lexicon import Lexicon, DEFAULT_LEXICON_PATH, USER_LEXICON_PATH
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
from .bili import (
    fetch,
    FetchResult,
    BiliError,
)

__version__ = "0.19.0"

__all__ = [
    "Lexicon",
    "DEFAULT_LEXICON_PATH",
    "USER_LEXICON_PATH",
    "lexicon_io",
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
    "fetch",
    "FetchResult",
    "BiliError",
    "__version__",
]
