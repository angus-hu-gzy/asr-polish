# -*- coding: utf-8 -*-
"""HTTP 层：极简 REST + 一个只读面板。

只用标准库 `http.server`。为什么不引 FastAPI：本项目的核心承诺是零依赖，
服务化如果为此拖进 uvicorn + starlette + pydantic，等于换了个项目。
这里每一行都能读，代价是功能少——没有鉴权、没有 TLS、没有优雅重启，
所以默认只监听 127.0.0.1，要对外必须先自己套一层反向代理。

接口
----
POST   /api/tasks            {"kind":"subs","target":"BV...", ...}  → 202 + 任务
GET    /api/tasks/{id}       查单个任务
GET    /api/tasks            列表，支持 ?status=&limit=
POST   /api/tasks/{id}/retry 手动重试（failed 或 running → pending）
GET    /api/health           健康检查
GET    /                     面板
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from ..bili import cookie_status, current_min_interval, throttle_waited
from .store import FAILED, PENDING, RUNNING, Store
from .worker import WorkerPool

_MAX_BODY = 1 << 20          # 1 MB，请求体上限


class App:
    def __init__(self, store: Store, pool: Optional[WorkerPool] = None) -> None:
        self.store = store
        self.pool = pool
        self.events: list[str] = []
        self._lock = threading.Lock()

    def note(self, msg: str) -> None:
        from datetime import datetime
        line = "%s %s" % (datetime.now().strftime("%H:%M:%S"), msg)
        with self._lock:
            self.events.append(line)
            del self.events[:-200]


def _index_html() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>asr-polish 任务面板</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#232a35;--text:#c9d1d9;--muted:#7d8590;
--pend:#8b949e;--run:#39d0d8;--done:#3fb950;--fail:#f85149;--accent:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:13px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:24px 18px 50px}
h1{font-size:17px;margin:0 0 4px}
.sub{color:var(--muted);font-size:12px;margin:0 0 18px}
.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card .lb{font-size:11px;color:var(--muted)}
.card .vl{font-size:19px;font-family:ui-monospace,Consolas,monospace}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th{font-size:11px;color:var(--muted);text-align:left;font-weight:600;padding:8px 10px;border-bottom:1px solid var(--line);background:#12171f}
td{padding:8px 10px;border-bottom:1px solid #1b222c;font-size:12px;vertical-align:middle}
tr:last-child td{border-bottom:none}
.mono{font-family:ui-monospace,Consolas,monospace}
.st{font-size:10.5px;letter-spacing:.04em}
.pending{color:var(--pend)}.running{color:var(--run)}.done{color:var(--done)}.failed{color:var(--fail)}
.bar{height:5px;background:#1b2430;border-radius:3px;overflow:hidden;min-width:70px}
.bar i{display:block;height:100%;background:var(--run)}
tr.done-row .bar i{background:var(--done)}
tr.failed-row .bar i{background:var(--fail)}
.err{color:var(--fail);font-size:11px}
.empty{text-align:center;color:var(--muted);padding:28px}
.ctl{display:flex;gap:8px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
input,select,button{background:#1a212b;border:1px solid var(--line);color:var(--text);
border-radius:6px;padding:6px 9px;font-size:12px;font-family:inherit}
button{cursor:pointer}button:hover{border-color:#31404f}
button.pri{background:#143a55;border-color:#1d5f8a;color:#cfe6ff}
.banner{display:none;padding:9px 12px;border-radius:8px;margin-bottom:14px;font-size:12px;line-height:1.6}
.banner.show{display:block}
.banner.error{background:#3a1416;border:1px solid #6e2326;color:#ffb4ab}
.banner.warn{background:#3a2e12;border:1px solid #6e5a1e;color:#e3b341}
.banner.ok{background:#12291a;border:1px solid #1f5233;color:#7ee2a8}
.banner .meta{opacity:.75;font-size:11px;margin-top:4px}
.msg{color:var(--run);font-size:11px;margin-top:2px}
</style></head>
<body><div class="wrap">
<h1>asr-polish 任务面板</h1>
<p class="sub">每 3 秒自动刷新 · 数据来自 SQLite，进程重启不丢</p>
<div id="banner" class="banner"></div>
<div class="cards" id="cards"></div>
<div class="ctl">
  <input id="target" placeholder="BV 号 / up:123 / search:关键词" style="width:280px">
  <button class="pri" id="submit">提交任务</button>
  <button id="checklogin">检查登录态</button>
  <span class="sub" id="msg" style="margin:0"></span>
</div>
<table><thead><tr>
<th style="width:104px">任务</th><th style="width:60px">类型</th>
<th>目标</th><th style="width:120px">进度</th>
<th style="width:76px">状态</th><th style="width:70px">耗时</th>
</tr></thead><tbody id="rows"></tbody></table>
</div>
<script>
const $ = id => document.getElementById(id);
function esc(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}
async function load(){
  const r = await fetch("/api/tasks?limit=40");
  const d = await r.json();
  const s = d.stats || {};
  $("cards").innerHTML = [["任务总数",s.total||0],["排队",s.pending||0],
    ["进行中",s.running||0],["完成",s.done||0],["失败",s.failed||0]]
    .map(([k,v])=>`<div class="card"><div class="lb">${k}</div><div class="vl">${v}</div></div>`).join("");
  const rows = d.tasks || [];
  if(!rows.length){$("rows").innerHTML='<tr><td colspan="6" class="empty">还没有任务</td></tr>';return}
  $("rows").innerHTML = rows.map(t=>{
    const pct = Math.round((t.progress||0)*100);
    const err = t.error ? `<div class="err">${esc(t.error).slice(0,80)}</div>` : "";
    const note = (t.status==="running" && t.message)
      ? `<div class="msg">${esc(t.message).slice(0,90)}</div>` : "";
    return `<tr class="${t.status}-row">
      <td class="mono">${t.id}</td>
      <td>${esc(t.kind)}</td>
      <td>${esc(t.payload && t.payload.target || "")}${note}${err}</td>
      <td><div class="bar"><i style="width:${pct}%"></i></div></td>
      <td class="st ${t.status}">${t.status.toUpperCase()}</td>
      <td class="mono">${t.duration ? t.duration+"s" : (t.status==="running"?"…":"")}</td>
    </tr>`}).join("");
}
async function loadHealth(force){
  let h;
  try { h = await (await fetch("/api/health" + (force ? "?fresh=1" : ""))).json() }
  catch(e){ return }
  const c = h.cookie || {}, t = h.throttle || {}, b = $("banner");
  b.className = "banner show " + (c.level || "ok");
  const bits = [];
  if (t.min_interval) bits.push("请求间隔 " + t.min_interval + "s");
  else bits.push("未限速");
  if (t.waited) bits.push("已为限速等待 " + t.waited + "s");
  if (c.logged_in === true) bits.push("登录态已确认有效");
  else if (c.logged_in === false) bits.push("登录态已失效");
  else bits.push("登录态未确认");
  if (c.age_days !== null && c.age_days !== undefined) bits.push("Cookie " + c.age_days + " 天前更新");
  b.innerHTML = "<b>Cookie：</b>" + esc(c.note || "")
    + (c.path ? ' <span class="mono" style="opacity:.7">' + esc(c.path) + "</span>" : "")
    + '<div class="meta">' + esc(bits.join(" · ")) + "</div>";
}
$("checklogin").onclick = () => {
  $("msg").textContent = "正在确认登录态…";
  loadHealth(true).then(() => { $("msg").textContent = "" });
};
$("submit").onclick = async () => {
  const target = $("target").value.trim();
  if(!target) return;
  const r = await fetch("/api/tasks",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({kind:"subs",target})});
  const d = await r.json();
  $("msg").textContent = r.ok ? "已提交 " + d.id : ("失败：" + (d.error||r.status));
  $("target").value = "";
  load();
};
load(); loadHealth(); setInterval(load, 3000); setInterval(loadHealth, 15000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "asr-polish"

    # ── 基础工具 ────────────────────────────────────────────

    @property
    def app(self) -> App:
        return self.server.app          # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        self.app.note("%s %s" % (self.command, fmt % args))

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _err(self, code: int, msg: str) -> None:
        self._json(code, {"error": msg})

    def _body(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0:
            return {}
        if length > _MAX_BODY:
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # ── 路由 ────────────────────────────────────────────────

    def do_OPTIONS(self) -> None:                       # noqa: N802
        self._send(204, b"", "text/plain")

    def do_GET(self) -> None:                           # noqa: N802
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        qs = parse_qs(u.query)

        if not parts:
            self._send(200, _index_html().encode("utf-8"),
                       "text/html; charset=utf-8")
            return

        if parts == ["api", "health"]:
            # 默认验证登录态：只检查「文件存在、有 SESSDATA」是不够的——
            # SESSDATA 可能时间没过期但服务端已注销，那样面板会一直显示绿色
            # 「已配置」，用户却怎么抓都风控失败。验证结果缓存 5 分钟，
            # 所以每 3 秒刷新的面板并不会真的每 3 秒去问一次 B站。
            verify = (qs.get("verify") or ["1"])[0] not in ("0", "false", "no")
            fresh = (qs.get("fresh") or ["0"])[0] in ("1", "true", "yes")
            self._json(200, {
                "ok": True,
                "workers": self.app.pool.status() if self.app.pool else [],
                "stats": self.app.store.stats(),
                "cookie": cookie_status(verify=verify, fresh=fresh),
                "throttle": {"min_interval": current_min_interval(),
                             "waited": round(throttle_waited(), 1)},
            })
            return

        if parts == ["api", "tasks"]:
            status = (qs.get("status") or [None])[0]
            if status and status not in (PENDING, RUNNING, "done", FAILED):
                self._err(400, "未知状态：%s" % status)
                return
            try:
                limit = min(200, max(1, int((qs.get("limit") or ["50"])[0])))
            except ValueError:
                limit = 50
            tasks = self.app.store.list(status=status, limit=limit)
            self._json(200, {"stats": self.app.store.stats(),
                             "tasks": [t.to_dict() for t in tasks]})
            return

        if len(parts) == 3 and parts[:2] == ["api", "tasks"]:
            task = self.app.store.get(parts[2])
            if task is None:
                self._err(404, "没有这个任务：%s" % parts[2])
                return
            self._json(200, task.to_dict())
            return

        self._err(404, "未知路径：%s" % u.path)

    def do_POST(self) -> None:                          # noqa: N802
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]

        if parts == ["api", "tasks"]:
            data = self._body()
            if data is None:
                self._err(400, "请求体不是合法 JSON（或超过 1MB）")
                return
            kind = data.get("kind") or "subs"
            if not data.get("target"):
                self._err(400, "缺少 target")
                return
            payload = {k: v for k, v in data.items() if k != "kind"}
            task = self.app.store.create(kind, payload)
            self.app.note("新建任务 %s（%s）" % (task.id, kind))
            self._json(202, task.to_dict())
            return

        if len(parts) == 4 and parts[:2] == ["api", "tasks"] \
                and parts[3] == "retry":
            task_id = parts[2]
            if not self.app.store.requeue(task_id):
                task = self.app.store.get(task_id)
                if task is None:
                    self._err(404, "没有这个任务：%s" % task_id)
                else:
                    self._err(409, "状态 %s 不能重试" % task.status)
                return
            self.app.note("任务 %s 手动重试" % task_id)
            task = self.app.store.get(task_id)
            self._json(202, task.to_dict() if task else {})
            return

        self._err(404, "未知路径：%s" % u.path)


def make_server(host: str, port: int, app: App) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.app = app                       # type: ignore[attr-defined]
    return srv
