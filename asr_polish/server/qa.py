# -*- coding: utf-8 -*-
"""单篇问答：就眼前这一篇提问，回答必须带出处。

为什么单独一个文件
------------------
和 `summary.py` 同一个理由，但它自己又是另一件事：总结是「读一遍，交一份摘要」，
问答是「带着上文，就一个问题回答」。两边的差别全在**提示词和会话怎么拼**上，
所以只共用 `summary.complete`（校验配置、拼端点、翻译错误），不共用提示词。

为什么服务端不读 transcript
---------------------------
正文由前端用 `buildPlainText` 拼好发过来，和第 1 层一致。后端一旦自己去遍历
segments，就会长出第二套「全文是怎么拼的」规则，两边慢慢分叉——某天导出的 md
和模型看到的正文对不上，而这种错没人会去查。

关于「回答必须带出处」
----------------------
模型只有在自己看见的时间戳里挑，才有可能挑对。所以正文带行首 `[mm:ss]`
（前端 buildSummarySource 干的活），提示词里明说「原样抄行首那个时间，
写成方括号」，前端再拿该篇真实的段首去核——对不上的标「无出处」，不给锚点。
这里能做的只有两件：把正文给全、把规矩说死。剩下的靠前端那一步核对。

关于对话历史
------------
历史是**前端存、前端发**，服务端不留。每一轮都要把整篇正文重发一次（接口无
状态），所以历史越长越贵；只带最近 `MAX_ROUNDS` 轮，更早的直接丢掉。服务端
自己再裁一次：前端传多少是它的事，账单和上下文窗口是这里的事。
"""
from __future__ import annotations

from .summary import (BODY_HEAD, BODY_TAIL, DEFAULT_TIMEOUT, SummaryError,
                      complete, require_len)

#: 最多带几轮历史。一轮 = 一问一答。六轮之后「刚才聊到哪」基本靠正文就能
#: 重新接上，再往前带只是把账单拉长。
MAX_ROUNDS = 6

#: 提示词。和第 1 层一样做成常量、不给用户改：它的输出形状被前端解析
#: （方括号时间戳要能被认出来），格式一变整块功能就废。
#:
#: 三条要求对应三件真实发生过的事：
#: 1. **只依据正文**——不然它会用训练数据里的常识把这篇没说的补上，
#:    读者以为视频里讲过。
#: 2. **每条事实标时间**——没有出处的话没法核对，也没法点回原文；
#:    要求「原样抄行首」而不是换算成秒，是因为换算它会算错。
#: 3. **没有就说没有**——这一条最容易被模型绕过，所以给了固定说法
#:    「原文中没有提到」，比「请勿编造」这种抽象要求管得住。
ASK_PROMPT = """你是学习助理。下面是一篇视频的文字稿，每行开头的 [分:秒] 是这一行对应的
时间。读者就这一篇向你提问。

回答规则，三条，都要遵守：

1. 只能依据这段文字稿回答。文字稿里没有的内容，就写「原文中没有提到」，
   不要用你自己的知识补充，也不要说「根据我的理解」。
2. 每一句事实性的内容后面，都要紧跟一个原文里真实出现过的时间，写成
   [分:秒] 的形式：方括号里原样抄那一行行首的时间，不要换算成秒数，
   不要写原文里没有的时间。一句里如果有几处依据，就把几个时间都写上。
3. 只回答被问到的问题。不要顺便总结全篇、不要列大纲、不要反问。

用中文回答，长度以说清问题为准，不要凑字数。"""

#: 这一篇没有逐句时间（必剪 ASR / 纯文本导出）时追加的一句。
#:
#: 不追加的话，模型看见「每行开头有 [分:秒]」却发现没有，很可能自己编一个
#: 出来——它编出来的会被前端判成无出处，但用户看到满屏「无出处」只会以为
#: 功能坏了。先在这里把路堵死。
NO_TIME_RULE = """

注意：这一篇的文字稿没有逐句时间戳，行首没有 [分:秒]。这种情况下不要写任何
时间，也不要编造时间。"""

#: 把正文认下来的那一句。没有一个「收到」回合，模型有时会把正文当成问题的一部分。
ACK = "好的，我只依据这段正文回答，并在每条事实后面标上原文里的 [分:秒]。"


def trim_history(history) -> list:
    """只留最近几轮，并且把脏数据丢掉。

    前端发来的东西一律当作「不可信」处理：可能是字符串、可能是缺字段的字典、
    可能是别人拿着 curl 打的。**不因为一条脏数据丢掉整个会话**，逐条收；
    真的一条都不剩，也就是「新对话」，本来就该这样。
    """
    out = []
    if not isinstance(history, list):
        # 前端传 1、传 "abc"、传对象：都不是「有几轮历史」，是没历史。
        # 这里不抛异常——问题是主线，历史只是附带的上下文。
        return out
    for item in history:
        if not isinstance(item, dict):
            continue
        q = item.get("q")
        a = item.get("a")
        if not isinstance(q, str) or not isinstance(a, str):
            continue
        q, a = q.strip(), a.strip()
        if not q or not a:
            continue
        out.append({"q": q, "a": a})
    return out[-MAX_ROUNDS:]


def build_messages(text: str, question: str, history=None,
                   has_time: bool = True) -> list:
    """拼这一次要发出去的会话。

    正文只出现一次（作为最早的那条 user 消息），后面才是历史和新问题——
    历史里的每一轮都不再重复正文，否则一次请求里正文会出现六七遍。
    """
    system = ASK_PROMPT + ("" if has_time else NO_TIME_RULE)
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": BODY_HEAD + (text or "") + BODY_TAIL},
        # 一条确认回合：把「正文读过了，接下来是问答」这件事在会话里钉住。
        # 少了它，有的模型会把正文当成「用户说的话」，然后接着往下续写。
        {"role": "assistant", "content": ACK},
    ]
    for r in trim_history(history):
        msgs.append({"role": "user", "content": r["q"]})
        msgs.append({"role": "assistant", "content": r["a"]})
    msgs.append({"role": "user", "content": (question or "").strip()})
    return msgs


def call(cfg: dict, text: str, question: str, history=None,
         has_time: bool = True, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """问一次。成功返回 ``{"answer": 模型的原话, "model": 模型名}``。

    `cfg` 还是那三样：``{"base", "model", "key"}``。key 用完即弃，
    不落盘、不进日志、不进任何一条错误消息。
    """
    q = (question or "").strip()
    if not q:
        raise SummaryError("empty", "问题还没写。就这一篇问点什么，再点提问。", 400)

    body = require_len(text)
    if not body:
        raise SummaryError(
            "empty", "这一篇没有正文，问不了。换一篇有字幕的再试。", 400)

    msgs = build_messages(body, q, history, has_time)
    # 这里**不开** json_mode：要的是人话，不是对象。开了模型会把回答塞进
    # {"answer": "..."}，正文里的引号一旦没转义，整段话就变成解析失败——
    # 而这一层本来就没有解析步骤，答错了看得见，加约束只添风险。
    out = complete(cfg, msgs, timeout=timeout)

    answer = (out["content"] or "").strip()
    if not answer:
        # 空回答不是「模型没话说」，是这一家服务出问题了：多半命中了审核，
        # 或者开了某种「只回结构化输出」的开关。别把它显示成一条空消息。
        raise SummaryError(
            "parse",
            "模型返回了空回答。可能是内容被服务方拦下了，或者这个模型不支持"
            "这种纯文本对话——换一个模型再试。", 502)
    return {"answer": answer, "model": out["model"]}
