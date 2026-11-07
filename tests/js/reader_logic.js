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

  // 笔记库那份清单（/api/notes 的 videos）——只留判断用得上的几样
  const noteList = [
    { file: "带时间的一篇 · P1.md", part_name: "BV1TEST00001-P1-带时间的一篇.srt",
      title: "带时间的一篇", label: "P1", at: "2026-11-07 13:00", stale: false },
    { file: "必剪转写的一篇 · P2.md", part_name: "BV1TEST00001-P2-必剪.srt",
      title: "必剪转写的一篇", label: "P2", at: "2026-11-06 09:00", stale: true,
      stale_why: "字幕变过了" },
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

  // ── 单篇总结 ────────────────────────────────────
  // 设置里那三样。key 用明显的假值：这文件进仓库，任何像真 key 的串都不该出现。
  const modelOk = api.parseModel(
    '{"base":"api.deepseek.com/v1","model":"deepseek-chat","key":"sk-fake-for-test"}');
  const modelJunk = api.parseModel('{"base":123,"model":null,"key":[]}');
  const modelBroken = api.parseModel("{oops");

  // 发给模型的正文：行首补回时间，模型才可能引用真实时间
  const srcP2 = api.buildSummarySource(fullMulti, 1);
  const srcAsr = api.buildSummarySource(fullAsr, 0);
  const srcOob = api.buildSummarySource(fullMulti, 9);

  // 模型给的时间戳：三种写法都认，认不出的返回 null
  const stampMmss = api.parseStamp("01:03");        // 63
  const stampHms = api.parseStamp("1:02:05");       // 3725
  const stampSec = api.parseStamp(83);
  const stampDigits = api.parseStamp("83");
  const stampBracket = api.parseStamp("[01:03]");   // 模型抄成带括号的
  const stampBad = api.parseStamp("刚才那里");
  const stampNull = api.parseStamp(null);

  const segs3 = [{ start: 0 }, { start: 5 }, { start: 63 }];
  const asrSegs = [{ start: null }, { start: null }];

  const sumA = JSON.stringify({
    v: 1,
    parts: {
      "a.srt": {
        sig: "3:20:开头|结尾", at: "2026-11-05 21:00", model: "deepseek-chat",
        summary: { tldr: "一句话总览", points: [{ text: "第一条", t: "01:03" }],
                   concepts: [{ term: "术语", def: "解释" }] },
      },
      "b.srt": { sig: "2:9:x|y", summary: { points: "不是数组" } },
      "c.srt": "不是一个对象",
    },
  });
  const sumStore = api.parseSumStore(sumA);
  const quizStoreRaw = JSON.stringify({
    v: 1, parts: { "a.srt": { sig: "s", questions: [
      { stem: "问", options: ["甲", "乙", "丙", "丁"], answer: 0 }], picks: [] } },
  });
  const quizStoreBase = api.parseQuizStore(quizStoreRaw);
  const quizStoreWrite = api.withQuiz(quizStoreBase, "d.srt",
    { sig: "9:9:x|y", questions: [], picks: [] });
  const quizStoreOriginal = Object.keys(quizStoreBase.parts).join(",");

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

    // ── 单篇总结 ──────────────────────────────────
    modelOk: modelOk,
    modelJunk: modelJunk,
    modelBroken: modelBroken,
    readyOk: api.modelReady(modelOk),
    // 缺什么就报什么：只缺 key 的时候不该提示「服务地址没填」
    readyMissingKey: api.modelReady({ base: "b", model: "m" }),
    readyMissingTwo: api.modelReady({ base: "", model: "m", key: "k" }),
    readyEmpty: api.modelReady(null),
    modelKeyName: api.modelKey(),
    sumKeyName: api.sumKey("T-test"),
    // 「缺什么」那句：中文与拉丁字母之间空一格，顿号后面不空
    lackKey: api.lackText(["API key"]),
    lackTwo: api.lackText(["服务地址", "API key"]),
    lackNone: api.lackText([]),

    srcP2: srcP2,
    srcAsr: srcAsr,
    srcOob: srcOob,

    stampMmss: stampMmss,
    stampHms: stampHms,
    stampSec: stampSec,
    stampDigits: stampDigits,
    stampBracket: stampBracket,
    stampBad: stampBad,
    stampNull: stampNull,

    matchNear: api.matchStamp(segs3, 63.4),   // 容差内：落在第 3 段
    matchFar: api.matchStamp(segs3, 70),      // 差 7 秒：无出处
    matchNull: api.matchStamp(segs3, null),
    matchEmpty: api.matchStamp([], 5),

    // 模型给了一个原文没有的时间：判无出处，不给锚点
    anchors: api.anchorsOf(
      [{ text: "第一条", t: "01:03" },
       { text: "编的", t: "07:12" },
       { text: "没给", t: null }], segs3, true),
    // 这一篇本来没有逐句时间：模型硬给了时间也一律无出处
    anchorsNoTime: api.anchorsOf([{ text: "a", t: "01:03" }], asrSegs, false),

    sigStable: api.summarySig(fullMulti.parts[1]) === api.summarySig(fullMulti.parts[1]),
    sigChangesWithText: api.summarySig(fullMulti.parts[1]) !== api.summarySig(fullAsr.parts[0]),
    sigEmpty: api.summarySig(null),

    sumStore: sumStore,
    badStores: [api.parseSumStore("{oops"), api.parseSumStore("[1,2]"),
                api.parseSumStore('{"v":1,"parts":[]}'), api.parseSumStore(null)],
    sumOfA: api.sumOf(sumStore, "a.srt"),
    sumOfMissing: api.sumOf(sumStore, "zz"),
    sumOfNull: api.sumOf(null, "a"),
    sumWrite: api.withSum(sumStore, "b.srt",
      { sig: "9:9:新|新", at: "", model: "", summary: { tldr: "新的", points: [], concepts: [] } }),
    sumWriteLeavesOrig: sumStore.parts["b.srt"].sig,

    // ── 单篇问答 ────────────────────────────────────
    qaKeyName: api.qaKey("T-test"),
    // 只认方括号：一句普通的话不许被切成引用
    citesPlain: api.splitCites("没有时间的一句话"),
    citesOne: api.splitCites("先装 Python [01:03]，再配 PATH"),
    citesAdjacent: api.splitCites("[01:03][01:10]"),
    citesHms: api.splitCites("在 [1:02:05] 那里"),
    // 不是时间的方括号原样留着：[abc] 不是时间，[12:34:56:78] 也不是
    citesJunk: api.splitCites("[abc] 和 [12:34:56:78]"),
    citesEmpty: api.splitCites(""),

    // 引用核对：能对上的给 idx，编出来的 idx 是 -1
    linesOk: api.answerLines(
      "先装 Python [01:03]，再配 PATH [01:10]。", fullMulti.parts[1].segments, true),
    linesMadeUp: api.answerLines(
      "开头 [00:00]，编的 [07:12]。\n\n第二段 [01:03]",
      fullMulti.parts[1].segments, true),
    // 这一篇本来没有逐句时间：模型给了时间也一律无出处
    linesNoTime: api.answerLines(
      "先装 Python [01:03]。", fullAsr.parts[0].segments, false),

    // 问答的正文：和总结同一份，但摘掉了「所有要点的 t 请写成 null」那句
    askSrcP1: api.buildModelSource(fullMulti, 1),
    askSrcAsr: api.buildModelSource(fullAsr, 0),

    qaStore: api.parseQaStore(JSON.stringify({
      v: 1,
      parts: {
        "a.srt": { sig: "2:9:x|y", at: "2026-11-06 13:00", model: "m",
                   msgs: [{ q: "问1", a: "答1 [01:03]", at: "2026-11-06 13:00" },
                          { q: "  ", a: "空问题" },
                          "不是对象",
                          { q: "问2" }] },
        "b.srt": { msgs: "不是数组" },
        "c.srt": { sig: "1:1:a|b", msgs: [] },
      },
    })),
    qaBad: [api.parseQaStore("{oops"), api.parseQaStore("[1,2]"),
            api.parseQaStore('{"v":1,"parts":[]}')],
    qaOfA: api.qaOf(api.parseQaStore(JSON.stringify({
      v: 1, parts: { "a.srt": { sig: "s", msgs: [{ q: "问", a: "答" }] } },
    })), "a.srt"),
    qaWrite: api.withQa(api.parseQaStore(JSON.stringify({
      v: 1, parts: { "a.srt": { sig: "s", msgs: [{ q: "旧问", a: "旧答" }] } },
    })), "d.srt", { sig: "9:9:x|y", msgs: [{ q: "q", a: "a" }] }),
    // 清空只清这一篇：别的篇的上下文不能被顺手删掉
    qaClear: api.withoutQa(api.parseQaStore(JSON.stringify({
      v: 1, parts: { "a.srt": { sig: "s", msgs: [{ q: "q", a: "a" }] },
                       "b.srt": { sig: "t", msgs: [{ q: "q2", a: "a2" }] } },
    })), "a.srt"),
    // ── 小测验 ──────────────────────────────────────
    quizKeyName: api.quizKey("T-test"),

    // 题目清洗：坏数据逐条丢，好的留着。判据和服务端一致（题干 / 四个选项 / 答案 0-3）
    quizQs: api.quizQuestionsOf([
      { stem: "先装什么？", options: ["装 Python", "装 Node", "装 Java", "都不用"],
        answer: 0, why: "原文说先装 Python [01:03]。", t: 63 },
      { stem: "只有两个选项", options: ["甲", "乙"], answer: 0 },
      { stem: "", options: ["甲", "乙", "丙", "丁"], answer: 1 },
      { stem: "答案是 9", options: ["甲", "乙", "丙", "丁"], answer: 9 },
      { stem: "选项是空的", options: ["甲", "", "丙", "丁"], answer: 1 },
      { stem: "解析缺了也留着", options: ["甲", "乙", "丙", "丁"], answer: 3 },
      "不是对象",
    ]),
    quizQsNotList: api.quizQuestionsOf("oops"),

    // 作答记录：短了补 null，长了截掉，越界的当没答
    picksPad: api.quizPicksOf([1], 3),
    picksTrim: api.quizPicksOf([1, 2, 3], 2),
    picksJunk: api.quizPicksOf([1, 9, "2", -1], 4),
    picksNone: api.quizPicksOf(null, 2),

    // 进度与错题
    scoreHalf: api.quizScore(
      [{ answer: 0 }, { answer: 1 }, { answer: 2 }], [0, 0, null]),
    scoreEmpty: api.quizScore([], []),
    wrongOf: api.quizWrong(
      [{ answer: 0 }, { answer: 1 }, { answer: 2 }], [0, 0, null]),

    // 解析：引用核对复用第 2 层；解析里没引用时给这道题的出处
    exOk: api.quizExplain(
      { why: "原文说先装 Python [01:03]。", t: "01:03" },
      fullMulti.parts[1].segments, true),
    exMadeUp: api.quizExplain(
      { why: "大概在 [07:12] 吧。", t: "01:03" },
      fullMulti.parts[1].segments, true),
    // 解析里没有引用：要靠 t 补一个出处锚点
    exNoCite: api.quizExplain(
      { why: "没有时间的一句话。", t: "01:03" },
      fullMulti.parts[1].segments, true),
    // 解析里没引用、题上的 t 也落不到任何一段：别补——补出来是一行「出处 无出处」
    exNoRef: api.quizExplain(
      { why: "没有时间的一句话。", t: "59:59" },
      fullMulti.parts[1].segments, true),
    // 这一篇本来没有逐句时间：一个锚点都不给
    exNoTime: api.quizExplain(
      { why: "先装 Python [01:03]。", t: "01:03" },
      fullAsr.parts[0].segments, false),

    // 存法：和总结、对话同一个路数
    quizStore: api.parseQuizStore(JSON.stringify({
      v: 1,
      parts: {
        "a.srt": { sig: "2:9:x|y", at: "2026-11-07 10:00", model: "m",
                   questions: [{ stem: "问", options: ["甲", "乙", "丙", "丁"],
                                 answer: 2, why: "", t: null }],
                   picks: [2] },
        "b.srt": { sig: "1:1:a|b", questions: [] },
        "c.srt": { sig: "1:1:a|b", questions: "不是数组" },
      },
    })),
    quizBad: [api.parseQuizStore("{oops"), api.parseQuizStore("[1,2]"),
              api.parseQuizStore('{"v":1,"parts":[]}')],
    quizOfA: api.quizOf(api.parseQuizStore(JSON.stringify({
      v: 1, parts: { "a.srt": { sig: "s", questions: [
        { stem: "问", options: ["甲", "乙", "丙", "丁"], answer: 0 }], picks: [] } },
    })), "a.srt"),
    // 笔记库：这一篇在库里存过没有，靠分 P 文件名认（标题会改，文件名不会）
    noteHit: api.noteFor(noteList, "BV1TEST00001-P1-带时间的一篇.srt"),
    noteMiss: api.noteFor(noteList, "BV1TEST00001-P3-没存过的一篇.srt"),
    noteEmpty: [api.noteFor([], "a"), api.noteFor(null, "a"),
                api.noteFor(noteList, ""), api.noteFor(noteList, undefined)],

    // 落点：sec 落在哪一段的区间里就选哪一段。只按「起点谁近」会选到下一段——
    // 本段起点可能早在十秒前，而下一段起点只差三秒。
    pickInside: api.seekCueIndex([{ start: 125, end: 137.7 },
                                  { start: 137.92, end: 141 }], 135),
    pickInsideLast: api.seekCueIndex([{ start: 0, end: 5 },
                                      { start: 5.2, end: 9 }], 8.9),
    pickGap: api.seekCueIndex([{ start: 0, end: 5 }, { start: 9, end: 12 }], 6.8),
    pickOverlap: api.seekCueIndex([{ start: 0, end: 20 }, { start: 10, end: 30 }], 15),
    pickSkipNull: api.seekCueIndex([{ start: null, end: null },
                                    { start: 3, end: 6 }], 4),
    pickEmpty: [api.seekCueIndex([], 3), api.seekCueIndex(null, 3),
                api.seekCueIndex([{ start: 0, end: 5 }], null),
                api.seekCueIndex([{ start: 0, end: 5 }], NaN)],

    // 写一篇不动别的篇；也不改传进来的那个对象
    quizWrite: quizStoreWrite,
    quizWriteKeepsA: quizStoreWrite.parts["a.srt"] !== undefined,
    quizWriteLeavesOrig: quizStoreOriginal,
  };
  process.stdout.write(JSON.stringify(out));
}

main();
