# -*- coding: utf-8 -*-
"""单篇问答的服务端部分：提示词、会话怎么拼、历史怎么裁、错误怎么带回来。

为什么值得一个文件
------------------
这一层的错法和总结不一样。总结错了是「要点不准」，问答错了是「回答了一个视频
里根本没说的事，还带着一个看起来很像真的时间戳」——后者只要发生一次，这个
功能就没人信了。所以这里钉的不是「能不能跑通」，是三件更硬的事：

1. 提示词里必须三条都在（只依据正文 / 每条事实标时间 / 没有就说没有）；
2. 正文在一次请求里**只出现一次**，历史不重复带正文——这条直接决定账单；
3. 历史只带最近几轮，脏数据逐条丢，不能因为一条坏数据把整段会话废掉。

假模型服务是标准库 http.server，bind 到 0 让系统分配端口，跑完就关。
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from asr_polish.server import qa

_KEY = "sk-secret-must-not-leak"

_BODY = "【正文】\n[00:00] 大家好\n[01:03] 打开官网下载安装包"


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


class PromptTest(unittest.TestCase):
    """提示词是这一层唯一能约束模型的绳子，三股都不能断。"""

    def test_all_three_rules_are_written_down(self) -> None:
        p = qa.ASK_PROMPT
        self.assertIn("只能依据这段文字稿", p)      # 不许用训练数据补
        self.assertIn("原文中没有提到", p)           # 没有就说没有，给固定说法
        self.assertIn("[分:秒]", p)                 # 每条事实要标时间
        self.assertIn("不要换算成秒数", p)           # 换算它会算错，让它抄
        self.assertIn("不要写原文里没有的时间", p)   # 编时间是最坏的一种

    def test_no_time_rule_only_when_the_part_has_none(self) -> None:
        """必剪 ASR 的篇：不提前堵死，模型很可能自己编个时间出来。"""
        with_time = qa.build_messages(_BODY, "问了", [], True)
        without = qa.build_messages(_BODY, "问了", [], False)
        self.assertNotIn("没有逐句时间戳", with_time[0]["content"])
        self.assertIn("没有逐句时间戳", without[0]["content"])
        self.assertIn("不要编造时间", without[0]["content"])


class TrimTest(unittest.TestCase):
    """历史越长越贵：每轮都要把整篇正文重发一次。"""

    def _rounds(self, n: int) -> list:
        return [{"q": "问%d" % i, "a": "答%d" % i} for i in range(n)]

    def test_keeps_only_the_recent_rounds(self) -> None:
        kept = qa.trim_history(self._rounds(10))
        self.assertEqual(len(kept), qa.MAX_ROUNDS)
        self.assertEqual(kept[-1]["q"], "问9")       # 留的是最近的那几轮
        self.assertEqual(kept[0]["q"], "问4")

    def test_short_history_is_untouched(self) -> None:
        kept = qa.trim_history(self._rounds(2))
        self.assertEqual([r["q"] for r in kept], ["问0", "问1"])

    def test_junk_is_dropped_one_by_one(self) -> None:
        """一条脏数据只丢它自己，不能把整段会话废掉。"""
        kept = qa.trim_history([
            {"q": "好的", "a": "好的回答"},
            "不是字典",
            {"q": "缺 a"},
            {"q": 123, "a": "类型不对"},
            {"q": "  ", "a": "空的"},
            {"q": "也好", "a": "也好的回答"},
        ])
        self.assertEqual([r["q"] for r in kept], ["好的", "也好"])

    def test_nothing_at_all(self) -> None:
        for junk in (None, [], {}, "字符串", 3):
            self.assertEqual(qa.trim_history(junk), [], junk)


class MessagesTest(unittest.TestCase):

    def test_body_appears_exactly_once(self) -> None:
        """正文重复一遍就是多一份钱，而且长文很容易把上下文窗口撑爆。"""
        msgs = qa.build_messages(_BODY, "这一篇讲了什么？",
                                 [{"q": "问1", "a": "答1"},
                                  {"q": "问2", "a": "答2"}])
        hits = sum(m["content"].count("[01:03] 打开官网") for m in msgs)
        self.assertEqual(hits, 1)
        # 历史里的问题不许把正文再带一遍
        self.assertFalse(any(_BODY in m["content"] and m["role"] != "user"
                             for m in msgs[3:]))

    def test_order_and_roles(self) -> None:
        msgs = qa.build_messages(_BODY, "最后的问题", [{"q": "问1", "a": "答1"}])
        self.assertEqual([m["role"] for m in msgs],
                         ["system", "user", "assistant", "user", "assistant",
                          "user"])
        self.assertEqual(msgs[0]["content"].startswith(qa.ASK_PROMPT), True)
        self.assertTrue(msgs[1]["content"].startswith(qa.BODY_HEAD))
        self.assertTrue(msgs[1]["content"].endswith(qa.BODY_TAIL))
        self.assertEqual(msgs[-1]["content"], "最后的问题")

    def test_no_history_still_has_the_ack_turn(self) -> None:
        """没有确认回合，有的模型会把正文当成自己要说的话，接着往下续写。"""
        msgs = qa.build_messages(_BODY, "问", [])
        self.assertEqual([m["role"] for m in msgs], ["system", "user",
                                                     "assistant", "user"])
        self.assertEqual(msgs[2]["content"], qa.ACK)

    def test_question_is_stripped(self) -> None:
        msgs = qa.build_messages(_BODY, "  问点什么  \n", [])
        self.assertEqual(msgs[-1]["content"], "问点什么")


class CallTest(unittest.TestCase):
    """真发一次请求：看它收到什么、拿回来什么。"""

    def setUp(self) -> None:
        self.srv, self.rec = _serve(_reply("先装 Python [01:03]。"))
        self.base = "http://127.0.0.1:%d/v1" % self.srv.server_address[1]
        self.cfg = {"base": self.base, "model": "m1", "key": _KEY}

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def test_request_shape(self) -> None:
        out = qa.call(self.cfg, _BODY, "为什么要先装 Python？")
        self.assertEqual(self.rec.path, "/v1/chat/completions")
        self.assertEqual(self.rec.headers.get("Authorization"), "Bearer " + _KEY)
        self.assertEqual(self.rec.body["model"], "m1")
        self.assertFalse(self.rec.body["stream"])
        self.assertNotIn("response_format", self.rec.body)
        # 提示词从服务端来，前端说了不算
        self.assertTrue(self.rec.body["messages"][0]["content"]
                        .startswith(qa.ASK_PROMPT))
        self.assertEqual(out["answer"], "先装 Python [01:03]。")
        self.assertEqual(out["model"], "m1")

    def test_history_goes_in_as_real_turns(self) -> None:
        qa.call(self.cfg, _BODY, "那第二步呢？", [{"q": "第一步呢？", "a": "先装 [01:03]。"}])
        roles = [m["role"] for m in self.rec.body["messages"]]
        self.assertEqual(roles.count("user"), 3)     # 正文 + 上一问 + 这一问
        self.assertIn("第一步呢？", [m["content"] for m in self.rec.body["messages"]])

    def test_empty_question_never_sends(self) -> None:
        with self.assertRaises(qa.SummaryError) as cm:
            qa.call(self.cfg, _BODY, "   ")
        self.assertEqual(cm.exception.kind, "empty")
        self.assertEqual(self.rec.path, "")          # 一个请求都没发出去

    def test_empty_body_never_sends(self) -> None:
        with self.assertRaises(qa.SummaryError) as cm:
            qa.call(self.cfg, "  ", "问")
        self.assertEqual(cm.exception.kind, "empty")
        self.assertEqual(self.rec.path, "")

    def test_too_long_body_is_explained(self) -> None:
        with self.assertRaises(qa.SummaryError) as cm:
            qa.call(self.cfg, "字" * (qa.MAX_TEXT + 1), "问")
        self.assertEqual(cm.exception.status, 413)
        self.assertIn("超过单次", cm.exception.message)
        self.assertEqual(self.rec.path, "")

    def test_missing_key_never_sends(self) -> None:
        with self.assertRaises(qa.SummaryError) as cm:
            qa.call({"base": self.base, "model": "m1", "key": ""}, _BODY, "问")
        self.assertEqual(cm.exception.kind, "no_key")
        self.assertEqual(self.rec.path, "")


class EmptyAnswerTest(unittest.TestCase):
    """空回答不是「模型没话说」，是这一家服务出问题了。"""

    def test_empty_content_is_an_error(self) -> None:
        srv, _ = _serve(_reply("   "))
        try:
            cfg = {"base": "http://127.0.0.1:%d/v1" % srv.server_address[1],
                   "model": "m1", "key": _KEY}
            with self.assertRaises(qa.SummaryError) as cm:
                qa.call(cfg, _BODY, "问")
            self.assertEqual(cm.exception.kind, "parse")
            self.assertIn("空回答", cm.exception.message)
        finally:
            srv.shutdown()
            srv.server_close()


class KeyNeverLeaksTest(unittest.TestCase):
    """这一条比「好不好用」重要得多：本地服务的日志会被人粘到 issue 里。"""

    def test_key_absent_from_auth_error(self) -> None:
        srv, _ = _serve(json.dumps({"error": {"message": "Incorrect API key"}}),
                        code=401)
        try:
            cfg = {"base": "http://127.0.0.1:%d/v1" % srv.server_address[1],
                   "model": "m1", "key": _KEY}
            with self.assertRaises(qa.SummaryError) as cm:
                qa.call(cfg, _BODY, "问")
            self.assertEqual(cm.exception.kind, "auth")
            self.assertNotIn(_KEY, cm.exception.message)
            self.assertNotIn(_KEY, str(cm.exception))
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
