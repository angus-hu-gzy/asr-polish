# -*- coding: utf-8 -*-
"""笔记库页面：搜得到、点得回去的那一页。

为什么是服务端生成的整块 HTML
----------------------------
和阅读页同一个理由：不引框架、不加构建，打开就能用。页面自己只做两件事——
取数据、画列表，逻辑都在 `window.__notes` 上挂着，node 里灌进 DOM 桩就能测。

为什么不内联数据
----------------
数据走 `/api/notes`，不塞进 HTML。内联的话「首次渲染」和「搜索」是两条数据
来源，前者来自页面快照、后者来自接口，搜出来的和刚看到的东西对不上。

只画固定的几种结构
------------------
标题、列表、`[[链接]]`、出处锚点。不为它写通用 Markdown 渲染器：笔记是我们
自己写的，结构固定；写一个渲染器要处理标题层级、代码块、表格、转义，每一处
都能出错，而收益只是「万一用户手改了一行」。
"""
from __future__ import annotations

#: 页面是一整块字符串（原始字符串：里面全是 JS 的 `\n` 和正则的 `\s`）。
_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>笔记库 · asr-polish</title>
<style>
:root{
  --bg:#fbfaf7;--panel:#fff;--text:#2b2b2b;--muted:#6b6b6b;--faint:#9a9a9a;
  --line:#e6e2da;--line-soft:#efece6;--field:#f4f1ec;--accent:#2f6feb;
  --ok:#1f7a4d;--warn:#9a6a00;--warnbg:#fdf6e3;--bad:#b3261e;
}
@media (prefers-color-scheme:dark){
  :root{--bg:#16181c;--panel:#1c1f24;--text:#dcdcdc;--muted:#9aa0a6;
  --faint:#7d838a;--line:#2b2f36;--line-soft:#24282e;--field:#23272d;
  --accent:#7aa7ff;--ok:#6fc48f;--warn:#e0b34a;--warnbg:#2c2717;--bad:#f2827a}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.75 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:26px 20px 70px}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin:0 0 4px}
h1{font-size:19px;margin:0;font-weight:650}
.sub{color:var(--faint);font-size:12.5px}
a{color:var(--accent);text-decoration:none;border-bottom:1px solid transparent}
a:hover{border-bottom-color:currentColor}
.sp{flex:1}
.search{display:flex;gap:8px;margin:18px 0 10px}
.search input{flex:1;background:var(--panel);border:1px solid var(--line);
  border-radius:9px;padding:9px 12px;font:inherit;font-size:14px;color:var(--text)}
.search input:focus{outline:none;border-color:var(--accent)}
.hint{color:var(--faint);font-size:12.5px;margin:0 0 20px}
section{margin:0 0 26px}
h2{font-size:13px;letter-spacing:.04em;color:var(--muted);font-weight:650;
  margin:0 0 10px;text-transform:none}
.card{background:var(--panel);border:1px solid var(--line);border-radius:11px;
  padding:12px 14px;margin:0 0 9px}
.card .top{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.card .t{font-weight:600;font-size:14.5px}
.card .meta{color:var(--faint);font-size:12px}
.card .why{color:var(--muted);font-size:12px;margin-top:3px}
.stale{display:inline-block;padding:1px 7px;border-radius:8px;font-size:11.5px;
  background:var(--warnbg);color:var(--warn);border:1px solid var(--line)}
.occ{margin:7px 0 0;padding:0;list-style:none;font-size:13px}
.occ li{color:var(--muted);padding:2px 0}
.occ a.t{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.card h3{font-size:12px;letter-spacing:.04em;color:var(--muted);font-weight:650;
  margin:13px 0 5px}
.card ul.items{margin:0;padding:0;list-style:none;font-size:13.5px}
.card ul.items li{padding:2.5px 0}
.card ul.items a.t{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.card p{margin:6px 0;font-size:13.5px}
.card p.note{color:var(--warn);background:var(--warnbg);border-radius:7px;
  padding:6px 9px;font-size:12.5px;margin:2px 0 8px}
.def{font-size:13.5px;color:var(--text);margin:5px 0 0}
.empty{color:var(--faint);font-size:13.5px;padding:14px 2px}
.err{color:var(--bad);font-size:13.5px;padding:12px 0}
.wikilink{color:var(--text);font-size:15px;font-weight:600}
@media (max-width:620px){.wrap{padding:20px 14px 60px}}
</style>
</head>
<body><div class="wrap">
<header>
  <h1>笔记库</h1>
  <span class="sub" id="count"></span>
  <span class="sp"></span>
  <a href="/">任务面板</a>
  <a href="/reader">阅读页</a>
</header>

<div class="search">
  <input id="q" type="text" spellcheck="false"
         placeholder="搜笔记：标题、概念名、正文都算…">
</div>
<p class="hint" id="hint">不搜的时候按日期倒序列出全部；只搜笔记，不搜整篇字幕。</p>

<section id="vsec">
  <h2>视频笔记</h2>
  <div id="videos"></div>
</section>

<section id="csec">
  <h2>概念笔记</h2>
  <div id="concepts"></div>
</section>
</div>

<script>
(function(){
"use strict";
var $ = function(s){ return document.querySelector(s) };

function esc(s){
  return String(s == null ? "" : s).replace(/[&<>"]/g, function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];
  });
}

/* 一条出处 → 阅读页的地址。参数名和阅读页认的那三个一致：
   task / part / t。少一个都会落到「打开整篇、从头开始」。 */
function readerHref(o){
  if (!o) return "";
  var q = "task=" + encodeURIComponent(o.task_id || "");
  if (o.part_index != null) q += "&part=" + encodeURIComponent(o.part_index);
  if (o.sec != null) q += "&t=" + encodeURIComponent(o.sec);
  return "/reader?" + q;
}

function noteHref(v){
  var q = "task=" + encodeURIComponent(v.task_id || "");
  if (v.part_index != null) q += "&part=" + encodeURIComponent(v.part_index);
  return "/reader?" + q;
}

/* 一行里的三种东西：`[[概念]]`、`[12:41](链接)`、其余是字。
   先转 wikilink 再转普通链接——顺序反了的话 `[[阈值]] — 定义 [00:00](url)`
   会被普通链接那条正则从第一个方括号一路吃到 `](`，标题变成
   「[阈值]] — 定义 [00:00」。

   `[[文件|显示名]]` 是 Obsidian 的别名写法：文件名带别名时（概念名里的
   `/` `:` 落盘会变成 `_`，`TCP/IP` 存成 `TCP_IP.md`）链接指文件、显示指
   概念名。搜的时候用**显示名**——搜索按概念名匹配，拿文件名去搜是搜不到的。 */
function inline(text){
  var s = esc(text);
  s = s.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, function(_, target, label){
    var shown = label || target;
    return '<a href="/notes?q=' + encodeURIComponent(shown) + '">' + shown + '</a>';
  });
  s = s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, function(_, label, url){
    return '<a class="t" href="' + url + '" title="回到原文这一段">'
         + label + '</a>';
  });
  return s;
}

/* 只认生成器写的那几种结构：`## 小标题`、`- 列表项`、`> 说明`。
   不写通用 Markdown 渲染器：标题层级、代码块、表格、转义每一处都能出错，
   而这些笔记的结构本来就是固定的。认不出来的行按普通段落处理，不丢。 */
function renderBody(body){
  var lines = String(body || "").split("\n");
  var out = [], list = false;
  function closeList(){ if (list){ out.push("</ul>"); list = false } }
  for (var i = 0; i < lines.length; i++){
    var s = lines[i].replace(/\s+$/, "");
    if (!s || s === "---"){ closeList(); continue }
    if (s.slice(0, 3) === "## "){
      closeList();
      out.push('<h3>' + esc(s.slice(3)) + '</h3>');
      continue;
    }
    if (s.charAt(0) === "#"){ closeList(); continue }   /* 大标题：卡片顶上已经有了 */
    if (s.slice(0, 2) === "- "){
      if (!list){ out.push('<ul class="items">'); list = true }
      out.push('<li>' + inline(s.slice(2)) + '</li>');
      continue;
    }
    closeList();
    if (s.charAt(0) === ">")
      out.push('<p class="note">' + inline(s.replace(/^>\s*/, "")) + '</p>');
    else
      out.push('<p>' + inline(s) + '</p>');
  }
  closeList();
  return out.join("");
}

/* 视频卡片的一行副标题。只放真实存在的字段——空字段留一个「·」比没有更碍眼。 */
function metaBits(v){
  var bits = [];
  if (v.bvid) bits.push(v.bvid);
  if (v.label) bits.push(v.label);
  if (v.date) bits.push(v.date);
  return bits.join(" · ");
}

function staleBadge(v){
  if (!v || !v.stale) return "";
  return '<span class="stale" title="' + esc(v.stale_why || "") + '">'
       + '来源已更新，建议重新生成</span>';
}

function renderVideos(list, q){
  if (!list || !list.length)
    return '<div class="empty">' + (q ? "没有命中这个词的视频笔记。" 
      : "还没有视频笔记。在阅读页顶栏点「笔记」，再点「存入笔记库」。") + '</div>';
  return list.map(function(v){
    var why = (v.hits && v.hits.length)
      ? '<div class="why">命中：' + esc(v.hits.join("、")) + '</div>' : "";
    var href = v.task_id ? noteHref(v) : "";
    var title = href
      ? '<a class="t" href="' + esc(href) + '">' + esc(v.title) + '</a>'
      : '<span class="t">' + esc(v.title) + '</span>';
    return '<div class="card"><div class="top">' + title
      + ' <span class="meta">' + esc(metaBits(v)) + '</span> '
      + staleBadge(v) + '</div>' + why
      + renderBody(v.body) + '</div>';
  }).join("");
}

function renderConcepts(list, q){
  if (!list || !list.length)
    return '<div class="empty">' + (q ? "没有命中这个词的概念笔记。"
      : "还没有概念笔记。概念是跟着视频笔记一起写进来的。") + '</div>';
  return list.map(function(c){
    var occ = (c.occ || []).map(function(o){
      var href = o.task_id ? readerHref(o) : "";
      var where = esc(o.title || "（未知视频）") + (o.label ? " · " + esc(o.label) : "");
      var when = o.sec == null ? "" :
        ' · <a class="t" href="' + esc(href) + '" title="回到原文这一段">'
        + esc(clockLabel(o.sec)) + '</a>';
      return '<li>' + where + when + '</li>';
    }).join("");
    var why = (c.hits && c.hits.length)
      ? '<div class="why">命中：' + esc(c.hits.join("、")) + '</div>' : "";
    return '<div class="card"><div class="top">'
      + '<a class="wikilink" href="/notes?q=' + encodeURIComponent(c.name) + '">'
      + esc(c.name) + '</a>'
      + '<span class="meta">出现 ' + (c.occ || []).length + ' 次'
      + (c.first ? " · 首次 " + esc(c.first) : "") + '</span></div>'
      + (c.define ? '<div class="def">' + esc(c.define) + '</div>' : "")
      + why
      + (occ ? '<ul class="occ">' + occ + '</ul>' : "")
      + '</div>';
  }).join("");
}

/* 秒 → mm:ss / h:mm:ss。和阅读页、笔记文件里长得一样，三处不能有两种写法。 */
function clockLabel(sec){
  var s = Math.max(0, Math.round(Number(sec) || 0));
  var p = function(n){ return n < 10 ? "0" + n : "" + n };
  if (s >= 3600) return Math.floor(s/3600) + ":" + p(Math.floor(s%3600/60)) + ":" + p(s%60);
  return p(Math.floor(s/60)) + ":" + p(s%60);
}

/* 把一次响应画到页面上。拆出来是为了 node 里能直接测「零条 / 有命中 / 有过期」
   三种情况渲染出来的东西对不对，而不必真的开浏览器。 */
function paint(data, q){
  var videos = (data && data.videos) || [];
  var concepts = (data && data.concepts) || [];
  $("#videos").innerHTML = renderVideos(videos, q);
  $("#concepts").innerHTML = renderConcepts(concepts, q);
  var count = $("#count");
  if (count){
    count.textContent = q
      ? ("搜「" + q + "」：视频 " + videos.length + " 篇 / 概念 " + concepts.length + " 个")
      : ("视频 " + videos.length + " 篇 / 概念 " + concepts.length + " 个");
  }
  var stale = videos.filter(function(v){ return v.stale }).length;
  var hint = $("#hint");
  if (hint && stale && !q){
    hint.textContent = "有 " + stale + " 篇笔记的来源字幕变过了（标了黄的），"
      + "那些出处可能已经对不上——回阅读页重新存一次就会刷新。";
  }
}

async function getJSON(url){
  var r = await fetch(url);
  var d = {};
  try { d = await r.json() } catch(e){}
  if (!r.ok){
    throw new Error(d.error || ("HTTP " + r.status));
  }
  return d;
}

async function loadAll(){
  try { paint(await getJSON("/api/notes"), ""); }
  catch(e){ showError("读不到笔记库：" + (e.message || e)); }
}

async function doSearch(q){
  if (!q){ loadAll(); return }
  try { paint(await getJSON("/api/notes/search?q=" + encodeURIComponent(q)), q); }
  catch(e){ showError("搜不了：" + (e.message || e)); }
}

function showError(msg){
  $("#videos").innerHTML = '<div class="err">' + esc(msg) + '</div>';
  $("#concepts").innerHTML = "";
}

var timer = null;
$("#q").addEventListener("input", function(e){
  var v = e.target.value.trim();
  if (timer) clearTimeout(timer);
  timer = setTimeout(function(){ doSearch(v) }, 200);
});
$("#q").addEventListener("keydown", function(e){
  if (e.key === "Escape"){ e.target.value = ""; doSearch(""); }
});

/* 带 ?q= 打开（概念笔记里的 `[[链接]]` 就是这么跳过来的）：直接把词填进搜索框。
   不填的话页面会显示全库，点链接的人会以为自己点错了。 */
function initialQuery(){
  var m = /[?&]q=([^&]*)/.exec(location.search || "");
  if (!m) return "";
  try { return decodeURIComponent(m[1]) } catch(e){ return m[1] }
}

var start = initialQuery();
if (start){ $("#q").value = start; doSearch(start); }
else loadAll();

/* 测试钩子：这几个都是纯函数（不读 DOM、不发请求），能带着夹具在 node 里跑。 */
window.__notes = {
  esc: esc,
  inline: inline,
  renderBody: renderBody,
  readerHref: readerHref,
  noteHref: noteHref,
  metaBits: metaBits,
  clockLabel: clockLabel,
  renderVideos: renderVideos,
  renderConcepts: renderConcepts
};
})();
</script>
</body></html>
"""


def html() -> str:
    """笔记库页面。纯静态字符串，不依赖任何外部资源。"""
    return _PAGE
