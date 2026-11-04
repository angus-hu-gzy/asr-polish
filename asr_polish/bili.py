# -*- coding: utf-8 -*-
"""B站视频文本获取：字幕轨优先，无字幕走必剪云端 ASR。

设计原则：
- 术语订正 / 交叉验证 / 质量自评与本模块解耦，同样零依赖。
- 本模块除「必剪 ASR 下载音频」需要 yt-dlp 外，其余全部纯标准库实现。
  yt-dlp 作为可选依赖，仅在走到「无字幕 → 必剪 ASR」时才 import：
      pip install asr-polish[bilibili]     # 或直接 pip install yt-dlp

降级链：
    字幕轨（wbi 签名，无需登录；有 Cookie 可多取到 AI 字幕）
        → 必剪云端 ASR（13 秒级，需 yt-dlp 下载音频）

隐私：必剪 ASR 会把音频上传至 B站服务器，对象为公开视频音轨，风险低。

必剪 ASR 协议参考 GitHub SocialSisterYi/bcut-asr（MIT），此处为纯标准库重写。
"""

from __future__ import annotations

import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ._atomic import replace_with_retry
from .throttle import Throttle

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# wbi 签名的 mixin_key 重排表（B站公开反爬算法常量）
_WBI_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

# 必剪云端 ASR 接口
_BCUT_REQ_UPLOAD = "https://member.bilibili.com/x/bcut/rubick-interface/resource/create"
_BCUT_COMMIT_UPLOAD = "https://member.bilibili.com/x/bcut/rubick-interface/resource/create/complete"
_BCUT_CREATE_TASK = "https://member.bilibili.com/x/bcut/rubick-interface/task"
_BCUT_QUERY_RESULT = "https://member.bilibili.com/x/bcut/rubick-interface/task/result"
# 必剪 ASR 模型版本。7 已于 2026-11 失效（resource/create 直接返回
# 139201 第三方服务异常），线上可用版本为 8。
_BCUT_MODEL_ID = 8

# 默认 cookies.txt 查找路径（兼容 WorkBuddy skill 的既有位置）
_DEFAULT_COOKIE_PATHS = (
    Path.home() / ".workbuddy" / "data" / "bilibili-video-summary" / "bilibili_cookies.txt",
    Path.home() / "Downloads" / "www.bilibili.com_cookies.txt",
    Path.home() / ".config" / "asr-polish" / "cookies.txt",
)

_BVID_RE = re.compile(r"BV[0-9A-Za-z]+")
_MID_RE = re.compile(r"space\.bilibili\.com/(\d+)")


class BiliError(Exception):
    """B站文本获取过程中的可预期错误。"""


class BiliRiskControl(BiliError):
    """被风控/限流拦下（code -352 / -412 / -799 / -509，或 HTTP 412 / 429）。

    这类错误和「这条视频没有字幕」性质完全不同：**它不是单条视频的问题**，
    而是当前 IP / 账号被临时限制了。继续打下一条只会延长封禁时间，所以上层
    应当整体退避一段时间再继续，而不是把这条记成失败然后接着打。
    """

    def __init__(self, message: str, code: Optional[int] = None,
                 retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after      # 服务端建议的等待秒数（若有）


class BiliLoginRequired(BiliRiskControl):
    """需要登录态（code -101 账号未登录 / -111 csrf 失败）。

    UP 主投稿、合集这类接口对未登录请求直接返回风控失败。**退避没用**——
    Cookie 不换，等多久都是同样的结果。所以单独成一个类型，让上层直接
    给出「更新 Cookie」的提示，而不是傻等几分钟再失败一次。
    """


#: 风控类返回码。命中任何一个都意味着「现在不该继续请求」。
_RISK_CODES = frozenset({-352, -412, -799, -509})
#: 需要登录态才能继续的返回码。
_LOGIN_CODES = frozenset({-101, -111})

#: 全局节流器。默认 1 秒一个 API 请求——批量抓时这是「不被封」和「不太慢」
#: 之间的保守取值，可用 configure() / CLI --min-interval 调整。
_throttle = Throttle(min_interval=1.0)


def configure(min_interval: Optional[float] = None,
              jitter: Optional[float] = None,
              enabled: Optional[bool] = None) -> None:
    """调整全局限流参数。手工调小/关掉仅供测试与排障。"""
    if enabled is not None and not enabled:
        _throttle.configure(min_interval=0.0)
        _throttle.reset()
        return
    _throttle.configure(min_interval=min_interval, jitter=jitter)
    _throttle.reset()


def current_min_interval() -> float:
    """当前生效的请求间隔下限（秒）。"""
    return _throttle.min_interval


def throttle_waited() -> float:
    """累计为遵守节流而等待的秒数。批量任务跑完后能看出「慢在哪」。"""
    return _throttle.waited


def _check_code(data: dict, what: str) -> None:
    """检查 B站返回的业务码，把风控/未登录单独抛出来。

    B站几乎所有接口都返回 ``{"code": 0, "message": "0", "data": ...}``，
    风控时 ``code`` 是负数而 ``data`` 为 null。老代码只看 ``data`` 空不空，
    于是风控被当成「这条视频取不到信息」——一条一条接着打，正好帮对方把
    封禁续上。
    """
    code = data.get("code")
    if code in (None, 0):
        return
    try:
        code_i = int(code)
    except (TypeError, ValueError):
        raise BiliError(f"{what}返回了非数字 code：{code!r}") from None
    msg = data.get("message") or ""
    if code_i in _LOGIN_CODES:
        raise BiliLoginRequired(
            f"{what}需要登录态（code {code_i}：{msg}）", code=code_i)
    if code_i in _RISK_CODES:
        raise BiliRiskControl(
            f"{what}触发风控（code {code_i}：{msg}）", code=code_i)
    raise BiliError(f"{what}失败（code {code_i}：{msg}）")


@dataclass
class FetchResult:
    bvid: str
    title: str
    owner: str
    duration: int                                  # 秒
    source: str                                    # "字幕轨" 或 "必剪云端ASR"
    text: str
    subtitle_lan: str = ""                         # 字幕轨语言码，ai-zh = B站自动生成（ASR 产物）
    page: int = 1                                  # 多 P 视频实际取的分 P 序号
    segments: list[dict] = field(default_factory=list)  # [{start,end,text}]，字幕轨才有
    warnings: list[str] = field(default_factory=list)


# ── 基础 HTTP ────────────────────────────────────────────────

def _resolve_cookie_path(cookie_path: Optional[str] = None) -> Optional[Path]:
    """定位 cookies.txt：显式路径优先，其次按 _DEFAULT_COOKIE_PATHS 自动查找。

    三处（opener / fetch / cookie_status）共用这一个函数，避免各自的查找顺序
    悄悄跑偏——README 承诺了三条路径，代码就必须都认。
    """
    if cookie_path:
        cand = Path(cookie_path).expanduser()
        if cand.exists():
            return cand
    for p in _DEFAULT_COOKIE_PATHS:
        if p.exists():
            return p
    return None


def _opener(cookie_path: Optional[str] = None):
    """构造带 Cookie 的 opener。

    UP 主投稿、合集这类接口对未登录请求会直接返回「风控校验失败」，必须带登录态。
    """
    path = _resolve_cookie_path(cookie_path)
    if path is None:
        return urllib.request.build_opener()
    jar = _load_jar(path)
    if not len(list(jar)):
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


# ── Cookie 状态 ──────────────────────────────────────────────

#: 登录态探测结果的缓存（秒）。面板每 3 秒刷一次，不能每次都真去问 B站。
_LOGIN_TTL = 300.0
_login_cache: dict = {"ts": 0.0, "value": None, "path": None}
_login_lock = threading.Lock()


def _text_has_sessdata(text: str) -> bool:
    """文本里是否真有 SESSDATA（B站的登录凭证）。

    只认 Mozilla 格式的 7 字段行，避免把注释行或别的站点的 cookie 当成登录态。
    ``#HttpOnly_`` 是 Netscape 格式给 HttpOnly cookie 的前缀，**不是注释**
    （SESSDATA 由浏览器插件导出时通常就带这个前缀，当注释跳过会把有效 Cookie
    误判成「没有 SESSDATA」）。
    """
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[5] == "SESSDATA" and parts[6].strip():
            return True
    return False


def _has_sessdata(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _text_has_sessdata(text)


def cookie_status(cookie_path: Optional[str] = None,
                  verify: bool = False, fresh: bool = False) -> dict:
    """返回 Cookie 的可用性快照，供面板与 CLI 提示用。

    默认只做本地检查（不联网）。``verify=True`` 时额外请求一次 nav 接口，
    判断 SESSDATA 是否仍然有效——**这是判断 Cookie 过期的唯一可靠办法**，
    文件存在、字段齐全都说明不了问题。``fresh=True`` 强制绕过登录态缓存。

    返回字段：
        path / found / parsable / has_sessdata / age_days / logged_in / level / note
    level 取 ok / warn / error，面板直接按它上色。
    """
    path = _resolve_cookie_path(cookie_path)
    if path is None:
        return {
            "path": None, "found": False, "parsable": False,
            "has_sessdata": False, "age_days": None, "logged_in": None,
            "level": "error",
            "note": "没有找到 Cookie 文件。未登录时 UP 主投稿与合集接口必然返回"
                    "「风控校验失败」，只能抓单个 BV 号。",
        }

    age_days: Optional[float] = None
    try:
        age_days = round((time.time() - path.stat().st_mtime) / 86400.0, 1)
    except OSError:
        age_days = None

    try:
        parsable = bool(_parse_netscape(
            path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        parsable = False
    has_sessdata = _has_sessdata(path)
    logged_in = verify_login(path, force=fresh) if (verify and parsable) else None

    if not parsable:
        level, note = "error", "Cookie 文件存在但无法解析（格式不对或已损坏）"
    elif not has_sessdata:
        level, note = "error", "Cookie 文件里没有 SESSDATA，等同于未登录"
    elif logged_in is False:
        level, note = "error", "SESSDATA 已失效（nav 接口返回未登录），请重新导出 Cookie"
    elif logged_in is True:
        level, note = "ok", "Cookie 有效，已登录"
    elif age_days is not None and age_days > 30:
        level = "warn"
        note = "Cookie 已 %d 天未更新，可能已过期" % int(age_days)
    else:
        level, note = "ok", "已配置 Cookie"

    return {
        "path": str(path), "found": True, "parsable": parsable,
        "has_sessdata": has_sessdata, "age_days": age_days,
        "logged_in": logged_in, "level": level, "note": note,
    }


# ── Cookie 导入 ──────────────────────────────────────────────

#: 新写入 cookie 的标注有效期。读取时用了 ignore_expires，这里只是让文件看起来
#: 正常，并给 B站 一个合理的续期预期（SESSDATA 实际有效期约半年）。
_COOKIE_LIFETIME = 180 * 86400
_COOKIE_DOMAIN = ".bilibili.com"


def default_cookie_target() -> Path:
    """新 Cookie 写到哪：已有文件就更新它，否则用项目的标准路径。"""
    found = _resolve_cookie_path()
    return found if found is not None else _DEFAULT_COOKIE_PATHS[-1]


def _is_netscape(text: str) -> bool:
    """是 cookies.txt 全文，还是请求头里的一串键值。"""
    for line in text.splitlines():
        s = line.strip().lstrip("\ufeff")
        if s:
            return s.startswith("# Netscape HTTP Cookie File")
    return False


def _parse_cookie_header(text: str) -> list[tuple[str, str]]:
    """从 ``Cookie: a=1; b=2``（或去掉前缀的 ``a=1; b=2``）取出 (name, value)。

    只按**第一个** ``=`` 切：cookie 值里可能自带 ``=``（base64 填充），
    从右边切会把值截错，而错一个字符就等于没登录。
    """
    body = "; ".join(line.strip() for line in text.splitlines() if line.strip())
    body = re.sub(r"^cookie\s*:\s*", "", body, flags=re.I)
    pairs: list[tuple[str, str]] = []
    for chunk in body.split(";"):
        name, sep, value = chunk.strip().strip('"').partition("=")
        name, value = name.strip(), value.strip().strip('"')
        if sep and name and value:
            pairs.append((name, value))
    return pairs


def _make_cookie(name: str, value: str) -> http.cookiejar.Cookie:
    # 不标 HttpOnly：从请求头里看不出某条 cookie 是不是 HttpOnly，标错等于
    # 在文件里写假信息。我们不在浏览器里跑，这个标记对 urllib 也毫无影响。
    return http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=_COOKIE_DOMAIN, domain_specified=True, domain_initial_dot=True,
        path="/", path_specified=True, secure=True,
        expires=int(time.time()) + _COOKIE_LIFETIME, discard=False,
        comment=None, comment_url=None, rest={}, rfc2109=False)


_HTTPONLY = "#HttpOnly_"
_NS_FIELDS = 7          # domain, include_sub, path, secure, expires, name, value


def _netscape_cookie(domain: str, path: str, secure: bool,
                     expires: Optional[int], name: str, value: str,
                     httponly: bool) -> http.cookiejar.Cookie:
    """按 Netscape 行的字段造一条 Cookie。"""
    return http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=path, path_specified=path.startswith("/"),
        secure=secure, expires=expires, discard=expires is None,
        comment=None, comment_url=None,
        rest={"HttpOnly": None} if httponly else {}, rfc2109=False)


def _parse_netscape(text: str) -> "list[http.cookiejar.Cookie]":
    """自己解析 cookies.txt，不走 ``MozillaCookieJar.load``。

    非得自己解析，是因为标准库在这件事上**按版本行为不同**：Python 3.9 的
    ``_really_load`` 把所有 ``#`` 开头的行都当注释丢掉，``#HttpOnly_`` 开头的
    行连看都不看；3.10 起才剥掉这个前缀当正常行处理。而浏览器插件导出的
    cookies.txt 里，SESSDATA 几乎一定带 ``#HttpOnly_`` 前缀。

    结果是同一个文件：3.10+ 能登进去，3.9 读到 0 条，用户看到的是「没登录」
    或者「导入的内容里没解析出任何 cookie」——文件明明是好的。自己解析就没有
    这种版本差异，顺带也让导入校验不再依赖临时文件。

    坏行直接跳过（不抛错）：一份真实的导出文件里混进半行、多一个空行都很常见，
    为了一行脏数据把整份 Cookie 拒掉，用户只能去手工改文件。
    """
    cookies = []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        httponly = False
        if line.startswith(_HTTPONLY):
            line, httponly = line[len(_HTTPONLY):], True
        elif line.startswith("#HttpOnly "):        # 早期插件用空格而非下划线
            line, httponly = line[len("#HttpOnly "):], True
        if not line.strip() or line.startswith(("#", "$")):
            continue
        fields = line.split("\t")
        if len(fields) < _NS_FIELDS:
            fields = line.split()                  # 少数工具用空格分隔
            if len(fields) < _NS_FIELDS:
                continue
        if len(fields) > _NS_FIELDS:               # 值里自带 tab：并回去
            fields = fields[:_NS_FIELDS - 1] + ["\t".join(fields[_NS_FIELDS - 1:])]
        domain, _include_sub, path, secure, expires, name, value = fields
        if not domain or not name:
            continue
        try:
            stamp: Optional[int] = int(expires)
        except ValueError:
            stamp = None                           # 时间列坏了就当成会话 cookie
        if stamp == 0:
            stamp = None
        cookies.append(_netscape_cookie(
            domain=domain, path=path or "/", secure=secure.strip().upper() == "TRUE",
            expires=stamp, name=name, value=value, httponly=httponly))
    return cookies


def _commit_cookie_file(payload: str, target: Path) -> None:
    """内容先验一遍、再写临时文件、最后原子替换。

    直接覆盖原文件的坏处是：粘贴内容有问题时，会把用户原本还能用的 Cookie
    一起毁掉，连回退的机会都没有。校验放在写盘之前，坏内容连临时文件都不
    会留下。

    校验用 ``_parse_netscape`` 而不是 ``MozillaCookieJar.load``：后者在
    Python 3.9 上会把 ``#HttpOnly_`` 行当注释丢掉，导致一份完全合法的导出
    文件被判成「没解析出 cookie」。
    """
    if not _parse_netscape(payload):
        raise BiliError("写入的内容里没解析出任何 cookie")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".new")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)
    replace_with_retry(
        tmp, target,
        on_error=lambda exc: BiliError(
            "写入 %s 失败：文件被别的程序占着（杀软正在扫描、或编辑器/浏览器"
            "正在读它）。稍后重试一次，或先关掉占用它的程序。" % target))


def import_cookies(text: str, path: Optional[str] = None) -> dict:
    """把用户粘贴的内容写成 cookies.txt，返回导入报告。

    接受两种输入：

    1. cookies.txt 全文（首行 ``# Netscape HTTP Cookie File``）——整份替换；
    2. 开发者工具里 ``Cookie:`` 请求头的值，或纯 ``a=1; b=2``——**合并**。

    合并而不是覆盖，是因为只贴了 SESSDATA / bili_jct 这几条时，覆盖会顺手把
    ``buvid3`` 这类设备指纹抹掉；少了它反而更容易被风控盯上。

    没有 SESSDATA 直接拒绝写入：那是「未登录状态」的 Cookie，写进去只会让人
    以为配好了，然后对着 -352 干瞪眼。
    """
    target = Path(path).expanduser() if path else default_cookie_target()
    text = text.replace("\ufeff", "").strip()
    if not text:
        raise BiliError("粘贴的内容是空的")

    kind = "netscape" if _is_netscape(text) else "header"

    if kind == "netscape":
        body = "\n".join(line.rstrip() for line in text.splitlines()) + "\n"
        if not _text_has_sessdata(body):
            raise BiliError(
                "这份 cookies.txt 里没有 SESSDATA，等于未登录状态。"
                "请在浏览器里登录 B站 后重新导出。")
        _commit_cookie_file(body, target)
        total = len(list(_load_jar(target)))
        return {"path": str(target), "kind": kind, "changed": total,
                "total": total, "has_sessdata": True}

    pairs = _parse_cookie_header(text)
    if not pairs:
        raise BiliError(
            "没能从粘贴内容里认出 cookie。请复制开发者工具里 Cookie 请求头的"
            "整行值（形如 SESSDATA=xxx; bili_jct=yyy），或直接贴 cookies.txt 全文。")
    names = [name for name, _ in pairs]
    if "SESSDATA" not in names:
        raise BiliError(
            "粘贴的 Cookie 里没有 SESSDATA（当前只有 %s），说明浏览器此时未登录。"
            "请先在 B站 登录，再刷新页面后重新复制。" % "、".join(names[:6]))

    jar = _load_jar(target)
    changed = 0
    for name, value in pairs:
        olds = [c for c in jar if c.name == name]
        if olds and all(c.value == value for c in olds):
            continue
        for c in olds:                       # 同名只留一条，避免新旧并存
            jar.clear(c.domain, c.path, c.name)
        jar.set_cookie(_make_cookie(name, value))
        changed += 1

    if changed:
        _commit_cookie_file(_jar_to_text(jar), target)
    return {"path": str(target), "kind": kind, "changed": changed,
            "total": len(list(jar)), "has_sessdata": _has_sessdata(target)}


def _load_jar(path: Path) -> http.cookiejar.MozillaCookieJar:
    """读 cookies.txt。坏文件当空处理——反正马上会重写。

    用 ``_parse_netscape`` 而不是 ``jar.load()``：见那个函数的说明，
    Python 3.9 会把 ``#HttpOnly_`` 行整行丢掉，SESSDATA 往往就在那种行里。
    """
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return jar
    for cookie in _parse_netscape(text):
        jar.set_cookie(cookie)
    return jar


def _jar_to_text(jar: http.cookiejar.MozillaCookieJar) -> str:
    """把 jar 渲染成 Netscape 文本（不落盘，交给 _commit_cookie_file 校验）。"""
    lines = ["# Netscape HTTP Cookie File", ""]
    for c in jar:
        domain = ("#HttpOnly_" if c.has_nonstandard_attr("HttpOnly")
                  else "") + c.domain
        lines.append("\t".join([
            domain,
            "TRUE" if c.domain_specified else "FALSE",
            c.path or "/",
            "TRUE" if c.secure else "FALSE",
            str(int(c.expires or 0)),
            c.name,
            c.value,
        ]))
    return "\n".join(lines) + "\n"


def verify_login(cookie_path: Optional[str] = None,
                 ttl: float = _LOGIN_TTL,
                 force: bool = False) -> Optional[bool]:
    """请求 nav 接口确认登录态。结果缓存 ttl 秒。

    返回 True/False。**只有「明确无法判断」才返回 None**（网络或风控问题）——
    不确定不等于失效，不能因为探测失败就把用户的 Cookie 判死。

    注意 ``code == -101`` 是明确结论（未登录），不是「未知」：B站的 SESSDATA
    可能时间上没过期、服务端却已注销（改密码、主动退出、被踢下线）。只看文件
    的过期时间永远发现不了这种情况。

    缓存键里带上文件的 mtime：用户刚重新导出 Cookie 就该立刻生效，而不是
    继续拿旧结论糊他 5 分钟。``force`` 可以再跳过缓存强制探一次。
    """
    path = _resolve_cookie_path(cookie_path)
    key = str(path) if path else ""
    if path is not None:
        try:
            key = "%s:%d" % (key, int(path.stat().st_mtime))
        except OSError:
            pass
    now = time.monotonic()
    with _login_lock:
        if not force and _login_cache["path"] == key \
                and now - _login_cache["ts"] < ttl:
            return _login_cache["value"]
    value: Optional[bool] = None
    try:
        data = _http_json("https://api.bilibili.com/x/web-interface/nav",
                          cookie_path=cookie_path, timeout=15)
    except BiliError:
        value = None                     # 风控或网络问题：结论未知
    else:
        code = data.get("code")
        inner = data.get("data") or {}
        if code == -101:
            value = False                # 明确：未登录
        elif code in (0, None) and "isLogin" in inner:
            value = bool(inner.get("isLogin"))
    with _login_lock:
        _login_cache.update({"ts": now, "value": value, "path": key})
    return value


def explain_risk(exc: BiliRiskControl) -> BiliRiskControl:
    """把 -352 细分清楚：是「请求太快」还是「压根没登录」。

    这两件事在返回码上长得一模一样，但处理方式相反：前者该退避，后者该换
    Cookie（退避多久都没用）。区分办法是探一次 nav——结果有 5 分钟缓存，
    不会因为诊断本身把请求量打上去。

    返回 BiliLoginRequired 表示「是登录态问题」，其余原样返回。
    """
    if isinstance(exc, BiliLoginRequired):
        return exc
    if exc.code not in (-352, -412):
        return exc
    if verify_login() is False:
        return BiliLoginRequired(
            "接口返回 -352（风控校验失败），但 nav 显示当前未登录。"
            "UP 主投稿、合集、搜索在未登录时必然返回 -352，"
            "这是登录态问题，不是速率问题，退避无效",
            code=exc.code)
    return exc


def _reset_login_cache() -> None:
    """清空登录态缓存（Cookie 被更新后调用，测试也会用）。"""
    with _login_lock:
        _login_cache.update({"ts": 0.0, "value": None, "path": None})


def _http_json(url: str, cookie_path: Optional[str] = None,
               timeout: int = 60) -> dict:
    """GET 一个返回 JSON 的接口。

    所有 B站 API 请求都经过这里，所以风控的两道防线都放在这：

    1. **发之前**先过 ``_throttle``，保证同一个 host 上有最小间隔；
    2. **收之后**把 HTTP 412/429 与「返回 HTML 验证页」识别成风控，
       而不是当成「这条视频取不到」。
    """
    _throttle.wait(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Referer": "https://www.bilibili.com/"})
    try:
        with _opener(cookie_path).open(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        if e.code in (412, 429):
            raise BiliRiskControl(
                f"请求被 B站拦截（HTTP {e.code}）", code=e.code) from e
        if e.code in (401, 403):
            raise BiliLoginRequired(
                f"请求被拒绝（HTTP {e.code}），Cookie 可能已失效", code=e.code) from e
        raise BiliError(f"接口返回 HTTP {e.code}：{url.split('?')[0]}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BiliError(
            f"请求失败（{type(e).__name__}）：{url.split('?')[0]}") from e

    text = raw.decode("utf-8", "replace")
    if text.lstrip()[:1] == "<":
        # 正常接口不会返回 HTML。这里出现 HTML 基本只有一种情况：
        # 被导向了人机验证页——按风控处理，等一会再来。
        raise BiliRiskControl("接口返回了 HTML 而非 JSON（疑似人机验证页）")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise BiliError(f"接口返回内容不是合法 JSON：{text[:80]!r}") from e


def _bcut_http(url: str, method: str = "GET", form: Optional[dict] = None,
               json_body: Optional[dict] = None, timeout: int = 120) -> dict:
    headers = {"User-Agent": _UA}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _bcut_put(url: str, payload: bytes) -> Optional[str]:
    """上传分片。强制 https + 重试（http:80 端口会被连接重置）。"""
    url = url.replace("http://", "https://", 1)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=payload, method="PUT",
                                         headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=180) as resp:
                return resp.headers.get("Etag")
        except Exception as e:                      # noqa: BLE001
            if attempt == 3:
                raise RuntimeError(f"分片上传失败: {type(e).__name__}: {e}")
            time.sleep(2)
    return None


# ── 元数据 / 字幕轨 ──────────────────────────────────────────

def extract_bvid(text: str) -> Optional[str]:
    """从链接或纯 bvid 中提取 BV 号。"""
    m = _BVID_RE.search(text)
    return m.group(0) if m else None


def parse_only_targets(raw) -> Optional[list[tuple[str, Optional[int]]]]:
    """解析「只跑这几条」的清单，返回 ``[(bvid, 分P或None), ...]``。

    每一项写成 ``BV1xxxxxxxxx`` 表示这个视频的所有分 P，
    ``BV1xxxxxxxxx#3`` 表示只跑第 3 P。分 P 那个 ``#`` 是刻意选的：
    BV 号里不会出现它，链接里也不会——用 ``:`` 会和 ``up:`` / ``season:``
    那套前缀混淆。

    返回 None 表示没指定（走正常的 target 解析）。列表顺序保留、重复项合并，
    所以「只重跑失败的 3 条」不会因为同一个视频挂了两个分 P 就跑两遍。
    """
    if raw in (None, "", [], ()):
        return None
    if isinstance(raw, str):
        raw = [x for x in raw.replace(",", " ").split() if x]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("only 应该是数组，例如 [\"BV1xx\", \"BV1yy#2\"]")

    out: list[tuple[str, Optional[int]]] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        page: Optional[int] = None
        if "#" in text:
            text, _, tail = text.partition("#")
            tail = tail.strip()
            if not tail.isdigit():
                raise ValueError("分 P 写法应为 BV号#序号（例如 BV1xx#3）：%s"
                                 % item)
            page = int(tail)
            if page < 1:
                raise ValueError("分 P 序号从 1 开始：%s" % item)
        bv = extract_bvid(text)
        if not bv:
            raise ValueError("only 里有无法识别的 BV 号：%s" % item)
        if (bv, page) not in out:
            out.append((bv, page))
    return out or None


def group_only_pages(items: list[tuple[str, Optional[int]]]
                     ) -> dict[str, Optional[set]]:
    """把 ``parse_only_targets`` 的结果压成 ``{bvid: 要跑的分P集合}``。

    值为 None 表示这条要全部 P。同一个 bvid 在清单里既有「整条」又有
    具体分 P 时按「整条」算——那说明用户本来就想全跑。
    """
    grouped: dict[str, Optional[set]] = {}
    for bv, page in items:
        if bv not in grouped:
            grouped[bv] = set()
        if page is None:
            grouped[bv] = None
        elif grouped[bv] is not None:
            grouped[bv].add(page)          # type: ignore[union-attr]
    return grouped


def extract_mid(text: str) -> Optional[int]:
    """从 UP 主空间链接或纯数字中提取 mid。"""
    m = _MID_RE.search(text or "")
    if m:
        return int(m.group(1))
    s = (text or "").strip()
    return int(s) if s.isdigit() else None


def get_video_info(bvid: str, cookie_path: Optional[str] = None) -> dict:
    data = _http_json(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        cookie_path=cookie_path)
    _check_code(data, f"取视频信息 {bvid}")
    d = data.get("data") or {}
    if not d:
        raise BiliError(
            f"未取到视频信息（bvid 错误或视频已删除）：{data.get('message')}")
    return {
        "bvid": bvid,
        "aid": d.get("aid"),
        "cid": d.get("cid"),
        "title": d.get("title", ""),
        "owner": (d.get("owner") or {}).get("name", ""),
        "duration": d.get("duration", 0),
        "pages": d.get("pages") or [],
        "page_url": f"https://www.bilibili.com/video/{bvid}",
    }


def _pick_subtitle(subs: list[dict]) -> Optional[dict]:
    """优先 AI 中文字幕 → 任意中文 → 第一条。"""
    for s in subs:
        if s.get("lan") == "ai-zh":
            return s
    for s in subs:
        if "zh" in (s.get("lan") or "").lower():
            return s
    return subs[0] if subs else None


def fetch_subtitle(video: dict, cookie_path: Optional[str] = None) -> Optional[str]:
    """用 wbi 签名从 player/wbi/v2 取字幕轨，返回纯文本（无字幕返回 None）。"""
    return (_fetch_subtitle_detail(video, cookie_path) or {}).get("text")


def _fetch_subtitle_detail(video: dict,
                           cookie_path: Optional[str] = None) -> Optional[dict]:
    """取字幕轨，返回 {"text": str, "lan": str}；无字幕返回 None。

    lan 为 B站字幕语言码：ai-zh 表示自动生成（ASR 产物，天然带错字），
    zh-CN 等表示人工上传字幕（文本本身基本正确）。

    签名密钥走 ``_signed_query`` 的进程内缓存。原先这里每次都单独请求一次
    ``nav`` 接口取密钥——批量抓 50 条就是 50 个多余的请求，纯粹在给风控
    递理由。
    """
    query = _signed_query({"bvid": video["bvid"], "aid": video["aid"],
                           "cid": video["cid"]}, cookie_path)
    data = _http_json(f"https://api.bilibili.com/x/player/wbi/v2?{query}",
                      cookie_path=cookie_path)
    _check_code(data, "取字幕列表")

    subs = ((data.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    pick = _pick_subtitle(subs)
    if not pick or not pick.get("subtitle_url"):
        return None

    sub_url = pick["subtitle_url"]
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url
    body = (_http_json(sub_url, cookie_path=cookie_path) or {}).get("body") or []
    segments = [
        {"start": float(it.get("from") or 0),
         "end": float(it.get("to") or 0),
         "text": (it.get("content") or "").strip()}
        for it in body if (it.get("content") or "").strip()
    ]
    if not segments:
        return None
    return {"text": "\n".join(s["text"] for s in segments),
            "lan": pick.get("lan") or "",
            "segments": segments}


# ── 多 P 展开 / 批量列表 ─────────────────────────────────────

_wbi_cache: dict = {}


def _wbi_mixin_key(cookie_path: Optional[str] = None) -> str:
    """取 wbi 的 mixin_key。B站每天轮换，进程内缓存。"""
    key = cookie_path or ""
    if key in _wbi_cache:
        return _wbi_cache[key]
    nav = _http_json("https://api.bilibili.com/x/web-interface/nav",
                     cookie_path=cookie_path)
    wbi = ((nav.get("data") or {}).get("wbi_img") or {})
    img_key = Path(wbi.get("img_url") or "").stem
    sub_key = Path(wbi.get("sub_url") or "").stem
    if not img_key or not sub_key:
        raise BiliError("取不到 wbi 签名密钥（nav 接口异常）")
    raw = img_key + sub_key
    if len(raw) < 64:
        # 重排表最大下标 63，长度不够会直接 IndexError——那是个看不懂的崩溃，
        # 不如明确告诉用户接口变了。
        raise BiliError(
            "wbi 密钥长度异常（%d，应为 64），B站接口可能已变更" % len(raw))
    mixin = "".join(raw[i] for i in _WBI_MIXIN_TAB)[:32]
    _wbi_cache[key] = mixin
    return mixin


def _signed_query(params: dict, cookie_path: Optional[str] = None) -> str:
    """返回已签名的 query string（含 wts / w_rid）。"""
    mixin = _wbi_mixin_key(cookie_path)
    clean = {k: "".join(ch for ch in str(v) if ch not in "!'()*")
             for k, v in params.items()}
    clean["wts"] = int(time.time())
    q = urllib.parse.urlencode(sorted(clean.items()))
    w_rid = hashlib.md5((q + mixin).encode()).hexdigest()
    return f"{q}&w_rid={w_rid}"


def expand_pages(video: dict) -> list[dict]:
    """展开多 P；无分 P 时返回单条。每项含 index/cid/title/duration。"""
    pages = video.get("pages") or []
    if not pages:
        return [{"index": 1, "cid": video.get("cid"),
                 "title": video.get("title") or "",
                 "duration": video.get("duration") or 0}]
    return [{"index": i, "cid": p.get("cid"),
             "title": p.get("part") or video.get("title") or "",
             "duration": p.get("duration") or 0}
            for i, p in enumerate(pages, 1)]


def list_up_videos(mid: int, cookie_path: Optional[str] = None,
                   max_count: int = 0) -> list[dict]:
    """遍历 UP 主投稿（按发布时间倒序），返回 [{bvid,title,duration}]。

    duration 是接口给的 "mm:ss" 字符串，保留原始形态。
    """
    out: list[dict] = []
    pn = 1
    while True:
        q = _signed_query({"mid": mid, "ps": 30, "pn": pn,
                           "order": "pubdate", "platform": "web"}, cookie_path)
        data = _http_json(
            f"https://api.bilibili.com/x/space/wbi/arc/search?{q}",
            cookie_path=cookie_path)
        if data.get("code") != 0:
            _check_code(data, "取 UP 主投稿")
        d = data.get("data") or {}
        vlist = ((d.get("list") or {}).get("vlist")) or []
        if not vlist:
            break
        for v in vlist:
            if not v.get("bvid"):
                continue
            out.append({"bvid": v["bvid"],
                        "title": v.get("title") or "",
                        "duration": v.get("length") or ""})
            if max_count and len(out) >= max_count:
                return out
        total = (d.get("page") or {}).get("count") or 0
        if len(out) >= total:
            break
        pn += 1
    return out


def list_season(mid: int, season_id: int, cookie_path: Optional[str] = None,
                max_count: int = 0) -> list[dict]:
    """遍历合集/系列，返回 [{bvid,title,duration}]（该接口不需要 wbi 签名）。"""
    out: list[dict] = []
    pn = 1
    while True:
        url = ("https://api.bilibili.com/x/polymer/web-space/"
               "seasons_archives_list"
               f"?mid={mid}&season_id={season_id}&page_num={pn}&page_size=30")
        data = _http_json(url, cookie_path=cookie_path)
        if data.get("code") != 0:
            _check_code(data, "取合集")
        archives = (data.get("data") or {}).get("archives") or []
        if not archives:
            break
        for a in archives:
            if not a.get("bvid"):
                continue
            out.append({"bvid": a["bvid"],
                        "title": a.get("title") or "",
                        "duration": a.get("duration") or 0})
            if max_count and len(out) >= max_count:
                return out
        if len(archives) < 30:
            break
        pn += 1
    return out


def _strip_em(text: str) -> str:
    """去掉搜索结果标题里的高亮标签 <em class="keyword">…</em>。"""
    return re.sub(r"</?em[^>]*>", "", text or "")


def search_videos(keyword: str, cookie_path: Optional[str] = None,
                  pages: int = 1, page_size: int = 42) -> list[dict]:
    """按关键词搜视频，返回 [{bvid,title,duration,author}]（按相关度排序）。

    duration 是接口给的 "mm:ss" 字符串，保留原始形态。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for pn in range(1, max(1, pages) + 1):
        q = _signed_query({"search_type": "video", "keyword": keyword,
                           "page": pn, "page_size": page_size},
                          cookie_path)
        data = _http_json(
            "https://api.bilibili.com/x/web-interface/wbi/search/type?" + q,
            cookie_path=cookie_path)
        if data.get("code") != 0:
            _check_code(data, "搜索")
        result = ((data.get("data") or {}).get("result")) or []
        if not result:
            break
        for v in result:
            if v.get("type") != "video" or not v.get("bvid"):
                continue
            if v["bvid"] in seen:
                continue
            seen.add(v["bvid"])
            out.append({"bvid": v["bvid"],
                        "title": _strip_em(v.get("title") or ""),
                        "duration": v.get("duration") or "",
                        "author": v.get("author") or ""})
    return out


# ── 必剪云端 ASR ─────────────────────────────────────────────

def _download_audio(video: dict, tmpdir: str,
                    cookie_path: Optional[str] = None) -> Optional[str]:
    """用 yt-dlp 拉 m4a 音频流（无需 ffmpeg）。"""
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        raise BiliError("必剪 ASR 需要 yt-dlp 下载音频，请先安装：pip install yt-dlp")
    args = [sys.executable, "-m", "yt_dlp",
            "-f", "ba[ext=m4a]/ba", "--no-warnings",
            "-o", f"{tmpdir}/audio.%(ext)s"]
    if cookie_path and Path(cookie_path).exists():
        args += ["--cookies", str(cookie_path)]
    args.append(video["page_url"])
    proc = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise BiliError(f"yt-dlp 下载音频失败：{detail[:200]}")
    for f in sorted(os.listdir(tmpdir)):
        if f.startswith("audio.") and not f.endswith(".part"):
            return os.path.join(tmpdir, f)
    return None


def transcribe_bcut(video: dict, cookie_path: Optional[str] = None,
                    max_wait: int = 900) -> Optional[str]:
    """必剪云端转写，返回带换行的文本，失败返回 None。"""
    tmpdir = tempfile.mkdtemp(prefix="asr_polish_")
    try:
        audio_path = _download_audio(video, tmpdir, cookie_path=cookie_path)
        if not audio_path:
            return None
        sound = Path(audio_path).read_bytes()
        fmt = Path(audio_path).suffix.lstrip(".") or "m4a"
        name = os.path.basename(audio_path)

        r = _bcut_http(_BCUT_REQ_UPLOAD, method="POST", form={
            "type": 2, "name": name, "size": len(sound),
            "resource_file_type": fmt, "model_id": _BCUT_MODEL_ID})
        if r.get("code"):
            return None
        d = r["data"]
        etags = []
        per = d["per_size"]
        for i, url in enumerate(d["upload_urls"]):
            etags.append(_bcut_put(url, sound[i * per:(i + 1) * per]) or "")

        r = _bcut_http(_BCUT_COMMIT_UPLOAD, method="POST", form={
            "in_boss_key": d["in_boss_key"], "resource_id": d["resource_id"],
            "etags": ",".join(etags), "upload_id": d["upload_id"], "model_id": _BCUT_MODEL_ID})
        if r.get("code"):
            return None
        download_url = r["data"]["download_url"]

        task_id = None
        for attempt in range(3):
            r = _bcut_http(_BCUT_CREATE_TASK, method="POST",
                           json_body={"resource": download_url, "model_id": str(_BCUT_MODEL_ID)})
            if r.get("code") == 0:
                task_id = r["data"]["task_id"]
                break
            if r.get("code") == -504:
                time.sleep(2 * (attempt + 1))
                continue
            return None
        if not task_id:
            return None

        deadline = time.time() + max_wait
        while time.time() < deadline:
            r = _bcut_http(_BCUT_QUERY_RESULT + "?" + urllib.parse.urlencode(
                {"model_id": _BCUT_MODEL_ID, "task_id": task_id}))
            if r.get("code"):
                return None
            data = r["data"]
            state = data.get("state")
            if state == 4:                          # COMPLETE
                result = json.loads(data.get("result") or "{}")
                lines = [u.get("transcript", "").strip()
                         for u in (result.get("utterances") or [])
                         if (u.get("transcript") or "").strip()]
                return "\n".join(lines) if lines else None
            if state == 3:                          # ERROR
                return None
            time.sleep(1)
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── 主入口 ───────────────────────────────────────────────────

def fetch(url: str, cookie_path: Optional[str] = None,
          prefer_subtitle: bool = True, page: int = 1,
          allow_asr: bool = True) -> FetchResult:
    """从 B站链接获取视频文本。字幕轨优先，无字幕走必剪云端 ASR。

    page 指定多 P 视频的分 P 序号（从 1 开始），默认第 1 P。
    allow_asr=False 时，无字幕轨直接返回空文本结果，不触发音频上传
    （批量抓字幕场景应当关掉，避免为无字幕视频白传音频）。
    """
    bvid = extract_bvid(url)
    if not bvid:
        raise BiliError(f"未能从输入识别 bvid：{url}")

    auto_cookie = False
    if not cookie_path:
        found = _resolve_cookie_path()
        if found is not None:
            cookie_path = str(found)
            auto_cookie = True

    video = get_video_info(bvid, cookie_path=cookie_path)
    result = FetchResult(
        bvid=bvid, title=video["title"], owner=video["owner"],
        duration=video["duration"], source="", text="")
    if auto_cookie:
        result.warnings.append("已自动加载本机 Cookie（用于获取更完整的字幕）")

    pages = video["pages"]
    if len(pages) > 1:
        if 1 <= page <= len(pages):
            chosen = pages[page - 1]
            video = dict(video, cid=chosen.get("cid"),
                         duration=chosen.get("duration", 0))
            result.duration = video["duration"]
            result.page = page
            result.warnings.append(
                f"多 P 视频（共 {len(pages)} P），已取第 {page} P"
                f"「{chosen.get('part', '')}」；用 --page 切换")
        else:
            result.warnings.append(
                f"多 P 视频（共 {len(pages)} P），page={page} 越界，回退到第 1 P")

    if prefer_subtitle:
        detail = _fetch_subtitle_detail(video, cookie_path=cookie_path)
        if detail and detail["text"]:
            result.source = "字幕轨"
            result.subtitle_lan = detail["lan"]
            result.text = detail["text"]
            result.segments = detail.get("segments") or []
            return result

    if not allow_asr:
        result.source = "无字幕"
        result.warnings.append("该视频没有可用字幕轨（未启用必剪 ASR 兜底）")
        # B站的 AI 字幕（lan=ai-zh）需要登录态才给。未登录时「取不到字幕」
        # 和「这视频本来就没字幕」长得一模一样，必须说清楚，否则用户会以为
        # 是这个视频的问题。
        if verify_login(cookie_path) is False:
            result.warnings.append(
                "当前未登录：B站的 AI 字幕需要登录态才能取到，"
                "所以上面的「无字幕」可能并不准确——更新 Cookie 后再试一次")
        return result

    result.warnings.append("无字幕轨，音频将上传至 B站必剪服务做云端转写（公开视频音轨，风险低）")
    text = transcribe_bcut(video, cookie_path=cookie_path)
    if not text:
        raise BiliError("字幕轨与必剪 ASR 均未取到文本（可能音频下载失败或视频受限）")
    result.source = "必剪云端ASR"
    result.text = text
    return result
