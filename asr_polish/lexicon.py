"""词表加载与管理模块。

加载术语订正词表（JSON），对外提供统一查询接口。
纯标准库实现，无第三方依赖。
"""
from __future__ import annotations

import json
import os
import re
import sys
import warnings
from pathlib import Path
from typing import Any, Optional

# 默认词表路径：包内 data/corrections.json（只读基线）
DEFAULT_LEXICON_PATH = Path(__file__).parent / "data" / "corrections.json"


def _user_config_dir() -> Path:
    """用户词表所在目录：Windows 用 %APPDATA%，POSIX 尊重 XDG_CONFIG_HOME。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "asr-polish"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "asr-polish"


# 用户词表路径：`lexicon import` 写入此处，避免写只读的 site-packages 包目录
USER_LEXICON_PATH = _user_config_dir() / "lexicon.json"

# 当前支持的词表格式版本；加载到更高版本时提示（不阻断，尽量向后兼容）
_SUPPORTED_LEXICON_VERSION = 2


class Lexicon:
    """术语订正词表。

    词表结构：
        {
          "version": 1,
          "entries": [
            {"correct": "SQL", "category": "编程语言",
             "variants": ["色考", "色口"], "note": "..."},
            ...
          ]
        }

    默认加载包内词表；若存在用户词表（USER_LEXICON_PATH），则按 correct 合并
    用户条目（用户条目优先，可覆盖包内条目）。
    """

    def __init__(self, path: Optional[str | Path] = None, include_user: bool = True):
        self.path = Path(path) if path else DEFAULT_LEXICON_PATH
        self.entries: list[dict[str, Any]] = self._load(self.path)
        if not path and include_user and USER_LEXICON_PATH.exists():
            try:
                user_entries = self._load(USER_LEXICON_PATH)
                self.entries = self._merge_entries(self.entries, user_entries)
            except Exception:  # 用户词表损坏时降级：忽略并告警，不拖垮整个 import
                warnings.warn(f"用户词表损坏，已忽略：{USER_LEXICON_PATH}", stacklevel=2)
        self._variant_map: dict[str, str] = self._build_variant_map()

    def _load(self, path: str | Path) -> list[dict[str, Any]]:
        # utf-8-sig 兼容 Windows 记事本「UTF-8 带 BOM」的常见情况
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        version = data.get("version", 1)
        if isinstance(version, int) and version > _SUPPORTED_LEXICON_VERSION:
            warnings.warn(
                f"词表版本 v{version} 高于当前支持的 v{_SUPPORTED_LEXICON_VERSION}，"
                f"部分字段可能被忽略：{path}", stacklevel=3)
        return [self._sanitize(e) for e in data.get("entries", [])]

    @staticmethod
    def _sanitize(entry: dict[str, Any]) -> dict[str, Any]:
        """规范化单条词目：列表字段容错、guards 正则预校验。

        - `context` / `strong_context` / `high_variants` / `medium_variants` 若被写成
          字符串，按常见分隔符拆成列表（否则会被逐字迭代，导致上下文误命中）；
        - `guards` 里的正则非法时丢弃并告警，避免运行时在 `correct()` 里抛
          `re.error` 把整条流水线打断。
        """
        entry = dict(entry)
        for key in ("high_variants", "medium_variants", "context", "strong_context"):
            val = entry.get(key)
            if isinstance(val, str):
                entry[key] = [p.strip() for p in re.split(r"[,，、;；|]", val) if p.strip()]
            elif isinstance(val, (list, tuple)):
                entry[key] = [str(v).strip() for v in val if str(v).strip()]

        guards = entry.get("guards")
        if isinstance(guards, dict):
            valid: dict[str, str] = {}
            for variant, guard in guards.items():
                guard = str(guard or "")
                if not guard:
                    continue
                try:
                    re.compile("(?!" + guard + ")")
                except re.error as exc:
                    warnings.warn(
                        f"词表 guards 正则非法，已忽略：{entry.get('correct')}/{variant}"
                        f"（{guard!r}: {exc}）", stacklevel=2)
                    continue
                valid[str(variant)] = guard
            entry["guards"] = valid
        return entry

    @staticmethod
    def _merge_entries(
        base: list[dict[str, Any]], user: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """用户词表条目按 correct 合并进基础词表（用户条目优先，变体去重合并）。"""
        by_correct: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for e in base:
            by_correct[e["correct"]] = dict(e)
            order.append(e["correct"])
        for e in user:
            c = e["correct"]
            if c in by_correct:
                merged = by_correct[c]
                vset = list(merged.get("variants", []))
                for v in e.get("variants", []):
                    if v not in vset:
                        vset.append(v)
                for k, v in e.items():
                    if k != "variants":
                        merged[k] = v
            else:
                by_correct[c] = dict(e)
                order.append(c)
        return [by_correct[c] for c in order]

    def _build_variant_map(self) -> dict[str, str]:
        """变体（小写归一）→ 正确词。用于快速反查。"""
        mapping: dict[str, str] = {}
        for entry in self.entries:
            for variant in entry.get("variants", []):
                mapping[variant.lower()] = entry["correct"]
        return mapping

    @property
    def correct_terms(self) -> list[str]:
        """所有正确术语，按词表顺序。"""
        return [e["correct"] for e in self.entries]

    @property
    def all_variants(self) -> list[tuple[str, str]]:
        """(变体, 正确词) 列表，按变体长度降序（长词优先）。"""
        pairs: list[tuple[str, str]] = []
        for entry in self.entries:
            correct = entry["correct"]
            for variant in entry.get("variants", []):
                pairs.append((variant, correct))
        pairs.sort(key=lambda x: len(x[0]), reverse=True)
        return pairs

    @staticmethod
    def _is_ascii(variant: str) -> bool:
        return all(ord(ch) < 128 for ch in variant)

    @classmethod
    def _is_short_cjk(cls, variant: str) -> bool:
        """短中文变体（≤2 字）。

        这类变体与正常中文词的碰撞率极高（全占/给特/色口 都能出现在日常句子里），
        因此**不允许**被条目级 `confidence: high` 顺带升级，必须在 `high_variants`
        里逐个显式声明「确认安全」。
        """
        return len(variant) <= 2 and not cls._is_ascii(variant)

    def resolve_confidence(self, entry: dict[str, Any], variant: str) -> str:
        """解析单个变体的置信度：变体级白名单 > 条目级 > 规则默认。

        规则默认：ASCII 变体（有词边界保护）→ high；中文变体 → medium。
        条目级 `confidence: high` 只对 ASCII 变体与 3 字及以上中文变体生效；
        ≤2 字的中文变体即使条目写了 high，也按 medium 处理——安全的中文短变体
        （如「色考」）必须用 `high_variants` 逐个升级。
        """
        high = entry.get("high_variants") or []
        if variant in high:
            return "high"
        medium = entry.get("medium_variants") or []
        if variant in medium:
            return "medium"
        conf = str(entry.get("confidence") or "").strip().lower()
        if conf in ("high", "medium", "low"):
            if conf == "high" and self._is_short_cjk(variant):
                return "medium"
            return conf
        return "high" if self._is_ascii(variant) else "medium"

    @property
    def all_variants_with_confidence(self) -> list[tuple[str, str, str]]:
        """(变体, 正确词, 置信度) 列表，按变体长度降序。供质量自评等按置信度过滤。"""
        pairs: list[tuple[str, str, str]] = []
        for entry in self.entries:
            correct = entry["correct"]
            for variant in entry.get("variants", []):
                pairs.append((variant, correct, self.resolve_confidence(entry, variant)))
        pairs.sort(key=lambda x: len(x[0]), reverse=True)
        return pairs

    def context_words(self, entry: dict[str, Any]) -> list[str]:
        """medium 变体的弱上下文佐证词；未配置时返回空列表（由调用方回退到类别）。"""
        return [str(w) for w in (entry.get("context") or []) if str(w)]

    def strong_context(self, entry: dict[str, Any]) -> list[str]:
        """medium 变体的强上下文佐证词（命中 1 个即可）。"""
        return [str(w) for w in (entry.get("strong_context") or []) if str(w)]

    @property
    def category_terms(self) -> dict[str, list[str]]:
        """类别 → 该类别下的正确术语（去重）。medium 变体的弱上下文回退来源。"""
        mapping: dict[str, list[str]] = {}
        for entry in self.entries:
            cat = entry.get("category", "")
            if not cat:
                continue
            mapping.setdefault(cat, [])
            if entry["correct"] not in mapping[cat]:
                mapping[cat].append(entry["correct"])
        return mapping

    def lookup_variant(self, text: str) -> Optional[str]:
        """若 text 是某个变体，返回对应正确词；否则 None。"""
        return self._variant_map.get(text.lower())

    def categories(self) -> list[str]:
        """所有类别，按首次出现顺序去重。"""
        seen: list[str] = []
        for entry in self.entries:
            cat = entry.get("category", "未分类")
            if cat not in seen:
                seen.append(cat)
        return seen
