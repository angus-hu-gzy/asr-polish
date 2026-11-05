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
    // 滚动监听与 pagehide 在页面加载时就绑上了；桩里收下不做事，
    // 这一层只跑纯函数，不模拟滚动时序。
    addEventListener() {},
    removeEventListener() {},
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

  const store1 = api.parsePos('{"v":1,"parts":{"a":3}}');

  // ── 全文底座 ────────────────────────────────────
  // 段与段之间留 >0.8 秒的间隔，groupCues 才会断段；贴着排的会被并成一句，
  // 拿它断言段数就是在测分段算法，不是在测这一层。
  const fullMulti = api.buildFullText({
    task: "T-test",
    parts: [
      {
        name: "BV1RXx169Exu-P1-【前言】Python从零入门到精通系统学习大纲.srt",
        source: "srt",
        segments: [
          { start: 0, end: 2, text: "大家好，这一期我们聊聊 Python" },
          { start: 5, end: 7, text: "先从安装开始" },
        ],
      },
      {
        name: "BV1RXx169Exu-P2-【基础篇】Python解释器的安装.srt",
        source: "srt",
        segments: [
          { start: 63, end: 66, text: "打开官网下载安装包" },
          { start: 70, end: 74, text: "下一步下一步就行" },
        ],
      },
    ],
  }, task);

  // 必剪 ASR：整篇包在一个 0 → 99:59:59 的占位块里。transcript 在 srt 那条路
  // 上会按行拆开并把 start 置 null，json 那条路不拆——这里故意给它没拆的原始形状，
  // 看底座兜不兜得住。
  const fullAsr = api.buildFullText({
    parts: [{
      name: "BV1demoASR-必剪转写的一期.srt",
      source: "srt",
      segments: [{ start: 0, end: 359999, text: "第一段\n第二段" }],
    }],
  }, task);

  // 纯文本导出：一开始就没有时间，一行一段
  const fullTxt = api.buildFullText({
    parts: [{
      name: "BV1txt0000-纯文本导出的一期.srt",
      source: "txt",
      segments: [{ start: null, end: null, text: "第一段" },
                 { start: null, end: null, text: "第二段" }],
    }],
  }, task);

  // 跨视频任务（up:/search:）：每个文件都是 P1，不能用 P 号标
  const fullMixed = api.buildFullText({
    parts: [
      { name: "BV1RXx169Exu-第一篇.srt", source: "srt",
        segments: [{ start: 0, end: 2, text: "第一篇的正文" }] },
      { name: "BV1demoASR-第二篇.srt", source: "srt",
        segments: [{ start: 0, end: 2, text: "第二篇的正文" }] },
    ],
  }, task);

  const fullEmpty = api.buildFullText({ parts: [] }, task);
  const fullNull = api.buildFullText(null, task);
  const mdWhen = new Date(2026, 10, 5, 20, 31);
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

    // ── 断点续读 ──────────────────────────────────
    posKey: api.posKey("T-test"),
    // 没存过 / 脏数据：都必须是空壳，页面据此从顶部开始且不提示
    posEmpty: api.parsePos(null),
    posBroken: api.parsePos("{oops"),
    posNotObject: api.parsePos("[1,2,3]"),
    posNoParts: api.parsePos('{"v":1}'),
    posPartsIsArray: api.parsePos('{"v":1,"parts":[1,2]}'),
    // 同一个 parts 里混进非法的段号：坏的丢掉，好的留着，不能让一条脏数据毁掉整篇
    posDirty: api.parsePos('{"v":1,"parts":{"a":3,"b":-1,"c":"x","d":2.5,"e":true,"f":0}}'),
    posRead: api.posOf(store1, "a"),
    posReadMissing: api.posOf(store1, "zz"),
    posReadNoStore: api.posOf(null, "a"),
    posWriteNew: api.withPos(store1, "b", 17),
    posWriteSame: api.withPos(store1, "a", 40),
    posWriteLeavesOrig: store1.parts.a,
    idxMid: api.pickCueIndex([-500, -200, 30, 300, 700], 82),
    idxAllAbove: api.pickCueIndex([-500, -200, -100], 82),
    idxAllBelow: api.pickCueIndex([300, 500, 900], 82),
    idxEmpty: api.pickCueIndex([], 82),
    idxExact: api.pickCueIndex([82, 300], 82),
    // 实测的一组：界 69、目标段停在 96、它前面那段下沿在 44。
    // 按「距离最近」会选错到前一段，按「第一个 >= 界」才对。
    idxRealLayout: api.pickCueIndex([-42, 44, 96, 183, 269], 69),

    // ── 夜间模式 ──────────────────────────────────
    themeKey: api.themeKey(),
    // 存过就用存的，哪怕和系统相反：用户按过一次，就是他的意思
    themeStoredDarkOnLight: api.themeFrom("dark", false),
    themeStoredLightOnDark: api.themeFrom("light", true),
    themeSystemDark: api.themeFrom(null, true),
    themeSystemLight: api.themeFrom(null, false),
    // 存的是认不出的值：当没存过，回去跟随系统
    themeJunkFollowsSystem: api.themeFrom("DARK", true),
    themeEmptyFollowsSystem: api.themeFrom("{oops}", false),
    nextFromLight: api.nextTheme("light"),
    nextFromDark: api.nextTheme("dark"),
    labelLight: api.themeLabel("light"),
    labelDark: api.themeLabel("dark"),
    labelJunk: api.themeLabel("nope"),

    // ── 全文底座 ──────────────────────────────────
    fullMulti: fullMulti,
    fullMultiPlain: api.buildPlainText(fullMulti),
    fullMultiMd: api.buildFullMarkdown(fullMulti, mdWhen, task),
    fullName: api.fullFileName(fullMulti, task),
    fullAsr: fullAsr,
    fullAsrPlain: api.buildPlainText(fullAsr),
    fullAsrMd: api.buildFullMarkdown(fullAsr, mdWhen, task),
    fullTxt: fullTxt,
    fullMixed: fullMixed,
    fullMixedMd: api.buildFullMarkdown(fullMixed, mdWhen, task),
    fullEmpty: fullEmpty,
    fullNull: fullNull,
    fullEmptyPlain: api.buildPlainText(fullEmpty),
    fullEmptyMd: api.buildFullMarkdown(fullEmpty, mdWhen, task),
  };
  process.stdout.write(JSON.stringify(out));
}

main();
