"""发布前的 CI 闸门：这个提交在 GitHub Actions 上到底绿了没有。

为什么需要它——打红标签这件事已经发生过两次：

    v0.8.0   标签那轮 CI 红了两个 job（Windows cp1252、ubuntu 3.9 的 #HttpOnly_），
             没人去 Actions 看，标签就这么打下去了，直到下一版才发现。
    v0.9.0   第一次标签指向的提交本身是红的，只能删标签、重指、重推。

两次的共同点：**打标签只看「代码改完了没」，不看「CI 过了没」。** 而 CI 是异步的，
推上去之后要等几分钟，人早就去干别的了。

这个脚本把「看 CI」变成一条命令、一个退出码：

    python tools/ci_guard.py                 # 检查 HEAD
    python tools/ci_guard.py --sha 969f1f3   # 检查指定提交
    python tools/ci_guard.py --wait 600      # 正在跑就等，最多等 10 分钟

退出码：0 绿（可以打标签）/ 1 红或还在跑 / 2 查不到（没跑过、网络或限流）。

它同时是 `tools/hooks/pre-push` 的内核：装好之后推标签会被自动拦一道，
推分支不受影响——日常提交不值得为此等 CI。

    git config core.hooksPath tools/hooks

绕过（明知自己在干什么时）：

    ASR_POLISH_SKIP_CI=1 git push origin v1.0.0
    git push --no-verify origin v1.0.0

判定按**提交**而不是按「某一次运行」：同一个 sha 上可能有两次运行（推 main 一次、
推标签一次），任何一次红都算红。v0.8.0 正是「main 绿、标签红」，只看最新一次
运行会漏掉。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"

#: 这些结论都算红。cancelled / skipped 不算——被后一次推送顶掉是常态，不是失败。
RED = frozenset({"failure", "timed_out", "startup_failure"})
PENDING = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})

STATE_TEXT = {
    "success": ("绿了", 0),
    "failure": ("红了", 1),
    "running": ("还在跑", 1),
    "missing": ("没有跑过", 2),
    "unknown": ("查不到", 2),
}


def _warn(msg: str) -> None:
    print(msg, file=sys.stderr)


# --------------------------------------------------------------------------- #
# 判定：纯函数，不碰网络，测试直接打这里
# --------------------------------------------------------------------------- #

def judge(runs: list, sha: str) -> str:
    """按提交判定：success / failure / running / missing。

    `runs` 是 Actions API 的 workflow_runs 列表（可以混着别的提交，这里只挑
    head_sha 对得上的）。
    """
    mine = [r for r in runs if (r.get("head_sha") or "").startswith(sha[:7])]
    if not mine:
        return "missing"

    reds = [r for r in mine if r.get("conclusion") in RED]
    if reds:
        return "failure"

    if any(r.get("status") in PENDING or not r.get("conclusion") for r in mine):
        return "running"

    if any(r.get("conclusion") == "success" for r in mine):
        return "success"

    # 只剩 cancelled / skipped 这类中性结论：没有任何一次真正跑完过。
    return "missing"


def parse_hook_lines(text: str) -> list:
    """解析 pre-push 的 stdin：`<local ref> <local sha> <remote ref> <remote sha>`。

    只挑出**要推的标签**。分支（refs/heads/...）直接放行；删除（local sha 全 0）
    也放行——删标签本来就是灭火操作，不该被闸门拦住。
    """
    tags = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha = parts[0], parts[1]
        if not local_ref.startswith("refs/tags/"):
            continue
        if set(local_sha) == {"0"}:
            continue
        tags.append((local_ref[len("refs/tags/"):], local_sha))
    return tags


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #

def _open(url: str, timeout: float = 10.0) -> bytes:
    headers = {"User-Agent": "asr-polish-ci-guard",
               "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError:
        raise
    except urllib.error.URLError as exc:
        # 本机 CA 链不完整时（企业代理、Windows 上的自制根证书）标准库会拒连。
        # 这是只读的公开数据，降级到不校验比让人卡住划算，但要说出来。
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
            raise
        _warn("  ！本机 CA 校验不通过（缺 key usage 扩展），本次请求跳过校验")
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read()


def detect_repo() -> str:
    """"owner/name"，从 origin 的 URL 里读。"""
    try:
        url = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        url = ""
    return parse_remote(url)


def parse_remote(url: str) -> str:
    """把 https / ssh / scp 三种写法都收成 owner/name，认不出返回空串。"""
    url = (url or "").strip()
    if not url:
        return ""
    m = re.search(r"[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", url)
    return "%s/%s" % (m.group(1), m.group(2)) if m else ""


def resolve_sha(ref: str) -> str:
    out = subprocess.run(["git", "rev-parse", ref], capture_output=True,
                         text=True, timeout=10)
    if out.returncode != 0:
        raise SystemExit("找不到这个提交：%s" % ref)
    return out.stdout.strip()


def deref_commit(sha: str) -> str:
    """把「标签对象」解成它指向的提交。

    pre-push 给的是**标签对象的 sha**，不是提交的 sha——注解标签本身就是个对象。
    拿标签对象的 sha 去查 Actions，结果是「这个提交没有任何运行记录」，闸门等于
    永远放行自己该拦的东西。第一次装钩子时就是这个坑：拦是拦住了，理由却是错的。
    """
    out = subprocess.run(["git", "rev-parse", sha + "^{commit}"],
                         capture_output=True, text=True, timeout=10)
    return out.stdout.strip() if out.returncode == 0 else sha


def fetch_runs(repo: str, sha: str, timeout: float = 10.0) -> list:
    """取这个提交可能相关的运行。公开仓库不带 token 也能读（每小时 60 次）。"""
    url = "%s/repos/%s/actions/runs?per_page=40&head_sha=%s" % (API, repo, sha)
    try:
        data = json.loads(_open(url, timeout).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise SystemExit("查不到：GitHub 限流了。设 GITHUB_TOKEN 环境变量可提高额度。")
        if exc.code == 404:
            raise SystemExit("查不到：仓库 %s 不存在，或者是私有的（要 token）。" % repo)
        raise SystemExit("查不到：HTTP %s" % exc.code)
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit("查不到：连不上 GitHub（%s）" % exc)
    return data.get("workflow_runs") or []


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #

def describe(runs: list, sha: str, state: str) -> str:
    mine = [r for r in runs if (r.get("head_sha") or "").startswith(sha[:7])]
    if not mine:
        return ("这个提交在 Actions 上没有任何运行记录。\n"
                "  多半是刚 push、CI 还没起来，等一两分钟再看；\n"
                "  也可能这个提交根本没触发 CI（改了无关路径、或者 workflow 有 paths 过滤）。")
    lines = []
    for r in mine:
        lines.append("  %s %-9s %s  %s" % (
            r.get("head_sha", "")[:7], r.get("conclusion") or r.get("status"),
            r.get("name") or "?", r.get("html_url") or ""))
    return "\n".join(lines)


def check(repo: str, sha: str) -> tuple:
    runs = fetch_runs(repo, sha)
    state = judge(runs, sha)
    return state, runs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ci_guard",
        description="打标签前确认这个提交的 CI 绿了没有（0 绿 / 1 红或未完成 / 2 查不到）")
    ap.add_argument("--sha", default="HEAD", help="要检查的提交（默认 HEAD）")
    ap.add_argument("--repo", default=None, help="owner/name（默认从 origin 读）")
    ap.add_argument("--wait", type=float, default=0,
                    help="还在跑就轮询等待的秒数（默认不等）")
    ap.add_argument("--interval", type=float, default=15, help="轮询间隔秒数")
    ap.add_argument("--hook", action="store_true",
                    help="pre-push 模式：从 stdin 读 refs，只拦标签")
    args = ap.parse_args(argv)

    if args.hook and os.environ.get("ASR_POLISH_SKIP_CI"):
        return 0

    repo = args.repo or detect_repo()
    if not repo:
        _warn("认不出仓库地址（origin 没配？），跳过 CI 检查。")
        return 0

    if args.hook:
        tags = parse_hook_lines(sys.stdin.read())
        if not tags:
            return 0
        targets = [(name, deref_commit(sha)) for name, sha in tags]
    else:
        targets = [(args.sha, resolve_sha(args.sha))]

    rc = 0
    for name, sha in targets:
        deadline = time.time() + max(0.0, args.wait)
        while True:
            state, runs = check(repo, sha)
            if state == "running" and time.time() < deadline:
                _warn("  %s 的 CI 还在跑，等 %ds…" % (sha[:7], args.interval))
                time.sleep(args.interval)
                continue
            break

        text, code = STATE_TEXT[state]
        label = name if args.hook else sha[:7]
        print("%s  %s" % (label, text), flush=True)
        print(describe(runs, sha, state), flush=True)
        if state != "success":
            rc = max(rc, code)

    if rc and args.hook:
        _warn("")
        _warn("拦住了一次标签推送：上面这个提交的 CI 不是绿的。")
        _warn("  等它绿了再推；确认要硬推的话：ASR_POLISH_SKIP_CI=1 git push origin <tag>")
    return rc


if __name__ == "__main__":
    sys.exit(main())
