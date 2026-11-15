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


class BrokenJsonDetailTest(unittest.TestCase):
    """解析失败时说的是「坏在哪」，不是「开头长什么样」。

    2026-11-15 那轮真跑，P1 测验报「JSON 解析不了」。消息里固定截的开头 200 字
    全是正常内容，坏处在两百字之后——知道坏了，却看不到坏处，只能再点一次。
    这里钉的就是「消息要落在断点上」。
    """

    @staticmethod
    def _long_broken() -> str:
        """造一段 200 字之后才出问题的 JSON：中间少一个逗号。"""
        pad = ", ".join('"k%02d": "v"' % i for i in range(20))
        return '{"tldr": "ok", %s "x": "y" "z": "w"}' % pad

    def test_shows_the_break_not_the_head(self) -> None:
        body = self._long_broken()
        self.assertGreater(len(body), 200)          # 前 200 字里看不到坏处
        with self.assertRaises(summary.SummaryError) as cm:
            summary.json_object(body)
        msg = cm.exception.message
        self.assertIn("⟪⟫", msg)
        head, _, tail = msg.partition("⟪⟫")
        self.assertIn("k19", head)                  # 断点之前的一点残片
        self.assertIn('"z": "w"', tail)             # 断点之后的残片在标记右边
        self.assertNotIn("k00", msg)                # 开头那截没被塞进来
        self.assertIn("…", msg)                     # 两头都省略了

    def test_bare_newline_is_shown_not_swallowed(self) -> None:
        """字符串里的裸换行是最常见的坏法，务必让它可见。

        消息是当一行文本显示的：真换行会被折叠，折叠之后就只剩「第一行第二行」
        粘在一起，看不出问题出在换行上。
        """
        with self.assertRaises(summary.SummaryError) as cm:
            summary.json_object('{"points": [{"text": "第一行\n第二行", "t": "00:12"}]}')
        msg = cm.exception.message
        self.assertNotIn("\n", msg)
        self.assertIn("⏎", msg)
        self.assertIn("⟪⟫⏎", msg)                    # 标记正落在那处换行上

    def test_short_json_has_no_ellipsis(self) -> None:
        """整段都装得下时不要加省略号——两头都齐的片段更可信。"""
        with self.assertRaises(summary.SummaryError) as cm:
            summary.json_object('{"a": 1 "b": 2}')
        msg = cm.exception.message
        self.assertNotIn("…", msg)
        self.assertIn("⟪⟫", msg)

    def test_falls_back_when_position_is_unknown(self) -> None:
        """不是 JSONDecodeError（没有 pos）时退回截前 200 字，别抛第二个异常。"""
        msg = summary._broken_json_detail('{"a": 1', ValueError("boom"))
        self.assertIn("解析不了", msg)
        self.assertIn('{"a": 1', msg)

    def test_truncated_output_shows_the_tail_not_the_head(self) -> None:
        """输出被截断时给**末尾**：开头总是正常的，看不到开口在哪。

        这一条是真实实验逼出来的：把真实模型的答案截掉 40 字之后，大括号配不上，
        于是走的不是「解析不了」那条，而是「没拿到对象」那条——而那条当时仍然
        只给开头 200 字，用户看到的是一段漂亮完整、看不出任何问题的 JSON。
        """
        body = '{"tldr": "开头标记", "points": [' + "很长的正文" * 40
        with self.assertRaises(summary.SummaryError) as cm:
            summary.json_object(body)
        msg = cm.exception.message
        self.assertIn("没写完", msg)
        self.assertIn("很长的正文", msg)         # 末尾那截
        self.assertNotIn("开头标记", msg)        # 开头不再塞进来
        self.assertNotIn("\n", msg)

    def test_no_brace_still_quotes_the_model(self) -> None:
        """通篇没有 `{` 是另一回事：它压根没打算给 JSON，原话最有信息量。"""
        with self.assertRaises(summary.SummaryError) as cm:
            summary.json_object("抱歉，我这次没法完成。")
        self.assertIn("抱歉", cm.exception.message)
        self.assertNotIn("没写完", cm.exception.message)

    def test_keeps_kind_parse_so_it_still_retries(self) -> None:
        """消息改了，但错误种类没变——重发一次那条路仍然认得它。"""
        with self.assertRaises(summary.SummaryError) as cm:
            summary.json_object(self._long_broken())
        self.assertEqual(cm.exception.kind, "parse")


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


class ParseRetryTest(unittest.TestCase):
    """JSON 解析失败时重发一次；**别的失败一概不重发**。

    背景：2026-11-15 那轮真跑，6 篇 18 层里 P1 测验失败一次，报「JSON 解析不了」，
    同一份正文立刻单发一次就过了——模型这一次没写好，不是输入有问题。这类偶发
    不该让整层作废，也不该让用户为它点一次「只重跑失败的篇」。

    反面同样重要：key 错、地址错、限流、连不上，重发只会再花一次钱、把等待拖长，
    所以重发条件写成「kind == parse」而不是「失败就重试」。
    """

    _GOOD = _reply(json.dumps({"tldr": "讲了安装",
                               "points": [{"text": "先去官网", "t": "01:03"}]},
                              ensure_ascii=False))
    #: 配对完整、但字符串里有裸换行——json.loads 会失败。真跑那次最可能是这种。
    _BROKEN = _reply('{"tldr": "第一行\n第二行", "points": []}')

    def _cfg(self, steps=None):
        srv, recs = _serve_seq(steps or [(200, self._BROKEN), (200, self._GOOD)])
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return {"base": "http://127.0.0.1:%d/v1" % srv.server_address[1],
                "model": "m1", "key": _KEY}, recs

    def test_broken_json_is_retried_once(self) -> None:
        cfg, recs = self._cfg()
        out = summary.call(cfg, "正文")
        self.assertEqual(len(recs), 2)
        self.assertEqual(out["summary"]["tldr"], "讲了安装")
        self.assertTrue(out["retried"])

    def test_retry_sends_the_same_request(self) -> None:
        """重发就是原样再发一遍：不改提示词、不改温度。"""
        cfg, recs = self._cfg()
        summary.call(cfg, "正文")
        self.assertEqual(recs[0], recs[1])

    def test_good_reply_is_not_retried(self) -> None:
        cfg, recs = self._cfg([(200, self._GOOD)])
        out = summary.call(cfg, "正文")
        self.assertEqual(len(recs), 1)
        self.assertFalse(out["retried"])

    def test_gives_up_after_two_broken_replies(self) -> None:
        """两次都坏就抛，不能无限重发——那是用户在等、在付钱。"""
        srv, recs = _serve_seq([(200, self._BROKEN)])
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        cfg = {"base": "http://127.0.0.1:%d/v1" % srv.server_address[1],
               "model": "m1", "key": _KEY}
        with self.assertRaises(summary.SummaryError) as cm:
            summary.call(cfg, "正文")
        self.assertEqual(cm.exception.kind, "parse")
        self.assertEqual(len(recs), 2)

    def test_auth_error_is_not_retried(self) -> None:
        srv, recs = _serve_seq([(401, json.dumps(
            {"error": {"message": "Incorrect API key provided"}}))])
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        cfg = {"base": "http://127.0.0.1:%d/v1" % srv.server_address[1],
               "model": "m1", "key": _KEY}
        with self.assertRaises(summary.SummaryError) as cm:
            summary.call(cfg, "正文")
        self.assertEqual(cm.exception.kind, "auth")
        self.assertEqual(len(recs), 1)          # 一次都不多打

    def test_server_error_is_not_retried(self) -> None:
        srv, recs = _serve_seq([(500, json.dumps({"error": {"message": "boom"}}))])
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        cfg = {"base": "http://127.0.0.1:%d/v1" % srv.server_address[1],
               "model": "m1", "key": _KEY}
        with self.assertRaises(summary.SummaryError):
            summary.call(cfg, "正文")
        self.assertEqual(len(recs), 1)

    def test_no_key_still_sends_nothing(self) -> None:
        cfg, recs = self._cfg()
        with self.assertRaises(summary.SummaryError) as cm:
            summary.call({"base": cfg["base"], "model": "m1", "key": ""}, "正文")
        self.assertEqual(cm.exception.kind, "no_key")
        self.assertEqual(recs, [])


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
