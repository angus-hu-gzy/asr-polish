# -*- coding: utf-8 -*-
"""单篇总结的服务端部分：端点拼装、时间解析、模型输出兜底、错误翻译。

为什么值得单独一个文件
----------------------
这一层最容易出的错不是「崩了」，而是「错了但没人发现」：把时间戳算错一位、
把提供方的话吞掉、把 key 拼进错误消息里。前两种只会让用户觉得「这功能不准」，
第三种是事故。所以这里既测纯函数，也真的起一个假模型服务，把请求发过去看一眼。

假服务用标准库 http.server，不占固定端口（bind 到 0 让系统分配），跑完就关。
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from asr_polish.server import summary

_KEY = "sk-secret-must-not-leak"


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
    """包成 chat/completions 的形状。"""
    return json.dumps({"choices": [{"message": {"content": content}}]})


def _serve_seq(steps):
    """按顺序回，``steps`` 是 ``[(http_code, body), ...]``，多出来的请求重复最后一条。

    返回 ``(srv, recs)``；``recs`` 收每一次的请求体——降级重试要看清第二次带了
    什么、一共打了几次。
    """
    recs: list = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:                       # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                recs.append(json.loads(raw.decode("utf-8")))
            except ValueError:
                recs.append({})
            code, text = steps[min(len(recs) - 1, len(steps) - 1)]
            payload = text.encode("utf-8")
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
    return srv, recs


class BaseUrlTest(unittest.TestCase):

    def test_keeps_a_full_endpoint(self) -> None:
        self.assertEqual(summary.base_url("https://a.com/v1/chat/completions"),
                         "https://a.com/v1/chat/completions")

    def test_appends_the_path(self) -> None:
        self.assertEqual(summary.base_url("https://a.com/v1"),
                         "https://a.com/v1/chat/completions")
        self.assertEqual(summary.base_url("https://a.com/v1/"),   # 尾部斜杠
                         "https://a.com/v1/chat/completions")

    def test_bare_host_defaults_to_https(self) -> None:
        """只贴域名也能用——人复制地址时常常不带协议。"""
        self.assertEqual(summary.base_url("api.deepseek.com/v1"),
                         "https://api.deepseek.com/v1/chat/completions")

    def test_http_is_respected(self) -> None:
        """本机 Ollama 之类要走 http，写明了就照办，不擅自升级成 https。"""
        self.assertEqual(summary.base_url("http://127.0.0.1:11434/v1"),
                         "http://127.0.0.1:11434/v1/chat/completions")

    def test_empty_is_a_clear_error(self) -> None:
        with self.assertRaises(summary.SummaryError) as cm:
            summary.base_url("   ")
        self.assertEqual(cm.exception.kind, "bad_base")
        self.assertIn("没填模型服务地址", cm.exception.message)


class StampTest(unittest.TestCase):
    """模型给的时间可能是三种写法，都要认；认不出就是认不出。"""

    def test_formats(self) -> None:
        self.assertEqual(summary.parse_stamp("01:03"), 63)
        self.assertEqual(summary.parse_stamp("1:02:05"), 3725)
        self.assertEqual(summary.parse_stamp("83"), 83)
        self.assertEqual(summary.parse_stamp(83), 83)
        self.assertEqual(summary.parse_stamp("[01:03]"), 63)   # 把方括号也抄回来了

    def test_unknown_is_none(self) -> None:
        for bad in ("刚才那里", "", None, "::", "-3"):
            self.assertIsNone(summary.parse_stamp(bad), bad)

    def test_does_not_invent_minutes(self) -> None:
        """"3" 是 3 秒，不是 3 分钟——不做任何补全。"""
        self.assertEqual(summary.parse_stamp("3"), 3)


class NormalizeTest(unittest.TestCase):

    def test_standard_shape(self) -> None:
        out = summary.normalize({
            "tldr": "这篇讲安装",
            "points": [{"text": "先装 Python", "t": "00:12"}],
            "concepts": [{"term": "解释器", "def": "执行 Python 的程序"}],
        })
        self.assertEqual(out["tldr"], "这篇讲安装")
        self.assertEqual(out["points"], [{"text": "先装 Python", "t": 12}])
        self.assertEqual(out["concepts"],
                         [{"term": "解释器", "def": "执行 Python 的程序"}])

    def test_aliases_and_loose_points(self) -> None:
        """模型爱换字段名：key_points / topics、要点写成纯字符串。"""
        out = summary.normalize({"summary": "总览", "topics": ["第一条", "第二条"]})
        self.assertEqual(out["tldr"], "总览")
        self.assertEqual([p["text"] for p in out["points"]], ["第一条", "第二条"])
        self.assertTrue(all(p["t"] is None for p in out["points"]))

    def test_empty_entries_are_dropped(self) -> None:
        out = summary.normalize({"tldr": "", "points": [{"text": "  "}, {"text": "有效"}],
                                 "concepts": [{"def": "没有术语"}]})
        self.assertEqual([p["text"] for p in out["points"]], ["有效"])
        self.assertEqual(out["concepts"], [])

    def test_nothing_useful_is_an_error(self) -> None:
        """既没有总览也没有要点：不能返回一份空摘要让人以为做完了。"""
        with self.assertRaises(summary.SummaryError) as cm:
            summary.normalize({"tldr": "", "points": []})
        self.assertEqual(cm.exception.kind, "parse")


class ParseReplyTest(unittest.TestCase):

    def test_plain_json(self) -> None:
        out = summary.parse_reply(_reply('{"tldr":"一句话","points":[{"text":"要点","t":null}]}'))
        self.assertEqual(out["tldr"], "一句话")

    def test_markdown_fence(self) -> None:
        """提示词说了不要围栏，但模型经常还是加。"""
        out = summary.parse_reply(_reply('```json\n{"tldr":"带围栏"}\n```'))
        self.assertEqual(out["tldr"], "带围栏")

    def test_chatter_before_json(self) -> None:
        """前面先写一句「好的，以下是摘要：」，后面才是 JSON。"""
        out = summary.parse_reply(_reply('好的，以下是摘要：\n{"tldr":"有前言"}'))
        self.assertEqual(out["tldr"], "有前言")

    def test_braces_inside_strings(self) -> None:
        """正文里带花括号不能把配对算错。"""
        out = summary.parse_reply(_reply('{"tldr":"用 f{}{x} 的写法","points":[]}'))
        self.assertEqual(out["tldr"], "用 f{}{x} 的写法")

    def test_not_json_at_all(self) -> None:
        with self.assertRaises(summary.SummaryError) as cm:
            summary.parse_reply("<html>502</html>")
        self.assertEqual(cm.exception.kind, "parse")

    def test_model_said_no_json_back(self) -> None:
        """模型压根没按 JSON 回：把它的原话带给用户，别只说「解析失败」。"""
        with self.assertRaises(summary.SummaryError) as cm:
            summary.parse_reply(_reply("抱歉，我无法完成这个请求。"))
        self.assertIn("抱歉", cm.exception.message)

    def test_error_envelope(self) -> None:
        with self.assertRaises(summary.SummaryError) as cm:
            summary.parse_reply(json.dumps({"error": {"message": "model not found"}}))
        self.assertIn("model not found", cm.exception.message)


class ProviderErrorTest(unittest.TestCase):
    """错误必须分得清是哪一种，而且带着提供方的原话。"""

    def _err(self, code: int, body: str):
        import io
        import urllib.error
        return urllib.error.HTTPError(
            "https://a.com/v1/chat/completions", code, "x", {},
            io.BytesIO(body.encode("utf-8")))

    def test_auth(self) -> None:
        e = summary.http_error(self._err(401, '{"error":{"message":"Invalid API key"}}'))
        self.assertEqual(e.kind, "auth")
        self.assertEqual(e.status, 401)
        self.assertIn("Invalid API key", e.message)

    def test_rate(self) -> None:
        e = summary.http_error(self._err(429, '{"error":{"message":"quota exceeded"}}'))
        self.assertEqual(e.kind, "rate")
        self.assertIn("quota exceeded", e.message)

    def test_bad_endpoint(self) -> None:
        e = summary.http_error(self._err(404, '{"error":{"message":"no such route"}}'))
        self.assertEqual(e.kind, "bad_base")
        self.assertIn("/v1", e.message)          # 提示该检查哪一层

    def test_provider_outage(self) -> None:
        e = summary.http_error(self._err(500, "upstream exploded"))
        self.assertEqual(e.kind, "model")
        self.assertIn("upstream exploded", e.message)

    def test_unreadable_body_still_explains(self) -> None:
        e = summary.http_error(self._err(500, ""))
        self.assertIn("HTTP 500", e.message)

    def test_the_failed_response_is_closed(self) -> None:
        """翻译完就把响应关掉。

        HTTPError 自己就是一份打开着的响应。以前只读不关，它挂在 GC 上等着被
        回收，Python 3.13+ 每条失败都会打一条 ResourceWarning——那行噪音长得
        像「我们自己漏了资源」，正好盖住真正的失败原因；顺带每失败一次漏一个
        连接。这一层（总结/问答/测验/笔记四条路都走这里）失败是常事，限流尤其。
        """
        exc = self._err(429, '{"error":{"message":"slow down"}}')
        fp = exc.fp
        summary.http_error(exc)
        self.assertTrue(fp.closed, "响应体读完了却还开着")

    def test_closing_a_broken_response_is_not_an_error(self) -> None:
        """关不掉就算了——不能因为「顺手关一下」把真正的异常换掉。

        3.9 的 `addbase.__getattr__` 是往 `self.fp` 上转发的：fp 不是真文件对象
        时（测试里塞的替身、或者已经关过一次的对象）抛的是 `KeyError`，不是
        `AttributeError`，所以连 `except Exception` 的位置都很讲究。CI 的 3.9
        那一路就是被这个弄红的。
        """
        class Ok:
            closed = False

            def close(self) -> None:
                self.closed = True

        class Broken:
            def close(self) -> None:
                raise KeyError("file")

        ok = Ok()
        summary.close_quietly(ok)
        self.assertTrue(ok.closed)
        summary.close_quietly(Broken())          # 不抛就算过
        summary.close_quietly(None)              # None 也不该炸


class TransportTest(unittest.TestCase):

    def test_timeout(self) -> None:
        e = summary.transport_error(TimeoutError("timed out"))
        self.assertEqual(e.kind, "timeout")

    def test_refused(self) -> None:
        e = summary.transport_error(ConnectionRefusedError("refused"))
        self.assertEqual(e.kind, "connect")
        self.assertIn("refused", e.message)

    def test_certificate(self) -> None:
        e = summary.transport_error(
            OSError("CERTIFICATE_VERIFY_FAILED: unable to get local issuer"))
        self.assertEqual(e.kind, "connect")
        self.assertIn("证书", e.message)


class CallTest(unittest.TestCase):
    """真发一次请求到假服务：看它收到什么、拿回来什么。"""

    def setUp(self) -> None:
        self.srv, self.rec = _serve(_reply(json.dumps({
            "tldr": "讲了安装",
            "points": [{"text": "先去官网", "t": "01:03"}],
            "concepts": [{"term": "PATH", "def": "找可执行文件的路径"}],
        }, ensure_ascii=False)))
        self.base = "http://127.0.0.1:%d/v1" % self.srv.server_address[1]

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def test_request_shape(self) -> None:
        out = summary.call({"base": self.base, "model": "m1", "key": _KEY}, "第一段")
        self.assertEqual(self.rec.path, "/v1/chat/completions")
        self.assertEqual(self.rec.headers.get("Authorization"), "Bearer " + _KEY)
        self.assertEqual(self.rec.body["model"], "m1")
        self.assertFalse(self.rec.body["stream"])
        # 提示词从服务端来，前端说了不算
        self.assertEqual(self.rec.body["messages"][0]["content"], summary.PROMPT)
        self.assertEqual(self.rec.body["messages"][1]["content"], "第一段")
        # 总结要的是 JSON 对象，采样阶段就约束住，别等写完再去残片里挖。
        # （没带会把 400 的端点由 complete 兜底降级，见 JsonModeFallbackTest）
        self.assertEqual(self.rec.body["response_format"], {"type": "json_object"})

        self.assertEqual(out["model"], "m1")
        self.assertEqual(out["summary"]["tldr"], "讲了安装")
        self.assertEqual(out["summary"]["points"][0]["t"], 63)

    def test_missing_key_never_sends(self) -> None:
        with self.assertRaises(summary.SummaryError) as cm:
            summary.call({"base": self.base, "model": "m1", "key": "  "}, "正文")
        self.assertEqual(cm.exception.kind, "no_key")
        self.assertEqual(self.rec.path, "")        # 一个请求都没发出去

    def test_empty_text_never_sends(self) -> None:
        with self.assertRaises(summary.SummaryError) as cm:
            summary.call({"base": self.base, "model": "m1", "key": _KEY}, "   ")
        self.assertEqual(cm.exception.kind, "empty")
        self.assertEqual(self.rec.path, "")


class JsonModeFallbackTest(unittest.TestCase):
    """要 JSON 的三层带上采样约束；不认这个参数的端点自动降级，别的 400 不降。

    这一层值得盯着的地方是「别把真错误吞掉」：降级的触发条件必须是**点名**
    response_format 的 400。放宽成「400 就重试」，模型名写错也会被静默重发一遍，
    用户最后看到的是同一条 400，但多花了一次调用、原因还更难认。
    """

    _GOOD = {
        "summary": '{"tldr":"讲了安装","points":[]}',
        "quiz": '{"questions":[{"stem":"题","options":["a","b","c","d"],"answer":"A"}]}',
        "notes": '{"points":[{"text":"要点","t":"00:10"}],"concepts":[]}',
    }

    def setUp(self) -> None:
        summary._NO_JSON_MODE.clear()
        self._srvs: list = []

    def tearDown(self) -> None:
        for srv in self._srvs:
            srv.shutdown()
            srv.server_close()
        summary._NO_JSON_MODE.clear()

    def _start(self, *steps):
        srv, recs = _serve_seq(list(steps))
        self._srvs.append(srv)
        return "http://127.0.0.1:%d/v1" % srv.server_address[1], recs

    def _cfg(self, base: str) -> dict:
        return {"base": base, "model": "m1", "key": _KEY}

    def test_fallback_retries_without_the_param(self) -> None:
        base, recs = self._start(
            (400, '{"error":{"message":"Unknown parameter: response_format"}}'),
            (200, _reply(self._GOOD["summary"])))
        out = summary.call(self._cfg(base), "正文")
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0].get("response_format"), {"type": "json_object"})
        self.assertNotIn("response_format", recs[1])
        self.assertEqual(out["summary"]["tldr"], "讲了安装")   # 降级之后照常出结果

    def test_endpoint_without_support_is_remembered(self) -> None:
        """同一端点第二次别再白打一遍注定被拒的请求——那也是一次调用。"""
        base, recs = self._start(
            (400, '{"error":{"message":"response_format is not supported"}}'),
            (200, _reply(self._GOOD["summary"])))
        summary.call(self._cfg(base), "正文")
        self.assertEqual(len(recs), 2)
        summary.call(self._cfg(base), "正文")
        self.assertEqual(len(recs), 3)
        self.assertNotIn("response_format", recs[2])

    def test_other_bad_request_is_not_retried(self) -> None:
        base, recs = self._start(
            (400, '{"error":{"message":"model not found"}}'),
            (200, _reply(self._GOOD["summary"])))
        with self.assertRaises(summary.SummaryError) as cm:
            summary.call(self._cfg(base), "正文")
        self.assertEqual(len(recs), 1)
        self.assertIn("model not found", cm.exception.message)

    def test_quiz_and_notes_also_ask(self) -> None:
        from asr_polish.server import notes, quiz
        base, recs = self._start((200, _reply(self._GOOD["quiz"])),
                                 (200, _reply(self._GOOD["notes"])))
        quiz.call(self._cfg(base), "正文")
        notes.call(self._cfg(base), "正文")
        self.assertEqual([r.get("response_format") for r in recs],
                         [{"type": "json_object"}] * 2)

    def test_qa_does_not_ask(self) -> None:
        """问答要的是人话。开了这个开关，模型会把回答包进对象里，答错还看不见。"""
        from asr_polish.server import qa
        base, recs = self._start((200, _reply("先装 Python。")))
        out = qa.call(self._cfg(base), "正文", "怎么装")
        self.assertEqual(out["answer"], "先装 Python。")
        self.assertNotIn("response_format", recs[0])

    def test_three_json_prompts_still_say_json(self) -> None:
        """JSON mode 的隐藏前提：提示词里得有 "json" 字样。

        DeepSeek 这类服务会校验，缺了直接 400（Prompt must contain the word
        'json'），而那个 400 不点名 response_format——它不会被当成降级理由，而是
        一路抛上去，三层同时挂。所以这里钉住：谁把这句删了，先红在这里。
        """
        from asr_polish.server import notes, quiz
        for name, prompt in (("summary", summary.PROMPT),
                             ("quiz", quiz.QUIZ_PROMPT),
                             ("notes", notes.PROMPT)):
            self.assertIn("json", prompt.lower(), name)


class KeyNeverLeaksTest(unittest.TestCase):
    """key 不进任何一种错误消息。这条比「功能好不好用」重要得多。"""

    def test_key_absent_from_auth_error(self) -> None:
        srv, _ = _serve(json.dumps({"error": {"message": "Incorrect API key provided"}}),
                        code=401)
        try:
            base = "http://127.0.0.1:%d/v1" % srv.server_address[1]
            with self.assertRaises(summary.SummaryError) as cm:
                summary.call({"base": base, "model": "m1", "key": _KEY}, "正文")
            self.assertEqual(cm.exception.kind, "auth")
            self.assertNotIn(_KEY, cm.exception.message)
            self.assertNotIn(_KEY, str(cm.exception))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_key_absent_from_connection_error(self) -> None:
        # 连不上的那种错。不断言 kind：本机若配了 http_proxy，这个请求可能被
        # 代理接住并返回一个 HTTP 错误页（于是变成 model 而不是 connect）——
        # 两台机器上跑出两个结论的断言，不如只守这条测试真正管的事：
        # **不管落到哪一种错误，key 都不许出现在消息里。**
        with self.assertRaises(summary.SummaryError) as cm:
            summary.call({"base": "http://127.0.0.1:1/v1", "model": "m1", "key": _KEY},
                         "正文")
        self.assertNotIn(_KEY, cm.exception.message)


if __name__ == "__main__":
    unittest.main()
