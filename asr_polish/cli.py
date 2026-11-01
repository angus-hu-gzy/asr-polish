"""asr-polish 命令行入口。

用法：
    asr-polish correct "今天讲色考和PASS"           # 术语订正
    asr-polish assess "转写文本" --duration 1316    # 质量打分
    asr-polish crosscheck a.txt b.txt               # 交叉验证两个转写
    asr-polish lexicon import 词表.txt             # 批量导入词表
    asr-polish lexicon list                        # 列出词表
    asr-polish lexicon export --out out.json       # 导出词表
    asr-polish version                             # 显示版本

纯标准库实现（argparse），无第三方依赖。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .correction import correct_text
from .crosscheck import cross_check, confidence_report
from .quality import assess
from .lexicon import Lexicon, DEFAULT_LEXICON_PATH
from . import lexicon_io


def _cmd_correct(args: argparse.Namespace) -> None:
    result = correct_text(args.text)
    print(result.text)
    if args.verbose:
        print("\n" + result.summary(), file=sys.stderr)


def _cmd_assess(args: argparse.Namespace) -> None:
    report = assess(args.text, duration_seconds=args.duration)
    print(report.summary())


def _cmd_crosscheck(args: argparse.Namespace) -> None:
    a = Path(args.file1).read_text(encoding="utf-8")
    b = Path(args.file2).read_text(encoding="utf-8")
    print(confidence_report(cross_check(a, b)))


def _cmd_lexicon_import(args: argparse.Namespace) -> None:
    incoming = lexicon_io.load_entries_from_file(args.file, args.format)
    if not incoming:
        print(f"未从 {args.file} 解析到任何有效条目", file=sys.stderr)
        sys.exit(1)
    out = args.out or str(DEFAULT_LEXICON_PATH)
    added, merged = lexicon_io.import_into_lexicon_file(out, incoming)
    total = len(lexicon_io.load_lexicon_data(out).get("entries", []))
    print(f"解析到 {len(incoming)} 条 → 新增 {added} 条，合并 {merged} 条")
    print(f"词表现有 {total} 条，已写入：{out}")


def _cmd_lexicon_export(args: argparse.Namespace) -> None:
    out = lexicon_io.export_lexicon(DEFAULT_LEXICON_PATH, args.out or "corrections.json")
    print(f"词表已导出到 {out}")


def _cmd_lexicon_list(args: argparse.Namespace) -> None:
    lex = Lexicon()
    print(f"共 {len(lex.entries)} 条，{len(lex.categories())} 个类别：{lex.categories()}\n")
    for i, e in enumerate(lex.entries, 1):
        variants = "；".join(e.get("variants", []))
        print(f"{i:>3}. {e['correct']:<20} [{e.get('category', '未分类')}]  {variants}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asr-polish",
        description="中文语音转写（ASR）质量打磨工具：术语订正 / 交叉验证 / 质量自评 / 词表管理",
    )
    parser.add_argument("--version", action="version", version=f"asr-polish {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_correct = sub.add_parser("correct", help="订正一段文本的术语错字")
    p_correct.add_argument("text", help="待订正的转写文本")
    p_correct.add_argument("-v", "--verbose", action="store_true", help="在 stderr 输出订正明细")
    p_correct.set_defaults(func=_cmd_correct)

    p_assess = sub.add_parser("assess", help="给一段转写文本打分（0~100）")
    p_assess.add_argument("text", help="待评估的转写文本")
    p_assess.add_argument("--duration", type=float, default=None, help="音频时长（秒），用于计算字符密度")
    p_assess.set_defaults(func=_cmd_assess)

    p_cc = sub.add_parser("crosscheck", help="交叉验证两个转写文本文件")
    p_cc.add_argument("file1", help="基准转写文本文件")
    p_cc.add_argument("file2", help="另一引擎的转写文本文件")
    p_cc.set_defaults(func=_cmd_crosscheck)

    p_lex = sub.add_parser("lexicon", help="词表管理")
    lex_sub = p_lex.add_subparsers(dest="lex_cmd")

    p_imp = lex_sub.add_parser("import", help="从文件批量导入词表（JSON/CSV/TXT）")
    p_imp.add_argument("file", help="待导入的文件路径")
    p_imp.add_argument("--out", default=None, help="输出词表文件（默认写入内置词表）")
    p_imp.add_argument("--format", choices=["auto", "json", "csv", "txt"], default="auto", help="输入格式（默认按扩展名/内容自动识别）")
    p_imp.set_defaults(func=_cmd_lexicon_import)

    p_exp = lex_sub.add_parser("export", help="导出当前内置词表为 JSON")
    p_exp.add_argument("--out", default=None, help="输出文件路径（默认 corrections.json）")
    p_exp.set_defaults(func=_cmd_lexicon_export)

    p_ls = lex_sub.add_parser("list", help="列出内置词表全部条目")
    p_ls.set_defaults(func=_cmd_lexicon_list)

    return parser


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
