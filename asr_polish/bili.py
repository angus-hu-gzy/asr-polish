# -*- coding: utf-8 -*-
"""B站视频文本获取：字幕轨优先，无字幕走必剪云端 ASR。

设计原则：
- 本项目的核心能力（术语订正 / 交叉验证 / 质量自评）始终零依赖。
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
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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

# 默认 cookies.txt 查找路径（兼容 WorkBuddy skill 的既有位置）
_DEFAULT_COOKIE_PATHS = (
    Path.home() / ".workbuddy" / "data" / "bilibili-video-summary" / "bilibili_cookies.txt",
)

_BVID_RE = re.compile(r"BV[0-9A-Za-z]+")


class BiliError(Exception):
    """B站文本获取过程中的可预期错误。"""


@dataclass
class FetchResult:
    bvid: str
    title: str
    owner: str
    duration: int                                  # 秒
    source: str                                    # "字幕轨" 或 "必剪云端ASR"
    text: str
    warnings: list[str] = field(default_factory=list)


# ── 基础 HTTP ────────────────────────────────────────────────

def _opener(cookie_path: Optional[str] = None):
    if cookie_path and Path(cookie_path).exists():
        jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
        jar.load(ignore_discard=True, ignore_expires=True)
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener()


def _http_json(url: str, cookie_path: Optional[str] = None,
               timeout: int = 60) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Referer": "https://www.bilibili.com/"})
    with _opener(cookie_path).open(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))


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


def get_video_info(bvid: str, cookie_path: Optional[str] = None) -> dict:
    data = _http_json(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        cookie_path=cookie_path)
    d = data.get("data") or {}
    if not d:
        raise BiliError(f"未取到视频信息（bvid 可能错误或被风控）：{data.get('message')}")
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
    nav = _http_json("https://api.bilibili.com/x/web-interface/nav",
                     cookie_path=cookie_path)
    wbi = ((nav.get("data") or {}).get("wbi_img") or {})
    img_key = Path(wbi.get("img_url") or "").stem
    sub_key = Path(wbi.get("sub_url") or "").stem
    if not img_key or not sub_key:
        return None

    mixin_key = "".join((img_key + sub_key)[i] for i in _WBI_MIXIN_TAB)[:32]
    params = {"bvid": video["bvid"], "aid": video["aid"],
              "cid": video["cid"], "wts": int(time.time())}
    query = urllib.parse.urlencode(sorted(params.items()))
    w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
    data = _http_json(
        f"https://api.bilibili.com/x/player/wbi/v2?{query}&w_rid={w_rid}",
        cookie_path=cookie_path)

    subs = ((data.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    pick = _pick_subtitle(subs)
    if not pick or not pick.get("subtitle_url"):
        return None

    sub_url = pick["subtitle_url"]
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url
    body = (_http_json(sub_url, cookie_path=cookie_path) or {}).get("body") or []
    lines = [it.get("content", "").strip()
             for it in body if (it.get("content") or "").strip()]
    return "\n".join(lines) if lines else None


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
            "resource_file_type": fmt, "model_id": 7})
        if r.get("code"):
            return None
        d = r["data"]
        etags = []
        per = d["per_size"]
        for i, url in enumerate(d["upload_urls"]):
            etags.append(_bcut_put(url, sound[i * per:(i + 1) * per]) or "")

        r = _bcut_http(_BCUT_COMMIT_UPLOAD, method="POST", form={
            "in_boss_key": d["in_boss_key"], "resource_id": d["resource_id"],
            "etags": ",".join(etags), "upload_id": d["upload_id"], "model_id": 7})
        if r.get("code"):
            return None
        download_url = r["data"]["download_url"]

        task_id = None
        for attempt in range(3):
            r = _bcut_http(_BCUT_CREATE_TASK, method="POST",
                           json_body={"resource": download_url, "model_id": "7"})
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
                {"model_id": 7, "task_id": task_id}))
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
          prefer_subtitle: bool = True) -> FetchResult:
    """从 B站链接获取视频文本。字幕轨优先，无字幕走必剪云端 ASR。"""
    bvid = extract_bvid(url)
    if not bvid:
        raise BiliError(f"未能从输入识别 bvid：{url}")

    auto_cookie = False
    if not cookie_path:
        for p in _DEFAULT_COOKIE_PATHS:
            if p.exists():
                cookie_path = str(p)
                auto_cookie = True
                break

    video = get_video_info(bvid, cookie_path=cookie_path)
    result = FetchResult(
        bvid=bvid, title=video["title"], owner=video["owner"],
        duration=video["duration"], source="", text="")
    if auto_cookie:
        result.warnings.append("已自动加载本机 Cookie（用于获取更完整的字幕）")
    if len(video["pages"]) > 1:
        result.warnings.append(f"多 P 视频（共 {len(video['pages'])} P），仅处理第一 P")

    if prefer_subtitle:
        text = fetch_subtitle(video, cookie_path=cookie_path)
        if text:
            result.source = "字幕轨"
            result.text = text
            return result

    result.warnings.append("无字幕轨，音频将上传至 B站必剪服务做云端转写（公开视频音轨，风险低）")
    text = transcribe_bcut(video, cookie_path=cookie_path)
    if not text:
        raise BiliError("字幕轨与必剪 ASR 均未取到文本（可能音频下载失败或视频受限）")
    result.source = "必剪云端ASR"
    result.text = text
    return result
