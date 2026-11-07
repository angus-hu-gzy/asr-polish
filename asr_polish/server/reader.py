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
<meta name="color-scheme" content="light dark">
<title>asr-polish · 字幕阅读</title>
<script>
/* 主题必须在第一次绘制之前定下来，所以这段单独放在 head、先于样式表执行。
   挪到页面末尾那段脚本里会晚一帧——夜间用户会先看见一下白底再变暗，正是
   最刺眼的那一下。键名与 __reader.themeKey() 是同一个，改一处要改两处。 */
(function(){
  var t = null;
  try {
    var v = localStorage.getItem("asr-polish:theme:v1");
    if (v === "dark" || v === "light") t = v;
  } catch(e){}                      // 隐私模式下读不了：当没存过，别拦着页面
  if (!t){
    try {
      t = (window.matchMedia
           && window.matchMedia("(prefers-color-scheme: dark)").matches)
          ? "dark" : "light";
    } catch(e){ t = "light" }
  }
  document.documentElement.setAttribute("data-theme", t);
})();
</script>
<style>
/* 颜色全在这一组变量里。白天那套是原值，夜间只换下面 [data-theme="dark"]
   那一块——规则里不写死颜色，切模式就没有「漏掉一个白底块」这种事。 */
:root{
  color-scheme:light;
  /* 纸面 → 面板 → 次级面板，三层由沉到浮 */
  --paper:#fbfaf7; --panel:#fff; --field:#f7f5ef; --head:#fcfbf7;
  /* 字色：正文 → 次要 → 最淡，另外几个是局部用的中间档 */
  --ink:#1f2328; --muted:#6b7280; --faint:#9aa4b0;
  --sub:#41474f; --quote:#4b5563; --note-strong:#6b6255;
  --placeholder:#b3aebf;
  /* 线 */
  --line:#e6e1d6; --line-soft:#efeadf; --track:#eae5da;
  /* 主色 */
  --accent:#2a6df4; --accent-hover:#1e5fe0; --accent-line:#bcd0f7;
  --accent-glow:rgba(42,109,244,.12); --on-accent:#fff;
  /* 悬停与浮起的表面 */
  --btn-hover:#f6f4ee; --btn-hover-line:#d8d2c4; --raise:#f1ede2;
  --header-bg:rgba(251,250,247,.92);
  --shadow:0 1px 2px rgba(29,33,41,.06),0 8px 24px rgba(29,33,41,.05);
  --shadow-pop:0 6px 20px rgba(40,60,50,.14);
  /* 正文：闪一下、悬停、已标重点 */
  --code:#f0ece1; --cue-hover:#f5f1e6; --flash:#fff6cc;
  --marked:#fffdf3; --marked-hover:#fffbe8;
  --star:#ffb020; --star-idle:#c9c2b2;
  /* 搜索命中 / 当前那一处命中。--mark-ink 管命中块里的字色：白天沿用正文色，
     夜间换浅黄——深底上的浅黄块配浅灰字会糊成一片。 */
  --mark:#ffe98a; --mark-ink:inherit;
  --mark-cur:#ffb020; --mark-cur-ink:inherit; --cur-glow:rgba(255,176,32,.35);
  /* 目录 */
  --toc-hover:#f4f1e9;
  /* 语义色 */
  --ok:#15803d; --warn:#b45309; --bad:#b91c1c;
  --ok-bg:#f5fbf7; --ok-line:#cfe6d6; --ok-ink:#41684f;
  --warn-bg:#fffaf0; --warn-line:#f0d9a8;
  --bad-bg:#fff6f6; --bad-line:#f2c9c9;
}

/* 夜间：深暖灰底 + 柔和浅灰字，不用纯黑纯白——那组对比太硬，读半小时眼睛
   就发酸。层级照白天来：纸面最沉、面板浮一档、输入框再亮一档；字色反过来，
   正文最亮、次要次之、最淡最暗。谁亮谁暗的顺序一处都没颠倒。 */
:root[data-theme="dark"]{
  color-scheme:dark;
  --paper:#17150f; --panel:#201d16; --field:#2a261c; --head:#26221a;
  --ink:#d8d2c4; --muted:#a09989; --faint:#8a8375;
  --sub:#b6b0a2; --quote:#a9a396; --note-strong:#c3bcaa;
  --placeholder:#7f7868;
  --line:#342f24; --line-soft:#2a261c; --track:#2e2a20;
  --accent:#7fa8ff; --accent-hover:#9dbcff; --accent-line:#3d4c6b;
  --accent-glow:rgba(127,168,255,.18); --on-accent:#11161f;
  --btn-hover:#2a261c; --btn-hover-line:#413b2c; --raise:#2b2620;
  --header-bg:rgba(23,21,15,.92);
  --shadow:0 1px 2px rgba(0,0,0,.5),0 10px 26px rgba(0,0,0,.38);
  --shadow-pop:0 8px 24px rgba(0,0,0,.5);
  --code:#2c271b; --cue-hover:#221f17; --flash:#453a19;
  --marked:#221f14; --marked-hover:#2b2717;
  --star:#e0a63c; --star-idle:#6a6355;
  /* 命中块：普通命中是压暗的琥珀，当前那一处反过来用亮琥珀 + 深字。
     两者在深底上一眼能分开——一个暗块亮字，一个亮块暗字。 */
  --mark:#574a1c; --mark-ink:#f0e2b4;
  --mark-cur:#d99a2b; --mark-cur-ink:#1b1508; --cur-glow:rgba(217,154,43,.4);
  --toc-hover:#231f16;
  --ok:#4ade80; --warn:#e2a33c; --bad:#f08a8a;
  --ok-bg:#16241b; --ok-line:#2c4434; --ok-ink:#a3d4b6;
  --warn-bg:#2a2114; --warn-line:#4a3a1c;
  --bad-bg:#2a1717; --bad-line:#4a2626;
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
  position:sticky;top:0;z-index:20;background:var(--header-bg);
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
input[type=text]:focus{border-color:var(--accent-line);
  box-shadow:0 0 0 3px var(--accent-glow)}
input[type=text]::placeholder{color:var(--placeholder)}
.btn{
  border:1px solid var(--line);background:var(--panel);border-radius:9px;
  padding:8px 14px;font-size:14px;white-space:nowrap;
  transition:background .15s,border-color .15s;
}
.btn:hover{background:var(--btn-hover);border-color:var(--btn-hover-line)}
.btn.primary{background:var(--accent);border-color:var(--accent);
             color:var(--on-accent);font-weight:550}
.btn.primary:hover{background:var(--accent-hover)}
.btn:disabled{opacity:.55;cursor:default}
.btn.sm{padding:5px 11px;font-size:12.5px;border-radius:7px}
/* 模式切换。固定最小宽度：🌙「夜间」和 ☀️「白天」字数一变，右边这个按钮
   就换宽度，搜索框跟着抖一下——切的是颜色，不该让版面前后差几像素。 */
.themetog{min-width:82px;text-align:center}
/* 「先配模型」和「总结」字数差三个，不固定宽度的话填完配置的一瞬间左边的
   搜索框会跟着变宽——为了一次状态变化抖一下版面，不值得。 */
.sumbtn{min-width:80px;text-align:center}
/* 「问答」只有两个字，也钉一下宽度：它在「总结」右边，那边一改字数这边就挪，
   两个按钮的相对位置每次都得重新找。 */
.askbtn{min-width:60px;text-align:center}

/* ── 进度 ─────────────────────────────────────────── */
#run{max-width:940px;margin:0 auto;padding:0 20px 12px}
#run[hidden]{display:none}
.runbar{height:3px;background:var(--track);border-radius:2px;overflow:hidden}
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
.notice.warn{border-color:var(--warn-line);background:var(--warn-bg)}
.notice.bad{border-color:var(--bad-line);background:var(--bad-bg)}
.notice.ok{border-color:var(--ok-line);background:var(--ok-bg);padding:10px 14px;
           font-size:13.5px}
.notice .meta{color:var(--muted);font-size:12.5px;margin-top:6px}
.notice ol{margin:8px 0 0;padding-left:20px}
.notice li{margin:2px 0}
.notice code{background:var(--code);padding:1px 5px;border-radius:4px;
             font-size:12.5px;font-family:ui-monospace,Consolas,monospace}

/* 断点续读的提示条。必须浮动：恢复位置时页面会滚到那一段，流内元素就跟着
   滚出视野了，用户根本看不到这句话。贴在底部居中，只占一行，不压住正在读
   的地方；9 秒后自己让开。 */
.resume{
  position:fixed;left:50%;transform:translateX(-50%);bottom:22px;z-index:30;
  display:flex;align-items:center;gap:12px;max-width:calc(100vw - 32px);
  padding:9px 13px;background:var(--ok-bg);border:1px solid var(--ok-line);border-radius:9px;
  font-size:12.5px;line-height:1.6;color:var(--ok-ink);
  box-shadow:var(--shadow-pop);
}
.resume[hidden]{display:none}
.resume .sp{flex:1}

/* 搜索条 */
.tools{
  display:flex;align-items:center;gap:8px;flex-wrap:wrap;
  padding:10px 12px;margin:0 0 22px;background:var(--panel);
  border:1px solid var(--line);border-radius:11px;box-shadow:var(--shadow);
}
.tools[hidden]{display:none}
.tools input{flex:1;min-width:170px;background:var(--field);border-color:transparent}
.tools input:focus{background:var(--panel);border-color:var(--accent-line)}
.tools .count{font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums;
              min-width:52px;text-align:right}
.tools .stat{font-size:12.5px;color:var(--faint);white-space:nowrap;
             border-left:1px solid var(--line);padding-left:11px}
/* 「还没配好」不能靠旁边挂一段说明来提示：工具栏在 1440 宽下本来就是一整行，
   多一段二十多字的灰字会把它挤成两行，正文跟着往下跳一截。所以状态写在按钮
   自己身上——文案改成「先配模型」，虚线框表示它不是坏了、是还没配。
   完整原因留在 title 里，点一下直接打开设置。 */
.btn.off{opacity:.62;border-style:dashed}
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
             align-items:start;padding:7px 0;border-top:1px solid var(--line-soft)}
.marksbox li:first-child{border-top:none}
.marksbox .mt{background:none;border:none;padding:0;text-align:right;cursor:pointer;
  font:11.5px/1.9 ui-monospace,Consolas,monospace;color:var(--faint);
  font-variant-numeric:tabular-nums}
.marksbox .mt:hover{color:var(--accent)}
.marksbox .mx{font-size:13.5px;line-height:1.7;color:var(--sub);cursor:pointer;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.marksbox .mx:hover{color:var(--ink)}
.marksbox .md{background:none;border:none;padding:0;cursor:pointer;
  color:var(--star-idle);font-size:12.5px;line-height:1.9}
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
.cue.flash{background:var(--flash);box-shadow:0 0 0 8px var(--flash)}
/* 可跳转的段落：整段可点。悬停才把时间戳染成主色，平时不打扰阅读。 */
.cue.can{cursor:pointer}
.cue.can:hover{background:var(--cue-hover);box-shadow:0 0 0 5px var(--cue-hover)}
.cue.can:hover .t{color:var(--accent)}
.partnote{
  border:1px solid var(--line);background:var(--field);border-radius:9px;
  padding:9px 12px;margin:0 0 20px;font-size:12.5px;line-height:1.75;color:var(--muted);
}
.partnote b{color:var(--note-strong);font-weight:600}
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
  color:var(--star-idle);opacity:0;cursor:pointer;
  transition:opacity .15s,color .15s;
}
.cue:hover .mk{opacity:1}
.cue .mk:hover{color:var(--star)}
.cue.marked .mk{opacity:1;color:var(--star)}
.cue.marked{background:var(--marked);box-shadow:inset 3px 0 0 var(--star)}
.cue.marked.can:hover{background:var(--marked-hover);
  box-shadow:inset 3px 0 0 var(--star),0 0 0 5px var(--marked-hover)}
@media (hover:none){ .cue .mk{opacity:.45} }
mark{background:var(--mark);color:var(--mark-ink);border-radius:2px;padding:0 1px}
mark.cur{background:var(--mark-cur);color:var(--mark-cur-ink);
         box-shadow:0 0 0 2px var(--cur-glow)}
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
.toc button:hover{background:var(--toc-hover);color:var(--ink)}
.toc button.on{background:var(--raise);border-left-color:var(--accent);color:var(--ink)}
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
            border-bottom:1px solid var(--line);background:var(--head)}
.player .pt{flex:1;min-width:0;font-size:12.5px;color:var(--muted);
            white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.player .plink{font-size:12px;white-space:nowrap}
/* 图标按钮（播放器收起、重点列表收起共用）。一开始它写在 .player 下面，
   结果重点列表那个 ✕ 拿不到样式，渲染成浏览器默认的凸起按钮。 */
.pbtn{border:none;background:none;color:var(--faint);font-size:13px;
      line-height:1;padding:3px 6px;border-radius:5px;cursor:pointer}
.pbtn:hover{background:var(--raise);color:var(--ink)}
.player .pbox{position:relative;width:100%;aspect-ratio:16/9;background:#000}
.player iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
.player .pf{padding:7px 10px;font-size:12px;color:var(--muted);
            font-variant-numeric:tabular-nums}

/* 模型设置面板：和重点列表一样走流内展开，不做浮层。
   浮层要处理「点外面关闭」、要压 z-index、夜间还得单独调一层阴影，而这里
   需要的只是一个能安稳填三行字的方框。 */
.modelbox{
  max-width:46em;margin:0 auto 18px;background:var(--panel);
  border:1px solid var(--line);border-radius:11px;padding:12px 14px 14px;
  box-shadow:var(--shadow);
}
.modelbox[hidden]{display:none}
.modelbox .mh{display:flex;align-items:center;gap:9px;margin-bottom:10px}
.modelbox h3{margin:0;font-size:13.5px;font-weight:600}
.modelbox .sp{flex:1}
.modelbox .mrow{display:grid;grid-template-columns:78px 1fr;gap:10px;
                align-items:center;margin:0 0 8px}
.modelbox label{font-size:12.5px;color:var(--muted)}
.modelbox input{
  width:100%;min-width:0;background:var(--field);border:1px solid var(--line);
  border-radius:8px;padding:7px 10px;font-size:13px;outline:none;
}
.modelbox input:focus{background:var(--panel);border-color:var(--accent-line);
  box-shadow:0 0 0 3px var(--accent-glow)}
.modelbox input::placeholder{color:var(--placeholder)}
.modelbox .mact{display:flex;align-items:center;gap:9px;margin-top:10px;flex-wrap:wrap}
.modelbox .mnote{font-size:12px;color:var(--muted)}
/* 这段是给人看的告知，不是免责声明：全文确实会离开这台机器，确实要花钱。 */
.modelbox .mhint{font-size:12px;line-height:1.85;color:var(--muted);margin:11px 0 0;
  padding-top:10px;border-top:1px solid var(--line-soft)}

/* 总结：正文上方、流内展开，不盖住正文，也不用管浮层的关闭时机。 */
.summary{
  max-width:46em;margin:0 auto 22px;background:var(--panel);
  border:1px solid var(--line);border-radius:11px;padding:13px 15px 15px;
  box-shadow:var(--shadow);
}
.summary[hidden]{display:none}
.summary .sh{display:flex;align-items:center;gap:9px;margin-bottom:10px}
.summary h3{margin:0;font-size:13.5px;font-weight:600}
.summary .sp{flex:1}
.summary .sstat{font-size:12px;color:var(--faint);font-variant-numeric:tabular-nums}
.summary .s-tldr{
  margin:0 0 14px;padding:11px 13px;background:var(--field);border-radius:9px;
  font-size:14.5px;line-height:1.8;color:var(--ink);
}
.summary .s-block{margin:0 0 14px}
.summary .s-block:last-child{margin-bottom:0}
.summary .s-block h4{margin:0 0 7px;font-size:12px;font-weight:600;
  color:var(--muted);letter-spacing:.04em}
.summary ol{margin:0;padding-left:20px}
.summary ol li{margin:0 0 7px;font-size:13.5px;line-height:1.8;color:var(--sub)}
.summary ol li:last-child{margin-bottom:0}
.summary .cl{list-style:none;margin:0;padding:0}
.summary .cl li{display:grid;grid-template-columns:auto 1fr;gap:9px;padding:4px 0;
  font-size:13px;line-height:1.75}
.summary .cl b{color:var(--note-strong);font-weight:600}
.summary .cl span{color:var(--muted)}
/* 锚点：一个小时间按钮。能点的悬停变主色；无出处的就是个灰字，不给手型、
   也不给悬停反馈——看起来能点却点不动，比看起来不能点更让人恼火。
   总结和问答共用这一套：同一个「时间是不是真的」的判断，长成一样最省事。 */
.summary .anchor,.qa .anchor{
  display:inline-block;margin-left:8px;padding:0 5px;border-radius:5px;
  border:1px solid var(--line);background:var(--panel);vertical-align:1px;
  font:11.5px/1.7 ui-monospace,Consolas,monospace;color:var(--faint);
  font-variant-numeric:tabular-nums;cursor:pointer;
}
.summary .anchor:hover,.qa .anchor:hover{border-color:var(--accent-line);
  color:var(--accent);background:var(--accent-glow)}
.summary .anchor.none,.qa .anchor.none{border-color:transparent;background:none;
  cursor:default;color:var(--faint)}
.summary .anchor.none:hover,.qa .anchor.none:hover{color:var(--faint);
  background:none;border-color:transparent}
.summary .note,.qa .note{
  margin:0 0 12px;padding:8px 11px;border-radius:8px;font-size:12.5px;
  line-height:1.75;background:var(--warn-bg);border:1px solid var(--warn-line);
  color:var(--note-strong);
}
.summary .s-empty{font-size:13px;color:var(--muted);line-height:1.9}
.summary .s-err{font-size:13px;line-height:1.85;color:var(--bad);word-break:break-word}
.summary .s-err .fix{display:block;margin-top:7px;color:var(--muted);font-size:12.5px}

/* 问答：和总结同一块地方、同一种排法——正文上方、流内展开，不盖住正文。
   两处差别是它比总结长得多，所以对话列表自己有最大高度，超过就在框里滚；
   不设的话问上十轮，正文会被顶到三屏以外，「问答区」变成了「问答页」。 */
.qa{
  max-width:46em;margin:0 auto 22px;background:var(--panel);
  border:1px solid var(--line);border-radius:11px;padding:13px 15px 15px;
  box-shadow:var(--shadow);
}
.qa[hidden]{display:none}
.qa .qh{display:flex;align-items:center;gap:9px;margin-bottom:10px}
.qa h3{margin:0;font-size:13.5px;font-weight:600}
.qa .sp{flex:1}
.qa .qstat{font-size:12px;color:var(--faint);font-variant-numeric:tabular-nums}
/* 没有逐句时间那一篇的说明放在顶上，常驻：它不是每条回答的脚注，是这一篇的
   前提。放在回答下面的话，读完一整段才发现「定位不了」，等于白读。 */
.qa .qanote{
  margin:0 0 12px;padding:8px 11px;border-radius:8px;font-size:12.5px;
  line-height:1.75;background:var(--warn-bg);border:1px solid var(--warn-line);
  color:var(--note-strong);
}
.qa .qalist{max-height:52vh;overflow:auto;margin:0 0 11px}
.qa .turn{margin:0 0 15px}
.qa .turn:last-child{margin-bottom:0}
.qa .q{margin:0 0 6px;font-size:13px;line-height:1.7;color:var(--muted)}
.qa .q b{margin-right:7px;font-weight:600;color:var(--note-strong)}
.qa .a{font-size:14px;line-height:1.85;color:var(--sub);word-break:break-word}
.qa .a p{margin:0 0 7px}
.qa .a p:last-child{margin-bottom:0}
/* 回答下方那句「没有可验证的出处」比总结里的提示更该显眼一点：它说的是
   「这一段话别当真」，而不是「这里有点小问题」。 */
.qa .a .note{margin:9px 0 0}
.qa .qasug{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 11px}
.qa .sug{border:1px dashed var(--line);background:var(--panel);border-radius:999px;
  padding:4px 11px;font-size:12.5px;color:var(--muted)}
.qa .sug:hover{border-color:var(--accent-line);color:var(--accent);
  background:var(--accent-glow)}
.qa .qaf{margin:0}
.qa .qaf input{background:var(--field);border-color:transparent}
.qa .qaf input:focus{background:var(--panel);border-color:var(--accent-line)}
/* 缺什么就写在按钮左边。总结button那边是靠 title 说的（顶栏没有位置），
   这里面一行有地方，写出来比让人把鼠标停在按钮上强。 */
.qa .qmiss{font-size:12px;color:var(--warn);white-space:nowrap}
/* 价格标签：每点一次就是一次计费调用。这句必须常驻，不能只在首次提示——
   用户是隔一会儿来问一句的，提示过就消失的写法等于没说过。 */
.qa .qcost{margin:9px 0 0;font-size:12px;line-height:1.75;color:var(--faint)}
.qa .qempty{font-size:13px;color:var(--muted);line-height:1.9}
.qa .qbusy{font-size:13px;color:var(--muted);padding:4px 0}
.qa .qerr{font-size:13px;line-height:1.85;color:var(--bad);word-break:break-word}
.qa .qerr .fix{display:block;margin-top:7px;color:var(--muted);font-size:12.5px}

/* 失败明细 */
.fails{margin:26px 0 0;border-top:1px solid var(--line);padding-top:18px}
.fails h3{font-size:13.5px;font-weight:600;margin:0 0 10px;color:var(--bad)}
.fails ul{list-style:none;margin:0 0 14px;padding:0}
.fails li{display:grid;grid-template-columns:150px 1fr;gap:14px;
          padding:8px 0;border-bottom:1px solid var(--line-soft);font-size:13px}
.fails li:last-child{border-bottom:none}
.fails .bv{font-family:ui-monospace,Consolas,monospace;color:var(--muted);font-size:12.5px}
.fails .why{color:var(--quote);word-break:break-word}

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
              white-space:nowrap;background:var(--field);padding:5px 10px}
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
    <button class="btn sm" id="modelbtn" type="button"
            title="配置模型服务：总结要用它。三样只存在这个浏览器里">⚙ 模型</button>
    <button class="btn sm off sumbtn" id="sumbtn" type="button"
            title="先在设置里填服务地址、模型名、API key">先配模型</button>
    <button class="btn sm askbtn" id="askbtn" type="button" disabled
            title="就这一篇提问。回答里的每条事实都要带原文时间，点时间能回到原文">问答</button>
    <button class="btn sm themetog" id="themebtn" type="button">🌙 夜间</button>
  </div>
  <div id="run" hidden>
    <div class="runbar"><i id="fill"></i></div>
    <div class="runmeta"><span class="m" id="runmsg"></span><span class="t" id="runtime"></span></div>
  </div>
</header>

<main>
  <div id="notice"></div>

  <div class="modelbox" id="modelbox" hidden>
    <div class="mh">
      <h3>模型设置</h3>
      <span class="sp"></span>
      <button class="pbtn" id="modelclose" type="button" title="收起设置">✕</button>
    </div>
    <div class="mrow">
      <label for="mbase">服务地址</label>
      <input id="mbase" type="text" spellcheck="false"
             placeholder="api.deepseek.com/v1">
    </div>
    <div class="mrow">
      <label for="mname">模型名</label>
      <input id="mname" type="text" spellcheck="false" placeholder="deepseek-chat">
    </div>
    <div class="mrow">
      <label for="mkey">API key</label>
      <input id="mkey" type="password" spellcheck="false" placeholder="sk-…">
    </div>
    <div class="mact">
      <button class="btn sm primary" id="modelsave" type="button">保存</button>
      <button class="btn sm" id="modelclear" type="button">清除</button>
      <span class="mnote" id="modelnote"></span>
    </div>
    <p class="mhint">
      地址填到 <code>/v1</code> 这一层就行，末尾的 <code>/chat/completions</code>
      会自己接上；不写 <code>https://</code> 的话按 https 算（要走 http 就得自己写明）。
      只支持 OpenAI 兼容的接口。<br>
      <b>点总结时，这一篇的全文会发给你填的这个服务</b>，按它的计费规则花钱——
      所以用你自己的 key。三样东西只存在这个浏览器里，不写进任何文件，
      也不会出现在服务日志里。换台机器就要重新填。
    </p>
  </div>

  <div class="tools" id="tools" hidden>
    <input id="find" type="text" spellcheck="false" placeholder="在文字稿里搜…">
    <button class="btn sm" id="prev" title="上一处 (Shift+Enter)">↑</button>
    <button class="btn sm" id="next" title="下一处 (Enter)">↓</button>
    <button class="btn sm" id="mkbtn" type="button" title="看标过的重点">重点 <span id="mkn">0</span></button>
    <button class="btn sm" id="exportFull" type="button" disabled
            title="把整篇文字稿导出成 Markdown，每段前面带时间戳。没有正文时这个按钮是灰的">导出全文 md</button>
    <span class="count" id="count"></span>
    <span class="stat" id="stat"></span>
  </div>

  <div class="summary" id="summary" hidden>
    <div class="sh">
      <h3 id="sumh">这一篇的总结</h3>
      <span class="sp"></span>
      <span class="sstat" id="sumstat"></span>
      <button class="btn sm" id="sumregen" type="button"
              title="重新调一次模型，覆盖现在这份（会再花一次钱）">重新生成</button>
      <button class="pbtn" id="sumclose" type="button" title="收起总结">✕</button>
    </div>
    <div id="sumbody"></div>
  </div>

  <div class="qa" id="qa" hidden>
    <div class="qh">
      <h3 id="qah">这一篇的问答</h3>
      <span class="sp"></span>
      <span class="qstat" id="qastat"></span>
      <button class="btn sm" id="qaclear" type="button"
              title="只清掉这一篇的对话，别的篇不动；清完再提问就是新对话">清空对话</button>
      <button class="pbtn" id="qaclose" type="button" title="收起问答">✕</button>
    </div>
    <div class="qanote" id="qanote" hidden></div>
    <div class="qalist" id="qalist"></div>
    <div class="qasug" id="qasug" hidden></div>
    <form class="qaf" id="qaf" autocomplete="off">
      <input id="qain" type="text" spellcheck="false"
             placeholder="就这一篇问点什么，回答会标出原文时间…">
      <span class="qmiss" id="qamiss" hidden></span>
      <button class="btn sm primary" id="qabtn" type="submit">提问</button>
    </form>
    <p class="qcost">
      每点一次「提问」都真的调一次模型服务，按你填的那家计费——刷新之后重问也要再花一次。
      只把最近 6 轮带过去，更早的留在页面上、不发。
    </p>
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

  <div class="resume" id="resume" hidden>
    <span id="resumetx">已回到上次读到的位置</span>
    <span class="sp"></span>
    <button class="btn sm" id="resumetop" type="button">回顶部</button>
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
          text:"", parts:0, task:null, mk:[], lsBroken:false, part:0, anyJump:false,
          posArmed:false, transcript:null, gen:0,
          /* 总结这一层的临时状态。sumMem 是内存里的那份缓存：localStorage
             写不进去时（隐私模式）这一页仍然要能看，不能存一遍就消失。 */
          sumBusy:false, sumErr:null, sumMem:null, sumAnchor:"", sumWarned:false,
          /* 问答这一层。qaMem 和 sumMem 同一个用途：localStorage 写不进去时，
             这一页里问过的必须还看得见。 */
          qaBusy:false, qaErr:null, qaMem:null, qaWarned:false };

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
  // 代次 +1：上一轮还没回来的请求（poll / loadTranscript）到此作废。
  // 只 clearTimeout 管不到已经 await 出去的那次——它回来时会把旧任务的
  // 失败原因、分 P 元信息写到新任务的页面上，甚至让「点句子跳转」打开上一个视频。
  S.gen++;
  S.id = id;
  S.transcript = null;              // 换任务了，上一条的全文不能留着被导出
  $("#exportFull").disabled = true;
  try { history.replaceState(null, "", "?task=" + encodeURIComponent(id)); } catch(e){}
  if (S.timer) { clearTimeout(S.timer); S.timer = null; }
  stopTimer();                       // 否则新任务会沿用上一个任务的开始时间
  resetNotice(); clear($("#doc")); clear($("#fails"));
  $("#tools").hidden = true;
  $("#marksbox").hidden = true;        // 换任务了，重点列表先收起来
  $("#summary").hidden = true;         // 总结是按任务存的，别把上一条的摆在眼前
  $("#qa").hidden = true;              // 对话同理：上一条任务的问答不能留在新页面上
  S.sumMem = {}; S.sumErr = null; S.sumAnchor = "";
  S.qaMem = {}; S.qaErr = null; S.qaBusy = false;
  updateAskEntry();
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
  var gen = S.gen;
  var res = await call("GET", "/api/tasks/" + encodeURIComponent(S.id));
  if (gen !== S.gen) return;      // 期间换任务了，这份响应不属于当前页面
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
  var gen = S.gen;
  // 拉的是传进来这个任务的正文，不是「此刻 S.id 指向的那个」——两者在切换
  // 任务的那一瞬间会叉开，用后者等于把新任务的文字渲染到旧任务的元信息上。
  var res = await call("GET", "/api/tasks/" + encodeURIComponent(task.id) + "/transcript");
  if (gen !== S.gen) return;      // 期间换任务了，别渲染到新页面上
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
  // 换篇之后上一篇的高亮已经不在这一篇里了：不重搜的话，计数还是上一篇的数，
  // 按 ↓ 会顺着上一篇的命中往前跳。带上 noJump，别把人从这一篇顶上拽走。
  var q = $("#find").value;
  if (q) runSearch(q, true);
  // 总结是按篇存的：切到哪一篇就看哪一篇，没做过的显示「还没生成」。
  if (!$("#summary").hidden) renderSummary();
  // 对话也一样：每篇一份，切篇就换一份。旧请求回来时会存回它自己那一篇，
  // 所以这里只管画当前这一篇。
  if (!$("#qa").hidden) renderQa();
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
  S.transcript = d;                  // 全文导出的唯一数据来源，就是这份接口返回
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
    $("#exportFull").disabled = true;   // 没有正文就不给导出，别让人下个空文件
    updateSumButton();
    updateAskEntry();                  // 没有正文，问答入口也点不动
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
  $("#exportFull").disabled = false;
  updateSumButton();                 // 有正文了，总结才可能点得动
  updateAskEntry();                  // 问答入口同理
  renderFailures(r, task);
  restoreMarks();
  // 带 ?q= 进来的是「我要搜这个词」，位置让位给搜索：搜索要跳到第一处命中，
  // 两套滚动抢起来只会落到一处谁也不想要的地方。
  if ($("#find").value) runSearch($("#find").value);
  else restorePos();
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

function mdFileName(title, bvid, suffix){
  // 标题里什么字符都可能有（`/ : < >` 在 Windows 上是非法文件名），统一换成短横，
  // 再把连续的短横压成一个——不然「从零: 入门」会变成「从零--入门」。
  var base = String(title || bvid || "重点")
    .replace(/[\\/:*?"<>|]+/g, "-").replace(/\s+/g, "-")
    .replace(/-+/g, "-").slice(0, 60).replace(/^-+|-+$/g, "");
  return (base || "重点") + "-" + (suffix || "重点") + ".md";
}

/* 下载就这一条路：Blob + a[download]，不新增后端接口。
   重点导出和全文导出都走它——两套下载逻辑迟早会分叉（revoke 的时机、
   文件名转义各改一半），而这类分叉只有用户拿到空文件才发现。 */
function saveBlob(name, text, mime){
  var blob = new Blob([text], { type: mime });
  var url = URL.createObjectURL(blob);
  var a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // 立刻 revoke 在部分浏览器上会让下载拿到空文件，等一拍再放
  setTimeout(function(){ URL.revokeObjectURL(url); }, 1500);
}

/* 按钮上回一句「已导出」，1.6 秒后恢复。导出没有别的反馈，不给这一下
   用户会怀疑自己点空了、再点一次。 */
function flashBtn(btn, text){
  if (!btn) return;
  var old = btn.textContent;
  btn.textContent = text;
  setTimeout(function(){ btn.textContent = old; }, 1600);
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
  saveBlob(mdFileName(scope.title, scope.single), md, "text/markdown;charset=utf-8");
  flashBtn($("#export"), "已导出");
}

$("#export").addEventListener("click", exportMarks);

/* ── 全文：工作台各层共用的底座 ──────────────────── */
/* 这一层只做一件事：把 transcript 的分段变成「结构化全文」，而且只变一次。

   为什么非要有个函数：总结、问答、测验、合集检索都要吃全文。要是各层自己
   去遍历 `d.parts[i].segments`，每层都得重新决定一遍「怎么断段、没时间怎么
   算、BV 号从哪来、分 P 怎么标」——四处各写一遍，就有四处会各错一次，而且
   改了一处忘掉另外三处时没有任何报错。

   所以规矩定死：**要全文就调 buildFullText，不要在别处遍历 segments。**
   拿到结构化全文之后再分两路——发给模型用 buildPlainText，给人看用
   buildFullMarkdown。两者都只消费这一份结构。

   断段用的是 groupCues，和阅读页屏幕上的段落是同一套判据。导出文件和页面对
   不上时，用户的第一反应是「导错了」，不会想到「分段算法本来就不同」。 */

/* 必剪 ASR 和纯文本导出的 srt，整篇被写成一块 0 → 99:59:59 的占位时间。
   transcript 在 srt 那条路上已经按行拆开并把 start 置 null，但 json 那条路
   不拆。所以这里再兜一次：任何一条下游都不该看到 99:59:59 这种假时间——
   假时间比没时间糟得多，它会被当成真的拿去引用。 */
var PLACEHOLDER_END = 99 * 3600 + 59 * 60 + 59;

function isPlaceholder(seg){
  return !!seg && seg.start === 0 && seg.end != null && seg.end >= PLACEHOLDER_END;
}

function fullSegments(segs){
  var out = [];
  (segs || []).forEach(function(s){
    s = s || {};
    var text = s.text == null ? "" : String(s.text);
    if (isPlaceholder(s)){
      // 整篇包在一个块里：按行拆开，时间置 null。这不是造数据，是「不知道就
      // 说不知道」——拆开之后每一行仍然没有时间，页面和导出都照实说。
      text.split("\n").forEach(function(ln){
        ln = ln.replace(/^\s+|\s+$/g, "");
        if (ln) out.push({ start: null, end: null, text: ln });
      });
      return;
    }
    out.push({ start: s.start == null ? null : s.start,
               end: s.end == null ? null : s.end,
               text: text });
  });
  return out;
}

/* 结构化全文。以后工作台每一层的入口就是这里。

   返回：
   {
     task, paragraphs, chars,
     parts: [{
       index, name, title, bvid, page, source,
       hasTime,        // 这一篇有没有逐句时间；false = 只能当纯文本用
       label,          // "P2" 或 "第 2 篇"（单篇时为空串）
       segments: [{ start, end, text }]   // start/end 是秒，没有就是 null
     }]
   } */
function buildFullText(d, task){
  d = d || {};
  task = task || S.task || {};
  var src = d.parts || [];
  var full = { task: d.task || S.id || "", parts: [], paragraphs: 0, chars: 0 };

  // 同一个视频的多个分 P 标「P2」，多条视频（up: / search:）标「第 2 篇」。
  // 和左边目录用同一个判据——导出写 P1、目录写第 1 篇会让人以为漏了篇。
  var sameBv = src.length > 0;
  var firstBv = src.length ? partMeta(src[0].name, task).bvid : "";
  for (var k = 1; k < src.length; k++){
    var bv = partMeta(src[k].name, task).bvid;
    if (!bv || bv !== firstBv) sameBv = false;
  }

  src.forEach(function(part, i){
    var pm = partMeta(part.name, task);
    var rows = groupCues(fullSegments(part.segments));
    var hasTime = false;
    for (var j = 0; j < rows.length; j++){
      if (rows[j].start != null){ hasTime = true; break; }
    }
    full.parts.push({
      index: i,
      name: part.name || "",
      title: pm.title || "",
      bvid: pm.bvid,
      page: pm.page,
      source: part.source || "",
      hasTime: hasTime,
      label: src.length > 1 ? (sameBv ? "P" + pm.page : "第 " + (i + 1) + " 篇") : "",
      segments: rows
    });
    full.paragraphs += rows.length;
    for (var m = 0; m < rows.length; m++) full.chars += rows[m].text.length;
  });
  return full;
}

/* 全文里出现了哪些视频。跨视频的任务（up: / search:）没有统一标题，只能列 BV。 */
function fullScope(full, task){
  var bvids = [];
  (full && full.parts || []).forEach(function(p){
    if (p.bvid && bvids.indexOf(p.bvid) < 0) bvids.push(p.bvid);
  });
  var single = bvids.length === 1 ? bvids[0] : "";
  return { bvids: bvids, single: single,
           title: single ? videoTitleOf(task || S.task, single) : "" };
}

/* 纯文本：以后发给模型用的就是这一份。
   只留段落、不留时间戳——给模型看的正文，时间戳是噪音；真需要时间的层
   （比如「这句话在第几分钟」）直接读 buildFullText 的结构，那里一个都没丢。
   分 P 之间用 `---` 隔开，多篇时每篇前面加一行标题，模型才知道边界在哪。 */
function buildPlainText(full){
  full = full || { parts: [] };
  var parts = full.parts || [];
  var multi = parts.length > 1;
  var blocks = [];
  parts.forEach(function(p){
    var body = (p.segments || []).map(function(s){
      return String(s.text || "").replace(/\s*\n\s*/g, " ").trim();
    }).filter(function(t){ return t; }).join("\n");
    if (!body) return;
    var head = multi
      ? "## " + (p.label ? p.label + " · " : "") + (p.title || p.bvid || p.name) + "\n\n"
      : "";
    blocks.push(head + body);
  });
  return blocks.join("\n\n---\n\n");
}

function fullFileName(full, task){
  var scope = fullScope(full, task || S.task);
  return mdFileName(scope.title, scope.single, "全文");
}

/* 导出给人看的全文 md。和「导出重点」不是一份东西：那是几条摘录，这是整篇。 */
function buildFullMarkdown(full, now, task){
  full = full || { parts: [] };
  var parts = full.parts || [];
  if (!parts.length) return "";        // 没有正文就不给文件，按钮本来就是灰的
  var scope = fullScope(full, task || S.task);
  var out = [];
  // 有标题就顶标题；标题查不到但有唯一 BV 号时用 BV 号顶上。跨视频任务两样
  // 都没有，写「全文」就够了——别拼成「全文导出 · 全文」这种自己重复自己的东西
  var head = scope.title || scope.single;
  out.push(head ? "# " + head + " · 全文" : "# 全文");
  out.push("");
  out.push("- 标题：" + (scope.title || "（任务里没有视频标题）"));
  if (scope.single) out.push("- BV 号：" + scope.single);
  else out.push("- BV 号：" + scope.bvids.length + " 条 · "
              + (scope.bvids.join("、") || "未知"));
  out.push("- 分 P：" + parts.length + " 篇");
  out.push("- 导出：" + mdStamp(now));
  out.push("- 正文：" + full.chars.toLocaleString() + " 字 · " + full.paragraphs + " 段");
  out.push("");
  out.push("---");
  out.push("");
  parts.forEach(function(p, i){
    out.push("## " + (p.label ? p.label + " · " : "")
           + (p.title || p.name || ("第 " + (i + 1) + " 篇")));
    out.push("");
    if (!p.hasTime){
      out.push("> 这一篇没有逐句时间戳（必剪 ASR 只给了 0 → 99:59:59 的整块占位时间，"
             + "或者来自纯文本导出）。下面每段前的 `[无时间戳]` 是标注，不是真实时间。");
      out.push("");
    }
    (p.segments || []).forEach(function(s){
      var stamp = s.start == null ? "无时间戳" : fmt(s.start);
      out.push("[" + stamp + "] "
             + String(s.text || "").replace(/\s*\n\s*/g, " ").trim());
    });
    out.push("");
  });
  return out.join("\n").replace(/\n+$/, "\n");
}

function exportFull(){
  var full = buildFullText(S.transcript, S.task);
  if (!full.parts.length) return;      // 没有产出就不导出空文件
  saveBlob(fullFileName(full, S.task),
           buildFullMarkdown(full, new Date(), S.task),
           "text/markdown;charset=utf-8");
  flashBtn($("#exportFull"), "已导出");
}

$("#exportFull").addEventListener("click", exportFull);

/* ── 单篇总结（学习工作台第 1 层）───────────────────── */
/* 这一层的规矩只有一条：**没有出处的总结不上线**。
   所以整块拆成两半——发送前（怎么让模型看见真实时间）和渲染前（哪些时间是真的）。

   模型配置只存浏览器，和划重点、读位、主题一个路数：没有账号，也就没有同步。
   key 不写文件、不进日志、不进任何一条错误消息——它的唯一去处是这个请求的
   Authorization 头。 */

var MODEL_KEY = "asr-polish:model:v1";
var SUM_PREFIX = "asr-polish:summary:v1:";

function modelKey(){ return MODEL_KEY }
function sumKey(taskId){ return SUM_PREFIX + taskId }

function lsDel(k){ try { localStorage.removeItem(k); return true } catch(e){ return false } }

/* 配置解析。坏 JSON、字段类型不对，一律当「没配」——宁可让按钮灰着让人去填，
   也不能拿一份半截配置去发请求（那会把 key 发到一个空地址上）。 */
function parseModel(raw){
  var empty = { base:"", model:"", key:"" };
  if (!raw) return empty;
  try {
    var d = JSON.parse(raw);
    if (!d || typeof d !== "object") return empty;
    return {
      base:  typeof d.base  === "string" ? d.base  : "",
      model: typeof d.model === "string" ? d.model : "",
      key:   typeof d.key   === "string" ? d.key   : ""
    };
  } catch(e){ return empty }
}

/* 缺什么就说什么。泛泛一句「请先配置」等于没说：用户不知道是地址没填、
   模型名没填，还是 key 忘了粘。 */
function modelReady(cfg){
  cfg = cfg || { base:"", model:"", key:"" };
  var miss = [];
  if (!String(cfg.base  || "").trim()) miss.push("服务地址");
  if (!String(cfg.model || "").trim()) miss.push("模型名");
  if (!String(cfg.key   || "").trim()) miss.push("API key");
  return {
    ok: !miss.length,
    missing: miss,
    why: miss.length ? ("先在设置里填" + miss.join("、")) : ""
  };
}

/* 发给模型的正文。
   
   为什么要带时间戳：要点要能点回原文，模型就必须在自己看得见的时间戳里挑。
   `buildPlainText` 刻意剥掉了时间（给模型读的正文，时间戳是噪音），所以这里
   回手把它按行对齐补回来——**行数与段数对不上就整篇不标**，错位的时间戳会
   让每一条要点都指到别处，比没有时间戳糟得多。
   
   正文本身仍然出自 buildPlainText：只有一份「全文怎么拼」的规矩。 */
function buildSummarySource(full, i){
  var part = (full && full.parts && full.parts[i]) || null;
  if (!part) return { text:"", hasTime:false, segs:[] };
  var segs = part.segments || [];
  // 单篇传进去，buildPlainText 就不会加「## P1 · 标题」那一层壳
  var body = buildPlainText({ parts: [part] });
  var lines = body ? body.split("\n") : [];
  var hasTime = !!part.hasTime;
  var out = [];
  if (hasTime && lines.length === segs.length){
    for (var k = 0; k < segs.length; k++){
      var st = segs[k].start == null ? "无时间" : fmt(segs[k].start);
      out.push("[" + st + "] " + lines[k]);
    }
  } else {
    out = lines.slice();
    if (lines.length !== segs.length) hasTime = false;
  }
  var head = hasTime ? ""
    : "（这一篇没有逐句时间戳，行首没有 [分:秒]。所有要点的 t 请写成 null。）\n\n";
  return { text: head + out.join("\n"), hasTime: hasTime, segs: segs };
}

/* 模型给的时间 → 秒。"01:23" / "1:02:05" / "83" 三种都认。
   只做单位换算，不做任何补全："3" 就是 3 秒，不是 3 分钟。 */
function parseStamp(v){
  if (v == null) return null;
  if (typeof v === "number") return v >= 0 ? Math.round(v) : null;
  var s = String(v).trim().replace(/^\[/, "").replace(/\]$/, "").trim();
  if (!s) return null;
  var m = /^(\d+)$/.exec(s);
  if (m) return parseInt(m[1], 10);
  m = /^(?:(\d+):)?(\d{1,3}):(\d{1,2})$/.exec(s);
  if (m){
    return (m[1] ? parseInt(m[1], 10) * 3600 : 0)
         + parseInt(m[2], 10) * 60 + parseInt(m[3], 10);
  }
  m = /^(\d{1,3}):(\d{1,2})$/.exec(s);
  if (m) return parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
  return null;
}

/* 这个时间在不在原文里：返回最近那一段的序号，够不着就 -1。
   
   容差 1.5 秒不是为了宽容，是因为**页面显示的时间是四舍五入过的**：段落
   start 是 62.7，屏幕上写 01:03，模型抄回 63——不放容差的话，每一条要点都会
   被判成「无出处」，而这个功能就白做了。1.5 秒不会指到相邻段（相邻段间隔
   普遍在 2 秒以上），取最近的即可。 */
function matchStamp(segs, sec, tol){
  if (sec == null || !segs || !segs.length) return -1;
  tol = tol == null ? 1.5 : tol;
  var best = -1, bestD = Infinity;
  for (var i = 0; i < segs.length; i++){
    var st = segs[i].start;
    if (st == null) continue;
    var d = Math.abs(st - sec);
    if (d < bestD){ bestD = d; best = i }
  }
  return bestD <= tol ? best : -1;
}

/* 把每条要点配上「在原文第几段」。idx = -1 就是无出处。
   这一篇本来没有逐句时间（必剪 ASR / 纯文本）时，哪怕模型硬给了时间也一律
   判无出处——它不可能知道，给了就是编的。 */
function anchorsOf(points, segs, hasTime, tol){
  return (points || []).map(function(p){
    var t = parseStamp(p && p.t);
    var idx = (hasTime && t != null) ? matchStamp(segs, t, tol) : -1;
    return { text: (p && p.text) || "", t: t, idx: idx };
  });
}

/* 这一篇正文的指纹。任务重跑、字幕变了，指纹就变，旧总结自动作废。
   用内容而不是时间戳：任务表的字段是「任务什么时候跑的」，不是「这篇字幕
   什么时候变的」；而字数 + 段数 + 首尾两句足以认出「同一篇」。 */
function summarySig(part){
  var segs = (part && part.segments) || [];
  if (!segs.length) return "0";
  var chars = 0;
  for (var i = 0; i < segs.length; i++) chars += String(segs[i].text || "").length;
  var head = String(segs[0].text || "").slice(0, 24);
  var tail = String(segs[segs.length - 1].text || "").slice(-24);
  return segs.length + ":" + chars + ":" + head + "|" + tail;
}

/* 缓存按「任务 + 分 P 名」分组，和重点、读位一致。
   解析照旧全容错：坏 JSON、条目缺 summary、points 不是数组，都当没存过。 */
function parseSumStore(raw){
  var empty = { v:1, parts:{} };
  if (!raw) return empty;
  try {
    var d = JSON.parse(raw);
    if (!d || typeof d !== "object") return empty;
    var src = d.parts;
    if (!src || typeof src !== "object"
        || Object.prototype.toString.call(src) === "[object Array]") return empty;
    var parts = {};
    for (var k in src){
      if (!Object.prototype.hasOwnProperty.call(src, k)) continue;
      var it = src[k], sm = it && it.summary;
      if (!sm || typeof sm !== "object") continue;
      parts[k] = {
        sig: typeof it.sig === "string" ? it.sig : "",
        at:  typeof it.at  === "string" ? it.at  : "",
        model: typeof it.model === "string" ? it.model : "",
        summary: {
          tldr: typeof sm.tldr === "string" ? sm.tldr : "",
          points: Object.prototype.toString.call(sm.points) === "[object Array]"
                  ? sm.points : [],
          concepts: Object.prototype.toString.call(sm.concepts) === "[object Array]"
                    ? sm.concepts : []
        }
      };
    }
    return { v:1, parts: parts };
  } catch(e){ return empty }
}

function sumOf(store, name){
  if (!store || !store.parts || !name) return null;
  return store.parts[name] || null;
}

/* 不改传进来的对象，理由和 withPos 一样：读-改-写中间抛了，半截数据不该流回去。 */
function withSum(store, name, item){
  var out = {};
  var src = (store && store.parts) || {};
  for (var k in src){
    if (Object.prototype.hasOwnProperty.call(src, k)) out[k] = src[k];
  }
  out[name] = item;
  return { v:1, parts: out };
}

/* ── 总结：读写与渲染 ─────────────────────────────── */

function cfgNow(){ return parseModel(lsRead(modelKey())) }

/* 当前这一篇。索引和 renderDoc 里 append 的顺序是同一个，因为两边都按
   d.parts 的顺序走。 */
function currentPart(){
  var full = buildFullText(S.transcript, S.task);
  var i = currentPartIndex();
  return { full: full, i: i, part: full.parts[i] || null };
}

/* 内存优先：隐私模式下 localStorage 只读，存进去的那份拿不回来，
   但这一页里点完就该立刻看得见。 */
function summaryFor(name){
  if (!name) return null;
  if (S.sumMem && S.sumMem[name]) return S.sumMem[name];
  return sumOf(parseSumStore(lsRead(sumKey(S.id))), name);
}

function saveSummaryItem(name, item){
  if (!name) return false;
  if (!S.sumMem) S.sumMem = {};
  S.sumMem[name] = item;
  if (!S.id) return false;
  var ok = lsWrite(sumKey(S.id),
                   JSON.stringify(withSum(parseSumStore(lsRead(sumKey(S.id))), name, item)));
  if (!ok && !S.sumWarned){
    S.sumWarned = true;
    notice("warn", "总结存不进浏览器",
      "这个浏览器不允许页面写 localStorage（隐私模式，或禁用了站点数据）。"
      + "这一份总结在这一页里照常看，但刷新就没了。");
  }
  return ok;
}

/* 按「这一段的时间」找 DOM，不用段序号。
   buildFullText 会多拆一次占位块、renderDoc 不会，两条路的段序号在某些数据
   上可能差一位；用时间找就不会指错地方。 */
function cueByStart(t, tol){
  if (t == null) return null;
  var secs = partSections();
  var sec = secs[currentPartIndex()];
  if (!sec) return null;
  var cues = sec.querySelectorAll(".cue");
  var best = null, bestD = Infinity;
  for (var i = 0; i < cues.length; i++){
    var st = cues[i]._start;
    if (st == null) continue;
    var d = Math.abs(st - t);
    if (d < bestD){ bestD = d; best = cues[i] }
  }
  return bestD <= (tol == null ? 1.5 : tol) ? best : null;
}

function errFix(kind){
  if (kind === "no_key" || kind === "auth")
    return "点顶栏「⚙ 模型」改 key，回来再点「重新生成」。";
  if (kind === "bad_base" || kind === "connect")
    return "点顶栏「⚙ 模型」检查服务地址；确认这台机器能访问它。";
  if (kind === "timeout")
    return "稍等一下再点「重新生成」；换一个更快的模型也有用。";
  if (kind === "rate")
    return "等几分钟再点，顺便看看账号额度。";
  if (kind === "parse")
    return "换个指令跟随好一些的模型再试——这一位没按格式输出。";
  // 「model」这一支是提供方那边出的错（5xx、或者 4xx 但不是 key/限流/地址）。
  // 原来这里返回空串，界面上就只剩一句错误原文，用户不知道下一步该做什么。
  if (kind === "model")
    return "按上面那句原因判断：服务那边出问题就过一会儿再试；"
         + "说不支持这个模型或参数就换一个。";
  return "";
}

/* 一个锚点按钮。`scope` 分隔「谁点了它」：总结和问答共用这一段，但两者各自
   记自己上一次点的是哪一个——混在一起的话，在总结里点过 01:03、再到问答里点
   01:03，会直接跳视频，而不是先回到那一段。 */
function anchorEl(r, part, scope){
  var ok = r.idx >= 0;
  var label = ok ? fmt(part.segments[r.idx].start)
                 : (r.t == null ? "无出处" : fmt(r.t));
  var b = el("button", "anchor" + (ok ? "" : " none"), label);
  b.type = "button";
  var key = (scope || "sum") + ":" + label + "@" + r.idx;
  if (!ok){
    b.title = r.t == null
      ? "这条要点没有时间戳：模型没给，或者这一篇本来就没有逐句时间"
      : ("模型给的 " + fmt(r.t) + " 在原文里找不到对应段落，所以不给跳转");
    return b;
  }
  b.title = "回到这一段（" + label + "），再点一次跳到视频这里";
  b.addEventListener("click", function(){
    var cue = cueByStart(r.t);
    if (!cue){
      // 页面里已经找不到这一段（多半是任务重跑、正文变了）：别假装能跳
      b.className = "anchor none";
      b.title = "现在的正文里找不到这一段了";
      return;
    }
    // 已经落在这儿了，再点一次才是「去视频里看」——一次点击不该同时干两件事
    if (S.sumAnchor === key){
      if (cue._jump) showPlayer(cue._meta, cue._start);
      return;
    }
    S.sumAnchor = key;
    focusCue(cue);
  });
  return b;
}

function paintSummaryItems(part, item){
  var body = $("#sumbody");
  var sm = item.summary || {};
  var rows = anchorsOf(sm.points, part.segments || [], !!part.hasTime);

  if (!part.hasTime){
    body.appendChild(el("div", "note",
      "这一篇没有逐句时间戳（必剪 ASR 或纯文本导出），要点无法定位到视频。"
      + "内容照常，只是点不了。"));
  }
  if (sm.tldr) body.appendChild(el("div", "s-tldr", sm.tldr));

  if (rows.length){
    var block = el("div", "s-block");
    block.appendChild(el("h4", null, "核心要点"));
    var ol = el("ol");
    rows.forEach(function(r){
      var li = el("li");
      li.appendChild(document.createTextNode(r.text));
      li.appendChild(anchorEl(r, part));
      ol.appendChild(li);
    });
    block.appendChild(ol);
    body.appendChild(block);
  }

  var concepts = sm.concepts || [];
  if (concepts.length){
    var cb = el("div", "s-block");
    cb.appendChild(el("h4", null, "关键概念"));
    var ul = el("ul", "cl");
    concepts.forEach(function(c){
      var li = el("li");
      li.appendChild(el("b", null, c.term || ""));
      li.appendChild(el("span", null, c.def || ""));
      ul.appendChild(li);
    });
    cb.appendChild(ul);
    body.appendChild(cb);
  }

  var bits = [];
  if (item.model) bits.push(item.model);
  if (item.at) bits.push(item.at + " 生成");
  if (bits.length) $("#sumstat").textContent = bits.join(" · ");
}

function renderSummary(){
  var box = $("#summary");
  if (!box || box.hidden) return;
  var body = $("#sumbody");
  clear(body);
  $("#sumstat").textContent = "";
  $("#sumregen").disabled = !!S.sumBusy;

  var cur = currentPart();
  if (!cur.part){
    $("#sumh").textContent = "总结";
    body.appendChild(el("div", "s-empty", "这一篇没有正文，没什么可总结的。"));
    return;
  }
  $("#sumh").textContent = "这一篇的总结"
    + (cur.part.label ? " · " + cur.part.label : "");

  if (S.sumBusy){
    body.appendChild(el("div", "s-empty",
      "正在让模型读这一篇…要点要一条条对回原文，可能要半分钟。"));
    return;
  }
  if (S.sumErr && S.sumErr.part === cur.part.name){
    var p = el("div", "s-err", S.sumErr.message);
    var fix = S.sumErr.fix || errFix(S.sumErr.kind);
    if (fix) p.appendChild(el("span", "fix", fix));
    body.appendChild(p);
    return;
  }

  var item = summaryFor(cur.part.name);
  if (!item){
    body.appendChild(el("div", "s-empty",
      "还没有这一篇的总结。点「重新生成」让它读一遍——这一篇有 "
      + (cur.part.segments || []).length + " 段。"));
    return;
  }
  if (item.sig !== summarySig(cur.part)){
    // 拿着旧总结配新字幕是最坏的一种：每条要点都点得动，但都指到别处。
    body.appendChild(el("div", "note",
      "字幕变过了（任务重跑过，或者这一篇被重新抓了一遍），这份总结对不上现在的"
      + "正文，所以不再显示。点「重新生成」重新做一份。"));
    $("#sumstat").textContent = "缓存已失效";
    return;
  }
  paintSummaryItems(cur.part, item);
}

/* 配好没有 → 按钮的样子。
   未配置时不是简单置灰：按钮文字变成「先配模型」，点它是去填设置而不是报错。
   灰着点不动的按钮只会让人猜「为什么」。
   也不加 aria-disabled：这个按钮**是可点的**（点了打开设置），对读屏器谎称
   禁用，等于把它从键盘和读屏用户的路径里摘掉。 */
function updateSumButton(){
  var btn = $("#sumbtn");
  if (!btn) return;
  var rd = modelReady(cfgNow());
  var hasDoc = !!(S.transcript && S.transcript.parts && S.transcript.parts.length);
  btn.className = "btn sm sumbtn" + (rd.ok ? "" : " off");
  btn.textContent = rd.ok ? "总结" : "先配模型";
  btn.disabled = !hasDoc || !!S.sumBusy;
  btn.title = rd.ok
    ? "把这一篇整理成要点。会调用设置里配的模型服务，按它那边花钱"
    : (rd.why + "。点这里去填。");
}

async function generateSummary(){
  var cur = currentPart();
  if (!cur.part || !S.id) return;

  var cfg = cfgNow();
  var rd = modelReady(cfg);
  if (!rd.ok){
    // 缺配置就别发请求：发出去也只会换回一句「key 无效」，白等一轮
    openModelBox(true);
    $("#modelnote").textContent = rd.why + "，填完再点总结。";
    return;
  }

  S.sumBusy = true; S.sumErr = null;
  updateSumButton();
  renderSummary();

  var src = buildSummarySource(cur.full, cur.i);
  var res = null, thrown = null;
  try {
    res = await call("POST", "/api/tasks/" + encodeURIComponent(S.id) + "/summary",
                     { model: cfg, text: src.text, hasTime: src.hasTime,
                       part: cur.part.name });
  } catch(e){ thrown = e }

  S.sumBusy = false;
  updateSumButton();

  if (thrown){
    S.sumErr = { part: cur.part.name, kind: "net",
      message: "请求发不出去：这一页和本机服务断开了（" + (thrown.message || thrown) + "）。",
      fix: "确认 asr-polish serve 还在跑，然后刷新页面重试。" };
    renderSummary();
    return;
  }
  if (!res.ok){
    S.sumErr = { part: cur.part.name, kind: res.data.kind || "model",
                 message: res.data.error || ("HTTP " + res.code) };
    renderSummary();
    return;
  }

  var item = { sig: summarySig(cur.part),
               at: mdStamp(new Date()),
               model: res.data.model || cfg.model,
               summary: res.data.summary || { tldr:"", points:[], concepts:[] } };
  saveSummaryItem(cur.part.name, item);
  S.sumAnchor = "";
  renderSummary();
}

/* ── 模型设置面板 ─────────────────────────────────── */

function openModelBox(open){
  var box = $("#modelbox");
  if (!box) return;
  if (open){
    var cfg = cfgNow();
    $("#mbase").value = cfg.base;
    $("#mname").value = cfg.model;
    $("#mkey").value = cfg.key;
    $("#modelnote").textContent = "";
  }
  box.hidden = !open;
}

$("#modelbtn").addEventListener("click", function(){
  openModelBox($("#modelbox").hidden);
});
$("#modelclose").addEventListener("click", function(){ openModelBox(false); });

$("#modelsave").addEventListener("click", function(){
  var cfg = {
    base:  $("#mbase").value.trim(),
    model: $("#mname").value.trim(),
    key:   $("#mkey").value.trim()
  };
  var rd = modelReady(cfg);
  if (!rd.ok){
    $("#modelnote").textContent = "还缺" + rd.missing.join("、") + "。";
    return;
  }
  var ok = lsWrite(modelKey(), JSON.stringify(cfg));
  $("#modelnote").textContent = ok
    ? "已保存，只在这个浏览器里。"
    : "存不进浏览器（隐私模式或禁用了站点数据）：这次能用，刷新就没了。";
  updateSumButton();
  updateAskButton();                 // 填完配置，两个入口一起活过来
  if (ok) setTimeout(function(){ openModelBox(false) }, 700);
});

$("#modelclear").addEventListener("click", function(){
  lsDel(modelKey());
  $("#mbase").value = ""; $("#mname").value = ""; $("#mkey").value = "";
  $("#modelnote").textContent = "已清除。总结和提问都会回到「先配模型」。";
  updateSumButton();
  updateAskButton();
});

$("#sumbtn").addEventListener("click", function(){
  var rd = modelReady(cfgNow());
  if (!rd.ok){
    // 点了不是报错，是直接把要填的东西摆出来
    openModelBox(true);
    $("#modelnote").textContent = rd.why + "，填完再点总结。";
    return;
  }
  var box = $("#summary");
  var wasOpen = !box.hidden;
  box.hidden = wasOpen;
  if (wasOpen) return;
  S.sumErr = null;
  S.sumAnchor = "";
  renderSummary();
  var cur = currentPart();
  // 有缓存就直接看，没缓存才真去调模型——打开就花钱是最不该有的默认
  if (cur.part && !summaryFor(cur.part.name)) generateSummary();
});

$("#sumclose").addEventListener("click", function(){ $("#summary").hidden = true });
$("#sumregen").addEventListener("click", function(){ generateSummary() });


/* ── 单篇问答（学习工作台第 2 层）───────────────────── */
/* 比第 1 层多一样东西：上下文。所以除了「回答必须带出处」，还要回答三个问题——
   历史存哪、带几轮过去、字幕变了怎么办。

   规矩和第 1 层一样：**没有出处的话不当权威**。模型给的时间一律拿去和真实的
   段首核对，对不上的灰着、点不动；一条都对不上，就在回答下面明说。

   历史只存浏览器，按「任务 + 分 P 名」分组，和划重点、读位、总结一个路数。 */

var QA_PREFIX = "asr-polish:qa:v1:";
/* 和服务端 qa.MAX_ROUNDS 对齐。两边各裁一次是有意的：前端裁是为了请求别太大，
   服务端裁是为了账单别失控——前端是可以被绕过去的，账单不是。 */
var QA_ROUNDS = 6;
/* 示例问题。空对话最难开头，给三个能直接点的，省掉「我该问什么」这一步。 */
var QA_SUG = ["这篇主要讲了什么？", "作者给的步骤是什么？", "他最强调的一点是什么？"];

function qaKey(taskId){ return QA_PREFIX + taskId }

/* 把一段回答切成「文字 / 时间引用」两种片段。纯函数。
   只认方括号里的 [分:秒]：裸的 12:34 在回答里可能是比例、比数、页码，误伤的
   代价是给一句普通的话配了个跳转；而提示词已经要求模型用方括号。
   秒数换算复用 parseStamp——和总结那边同一套，不另写一份。 */
var CITE_RE = /\[(\d{1,3}(?::\d{2}){1,2})\]/g;

function splitCites(text){
  var out = [], last = 0, m;
  text = String(text == null ? "" : text);
  CITE_RE.lastIndex = 0;
  while ((m = CITE_RE.exec(text)) !== null){
    if (m.index > last) out.push({ text: text.slice(last, m.index) });
    out.push({ cite:true, raw:m[0], t:parseStamp(m[1]) });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last) });
  return out;
}

/* 回答 → 一行行「片段」。核对就在这一步做完：这条引用对得上第几段，这里定。
   纯函数，测试直接断言 idx；DOM 由 answerEl 拼。

   这一篇本来没有逐句时间（必剪 ASR / 纯文本）时，模型就算给了时间也一律
   idx = -1——它不可能知道，给了就是编的。 */
function answerLines(answer, segs, hasTime, tol){
  var out = [];
  String(answer == null ? "" : answer).split(/\r?\n/).forEach(function(line){
    line = line.replace(/\s+$/, "");
    if (!line){ out.push({ blank:true, runs:[] }); return }
    var runs = splitCites(line).map(function(r){
      if (!r.cite) return { text: r.text };
      var idx = (hasTime && r.t != null) ? matchStamp(segs, r.t, tol) : -1;
      return { cite:true, raw:r.raw, t:r.t, idx:idx };
    });
    out.push({ blank:false, runs:runs });
  });
  return out;
}

/* 对话的存法：{v:1, parts:{"<分 P 名>": {sig, at, model, msgs:[{q,a,at}]}}}
   解析态度和 parseSumStore 一样：坏 JSON、条目缺 msgs、msgs 不是数组，都当
   没存过；单条脏数据只丢它自己，不能一条坏数据废掉整段会话。 */
function parseQaStore(raw){
  var empty = { v:1, parts:{} };
  if (!raw) return empty;
  try {
    var d = JSON.parse(raw);
    if (!d || typeof d !== "object") return empty;
    var src = d.parts;
    if (!src || typeof src !== "object"
        || Object.prototype.toString.call(src) === "[object Array]") return empty;
    var parts = {};
    for (var k in src){
      if (!Object.prototype.hasOwnProperty.call(src, k)) continue;
      var it = src[k];
      if (!it || typeof it !== "object") continue;
      var msgs = it.msgs;
      if (Object.prototype.toString.call(msgs) !== "[object Array]") continue;
      var rows = [];
      for (var i = 0; i < msgs.length; i++){
        var m = msgs[i];
        if (!m || typeof m.q !== "string" || typeof m.a !== "string") continue;
        if (!m.q.replace(/\s/g, "") || !m.a.replace(/\s/g, "")) continue;
        rows.push({ q: m.q, a: m.a, at: typeof m.at === "string" ? m.at : "" });
      }
      if (!rows.length) continue;
      parts[k] = {
        sig: typeof it.sig === "string" ? it.sig : "",
        at:  typeof it.at  === "string" ? it.at  : "",
        model: typeof it.model === "string" ? it.model : "",
        msgs: rows
      };
    }
    return { v:1, parts: parts };
  } catch(e){ return empty }
}

function qaOf(store, name){
  if (!store || !store.parts || !name) return null;
  return store.parts[name] || null;
}

/* 读-改-写不碰传进来的对象：中间抛了，半截数据不该流回去（同 withPos / withSum）。 */
function withQa(store, name, item){
  var out = {};
  var src = (store && store.parts) || {};
  for (var k in src){
    if (Object.prototype.hasOwnProperty.call(src, k)) out[k] = src[k];
  }
  out[name] = item;
  return { v:1, parts: out };
}

/* 清空只要「去掉这一篇」。别的篇的对话是别的上下文，一并清掉等于顺手把人家
   在第二篇问的东西删了，而这个按钮写的是「清空对话」，不是「清空全部」。 */
function withoutQa(store, name){
  var out = {};
  var src = (store && store.parts) || {};
  for (var k in src){
    if (Object.prototype.hasOwnProperty.call(src, k) && k !== name) out[k] = src[k];
  }
  return { v:1, parts: out };
}

/* ── 问答：读写与渲染 ─────────────────────────────── */

function qaItemFor(name){
  if (!name) return null;
  if (S.qaMem && S.qaMem[name]) return S.qaMem[name];
  return qaOf(parseQaStore(lsRead(qaKey(S.id))), name);
}

function saveQaItem(name, item){
  if (!name) return false;
  if (!S.qaMem) S.qaMem = {};
  S.qaMem[name] = item;
  if (!S.id) return false;
  var ok = lsWrite(qaKey(S.id),
                   JSON.stringify(withQa(parseQaStore(lsRead(qaKey(S.id))), name, item)));
  if (!ok && !S.qaWarned){
    S.qaWarned = true;
    notice("warn", "对话存不进浏览器",
      "这个浏览器不允许页面写 localStorage（隐私模式，或禁用了站点数据）。"
      + "这一段对话在这一页里照常看，但刷新就没了。");
  }
  return ok;
}

/* 发给问答的正文：本体和总结一模一样（同一个 buildSummarySource：按行对齐、行首补
   [分:秒]），只摘掉它给总结加的那句「所有要点的 t 请写成 null」。

   为什么摘：问答这边没有「要点」这个概念，那句话混在正文里会被当成原文的一部分。
   「这一篇没有逐句时间」这件事由服务端的提示词说——说的是同一件事，但用的是问答
   的说法。不复制 buildSummarySource 的第二份：对齐规则只有一处，改也只改一处。 */
var SUM_NO_TIME_HEAD = /^（这一篇没有逐句时间戳[^）]*）\n\n/;

function buildAskSource(full, i){
  var src = buildSummarySource(full, i);
  if (!src.hasTime) src.text = src.text.replace(SUM_NO_TIME_HEAD, "");
  return src;
}

/* 一条回答。返回节点；顺带把「有没有有效出处」这件事办掉。
   没有时间戳的篇不画灰字锚点——满屏「无出处」会让人以为功能坏了，而真原因
   在问答区顶上那条常驻说明里。 */
function answerEl(text, part){
  var ans = el("div", "a");
  var segs = part.segments || [];
  var lines = answerLines(text, segs, !!part.hasTime);
  var valid = 0;
  lines.forEach(function(ln){
    if (ln.blank) return;               // 空行只当段落间隔，段间距已经有
    var p = el("p");
    ln.runs.forEach(function(r){
      if (!r.cite){ p.appendChild(document.createTextNode(r.text)); return }
      if (!part.hasTime){
        p.appendChild(document.createTextNode(r.raw));   // 原样留着方括号，不加锚点
        return;
      }
      if (r.idx >= 0) valid++;
      p.appendChild(anchorEl(r, part, "qa"));
    });
    ans.appendChild(p);
  });
  if (part.hasTime && !valid){
    // 摆得和有出处的回答一样可信，是这个功能最不该做的事
    ans.appendChild(el("div", "note", "本次回答没有可验证的出处，仅供参考。"));
  }
  return ans;
}

function renderSuggestions(show){
  var box = $("#qasug");
  clear(box);
  box.hidden = !show;
  if (!show) return;
  QA_SUG.forEach(function(s){
    var b = el("button", "sug", s);
    b.type = "button";
    b.addEventListener("click", function(){ askQuestion(s) });
    box.appendChild(b);
  });
}

function renderQa(){
  var box = $("#qa");
  if (!box || box.hidden) return;
  var list = $("#qalist");
  clear(list);
  $("#qastat").textContent = "";
  updateAskButton();

  var cur = currentPart();
  if (!cur.part){
    $("#qah").textContent = "问答";
    list.appendChild(el("div", "qempty", "这一篇没有正文，问不了。"));
    renderSuggestions(false);
    return;
  }
  $("#qah").textContent = "这一篇的问答"
    + (cur.part.label ? " · " + cur.part.label : "");

  // 没有逐句时间这件事写在顶上、常驻：它是这一篇的前提，不是每条回答的脚注
  var note = $("#qanote");
  clear(note);
  note.hidden = !!cur.part.hasTime;
  if (!cur.part.hasTime){
    note.appendChild(document.createTextNode(
      "这一篇没有逐句时间戳，回答无法定位到视频（必剪 ASR 和纯文本导出会这样）。"
      + "内容照常答，只是不给时间锚点。"));
  }

  var item = qaItemFor(cur.part.name);
  var rounds = (item && item.msgs) || [];
  // 拿着旧对话配新字幕，比没有对话更糟：问题问的是旧内容，回答的引用全指错地方
  if (item && item.sig !== summarySig(cur.part)){
    list.appendChild(el("div", "note",
      "字幕变过了（任务重跑过，或者这一篇被重新抓了一遍），这一段对话对不上现在的"
      + "正文，所以不再显示。再问一句就是新的，起点从这里开始。"));
    rounds = [];
    $("#qastat").textContent = "缓存已失效";
  }

  rounds.forEach(function(r){
    var t = el("div", "turn");
    var q = el("p", "q");
    q.appendChild(el("b", null, "问"));
    q.appendChild(document.createTextNode(r.q));
    t.appendChild(q);
    t.appendChild(answerEl(r.a, cur.part));
    list.appendChild(t);
  });

  if (S.qaBusy){
    list.appendChild(el("div", "qbusy",
      "正在问模型…它要把这一篇读完再回答，可能要半分钟。"));
  }
  if (S.qaErr && S.qaErr.part === cur.part.name){
    var p = el("div", "qerr", S.qaErr.message);
    var fix = S.qaErr.fix || errFix(S.qaErr.kind);
    if (fix) p.appendChild(el("span", "fix", fix));
    list.appendChild(p);
  }

  var empty = !rounds.length && !S.qaBusy
           && !(S.qaErr && S.qaErr.part === cur.part.name);
  if (empty){
    list.appendChild(el("div", "qempty",
      "还没问过这一篇。可以点下面这几个，也可以自己写一句——回答里的每条事实"
      + "都会标出原文时间。"));
  }
  renderSuggestions(empty);

  if (item && item.model) $("#qastat").textContent = item.model;
  // 新问题在下面：滚到底，别让人以为没反应
  list.scrollTop = list.scrollHeight;
}

/* 配好没有 → 提问按钮的样子。和总结按钮同一套做法：文案变「先配模型」、虚线框，
   点它不是报错，是去填设置。理由也一样——灰着点不动的按钮只会让人猜「为什么」。 */
function updateAskButton(){
  var btn = $("#qabtn");
  if (!btn) return;
  var rd = modelReady(cfgNow());
  btn.className = "btn sm" + (rd.ok ? " primary" : " off");
  btn.textContent = rd.ok ? "提问" : "先配模型";
  btn.disabled = !!S.qaBusy;
  btn.title = rd.ok
    ? "就这一篇提问。会调用设置里配的模型服务，按它那边花钱"
    : (rd.why + "。点这里去填。");
  var miss = $("#qamiss");
  if (miss){
    miss.hidden = rd.ok;
    miss.textContent = rd.ok ? "" : ("缺" + rd.missing.join("、"));
    miss.title = rd.why;
  }
}

/* 顶栏那个入口：没有正文时点不动，有正文才有得问。 */
function updateAskEntry(){
  var btn = $("#askbtn");
  if (!btn) return;
  btn.disabled = !(S.transcript && S.transcript.parts
                   && S.transcript.parts.length);
}

function openQa(open){
  var box = $("#qa");
  if (!box) return;
  box.hidden = !open;
  if (!open) return;
  S.qaErr = null;
  renderQa();
  updateAskButton();
  // 顶栏是 sticky 的：在正文中间点「问答」，不滚一下等于什么都没发生
  if (box.scrollIntoView) box.scrollIntoView({ block: "start", behavior: "smooth" });
}

async function askQuestion(text){
  var q = String(text == null ? "" : text).replace(/^\s+|\s+$/g, "");
  if (!q || S.qaBusy) return;

  var cur = currentPart();
  if (!cur.part || !S.id) return;

  var cfg = cfgNow();
  var rd = modelReady(cfg);
  if (!rd.ok){
    // 缺配置就别发请求：发出去只会换回一句「key 无效」，白等一轮
    openModelBox(true);
    $("#modelnote").textContent = rd.why + "，填完再提问。";
    return;
  }

  var name = cur.part.name;
  var gen = S.gen;                 // 换任务了的话，这一轮的答案不属于新页面
  var prev = qaItemFor(name);
  // 只接得上同一份字幕的历史；sig 对不上就当新对话（和渲染那边同一条规则）
  var rounds = (prev && prev.sig === summarySig(cur.part)) ? prev.msgs.slice() : [];

  S.qaBusy = true; S.qaErr = null;
  renderQa();

  // 正文仍然出自第 1 层：buildAskSource 把纯文本按行对齐回全文，在行首补 [mm:ss]。
  // 模型只有自己看见时间，才可能抄对。
  var src = buildAskSource(cur.full, cur.i);
  var res = null, thrown = null;
  try {
    res = await call("POST", "/api/tasks/" + encodeURIComponent(S.id) + "/ask",
                     { model: cfg, text: src.text, hasTime: src.hasTime,
                       question: q, history: rounds.slice(-QA_ROUNDS),
                       part: name });
  } catch(e){ thrown = e }

  if (S.gen !== gen) return;       // 换任务了：这份回答不属于现在这一篇，连状态都别动
  S.qaBusy = false;

  if (thrown){
    S.qaErr = { part: name, kind: "net",
      message: "请求发不出去：这一页和本机服务断开了（" + (thrown.message || thrown) + "）。",
      fix: "确认 asr-polish serve 还在跑，然后刷新页面重试。" };
    renderQa();
    return;
  }
  if (!res.ok){
    // 提供方给的原因原样带出来：key 错、限流、地址不通，处理方式各不相同
    S.qaErr = { part: name, kind: res.data.kind || "model",
                message: res.data.error || ("HTTP " + res.code) };
    renderQa();
    return;
  }

  var answer = res.data.answer || "";
  var item = { sig: summarySig(cur.part),
               at: mdStamp(new Date()),
               model: res.data.model || cfg.model,
               msgs: rounds.concat([{ q: q, a: answer, at: mdStamp(new Date()) }]) };
  saveQaItem(name, item);
  renderQa();
}

function clearQa(){
  var cur = currentPart();
  if (!cur.part) return;
  if (S.qaMem) delete S.qaMem[cur.part.name];
  if (!S.id) return;
  lsWrite(qaKey(S.id),
          JSON.stringify(withoutQa(parseQaStore(lsRead(qaKey(S.id))),
                                   cur.part.name)));
  S.qaErr = null;
  renderQa();
}

$("#askbtn").addEventListener("click", function(){
  var box = $("#qa");
  if (!box) return;
  if (box.hidden && !(S.transcript && S.transcript.parts
                      && S.transcript.parts.length)) return;
  openQa(!!box.hidden);
});

$("#qaclose").addEventListener("click", function(){ openQa(false) });
$("#qaclear").addEventListener("click", clearQa);

$("#qaf").addEventListener("submit", function(ev){
  ev.preventDefault();
  // 正在问的时候按回车仍会提交（提交按钮 disabled 拦不住这条路径），
  // 而输入框在下面会被清空——等于把用户刚打的那句话吃掉。先挡一下。
  if (S.qaBusy) return;
  // 先判配置，再判问题。顺序反过来的话，没配模型时点「先配模型」而输入框是空的，
  // 会走到「问题还没写」那一支——按钮明明写着去配模型，点了却没反应。
  var rd = modelReady(cfgNow());
  if (!rd.ok){
    openModelBox(true);
    $("#modelnote").textContent = rd.why + "，填完再提问。";
    return;
  }
  var q = $("#qain").value.replace(/^\s+|\s+$/g, "");
  if (!q){ $("#qain").focus(); return }
  $("#qain").value = "";
  askQuestion(q);
});


/* ── 断点续读 ────────────────────────────────────── */
/* 记的是「哪一篇 + 该篇第几段」，不是滚动像素：换个窗口大小像素全变，段落
   序号不变。仍然只存 localStorage，按任务 id 分块，块里再按分 P 名分开——
   这样在第二篇读一半关掉，也不会把第一篇的位置顶掉。 */
var POS_PREFIX = "asr-polish:pos:v1:";

function posKey(taskId){ return POS_PREFIX + taskId; }

/* 解析整块记录。JSON 坏了、格式不是我们写的、段落号不是非负整数，一律当成
   「没存过」——这种时候必须静默退回顶部，不能因为一行脏数据让页面白屏。 */
function parsePos(raw){
  var empty = { v: 1, parts: {} };
  if (!raw) return empty;
  try {
    var d = JSON.parse(raw);
    if (!d || typeof d !== "object") return empty;
    var src = d.parts;
    if (!src || typeof src !== "object"
        || Object.prototype.toString.call(src) === "[object Array]") return empty;
    var parts = {};
    for (var k in src){
      if (!Object.prototype.hasOwnProperty.call(src, k)) continue;
      var n = src[k];
      if (typeof n === "number" && isFinite(n) && n >= 0 && Math.floor(n) === n){
        parts[k] = n;
      }
    }
    return { v: 1, parts: parts };
  } catch(e){ return empty }
}

function posOf(store, partName){
  if (!store || !store.parts || !partName) return null;
  var n = store.parts[partName];
  return typeof n === "number" ? n : null;
}

/* 不改传进来的对象：读-改-写之间如果哪一步抛了，半截数据不该流回去。 */
function withPos(store, partName, idx){
  var out = {};
  var src = (store && store.parts) || {};
  for (var k in src){
    if (Object.prototype.hasOwnProperty.call(src, k)) out[k] = src[k];
  }
  out[partName] = idx;
  return { v: 1, parts: out };
}

/* 「视口顶部那一屏」是哪一段：给一串 rect.top，返回第一个完整落在界下面的。

   不用「离界最近」：`.cue` 有 scroll-margin-top:96px，滚到位后目标段停在界
   下方 27px，而它前面那段的下沿离界只有 25px——按距离算会稳定选中上一段，
   每次续读都差一段。实测 1400×900：界 69、目标 96、上一段 44。
   判据换成「top >= 界」，语义也更直白：header 下面第一条完整的段落。
   全滚过去了（没有一段在界下面）就取最后一段。 */
function pickCueIndex(tops, boundary){
  if (!tops || !tops.length) return -1;
  for (var i = 0; i < tops.length; i++){
    if (tops[i] >= boundary) return i;
  }
  return tops.length - 1;
}

/* 位置存不进去（隐私模式）不弹提示：读位这件事不值得为它打断一次阅读，
   页面照旧能用，只是刷新后从头开始。返回值给总结那层用——总结是要花钱的，
   「存不住」必须说一声。 */
function lsRead(k){ try { return localStorage.getItem(k); } catch(e){ return null } }
function lsWrite(k, v){
  try { localStorage.setItem(k, v); return true; } catch(e){ return false }
}

function partNameOf(sec){
  var c = sec.querySelector(".cue");
  return (c && c._partName) || "";
}

/* header 是吸顶的，界不能拿 0 当——那样「顶部那段」会少算一屏。 */
function visibleTop(){
  var h = document.querySelector("header");
  var b = (h && h.getBoundingClientRect) ? h.getBoundingClientRect().bottom : 0;
  return (b > 0 ? b : 0) + 6;
}

function currentPartIndex(){
  var secs = partSections();
  for (var i = 0; i < secs.length; i++){
    if (!secs[i].hidden) return i;
  }
  return S.part;
}

/* 只写一处，省得两处各拼一遍 key 与 part 名。 */
function writePos(idx){
  if (!S.id) return;
  var sec = partSections()[currentPartIndex()];
  if (!sec) return;
  var name = partNameOf(sec);
  if (!name) return;
  lsWrite(posKey(S.id),
          JSON.stringify(withPos(parsePos(lsRead(posKey(S.id))), name, idx)));
}

/* 只有人真的滚动过才记。页面加载时浏览器自己也会发 scroll（恢复滚动位置、
   字体或图片把内容撑高），不设这道闸的话，第一次打开就会顺手写下一个 0，
   下次进来白弹一句「已回到上次读到的位置」——位置是对的，提示是噪音。 */
function savePos(){
  if (!S.posArmed) return;
  var sec = partSections()[currentPartIndex()];
  if (!sec || sec.hidden) return;
  var cues = sec.querySelectorAll(".cue");
  if (!cues.length) return;
  var tops = [];
  for (var i = 0; i < cues.length; i++) tops.push(cues[i].getBoundingClientRect().top);
  var at = pickCueIndex(tops, visibleTop());
  if (at < 0) return;
  writePos(at);
}

/* 停下 300ms 再写一次。每次 scroll 都写等于拿 localStorage 当草稿纸。 */
var posTimer = null;
function onScroll(){
  if (!S.posArmed && (window.scrollY || 0) > 20) S.posArmed = true;
  if (!S.posArmed) return;
  if (posTimer) clearTimeout(posTimer);
  posTimer = setTimeout(savePos, 300);
}
window.addEventListener("scroll", onScroll, { passive: true });
/* 关标签 / 刷新：上面的节流多半还没到点，这里补最后一次。pagehide 比
   unload 可靠——bfcache 里也会触发。 */
window.addEventListener("pagehide", savePos);
window.addEventListener("beforeunload", savePos);

var resumeTimer = null;

function hideResume(){
  var box = $("#resume");
  if (box) box.hidden = true;
  if (resumeTimer){ clearTimeout(resumeTimer); resumeTimer = null; }
}

function showResume(page){
  var box = $("#resume");
  if (!box) return;
  var tx = $("#resumetx");
  if (tx) tx.textContent = "已回到上次读到的位置" + (page ? "（P" + page + "）" : "");
  box.hidden = false;
  if (resumeTimer) clearTimeout(resumeTimer);
  resumeTimer = setTimeout(hideResume, 9000);     // 读几行就自己让开
}

$("#resumetop").addEventListener("click", function(){
  hideResume();
  // 「从头读」要立刻落库：smooth 滚动是异步的，等 scroll 事件算出来的还是
  // 点击前那个位置，等于什么也没改。
  writePos(0);
  if (window.scrollTo){
    try { window.scrollTo({ top: 0, behavior: "smooth" }); return; } catch(e){}
  }
  if (document.documentElement) document.documentElement.scrollTop = 0;
});

/* 打开页面时接上上次的位置。找不到那一篇、序号超过当前篇的段数（重跑后
   字幕变短），都静默从头开始——位置只是方便，不是数据，不能因为它让页面
   报错，也不能滚到一片空白里。 */
function restorePos(){
  if (!S.id) return false;
  var store = parsePos(lsRead(posKey(S.id)));
  var secs = partSections();
  for (var i = 0; i < secs.length; i++){
    var name = partNameOf(secs[i]);
    if (!name) continue;
    var at = posOf(store, name);
    if (at == null) continue;
    var cues = secs[i].querySelectorAll(".cue");
    if (at >= cues.length) continue;
    selectPart(i, false);                 // 位置在别的篇里，先切过去再滚
    focusCue(cues[at]);                   // 复用搜索那套：滚到中间 + 闪一下
    showResume(secs.length > 1 ? (cues[at]._page || "") : "");
    return true;
  }
  return false;
}

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

function runSearch(q, noJump){
  q = (q || "").trim();
  S.marks = []; S.cur = -1;
  // 只在当前显示的分 P 里搜：藏起来的段落既看不见也滚不到，算进计数只会
  // 让人以为「搜到了却跳不过去」。藏起来那一篇的旧高亮也要清掉——留着的话，
  // 下面按 mark.hit 收集命中时会把上一篇的一起数进来，计数虚高，按 ↓ 还会
  // 顺着看不见的命中往前跳，一路把页面弹回上一篇。
  var vis = document.querySelectorAll("#doc .part:not([hidden]) .cue");
  var hid = document.querySelectorAll("#doc .part[hidden] .cue");
  for (var i = 0; i < vis.length; i++){
    paint(vis[i], q);
    vis[i].classList.remove("flash");
  }
  for (var j = 0; j < hid.length; j++) paint(hid[j], "");
  if (!q){ $("#count").textContent = ""; return; }
  S.marks = Array.prototype.slice.call(
    document.querySelectorAll("#doc .part:not([hidden]) mark.hit"));
  if (!S.marks.length){ $("#count").textContent = "0 处"; return; }
  // 换篇后的重搜不抢滚动位置：人是在读这一篇，不是要跳到第一篇的第一个命中。
  if (noJump){ $("#count").textContent = S.marks.length + " 处"; return; }
  jump(0);
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

/* ── 夜间模式 ────────────────────────────────────── */
/* 只存一个 "light" / "dark"。和重点、读位一样留在本机，没有账号也就没有
   同步。切换动作只碰 data-theme 这一个属性——颜色全在 CSS 变量里，这边
   一个具体色值都不写，所以「切了颜色但布局变了」这种事故不存在。 */
var THEME_KEY = "asr-polish:theme:v1";

function themeKey(){ return THEME_KEY }

/* 没存过就跟随系统；存过就用存的。认不出的值当「没存过」——脏数据不该让
   页面卡在两套都不对的状态里。 */
function themeFrom(stored, sysDark){
  if (stored === "dark" || stored === "light") return stored;
  return sysDark ? "dark" : "light";
}

function nextTheme(cur){ return cur === "dark" ? "light" : "dark" }

/* 按钮显示的是「点一下会变成什么」，不是当前状态：现在是白天就显示 🌙。
   为了让这件事不用猜，title 与 aria-label 都把话说完整。 */
function themeLabel(t){
  if (t === "dark"){
    return { text: "☀️ 白天", title: "当前是夜间模式，点一下切回白天" };
  }
  return { text: "🌙 夜间", title: "当前是白天模式，点一下切到夜间（深色底，夜里不刺眼）" };
}

function sysDark(){
  try {
    return !!(window.matchMedia
              && window.matchMedia("(prefers-color-scheme: dark)").matches);
  } catch(e){ return false }
}

function themeNow(){
  var root = document.documentElement;
  var v = (root && root.getAttribute) ? root.getAttribute("data-theme") : null;
  return v === "dark" ? "dark" : "light";
}

function applyTheme(t){
  var root = document.documentElement;
  if (root && root.setAttribute) root.setAttribute("data-theme", t);
  var btn = $("#themebtn");
  if (!btn) return;
  var lb = themeLabel(t);
  btn.textContent = lb.text;
  btn.title = lb.title;
  if (btn.setAttribute) btn.setAttribute("aria-label", lb.title);
}

/* head 里那段已经定过一次（为了不闪），这里再定一次是为了让按钮文字跟上，
   顺带兜住「head 那段被 CSP 之类挡掉」的情况。两处算同一个结果，重复无害。 */
function initTheme(){
  applyTheme(themeFrom(lsRead(THEME_KEY), sysDark()));
}

$("#themebtn").addEventListener("click", function(){
  var t = nextTheme(themeNow());
  applyTheme(t);
  lsWrite(THEME_KEY, t);      // 只有手动切才落库：跟随系统时系统变了还得跟着变
});

initTheme();

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
  fmt: fmt,
  buildFullText: buildFullText,
  buildPlainText: buildPlainText,
  buildFullMarkdown: buildFullMarkdown,
  fullFileName: fullFileName,
  fullScope: fullScope,
  posKey: posKey,
  parsePos: parsePos,
  posOf: posOf,
  withPos: withPos,
  pickCueIndex: pickCueIndex,
  themeKey: themeKey,
  themeFrom: themeFrom,
  nextTheme: nextTheme,
  themeLabel: themeLabel,
  modelKey: modelKey,
  sumKey: sumKey,
  parseModel: parseModel,
  modelReady: modelReady,
  buildSummarySource: buildSummarySource,
  parseStamp: parseStamp,
  matchStamp: matchStamp,
  anchorsOf: anchorsOf,
  summarySig: summarySig,
  parseSumStore: parseSumStore,
  sumOf: sumOf,
  withSum: withSum,
  qaKey: qaKey,
  splitCites: splitCites,
  answerLines: answerLines,
  buildAskSource: buildAskSource,
  parseQaStore: parseQaStore,
  qaOf: qaOf,
  withQa: withQa,
  withoutQa: withoutQa
};
})();
</script>
</body></html>
"""


def html() -> str:
    """阅读页。纯静态字符串，不依赖任何外部资源。

    唯一的外部引用是 B站官方播放器 iframe，而且只在点了句子时才赋 src：
    不点就一个请求都不发，离线/内网读文字稿不受影响。
    """
    return _HTML
