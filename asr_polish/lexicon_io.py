"""词表批量导入 / 导出工具。

解决「词表维护」这个痛点：默认词表只有几十条，想覆盖自己的领域
（医疗、金融、法律……）就得一条条手写 JSON，既慢又容易写错格式。

本模块提供三种人类友好的批量格式（自动识别），导入后按 `correct`
去重合并，写回 JSON 时保留词表元信息（name / description / version）。

支持的输入格式：
- JSON : {"entries": [...]} 或 [...]，即标准词表结构
- CSV  : 表头 `correct,category,variants,note`，variants 用 `|` / `;` / `、` 分隔
- TXT  : 每行一条 `正确词 = 变体1, 变体2`（分隔符支持 `=` `->` `=>` `:` `：`），
         `#` 开头为注释，空行跳过

纯标准库实现，无第三方依赖。
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any, Optional

from .lexicon import DEFAULT_LEXICON_PATH

# 变体字段内的分隔符（CSV/TXT 里变体之间用这些隔开）
# 注意：不能用空格作分隔符——变体本身可能含空格（如口语化缩写 "h t t p"）。
_VARIANT_SPLIT_RE = re.compile(r"[,、;|，]+")

# TXT 每行的「正确词 <分隔符> 变体列表」分隔符
_LINE_SPLIT_RE = re.compile(r"\s*(?:->|=>|=|：|:)\s*")


def _clean_variants(raw: str) -> list[str]:
    """把变体字符串按常见分隔符拆开并去重、去空白。"""
    seen: list[str] = []
    for part in _VARIANT_SPLIT_RE.split(raw):
        part = part.strip()
        if part and part not in seen:
            seen.append(part)
    return seen


def normalize_entry(item: Any) -> Optional[dict[str, Any]]:
    """把一条原始条目规范化为标准结构；无效则返回 None。"""
    if not isinstance(item, dict):
        return None
    correct = str(item.get("correct") or "").strip()
    if not correct:
        return None

    variants = item.get("variants", [])
    if isinstance(variants, str):
        variants = _clean_variants(variants)
    variants = [str(v).strip() for v in variants if str(v).strip()]

    entry: dict[str, Any] = {"correct": correct}
    category = str(item.get("category") or "").strip()
    if category:
        entry["category"] = category
    if variants:
        entry["variants"] = variants
    note = str(item.get("note") or "").strip()
    if note:
        entry["note"] = note
    return entry


def parse_json(text: str) -> list[dict[str, Any]]:
    data = json.loads(text.lstrip("\ufeff"))
    if isinstance(data, dict):
        data = data.get("entries", [])
    entries = []
    for item in data:
        e = normalize_entry(item)
        if e:
            entries.append(e)
    return entries


def parse_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        return []
    entries = []
    for row in reader:
        e = normalize_entry(row)
        if e:
            entries.append(e)
    return entries


def parse_txt(text: str) -> list[dict[str, Any]]:
    entries = []
    for line in text.lstrip("\ufeff").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = _LINE_SPLIT_RE.split(line, maxsplit=1)
        if len(parts) != 2:
            continue
        correct = parts[0].strip()
        variants = _clean_variants(parts[1])
        if not correct or not variants:
            continue
        entries.append({"correct": correct, "variants": variants})
    return entries


def parse_entries(text: str, fmt: Optional[str] = None) -> list[dict[str, Any]]:
    """按指定格式（或自动识别）解析条目。"""
    fmt = (fmt or "auto").lower()
    if fmt == "json":
        return parse_json(text)
    if fmt == "csv":
        return parse_csv(text)
    if fmt == "txt":
        return parse_txt(text)
    # 自动识别：JSON 以 { / [ 开头；CSV 首行以 correct 开头；否则按 TXT
    stripped = text.lstrip("\ufeff").lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return parse_json(text)
    if stripped.lower().startswith("correct"):
        return parse_csv(text)
    return parse_txt(text)


def load_entries_from_file(path: str | Path, fmt: Optional[str] = None) -> list[dict[str, Any]]:
    """从文件读取条目，按扩展名或指定格式解析。"""
    p = Path(path)
    if fmt is None:
        suffix = p.suffix.lower().lstrip(".")
        if suffix in ("json", "csv", "txt"):
            fmt = suffix
    return parse_entries(p.read_text(encoding="utf-8-sig"), fmt)


def merge_entries(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    """合并两批条目，返回 (合并后条目, 新增数, 合并数)。

    按 `correct` 判重：已存在则合并 variants（去重）并更新 category/note，
    不存在则追加。保持原有顺序，新条目追加到末尾。
    """
    by_correct: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for e in existing:
        c = e["correct"]
        by_correct[c] = dict(e)
        order.append(c)

    added = merged = 0
    for inc in incoming:
        c = inc["correct"]
        if c in by_correct:
            base = by_correct[c]
            vset = list(base.get("variants", []))
            for v in inc.get("variants", []):
                if v not in vset:
                    vset.append(v)
            if vset:
                base["variants"] = vset
            if inc.get("category"):
                base["category"] = inc["category"]
            if inc.get("note"):
                base["note"] = inc["note"]
            merged += 1
        else:
            by_correct[c] = dict(inc)
            order.append(c)
            added += 1

    return [by_correct[c] for c in order], added, merged


def load_lexicon_data(path: str | Path) -> dict[str, Any]:
    """读取词表文件的完整 dict（含 meta 与 entries）。"""
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def import_into_lexicon_file(
    lexicon_path: str | Path,
    incoming_entries: list[dict[str, Any]],
) -> tuple[int, int]:
    """把新条目合并进一个词表文件，就地写回。返回 (新增数, 合并数)。

    目标文件不存在时，自动初始化为空词表后导入（用于「导入到新文件」）。
    """
    p = Path(lexicon_path)
    if p.exists():
        data = load_lexicon_data(p)
    else:
        data = {
            "version": 1,
            "name": "自定义术语订正词表",
            "description": "由 asr-polish 批量导入生成。",
            "entries": [],
        }
    existing = data.get("entries", [])
    merged_entries, added, merged = merge_entries(existing, incoming_entries)
    data["entries"] = merged_entries
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return added, merged


def export_lexicon(
    lexicon_path: str | Path = DEFAULT_LEXICON_PATH,
    out_path: str | Path = "corrections.json",
) -> str:
    """把词表导出为 JSON 文件，返回输出路径。"""
    data = load_lexicon_data(lexicon_path)
    out = Path(out_path)
    out.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return str(out)
