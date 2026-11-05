# -*- coding: utf-8 -*-
"""MCP server：协议层要稳，工具层要复用同一套抓取逻辑。

这里不测抓取（那是 test_jobs_outcome 的事），测三件容易悄悄坏掉的事：

1. stdout 里只能有协议消息。混进一行日志，对端整个 server 就失联了——
   而且现象是「连上就断」，几乎不会有人想到是打印造成的。
2. 通知不该有回包。回一条 ``"id": null`` 的错误是 MCP 里最常见的手写 bug。
3. 工具的失败要变成 ``isError`` 的结果，不能把进程带崩。
"""
from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from asr_polish import mcp


def _lines(*msgs) -> io.StringIO:
    return io.StringIO("\n".join(json.dumps(m, ensure_ascii=False) for m in msgs) + "\n")


def _roundtrip(*msgs):
    out = io.StringIO()
    mcp.serve(_lines(*msgs), out, io.StringIO())
    text = out.getvalue()
    assert text.endswith("\n") or not text, "每条消息必须以换行结尾"
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def _call(name, **args):
    return {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": name, "arguments": args}}


class ProtocolTest(unittest.TestCase):
    def test_initialize_echoes_known_version(self):
        (r,) = _roundtrip({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertEqual(r["result"]["serverInfo"]["name"], "asr-polish")

    def test_unknown_word_version_falls_back(self):
        (r,) = _roundtrip({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(r["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)

    def test_notification_gets_no_reply(self):
        """``notifications/initialized`` 回包会让客户端直接断开。"""
        replies = _roundtrip({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(replies, [])

    def test_unknown_method_is_json_rpc_error(self):
        (r,) = _roundtrip({"jsonrpc": "2.0", "id": 2, "method": "no/such"})
        self.assertEqual(r["error"]["code"], -32601)

    def test_broken_json_does_not_kill_the_loop(self):
        """一条坏消息之后必须还能继续服务。"""
        stdin = io.StringIO("{不是json\n" + json.dumps(
            {"jsonrpc": "2.0", "id": 3, "method": "ping"}) + "\n")
        out = io.StringIO()
        mcp.serve(stdin, out, io.StringIO())
        replies = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32700)
        self.assertEqual(replies[1]["result"], {})

    def test_stdout_carries_only_protocol_messages(self):
        """把 stderr 和 stdout 分开：诊断信息一律走 stderr。"""
        out, err = io.StringIO(), io.StringIO()
        mcp.serve(_lines({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}), out, err)
        for line in out.getvalue().splitlines():
            json.loads(line)                       # 每一行都必须是合法 JSON
        self.assertEqual(err.getvalue(), "")


class ToolListTest(unittest.TestCase):
    def test_tools_have_schema_and_description(self):
        (r,) = _roundtrip({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = r["result"]["tools"]
        self.assertEqual(len(tools), 4)
        for t in tools:
            self.assertTrue(t["description"], t["name"])
            self.assertEqual(t["inputSchema"]["type"], "object")
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"fetch_subtitles", "list_videos",
                                 "correct_text", "check_login"})


class ToolCallTest(unittest.TestCase):
    def test_correct_text_returns_two_blocks(self):
        (r,) = _roundtrip(_call("correct_text", text="用接生解析数据"))
        blocks = r["result"]["content"]
        self.assertFalse(r["result"]["isError"])
        self.assertEqual(len(blocks), 2)           # 一句人话 + 一份 JSON
        self.assertIn("JSON", blocks[0]["text"])
        body = json.loads(blocks[1]["text"])
        self.assertEqual(body["text"], "用JSON解析数据")

    def test_unknown_tool_is_rpc_error(self):
        (r,) = _roundtrip(_call("nope"))
        self.assertEqual(r["error"]["code"], -32602)

    def test_tool_exception_becomes_is_error_not_crash(self):
        with mock.patch.object(mcp, "_correct", side_effect=RuntimeError("炸了")):
            (r,) = _roundtrip(_call("correct_text", text="x"))
        self.assertTrue(r["result"]["isError"])
        self.assertIn("炸了", r["result"]["content"][0]["text"])

    def test_bv_without_prefix_is_rejected_for_list(self):
        """list_videos 只接受 up:/season:/search:，给 BV 号要说清该用哪个工具。"""
        (r,) = _roundtrip(_call("list_videos", target="BV1xx411c7mD"))
        self.assertTrue(r["result"]["isError"])
        self.assertIn("fetch_subtitles", r["result"]["content"][0]["text"])


class FetchToolTest(unittest.TestCase):
    def test_partial_result_reports_failed_items_and_retry_hint(self):
        """fetch_subtitles 的返回值要能让模型直接说清「哪几条失败了」。"""
        result = {
            "outcome": "partial", "videos": 3, "parts_written": 2,
            "no_subtitle": 0, "failed": 1, "outdir": "/tmp/subs",
            "files": ["/tmp/subs/a.txt", "/tmp/subs/b.txt"],
            "items": [], "failed_items": [
                {"bvid": "BV1xx411c001", "page": None,
                 "error": "取字幕失败", "retry": "BV1xx411c001"}],
            "errors": [],
        }
        with mock.patch.object(mcp, "run_subs", return_value=result) as run:
            (r,) = _roundtrip(_call("fetch_subtitles", target="up:1", limit=3))
        self.assertFalse(r["result"]["isError"])
        head = r["result"]["content"][0]["text"]
        self.assertIn("部分成功", head)
        self.assertIn("BV1xx411c001", head)
        self.assertIn("only=", head)               # 告诉调用方可以只重跑
        self.assertEqual(run.call_args[0][0]["limit"], 3)

    def test_all_failed_still_returns_detail(self):
        """全失败时要带出明细，而不是只回一句错误。"""
        from asr_polish.server.jobs import JobFailure

        failed = {"outcome": "failed", "videos": 1, "parts_written": 0,
                  "no_subtitle": 0, "failed": 2, "outdir": "/tmp/subs",
                  "files": [], "items": [],
                  "failed_items": [{"bvid": "BV1x", "page": 2,
                                    "error": "boom", "retry": "BV1x#2"}],
                  "errors": ["boom"]}
        with mock.patch.object(mcp, "run_subs",
                               side_effect=JobFailure("全部失败", failed)):
            (r,) = _roundtrip(_call("fetch_subtitles", target="BV1x"))
        self.assertTrue(r["result"]["isError"])
        text = r["result"]["content"][0]["text"]
        self.assertIn("全部失败", text)
        self.assertIn("BV1x#2", text)

    def test_progress_notification_uses_client_token(self):
        """客户端给了 progressToken 才发进度；没给就不许往 stdout 发东西。"""
        seen = []

        def fake_run(payload, progress):
            progress(0.5, "一半了")
            return {"outcome": "complete", "videos": 1, "parts_written": 1,
                    "no_subtitle": 0, "failed": 0, "outdir": "/tmp",
                    "files": [], "items": [], "failed_items": [], "errors": []}

        msg = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
               "params": {"name": "fetch_subtitles",
                          "arguments": {"target": "BV1x"},
                          "_meta": {"progressToken": "tok-1"}}}
        with mock.patch.object(mcp, "run_subs", side_effect=fake_run):
            replies = _roundtrip(msg)
        notes = [r for r in replies if r.get("method") == "notifications/progress"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["params"]["progressToken"], "tok-1")
        seen.append(notes[0]["params"]["progress"])
        self.assertAlmostEqual(seen[0], 0.5, places=3)
        self.assertEqual(replies[-1]["id"], 7)    # 响应在通知之后

    def test_no_progress_token_means_no_notifications(self):
        def fake_run(payload, progress):
            progress(0.5, "一半了")
            return {"outcome": "complete", "videos": 1, "parts_written": 1,
                    "no_subtitle": 0, "failed": 0, "outdir": "/tmp",
                    "files": [], "items": [], "failed_items": [], "errors": []}

        with mock.patch.object(mcp, "run_subs", side_effect=fake_run):
            replies = _roundtrip(_call("fetch_subtitles", target="BV1x"))
        self.assertEqual(len(replies), 1)

    def test_multi_part_video_is_capped_by_default(self):
        """一条 84 P 的合集不该被当成「一条视频」打 84 次请求。"""
        payloads = []

        def fake_run(payload, progress):
            payloads.append(payload)
            return {"outcome": "complete", "videos": 1, "parts_written": 20,
                    "no_subtitle": 0, "failed": 0, "outdir": "/tmp",
                    "files": [], "items": [], "failed_items": [], "errors": [],
                    "parts_truncated": 64}

        with mock.patch.object(mcp, "run_subs", side_effect=fake_run):
            (r,) = _roundtrip(_call("fetch_subtitles", target="BV1x"))
        self.assertEqual(payloads[0]["max_parts"], 20)
        self.assertIn("max_parts=0", r["result"]["content"][0]["text"])

    def test_body_files_are_trimmed_with_a_note(self):
        result = {"outcome": "complete", "videos": 1, "parts_written": 83,
                  "no_subtitle": 0, "failed": 0, "outdir": "/tmp/subs",
                  "files": ["/tmp/subs/f%03d.txt" % i for i in range(166)],
                  "items": [{"bvid": "BV1x"}], "failed_items": [], "errors": []}
        with mock.patch.object(mcp, "run_subs", return_value=result):
            (r,) = _roundtrip(_call("fetch_subtitles", target="BV1x"))
        body = json.loads(r["result"]["content"][1]["text"])
        self.assertEqual(len(body["files"]), mcp._MAX_FILES_IN_BODY)
        self.assertEqual(body["files_total"], 166)
        self.assertIn("只列了前", body["files_note"])
        self.assertNotIn("items", body)          # 逐视频明细不塞给模型


if __name__ == "__main__":
    unittest.main()
