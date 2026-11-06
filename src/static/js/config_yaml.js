// Tiny YAML subset encoder/decoder for the config file, shared by the frontend
// (config import/export) and the Node test harness.
//
// The config is a flat "key: value" mapping. Every value is stored as a
// double-quoted YAML scalar for single-line fields (JSON-escaped, so colons,
// @, quotes and spaces are all safe), and as a single-quoted multi-line "flow"
// scalar for fields that may contain newlines (the SSH private key) — the same
// shape PyYAML emits, so files produced here round-trip with files produced by
// the Python side.
//
// Browser:  <script src="config_yaml.js"></script>  ->  global `cym`
// Node:     const cym = require("./config_yaml.js")
//
//   cym.toYaml(obj)   -> YAML text
//   cym.parseYaml(s)  -> object

(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.cym = api;
})(typeof self !== "undefined" ? self : this, function () {

  // A "key:" line: one leading word, a colon, then the (possibly empty) value.
  var KEY_RE = /^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$/;

  // Find the index of the closing single-quote for a value whose opening quote
  // is at startIdx, honouring the single-quote '' -> ' doubling rule.
  function closeQuoteIndex(str, startIdx) {
    for (var i = startIdx + 1; i < str.length; i++) {
      if (str[i] === "'") {
        if (str[i + 1] === "'") { i++; continue; } // doubled -> literal, skip
        return i;
      }
    }
    return -1;
  }

  // Fold the raw inner content of a single-quoted flow scalar back into a
  // string: a single physical newline -> a space, a blank line -> a newline,
  // and leading/trailing whitespace on each physical line is stripped.
  function foldFlow(raw) {
    var parts = raw.split(/\r?\n/).map(function (p) {
      return p.replace(/^\s+/, "").replace(/\s+$/, "");
    });
    while (parts.length && parts[parts.length - 1] === "") parts.pop();
    var result = "";
    for (var i = 0; i < parts.length; i++) {
      if (parts[i] === "") {
        if (result !== "") result += "\n";
      } else {
        if (result !== "" && result.charAt(result.length - 1) !== "\n") result += " ";
        result += parts[i];
      }
    }
    return result;
  }

  function toYaml(obj) {
    var out = [];
    for (var k in obj) {
      if (!Object.prototype.hasOwnProperty.call(obj, k)) continue;
      var v = (obj[k] === null || obj[k] === undefined) ? "" : String(obj[k]);
      // Trim a single trailing newline (a block usually ends with one).
      if (v.length && v[v.length - 1] === "\n") v = v.slice(0, -1);
      if (v === "") {
        out.push(k + ": ''");
      } else if (v.indexOf("\n") >= 0) {
        // Single-quoted multi-line flow scalar (PyYAML-compatible): a blank
        // line between each physical line preserves a real newline.
        var body = v.split("\n").map(function (l, idx) {
          return (idx === 0 ? l : "  " + l);
        }).join("\n\n");
        out.push(k + ": '" + body + "'");
      } else {
        out.push(k + ": " + JSON.stringify(v));
      }
    }
    return out.join("\n") + "\n";
  }

  function parseYaml(text) {
    var out = {};
    var lines = String(text).split(/\r?\n/);
    for (var i = 0; i < lines.length; i++) {
      var m = lines[i].match(KEY_RE);
      if (!m) continue; // blank, comment, or a continuation line
      var key = m[1];
      var val = m[2].trim();

      if (val === "") {
        out[key] = "";
        continue;
      }
      // Multi-line single-quoted flow value (the SSH key): opens with a quote
      // that does not close on this physical line.
      if (val[0] === "'" && val[val.length - 1] !== "'") {
        var candidate = val;
        var consumed = 0;
        for (var j = i + 1; j < lines.length; j++) {
          candidate += "\n" + lines[j];
          consumed = j - i;
          if (closeQuoteIndex(candidate, 0) !== -1) break;
        }
        var cIdx = closeQuoteIndex(candidate, 0);
        out[key] = (cIdx >= 0) ? foldFlow(candidate.slice(1, cIdx)) : val.slice(1);
        i += consumed; // advance past the continuation lines
        continue;
      }
      // Single-line quoted scalars.
      if (val[0] === "'" && val[val.length - 1] === "'") {
        out[key] = val.slice(1, -1).replace(/''/g, "'");
        continue;
      }
      if (val[0] === '"' && val[val.length - 1] === '"') {
        try { out[key] = JSON.parse(val); }
        catch (e) { out[key] = val.slice(1, -1); }
        continue;
      }
      out[key] = val; // plain scalar
    }
    return out;
  }

  return { toYaml: toYaml, parseYaml: parseYaml };
});
