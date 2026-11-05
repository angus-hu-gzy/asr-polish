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
main{max-width:1240px;margin:0 auto;padding:22px 20px 120px}
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
/* 提示条与搜索条跟正文同宽：铺满 1240 的话，视线要横跨到播放器那边去 */
#notice,.tools,#fails{max-width:46em;margin-left:auto;margin-right:auto}

/* 重点列表：就放在搜索条下面，不用浮层。浮层会跟播放器抢地方，而这个页面
   本来就有两栏了。展开时把正文往下推一点，比盖住一行字好。 */
.marksbox{
  max-width:46em;margin:0 auto 22px;background:var(--panel);
  border:1px solid var(--line);border-radius:11px;padding:12px 14px 10px;
  box-shadow:var(--shadow);
}
.marksbox[hidden]{display:none}
.marksbox .mh{display:flex;align-items:center;gap:9px;margin-bottom:6px}
.marksbox h3{margin:0;font-size:13.5px;font-weight:600}
.marksbox .sp{flex:1}
.marksbox ul{list-style:none;margin:0;padding:0}
.marksbox li{display:grid;grid-template-columns:58px 1fr 20px;gap:10px;
             align-items:start;padding:7px 0;border-top:1px solid #efeadf}
.marksbox li:first-child{border-top:none}
.marksbox .mt{background:none;border:none;padding:0;text-align:right;cursor:pointer;
  font:11.5px/1.9 ui-monospace,Consolas,monospace;color:var(--faint);
  font-variant-numeric:tabular-nums}
.marksbox .mt:hover{color:var(--accent)}
.marksbox .mx{font-size:13.5px;line-height:1.7;color:#41474f;cursor:pointer;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.marksbox .mx:hover{color:var(--ink)}
.marksbox .md{background:none;border:none;padding:0;cursor:pointer;
  color:#c9c2b2;font-size:12.5px;line-height:1.9}
.marksbox .md:hover{color:var(--bad)}
.marksbox .empty{font-size:12.5px;color:var(--muted);padding:2px 0 4px;line-height:1.8}

/* 正文 */
.stage{display:flex;justify-content:center;align-items:flex-start;gap:26px}
.stage article{flex:0 1 46em;min-width:0}
.part + .part{margin-top:46px;padding-top:26px;border-top:1px dashed var(--line)}
.part{scroll-margin-top:84px}
.part h2{font-size:15px;font-weight:600;color:var(--muted);margin:0 0 18px;
         letter-spacing:.01em}
.cue{
  display:grid;grid-template-columns:56px 1fr 26px;column-gap:20px;
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
/* 可跳转的段落：整段可点。悬停才把时间戳染成主色，平时不打扰阅读。 */
.cue.can{cursor:pointer}
.cue.can:hover{background:#f5f1e6;box-shadow:0 0 0 5px #f5f1e6}
.cue.can:hover .t{color:var(--accent)}
.partnote{
  border:1px solid var(--line);background:#f7f4ec;border-radius:9px;
  padding:9px 12px;margin:0 0 20px;font-size:12.5px;line-height:1.75;color:var(--muted);
}
.partnote b{color:#6b6255;font-weight:600}
/* 没有时间戳的分 P：把左边那一列也收掉。留着一个空格子会让整篇正文右移
   半个缩进，看起来像是排版错了。 */
.part.notime .cue{grid-template-columns:1fr 26px}
.part.notime .cue .t{display:none}
.part.notime .cue .mk{grid-column:2}

/* 划重点：星标平时不出现（一段一段全是星星会很吵），悬停时才露出来；
   已经标过的常亮，这样扫一眼就知道哪些是重点。 */
.cue .mk{
  grid-column:3;justify-self:start;align-self:start;margin-top:.2em;
  border:none;background:none;padding:0 3px;font-size:14px;line-height:1;
  color:#c9c2b2;opacity:0;cursor:pointer;
  transition:opacity .15s,color .15s;
}
.cue:hover .mk{opacity:1}
.cue .mk:hover{color:var(--mark-cur)}
.cue.marked .mk{opacity:1;color:var(--mark-cur)}
.cue.marked{background:#fffdf3;box-shadow:inset 3px 0 0 var(--mark-cur)}
.cue.marked.can:hover{background:#fffbe8;
  box-shadow:inset 3px 0 0 var(--mark-cur),0 0 0 5px #fffbe8}
@media (hover:none){ .cue .mk{opacity:.45} }
mark{background:var(--mark);color:inherit;border-radius:2px;padding:0 1px}
mark.cur{background:var(--mark-cur);box-shadow:0 0 0 2px rgba(255,176,32,.35)}
.empty{padding:70px 0;text-align:center;color:var(--muted);font-size:14px;line-height:2}
.empty kbd{background:var(--panel);border:1px solid var(--line);border-bottom-width:2px;
           border-radius:5px;padding:1px 6px;font-size:12px;
           font-family:ui-monospace,Consolas,monospace}

/* 目录：只在多分 P 时出现。单分 P 不占地方——空着一栏会让人以为没加载出来。 */
.toc{flex:0 0 172px;position:sticky;top:74px;padding-top:2px}
.toc[hidden]{display:none}
.toc .th{color:var(--faint);font-size:11.5px;margin:0 0 8px 2px}
.toc ol{list-style:none;margin:0;padding:0;max-height:calc(100vh - 150px);overflow:auto}
.toc li{margin:0 0 2px}
.toc button{
  display:block;width:100%;text-align:left;background:none;border:none;cursor:pointer;
  border-left:2px solid transparent;border-radius:0 6px 6px 0;
  padding:5px 8px 6px 9px;color:var(--muted);font-size:12.5px;line-height:1.5;
}
.toc button:hover{background:#f4f1e9;color:var(--ink)}
.toc button.on{background:#f1ede2;border-left-color:var(--accent);color:var(--ink)}
.toc .n{color:var(--faint);font:11px/1.6 ui-monospace,Consolas,monospace;margin-right:5px;
        white-space:nowrap}
.toc button.on .n{color:var(--accent)}
.toc .tt{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.toc .c{display:block;color:var(--faint);font-size:11px;margin-top:1px}

/* 播放器：右侧一栏，滚到哪儿都在。不自动加载——只在真的点了句子才建 iframe，
   否则光是打开一篇文字稿就会去 B站拉一个播放器（慢、且没必要）。 */.player{
  flex:0 0 330px;position:sticky;top:74px;background:var(--panel);
  border:1px solid var(--line);border-radius:11px;overflow:hidden;
  box-shadow:var(--shadow);
}
.player[hidden]{display:none}
.player .ph{display:flex;align-items:center;gap:8px;padding:8px 10px;
            border-bottom:1px solid var(--line);background:#fcfbf7}
.player .pt{flex:1;min-width:0;font-size:12.5px;color:var(--muted);
            white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.player .plink{font-size:12px;white-space:nowrap}
/* 图标按钮（播放器收起、重点列表收起共用）。一开始它写在 .player 下面，
   结果重点列表那个 ✕ 拿不到样式，渲染成浏览器默认的凸起按钮。 */
.pbtn{border:none;background:none;color:var(--faint);font-size:13px;
      line-height:1;padding:3px 6px;border-radius:5px;cursor:pointer}
.pbtn:hover{background:#f1ede3;color:var(--ink)}
.player .pbox{position:relative;width:100%;aspect-ratio:16/9;background:#000}
.player iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
.player .pf{padding:7px 10px;font-size:12px;color:var(--muted);
            font-variant-numeric:tabular-nums}

/* 失败明细 */
.fails{margin:26px 0 0;border-top:1px solid var(--line);padding-top:18px}
.fails h3{font-size:13.5px;font-weight:600;margin:0 0 10px;color:var(--bad)}
.fails ul{list-style:none;margin:0 0 14px;padding:0}
.fails li{display:grid;grid-template-columns:150px 1fr;gap:14px;
          padding:8px 0;border-bottom:1px solid #f0ece1;font-size:13px}
.fails li:last-child{border-bottom:none}
.fails .bv{font-family:ui-monospace,Consolas,monospace;color:var(--muted);font-size:12.5px}
.fails .why{color:#4b5563;word-break:break-word}

/* 窄屏：一栏往下堆。播放器放到正文上面——它本来就是为了「边看边读」，
   压在正文底下的话，点了句子还得往下翻才能看见。 */
@media (max-width:1180px){
  .stage{flex-direction:column;align-items:stretch}
  .stage article{flex:1 1 auto;max-width:46em}
  .player{position:static;flex:0 0 auto;width:100%;max-width:420px;order:-1}
  /* 目录改成标题下面的一排横向胶囊，不另占一栏 */
  .toc{position:static;flex:0 0 auto;width:100%;order:-2}
  .toc .th{display:none}
  .toc ol{display:flex;gap:6px;overflow-x:auto;max-height:none;padding-bottom:2px}
  .toc li{flex:0 0 auto;margin:0}
  .toc button{border-left:none;border-bottom:2px solid transparent;border-radius:7px;
              white-space:nowrap;background:#f7f4ec;padding:5px 10px}
  .toc button.on{border-bottom-color:var(--accent)}
  .toc .tt{-webkit-line-clamp:1;max-width:13em}
}
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
    <button class="btn sm" id="mkbtn" type="button" title="看标过的重点">重点 <span id="mkn">0</span></button>
    <span class="count" id="count"></span>
    <span class="stat" id="stat"></span>
  </div>

  <div class="marksbox" id="marksbox" hidden>
    <div class="mh">
      <h3 id="mkh">重点</h3>
      <span class="sp"></span>
      <button class="btn sm" id="export" type="button" disabled
              title="把这几条导出成 Markdown">导出 md</button>
      <button class="pbtn" id="mkclose" type="button" title="收起列表">✕</button>
    </div>
    <ul id="mklist"></ul>
    <div class="empty" id="mkempty" hidden>
      还没有标重点。鼠标移到任意一段上，点右边的 ☆ 就标上了；再点一次取消。
    </div>
  </div>

  <div class="stage">
    <aside class="toc" id="toc" hidden>
      <div class="th" id="toch">目录</div>
      <ol id="toclist"></ol>
    </aside>

    <article id="doc"></article>

    <aside class="player" id="player" hidden>
      <div class="ph">
        <span class="pt" id="ptitle">视频</span>
        <a class="plink" id="popen" href="#" target="_blank" rel="noopener">在 B站打开</a>
        <button class="pbtn" id="pclose" type="button" title="收起播放器">✕</button>
      </div>
      <div class="pbox">
        <iframe id="pframe" title="B站播放器" allow="autoplay; fullscreen"
                allowfullscreen scrolling="no" frameborder="0"></iframe>
      </div>
      <div class="pf" id="pfoot"></div>
    </aside>
  </div>

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
          text:"", parts:0, task:null, mk:[], lsBroken:false, part:0, anyJump:false };

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
  $("#marksbox").hidden = true;        // 换任务了，重点列表先收起来
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

/* ── 定位视频：BV 号与分 P ───────────────────────── */
/* 两个值都从产物文件名里取，形如 `BV1xx-P2-标题.srt`（多 P）或 `BV1xx-标题.srt`
   （单 P）——这是 export.base_name 定的规则。文件名里没有 BV 号时（`--only` 重跑、
   或者手工放进输出目录的文件）退回任务 payload.target 里的那个。
   **没有为这件事改 transcript 接口**：它返回的 name 本来就带着这两个信息。 */
var BV_RE = /^(BV[0-9A-Za-z]{8,14})(?:-P(\d+))?-/;

function bvFromTarget(t){
  var m = /(BV[0-9A-Za-z]{8,14})/i.exec(t || "");
  return m ? m[1] : "";
}

function pageFromTarget(t){
  var m = /[?&]p=(\d+)/i.exec(t || "");
  return m ? parseInt(m[1], 10) : 1;
}

function partMeta(name, task){
  var m = BV_RE.exec(name || "");
  var target = (task && task.payload && task.payload.target) || "";
  return {
    bvid: m ? m[1] : bvFromTarget(target),
    page: (m && m[2]) ? parseInt(m[2], 10) : pageFromTarget(target),
    title: (name || "").replace(/\.(srt|json|txt)$/i, "").replace(BV_RE, "")
  };
}

function videoTitleOf(task, bvid){
  var items = (task && task.result && task.result.items) || [];
  for (var i = 0; i < items.length; i++){
    if (items[i].bvid === bvid && items[i].title) return items[i].title;
  }
  return "";
}

/* 有没有「逐句」时间。必剪 ASR 那条路只有一块 0 → 99:59:59 的占位时间，
   transcript 会把它按行拆开并把 start 置 null；纯文本导出同理。
   这种分 P 不能跳——t=0 会把人带到视频开头，看起来就像跳错了。 */
function hasRealTime(part){
  for (var i = 0; i < part.segments.length; i++){
    if (part.segments[i].start != null) return true;
  }
  return false;
}

/* ── 播放器：点句子跳过去 ───────────────────────── */
/* B站官方外链播放器（player.bilibili.com/player.html）的官方参数里，跟这件事
   有关的只有三个：`bvid`（视频号）、`p`（第几集，从 1 开始）、`t`（跳转到媒体的
   初始时间点，单位秒）。官方**没有**提供 postMessage / seek 之类的接口，
   所以「跳到第 3 分 20 秒」只能重设 src，让它带着 t 重新加载。

   这也是这里不做「同一个播放器内平滑 seek」的原因：非官方的方式一旦对方改
   行为就会静默失效，而读者会以为是自己点错了。 */
function playerUrl(meta, sec){
  var q = ["bvid=" + encodeURIComponent(meta.bvid),
           "p=" + meta.page,
           "page=" + meta.page,        // 官方写 p，社区文档写 page，两个都带上
           "t=" + Math.max(0, Math.floor(sec || 0)),
           "autoplay=1", "danmaku=0", "high_quality=1"];
  return "https://player.bilibili.com/player.html?" + q.join("&");
}

function showPlayer(meta, sec){
  $("#pframe").src = playerUrl(meta, sec);
  $("#ptitle").textContent = meta.label;
  $("#popen").href = "https://www.bilibili.com/video/" + meta.bvid + "/?p=" + meta.page;
  $("#pfoot").textContent = "从 " + fmt(sec) + " 开始播 · 点别的句子可以再跳";
  $("#player").hidden = false;
}

function closePlayer(){
  $("#player").hidden = true;
  // 只藏起来的话声音还在放，所以把 src 换成空白页（顺带停掉网络请求）
  $("#pframe").src = "about:blank";
}

$("#pclose").addEventListener("click", closePlayer);

function partNote(part, pm){
  var box = el("div", "partnote");
  var why;
  if (!pm.bvid){
    why = "这一篇定位不到视频：产物文件名里没有 BV 号（`--only` 重跑或手工放进"
        + "输出目录的文件会这样），所以点句子不跳转。读和搜索都照常。";
  } else if (part.source === "txt"){
    why = "这一篇来自纯文本导出，没有时间戳，所以点句子不能跳转。"
        + "想要跳转的话，任务提交时 formats 带上 srt。";
  } else {
    why = "这一篇没有逐句时间戳：必剪 ASR 只给了 0 → 99:59:59 的一整块占位时间，"
        + "整篇是按行拆开的。跳转会落到视频开头，看着像跳错，所以这里不给跳。";
  }
  box.appendChild(el("b", null, "不能跳到视频"));
  box.appendChild(document.createTextNode(" · " + why));
  return box;
}

/* ── 目录：多分 P 才有 ──────────────────────────── */
/* 只消费 transcript 已经返回的 parts：标题从产物文件名里来，段数是渲染完
   数出来的。为此没有动后端——多分 P 的结构本来就在返回值里。 */
function partSections(){ return document.querySelectorAll("#doc .part") }

function selectPart(i, scroll){
  var secs = partSections();
  if (!secs.length) return;
  i = Math.max(0, Math.min(secs.length - 1, i));
  for (var k = 0; k < secs.length; k++) secs[k].hidden = (k !== i);
  var btns = document.querySelectorAll("#toc button");
  for (var j = 0; j < btns.length; j++) btns[j].classList.toggle("on", j === i);
  S.part = i;
  if (scroll && secs[i].scrollIntoView){
    secs[i].scrollIntoView({ block: "start", behavior: "smooth" });
  }
  updateStat();
}

/* 统计只算当前这一篇。多分 P 时若把各篇加起来，屏幕上摆着 8 段，
   右上角却写「182 段」，看的人会以为正文没加载全。 */
function updateStat(){
  var secs = partSections();
  var sec = secs[S.part] || secs[0];
  if (!sec) return;
  $("#stat").textContent = (sec._chars || 0).toLocaleString() + " 字 · "
    + sec.querySelectorAll(".cue").length + " 段"
    + (S.anyJump ? " · 点句子跳到视频" : "");
}

/* 点重点列表里的条目时，那一段可能在别的分 P 里（被 hidden 掉了）。 */
function revealPartOf(cue){
  var sec = cue && cue.closest ? cue.closest(".part") : null;
  if (!sec || !sec.hidden) return;
  var secs = partSections();
  for (var i = 0; i < secs.length; i++){
    if (secs[i] === sec){ selectPart(i, false); return }
  }
}

function buildToc(infos){
  clear($("#toclist"));
  if (infos.length < 2){          // 单分 P：不显示目录，也不留一栏空白
    $("#toc").hidden = true;
    return;
  }
  // 同一个视频的多个分 P 标「P2」，多条视频（up: / search:）标「第 2 篇」——
  // 后者每个文件都是 P1，写 P1 只会让人困惑。
  var sameBv = true;
  for (var i = 1; i < infos.length; i++){
    if (!infos[i].pm.bvid || infos[i].pm.bvid !== infos[0].pm.bvid) sameBv = false;
  }
  var ol = $("#toclist");
  infos.forEach(function(x, i){
    var btn = el("button");
    btn.type = "button";
    btn.dataset.i = i;
    btn.appendChild(el("span", "n", sameBv ? ("P" + x.pm.page) : ("第 " + (i + 1) + " 篇")));
    btn.appendChild(el("span", "tt", x.pm.title || x.pm.bvid || ("第 " + (i + 1) + " 篇")));
    btn.appendChild(el("span", "c", x.sec.querySelectorAll(".cue").length + " 段"));
    btn.addEventListener("click", function(ev){
      selectPart(parseInt(ev.currentTarget.dataset.i, 10), true);
    });
    var li = el("li");
    li.appendChild(btn);
    ol.appendChild(li);
  });
  $("#toch").textContent = "目录 · " + infos.length + " 篇";
  $("#toc").hidden = false;
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
  S.parts = d.parts.length; S.task = task; S.anyJump = false;
  var infos = [];
  d.parts.forEach(function(part, i){
    var sec = el("section", "part");
    var pm = partMeta(part.name, task);
    var jump = !!pm.bvid && hasRealTime(part);
    pm.label = (videoTitleOf(task, pm.bvid) || pm.title || pm.bvid)
             + (d.parts.length > 1 ? " · P" + pm.page : "");
    if (d.parts.length > 1){
      // 标题用文件名里去掉 BV 号和扩展名的那段。原来直接把整个文件名摆出来，
      // 左边目录又写着 P1，一句话里三遍同样的信息。
      var h = el("h2", null, "第 " + (i+1) + " 篇 · " + (pm.title || part.name));
      sec.appendChild(h);
    }
    if (!jump){
      sec.className = "part notime";
      sec.appendChild(partNote(part, pm));
    }
    sec._chars = 0;
    groupCues(part.segments).forEach(function(p){
      var cue = el("div", jump && p.start != null ? "cue can" : "cue");
      if (p.start != null){
        cue.dataset.t = p.start;
        cue.title = fmt(p.start) + (p.end != null ? " → " + fmt(p.end) : "")
                  + (jump ? "（点一下跳到视频这里）" : "");
        cue.appendChild(el("span", "t", fmt(p.start)));
      } else {
        cue.appendChild(el("span", "t", ""));
      }
      var txt = el("p", "txt", p.text);
      cue.appendChild(txt);
      cue._text = p.text;
      cue._start = p.start;
      cue._meta = pm;
      cue._jump = jump;
      cue.dataset.key = mkKey(part.name, p.start, p.text);
      cue._part = i;
      cue._partName = part.name;
      cue._page = pm.page;
      cue._label = pm.label;
      // 星标用 currentTarget.parentNode 找自己那一句，不闭包 cue——
      // 这个循环里 cue 是 var，闭包会全都指向最后一段。
      var mk = el("button", "mk", "☆");
      mk.type = "button";
      mk.title = "标为重点";
      mk.addEventListener("click", function(ev){
        ev.stopPropagation();          // 否则同时触发「跳到视频」
        toggleMark(ev.currentTarget.parentNode, ev.currentTarget);
      });
      cue.appendChild(mk);
      sec.appendChild(cue);
      sec._chars += p.text.length;
    });
    if (jump) S.anyJump = true;
    doc.appendChild(sec);
    infos.push({ pm: pm, sec: sec });
  });

  buildToc(infos);
  selectPart(0, false);          // 多分 P 默认只显示第一篇，避免一屏几百段

  // 标签页标题跟着视频走：开着好几个任务时，靠「字幕阅读」四个字分不出谁是谁。
  var firstBv = infos.length ? infos[0].pm.bvid : "";
  var heading = videoTitleOf(task, firstBv) || (infos.length ? infos[0].pm.title : "");
  if (heading) document.title = heading + " · asr-polish";

  $("#stat").textContent = "";
  updateStat();
  $("#tools").hidden = false;
  renderFailures(r, task);
  restoreMarks();
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

/* ── 划重点 ──────────────────────────────────────── */
/* 存 localStorage，按任务 id 分组：换个任务不会串。取舍很清楚——不做账号，
   就换不来跨设备同步；清掉浏览器数据就没了。这一版就要这个。
   键里带 partName + 起始秒 + 正文：既能在刷新后把星标贴回原来那一段，
   也不会因为某段文字重复而被误认。 */
var MK_PREFIX = "asr-polish:marks:v1:";

function mkKey(partName, start, text){
  return partName + "|" + (start == null ? "x" : start) + "|" + text;
}

function loadMarks(){
  try {
    var raw = localStorage.getItem(MK_PREFIX + S.id);
    var list = raw ? JSON.parse(raw) : [];
    return Object.prototype.toString.call(list) === "[object Array]" ? list : [];
  } catch(e){ return [] }
}

function saveMarks(list){
  try {
    localStorage.setItem(MK_PREFIX + S.id, JSON.stringify(list));
    return true;
  } catch(e){
    // 隐私模式 / 禁用站点数据时会抛。能做的只有说实话：这一页还能用，刷新就没。
    if (!S.lsBroken){
      S.lsBroken = true;
      notice("warn", "重点存不进浏览器",
        "这个浏览器不允许页面写 localStorage（隐私模式，或禁用了站点数据）。"
        + "重点在这一页里照常能用，但刷新或关掉标签页就没了。");
    }
    return false;
  }
}

function cueByKey(key){
  var cues = document.querySelectorAll("#doc .cue");
  for (var i = 0; i < cues.length; i++){
    if (cues[i].dataset.key === key) return cues[i];
  }
  return null;
}

function focusCue(cue){
  if (!cue) return false;
  revealPartOf(cue);            // 在别的分 P 里的话，先切过去
  if (cue.scrollIntoView) cue.scrollIntoView({ block: "center", behavior: "smooth" });
  cue.classList.add("flash");
  setTimeout(function(){ cue.classList.remove("flash"); }, 1400);
  return true;
}

function toggleMark(cue, btn){
  if (!cue.dataset.key) return;
  var on = !cue.classList.contains("marked");
  var list = loadMarks().filter(function(m){ return m.key !== cue.dataset.key });
  if (on){
    list.push({
      key: cue.dataset.key,
      t: cue._start == null ? null : cue._start,
      text: cue._text,
      part: cue._part,
      partName: cue._partName,
      page: cue._page,
      bvid: cue._meta ? cue._meta.bvid : "",
      label: cue._label
    });
  }
  cue.classList.toggle("marked", on);
  if (btn){
    btn.title = on ? "取消重点" : "标为重点";
    btn.textContent = on ? "★" : "☆";
  }
  saveMarks(list);
  renderMarks(list);
}

function removeMark(key){
  var list = loadMarks().filter(function(m){ return m.key !== key });
  saveMarks(list);
  var cue = cueByKey(key);
  if (cue){
    cue.classList.remove("marked");
    var btn = cue.querySelector(".mk");
    if (btn){ btn.textContent = "☆"; btn.title = "标为重点"; }
  }
  renderMarks(list);
}

function renderMarks(list){
  list = list || loadMarks();
  S.mk = list;
  $("#mkn").textContent = list.length;
  $("#mkh").textContent = "重点 " + list.length + " 条";
  $("#mkempty").hidden = list.length > 0;
  $("#export").disabled = !list.length;
  var ul = $("#mklist");
  clear(ul);
  list.forEach(function(m){
    var li = el("li");
    var t = el("button", "mt", m.t == null ? "无时间" : fmt(m.t));
    t.title = m.t == null ? "这一段没有时间戳" : ("跳到 " + fmt(m.t));
    t.addEventListener("click", function(){ jumpToKey(m.key) });
    var txt = el("span", "mx", m.text);
    txt.title = "跳到这一段";
    txt.addEventListener("click", function(){ jumpToKey(m.key) });
    var del = el("button", "md", "✕");
    del.title = "取消这条重点";
    del.addEventListener("click", function(){ removeMark(m.key) });
    li.appendChild(t);
    li.appendChild(txt);
    li.appendChild(del);
    ul.appendChild(li);
  });
}

function jumpToKey(key){
  if (focusCue(cueByKey(key))) return;
  notice("warn", "找不到这一段了",
    "重点记录还在，但这篇文字稿里已经没有对应的段落——多半是重新抓过一次，"
    + "字幕分段变了。可以直接删掉这条。");
}

function restoreMarks(){
  var list = loadMarks();
  list.forEach(function(m){
    var cue = cueByKey(m.key);
    if (!cue) return;
    cue.classList.add("marked");
    var btn = cue.querySelector(".mk");
    if (btn){ btn.textContent = "★"; btn.title = "取消重点"; }
  });
  renderMarks(list);
}

/* ── 导出 Markdown ──────────────────────────────── */
/* 纯字符串拼装、不碰 DOM，所以能直接测（见文件末尾的 window.__reader）。
   每条带上时间戳链接：导出去的东西才不只是「一堆没有出处的句子」。 */
function mdStamp(now){
  var d = now || new Date();
  function p(n){ return n < 10 ? "0" + n : "" + n }
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate())
       + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

function markScope(task, list){
  var bvids = [];
  list.forEach(function(m){
    if (m.bvid && bvids.indexOf(m.bvid) < 0) bvids.push(m.bvid);
  });
  var single = bvids.length === 1 ? bvids[0] : "";
  return { bvids: bvids, single: single,
           title: single ? videoTitleOf(task, single) : "" };
}

function markLink(m){
  if (m.t == null || !m.bvid) return "";
  return "https://www.bilibili.com/video/" + m.bvid + "/?p=" + (m.page || 1)
       + "&t=" + Math.floor(m.t);
}

function mdFileName(title, bvid){
  // 标题里什么字符都可能有（`/ : < >` 在 Windows 上是非法文件名），统一换成短横，
  // 再把连续的短横压成一个——不然「从零: 入门」会变成「从零--入门」。
  var base = String(title || bvid || "重点")
    .replace(/[\\/:*?"<>|]+/g, "-").replace(/\s+/g, "-")
    .replace(/-+/g, "-").slice(0, 60).replace(/^-+|-+$/g, "");
  return (base || "重点") + "-重点.md";
}

function buildMarkdown(list, task, now){
  list = list || loadMarks();
  task = task || S.task || {};
  var scope = markScope(task, list);
  var out = [];
  out.push("# " + (scope.title || scope.single || "重点摘录"));
  out.push("");
  if (scope.single){
    out.push("- 视频：" + (scope.title ? scope.title + "（" + scope.single + "）" : scope.single));
  } else if (scope.bvids.length){
    // 跨多个视频的重点（up: / search: 任务）：标题没法用一个，就把 BV 号都列出来
    out.push("- 视频：" + scope.bvids.length + " 条 · " + scope.bvids.join("、"));
  }
  out.push("- 任务：" + (S.id || "—"));
  out.push("- 导出：" + mdStamp(now));
  out.push("- 重点：" + list.length + " 条");
  out.push("");
  out.push("---");
  out.push("");
  list.forEach(function(m, i){
    var head = (i + 1) + ". " + (m.t == null ? "〔无时间戳〕" : "**" + fmt(m.t) + "**");
    head += " · P" + (m.page || 1);
    var link = markLink(m);
    if (link) head += " · [在视频里看](" + link + ")";
    out.push(head);
    out.push("   " + String(m.text || "").replace(/\s*\n\s*/g, " "));
    out.push("");
  });
  return out.join("\n").replace(/\n+$/, "\n");
}

function exportMarks(){
  var list = loadMarks();
  if (!list.length) return;                 // 没重点就不导出空文件
  var md = buildMarkdown(list, S.task);
  var scope = markScope(S.task, list);
  var blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
  var url = URL.createObjectURL(blob);
  var a = document.createElement("a");
  a.href = url;
  a.download = mdFileName(scope.title, scope.single);
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(function(){ URL.revokeObjectURL(url); }, 1500);
  var btn = $("#export"), old = btn.textContent;
  btn.textContent = "已导出";
  setTimeout(function(){ btn.textContent = old; }, 1600);
}

$("#export").addEventListener("click", exportMarks);

$("#mkbtn").addEventListener("click", function(){
  var box = $("#marksbox");
  box.hidden = !box.hidden;
  if (!box.hidden) renderMarks();
});
$("#mkclose").addEventListener("click", function(){ $("#marksbox").hidden = true; });

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
  // 只在当前显示的分 P 里搜：藏起来的段落既看不见也滚不到，算进计数只会
  // 让人以为「搜到了却跳不过去」。
  var cues = document.querySelectorAll("#doc .part:not([hidden]) .cue");
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
  focusCue(m.closest(".cue"));
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

/* 页面里的点击有两种：点高亮词 = 定位到那一处搜索结果；点句子 = 把播放器跳过去。
   先判高亮词，因为它在句子里，两个都想接的话就会一次干两件事。 */
$("#doc").addEventListener("click", function(e){
  var m = e.target.closest ? e.target.closest("mark.hit") : null;
  if (m){
    var i = S.marks.indexOf(m);
    if (i >= 0) jump(i);
    return;
  }
  var cue = e.target.closest ? e.target.closest(".cue.can") : null;
  if (cue && cue._jump && cue._start != null) showPlayer(cue._meta, cue._start);
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

/* 测试钩子。这几个是纯逻辑——不读 DOM、不发请求——所以能在 node 里带着夹具
   直接跑（tests/test_reader_ui.py）。留在页面上没有副作用，比让测试去
   匹配字符串靠得住。 */
window.__reader = {
  groupCues: groupCues,
  partMeta: partMeta,
  hasRealTime: hasRealTime,
  playerUrl: playerUrl,
  markScope: markScope,
  buildMarkdown: buildMarkdown,
  mdFileName: mdFileName,
  markLink: markLink,
  mkKey: mkKey,
  fmt: fmt
};
})();
</script>
</body></html>
"""


def html() -> str:
    """阅读页。纯静态，不依赖任何外部资源（离线/内网也能用）。"""
    return _HTML
