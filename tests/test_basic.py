"""asr-polish 基础单元测试。运行：python tests/test_basic.py"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from asr_polish import correct_text, cross_check, assess  # noqa: E402


class TestCorrection(unittest.TestCase):
    def test_sql(self):
        r = correct_text("讲色考数据库")
        self.assertIn("SQL", r.text)
        self.assertEqual(r.count, 1)

    def test_python_uppercase(self):
        # 全大写 PASS 是 ASR 常见错误写法，应订正
        r = correct_text("学PASS")
        self.assertIn("Python", r.text)

    def test_long_word_priority(self):
        # 「Aging Decoding」应整体订正为 Agentic Coding，而不是只碰其中的片段
        r = correct_text("讲Aging Decoding")
        self.assertIn("Agentic Coding", r.text)

    def test_spelled_out_ascii(self):
        # 字母拼读（含空格）应订正
        r = correct_text("学习h t t p协议")
        self.assertIn("HTTP", r.text)


class TestNoFalsePositive(unittest.TestCase):
    """误伤回归测试：这些句子里的词形与某些「错误变体」撞车，但语义是正常中文/英文，不应被替换。"""

    def _assert_untouched(self, text):
        r = correct_text(text)
        self.assertEqual(r.text, text, f"不应改动：{text!r} -> {r.text!r}")
        self.assertEqual(r.count, 0)

    def test_pass_keyword(self):
        # pass 是英文「通过」关键字，不应被当 Python
        self._assert_untouched("请 pass 这个参数")
        self._assert_untouched("我今天通过了考试")

    def test_compass_substring(self):
        # 词边界应避免子串误伤
        self._assert_untouched("这是 compass 指南针")

    def test_k8s_legit_abbrev(self):
        # k8s 是合法缩写，不应被替换成 Kubernetes
        self._assert_untouched("k8s 集群")

    def test_yibu_movie(self):
        # 「一部」是量词，不应变「异步」
        self._assert_untouched("我们看一部电影")

    def test_bingfa_art_of_war(self):
        self._assert_untouched("孙子兵法")

    def test_kaiyuan_park(self):
        self._assert_untouched("公园开园了")

    def test_aide_education(self):
        self._assert_untouched("这是爱的教育")

    def test_shujuji_and(self):
        # 「数据及」是「数据以及」的省略，不应变「数据集」
        self._assert_untouched("数据及其他信息")

    def test_diaoshi_music(self):
        # 「调式」是音乐术语，不应变「调试」
        self._assert_untouched("大调式和小调式")


class TestMediumConfidence(unittest.TestCase):
    """medium 变体在出现同类别术语的技术语境下仍应订正。"""

    def test_medium_with_context(self):
        # 「并发」是「技术术语」类别，命中附近出现后，「一部」被订正为「异步」
        r = correct_text("我们讲一部编程和并发处理")
        self.assertIn("异步", r.text)

    def test_medium_without_context(self):
        # 无技术语境 → 不订正
        r = correct_text("一部电影")
        self.assertEqual(r.text, "一部电影")


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
