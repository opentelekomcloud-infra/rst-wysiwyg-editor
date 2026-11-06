// Node harness for the RST grid-table engine in static/js/app.js.
// The grid functions are DOM-free pure logic (they read/write $("source") and
// call setEditorValue), so we extract their block from app.js, run it inside a
// tiny fake-DOM environment, and assert against the examples in the spec.
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "..", "src", "static", "js", "app.js"), "utf8");
const START = "  // =============================================================== RST grid tables";
const END = "  const toolbarActions = {";
const i = src.indexOf(START);
const j = src.indexOf(END);
if (i < 0 || j < 0 || j < i) throw new Error("grid-table block not found in app.js");
const block = src.slice(i, j);

// ---- fake DOM: one mutable "editor" the block reads and writes ------------
global.__editor = { value: "", selectionStart: 0, selectionEnd: 0, focus() {} };
global.__sevc = 0;
global.__lastSet = null;
const state = { currentKind: "text" };
const helpers = `
    function $(id) { return __editor; }
    function setEditorValue(v, s, e) { __sevc++; __lastSet = { value: v, start: s, end: e }; __editor.value = v; __editor.selectionStart = s; __editor.selectionEnd = e; }
    function commit(v) {}
    function renderPreview() {}
    function toast() {}
    const state = __state__;
`;
const H = (function () {
  const body = "(function () {\n" + helpers + block +
    "return { isBorderLine, isTableLine, borderColPos, colOf, lineOf, renderBlock, computeWidths," +
    "extractCells, parseRows, findTable, tableAtCaret, applyTableEdit, maybeRealignTable, tableEditOp, enterInCell, backspaceInCell, detectColumn }; })()";
  return new Function("__state__", "__editor", "return " + body)(state, global.__editor);
})();

function setEditor(text, pos) {
  global.__editor.value = text;
  global.__editor.selectionStart = pos == null ? 0 : pos;
  global.__editor.selectionEnd = global.__editor.selectionStart;
}
const lastSet = () => global.__lastSet;

let pass = 0, fail = 0;
function ok(cond, msg) {
  if (cond) { pass++; console.log("  PASS " + msg); }
  else { fail++; console.log("  FAIL " + msg); }
}

// The user's example: header + 3 data rows (4 content rows, 3 columns).
function userExample() {
  return [
    "+--------------+---------------------+---------------------------------------------------------------------------+",
    "| Parameter    | Type                | Description                                                               |",
    "+==============+=====================+===========================================================================+",
    "| warn_config  | List data structure | Alarm configuration                                                       |",
    "+--------------+---------------------+---------------------------------------------------------------------------+",
    "| topic_urn    | String              | ID of an alarm group                                                      |",
    "+--------------+---------------------+---------------------------------------------------------------------------+",
    "| display_name | String              | Specifies the name of the SMN topic used for sending alarm notifications. |",
    "+--------------+---------------------+---------------------------------------------------------------------------+",
  ].join("\n");
}
const clone = (rows) => rows.map((r) => r.map((cell) => cell.slice()));
const EMPTY = () => [""];

console.log("1) parse the user's example");
{
  const text = userExample();
  const ft = H.findTable(text, text.indexOf("| warn_config") + 5);
  ok(!!ft, "findTable locates the table from inside a cell");
  const pr = H.parseRows(ft.lines, ft.top, ft.bottom, ft.p);
  ok(pr.rows.length === 4, "4 content rows (header + 3 data), got " + pr.rows.length);
  ok(pr.rows[0].length === 3, "3 columns, got " + pr.rows[0].length);
  ok(pr.rows[0][0][0] === "Parameter" && pr.rows[0][1][0] === "Type" && pr.rows[0][2][0] === "Description",
     "header = Parameter / Type / Description");
  ok(pr.rows[1][0][0] === "warn_config" && pr.rows[1][1][0] === "List data structure", "row1 = warn_config / List data structure");
  ok(pr.rows[2][0][0] === "topic_urn", "row2 = topic_urn");
  ok(pr.rows[3][2][0] === "Specifies the name of the SMN topic used for sending alarm notifications.",
     "row3 col2 full text");
}

console.log("2) render preserves content, recomputes widths, stays aligned");
{
  const text = userExample();
  const ft = H.findTable(text, text.indexOf("| warn_config") + 5);
  const pr = H.parseRows(ft.lines, ft.top, ft.bottom, ft.p);
  const w = H.computeWidths(pr.rows);
  ok(w[0] === 14, "col0 width 14 (display_name+2), got " + w[0]);
  ok(w[1] === 21, "col1 width 21 (List data structure+2), got " + w[1]);
  ok(w[2] === "Specifies the name of the SMN topic used for sending alarm notifications.".length + 2,
     "col2 width = longest cell + 2, got " + w[2]);
  const rendered = H.renderBlock(pr.rows, w).text;
  const rt = H.findTable(rendered, rendered.indexOf("| warn_config") + 5);
  const rp = H.parseRows(rt.lines, rt.top, rt.bottom, rt.p);
  ok(rp.rows[0][0][0] === "Parameter" && rp.rows[3][2][0].startsWith("Specifies"), "re-rendered content intact");
  ok(rp.rows[1][0][0] === "warn_config", "re-rendered row1 col0 intact");
  const lens = rendered.split("\n").map((l) => l.length);
  ok(lens.every((l) => l === lens[0]), "all lines equal length (aligned), got " + JSON.stringify(lens));
  const lines = rendered.split("\n");
  ok(lines[2].includes("=") && !lines[4].includes("=") && !lines[6].includes("="),
     "header separator '=' only on line 2");
  // Round-trip idempotency.
  const rendered2 = H.renderBlock(rp.rows, H.computeWidths(rp.rows)).text;
  ok(rendered2 === rendered, "render is idempotent");
}

console.log("3) row operations (add above / add below / delete)");
{
  const text = userExample();
  const ft = H.findTable(text, text.indexOf("| warn_config") + 5);
  const pr = H.parseRows(ft.lines, ft.top, ft.bottom, ft.p);
  const c = H.colOf(5, ft.p); // inside col 0

  // add row below (after warn_config, row 1) -> 5 rows, caret in new row 2
  setEditor(text);
  const below = clone(pr.rows); below.splice(2, 0, [EMPTY(), EMPTY(), EMPTY()]);
  H.applyTableEdit(ft, below, { row: 2, cellLine: 0, col: c }, 0);
  let rt = H.findTable(lastSet().value, lastSet().start); // table at the NEW caret
  let rp = H.parseRows(rt.lines, rt.top, rt.bottom, rt.p);
  ok(rp.rows.length === 5, "add row below -> 5 rows, got " + rp.rows.length);
  ok(rp.rows[2][0][0] === "", "new row is empty");
  ok(rp.lineMap[rt.ln] && rp.lineMap[rt.ln].row === 2, "caret lands in the new row (row 2)");

  // add row above (before warn_config) -> 5 rows, warn_config pushed to row 2
  setEditor(text);
  const above = clone(pr.rows); above.splice(1, 0, [EMPTY(), EMPTY(), EMPTY()]);
  H.applyTableEdit(ft, above, { row: 1, cellLine: 0, col: c }, 0);
  rt = H.findTable(lastSet().value, lastSet().value.indexOf("| warn_config") + 5);
  rp = H.parseRows(rt.lines, rt.top, rt.bottom, rt.p);
  ok(rp.rows.length === 5, "add row above -> 5 rows, got " + rp.rows.length);
  ok(rp.rows[2][0][0] === "warn_config", "warn_config pushed to row 2");

  // delete row (warn_config) -> 3 rows, warn_config gone
  setEditor(text);
  const del = clone(pr.rows); del.splice(1, 1);
  H.applyTableEdit(ft, del, { row: 1, cellLine: 0, col: c }, 0);
  rt = H.findTable(lastSet().value, lastSet().value.indexOf("| display_name") + 5);
  rp = H.parseRows(rt.lines, rt.top, rt.bottom, rt.p);
  ok(rp.rows.length === 3, "delete row -> 3 rows, got " + rp.rows.length);
  ok(!rp.rows.some((row) => row[0][0] === "warn_config"), "warn_config removed");
}

console.log("4) column operations (add left / add right / delete)");
{
  const text = userExample();
  const ft = H.findTable(text, text.indexOf("| warn_config") + 5);
  const pr = H.parseRows(ft.lines, ft.top, ft.bottom, ft.p);
  const c = 1; // inside col 1 (Type)

  setEditor(text);
  const right = clone(pr.rows); right.forEach((row) => row.splice(c + 1, 0, EMPTY()));
  H.applyTableEdit(ft, right, { row: 1, cellLine: 0, col: c + 1 }, 0);
  let rt = H.findTable(lastSet().value, lastSet().value.indexOf("| warn_config") + 5);
  let rp = H.parseRows(rt.lines, rt.top, rt.bottom, rt.p);
  ok(rp.rows[0].length === 4, "add col right -> 4 cols, got " + rp.rows[0].length);

  setEditor(text);
  const left = clone(pr.rows); left.forEach((row) => row.splice(c, 0, EMPTY()));
  H.applyTableEdit(ft, left, { row: 1, cellLine: 0, col: c }, 0);
  rt = H.findTable(lastSet().value, lastSet().value.indexOf("| warn_config") + 5);
  rp = H.parseRows(rt.lines, rt.top, rt.bottom, rt.p);
  ok(rp.rows[0].length === 4, "add col left -> 4 cols, got " + rp.rows[0].length);

  setEditor(text);
  const delc = clone(pr.rows); delc.forEach((row) => row.splice(c, 1));
  H.applyTableEdit(ft, delc, { row: 1, cellLine: 0, col: c }, 0);
  rt = H.findTable(lastSet().value, lastSet().value.indexOf("| warn_config") + 5);
  rp = H.parseRows(rt.lines, rt.top, rt.bottom, rt.p);
  ok(rp.rows[0].length === 2, "delete col -> 2 cols, got " + rp.rows[0].length);
  ok(!rp.rows[0].some((cell) => cell[0] === "Type"), "Type column removed");
}

console.log("5) live re-align (dynamic adaptation)");
{
  const text = userExample();
  // Start from the properly-aligned table, caret after 'warn_config' (col0, row1).
  const caretPos = text.indexOf("warn_config") + "warn_config".length;
  setEditor(text, caretPos);
  // Simulate one keystroke: type 'x' at the caret -> 'warn_configx', borders
  // still have the OLD widths (misaligned), exactly like the browser's input.
  const t = __editor.value;
  const p0 = __editor.selectionStart;
  __editor.value = t.slice(0, p0) + "x" + t.slice(p0);
  __editor.selectionStart = p0 + 1;
  __editor.selectionEnd = p0 + 1;
  const before = __editor.value;
  const res = H.maybeRealignTable();
  ok(res === true, "maybeRealignTable returned true for an in-cell edit");
  const after = __editor.value;
  ok(after !== before, "table text changed after re-align");
  const rt = H.findTable(after, after.indexOf("warn_configx") + 5);
  const p = rt.p;
  // The border segment between the two '+' spans the cell interior plus one,
  // so the interior width = (p[1]-p[0]) - 1. It must fit 'warn_configx' + 2.
  const col0Interior = (p[1] - p[0]) - 1;
  ok(col0Interior === "warn_configx".length + 2,
     "col0 widened to " + ("warn_configx".length + 2) + " for 'warn_configx', got " + col0Interior);
  const lens = after.split("\n").map((l) => l.length);
  ok(lens.every((l) => l === lens[0]), "still fully aligned after re-align");
  const rp = H.parseRows(rt.lines, rt.top, rt.bottom, rt.p);
  ok(rp.rows[1][0][0] === "warn_configx", "edited cell preserved");
  ok(rp.rows[0][0][0] === "Parameter" && rp.rows[3][2][0].startsWith("Specifies"), "other cells preserved");
  // caret stays right after the typed character.
  const caretInLine = __editor.selectionStart - rt.lineStarts[rt.ln];
  const cellLine = after.split("\n")[rt.ln];
  ok(caretInLine === cellLine.indexOf("warn_configx") + "warn_configx".length,
     "caret lands right after the typed char");
  // Idempotent: re-running with no new keystroke must not move the caret.
  const stableCaret = __editor.selectionStart;
  H.maybeRealignTable();
  ok(__editor.selectionStart === stableCaret && __editor.value === after,
     "stable once aligned (no further drift)");
}

console.log("6) no-op outside a table / on a border line");
{
  setEditor("just some text\nno table here", 5);
  ok(H.maybeRealignTable() === false, "no table -> no re-align (returns false)");
  ok(H.enterInCell() === false, "no table -> enterInCell returns false (default Enter)");
}

console.log("7) grid-table insert produces a readable (single-line) grid");
{
  // Build the exact table the toolbar button inserts, via the same data shape.
  const rows = [
    [["Header 1"], ["Header 2"], ["Header 3"]],
    [["Text"], ["Text"], ["Text"]],
    [["Text"], ["Text"], ["Text"]],
  ];
  const block = H.renderBlock(rows, H.computeWidths(rows));
  const lines = block.text.split("\n");
  // 3 rows (header + 2 data), one cell line each + 4 borders = 7 lines.
  ok(lines.length === 7, "7 lines (3 rows x 1 cell line + 4 borders), got " + lines.length);
  // Each cell is a single line of text, not wrapped per-character.
  ok(lines[1] === "| Header 1 | Header 2 | Header 3 |", "header row on one line, got: " + JSON.stringify(lines[1]));
  ok(lines[3] === "| Text     | Text     | Text     |", "data row on one line, got: " + JSON.stringify(lines[3]));
  ok(lines[2].includes("="), "header separator uses '='");
  const lens = lines.map((l) => l.length);
  ok(lens.every((l) => l === lens[0]), "all lines equal length, got " + JSON.stringify(lens));
  // No column collapsed to the 4-char minimum (the old bug).
  const p = H.borderColPos(lines[0]);
  ok((p[1] - p[0]) > 5, "columns are content-width (not 4-char), seg0=" + (p[1] - p[0]));
}

console.log("8) Enter inside a cell extends the cell (does not break the grid)");
{
  // Start from the readable 3x3 grid, caret at the END of 'Header 1' (col0, row0).
  const rows = [
    [["Header 1"], ["Header 2"], ["Header 3"]],
    [["Text"], ["Text"], ["Text"]],
    [["Text"], ["Text"], ["Text"]],
  ];
  const block = H.renderBlock(rows, H.computeWidths(rows));
  const text = block.text;
  const caretPos = text.indexOf("Header 1") + "Header 1".length;
  setEditor(text, caretPos);
  const res = H.enterInCell();
  ok(res === true, "enterInCell handled the key");
  const after = __editor.value;
  const lines = after.split("\n");
  // The cell now has two lines: 'Header 1' then '' -> row0 spans 2 cell lines.
  // Total lines: 4 borders + (2 + 1 + 1) cell lines = 8.
  ok(lines.length === 8, "row0 gained a cell line -> 8 total lines, got " + lines.length);
  // Every line is still a valid grid line (border or cell) — nothing broke.
  const allGrid = lines.every((l) => H.isTableLine(l));
  ok(allGrid, "every line is still a grid line (no raw newline broke the table)");
  // The cell split at the caret: 'Header 1' on line 1, '' on the new line 2.
  const ft = H.findTable(after, after.indexOf("Header 1") + 5);
  const rp = H.parseRows(ft.lines, ft.top, ft.bottom, ft.p);
  ok(rp.rows[0][0][0] === "Header 1" && rp.rows[0][0][1] === "",
     "cell split into ['Header 1', ''], got " + JSON.stringify(rp.rows[0][0]));
  // The other columns' rows must have gained a blank line to match.
  ok(rp.rows[0][1][1] === "" && rp.rows[0][2][1] === "", "other columns gained a matching blank line");
  // Caret lands at the start of the new (empty) second line in col0.
  const newLineIdx = ft.ln + 1; // the second cell line of the (now taller) row
  const newLineStart = newLineIdx === 0 ? 0 : after.split("\n").slice(0, newLineIdx).reduce((a, l) => a + l.length + 1, 0);
  const caretInLine = __editor.selectionStart - newLineStart;
  ok(caretInLine === ft.p[0] + 1 + 1, "caret at start of the new cell line, got " + caretInLine);
  // Still fully aligned.
  const lens = lines.map((l) => l.length);
  ok(lens.every((l) => l === lens[0]), "still fully aligned after Enter");
}

console.log("9) Enter mid-word splits the text correctly");
{
  const rows = [
    [["Header 1"], ["Header 2"], ["Header 3"]],
    [["Text"], ["Text"], ["Text"]],
    [["Text"], ["Text"], ["Text"]],
  ];
  const text = H.renderBlock(rows, H.computeWidths(rows)).text;
  // Caret after 'Head' in 'Header 1' (col0, row0).
  const caretPos = text.indexOf("Header 1") + "Head".length;
  setEditor(text, caretPos);
  H.enterInCell();
  const ft = H.findTable(__editor.value, __editor.value.indexOf("Head") + 5);
  const rp = H.parseRows(ft.lines, ft.top, ft.bottom, ft.p);
  ok(rp.rows[0][0][0] === "Head" && rp.rows[0][0][1] === "er 1",
     "mid-word split -> ['Head', 'er 1'], got " + JSON.stringify(rp.rows[0][0]));
  const lens = __editor.value.split("\n").map((l) => l.length);
  ok(lens.every((l) => l === lens[0]), "aligned after mid-word Enter");
}

console.log("10) Backspace removes a blank cell line (inverse of Enter)");
{
  // Build the user's example: 3x3 grid, then Enter in row1 (first "Text")
  // adds a blank line to all three cells of that row.
  const rows = [
    [["Header 1"], ["Header 2"], ["Header 3"]],
    [["Text"], ["Text"], ["Text"]],
    [["Text"], ["Text"], ["Text"]],
  ];
  let text = H.renderBlock(rows, H.computeWidths(rows)).text;
  // Caret at end of the first "Text" (row1, col0) -> Enter adds a blank line.
  const caretPos = text.indexOf("Text") + "Text".length;
  setEditor(text, caretPos);
  H.enterInCell();
  text = __editor.value;
  // Sanity: row1 now has a second (blank) cell line in every column.
  let ft = H.findTable(text, text.indexOf("Text") + 5);
  let rp = H.parseRows(ft.lines, ft.top, ft.bottom, ft.p);
  ok(rp.rows[1][0].length === 2 && rp.rows[1][1].length === 2 && rp.rows[1][2].length === 2,
     "after Enter, row1 has 2 cell lines in every column");
  ok(rp.rows[1][0][1] === "" && rp.rows[1][1][1] === "" && rp.rows[1][2][1] === "",
     "the second cell lines are all blank");

  // Backspace from the LAST column (col2) of that row — must still remove the
  // line from the whole row, since every cell is blank on it.
  const blankLine = text.split("\n").find((l) => l.trim() === "|          |          |          |");
  ok(!!blankLine, "the blank line exists in the rendered table");
  // Position caret at end of the last cell on the blank line.
  const caretOnBlank = text.indexOf(blankLine) + blankLine.length;
  setEditor(text, caretOnBlank);
  const res = H.backspaceInCell();
  ok(res === true, "backspaceInCell removed the blank line (from col2)");
  text = __editor.value;
  // The blank line is gone: row1 is back to a single cell line in every column.
  ft = H.findTable(text, text.indexOf("Text") + 5);
  rp = H.parseRows(ft.lines, ft.top, ft.bottom, ft.p);
  ok(rp.rows[1][0].length === 1 && rp.rows[1][1].length === 1 && rp.rows[1][2].length === 1,
     "after backspace, row1 is back to 1 cell line in every column");
  ok(rp.rows[1][0][0] === "Text" && rp.rows[1][1][0] === "Text" && rp.rows[1][2][0] === "Text",
     "the 'Text' content is preserved");
  ok(!text.includes(blankLine), "the blank line is no longer in the table");
  const lens = text.split("\n").map((l) => l.length);
  ok(lens.every((l) => l === lens[0]), "still fully aligned after backspace");
  // Caret landed at the end of the previous cell line in col2 (the 'Text' line).
  const caretInLine = __editor.selectionStart - ft.lineStarts[ft.ln];
  const caretLine = text.split("\n")[ft.ln];
  ok(caretInLine === caretLine.lastIndexOf("Text") + "Text".length,
     "caret at end of the Text line in col2, got " + caretInLine);
}

console.log("11) Backspace does NOT remove a line that has content");
{
  // Two-line cell with content on line 2 -> backspace must NOT remove the line.
  const rows = [
    [["Header 1"], ["Header 2"], ["Header 3"]],
    [["Text", "more"], ["Text"], ["Text"]],
    [["Text"], ["Text"], ["Text"]],
  ];
  const text = H.renderBlock(rows, H.computeWidths(rows)).text;
  // Caret on the second cell line of row1, col0 ('more').
  const caretPos = text.indexOf("more") + 2;
  setEditor(text, caretPos);
  const res = H.backspaceInCell();
  ok(res === false, "line has content ('more') -> backspaceInCell returns false");
  // Also: row where only SOME cells are blank on line 2 must be kept.
  const rows2 = [
    [["Header 1"], ["Header 2"], ["Header 3"]],
    [["Text", ""], ["Text", "x"], ["Text"]],
    [["Text"], ["Text"], ["Text"]],
  ];
  const text2 = H.renderBlock(rows2, H.computeWidths(rows2)).text;
  const caretPos2 = text2.indexOf("Text") + 4; // first cell, but row has 'x'
  setEditor(text2, caretPos2);
  ok(H.backspaceInCell() === false, "one cell has content in the row -> keep the line");
}

console.log("\n" + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
