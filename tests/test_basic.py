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

    def test_correction_carries_confidence(self):
        # 每处订正都应带置信度：白名单变体 high，上下文佐证的 medium
        high = correct_text("请用易步的方式处理").corrections[0]
        self.assertEqual(high.confidence, "high")
        med = correct_text("线程和一部配合回调处理").corrections[0]
        self.assertEqual(med.confidence, "medium")
        # summary 对 medium 改动应标注「待确认」，便于只审 medium
        self.assertIn("待确认", correct_text("线程和一部配合回调处理").summary())


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

    def test_jason_name(self):
        # 「杰森」是英文名 Jason 音译，不应变 JSON
        self._assert_untouched("杰森·斯坦森主演的动作片")

    def test_jiesheng_doctor(self):
        # 「接生」是医疗用语，不应变 JSON
        self._assert_untouched("产科医生正在接生")

    def test_daoke_rivers(self):
        # 「刀客」是江湖用语，不应变 Docker
        self._assert_untouched("江湖上的刀客")

    def test_duoke_name(self):
        # 「多克」可作人名，不应变 Docker
        self._assert_untouched("多克是一个人名")

    def test_xipu_company(self):
        # 「西普」可作公司名，不应变 SICP
        self._assert_untouched("西普公司发布了新品")

    def test_xiangnong_name(self):
        # 「向农」可作人名，不应变 香农
        self._assert_untouched("向农是一位老支书")

    def test_yibu_database_video(self):
        # 「一部」是量词，即便附近出现「数据库」也不应变 异步
        self._assert_untouched("我看了一部关于数据库优化的视频")

    def test_yibu_microservice_book(self):
        self._assert_untouched("这是一部讲微服务的书")

    def test_bingfa_distributed(self):
        # 「兵法」即便附近出现「分布式系统」也不应变 并发
        self._assert_untouched("他讲的兵法在分布式系统里也能用")

    def test_kaiyuan_poster(self):
        # 「开园」即便附近出现「开源海报」也不应变 开源
        self._assert_untouched("那个庙会开园了，门口贴着开源海报")

    def test_quanzhan_machine_full(self):
        # 「全占满」是正常中文，guards 应阻止「全占」被误判为「全栈」
        self._assert_untouched("这台机器全占满了")

    def test_geite_closeup(self):
        # 「给特写」是正常中文，guards 应阻止「给特」被误判为 Git
        self._assert_untouched("请给特写镜头")

    def test_sekou_lipstick(self):
        # 「买色口红色号」是「买+口红色号」，guards 应阻止误判为 MySQL/SQL
        self._assert_untouched("买色口红色号")

    def test_proust_not_rust(self):
        # 「普鲁斯特」里含「鲁斯特」，不是 Rust
        self._assert_untouched("普鲁斯特的小说")

    def test_ringgit_not_git(self):
        # 「马来西亚林吉特」是货币名，里含「吉特」，不是 Git
        self._assert_untouched("马来西亚林吉特汇率")

    def test_ascii_boundary_with_digits(self):
        # 「a i18next」里含「a i」，词边界要连数字一起挡掉
        self._assert_untouched("a i18next language")

    def test_pass_slash_fail(self):
        # 「PASS/FAIL」是测试结果，不是 Python
        self._assert_untouched("PASS/FAIL 测试")


class TestMediumConfidence(unittest.TestCase):
    """medium 变体在强相关上下文中才订正；high_variants 白名单无条件订正。"""

    def test_medium_with_strong_context(self):
        # 「一部」±8 字内出现 ≥2 个强相关词（线程、回调）→ 订正为「异步」
        r = correct_text("线程和一部配合回调处理")
        self.assertIn("异步", r.text)

    def test_medium_without_context(self):
        # 无技术语境 → 不订正
        r = correct_text("一部电影")
        self.assertEqual(r.text, "一部电影")

    def test_medium_single_related_word_insufficient(self):
        # 只有 1 个强相关词（并发），不足 ≥2 → 不订正
        r = correct_text("我们讲一部和并发")
        self.assertEqual(r.text, "我们讲一部和并发")

    def test_whitelisted_variant_fires_unconditionally(self):
        # 「易步」被 high_variants 白名单升为 high，无技术语境也应订正
        r1 = correct_text("请用易步的方式处理")
        self.assertIn("异步", r1.text)

    def test_bingfa_needs_context(self):
        # 「病发」与「疾病发生」同形，已降为 medium：无技术语境不动，有强上下文才订正
        self.assertEqual(correct_text("这属于病发场景").text, "这属于病发场景")
        self.assertEqual(correct_text("对疾病发生与发展的认识").text, "对疾病发生与发展的认识")
        self.assertIn("并发", correct_text("病发导致死锁，需要加锁").text)

    def test_strong_context_single_word_suffices(self):
        # 强上下文词（虚拟机）命中 1 个即可订正「爪哇」→ Java
        r = correct_text("我们讲爪哇的虚拟机")
        self.assertIn("Java", r.text)

    def test_weak_context_needs_two_words(self):
        # 弱上下文词（数据 + 格式）命中 2 个，订正「杰森」→ JSON
        r = correct_text("杰森这种数据格式很常见")
        self.assertIn("JSON", r.text)
        # 只有 1 个弱词则不够，不订正
        r2 = correct_text("杰森这个格式")
        self.assertNotIn("JSON", r2.text)


class TestCrossCheck(unittest.TestCase):
    def test_fully_agree(self):
        r = cross_check("SQL和Python", "SQL和Python")
        self.assertEqual(r.overall_confidence, 1.0)

    def test_partial_conflict(self):
        r = cross_check("SQL和Python", "色考和PASS")
        # 两段完全不同 → 字符一致率应显著低于 1
        self.assertLess(r.overall_confidence, 1.0)
        self.assertTrue(len(r.conflict_segments) >= 1)

    def test_single_engine(self):
        r = cross_check("只有一段")
        self.assertEqual(r.overall_confidence, 1.0)

    def test_single_char_disagreement_kept_short(self):
        # 只差一个「的」/「得」，分歧段应是单字「化」，而不是 13 个字被整段误标
        a = "我们今天讨论数据库性能优化的问题"
        b = "我们今天讨论数据库性能优得的问题"
        r = cross_check(a, b)
        self.assertTrue(len(r.conflict_segments) >= 1)
        for seg in r.conflict_segments:
            self.assertLessEqual(len(seg.text), 3, f"分歧段被错误合并：{seg.text!r}")

    def test_two_engine_conflict_is_low_confidence(self):
        # 两引擎各执一词（confidence=0.5）应计入低置信片段，不能为空
        r = cross_check("SQL和Python", "色考和PASS")
        self.assertTrue(len(r.low_confidence_segments) >= 1)

    def test_empty_engine_excluded(self):
        # 空文本不应被算成「反对一切」：剔除后只剩 1 个有效引擎，一致率为 1
        r = cross_check("今天讲SQL", "")
        self.assertEqual(r.overall_confidence, 1.0)
        self.assertEqual(r.segments[0].total, 1)


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

    def test_medium_variant_not_flagged_as_suspicious(self):
        # medium 变体「一部」无技术语境时不订正，也不应被标成「疑似异步」扣分
        r = assess("我们看一部电影", duration_seconds=4)
        dim = next(d for d in r.dimensions if d.name == "可疑同音字")
        self.assertEqual(dim.metric, 0)

    def test_ascii_word_boundary_in_assess(self):
        # 与 correct 一致：pass/compass 有词边界保护，assess 不应误判为 Python
        for t in ("请 pass 这个参数", "这是 compass 指南针"):
            r = assess(t, duration_seconds=4)
            dim = next(d for d in r.dimensions if d.name == "可疑同音字")
            self.assertEqual(dim.metric, 0, f"{t!r} 不应被标为可疑")

    def test_overlapping_variants_not_double_counted(self):
        # 「h t t p s」应只命中 HTTPS 一次，不再被 HTTP+HTTPS 扣两次分
        r = assess("h t t p s 协议", duration_seconds=2)
        dim = next(d for d in r.dimensions if d.name == "可疑同音字")
        self.assertEqual(dim.metric, 1)


class TestShortCjkPolicy(unittest.TestCase):
    """≤2 字中文变体：条目级 confidence 不再顺带升级，必须 high_variants 显式声明。"""

    def test_entry_level_high_downgraded(self):
        lex = Lexicon()
        sql = next(e for e in lex.entries if e["correct"] == "SQL")
        self.assertEqual(lex.resolve_confidence(sql, "色口"), "medium")
        self.assertEqual(lex.resolve_confidence(sql, "色考"), "high")  # 白名单显式升级

    def test_quanzhan_collisions(self):
        # 「全占」与「完全占领 / 全占了 / 全占满」同形，不能无条件替换
        for t in ["完全占领了市场", "我军全占领了高地", "这台机器全占了吧"]:
            self.assertEqual(correct_text(t).text, t, f"误伤：{t!r}")

    def test_geite_collision(self):
        self.assertEqual(correct_text("给特别的人留个位置").text, "给特别的人留个位置")

    def test_sekou_collision(self):
        self.assertEqual(correct_text("买色口腔的检查").text, "买色口腔的检查")

    def test_tuili_collision(self):
        self.assertEqual(correct_text("推里面").text, "推里面")


class TestAssessSeesMedium(unittest.TestCase):
    """medium 变体按上下文计入评分：技术语境里的错别字不能被评分无视。"""

    def test_medium_with_context_penalized(self):
        # 「杰森」+「数据格式」→ 判为可疑「JSON」，应有扣分
        r = assess("杰森这种数据格式很常见", duration_seconds=6)
        dim = next(d for d in r.dimensions if d.name == "可疑同音字")
        self.assertEqual(dim.metric, 1)
        self.assertLess(r.score, 100)

    def test_medium_without_context_clean(self):
        # 无技术语境的合法用词，不应扣分
        for t in ["我们看一部电影", "完全占领了市场", "添砖加瓦"]:
            dim = next(d for d in assess(t, duration_seconds=6).dimensions
                       if d.name == "可疑同音字")
            self.assertEqual(dim.metric, 0, f"{t!r} 不应被标为可疑")


class TestBiliHelpers(unittest.TestCase):
    """bili.py 里不依赖网络的纯函数。"""

    def test_extract_bvid(self):
        from asr_polish.bili import extract_bvid
        self.assertEqual(extract_bvid("https://www.bilibili.com/video/BV1xx411c7mD?p=1"),
                         "BV1xx411c7mD")
        self.assertEqual(extract_bvid("BV1xx411c7mD"), "BV1xx411c7mD")
        self.assertIsNone(extract_bvid("这不是链接"))

    def test_pick_subtitle_preference(self):
        from asr_polish.bili import _pick_subtitle
        subs = [{"lan": "en", "subtitle_url": "u1"},
                {"lan": "zh-CN", "subtitle_url": "u2"},
                {"lan": "ai-zh", "subtitle_url": "u3"}]
        self.assertEqual(_pick_subtitle(subs)["lan"], "ai-zh")
        self.assertEqual(_pick_subtitle([{"lan": "en", "subtitle_url": "u1"}])["lan"], "en")
        self.assertIsNone(_pick_subtitle([]))


class TestAuditTool(unittest.TestCase):
    """tools/audit_corrections.py：语料审计能标出「变体被夹在更长中文词里」的疑似误伤。"""

    def test_audit_embedded_flag(self):
        from tools.audit_corrections import audit_text
        # 正常词不再被订正
        self.assertEqual(audit_text("完全占领了市场"), [])
        # 词边界干净的真实错字：不算嵌入
        rec = audit_text("请用色考 查询")[0]
        self.assertEqual(rec.corrected, "SQL")
        self.assertFalse(rec.embedded)
        # 前后都是汉字 → 标记为疑似嵌入
        self.assertTrue(audit_text("我们今天讲色考和PASS")[0].embedded)

    def test_audit_grouping(self):
        from tools.audit_corrections import audit_text, group_records
        records = audit_text("色考 和 PASS 都错了") + audit_text("再讲一次色考 ")
        groups = group_records(records)
        counts = {(g.original, g.corrected): g.count for g in groups}
        self.assertEqual(counts[("色考", "SQL")], 2)


class TestCLI(unittest.TestCase):
    def test_parser(self):
        from asr_polish.cli import build_parser
        p = build_parser()
        args = p.parse_args(["correct", "讲色考"])
        self.assertEqual(args.text, "讲色考")
        args2 = p.parse_args(["lexicon", "list"])
        self.assertEqual(args2.lex_cmd, "list")


if __name__ == "__main__":
    unittest.main(verbosity=2)
