# -*- coding: utf-8 -*-
"""单篇小测验的服务端部分：提示词、题目怎么解析、答案怎么对上、错误怎么带回来。

为什么值得一个文件
------------------
出题这一层的错法比前两层更隐蔽：总结错了一句，读者看得出来；题目错了，读者
**会以为自己没学会，或者以为自己学会了**。而这里没有任何东西能验证「这道题出得
好不好」——能验证的只有三件：

1. 提示词里那五条要求都在（尤其「只能考原文讲过的」和「答案位置打乱」）；
2. 模型的「正确答案」不管写成字母、选项原文还是下标，都能落到同一个选项上，
   对不上的题**宁可丢掉**，不猜；
3. 正文只发一次、不落盘、key 不进错误消息。

假模型服务是标准库 http.server，bind 0 让系统分配端口，跑完就关。
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from asr_polish.server import quiz, summary

_KEY = "sk-secret-must-not-leak"

_BODY = "[00:00] 大家好\n[00:05] 先从安装 Python 开始\n[01:03] 装完配 PATH"

_OPTIONS = ["装 Python", "装 Node", "装 Java", "什么都不用装"]


def _q(**kw) -> dict:
    """一道合法题的默认形状，按需覆盖字段。"""
    d = {"stem": "第一步做什么？", "options": list(_OPTIONS),
         "answer": "A", "why": "原文说先装 Python [00:05]。", "t": "00:05"}
    d.update(kw)
    return d


class _Rec:
    """假服务收到的那一次请求。"""

    def __init__(self) -> None:
        self.path = ""
        self.headers: dict = {}
        self.body: dict = {}


def _serve(reply: str, code: int = 200):
    rec = _Rec()

    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:                       # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            rec.path = self.path
            rec.headers = dict(self.headers)
            try:
                rec.body = json.loads(raw.decode("utf-8"))
            except ValueError:
                rec.body = {}
            payload = reply.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, rec


def _reply(content: str) -> str:
    return json.dumps({"choices": [{"message": {"content": content}}]})


def _quiz_reply(*qs: dict) -> str:
    return _reply(json.dumps({"questions": list(qs)}, ensure_ascii=False))


class PromptTest(unittest.TestCase):
    """提示词是服务端常量；这五条少一条，后面全白搭。"""

    def test_five_rules_are_written_down(self) -> None:
        p = quiz.QUIZ_PROMPT
        self.assertIn("确实讲过", p)        # 只能考原文讲过的
        self.assertIn("原样抄", p)          # 出处时间原样抄行首
        self.assertIn("[分:秒]", p)         # 解析里也要带时间
        self.assertIn("干扰项要合理", p)     # 三个干扰项不能一眼假
        self.assertIn("位置要打乱", p)       # 正确答案不许总在一个字母
        self.assertIn("不许编", p)

    def test_count_is_filled_in(self) -> None:
        """题数进提示词，而且两处都填上（出几道 / 四个位置都要出现）。"""
        p = quiz.QUIZ_PROMPT % {"count": 5}
        self.assertIn("出 5 道单选题", p)
        self.assertIn("5 道题里四个位置都要出现", p)
        self.assertNotIn("%(count)d", p)

    def test_no_time_rule_only_when_the_part_has_none(self) -> None:
        self.assertIn("没有逐句时间戳", quiz.NO_TIME_RULE)
        self.assertIn("t 一律写", quiz.NO_TIME_RULE)
        # 有时间的篇不许把这句话发过去：它会让模型连时间都不敢写
        self.assertNotIn(quiz.NO_TIME_RULE, quiz.QUIZ_PROMPT)


class AnswerIndexTest(unittest.TestCase):
    """模型的「正确答案」怎么落到选项上。

    落错的代价是：用户答对了被判错，或者答错了被判对，而两种看上去都正常。
    """

    def test_letter(self) -> None:
        self.assertEqual(quiz.answer_index("A", _OPTIONS), 0)
        self.assertEqual(quiz.answer_index("c", _OPTIONS), 2)
        self.assertEqual(quiz.answer_index("B.", _OPTIONS), 1)
        self.assertEqual(quiz.answer_index("选项D", _OPTIONS), 3)
        self.assertEqual(quiz.answer_index("答案是 C，因为……", _OPTIONS), 2)

    def test_exact_option_text(self) -> None:
        """模型常把正确选项整句抄回来——这比字母还不容易歧义，先认它。"""
        self.assertEqual(quiz.answer_index("装 Java", _OPTIONS), 2)
        self.assertEqual(quiz.answer_index("  装 Python ", _OPTIONS), 0)

    def test_zero_based_index(self) -> None:
        self.assertEqual(quiz.answer_index(0, _OPTIONS), 0)
        self.assertEqual(quiz.answer_index(3, _OPTIONS), 3)
        self.assertEqual(quiz.answer_index("2", _OPTIONS), 2)
        self.assertEqual(quiz.answer_index(2.0, _OPTIONS), 2)

    def test_unusable_values_give_none(self) -> None:
        for bad in (None, "", "  ", 4, -1, "E", True, False, [], {}, "不知道"):
            self.assertIsNone(quiz.answer_index(bad, _OPTIONS), bad)

    def test_letter_outside_the_option_count(self) -> None:
        """只有三个选项时 C 没了：不能把 D 之外的一切都当地三个。"""
        self.assertEqual(quiz.answer_index("C", _OPTIONS[:3]), 2)
        self.assertIsNone(quiz.answer_index("D", _OPTIONS[:3]))

    def test_fifth_letter_when_there_really_are_five(self) -> None:
        """认到 E：五选项的题，答案可能就在第 5 个上。

        削枝要先算出这个下标，才知道该保哪一项。四个选项时 "E" 照样越界、
        照样 None（上面 `test_unusable_values_give_none` 钉着），所以放宽字符集
        不会让四选项的判分变松。
        """
        five = _OPTIONS + ["装 Go"]
        self.assertEqual(quiz.answer_index("E", five), 4)
        self.assertEqual(quiz.answer_index("e", five), 4)
        self.assertEqual(quiz.answer_index("选项E", five), 4)
        self.assertEqual(quiz.answer_index("装 Go", five), 4)


class NormalizeTest(unittest.TestCase):
    """不管模型吐什么，出去的题目都得是能判分的。"""

    def test_keeps_a_usable_question(self) -> None:
        out = quiz.normalize({"questions": [_q()]})
        self.assertEqual(len(out["questions"]), 1)
        q = out["questions"][0]
        self.assertEqual(q["stem"], "第一步做什么？")
        self.assertEqual(q["options"], _OPTIONS)
        self.assertEqual(q["answer"], 0)
        self.assertEqual(q["t"], 5)                    # 秒，不再是字符串
        self.assertIn("[00:05]", q["why"])

    def test_drops_questions_that_cannot_be_graded(self) -> None:
        out = quiz.normalize({"questions": [
            _q(),                                       # 好的
            _q(options=_OPTIONS[:3]),                   # 只有三个选项
            _q(options=_OPTIONS + ["装 Go"]),           # 五个选项：削一个，留下
            _q(answer="Z"),                             # 答案对不上
            _q(stem=""),
            _q(options=["装 Python", "", "装 Java", "装 Go"]),
            "不是对象",
        ]})
        self.assertEqual(len(out["questions"]), 2)
        # 丢掉的每一道都带一句理由：界面上要说清「模型这次哪几道不能用」，
        # 而不是让人数着 5 道却只看到 1 道
        self.assertEqual(len(out["skipped"]), 5)
        self.assertIn("第 2 题有 3 个选项", out["skipped"][0])
        self.assertIn("第 4 题的答案对不上", out["skipped"][1])
        self.assertIn("第 5 题没有题干", out["skipped"][2])
        self.assertIn("第 7 题不是一个对象", out["skipped"][4])

    def test_five_options_shrink_to_four(self) -> None:
        """五选一不是坏题：削掉一个干扰项，题照出。

        第二轮真跑里 P4 五道题全是五个选项，一篇的测验直接「一道都用不了」。
        丢掉的其实是本来能判分的题，只因为多了一个干扰项。
        """
        out = quiz.normalize({"questions": [_q(options=_OPTIONS + ["装 Go"])]})
        self.assertEqual(len(out["questions"]), 1)
        q = out["questions"][0]
        self.assertEqual(q["options"], _OPTIONS)        # 削的是最后那个非答案项
        self.assertEqual(q["answer"], 0)                # A 还在原位
        # 削过的题不算「跳过的题」：能判分就是能用，读者不需要知道本来多一项
        self.assertNotIn("skipped", out)

    def test_five_options_answer_at_the_end(self) -> None:
        """答案是 E：削掉最后一个**非答案**项，答案跟着落到下标 3。

        断言落在「下标指向的那一项还是不是原来那句」，而不是只比下标数字——
        判分对不对是由这个决定的。
        """
        five = _OPTIONS + ["装 Go"]
        for answer in ("E", "装 Go"):     # 字母、原文两种写法
            q = quiz.normalize({"questions": [_q(options=five, answer=answer)]})["questions"][0]
            self.assertEqual(q["options"], ["装 Python", "装 Node", "装 Java", "装 Go"])
            self.assertEqual(q["answer"], 3, answer)
            self.assertEqual(q["options"][q["answer"]], "装 Go", answer)

    def test_five_options_with_unusable_answer_still_dropped(self) -> None:
        """答案对不上就还是不能判分：不知道该保哪一项，也就没法削。"""
        with self.assertRaises(summary.SummaryError) as cm:
            quiz.normalize({"questions": [_q(options=_OPTIONS + ["装 Go"], answer="Z")]})
        self.assertIn("第 1 题的答案对不上", cm.exception.message)

    def test_five_options_with_labels_lose_them_all(self) -> None:
        """五选项带标号，削完标号必须一起去干净。

        这是最难的一格：答案是 E 时删掉的是 D，剩下 A/B/C/E。`_strip_labels` 的
        顺序校验认不出这种「缺一个」的形状，会把标号全留下，前端自己再画一遍
        A/B/C/D，界面上就成了「D  E. 装 Go」。
        """
        labelled = ["A. 装 Python", "B、装 Node", "C．装 Java",
                    "D 什么都不用装", "E. 装 Go"]
        q = quiz.normalize({"questions": [_q(options=labelled, answer="E")]})["questions"][0]
        self.assertEqual(q["options"], ["装 Python", "装 Node", "装 Java", "装 Go"])
        self.assertEqual(q["options"][q["answer"]], "装 Go")

    def test_five_options_with_labels_and_fourth_answer(self) -> None:
        """同样是带标号的五选项，但答案是 D：删掉的是 E，剩下 A/B/C/D 连号。"""
        labelled = ["A. 装 Python", "B、装 Node", "C．装 Java",
                    "D 什么都不用装", "E. 装 Go"]
        q = quiz.normalize({"questions": [_q(options=labelled, answer="D")]})["questions"][0]
        self.assertEqual(q["options"], _OPTIONS)
        self.assertEqual(q["options"][q["answer"]], "什么都不用装")

    def test_six_options_are_still_dropped(self) -> None:
        """只削一个。六个说明模型根本没在看「四个选项」这条要求，不替它揉。"""
        with self.assertRaises(summary.SummaryError) as cm:
            quiz.normalize({"questions": [
                _q(options=_OPTIONS + ["装 Go", "装 Rust"])]})
        self.assertIn("第 1 题有 6 个选项", cm.exception.message)

    def test_five_option_labels_that_are_content(self) -> None:
        """削枝那一路的去标号也不能误伤内容：不在正位就原样留着。"""
        opts = ["B 站很火", "A 股大涨", "C 盘满了", "D 盘没空间", "E 盘是光驱"]
        q = quiz.normalize({"questions": [_q(options=list(opts), answer="E")]})["questions"][0]
        self.assertEqual(q["options"], [opts[0], opts[1], opts[2], opts[4]])   # 内容一字不动
        self.assertEqual(q["options"][q["answer"]], "E 盘是光驱")

    def test_all_usable_means_no_skipped_key(self) -> None:
        """全都能用时不带 skipped：界面据此不画那行说明。"""
        self.assertNotIn("skipped", quiz.normalize({"questions": [_q()]}))

    def test_letter_prefix_is_stripped_from_options(self) -> None:
        """界面上会自己画 A/B/C/D，选项里自带前缀会变成「A  A. 装 Python」。"""
        q = quiz.normalize({"questions": [
            _q(options=["A. 装 Python", "B、装 Node", "C．装 Java", "D 什么都不用装"]),
        ]})["questions"][0]
        self.assertEqual(q["options"], _OPTIONS)

    def test_letters_that_are_content_are_left_alone(self) -> None:
        """「A 股」「B 站」是内容不是标号：不在正位上就别去。"""
        opts = ["B 站很火", "A 股大涨", "C 盘满了", "D 盘没空间"]
        q = quiz.normalize({"questions": [_q(options=list(opts))]})["questions"][0]
        self.assertEqual(q["options"], opts)
        # 连着的更不该动：「A股」根本没有分隔
        opts2 = ["A股大涨", "B站很火", "C盘满了", "D盘没空间"]
        q2 = quiz.normalize({"questions": [_q(options=list(opts2))]})["questions"][0]
        self.assertEqual(q2["options"], opts2)

    def test_missing_explanation_stays_empty(self) -> None:
        """没有解析就是没有，不拿题干顶上去——解析是读者判断会不会的依据。"""
        q = quiz.normalize({"questions": [_q(why=None)]})["questions"][0]
        self.assertEqual(q["why"], "")
        self.assertIsNone(quiz.normalize({"questions": [_q(t=None)]})
                          ["questions"][0]["t"])

    def test_alternate_field_names(self) -> None:
        """字段名认几个常见别名，别因为叫 question 就把整题丢了。"""
        out = quiz.normalize({"quiz": [
            {"question": "第一步？", "choices": list(_OPTIONS),
             "correct": "B", "explanation": "因为 [00:05]。", "time": "00:05"},
        ]})
        self.assertEqual(out["questions"][0]["answer"], 1)

    def test_nothing_usable_is_an_error(self) -> None:
        with self.assertRaises(summary.SummaryError) as cm:
            quiz.normalize({"questions": []})
        self.assertEqual(cm.exception.kind, "parse")
        self.assertIn("一道题都没给", cm.exception.message)
        self.assertIn("重新生成一套", cm.exception.message)

    def test_all_broken_says_which_item_is_wrong(self) -> None:
        """全坏的时候要报「哪一项不对」，不是一句「生成失败」。

        报「第 2 题有 3 个选项」能让人判断该重试还是换模型；报「生成失败」
        只能让人再点一次，还是坏。
        """
        with self.assertRaises(summary.SummaryError) as cm:
            quiz.normalize({"questions": [_q(options=_OPTIONS[:3]), _q(answer="Z")]})
        msg = cm.exception.message
        self.assertEqual(cm.exception.kind, "parse")
        self.assertIn("模型回了 2 道题", msg)
        self.assertIn("第 1 题有 3 个选项", msg)
        self.assertIn("第 2 题的答案对不上", msg)

    def test_no_questions_key(self) -> None:
        with self.assertRaises(summary.SummaryError) as cm:
            quiz.normalize({"tldr": "这是总结，模型跑偏了"})
        self.assertEqual(cm.exception.kind, "parse")
        self.assertIn("questions", cm.exception.message)


class CountTest(unittest.TestCase):
    def test_default_and_clamp(self) -> None:
        self.assertEqual(quiz.count_of(None), quiz.DEFAULT_COUNT)
        self.assertEqual(quiz.count_of("3"), 3)
        self.assertEqual(quiz.count_of(0), 1)
        self.assertEqual(quiz.count_of(99), quiz.MAX_COUNT)
        self.assertEqual(quiz.count_of("很多"), quiz.DEFAULT_COUNT)
        self.assertEqual(quiz.count_of(True), quiz.DEFAULT_COUNT)
        self.assertEqual(quiz.count_of(2.0), 2)


class CallTest(unittest.TestCase):
    """真发一次请求：看它收到什么、拿回来什么。"""

    def setUp(self) -> None:
        self.srv, self.rec = _serve(_quiz_reply(_q()))
        self.base = "http://127.0.0.1:%d/v1" % self.srv.server_address[1]
        self.cfg = {"base": self.base, "model": "m1", "key": _KEY}

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def test_request_shape(self) -> None:
        out = quiz.call(self.cfg, _BODY, count=3)
        self.assertEqual(self.rec.path, "/v1/chat/completions")
        self.assertEqual(self.rec.headers.get("Authorization"), "Bearer " + _KEY)
        self.assertEqual(self.rec.body["model"], "m1")
        self.assertFalse(self.rec.body["stream"])
        self.assertEqual(self.rec.body["response_format"], {"type": "json_object"})
        sysmsg = self.rec.body["messages"][0]["content"]
        self.assertTrue(sysmsg.startswith(quiz.QUIZ_PROMPT % {"count": 3}))
        self.assertNotIn(quiz.NO_TIME_RULE, sysmsg)
        self.assertEqual(len(out["quiz"]["questions"]), 1)
        self.assertEqual(out["model"], "m1")

    def test_body_is_wrapped_and_sent_once(self) -> None:
        """正文包在标记之间，且整段请求里只出现一次——重复计费就是从这里来的。"""
        quiz.call(self.cfg, _BODY)
        msgs = self.rec.body["messages"]
        joined = "\n".join(m["content"] for m in msgs)
        self.assertEqual(joined.count(_BODY), 1)
        self.assertIn(summary.BODY_HEAD + _BODY + summary.BODY_TAIL,
                      [m["content"] for m in msgs])

    def test_no_time_part_gets_the_extra_rule(self) -> None:
        quiz.call(self.cfg, _BODY, has_time=False)
        self.assertIn(quiz.NO_TIME_RULE,
                      self.rec.body["messages"][0]["content"])

    def test_empty_body_never_sends(self) -> None:
        with self.assertRaises(summary.SummaryError) as cm:
            quiz.call(self.cfg, "   ")
        self.assertEqual(cm.exception.kind, "empty")
        self.assertIn("出不了题", cm.exception.message)
        self.assertEqual(self.rec.path, "")

    def test_too_long_body_is_explained(self) -> None:
        with self.assertRaises(summary.SummaryError) as cm:
            quiz.call(self.cfg, "字" * (summary.MAX_TEXT + 1))
        self.assertEqual(cm.exception.status, 413)
        self.assertIn("超过单次", cm.exception.message)
        self.assertEqual(self.rec.path, "")

    def test_missing_key_never_sends(self) -> None:
        with self.assertRaises(summary.SummaryError) as cm:
            quiz.call({"base": self.base, "model": "m1", "key": ""}, _BODY)
        self.assertEqual(cm.exception.kind, "no_key")
        self.assertEqual(self.rec.path, "")

    def test_answers_land_on_the_right_option(self) -> None:
        """整条路走一遍：模型给字母，回来的题目的 answer 是下标。"""
        out = quiz.call(self.cfg, _BODY)
        self.assertEqual(out["quiz"]["questions"][0]["answer"], 0)


class EmptyAnswerTest(unittest.TestCase):
    def test_empty_content_is_an_error(self) -> None:
        srv, _ = _serve(_reply("   "))
        try:
            cfg = {"base": "http://127.0.0.1:%d/v1" % srv.server_address[1],
                   "model": "m1", "key": _KEY}
            with self.assertRaises(summary.SummaryError) as cm:
                quiz.call(cfg, _BODY)
            self.assertEqual(cm.exception.kind, "parse")
        finally:
            srv.shutdown()
            srv.server_close()


class KeyNeverLeaksTest(unittest.TestCase):
    """本地服务的日志会被人粘到 issue 里，key 一次都不许出现在消息里。"""

    def test_key_absent_from_auth_error(self) -> None:
        srv, _ = _serve(json.dumps({"error": {"message": "Incorrect API key"}}),
                        code=401)
        try:
            cfg = {"base": "http://127.0.0.1:%d/v1" % srv.server_address[1],
                   "model": "m1", "key": _KEY}
            with self.assertRaises(summary.SummaryError) as cm:
                quiz.call(cfg, _BODY)
            self.assertEqual(cm.exception.kind, "auth")
            self.assertNotIn(_KEY, cm.exception.message)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
