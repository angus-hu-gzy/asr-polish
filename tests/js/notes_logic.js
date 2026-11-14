// 笔记库页面（/notes）的取数器：把 notes_page.py 里那段 <script> 灌进极简 DOM
// 桩里跑，把纯函数的结果当 JSON 打出来，断言写在 tests/test_notes.py（Python 侧）。
//
// 为什么值得这么绕：这一页的全部逻辑就是「把服务端给的清单画成两张表」，而最容易
// 出错的地方都不在字符串里——`[[概念]]` 和 `[00:00](链接)` 挨在一行时谁先替换、
// 没有秒数的出处该不该画出时间、空库和搜不到要说什么话。这些只有真跑一遍才知道。
const fs = require("fs");

function fakeNode(tag) {
  const cls = new Set();
  return {
    nodeType: 1,
    tagName: String(tag || "div").toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    classList: {
      add: (...c) => c.forEach((x) => cls.add(x)),
      remove: (...c) => c.forEach((x) => cls.delete(x)),
      contains: (c) => cls.has(c),
    },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {},
    setAttribute() {},
    getAttribute() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    textContent: "",
    innerHTML: "",
    value: "",
    hidden: false,
    disabled: false,
  };
}

function makeEnv(src, search) {
  const doc = {
    body: fakeNode("body"),
    querySelector: () => fakeNode("div"),
    querySelectorAll: () => [],
    createElement: (t) => fakeNode(t),
    addEventListener() {},
  };
  const win = {
    document: doc,
    // fetch 永远 pending：这一层只验纯函数，不许真去请求。页面自己在加载时会
    // 调一次 loadAll()，桩里让它挂着就行。
    fetch: () => new Promise(() => {}),
    location: { search: search || "" },
    setTimeout: () => 0,
    clearTimeout: () => {},
    console,
  };
  win.window = win;
  const run = new Function(
    "window", "document", "location", "fetch", "setTimeout", "clearTimeout", "console",
    src
  );
  run(win, doc, win.location, win.fetch, win.setTimeout, win.clearTimeout, console);
  return win;
}

function main() {
  const src = fs.readFileSync(process.argv[2], "utf8");
  const api = makeEnv(src, "").__notes;
  if (!api) throw new Error("笔记页没有挂出 window.__notes，测试钩子被删了？");

  // 一篇正常的、一篇必剪（没有逐句时间戳）的
  const videos = [
    {
      file: "带时间的一篇 · P1.md",
      title: "带时间的一篇",
      bvid: "BV1TEST00001",
      label: "P1",
      date: "2026-11-07",
      at: "2026-11-07 13:00",
      task_id: "T569ceb995d",
      part_index: 0,
      stale: false,
      body: [
        "",
        "# 带时间的一篇",
        "",
        "## 要点",
        "",
        "- 讲了 <b>阈值</b> 怎么调 [00:00](http://h/reader?task=T&part=0&t=0)",
        "- 结尾提到脚本放同一目录 [00:30](http://h/reader?task=T&part=0&t=30)",
        "",
        "## 概念",
        "",
        "- [[阈值]] — 触发判定的那条界线 [00:00](http://h/reader?task=T&part=0&t=0)",
        "- [[限流]] — 单位时间里只放行固定数量",
        "",
      ].join("\n"),
    },
    {
      file: "必剪转写的一篇 · P2.md",
      title: "必剪转写的一篇",
      bvid: "BV1TEST00001",
      label: "P2",
      date: "2026-11-06",
      task_id: "T569ceb995d",
      part_index: 1,
      stale: true,
      stale_why: "字幕变了（5 段 → 6 段）",
      body: [
        "# 必剪转写的一篇",
        "",
        "> 这一篇没有逐句时间戳（必剪 ASR 或纯文本导出），概念无法定位到视频。",
        "",
        "## 要点",
        "",
        "- 只按行拆开",
        "",
      ].join("\n"),
    },
  ];

  const concepts = [
    {
      name: "阈值",
      first: "2026-11-07",
      define: "触发判定的那条界线。",
      occ: [
        { title: "带时间的一篇", label: "P1", task_id: "T", part_index: 0, sec: 0 },
        { title: "必剪转写的一篇", label: "P2", task_id: "T", part_index: 1, sec: null },
      ],
      file: "阈值.md",
      hits: ["概念名"],
    },
  ];

  const out = {
    // ── 行内三种东西 ──────────────────────────────
    // `[[阈值]] — 定义 [00:00](url)`：wikilink 必须先替换，否则普通链接那条
    // 正则会把 `[[阈值]] — 定义 [00:00` 整段当成链接文字。
    inlineMixed: api.inline("[[阈值]] — 触发判定的那条界线 [00:00](http://h/r?t=0)"),
    // 昵称写法：文件名是 `TCP_IP`，概念名是 `TCP/IP`。链接要跳到按概念名搜的
    // 结果页——拿文件名去搜是搜不到的。
    inlineAlias: api.inline("[[TCP_IP|TCP/IP]] — 传输层协议族"),
    inlineEsc: api.inline('<script>alert(1)</script> & "引号"'),
    inlinePlain: api.inline("一句没有链接的话"),

    // ── 正文按固定结构画 ──────────────────────────
    body: api.renderBody(videos[0].body),
    bodyNotime: api.renderBody(videos[1].body),
    bodyEmpty: api.renderBody(""),

    // ── 视频笔记 ─────────────────────────────────
    videosHtml: api.renderVideos(videos, ""),
    videosEmpty: api.renderVideos([], ""),
    videosEmptySearch: api.renderVideos([], "阈值"),
    videoStale: api.renderVideos([videos[1]], ""),

    // ── 概念笔记 ─────────────────────────────────
    conceptsHtml: api.renderConcepts(concepts, ""),
    conceptsEmpty: api.renderConcepts([], ""),
    // 没有定义的概念（手改坏的库）也不能画成一个空壳
    conceptNoDef: api.renderConcepts([{ name: "空的", occ: [] }], ""),

    // ── 链接与时间 ───────────────────────────────
    hrefFull: api.readerHref({ task_id: "T 1", part_index: 1, sec: 63 }),
    hrefNoSec: api.readerHref({ task_id: "T", part_index: 0, sec: null }),
    hrefNoPart: api.readerHref({ task_id: "T" }),
    hrefEmpty: api.readerHref(null),
    noteHref: api.noteHref({ task_id: "T", part_index: 2 }),

    clocks: [0, 9, 63, 605, 3725].map(api.clockLabel),
    metaBits: [api.metaBits(videos[0]), api.metaBits({ title: "只有标题" })],
  };
  process.stdout.write(JSON.stringify(out));
}

main();
