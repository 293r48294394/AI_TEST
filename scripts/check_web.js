/**
 * 前端模板编译校验（无需浏览器）。
 *
 * 为什么需要它：单文件 Vue 应用最危险的是模板语法错误 —— 页面白屏，但 HTTP 200、
 * 静态资源全 200，curl 完全看不出来。这里用 Vue 完整版（含编译器）在 Node vm 里编译模板，
 * 把"白屏风险"变成一条可执行的检查。
 *
 * 用法：node scripts/check_web.js
 * 退出码：0 = 通过；1 = 有编译错误；2 = 环境/文件问题
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const HTML_PATH = path.join(ROOT, "web", "index.html");
const VUE_PATH = path.join(ROOT, "web", "vendor", "vue.global.prod.js");

for (const p of [HTML_PATH, VUE_PATH]) {
  if (!fs.existsSync(p)) {
    console.error(`[check_web] 缺少文件：${p}`);
    process.exit(2);
  }
}

/** 从 <div id="app"> 开始做标签计数，取出整个根容器（含嵌套 div）。 */
function extractAppTemplate(html) {
  const start = html.indexOf('<div id="app">');
  if (start === -1) throw new Error('找不到 <div id="app">');
  const re = /<div\b[^>]*>|<\/div>/g;
  re.lastIndex = start;
  let depth = 0;
  let m;
  while ((m = re.exec(html))) {
    depth += m[0].startsWith("</") ? -1 : 1;
    if (depth === 0) return html.slice(start, m.index + m[0].length);
  }
  throw new Error("根容器 div 未闭合");
}

// --- 最小 DOM 兜底：属性值里的 && 会触发 Vue 的实体解码，需要 document ---
function decodeEntities(s) {
  return String(s).replace(/&(amp|lt|gt|quot|#39|nbsp);/g, (_, k) =>
    ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'", nbsp: " " })[k]
  );
}
function fakeElement() {
  const el = {
    _html: "",
    style: {},
    children: [],
    set innerHTML(v) {
      this._html = v;
      const attr = /foo="([^"]*)"/.exec(v);
      el.children = [{ getAttribute: () => (attr ? decodeEntities(attr[1]) : "") }];
    },
    get innerHTML() {
      return this._html;
    },
    get textContent() {
      return decodeEntities(this._html.replace(/<[^>]*>/g, ""));
    },
    setAttribute() {},
    getAttribute() {
      return null;
    },
    removeAttribute() {},
    appendChild() {},
    addEventListener() {},
  };
  return el;
}

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  navigator: { userAgent: "node" },
  document: { createElement: () => fakeElement(), querySelector: () => null },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(VUE_PATH, "utf8"), sandbox);

const Vue = sandbox.Vue;
if (!Vue || typeof Vue.compile !== "function") {
  console.error("[check_web] vue.global.prod.js 未暴露 compile");
  process.exit(2);
}

let template;
try {
  template = extractAppTemplate(fs.readFileSync(HTML_PATH, "utf8"));
} catch (err) {
  console.error("[check_web] 模板提取失败：" + err.message);
  process.exit(1);
}

const errors = [];
const warnings = [];
let render;
try {
  render = Vue.compile(template, {
    hoistStatic: false,
    decodeEntities: (s) => s, // 语法校验不需要实体解码，绕开 DOM 依赖
    onError: (e) => errors.push(e && e.message ? e.message : String(e)),
    onWarn: (w) => warnings.push(w && w.message ? w.message : String(w)),
  });
} catch (err) {
  errors.push(err && err.message ? err.message : String(err));
}

console.log(`[check_web] 模板 ${template.length} 字符，render=${typeof render}`);
if (warnings.length) {
  console.log(`[check_web] 警告 ${warnings.length} 条`);
  warnings.slice(0, 10).forEach((w) => console.log("  - " + w));
}
if (errors.length) {
  console.error(`[check_web] 编译错误 ${errors.length} 条`);
  errors.slice(0, 20).forEach((e) => console.error("  x " + e));
  process.exit(1);
}
console.log("[check_web] 通过：无模板编译错误");
