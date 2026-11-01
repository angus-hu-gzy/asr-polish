"""asr-polish 基础单元测试。运行：python tests/test_basic.py"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from asr_polish import correct_text, cross_check, assess, Lexicon  # noqa: E402


class TestCorrection(unittest.TestCase):
    def test_sql(self):
        r = correct_text("讲色考数据库")
        self.assertIn("SQL", r.text)
        self.assertEqual(r.count, 1)

    def test_python_uppercase(self):
        r = correct_text("学PASS")
        self.assertIn("Python", r.text)

    def test_python_case_insensitive(self):
        r = correct_text("学Pass")
        self.assertIn("Python", r.text)

    def test_long_word_priority(self):
        # 「Aging Decoding」应整体订正为 Agentic Coding，而不是只碰其中的片段
        r = correct_text("讲Aging Decoding")
        self.assertIn("Agentic Coding", r.text)

    def test_no_false_positive(self):
        # 不含错误变体的正常文本应原样保留
        raw = "今天天气很好"
        r = correct_text(raw)
        self.assertEqual(r.text, raw)
        self.assertEqual(r.count, 0)


class TestCrossCheck(unittest.TestCase):
    def test_fully_agree(self):
        r = cross_check("SQL和Python", "SQL和Python")
        self.assertEqual(r.overall_confidence, 1.0)

    def test_partial_conflict(self):
        r = cross_check("SQL和Python", "色考和PASS")
        # 两段完全不同 → 整体置信度应显著低于 1
        self.assertLess(r.overall_confidence, 1.0)
        self.assertTrue(len(r.conflict_segments) >= 1)

    def test_single_engine(self):
        r = cross_check("只有一段")
        self.assertEqual(r.overall_confidence, 1.0)


class TestQuality(unittest.TestCase):
    def test_clean_text_scores_high(self):
        r = assess("今天我们讲SQL和Python还有Flask框架。", duration_seconds=6)
        self.assertGreaterEqual(r.score, 70)

    def test_garbled_text_scores_low(self):
        r = assess("乱码\x00\x00\x00\x00", duration_seconds=2)
        self.assertLess(r.score, 60)

    def test_repetition_scores_low(self):
        r = assess("很好很好很好很好很好很好", duration_seconds=2)
        self.assertLessEqual(r.score, 80)


if __name__ == "__main__":
    unittest.main(verbosity=2)
