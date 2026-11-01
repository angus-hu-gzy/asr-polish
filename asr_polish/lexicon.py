"""词表加载与管理模块。

加载术语订正词表（JSON），对外提供统一查询接口。
纯标准库实现，无第三方依赖。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

# 默认词表路径：包内 data/corrections.json（只读基线）
DEFAULT_LEXICON_PATH = Path(__file__).parent / "data" / "corrections.json"

# 用户词表路径：`lexicon import` 写入此处，避免写只读的 site-packages 包目录
USER_LEXICON_PATH = Path.home() / ".config" / "asr-polish" / "lexicon.json"


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
            user_entries = self._load(USER_LEXICON_PATH)
            self.entries = self._merge_entries(self.entries, user_entries)
        self._variant_map: dict[str, str] = self._build_variant_map()

    def _load(self, path: str | Path) -> list[dict[str, Any]]:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("entries", [])

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
