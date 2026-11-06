// Verifies the new hierarchical, indented file tree by driving renderFileTree()
// with a minimal DOM stub. run() returns the rendered rows as {name, indent,
// isDir, icon}. This is the "what does the user see" check for the fix.

const fs = require("fs");
const path = require("path");

const app = fs.readFileSync(path.join(__dirname, "..", "src", "static", "js", "app.js"), "utf8");

// Extract renderFileTree's function body. It starts at "function renderFileTree()"
// and ends at the matching close. We take from there to the next top-level
// "function " at the same indent (the following helper). Simpler: slice from the
// marker to the line "    renderChanged();\n  }" that closes it.
const start = app.indexOf("  function renderFileTree() {");
if (start < 0) throw new Error("renderFileTree not found");
const endMarker = app.indexOf("    renderChanged();\n  }\n", start);
if (endMarker < 0) throw new Error("renderFileTree end not found");
const fnSrc = app.slice(start, endMarker + "    renderChanged();\n  }".length);

// ---- Minimal DOM stub -------------------------------------------------------
class El {
  constructor() {
    this.children = [];
    this.style = {};
    this.className = "";
    this._text = "";
    this.title = "";
  }
  set innerHTML(v) { this.children = []; this._text = v || ""; }
  get innerHTML() { return this._text; }
  set textContent(v) { this._text = v; }
  get textContent() { return this._text; }
  appendChild(c) { this.children.push(c); return c; }
  insertBefore(c, ref) {
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, c);
    return c;
  }
  addEventListener() {} // clicks not exercised here
}
const els = {
  "file-tree": new El(),
  "changed-files": new El(),
};
global.document = {
  getElementById: (id) => els[id] || new El(),
  createElement: () => new El(),
};
global.state = { currentFile: "", changed: [], openDirs: new Set(), treeInit: false };

// Stub the functions renderFileTree calls.
global.openFile = () => {};
global.renameFile = () => {};
global.deleteFile = () => {};
global.fileList = () => state.allFiles;
global.renderChanged = () => {};

// Eval the function in this scope. $ is a module-level closure, so define it
// inside the body before renderFileTree.
const renderFileTree = new Function("state", "openFile", "renameFile", "deleteFile",
  "fileList", "renderChanged",
  "const $ = (id) => document.getElementById(id);\n" + fnSrc + "\nreturn renderFileTree;")(
  state, global.openFile, global.renameFile, global.deleteFile,
  global.fileList, global.renderChanged);

// ---- Test cases -------------------------------------------------------------
let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { pass++; console.log("PASS", name); }
  else { fail++; console.log("FAIL", name, extra || ""); }
}

function rows() {
  // Flatten rendered tree rows (direct children of #file-tree).
  const out = [];
  for (const el of els["file-tree"].children) {
    const indent = parseInt(el.style.paddingLeft || "10", 10);
    // icon is first child (or second if a dot precedes it); find the .tree-icon or 📄 span
    let icon = "";
    for (const c of el.children) {
      if (c.className === "tree-icon") icon = c.textContent;
    }
    const nameEl = el.children.find((c) => c.className === "fname");
    out.push({ name: nameEl ? nameEl.textContent : "", indent, icon,
               isDir: el.className.includes("dir") });
  }
  return out;
}

// Scenario: a nested repo. Top folders (depth 0) auto-open; deeper folders
// start collapsed, so the user drills in as needed.
state.allFiles = [
  "index.rst",
  "doc/source/index.rst",
  "doc/source/overview/index.rst",
  "doc/source/overview/background.rst",
  "doc/source/overview/details.rst",
  "doc/source/api/index.rst",
  "doc/source/api/endpoints.rst",
  "doc/source/_static/images/a.png",
];
state.openDirs = new Set();
state.treeInit = false;
renderFileTree();
let r = rows();
console.log("\n--- rendered rows (only top-level auto-open) ---");
r.forEach((x) => console.log("  " + (x.isDir ? "▾" : "·") + " " +
  (x.indent - 10).toString().padStart(2, " ") + x.name));

// 1. Root index.rst present at depth 0.
check("root index.rst at top", r.some((x) => x.name === "index.rst" && x.indent === 10 && !x.isDir), r);
// 2. Top folder "doc" open (rendered as a dir row at depth 0).
check("top folder 'doc' is a dir row", r.some((x) => x.name === "doc" && x.isDir && x.indent === 10), r);
// 3. "source" indented one level under doc (depth 1) because doc is open.
check("'source' indented under doc (depth 1)", r.some((x) => x.name === "source" && x.isDir && x.indent === 24), r);
// 4. Deeper "overview" is hidden while "source" is still collapsed.
check("'overview' hidden until 'source' expanded", !r.some((x) => x.name === "overview"), r);

// Expand "source": overview should now appear at depth 2.
state.openDirs.add("doc/source");
renderFileTree();
r = rows();
check("expanding 'source' reveals 'overview' (depth 2)",
  r.some((x) => x.name === "overview" && x.isDir && x.indent === 38), r);

// Expand "overview": background.rst should appear at depth 3.
state.openDirs.add("doc/source/overview");
renderFileTree();
r = rows();
check("expanding 'overview' reveals background.rst (depth 3)",
  r.some((x) => x.name === "background.rst" && x.indent === 52), r);
check("expanding 'overview' reveals details.rst (depth 3)",
  r.some((x) => x.name === "details.rst" && x.indent === 52), r);

// 5. "api" is still collapsed → its endpoints.rst hidden.
check("collapsed 'api' children hidden", !r.some((x) => x.name === "endpoints.rst"), r);
// 6. Directories sort before files at the same level.
const topOrder = r.filter((x) => x.indent === 10);
const docIdx = topOrder.findIndex((x) => x.name === "doc");
const idxIdx = topOrder.findIndex((x) => x.name === "index.rst");
check("dirs before files at top level", docIdx > -1 && idxIdx > -1 && docIdx < idxIdx, topOrder);

// 7. Collapse the top folder "doc": everything under it vanishes.
state.openDirs.delete("doc");
renderFileTree();
r = rows();
check("collapsing 'doc' hides source", !r.some((x) => x.name === "source"), r);
check("collapsing 'doc' still shows root index.rst", r.some((x) => x.name === "index.rst"), r);

// 8. Every directory row carries a "Generate index.rst" action (gen-index).
//    File rows must NOT have one.
state.openDirs = new Set(["doc", "doc/source"]);
state.treeInit = false;
renderFileTree();
function dirHasGenIndex() {
  return els["file-tree"].children
    .filter((el) => el.className.includes("dir"))
    .every((el) => el.children.some((c) => c.className === "gen-index"));
}
function noFileGenIndex() {
  return els["file-tree"].children
    .filter((el) => !el.className.includes("dir"))
    .every((el) => !el.children.some((c) => c.className === "gen-index"));
}
check("every dir row has a gen-index button", dirHasGenIndex());
check("no file row has a gen-index button", noFileGenIndex());

// ---------------------------------------------------------------------------
// 9. toctree entry resolution + preview link builder.
//    Extract resolveTocEntry / toctreeHtml and drive them with real inputs.
// ---------------------------------------------------------------------------
function extractFn(name) {
  const s = app.indexOf("  function " + name + "(");
  if (s < 0) throw new Error(name + " not found");
  // Naive brace matching from the first "{" after the name.
  let i = app.indexOf("{", s);
  let depth = 0;
  for (let k = i; k < app.length; k++) {
    if (app[k] === "{") depth++;
    else if (app[k] === "}") { depth--; if (depth === 0) return app.slice(s, k + 1); }
  }
  throw new Error(name + " end not found");
}
const esc = (s) => (s == null ? "" : String(s))
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");
const resolveTocEntry = new Function("files",
  "const escapeHtml = (s) => (s == null ? \"\" : String(s));\n" +
  extractFn("resolveTocEntry") + "\nreturn resolveTocEntry;")(null);
const FILES = [
  "doc/source/index.rst", "doc/source/chapter.rst",
  "doc/source/overview/index.rst", "doc/source/overview/background.rst",
  "doc/api.rst",
];
// Toctree entries are relative to the index.rst that holds them. For
// doc/source/index.rst the base dir is "doc/source".
const BASE = "doc/source";
check("toctree: 'chapter' -> doc/source/chapter.rst (base-relative)",
  resolveTocEntry("chapter", FILES, BASE) === "doc/source/chapter.rst",
  resolveTocEntry("chapter", FILES, BASE));
check("toctree: 'overview' -> doc/source/overview/index.rst (dir with index)",
  resolveTocEntry("overview", FILES, BASE) === "doc/source/overview/index.rst",
  resolveTocEntry("overview", FILES, BASE));
check("toctree: 'overview/index' -> doc/source/overview/index.rst (explicit)",
  resolveTocEntry("overview/index", FILES, BASE) === "doc/source/overview/index.rst",
  resolveTocEntry("overview/index", FILES, BASE));
check("toctree: missing entry -> null",
  resolveTocEntry("nope", FILES, BASE) === null);
check("toctree: explicit absolute .rst resolves verbatim",
  resolveTocEntry("/doc/api.rst", FILES, BASE) === "doc/api.rst",
  resolveTocEntry("/doc/api.rst", FILES, BASE));
check("toctree: root index (base '') resolves top-level",
  resolveTocEntry("chapter", FILES, "") === null,   // no top-level chapter.rst
  resolveTocEntry("chapter", FILES, ""));

// toctreeHtml: builds clickable <a class="tocfile"> links for resolvable
// entries (data-path = resolved file) and italic spans for missing ones.
const toctreeHtml = new Function("escapeHtml",
  extractFn("resolveTocEntry") + "\n" +
  extractFn("toctreeHtml") + "\nreturn toctreeHtml;")(esc);
const html = toctreeHtml([
  { caption: "Contents", entries: ["chapter", "overview", "missing"] },
], FILES, BASE);
check("toctreeHtml: has caption", html.includes("tocfile-cap") && html.includes("Contents"), html);
check("toctreeHtml: renders a <ul> with three <li>",
  (html.match(/<li>/g) || []).length === 3, html);
check("toctreeHtml: 'chapter' -> clickable link to doc/source/chapter.rst",
  /class="tocfile" data-path="doc\/source\/chapter\.rst"/.test(html), html);
check("toctreeHtml: 'overview' -> link to overview/index.rst",
  /data-path="doc\/source\/overview\/index\.rst"/.test(html), html);
check("toctreeHtml: 'missing' marked as missing",
  html.includes("tocfile-missing") && /tocfile-missing[^>]*>missing</.test(html), html);

console.log("\n" + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
