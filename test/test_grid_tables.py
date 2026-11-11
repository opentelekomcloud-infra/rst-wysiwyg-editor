"""Unit tests for importer._sanitize_subst_names and _normalize_grid_tables.

Covers the real-world failure mode found in HLD_ECS_v57.docx: pandoc emits
grid tables sized to the *original* image substitution name; a rename to a
shorter name (required when the original contains a backslash, which RST's
escape handling would otherwise consume) breaks the row alignment and
docutils reports "Malformed table". The normalizer must restore alignment
for structurally complete rows without touching merged-cell rows (which
docutils is lenient about).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import importer

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS " + name)
    else:
        FAIL += 1
        print("FAIL " + name + "  " + str(detail))


def _docutils_msgs(text):
    from docutils import core
    import io
    ws, es = io.StringIO(), io.StringIO()
    core.publish_doctree(text, settings_overrides={
        "report_level": 0, "halt_level": 5,
        "warning_stream": ws, "error_stream": es})
    return [l for l in (ws.getvalue() + es.getvalue()).splitlines() if l.strip()]


# --------------------------------------------------------------------- case 1
# A two-column grid table. Pandoc sized the borders to fit an original 12-char
# image name that now reads as the 4-char "img1". That row is therefore 8
# chars shorter than the borders but still carries both borders.

W = 40                       # full table width (including both borders)
gap = W - 2                  # inner content width
defn = ".. |img1| image:: _static/images/img1.png"
top = "+" + "-" * gap + "+"
norm = "|" + "A" * (gap // 2) + " " * (gap - gap // 2) + "|"
# The short row: original name was 8 chars longer, so this row is 8 short.
short_content = gap - 8
short = "|" + " |img1|" + " " * (short_content - len(" |img1|")) + "|"
bot = "+" + "-" * gap + "+"
text = "\n".join([defn, "", top, norm, short, norm, bot, ""])

assert len(top) == W and len(norm) == W and len(bot) == W, (len(top), len(norm))
check("precondition: short row is narrower than border",
      len(short) < W, f"{len(short)} vs {W}")

n1 = importer._normalize_grid_tables(text).split("\n")
check("all table rows uniform width after normalize",
      all(len(l) == W for l in n1 if l.startswith(("+", "|"))),
      [len(l) for l in n1 if l])
check("short row repaired to full width with closing border",
      len(n1[4]) == W and n1[4].rstrip().endswith("|"), n1[4])

errs = _docutils_msgs(text)
check("docutils: malformed-table on unnormalized input",
      any("alformed" in m for m in errs), errs[:3])
errs_n = _docutils_msgs("\n".join(n1))
check("docutils: no malformed-table after normalize",
      not any("alformed" in m for m in errs_n), errs_n[:3])


# --------------------------------------------------------------------- case 2
# A merged-cell row (missing an inner border |) must NOT be padded: docutils
# is lenient about those being shorter, and forcing a right border onto them
# turns a lenient parse into a hard error.

text2 = "\n".join([
    "+-----+-----+-----+",
    "| a   | b   | c   |",
    "+=====+=====+=====",
    "| 1   | 2   | 3   |",
    "| 4   |     | 5   |",
    "+-----+-----+-----+",
])
n2 = importer._normalize_grid_tables(text2).split("\n")
# Merged row (no inner | at col 6) stays byte-identical.
check("merged-cell row left untouched",
      n2[4] == "| 4   |     | 5   |", n2[4])
check("complete row unchanged (already aligned)",
      n2[3] == "| 1   | 2   | 3   |", n2[3])


# --------------------------------------------------------------------- case 3
# sanitize_subst_names: a name with a backslash is renamed and every
# reference rewritten; a plain name is left untouched.

orig_name = "C:\\abcd1234"          # 12 chars incl. the backslash
text3 = (".. |" + orig_name + "| image:: _static/images/img1.png\n\n"
         "see |" + orig_name + "| and |" + orig_name + "|\n")
s3 = importer._sanitize_subst_names(text3)
check("sanitize: backslash name -> img1 in definition",
      ".. |img1| image::" in s3, s3)
check("sanitize: both references rewritten",
      s3.count("|img1|") == 3, s3)   # definition + 2 references
check("sanitize: original name gone", orig_name not in s3, s3)

text4 = ".. |plainname| image:: _static/images/x.png\n\nuse |plainname|\n"
check("sanitize: plain name unchanged",
      importer._sanitize_subst_names(text4) == text4, text4)


# --------------------------------------------------------------------- case 4
# Idempotency: normalizing an already-aligned table changes nothing.

aligned = "\n".join([
    "+-----+-----+",
    "| a   | b   |",
    "+-----+-----+",
    "| 1   | 2   |",
    "+-----+-----+",
])
# All rows here are the same width; the normalize must not change any of
# them (idempotent no-op).
out = importer._normalize_grid_tables(aligned)
check("normalize is a no-op on aligned tables",
      out == aligned, out)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
