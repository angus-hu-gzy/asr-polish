// 阅读页前端逻辑的取数器：把 reader.py 里那段 <script> 灌进一个极简 DOM 桩里跑，
// 再把纯函数的结果当 JSON 打出来。断言写在 tests/test_reader_ui.py（Python 侧），
// 这样测试仍然只有一个入口：python -m unittest discover -s tests。
//
// 为什么要这么绕：阅读页的功能全在前端，字符串断言（"HTML 里有没有 localStorage"）
// 只能证明代码写进去了，证明不了算得对。playerUrl / partMeta / buildMarkdown
// 都是纯函数，给它们夹具就够了；真正的点击流程另有浏览器验证。
const fs = require("fs");

function fakeNode(tag) {
  const cls = new Set();
  const n = {
    nodeType: 1,
    tagName: String(tag).toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    classList: {
      add: (...c) => c.forEach((x) => cls.add(x)),
      remove: (...c) => c.forEach((x) => cls.delete(x)),
      toggle: (c, on) => (on ? cls.add(c) : cls.delete(c)),
      contains: (c) => cls.has(c),
    },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    insertBefore(c) { this.children.unshift(c); return c; },
    removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; },
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    getAttribute() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    focus() {},
    click() {},
    get firstChild() { return this.children[0] || null; },
    textContent: "",
    innerHTML: "",
    value: "",
    title: "",
    href: "",
    src: "",
    hidden: false,
    disabled: false,
  };
  return n;
}

function makeEnv(src) {
  const store = Object.create(null);
  const doc = {
    body: fakeNode("body"),
    documentElement: fakeNode("html"),
    getElementById: () => fakeNode("div"),
    querySelector: () => fakeNode("div"),
    querySelectorAll: () => [],
    createElement: (t) => fakeNode(t),
    createTextNode: (t) => ({ nodeType: 3, textContent: t }),
    addEventListener() {},
  };
  const win = {
    document: doc,
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
    },
    // ?task=… 让页面进「直接看某个任务」那一路，S.id 有值导出的 md 才带任务号。
    // fetch 永远 pending：这一层只验纯函数，不要真去请求。
    location: { search: "?task=T-test", href: "http://127.0.0.1/reader?task=T-test" },
    history: { replaceState() {} },
    setTimeout: () => 0,
    setInterval: () => 0,
    clearTimeout: () => {},
    clearInterval: () => {},
    fetch: () => new Promise(() => {}),
    console,
  };
  win.window = win;
  const run = new Function(
    "window", "document", "localStorage", "location", "history",
    "fetch", "setTimeout", "setInterval", "clearTimeout", "clearInterval", "console",
    src
  );
  run(win, doc, win.localStorage, win.location, win.history, win.fetch,
      win.setTimeout, win.setInterval, win.clearTimeout, win.clearInterval, console);
  return win;
}

function main() {
  const src = fs.readFileSync(process.argv[2], "utf8");
  const api = makeEnv(src).__reader;
  if (!api) throw new Error("页面脚本没有挂出 window.__reader，测试钩子被删了？");

  const task = {
    id: "T-test",
    result: {
      items: [
        { bvid: "BV1RXx169Exu", title: "【2024最新】Python从零入门到精通系统学习大纲" },
      ],
    },
  };
  const marks = [
    { key: "k1", t: 3.12, text: "第一句重点", part: 0, page: 1,
      bvid: "BV1RXx169Exu", partName: "BV1RXx169Exu-P1-前言.srt" },
    { key: "k2", t: 87, text: "第二句\n重点（带换行）", part: 1, page: 2,
      bvid: "BV1RXx169Exu", partName: "BV1RXx169Exu-P2-基础.srt" },
    { key: "k3", t: null, text: "必剪 ASR 拆出来的，没有时间", part: 0, page: 1,
      bvid: "BV1demoASR", partName: "BV1demoASR-没有时间戳的一期.srt" },
  ];

  const out = {
    playerUrl: api.playerUrl({ bvid: "BV1RXx169Exu", page: 2 }, 62.7),
    playerUrlZero: api.playerUrl({ bvid: "BV1RXx169Exu", page: 1 }, 0),
    partMulti: api.partMeta("BV1RXx169Exu-P2-【基础篇】Python解释器的安装.srt", task),
    partSingle: api.partMeta("BV1RXx169Exu-【2024最新】Python从零入门.srt", task),
    partFromTarget: api.partMeta("手工放进来的.srt",
      { payload: { target: "https://www.bilibili.com/video/BV1xx411c7mD?p=3" } }),
    partNoBv: api.partMeta("随便.srt", { payload: { target: "up:12345" } }),
    jumpReal: api.hasRealTime({ segments: [{ start: null, text: "a" }, { start: 1.2, text: "b" }] }),
    jumpAsr: api.hasRealTime({ segments: [{ start: null, text: "a" }, { start: null, text: "b" }] }),
    mkKey: api.mkKey("a.srt", 3.12, "文字"),
    mkKeyNoTime: api.mkKey("a.srt", null, "文字"),
    fmt87: api.fmt(87),
    fmt3725: api.fmt(3725),
    mdFile: api.mdFileName('Python/从零: 入门 <精讲>', ""),
    mdFileEmpty: api.mdFileName("", "BV1RXx169Exu"),
    mdSingle: api.buildMarkdown(marks.slice(0, 2), task, new Date(2026, 10, 5, 13, 43)),
    mdMixed: api.buildMarkdown(marks, task, new Date(2026, 10, 5, 13, 43)),
    mdAsr: api.buildMarkdown([marks[2]], task, new Date(2026, 10, 5, 13, 43)),
    link: api.markLink(marks[0]),
    linkNoTime: api.markLink(marks[2]),
  };
  process.stdout.write(JSON.stringify(out));
}

main();
