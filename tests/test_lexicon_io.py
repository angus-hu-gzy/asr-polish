"""词表批量导入/导出模块的单元测试。

运行：python tests/test_lexicon_io.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from asr_polish import lexicon_io


def test_parse_txt():
    txt = """
# 注释行
SQL = 色考, 色口
Python = PASS, 派森
HTTPS -> h t t p s, 埃去替替匹s
"""
    entries = lexicon_io.parse_txt(txt)
    assert len(entries) == 3
    assert entries[0] == {"correct": "SQL", "variants": ["色考", "色口"]}
    assert entries[1]["correct"] == "Python"
    assert entries[2]["variants"] == ["h t t p s", "埃去替替匹s"]


def test_parse_csv():
    csv_text = 'correct,category,variants,note\nVue,框架工具,"v u e|维优",前端框架\nGPT,AI术语,"g p t|吉皮提",大语言模型\n'
    entries = lexicon_io.parse_csv(csv_text)
    assert len(entries) == 2
    assert entries[0]["correct"] == "Vue"
    assert entries[0]["category"] == "框架工具"
    assert entries[0]["variants"] == ["v u e", "维优"]
    assert entries[0]["note"] == "前端框架"


def test_parse_json():
    entries = lexicon_io.parse_json('[{"correct":"K8s","variants":["k 8 s","库伯内特斯"]}]')
    assert len(entries) == 1
    assert entries[0]["correct"] == "K8s"
    # dict 形式（含 entries 键）
    entries2 = lexicon_io.parse_json('{"entries":[{"correct":"Vue","variants":["维优"]}]}')
    assert entries2[0]["correct"] == "Vue"


def test_auto_detect():
    assert len(lexicon_io.parse_entries('[{"correct":"A","variants":["a"]}]')) == 1
    assert len(lexicon_io.parse_entries("correct,category,variants\nA,x,a\n")) == 1
    assert len(lexicon_io.parse_entries("A = a, b\n")) == 1


def test_merge_entries():
    existing = [{"correct": "SQL", "variants": ["色考"]}]
    incoming = [
        {"correct": "SQL", "variants": ["色考", "色口"], "category": "编程语言"},
        {"correct": "Vue", "variants": ["维优"]},
    ]
    merged, added, merged_count = lexicon_io.merge_entries(existing, incoming)
    assert added == 1          # Vue 是新增
    assert merged_count == 1   # SQL 是合并
    sql = next(e for e in merged if e["correct"] == "SQL")
    assert sql["variants"] == ["色考", "色口"]   # 去重且保留顺序
    assert sql["category"] == "编程语言"


def test_import_into_file():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "lex.json"
        p.write_text(json.dumps({
            "version": 1, "name": "test", "description": "d",
            "entries": [{"correct": "SQL", "variants": ["色考"]}],
        }, ensure_ascii=False), encoding="utf-8")

        added, merged = lexicon_io.import_into_lexicon_file(
            p, [{"correct": "SQL", "variants": ["色口"]}, {"correct": "Vue", "variants": ["维优"]}]
        )
        assert added == 1 and merged == 1

        data = lexicon_io.load_lexicon_data(p)
        # meta 保留
        assert data["name"] == "test" and data["description"] == "d"
        assert len(data["entries"]) == 2
        sql = next(e for e in data["entries"] if e["correct"] == "SQL")
        assert sql["variants"] == ["色考", "色口"]


def test_load_with_utf8_bom():
    # Windows 记事本「UTF-8 带 BOM」很常见，读取不应崩
    from asr_polish.lexicon import Lexicon
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "lex.json"
        content = '{"version":1,"entries":[{"correct":"SQL","variants":["色考"]}]}'
        p.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
        data = lexicon_io.load_lexicon_data(p)
        assert data["entries"][0]["correct"] == "SQL"
        lex = Lexicon(path=p)
        assert lex.entries[0]["correct"] == "SQL"


def test_variant_level_confidence():
    from asr_polish.lexicon import Lexicon
    lex = Lexicon()
    # 规则默认：中文变体 medium、ASCII 变体 high
    e = {"correct": "X", "variants": ["某词", "x y z"]}
    assert lex.resolve_confidence(e, "某词") == "medium"
    assert lex.resolve_confidence(e, "x y z") == "high"
    # high_variants 白名单升级「不可能独立成词」的中文变体
    e2 = {"correct": "异步", "variants": ["一部", "易步"], "high_variants": ["易步"]}
    assert lex.resolve_confidence(e2, "一部") == "medium"
    assert lex.resolve_confidence(e2, "易步") == "high"
    # 条目级 confidence 整体覆盖
    e3 = {"correct": "SQL", "confidence": "high", "variants": ["色考"]}
    assert lex.resolve_confidence(e3, "色考") == "high"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as e:  # 捕获所有异常，避免单个用例打断整轮并报出具体用例
            failed += 1
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    sys.exit(1 if failed else 0)
