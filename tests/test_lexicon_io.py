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


def test_failed_write_keeps_original():
    """写盘失败不能把用户原有的词表截断。

    `write_text` 是先截断再逐块写：写到一半被杀、磁盘满、或者两个导入同时
    跑，他一条条手写的那份就成了半截 JSON。先写临时文件、再原子替换，
    失败时原文件一个字节都不动。
    """
    from unittest import mock
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "lex.json"
        original = {"version": 1, "name": "mine",
                    "entries": [{"correct": "SQL", "variants": ["色考"]}]}
        p.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

        with mock.patch.object(lexicon_io, "replace_with_retry",
                               side_effect=OSError("磁盘满")):
            try:
                lexicon_io.import_into_lexicon_file(
                    p, [{"correct": "Vue", "variants": ["维优"]}])
            except OSError:
                pass
            else:
                raise AssertionError("写盘失败应当上抛，不能假装成功")

        assert json.loads(p.read_text(encoding="utf-8")) == original
        assert not [f for f in Path(td).iterdir() if f.suffix == ".tmp"], \
            "失败后不该留下临时文件"


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
    # 条目级 confidence 对 ASCII / 长中文变体生效
    e3 = {"correct": "SQL", "confidence": "high", "variants": ["色考", "seq l"]}
    assert lex.resolve_confidence(e3, "seq l") == "high"
    # 但 ≤2 字中文变体不允许被顺带升级，必须写 high_variants（安全优先）
    assert lex.resolve_confidence(e3, "色考") == "medium"
    e4 = {"correct": "SQL", "confidence": "high",
          "high_variants": ["色考"], "variants": ["色考", "色口"]}
    assert lex.resolve_confidence(e4, "色考") == "high"
    assert lex.resolve_confidence(e4, "色口") == "medium"


def test_json_roundtrip_keeps_new_fields():
    """JSON 导入必须透传 guards / strong_context，否则保护会静默失效。"""
    entry = {
        "correct": "全栈", "variants": ["全占"], "high_variants": ["全占"],
        "guards": {"全占": "满"}, "strong_context": ["前端"], "context": ["后端"],
    }
    parsed = lexicon_io.parse_json(json.dumps({"entries": [entry]}, ensure_ascii=False))
    assert parsed[0]["guards"] == {"全占": "满"}
    assert parsed[0]["strong_context"] == ["前端"]
    assert parsed[0]["high_variants"] == ["全占"]
    assert parsed[0]["context"] == ["后端"]


def test_merge_keeps_new_fields():
    merged, added, merged_n = lexicon_io.merge_entries(
        [{"correct": "全栈", "variants": ["全占"]}],
        [{"correct": "全栈", "variants": ["全战"],
          "guards": {"全战": "满"}, "strong_context": ["前端"]}],
    )
    assert added == 0 and merged_n == 1
    assert merged[0]["guards"] == {"全战": "满"}
    assert merged[0]["strong_context"] == ["前端"]


def test_left_guards_roundtrip():
    """left_guards（左邻保护）也必须透传，否则「6/6 PASS」会重新被改成 Python。"""
    entry = {"correct": "Python", "variants": ["PASS"], "left_guards": {"PASS": r"\s"}}
    parsed = lexicon_io.parse_json(json.dumps({"entries": [entry]}, ensure_ascii=False))
    assert parsed[0]["left_guards"] == {"PASS": r"\s"}

    merged, _added, _n = lexicon_io.merge_entries(
        [{"correct": "Python", "variants": ["PASS"]}],
        [{"correct": "Python", "variants": ["PASS"], "left_guards": {"PASS": r"\s"}}],
    )
    assert merged[0]["left_guards"] == {"PASS": r"\s"}


def test_left_guard_regex_must_compile():
    """变长/非法左邻正则在加载时丢弃并告警（lookbehind 必须定长）。"""
    import warnings as _warnings
    from asr_polish.lexicon import Lexicon
    entry = {"correct": "测试", "variants": ["甲"], "left_guards": {"甲": "a|ab"}}
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        clean = Lexicon._sanitize(entry)
    assert "left_guards" not in clean
    assert any("left_guards" in str(w.message) for w in caught)


def test_invalid_guard_is_dropped():
    """guards 里的非法正则应在加载时丢弃并告警，而不是运行时崩在 correct()。"""
    import warnings as _warnings
    from asr_polish.lexicon import Lexicon
    entry = {"correct": "测试", "variants": ["甲"], "guards": {"甲": "(", "甲好": "满"}}
    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        clean = Lexicon._sanitize(entry)
    # 非法 guard 被丢弃，合法 guard 保留
    assert clean["guards"] == {"甲好": "满"}
    assert any("guards" in str(w.message) for w in caught)
    # 列表字段写成字符串时应被拆分，避免逐字迭代导致上下文误命中
    clean2 = Lexicon._sanitize({"correct": "X", "variants": ["某"], "context": "线程, 协程"})
    assert clean2["context"] == ["线程", "协程"]


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
