# -*- coding: utf-8 -*-
"""单篇总结：把一篇文字稿发给「OpenAI 兼容」的模型服务，要回一份结构化摘要。

为什么单独一个文件
------------------
`app.py` 只负责路由，`reader.py` 只负责页面。调模型这件事有自己的三件麻烦事：
拼端点、写提示词、把提供方的错误翻译成人话。塞进路由里就会变成 handler 中间
一段两百行的杂物，且无法单测——而这三件全是纯逻辑，正好都能测。

为什么只调标准库
----------------
`urllib.request` 足够发一次 POST。换 requests 只是少写几行，代价是给「核心零
依赖」破一个口子，而这一层的调用频率是「人点一次按钮一次」，根本不需要连接池。

关于 key
--------
key 从请求体里来、拼进 Authorization 头、用完即弃：**不落盘、不进日志、不进
异常消息**。本文件里没有任何 `print`，错误消息只带提供方返回的原因和 HTTP 码。
这不是洁癖——本地服务的日志会被人粘到 issue 里。

关于「时间戳必须真实」
----------------------
摘要的每一条要点要能点回原文。模型只有在自己看见的时间戳里挑，才有可能挑对。
所以发给它的正文带行首 `[mm:ss]`（见 `reader.py` 的 buildSummarySource），提示词
里明说「原样抄行首那个时间，不许自己算秒数、不许写原文里没有的时间」。前端
再拿该篇真实的 `start` 集合去核一遍——模型编出来的会被标成「无出处」，不给锚点。
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request

#: 模型偶尔会慢。总结一篇两万字的中文，实测 30～60 秒；给到两分钟。
DEFAULT_TIMEOUT = 120.0

#: 单篇正文上限。两万字约等于两小时视频；二十万字已经远超模型的上下文窗口，
#: 发过去只会换回一个 400。这里先拦住，并说清是多少字——比让提供方报
#: 「context length exceeded」好懂得多。
MAX_TEXT = 200_000

#: 提示词。放在这里当常量，不从配置文件读、也不做成用户可编辑：这一层的输出
#: 结构是被前端解析的，格式一变整块就崩，不该留一个「随便改」的口子。
#:
#: 几个刻意的约束：
#: - **只要 JSON，不要围栏**。很多兼容服务不支持 response_format，只能靠提示词；
#:   真带了 ```json 围栏也在 parse_reply 里兜住。
#: - **`t` 是原样抄的字符串，不是秒数**。让模型把 01:23 换算成 83 秒，它会算错；
#:   让它抄，它基本不会错。换算的事交给 parse_stamp。
#: - **要点不许整段抄原文**。抄出来的摘要读起来像摘录拼接，而且字数会失控。
#: - **没有时间戳时 t 必须是 null**。必剪 ASR 的篇本来就没有逐句时间，模型要是
#:   硬编一个，前端核对时会判无出处——但先在这里断了它的念头更省事。
PROMPT = """你是学习助理。下面是一篇视频文字稿，每行开头的 [分:秒] 是这一行对应的时间。

请整理成结构化摘要，**只输出一个 JSON 对象**，不要解释、不要 markdown 围栏：

{
  "tldr": "一句话说清这篇讲了什么",
  "points": [{"text": "要点", "t": "01:23"}],
  "concepts": [{"term": "术语", "def": "一句话解释"}]
}

要求：
1. tldr 一句话，40 字以内，说结论不说过程。
2. points 3 到 8 条，按原文顺序排列，每条 30 字以内，用自己的话概括，不要整段抄原文。
3. 每个 point 的 t 必须**原样抄**它所依据的那一行行首的时间，写成 "01:23" 这样的
   字符串（不带方括号）。不许自己换算成秒数，不许写原文里没有的时间。
4. concepts 0 到 6 个，只挑这一篇里真的出现过、且读者可能不懂的术语。
5. 如果文字稿的行首没有时间（整篇都这样），所有 point 的 t 一律写 null。"""


class SummaryError(Exception):
    """一次总结失败的完整说明。

    `kind` 给前端做分支（比如 key 错了可以就地弹设置面板），`message` 直接给
    人看——**必须带上提供方返回的原因**，不能吞成一句「总结失败」。
    """

    def __init__(self, kind: str, message: str, status: int = 502) -> None:
        self.kind = kind
        self.message = message
        self.status = status
        super().__init__(message)


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #

def base_url(raw: str) -> str:
    """把用户填的地址补成 ``…/chat/completions``。

    只认 OpenAI 兼容这一种形状，所以规则就三条：不写协议按 https 算（人贴地址
    时常常只贴域名），带不带尾部斜杠都行，填到 ``/v1`` 为止或者干脆贴完整端点
    都行。不去猜 ``/v1`` 该不该加——猜错了会把请求发到一个不存在的路径上，
    而报错是 404，看起来像「地址填错了」，用户改半天也改不对。
    """
    u = (raw or "").strip()
    if not u:
        raise SummaryError(
            "bad_base",
            "设置里没填模型服务地址。填到 /v1 为止即可，"
            "例如 api.deepseek.com/v1", 400)
    if not re.match(r"^https?://", u, re.I):
        # 只贴了域名：按 https 补。真要 http（本机 Ollama 之类）就得自己写明，
        # 这条不去猜——猜错会悄悄把请求发到 80 端口，报错是「连不上」。
        u = "https://" + u
    u = u.rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    return u + "/chat/completions"


# --------------------------------------------------------------------------- #
# 提示词与请求体
# --------------------------------------------------------------------------- #

def build_request(base: str, model: str, key: str, text: str) -> urllib.request.Request:
    """拼一次 chat/completions 请求。

    不传 `response_format`：OpenAI 有，很多兼容服务没有，带上会被直接拒绝
    （400 unknown parameter）。结构靠提示词约束 + parse_reply 兜底，兼容性优先。
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return urllib.request.Request(
        base_url(base), data=body, method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + key,
            "User-Agent": "asr-polish",
        })


# --------------------------------------------------------------------------- #
# 解析模型输出
# --------------------------------------------------------------------------- #

def _strip_fence(raw: str) -> str:
    """去掉 ```json … ``` 围栏。提示词已经说了不要，但模型经常还是加。"""
    s = (raw or "").strip()
    m = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", s, re.S)
    return m.group(1).strip() if m else s


def _first_json_object(s: str) -> str:
    """从一堆话里挖出第一个完整的 JSON 对象。

    模型常见的跑偏：前面写一句「好的，以下是摘要：」，后面跟 JSON。直接
    json.loads 会失败，而失败信息是「Expecting value」，对用户毫无意义。
    按大括号配对找，比正则可靠（正文里会有花括号）。
    """
    start = s.find("{")
    if start < 0:
        return ""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return ""


def parse_stamp(raw) -> int | None:
    """``"01:23"`` / ``"1:02:05"`` / ``"83"`` → 秒。认不出返回 None。

    模型有时会把时间写成秒数（83）而不是 mm:ss。两种都收，但**只做单位换算，
    不做任何补全**——`"3"` 会被当成 3 秒，不是 3 分钟。
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw) if raw >= 0 else None
    s = str(raw).strip().strip("[]").strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return int(float(s))
    m = re.fullmatch(r"(?:(\d+):)?(\d{1,3}):(\d{1,2})", s)
    if m:
        h = int(m.group(1) or 0)
        return h * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    m = re.fullmatch(r"(\d{1,3}):(\d{1,2})", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


def _text_of(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def normalize(obj: dict) -> dict:
    """把模型吐出来的对象收成前端要的那三个键。

    容错只做「形状」这一层：字段名认几个常见别名、空条目丢掉、类型不对的
    置空。**不补内容**——tldr 空就是空，不拿第一条要点顶上去，那等于替模型
    编了一句话。
    """
    if not isinstance(obj, dict):
        raise SummaryError("parse", "模型返回的不是一个 JSON 对象。", 502)

    tldr = _text_of(obj.get("tldr") or obj.get("summary") or obj.get("overview"))

    raw_points = obj.get("points")
    if not isinstance(raw_points, list):
        # 有的模型会写成 topics / key_points
        for alt in ("key_points", "topics", "highlights"):
            if isinstance(obj.get(alt), list):
                raw_points = obj[alt]
                break
        else:
            raw_points = []

    points = []
    for p in raw_points:
        if isinstance(p, str):
            text, stamp = p.strip(), None
        elif isinstance(p, dict):
            text = _text_of(p.get("text") or p.get("point") or p.get("内容"))
            stamp = p.get("t", p.get("time", p.get("stamp")))
        else:
            continue
        if not text:
            continue
        points.append({"text": text, "t": parse_stamp(stamp)})

    raw_concepts = obj.get("concepts") if isinstance(obj.get("concepts"), list) else []
    concepts = []
    for c in raw_concepts:
        if isinstance(c, dict):
            term = _text_of(c.get("term") or c.get("name") or c.get("术语"))
            define = _text_of(c.get("def") or c.get("definition")
                              or c.get("desc") or c.get("解释"))
        elif isinstance(c, str) and "：" in c:
            term, _, define = c.partition("：")
            term, define = term.strip(), define.strip()
        else:
            continue
        if term:
            concepts.append({"term": term, "def": define})

    if not tldr and not points:
        raise SummaryError(
            "parse", "模型返回的 JSON 里既没有一句话总览、也没有要点，"
                     "多半是它没按格式来。点「重新生成」再试一次。", 502)

    return {"tldr": tldr, "points": points, "concepts": concepts}


def parse_reply(raw: str) -> dict:
    """模型响应体（chat/completions 的完整 JSON）→ 结构化摘要。"""
    try:
        env = json.loads(raw)
    except (ValueError, TypeError):
        raise SummaryError(
            "parse", "模型服务返回的不是 JSON：%s" % (raw or "")[:200], 502) from None

    if isinstance(env, dict) and env.get("error"):
        detail = _provider_message(raw)
        raise SummaryError("model", "模型服务返回错误：%s" % detail, 502)

    try:
        content = env["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise SummaryError(
            "parse", "模型服务的响应里没有 choices[0].message.content：%s"
                     % (raw or "")[:200], 502) from None

    if isinstance(content, list):
        # 少数服务把 content 写成 [{type:text, text:...}] 的分段
        content = "".join(seg.get("text", "") for seg in content
                          if isinstance(seg, dict))

    body = _strip_fence(content if isinstance(content, str) else "")
    obj = _first_json_object(body)
    if not obj:
        raise SummaryError(
            "parse", "模型没有按格式返回 JSON，它说的是：" + body[:200], 502)
    try:
        data = json.loads(obj)
    except ValueError:
        raise SummaryError(
            "parse", "模型返回的 JSON 解析不了：%s" % obj[:200], 502) from None
    return normalize(data)


# --------------------------------------------------------------------------- #
# 错误翻译
# --------------------------------------------------------------------------- #

def _provider_message(raw: str) -> str:
    """从提供方的错误体里挖出「人话」。

    OpenAI 形状：``{"error": {"message": "Incorrect API key provided"}}``。
    挖不到就把原文截一段——**原文比「未知错误」有用**，用户能拿它去搜。
    """
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return (raw or "").strip()[:300] or "（提供方没有返回原因）"
    if isinstance(d, dict):
        err = d.get("error")
        if isinstance(err, dict):
            for k in ("message", "msg", "detail", "type"):
                v = err.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()[:300]
        if isinstance(err, str) and err.strip():
            return err.strip()[:300]
        for k in ("message", "msg", "detail"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:300]
    return (raw or "").strip()[:300] or "（提供方没有返回原因）"


def http_error(exc: urllib.error.HTTPError) -> SummaryError:
    """HTTP 错误 → 分得清是哪一种的 SummaryError。

    分类是为了让前端能给不同的反应：key 错可以直接弹设置面板，限流可以提示
    「等一会儿再点」，其余的就是「把提供方的话原样给你看」。
    """
    code = exc.code
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:                      # 响应体读不出来也不该让整件事崩
        raw = ""
    detail = _provider_message(raw)
    if code in (401, 403):
        return SummaryError(
            "auth", "模型服务拒绝了这把 key（HTTP %d）：%s" % (code, detail), 401)
    if code == 429:
        return SummaryError(
            "rate", "模型服务说请求太频繁、或额度用完了（HTTP 429）：%s" % detail, 429)
    if code == 404:
        return SummaryError(
            "bad_base",
            "模型服务说这个地址不存在（HTTP 404）：%s。"
            "检查一下设置里的地址是不是填到了 /v1 这一层，模型名对不对。" % detail,
            400)
    if code >= 500:
        return SummaryError(
            "model", "模型服务自己出错了（HTTP %d）：%s" % (code, detail), 502)
    return SummaryError(
        "model", "模型服务返回 HTTP %d：%s" % (code, detail), 502)


def transport_error(exc: Exception) -> SummaryError:
    """连不上 / 超时。分开报，因为处理方式不一样。"""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return SummaryError(
            "timeout",
            "等了 %d 秒还没返回。模型服务可能太慢，或这一篇太长。"
            "可以稍后再点一次「重新生成」。" % int(DEFAULT_TIMEOUT), 504)
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
        return SummaryError(
            "timeout", "等了 %d 秒还没返回，连接超时了。" % int(DEFAULT_TIMEOUT), 504)
    text = str(reason)
    if "CERTIFICATE" in text.upper():
        return SummaryError(
            "connect",
            "连上了但证书校验没通过：%s。公司代理或自签证书会这样；"
            "确认地址是可信的再处理。" % text[:200], 502)
    return SummaryError(
        "connect",
        "连不上模型服务：%s。检查设置里的地址、以及这台机器能不能访问它。"
        % text[:200], 502)


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def call(cfg: dict, text: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """发一次总结请求。成功返回 ``{"summary": …, "model": …}``。

    `cfg` 就是前端设置面板里那三样：``{"base", "model", "key"}``。
    """
    cfg = cfg or {}
    base = (cfg.get("base") or "").strip()
    model = (cfg.get("model") or "").strip()
    key = (cfg.get("key") or "").strip()

    if not key:
        raise SummaryError(
            "no_key",
            "设置里没填 API key。点顶栏的「模型」填一下——全文会发给这个服务，"
            "费用按它的规则算，所以用你自己的 key。", 400)
    if not model:
        raise SummaryError("bad_base", "设置里没填模型名，例如 deepseek-chat。", 400)

    body = (text or "").strip()
    if not body:
        raise SummaryError("empty", "这一篇没有正文，没什么可总结的。", 400)
    if len(body) > MAX_TEXT:
        raise SummaryError(
            "empty",
            "这一篇有 %d 字，超过单次 %.0f 万字的处理上限。"
            "先分段读，或者换一个上下文更长的模型。"
            % (len(body), MAX_TEXT / 10000.0), 413)

    req = build_request(base, model, key, body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise http_error(e) from None
    except urllib.error.URLError as e:
        raise transport_error(e) from None
    except (socket.timeout, TimeoutError) as e:
        raise transport_error(e) from None
    except OSError as e:
        raise transport_error(e) from None

    out = parse_reply(raw)
    return {"summary": out, "model": model}
