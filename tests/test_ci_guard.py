# -*- coding: utf-8 -*-
"""打标签前的 CI 闸门：判定要按提交算，不能按「最新一次运行」算。

这两个坑都是真的踩过的：v0.8.0 标签那轮红在 Windows 和 ubuntu 3.9 上，
而同一提交推 main 那轮是绿的——只看最新一次运行就会放行。v0.9.0 第一次
更直接：标签指向的提交本身就是红的。

测试不碰网络：判定（`judge`）、remote 解析（`parse_remote`）、pre-push 的
stdin 解析（`parse_hook_lines`）都是纯函数，喂数据断言结果即可。
"""
from __future__ import annotations

import io
import itertools
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tools import ci_guard

SHA = "969f1f3" + "0" * 33
ROOT = Path(__file__).resolve().parent.parent


def _run(sha: str, status: str = "completed", conclusion: str = "success",
         name: str = "CI") -> dict:
    return {"head_sha": sha, "status": status, "conclusion": conclusion,
            "name": name, "html_url": "https://example.invalid/r/1"}


class JudgeTest(unittest.TestCase):
    def test_success_is_green(self):
        self.assertEqual(ci_guard.judge([_run(SHA)], SHA), "success")

    def test_failure_anywhere_blocks_even_if_another_run_passed(self):
        """v0.8.0 的形状：main 绿、标签红。任何一次红都算红。"""
        runs = [_run(SHA, name="CI"), _run(SHA, conclusion="failure", name="CI")]
        self.assertEqual(ci_guard.judge(runs, SHA), "failure")

    def test_timed_out_counts_as_red(self):
        runs = [_run(SHA, conclusion="timed_out")]
        self.assertEqual(ci_guard.judge(runs, SHA), "failure")

    def test_in_progress_is_not_green(self):
        runs = [_run(SHA, status="in_progress", conclusion=None)]
        self.assertEqual(ci_guard.judge(runs, SHA), "running")

    def test_queued_is_not_green(self):
        runs = [_run(SHA, status="queued", conclusion=None)]
        self.assertEqual(ci_guard.judge(runs, SHA), "running")

    def test_no_run_for_this_sha(self):
        self.assertEqual(ci_guard.judge([_run("a" * 40)], SHA), "missing")

    def test_empty_response(self):
        self.assertEqual(ci_guard.judge([], SHA), "missing")

    def test_cancelled_does_not_pass_on_its_own(self):
        """被顶掉的运行既不是绿也不是红，但它不能冒充绿。"""
        runs = [_run(SHA, conclusion="cancelled")]
        self.assertEqual(ci_guard.judge(runs, SHA), "missing")

    def test_cancelled_alongside_success_is_green(self):
        runs = [_run(SHA, conclusion="cancelled"), _run(SHA)]
        self.assertEqual(ci_guard.judge(runs, SHA), "success")

    def test_short_sha_matches_full_head_sha(self):
        runs = [_run(SHA)]
        self.assertEqual(ci_guard.judge(runs, "969f1f3"), "success")


class ParseRemoteTest(unittest.TestCase):
    def test_https(self):
        self.assertEqual(ci_guard.parse_remote(
            "https://github.com/angus-hu-gzy/asr-polish.git"), "angus-hu-gzy/asr-polish")

    def test_https_without_dot_git(self):
        self.assertEqual(ci_guard.parse_remote(
            "https://github.com/angus-hu-gzy/asr-polish"), "angus-hu-gzy/asr-polish")

    def test_ssh(self):
        self.assertEqual(ci_guard.parse_remote(
            "git@github.com:angus-hu-gzy/asr-polish.git"), "angus-hu-gzy/asr-polish")

    def test_scp_like_without_git_suffix(self):
        self.assertEqual(ci_guard.parse_remote(
            "git@github.com:angus-hu-gzy/asr-polish"), "angus-hu-gzy/asr-polish")

    def test_garbage(self):
        self.assertEqual(ci_guard.parse_remote(""), "")
        self.assertEqual(ci_guard.parse_remote("not a url"), "")


class HookLinesTest(unittest.TestCase):
    def test_only_tags_are_guarded(self):
        text = ("refs/heads/main %s refs/heads/main %s\n"
                "refs/tags/v1.0.0 %s refs/tags/v1.0.0 %s\n"
                % (SHA, "1" * 40, SHA, "0" * 40))
        self.assertEqual(ci_guard.parse_hook_lines(text), [("v1.0.0", SHA)])

    def test_branch_push_is_ignored(self):
        text = "refs/heads/main %s refs/heads/main %s\n" % (SHA, "1" * 40)
        self.assertEqual(ci_guard.parse_hook_lines(text), [])

    def test_deleting_a_tag_is_allowed(self):
        """删标签是灭火动作，不该被闸门拦住。"""
        text = "(delete) %s refs/tags/v0.8.0 %s\n" % ("0" * 40, SHA)
        self.assertEqual(ci_guard.parse_hook_lines(text), [])

    def test_empty_stdin(self):
        self.assertEqual(ci_guard.parse_hook_lines(""), [])

    def test_malformed_line_is_skipped(self):
        text = "refs/tags/v1.0.0\n\nrefs/tags/v1.1.0 %s x\n" % SHA
        self.assertEqual(ci_guard.parse_hook_lines(text), [])

    def test_multiple_tags(self):
        a, b = "a" * 40, "b" * 40
        text = ("refs/tags/v1.0.0 %s refs/tags/v1.0.0 %s\n"
                "refs/tags/v1.1.0 %s refs/tags/v1.1.0 %s\n"
                % (a, "0" * 40, b, "0" * 40))
        self.assertEqual(ci_guard.parse_hook_lines(text),
                         [("v1.0.0", a), ("v1.1.0", b)])


class HookModeTest(unittest.TestCase):
    def _run_hook(self, argv, patches):
        """跑一次 hook 模式，把它的输出吃掉——否则测试日志里全是徽章和链接。"""
        stack = [mock.patch.object(ci_guard, k, **v) for k, v in patches.items()]
        for p in stack:
            p.start()
        try:
            with mock.patch("sys.stdin", io.StringIO("ignored\n")), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                return ci_guard.main(argv)
        finally:
            for p in reversed(stack):
                p.stop()

    def test_tag_object_sha_is_derefed_before_querying(self):
        """pre-push 给的是标签对象的 sha。拿它去查 Actions 只会得到「没有运行记录」，
        闸门就会以「查不到」的方式放行自己该拦的东西——装钩子当天就踩到了。"""
        tag_obj, commit = "a" * 40, "b" * 40
        calls = []

        def fake_fetch(repo, sha, timeout=10.0):
            calls.append(sha)
            return [_run(commit)]

        rc = self._run_hook(["--hook", "--repo", "o/r"], {
            "parse_hook_lines": {"return_value": [("v1.0.0", tag_obj)]},
            "deref_commit": {"return_value": commit},
            "fetch_runs": {"side_effect": fake_fetch},
        })
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [commit])

    def test_red_tag_blocks_in_hook_mode(self):
        commit = "b" * 40
        rc = self._run_hook(["--hook", "--repo", "o/r"], {
            "parse_hook_lines": {"return_value": [("v1.0.0", commit)]},
            "deref_commit": {"return_value": commit},
            "fetch_runs": {"return_value": [_run(commit, conclusion="failure")]},
        })
        self.assertEqual(rc, 1)

    def test_skip_env_var_bypasses(self):
        fetch = mock.Mock()
        with mock.patch.dict(os.environ, {"ASR_POLISH_SKIP_CI": "1"}), \
                mock.patch.object(ci_guard, "fetch_runs", fetch), \
                mock.patch("sys.stdin", io.StringIO("x\n")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = ci_guard.main(["--hook", "--repo", "o/r"])
        self.assertEqual(rc, 0)
        fetch.assert_not_called()


class FetchRunsTest(unittest.TestCase):
    """`?head_sha=` 依赖 GitHub 的索引，刚推上去会返回空数组——那不是「没跑过」。
    发布 0.15.0 时就是被这个挡住了：刚推完 main 立刻打标签，闸门说「没有运行记录」。"""

    def _resp(self, runs):
        return json.dumps({"workflow_runs": runs}).encode("utf-8")

    def test_empty_filtered_result_falls_back_to_recent_page(self):
        urls = []

        def fake_open(url, timeout=10.0):
            urls.append(url)
            if "head_sha=" in url:
                return self._resp([])          # 索引还没建好
            return self._resp([_run(SHA)])     # 最近一页里有

        with mock.patch.object(ci_guard, "_open", fake_open):
            runs = ci_guard.fetch_runs("o/r", SHA)

        self.assertEqual(len(urls), 2)
        self.assertNotIn("head_sha", urls[1])
        self.assertEqual(ci_guard.judge(runs, SHA), "success")

    def test_non_empty_result_does_not_ask_again(self):
        urls = []

        def fake_open(url, timeout=10.0):
            urls.append(url)
            return self._resp([_run(SHA)])

        with mock.patch.object(ci_guard, "_open", fake_open):
            runs = ci_guard.fetch_runs("o/r", SHA)

        self.assertEqual(len(urls), 1, "有结果就不该多花一次请求")
        self.assertEqual(ci_guard.judge(runs, SHA), "success")

    def test_still_missing_when_recent_page_has_nothing(self):
        with mock.patch.object(ci_guard, "_open",
                               lambda url, timeout=10.0: self._resp([_run("b" * 40)])):
            self.assertEqual(ci_guard.judge(ci_guard.fetch_runs("o/r", SHA), SHA), "missing")


class PollTest(unittest.TestCase):
    """轮询的两种等待：running 等 CI 跑完，missing 等索引建好。"""

    def _poll(self, states, wait, interval=10):
        seq = list(states)
        calls = []
        clock = itertools.count(0, interval)      # 每次读表前进一个间隔

        def fake_check(sha):
            calls.append(sha)
            return seq[min(len(calls) - 1, len(seq) - 1)]

        with mock.patch.object(ci_guard.time, "time", lambda: next(clock)), \
                mock.patch.object(ci_guard.time, "sleep", lambda s: None), \
                redirect_stderr(io.StringIO()):
            state, runs = ci_guard.poll(fake_check, SHA, wait, interval)
        return state, calls

    def test_running_is_waited_for(self):
        state, calls = self._poll([("running", []), ("running", []), ("success", [1])], 100)
        self.assertEqual(state, "success")
        self.assertEqual(len(calls), 3)

    def test_missing_is_given_a_grace_period(self):
        """索引延迟不该被当成「没跑过」。但也不能一直等——超过宽限就认命。"""
        state, calls = self._poll([("missing", [])], 100)
        self.assertEqual(state, "missing")
        self.assertEqual(len(calls), int(ci_guard.MISSING_GRACE / 10))

    def test_missing_becomes_success_once_index_catches_up(self):
        state, calls = self._poll([("missing", []), ("missing", []), ("success", [1])], 100)
        self.assertEqual(state, "success")
        self.assertEqual(len(calls), 3)

    def test_zero_wait_checks_once(self):
        for state in ("running", "missing"):
            got, calls = self._poll([(state, [])], 0)
            self.assertEqual(got, state)
            self.assertEqual(len(calls), 1)

    def test_failure_is_not_polled(self):
        got, calls = self._poll([("failure", [1])], 100)
        self.assertEqual(got, "failure")
        self.assertEqual(len(calls), 1)


class DefaultWaitTest(unittest.TestCase):
    def _wait_of(self, argv):
        seen = {}

        def fake_poll(fn, sha, wait, interval):
            seen["wait"] = wait
            return "success", [_run(sha)]

        patches = {"poll": {"side_effect": fake_poll},
                   "parse_hook_lines": {"return_value": [("v1.0.0", SHA)]},
                   "deref_commit": {"return_value": SHA},
                   "resolve_sha": {"return_value": SHA}}
        stack = [mock.patch.object(ci_guard, k, **v) for k, v in patches.items()]
        for p in stack:
            p.start()
        try:
            with mock.patch("sys.stdin", io.StringIO("x\n")), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                ci_guard.main(argv)
        finally:
            for p in reversed(stack):
                p.stop()
        return seen["wait"]

    def test_hook_waits_a_minute_by_default(self):
        """刚推完 main 就打标签是常见节奏，钩子默认等一会儿，省掉「查不到再来一次」。"""
        self.assertEqual(self._wait_of(["--hook", "--repo", "o/r"]), 60.0)

    def test_command_line_does_not_wait_by_default(self):
        self.assertEqual(self._wait_of(["--repo", "o/r"]), 0.0)

    def test_explicit_wait_wins_in_hook_mode(self):
        self.assertEqual(self._wait_of(["--hook", "--repo", "o/r", "--wait", "5"]), 5.0)


class HookScriptTest(unittest.TestCase):
    """钩子本身也是要能跑起来的代码。"""

    def setUp(self):
        self.path = ROOT / "tools" / "hooks" / "pre-push"
        self.text = self.path.read_text(encoding="utf-8")

    def test_shebang_and_lf(self):
        self.assertTrue(self.text.startswith("#!/bin/sh\n"))
        self.assertNotIn("\r", self.text, "CRLF 会让 shebang 变成 bad interpreter")

    def test_python_interpreter_is_probed(self):
        """本机是 `py`，Windows 上还有个会弹商店的假 python。不能写死。"""
        self.assertNotIn('exec "${PYTHON:-python}"', self.text)
        for cand in ("python3", "py"):
            self.assertIn(cand, self.text)

    def test_gitattributes_keeps_hook_lf(self):
        """core.autocrlf=true 的机器上，clone 会把钩子写成 CRLF。"""
        attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("tools/hooks/*", attrs)
        self.assertIn("eol=lf", attrs)


class ExitCodeTest(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(ci_guard.STATE_TEXT["success"][1], 0)
        self.assertEqual(ci_guard.STATE_TEXT["failure"][1], 1)
        self.assertEqual(ci_guard.STATE_TEXT["running"][1], 1)
        self.assertEqual(ci_guard.STATE_TEXT["missing"][1], 2)


if __name__ == "__main__":
    unittest.main()
