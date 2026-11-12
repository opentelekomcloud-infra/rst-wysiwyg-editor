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
      ".. |img1" in s3 and "| image::" in s3, s3)
check("sanitize: both references rewritten",
      s3.count("|img1") == 3, s3)   # definition + 2 references (length-padded)
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


# --------------------------------------------------------------------- case 5
# Reviewer finding #1: an image substitution in a NON-LAST (earlier) cell.
# Renaming the name changes the cell width, shifting every following pipe;
# the fix is a SAME-LENGTH safe name so no grid row changes width and no
# table can become malformed.

def _grid_first_cell(orig_name):
    """A 2-col grid table whose first cell holds |orig_name|, sized to that
    name's width (as pandoc would emit it)."""
    c1w, c2w = len(orig_name), 20
    top = "+" + "-" * c1w + "+" + "-" * c2w + "+"
    row = "|" + orig_name.ljust(c1w) + "|" + " some text".ljust(c2w) + "|"
    bot = "+" + "-" * c1w + "+" + "-" * c2w + "+"
    return "\n".join([".. |" + orig_name + "| image:: /_static/images/x.png",
                      "", top, row, bot, ""])

orig_name1 = "C:\\c42bb3dcab"          # 13 chars incl. backslash
doc1 = _grid_first_cell(orig_name1)
n1 = importer._normalize_grid_tables(importer._sanitize_subst_names(doc1)).split("\n")
# The renamed name is the SAME length as the original, so the row width and
# every border are unchanged -> no row is shorter than the border.
max_w = max(len(l) for l in n1 if l.startswith(("+", "|")))
check("first-cell: no row shorter than the border after rename",
      all(len(l) == max_w for l in n1 if l.startswith(("+", "|"))),
      [len(l) for l in n1 if l])
m1 = _docutils_msgs("\n".join(n1))
check("first-cell: no malformed table after sanitize+normalize",
      not any("alformed" in mm for mm in m1), m1[:4])
# And the substitution name is safe (no backslash) yet resolves.
check("first-cell: renamed name is RST-safe",
      "\\" not in n1[0] and "|img" in n1[0], n1[0])

# --------------------------------------------------------------------- case 6
# _correct_image_ext: files whose extension is wrong for their content
# (pandoc keeps a docx media's raw name, which can be ".tmp" for a real
# PNG) are renamed to the extension their magic bytes identify; files with
# a trusted extension (incl. .emf) are never renamed.

import tempfile

PNG1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63f8ffff3f0005fe02fea535580000000049454e44ae426082")

with tempfile.TemporaryDirectory() as td:
    # PNG stored as .tmp -> renamed to .png
    p1 = os.path.join(td, "image1.tmp")
    open(p1, "wb").write(PNG1)
    r1 = importer._correct_image_ext(p1)
    check("png-in-tmp renamed to .png", r1 == "image1.png", r1)
    check("renamed file exists on disk",
          os.path.isfile(os.path.join(td, "image1.png")))

    # PNG already named .png -> untouched
    p2 = os.path.join(td, "image2.png")
    open(p2, "wb").write(PNG1)
    check("png-in-png unchanged",
          importer._correct_image_ext(p2) == "image2.png")

    # genuine EMF (01 00 00 00 header) named .emf -> untouched
    p3 = os.path.join(td, "logo.emf")
    open(p3, "wb").write(b"\x01\x00\x00\x00" + b"\x00" * 32)
    check("emf untouched",
          importer._correct_image_ext(p3) == "logo.emf")

    # .svg (no sniffed magic) -> untouched
    p4 = os.path.join(td, "chart.svg")
    open(p4, "wb").write(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    check("svg untouched",
          importer._correct_image_ext(p4) == "chart.svg")

    # unidentifiable content with unknown ext -> untouched (kept as-is)
    p5 = os.path.join(td, "mystery.bin")
    open(p5, "wb").write(b"\xde\xad\xbe\xef")
    check("unknown content untouched",
          importer._correct_image_ext(p5) == "mystery.bin")

    # collision: image1.png already exists -> image11.png
    p6 = os.path.join(td, "image1.tmp")
    open(p6, "wb").write(PNG1)
    r6 = importer._correct_image_ext(p6)
    check("collision gets numeric suffix", r6 == "image11.png", r6)

    # ---------------------------------------------------------------------
    # _remove_word_toc: drop the Word static TOC (stale page numbers, no
    # links) but leave a real "Contents" section or a short numbered list.
    toc_doc = "\n".join([
        "Intro",
        "=====",
        "",
        "Some lead-in paragraph.",
        "",
        "Contents",
        "",
        "1 Solution Design 8",
        "",
        "1.1 Overview 8",
        "",
        "2 Deployment 19",
        "",
        "Solution Design",
        "===============",
        "",
        "Real body text here.",
    ])
    toc_out = importer._remove_word_toc(toc_doc)
    check("toc: 'Contents' removed", "Contents" not in toc_out, toc_out)
    check("toc: stale entries removed", "1 Solution Design 8" not in toc_out
          and "1.1 Overview 8" not in toc_out, toc_out)
    check("toc: heading + body preserved",
          "Solution Design" in toc_out and "Real body text here." in toc_out
          and "Intro" in toc_out, toc_out)
    check("toc: no doubled blank lines left",
          "\n\n\n" not in toc_out, repr(toc_out))

    # A "Contents" section with prose (no dotted-number+page list) is kept.
    toc_keep = "Contents\n==========\n\nThis chapter lists the contents.\n"
    check("toc: prose 'Contents' section kept",
          "This chapter lists the contents." in importer._remove_word_toc(toc_keep),
          importer._remove_word_toc(toc_keep))

    # A short numbered list (one entry) after "Contents" is NOT a TOC -> kept.
    toc_one = "Contents\n\n1 First item\n\nBody.\n"
    check("toc: single-entry list kept",
          "1 First item" in importer._remove_word_toc(toc_one),
          importer._remove_word_toc(toc_one))

    # Underlined TOC title variant.
    toc_ul = "Body before\n\nContents\n========\n\n1 A 2\n\n2 B 5\n\nAfter\n-----\n\nx\n"
    toc_ul_out = importer._remove_word_toc(toc_ul)
    check("toc: underlined title removed too",
          "1 A 2" not in toc_ul_out and "2 B 5" not in toc_ul_out
          and "Body before" in toc_ul_out and "After" in toc_ul_out, toc_ul_out)

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
