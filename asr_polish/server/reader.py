# -*- coding: utf-8 -*-
"""阅读页：把「取到的字幕」变成一篇能读的文字稿。

为什么单独一个文件
------------------
`app.py` 已经有四百多行，其中还嵌着一个任务面板的 HTML。再往里塞一个页面，
下次找路由就得先翻过两千行字符串。这里只放页面本身，路由仍然在 `app.py`——
一个入口，职责清楚。

为什么不做框架、不做构建
------------------------
这个项目的卖点是「核心零第三方依赖」。页面如果要 npm，就等于给「打开就能用」
加了一道门槛，而它做的事只是：发三个请求、画几段字、标几个黄底。

三个刻意的选择
--------------
1. **不在前端解析 BV 号**。输入框拿到什么就原样提交，提取由服务端已有的
   `extract_bvid` 完成。前端再写一遍会和服务端慢慢分叉（`?spm_id_from=`
   这类后缀、带不带 `https://`、大小写），两套规则意味着两种结果。
2. **相邻字幕行合并成段落**。B站 AI 字幕一条只有十来个字，一句被切成好几行。
   逐条画出来仍然是碎的，只是每行前面多了个时间戳。按时间间隔合并，段首保留
   时间戳，读起来才是一篇文章。
3. **正文一律走 textContent**。字幕是别人视频里的文本，直接拼进 innerHTML
   就等于给自己开一个 XSS 口子；本地自用也不该养成这种习惯。
"""
from __future__ import annotations

#: 页面是一整块字符串。用原始字符串：里面全是 JS 的 `\n`、正则的 `\s`，
#: 走 Python 转义会变成别的字符。
_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>asr-polish · 字幕阅读</title>
<style>
:root{
  --paper:#fbfaf7; --ink:#1f2328; --muted:#6b7280; --faint:#9aa4b0;
  --line:#e6e1d6; --accent:#2a6df4; --mark:#ffe98a; --mark-cur:#ffb020;
  --ok:#15803d; --warn:#b45309; --bad:#b91c1c;
  --panel:#fff; --shadow:0 1px 2px rgba(29,33,41,.06),0 8px 24px rgba(29,33,41,.05);
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--paper); color:var(--ink);
  font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
       "Microsoft YaHei",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;
}
button,input{font:inherit;color:inherit}
button{cursor:pointer}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

/* ── 顶栏 ─────────────────────────────────────────── */
header{
  position:sticky;top:0;z-index:20;background:rgba(251,250,247,.92);
  backdrop-filter:saturate(1.4) blur(8px);border-bottom:1px solid var(--line);
}
.bar{max-width:940px;margin:0 auto;padding:12px 20px 10px;
     display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.brand{font-size:14px;font-weight:650;letter-spacing:.01em;white-space:nowrap}
.brand span{color:var(--faint);font-weight:400;margin-left:6px}
.brand a{color:var(--faint);font-weight:400;font-size:12px;margin-left:10px}
form{display:flex;gap:8px;flex:1;min-width:260px}
input[type=text]{
  flex:1;min-width:0;background:var(--panel);border:1px solid var(--line);
  border-radius:9px;padding:8px 12px;outline:none;font-size:14px;
  transition:border-color .15s,box-shadow .15s;
}
input[type=text]:focus{border-color:#bcd0f7;box-shadow:0 0 0 3px rgba(42,109,244,.12)}
input[type=text]::placeholder{color:#b3aebf}
.btn{
  border:1px solid var(--line);background:var(--panel);border-radius:9px;
  padding:8px 14px;font-size:14px;white-space:nowrap;
  transition:background .15s,border-color .15s;
}
.btn:hover{background:#f6f4ee;border-color:#d8d2c4}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:550}
.btn.primary:hover{background:#1e5fe0}
.btn:disabled{opacity:.55;cursor:default}
.btn.sm{padding:5px 11px;font-size:12.5px;border-radius:7px}

/* ── 进度 ─────────────────────────────────────────── */
#run{max-width:940px;margin:0 auto;padding:0 20px 12px}
#run[hidden]{display:none}
.runbar{height:3px;background:#eae5da;border-radius:2px;overflow:hidden}
.runbar i{display:block;height:100%;width:0;background:var(--accent);
          transition:width .35s ease}
.runmeta{display:flex;gap:12px;justify-content:space-between;margin-top:7px;
         font-size:12.5px;color:var(--muted)}
.runmeta .m{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.runmeta .t{font-variant-numeric:tabular-nums;white-space:nowrap}
.dots::after{content:"";animation:dots 1.4s steps(4,end) infinite}
@keyframes dots{0%{content:""}25%{content:"·"}50%{content:"··"}75%{content:"···"}}

/* ── 主体 ─────────────────────────────────────────── */
main{max-width:940px;margin:0 auto;padding:22px 20px 120px}
.notice{
  border:1px solid var(--line);background:var(--panel);border-radius:11px;
  padding:14px 16px;margin:0 0 18px;font-size:14px;line-height:1.7;
  box-shadow:var(--shadow);
}
.notice b{font-weight:650}
.notice.warn{border-color:#f0d9a8;background:#fffaf0}
.notice.bad{border-color:#f2c9c9;background:#fff6f6}
.notice.ok{border-color:#cfe6d6;background:#f5fbf7;padding:10px 14px;font-size:13.5px}
.notice .meta{color:var(--muted);font-size:12.5px;margin-top:6px}
.notice ol{margin:8px 0 0;padding-left:20px}
.notice li{margin:2px 0}
.notice code{background:#f0ece1;padding:1px 5px;border-radius:4px;
             font-size:12.5px;font-family:ui-monospace,Consolas,monospace}

/* 搜索条 */
.tools{
  display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  padding:10px 12px;margin:0 0 22px;background:var(--panel);
  border:1px solid var(--line);border-radius:11px;box-shadow:var(--shadow);
}
.tools[hidden]{display:none}
.tools input{flex:1;min-width:170px;background:#f7f5ef;border-color:transparent}
.tools input:focus{background:var(--panel);border-color:#bcd0f7}
.tools .count{font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums;
              min-width:52px;text-align:right}
.tools .stat{font-size:12.5px;color:var(--faint);white-space:nowrap;
             border-left:1px solid var(--line);padding-left:11px}

/* 正文 */
article{max-width:46em}
.part + .part{margin-top:46px;padding-top:26px;border-top:1px dashed var(--line)}
.part h2{font-size:15px;font-weight:600;color:var(--muted);margin:0 0 18px;
         letter-spacing:.01em}
.cue{
  display:grid;grid-template-columns:56px 1fr;column-gap:20px;
  margin:0 0 18px;scroll-margin-top:96px;border-radius:6px;
  transition:background .3s;
}
.cue .t{
  font:11.5px/1.9 ui-monospace,SFMono-Regular,Consolas,monospace;
  color:var(--faint);text-align:right;padding-top:.42em;
  user-select:none;font-variant-numeric:tabular-nums;
}
.cue .txt{
  margin:0;font-size:17.5px;line-height:1.95;letter-spacing:.012em;
  text-align:justify;word-break:break-word;
}
.cue.flash{background:#fff6cc;box-shadow:0 0 0 8px #fff6cc}
mark{background:var(--mark);color:inherit;border-radius:2px;padding:0 1px}
mark.cur{background:var(--mark-cur);box-shadow:0 0 0 2px rgba(255,176,32,.35)}
.empty{padding:70px 0;text-align:center;color:var(--muted);font-size:14px;line-height:2}
.empty kbd{background:var(--panel);border:1px solid var(--line);border-bottom-width:2px;
           border-radius:5px;padding:1px 6px;font-size:12px;
           font-family:ui-monospace,Consolas,monospace}

/* 失败明细 */
.fails{margin:26px 0 0;border-top:1px solid var(--line);padding-top:18px}
.fails h3{font-size:13.5px;font-weight:600;margin:0 0 10px;color:var(--bad)}
.fails ul{list-style:none;margin:0 0 14px;padding:0}
.fails li{display:grid;grid-template-columns:150px 1fr;gap:14px;
          padding:8px 0;border-bottom:1px solid #f0ece1;font-size:13px}
.fails li:last-child{border-bottom:none}
.fails .bv{font-family:ui-monospace,Consolas,monospace;color:var(--muted);font-size:12.5px}
.fails .why{color:#4b5563;word-break:break-word}
</style>
</head>
<body>

<header>
  <div class="bar">
    <div class="brand">asr-polish<span>字幕阅读</span><a href="/">任务面板 →</a></div>
    <form id="f" autocomplete="off">
      <input id="target" type="text" spellcheck="false"
             placeholder="粘贴 BV 号或视频链接，回车取字幕">
      <button class="btn primary" id="go" type="submit">取字幕</button>
    </form>
  </div>
  <div id="run" hidden>
    <div class="runbar"><i id="fill"></i></div>
    <div class="runmeta"><span class="m" id="runmsg"></span><span class="t" id="runtime"></span></div>
  </div>
</header>

<main>
  <div id="notice"></div>

  <div class="tools" id="tools" hidden>
    <input id="find" type="text" spellcheck="false" placeholder="在文字稿里搜…">
    <button class="btn sm" id="prev" title="上一处 (Shift+Enter)">↑</button>
    <button class="btn sm" id="next" title="下一处 (Enter)">↓</button>
    <span class="count" id="count"></span>
    <span class="stat" id="stat"></span>
  </div>

  <article id="doc"></article>
  <div id="fails"></div>
</main>

<script>
(function(){
"use strict";

var $ = function(s){ return document.querySelector(s); };
var OUT = {
  complete: ["全部成功", "ok"],
  partial:  ["部分成功", "warn"],
  empty:    ["没有产出", "warn"],
  failed:   ["全部失败", "bad"]
};
var S = { id:null, timer:null, tick:null, startedAt:0, marks:[], cur:-1,
          text:"", parts:0, chars:0 };

/* ── 小工具 ──────────────────────────────────────── */
function el(tag, cls, text){
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}
function fmt(sec){
  if (sec == null || isNaN(sec)) return "";
  sec = Math.max(0, Math.round(sec));
  var h = Math.floor(sec/3600), m = Math.floor(sec%3600/60), s = sec%60;
  var pad = function(x){ return x < 10 ? "0"+x : ""+x; };
  return h ? h+":"+pad(m)+":"+pad(s) : pad(m)+":"+pad(s);
}
function clear(node){ while (node.firstChild) node.removeChild(node.firstChild); }

function notice(kind, title, body, extra){
  var box = el("div", "notice " + (kind||""));
  if (title) box.appendChild(el("b", null, title));
  if (body) {
    if (title) box.appendChild(document.createElement("br"));
    box.appendChild(document.createTextNode(body));
  }
  if (extra) box.appendChild(extra);
  var host = $("#notice");
  host.appendChild(box);
  return box;
}
function resetNotice(){ clear($("#notice")); }

/* ── 任务：提交 / 轮询 ───────────────────────────── */
async function call(method, path, body){
  var opt = { method: method, headers: {} };
  if (body !== undefined){
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  var r = await fetch(path, opt);
  var data = {};
  try { data = await r.json(); } catch(e){}
  return { code: r.status, ok: r.ok, data: data };
}

function setRun(show){ $("#run").hidden = !show; }

function showProgress(t){
  setRun(true);
  var pct = Math.round((t.progress || 0) * 100);
  $("#fill").style.width = Math.max(pct, t.status === "pending" ? 2 : 4) + "%";
  var msg = t.message || (t.status === "pending" ? "排队中" : "正在抓取");
  if (t.status === "running") {
    $("#runmsg").textContent = msg;
    $("#runmsg").className = "m dots";
  } else {
    $("#runmsg").textContent = "排队中：等 worker 领任务";
    $("#runmsg").className = "m";
  }
  if (t.status === "pending") { $("#runtime").textContent = ""; }
}

function stopTimer(){ if (S.tick) { clearInterval(S.tick); S.tick = null; } }

function startTimer(startedAt){
  stopTimer();
  S.startedAt = startedAt || (Date.now()/1000);
  var paint = function(){
    var sec = Date.now()/1000 - S.startedAt;
    $("#runtime").textContent = "已用 " + fmt(sec);
  };
  paint();
  S.tick = setInterval(paint, 1000);
}

function switchTo(id, note){
  S.id = id;
  try { history.replaceState(null, "", "?task=" + encodeURIComponent(id)); } catch(e){}
  if (S.timer) { clearTimeout(S.timer); S.timer = null; }
  stopTimer();                       // 否则新任务会沿用上一个任务的开始时间
  resetNotice(); clear($("#doc")); clear($("#fails"));
  $("#tools").hidden = true;
  if (note) notice("ok", note, "");
  poll();
}

async function submit(ev){
  if (ev) ev.preventDefault();
  var raw = $("#target").value.trim();
  if (!raw) return;
  $("#go").disabled = true;
  resetNotice(); clear($("#doc")); clear($("#fails")); $("#tools").hidden = true;
  // formats 必须是 srt：阅读页要按时间戳分段，txt 里没有时间。
  // 这里只是把现有接口已有的 payload 字段填上，没有改接口。
  var res = await call("POST", "/api/tasks",
                       { kind: "subs", target: raw, formats: ["srt"] });
  $("#go").disabled = false;
  if (!res.ok){
    notice("bad", "没能提交任务", res.data.error || ("HTTP " + res.code));
    idle();
    return;
  }
  switchTo(res.data.id, "任务 " + res.data.id + " 已提交");
}

async function poll(){
  if (!S.id) return;
  if (S.timer) clearTimeout(S.timer);
  var res = await call("GET", "/api/tasks/" + encodeURIComponent(S.id));
  if (res.code === 404){
    setRun(false);
    notice("bad", "任务不存在", "可能已被清理：" + S.id);
    return;
  }
  if (!res.ok) { S.timer = setTimeout(poll, 2500); return; }
  var t = res.data;
  if (t.status === "pending" || t.status === "running"){
    showProgress(t);
    if (!S.tick) startTimer(t.started_at || t.created_at);
    S.timer = setTimeout(poll, 1500);
    return;
  }
  // 终态
  stopTimer();
  setRun(false);
  if (t.status === "done") await loadTranscript(t);
  else renderFailure(t);
}

/* ── 正文 ────────────────────────────────────────── */
async function loadTranscript(task){
  var res = await call("GET", "/api/tasks/" + encodeURIComponent(S.id) + "/transcript");
  if (res.code === 409){          // 后端说还没跑完，继续等
    poll();
    return;
  }
  if (!res.ok){
    notice("bad", "读不到文字稿", res.data.error || ("HTTP " + res.code));
    return;
  }
  renderDoc(res.data, task);
}

function renderDoc(d, task){
  var outcome = d.outcome || (task && task.result && task.result.outcome) || "complete";
  var meta = OUT[outcome] || [outcome, "warn"];
  var r = (task && task.result) || {};
  var bits = [];
  // 「段」在这个页面里已经被段落占用了，所以任务层一律说「分 P」，
  // 否则一行里两个「段」指的是两件事。
  if (r.parts_written != null) bits.push("成功 " + r.parts_written + " 个分 P");
  if (r.no_subtitle) bits.push("无字幕 " + r.no_subtitle + " 个");
  if (r.failed) bits.push("失败 " + r.failed + " 处");
  if (d.parts.length > 1) bits.push("显示 " + d.parts.length + " 篇");

  if (!d.parts || !d.parts.length){
    notice(meta[1], meta[0], d.hint || "没有可显示的内容");
    if (r.parts_truncated) {
      notice("warn", "分 P 被截断", "这条合集共 " + (r.parts_truncated + (r.parts_written||0)) +
             " 个分 P，按单条上限只取了一部分。要全取请用命令行带 max_parts=0 或走任务面板。");
    }
    renderFailures(r, task);
    return;
  }

  notice(meta[1], meta[0], bits.length ? bits.join("，") : "",
         d.skipped && d.skipped.length ? skipList(d.skipped) : null);

  var doc = $("#doc");
  clear(doc);
  S.chars = 0; S.parts = d.parts.length;
  d.parts.forEach(function(part, i){
    var sec = el("section", "part");
    if (d.parts.length > 1){
      var h = el("h2", null, "第 " + (i+1) + " 篇 · " + part.name.replace(/\.(srt|json|txt)$/i, ""));
      sec.appendChild(h);
    }
    groupCues(part.segments).forEach(function(p){
      var cue = el("div", "cue");
      if (p.start != null){
        cue.dataset.t = p.start;
        cue.title = fmt(p.start) + (p.end != null ? " → " + fmt(p.end) : "");
        cue.appendChild(el("span", "t", fmt(p.start)));
      } else {
        cue.appendChild(el("span", "t", ""));
      }
      var txt = el("p", "txt", p.text);
      cue.appendChild(txt);
      cue._text = p.text;
      sec.appendChild(cue);
      S.chars += p.text.length;
    });
    doc.appendChild(sec);
  });

  $("#stat").textContent = S.chars.toLocaleString() + " 字 · " + doc.querySelectorAll(".cue").length + " 段";
  $("#tools").hidden = false;
  renderFailures(r, task);
  if ($("#find").value) runSearch($("#find").value);
}

function skipList(skipped){
  var box = el("div", "meta");
  box.textContent = "已跳过：" + skipped.map(function(s){
    return s.file + "（" + s.reason + "）";
  }).join("；");
  return box;
}

/* 相邻字幕行合并成段落。B站 AI 字幕一条只有十来个字，逐条画出来仍然是碎的。
   三个断段判据，按可靠性排序：
   1. 时间间隔 —— 话与话之间的静默。实测这版数据里 58 条字幕的间隔全是 0.00s
      （字幕轨本来就是首尾相接排的），所以它只在有真停顿时起作用。
   2. 句子起始词 —— 没有停顿也没有标点时，只能靠「那 / 所以 / 但是」这些
      开头判断换句。纯展示用，不改动原文一个字。
   3. 长度上限 —— 兜底。没有这条，一小时的视频会变成一个段落。 */
var SENT_START = ["那","所以","但是","然后","因为","如果","就是","其实","首先",
                  "其次","最后","接下来","我们","大家","这个","那么","另外",
                  "总之","比如","还有","这里","注意"];

function startsSentence(t){
  for (var i = 0; i < SENT_START.length; i++){
    if (t.indexOf(SENT_START[i]) === 0) return true;
  }
  return false;
}

function groupCues(segs){
  var MAXLEN = 120, MAXGAP = 0.8, MINLEN = 24;
  var out = [], cur = null;
  for (var i = 0; i < segs.length; i++){
    var s = segs[i], text = (s.text || "").replace(/\s+$/,"").replace(/^\s+/,"");
    if (!text) continue;
    var brk = true;
    if (cur){
      var gap = (s.start != null && cur.end != null) ? (s.start - cur.end) : MAXGAP;
      brk = gap >= MAXGAP
         || (cur.text.length + text.length) > MAXLEN
         || (cur.text.length >= MINLEN && startsSentence(text));
    }
    if (brk){
      cur = { start: s.start, end: s.end, text: text };
      out.push(cur);
    } else {
      if (/[A-Za-z0-9]$/.test(cur.text) && /^[A-Za-z0-9]/.test(text)) cur.text += " ";
      cur.text += text;
      cur.end = s.end;
    }
  }
  return out;
}

/* ── 搜索高亮 ────────────────────────────────────── */
function paint(cue, q){
  var host = cue.querySelector(".txt");
  clear(host);
  var text = cue._text;
  if (!q){ host.textContent = text; return; }
  var low = text.toLowerCase(), lq = q.toLowerCase(), i = 0;
  while (true){
    var k = low.indexOf(lq, i);
    if (k < 0){ host.appendChild(document.createTextNode(text.slice(i))); break; }
    if (k > i) host.appendChild(document.createTextNode(text.slice(i, k)));
    var m = el("mark", "hit", text.slice(k, k + q.length));
    host.appendChild(m);
    i = k + q.length;
  }
}

function runSearch(q){
  q = (q || "").trim();
  S.marks = []; S.cur = -1;
  var cues = document.querySelectorAll("#doc .cue");
  for (var i = 0; i < cues.length; i++){
    paint(cues[i], q);
    cues[i].classList.remove("flash");
  }
  if (!q){ $("#count").textContent = ""; return; }
  S.marks = Array.prototype.slice.call(document.querySelectorAll("#doc mark.hit"));
  $("#count").textContent = S.marks.length ? "1 / " + S.marks.length : "0 处";
  if (S.marks.length) jump(0);
}

function jump(i){
  if (!S.marks.length) return;
  i = (i + S.marks.length) % S.marks.length;
  if (S.cur >= 0 && S.marks[S.cur]) S.marks[S.cur].classList.remove("cur");
  S.cur = i;
  var m = S.marks[i];
  m.classList.add("cur");
  $("#count").textContent = (i + 1) + " / " + S.marks.length;
  var cue = m.closest(".cue");
  if (cue){
    cue.scrollIntoView({ block: "center", behavior: "smooth" });
    cue.classList.add("flash");
    setTimeout(function(){ cue.classList.remove("flash"); }, 1400);
  }
}

/* ── 失败：明细 + 只重跑失败条目 ─────────────────── */
function renderFailure(t){
  resetNotice();
  var err = t.error || "任务失败，但没有给出原因";
  var r = t.result || {};
  if (/需要登录态|Cookie 可能已失效|BiliLoginRequired/.test(err)){
    var box = el("div");
    box.appendChild(el("div", null, "B站要求登录态才能取 AI 字幕，而当前 Cookie 不可用。"));
    var ol = el("ol");
    ol.appendChild(el("li", null, "在浏览器登录 B站，用扩展导出 cookies.txt（Netscape 格式）"));
    ol.appendChild(el("li", null, "放到 README「登录态」一节列出的任一位置，或用 --cookie 指定"));
    ol.appendChild(el("li", null, "把服务重启一次，然后回来重试这个任务"));
    box.appendChild(ol);
    var meta = el("div", "meta", "原始错误：" + err.split("\n")[0]);
    box.appendChild(meta);
    notice("bad", "Cookie 失效", "", box);
  } else if (/风控|退避|412|429|请求被 B站拦截/.test(err)){
    notice("warn", "被 B 站限流（不是代码问题）",
           "抓太快触发了风控。等几分钟再试，或者把请求间隔调大（serve 的 min-interval）。" +
           "剩余任务不会因为这次失败而丢失。",
           el("div", "meta", "原始错误：" + err.split("\n")[0]));
  } else {
    notice("bad", "任务失败", err, null);
  }
  renderFailures(r, t);
}

function renderFailures(r, t){
  var items = (r && r.failed_items) || [];
  var host = $("#fails");
  clear(host);
  if (!items.length){
    if (t && t.status === "failed"){
      var retry = el("button", "btn sm", "重试整个任务");
      retry.addEventListener("click", function(){
        retry.disabled = true; retry.textContent = "提交中…";
        call("POST", "/api/tasks/" + encodeURIComponent(S.id) + "/retry").then(function(res){
          if (res.ok){ switchTo(S.id, "已重新排队"); }
          else { retry.disabled = false; retry.textContent = "重试整个任务";
                 notice("bad", "重试失败", res.data.error || ("HTTP " + res.code)); }
        });
      });
      host.appendChild(retry);
    }
    return;
  }
  var box = el("div", "fails");
  box.appendChild(el("h3", null, "失败明细（" + items.length + " 处）"));
  var ul = el("ul");
  items.slice(0, 50).forEach(function(it){
    var li = el("li");
    li.appendChild(el("span", "bv", it.retry || it.bvid || ""));
    li.appendChild(el("span", "why", it.error || "未知原因"));
    ul.appendChild(li);
  });
  box.appendChild(ul);
  if (items.length > 50) box.appendChild(el("div", "meta", "只列前 50 条，共 " + items.length + " 处"));

  var btn = el("button", "btn sm", "只重跑这 " + items.length + " 条");
  btn.addEventListener("click", async function(){
    btn.disabled = true; btn.textContent = "提交中…";
    var res = await call("POST", "/api/tasks/" + encodeURIComponent(S.id) + "/retry-failed");
    if (!res.ok){
      btn.disabled = false;
      btn.textContent = "只重跑这 " + items.length + " 条";
      notice("bad", "没能重跑", res.data.error || ("HTTP " + res.code));
      return;
    }
    switchTo(res.data.id, "已新建 " + res.data.id + "，只跑失败的 " + (res.data.only || []).length + " 条");
  });
  box.appendChild(btn);
  host.appendChild(box);
}

/* ── 启动 ────────────────────────────────────────── */
function idle(){
  var box = el("div", "empty");
  box.appendChild(el("div", null, "粘贴一个 B站视频的 BV 号或链接，回车。"));
  var d = el("div");
  d.appendChild(document.createTextNode("也可以打开一个历史任务："));
  d.appendChild(document.createTextNode(" "));
  var c = el("kbd", null, "?task=T…");
  d.appendChild(c);
  box.appendChild(d);
  $("#doc").appendChild(box);
}

$("#f").addEventListener("submit", submit);

var findTimer = null;
$("#find").addEventListener("input", function(e){
  var v = e.target.value;
  if (findTimer) clearTimeout(findTimer);
  findTimer = setTimeout(function(){ runSearch(v); }, 160);
});
$("#find").addEventListener("keydown", function(e){
  if (e.key === "Enter"){ e.preventDefault(); jump(S.cur + (e.shiftKey ? -1 : 1)); }
  if (e.key === "Escape"){ e.target.value = ""; runSearch(""); }
});
$("#next").addEventListener("click", function(){ jump(S.cur + 1); });
$("#prev").addEventListener("click", function(){ jump(S.cur - 1); });

// 点高亮处 = 把这一处设为当前，跳到它所在段落
$("#doc").addEventListener("click", function(e){
  var m = e.target.closest ? e.target.closest("mark.hit") : null;
  if (!m) return;
  var i = S.marks.indexOf(m);
  if (i >= 0) jump(i);
});

var params = new URLSearchParams(location.search);
var q0 = params.get("q");
if (q0) $("#find").value = q0;          // 搜索词可以随链接分享 / 刷新后保留

var pre = params.get("task");
if (pre){
  S.id = pre;
  poll();
} else {
  idle();
}
})();
</script>
</body></html>
"""


def html() -> str:
    """阅读页。纯静态，不依赖任何外部资源（离线/内网也能用）。"""
    return _HTML
