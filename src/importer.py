"""Import external documentation (docx / html / markdown) as reStructuredText.

Uses the system ``pandoc`` binary for the format -> RST conversion, extracts
embedded / local images into the doc tree's ``_static/images`` folder, and can
either produce a single .rst file or split the document into one file per
top-level chapter.

Layout produced (matches the project's documentation rules):
    <doc>/source/<slug>.rst              single mode
    <doc>/source/<slug>.rst              split: one file per chapter
    <doc>/source/index.rst               split: a toctree over the chapters
    <doc>/source/_static/images/<img>    extracted images

Image references inside the generated RST are rewritten to ``/_static/images/...``
so they resolve under Sphinx's static handling.

Pandoc represents docx images as RST *substitution definitions* — a block of
``.. |name| image:: /_static/images/x.png`` lines that it collects at the END of
the document, with the chapter bodies referring to them as ``|name|``. Because
an RST substitution only resolves *within the file that defines it*, splitting
the document by chapter would trap every definition in the last chapter file
and leave ``|name|`` in the other chapters as literal, unrendered text. The
splitter therefore extracts those definitions up front and re-inserts each one
into exactly the file(s) that reference it, so every chapter file is
self-contained.
"""
import os
import re
import shutil
import subprocess
import tempfile

PANDOC = os.environ.get("PANDOC_PATH", "pandoc")


class ImportError(Exception):
    """Raised when pandoc is missing or the conversion fails. Carries stderr."""

    def __init__(self, message, stderr=""):
        super().__init__(message)
        self.stderr = stderr


def find_pandoc():
    return shutil.which(PANDOC)


def slugify(title):
    """Turn a chapter title into a safe, lowercase, hyphenated filename stem."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    s = re.sub(r"^-+\d+-", "-", s).strip("-")  # drop leading "1-2-3" numbering
    return s or "chapter"


# Longest filename stem we emit. Keeps "<stem>.rst" comfortably under the
# 255-byte per-component limit that POSIX filesystems (ext4, APFS, …) enforce.
MAX_STEM_LEN = 180


def _truncate_stem(base, max_len):
    """Cut *base* to at most *max_len* chars, preferring a hyphen boundary so
    we don't leave a dangling half-word."""
    if len(base) <= max_len:
        return base
    cut = base[:max_len]
    idx = cut.rfind("-")
    if idx > max_len // 2:
        cut = cut[:idx]
    cut = cut.rstrip("-")
    return cut or base[:max_len].rstrip("-") or "chapter"


def make_stem(title, used=None, max_len=MAX_STEM_LEN):
    """Slugify + truncate a title to a filesystem-safe filename stem.

    Very long titles are cut to fit the filesystem. When *used* (a set of stems
    already taken by this import) is given, an incrementing index is appended so
    two long-but-similar titles that truncate to the same prefix never overwrite
    each other (e.g. ``…-notice``, ``…-notice-2``).
    """
    used = used if used is not None else set()
    base = slugify(title)
    reserve = 8  # room for a collision index suffix such as "-12"
    limit = max_len - reserve
    stem = _truncate_stem(base, limit)
    if stem not in used:
        used.add(stem)
        return stem
    # Truncated collision: shorten the base further and append a numeric index.
    n = 2
    while True:
        room = max(limit - len(str(n)) - 1, 1)
        prefix = _truncate_stem(base, room).rstrip("-") or "chapter"
        candidate = f"{prefix}-{n}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1


# RST image directives, with the target captured (group 2):
#   .. image::  /some/path/foo.png
#   .. figure:: /some/path/foo.png
#   .. |name| image:: /some/path/foo.png
_IMG_RE = re.compile(r"((?:\.\.\s*\|[^|]*\|\s*)?(?:image|figure)::\s*)(\S+)")


def convert_to_rst(src_bytes, fmt, media_dir, source_dir):
    """Convert a docx/html/markdown document to RST via pandoc.

    Embedded / local images are extracted into *media_dir* (a temp tree).
    The actual image files are copied into ``source_dir/_static/images`` and the
    RST references are rewritten to point there.

    Returns ``(rst_text, [copied_image_names])``.
    """
    if not find_pandoc():
        raise ImportError(
            "pandoc is not installed (needed for document import). "
            "Install it, e.g. 'apt-get install pandoc'."
        )
    fmt = (fmt or "").lower().strip()
    fmt_map = {"docx": "docx", "doc": "docx", "html": "html", "htm": "html",
               "md": "markdown", "markdown": "markdown", "rst": "rst"}
    if fmt not in fmt_map:
        raise ImportError("Unsupported format: " + (fmt or "?"))
    out_fmt = fmt_map[fmt]

    os.makedirs(media_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        src_path = os.path.join(td, "src." + fmt)
        with open(src_path, "wb") as fh:
            fh.write(src_bytes)
        cmd = [PANDOC, src_path, "-f", out_fmt, "-t", "rst", "--wrap=none"]
        if out_fmt in ("docx", "markdown"):
            cmd += ["--extract-media=" + media_dir]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired as exc:
            raise ImportError("pandoc conversion timed out.") from exc
        if proc.returncode != 0:
            raise ImportError("pandoc conversion failed.", proc.stderr or proc.stdout)
        rst_text = proc.stdout

    # Build the image map and copy files into the doc's _static/images.
    images_dir = os.path.join(source_dir, "_static", "images")
    os.makedirs(images_dir, exist_ok=True)
    mapping = _build_image_mapping(fmt, media_dir, images_dir)

    # Rewrite references (docx uses rIdN; markdown uses original names).
    new_rst = _rewrite_images(rst_text, mapping, fmt)
    # Sanitize image substitution *names* that are not RST-safe (pandoc uses
    # the original Windows image path, e.g. "C:\c42bb3dc…", for some docx
    # images; a backslash in a substitution name is eaten by RST's escape
    # handling, so the reference never matches the definition).
    new_rst = _sanitize_subst_names(new_rst)
    # Renaming a substitution shortens the name, which un-aligns any
    # grid-table rows that embedded it (pandoc sized the borders to the
    # original name). Re-align so the tables parse cleanly.
    new_rst = _normalize_grid_tables(new_rst)
    return new_rst, sorted(set(mapping.values()))


def _sanitize_subst_names(rst_text):
    """Make image substitution *names* RST-safe, consistently.

    Pandoc names some docx images after their original Windows path
    (e.g. ``C:\\c42bb3dc…``). RST's escape handling consumes a backslash in a
    substitution name (``|C:\\abc|`` parses as ``C:abc``), so the definition
    and its reference silently stop matching and the image never renders.
    Any name containing a backslash is renamed to a collision-free form
    (``img1``, ``img2``, … in first-appearance order) in BOTH the definitions
    and the references, so they always agree. The rename shrinks the name,
    so it also un-aligns any grid-table rows that embedded the original
    name — :func:`_normalize_grid_tables` repairs that.
    """
    names = []
    for m in re.finditer(r"\.\.\s+\|([^|]+)\|\s+image::", rst_text):
        name = m.group(1).strip()
        if "\\" in name and name not in names:
            names.append(name)
    if not names:
        return rst_text
    new_names = {}
    for i, name in enumerate(names, 1):
        base = f"img{i}"
        while base in new_names.values():
            base += "x"
        new_names[name] = base

    # A reference or definition name is the text between the surrounding
    # pipes. Rewrite only names that are on the unsafe list, leaving every
    # other ``|…|`` (table cells, inline literals) untouched.
    def _sub(m):
        name = m.group(1)
        if name in new_names:
            return "|" + new_names[name] + "|"
        return m.group(0)

    # Match |name| where name contains a backslash (the only unsafe char we
    # rename); a plain [^|]* would also catch table cells.
    return re.sub(r"\|((?:(?!\|).)*\\(?:(?!\|).)*)\|", _sub, rst_text)


def _normalize_grid_tables(rst_text):
    """Re-align grid-table rows that lost their right border after a
    substitution rename shortened the cell content.

    Pandoc sizes grid-table borders to the original cell content. After
    :func:`_sanitize_subst_names` rewrites a name to a shorter form, the
    rows that embedded it become shorter than the borders, and docutils
    reports ``Malformed table: right border not aligned or missing``.

    A row is re-alignable only if it is *structurally complete*: every
    inner border column (the ``+`` positions in the top border, minus the
    rightmost) already holds a ``|``. Rows that lack inner borders (merged
    cells in the source table) are left untouched — docutils is already
    lenient about those being shorter than the borders, and forcing a
    right border onto them turns a lenient parse into a hard error.
    """
    lines = rst_text.split("\n")
    out = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].startswith("+"):
            j = i
            while j < n:
                l = lines[j]
                if l == "":
                    break
                if l.startswith("+") or l.startswith("|"):
                    j += 1
                else:
                    break
            block = lines[i:j]
            if not block:
                i = j
                continue
            # Border columns = `+` positions present in every `+` row of
            # the block (their intersection is the true column set).
            plus_cols = None
            for l in block:
                if l.startswith("+"):
                    cols = {k for k, c in enumerate(l) if c == "+"}
                    plus_cols = cols if plus_cols is None else (plus_cols & cols)
            w = max(len(l) for l in block)
            if not plus_cols:
                out.extend(block)
                i = j
                continue
            inner_cols = sorted(c for c in plus_cols if c < w - 1)
            fixed = []
            for l in block:
                if l.startswith("+"):
                    if len(l) < w:
                        fixed.append(l.ljust(w, "-"))
                    else:
                        fixed.append(l)
                elif l.startswith("|"):
                    structurally_ok = all(
                        k < len(l) and l[k] == "|" for k in inner_cols
                    )
                    if structurally_ok and len(l) < w:
                        if l.rstrip().endswith("|"):
                            fixed.append(l[:-1] + " " * (w - len(l)) + "|")
                        else:
                            fixed.append(l + " " * (w - len(l) - 1) + "|")
                    else:
                        fixed.append(l)
                else:
                    # Continuation row (text-only, no left border) — pad if short.
                    fixed.append(l.ljust(w) if len(l) < w else l)
            out.extend(fixed)
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _build_image_mapping(fmt, media_dir, images_dir):
    """Return {reference_in_rst: basename_in_static}.

    * docx: pandoc keeps ``rIdN`` refs and extracts to media_dir/media/rIdN.png
            -> map 'media/rIdN.png' (the path pandoc writes) to the file's real name.
    * markdown: pandoc rewrites local refs to the extracted (hash-named) file,
            copied to media_dir/<hash>.<ext> -> map that ref to its basename.
    We map every image file found under media_dir to its basename, keyed both by
    the RST reference form and the on-disk path, so _rewrite_images can match.
    """
    mapping = {}
    for root, _dirs, files in os.walk(media_dir):
        for f in files:
            full = os.path.join(root, f)
            dest = os.path.join(images_dir, f)
            shutil.copy2(full, dest)
            # key by the basename (what pandoc's ref resolves to) and the full path
            mapping[f] = f
            mapping[full] = f
    return mapping


def _rewrite_images(rst_text, mapping, fmt):
    def _sub(m):
        prefix, target = m.group(1), m.group(2)
        if target.lower().startswith(("http://", "https://", "data:")):
            return m.group(0)
        name = None
        # markdown/docx refs may be 'media/rId5.png' or an absolute media path;
        # resolve by basename (and by exact path) against the mapping.
        if target in mapping:
            name = mapping[target]
        else:
            base = os.path.basename(target)
            if base in mapping:
                name = mapping[base]
            else:
                # match by the trailing token for 'media/rIdN.ext' forms
                tok = target.split("/")[-1]
                if tok in mapping:
                    name = mapping[tok]
        if not name:
            return m.group(0)  # leave remote / unknown refs untouched
        return prefix + "/_static/images/" + name

    out = []
    for line in rst_text.split("\n"):
        if "image::" in line or "figure::" in line:
            line = _IMG_RE.sub(_sub, line)
        out.append(line)
    return "\n".join(out)


# A pandoc image substitution definition: the defining line plus any indented
# option lines (":width: …", ":height: …", …) that follow it.
_IMG_DEF_RE = re.compile(r"^(\.\.\s+\|([^|]+)\|\s+image::\s+(\S+))\s*$")


def _extract_image_defs(rst_text):
    """Pull pandoc's image substitution definitions out of the RST.

    Returns ``(clean_text, defs)`` where *clean_text* is the RST with every
    definition removed (blank-line run collapsed) and *defs* is an ordered list
    of ``{"name": …, "lines": […full definition lines…]}`` in document order.
    Only definitions of the ``.. |name| image:: target`` form are matched, so
    plain text substitutions and the image *references* are left untouched.
    """
    lines = rst_text.split("\n")
    defs = []
    out = []
    i, n = 0, len(lines)
    while i < n:
        m = _IMG_DEF_RE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        block = [lines[i]]
        j = i + 1
        while j < n:
            nxt = lines[j]
            # Option lines are indented and start with ":" (":width: x", …);
            # any other line (including another definition) ends the block.
            if nxt.strip().startswith(":") and nxt[0].isspace():
                block.append(nxt)
                j += 1
                continue
            break
        defs.append({"name": m.group(2).strip(), "lines": block})
        i = j
    # Collapse the blank-line run left behind by the removed definitions.
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, defs


def _used_defs(text, defs):
    """The subset of *defs* referenced (``|name|``) in *text*, in def order.

    A name counts as used only when its pipe token actually appears in the
    body. The definition's own text is excluded by construction (definitions
    have already been extracted out of the text).
    """
    used = []
    for d in defs:
        if ("|" + d["name"] + "|") in text:
            used.append(d)
    return used


def _prepend_defs(text, defs):
    """Prepend a file's needed image definitions right under its heading.

    Each split file starts with its title line + underline line. The
    definitions are inserted after that underline (before the body) so they
    read as part of the file's preamble. When there is no recognizable
    heading (e.g. the toctree-only index.rst), they go at the very top.
    """
    if not defs:
        return text
    block = "\n".join(l for d in defs for l in d["lines"])
    lines = text.split("\n")
    # Locate the end of the leading title/underline pair (title line, then a
    # line of a single repeated non-space char). Fall back to the top.
    insert_at = 0
    if len(lines) >= 2 and lines[0].strip():
        first = lines[0].strip()
        under = lines[1].strip()
        # Never treat a grid-table border (+ / | / = separator) as an
        # underline: the file starts with a table, so go to the very top.
        if (first[0] not in "+|" and under and len(set(under)) == 1
                and not under[0].isalnum() and len(under) >= 3):
            insert_at = 2
    head = lines[:insert_at]
    tail = lines[insert_at:]
    # Strip leading blank lines from the tail so the block sits flush.
    while tail and not tail[0].strip():
        tail.pop(0)
    parts = head
    if head:
        parts.append("")
    parts.append(block)
    parts.append("")
    parts.extend(tail)
    return "\n".join(parts).rstrip() + "\n"


def _is_top_heading(title, underline):
    """True when *title* is a section heading and *underline* is a valid RST
    underline for it.

    An RST underline is a run of a single standard heading character (`=`, `-`,
    `~`, `^`, `"`, `#`, `%`, `+`, `*`, `_`) with length >= 3. It does NOT have
    to be as long as the title (doc authors routinely use short underlines for
    numbered sub-sections like "1.1 background"). The title line must look like
    a heading, not a table row or grid-table continuation line (which pandoc
    emits when converting docx documents that contain tables).
    """
    if not title or not underline:
        return False
    # Reject grid-table / table-row / continuation titles. A real heading
    # never starts with a table border/pipe or a backslash continuation, and
    # must contain at least one alphabetic character (this drops brace-only
    # code-block lines such as "}" that pandoc can mis-underline).
    if title[0] in "+|\\":
        return False
    if underline[0] in "+|\\":
        return False
    if not any(c.isalpha() for c in title):
        return False
    if len(set(underline)) != 1:
        return False
    ch = underline[0]
    # Standard RST underline characters. (Must include `-` — the most common
    # heading underline — and must NOT include `/`, which appears in
    # backslash-continuation lines and table cells.)
    if ch not in "=~_-\"#%*^'" or len(underline) < 3:
        return False
    # A real heading's underline is no longer than the title (pandoc writes
    # them the same length). A separator rule is much longer than the line it
    # sits under, so treat that as a horizontal rule, not a heading.
    if len(underline) > len(title) + 1:
        return False
    return True


def _heading_at(lines, i, n):
    """True when lines[i] is a real RST heading (a title line whose underline
    sits on the very next line and is no longer than the title itself).

    Pandoc emits underlines whose length matches (or is within 1 of) the title.
    A standalone separator rule — like the 470-char ``====`` under a 300-char
    legal paragraph — is far longer than the line above it and is *not* a
    heading.
    """
    if i + 1 >= n:
        return False
    title = lines[i].strip()
    under = lines[i + 1].strip()
    if not title or not under:
        return False
    return _is_top_heading(title, under)


def parse_headings(rst_text):
    """Parse RST into a flat, level-ordered list of sections.

    Returns a list of ``{level, title, body, ul}`` where *level* is the 1-based
    heading level and *ul* is the underline character. RST defines the level by
    *underline character* (in order of first appearance), so sibling headings
    sharing a character get the same level. The document's preamble (text before
    the first heading) is a single level-0 entry with an empty title.
    """
    lines = rst_text.split("\n")
    n = len(lines)
    # First pass: map each underline char to its level (by first appearance).
    ul_to_level = {}
    for i in range(n):
        if _heading_at(lines, i, n):
            c = lines[i + 1].strip()[0]
            if c not in ul_to_level:
                ul_to_level[c] = len(ul_to_level) + 1

    sections = []
    preamble = []          # lines before the first heading
    current = None
    i = 0
    while i < n:
        if _heading_at(lines, i, n):
            c = lines[i + 1].strip()[0]
            current = {"level": ul_to_level[c], "title": lines[i].strip(),
                       "ul": c, "body": []}
            sections.append(current)
            i += 2  # skip the title line AND its underline line
            continue
        if current is None:
            preamble.append(lines[i])
        else:
            current["body"].append(lines[i])
        i += 1

    # Each section's body is a list of lines; finalize all to strings.
    for s in sections:
        if isinstance(s["body"], list):
            s["body"] = "\n".join(s["body"]).strip()
    pre = "\n".join(preamble).strip()
    if pre:
        sections.insert(0, {"level": 0, "title": "", "ul": "", "body": pre})
    if not sections:
        return []
    return sections


def _build_tree(flat):
    """Nest the flat heading list into a tree of {title, body, children, ul}."""
    root = {"title": "", "body": "", "ul": "", "level": 0, "children": []}
    stack = [root]
    for s in flat:
        level = s["level"]
        if level == 0:
            root["body"] = (root["body"] + "\n" + s["body"]).strip()
            continue
        while stack[-1]["level"] >= level:
            stack.pop()
        node = {"level": level, "title": s["title"], "body": s["body"],
                "ul": s.get("ul", "="), "children": []}
        stack[-1]["children"].append(node)
        stack.append(node)
    return root


def _body_text(node):
    """The node's own content: its body plus any level-0 (preamble) children."""
    parts = [node["body"]]
    for ch in node["children"]:
        if ch["level"] == 0:
            parts.append(ch["body"])
    return "\n\n".join(p for p in parts if p and p.strip()).strip()


def split_by_levels(rst_text, level=1, title="Documentation"):
    """Split RST into files by heading level.

    ``level=1``: one ``.rst`` file per top-level chapter + a root ``index.rst``
    toctree.

    ``level >= 2``: chapters at levels *above* ``level`` that have sub-chapters
    become **folders** containing an ``index.rst`` (their own content + a
    toctree to the sub-chapters). Sub-chapters at exactly ``level`` become
    ``.rst`` files inside that folder. A chapter with no sub-chapters stays a
    single ``.rst`` file. Deeper (beyond *level*) sections remain inside their
    parent file's content.

    Returns ``{path: content}`` where paths are relative to the source root
    (e.g. ``index.rst``, ``overview/index.rst``, ``overview/background.rst``).

    Pandoc's image substitution definitions (the ``.. |name| image:: …`` block
    it emits at the end of a docx conversion) are extracted from the source and
    re-inserted into exactly the files that reference each image, so every
    generated chapter file is self-contained and its ``|name|`` references
    resolve.
    """
    # Pandoc collects image substitution definitions at the end of the
    # document; a chapter split would trap them in the last file, breaking
    # every |name| reference elsewhere. Extract them now, split the clean
    # body, then hand each file only the definitions it uses.
    clean_text, image_defs = _extract_image_defs(rst_text)
    flat = parse_headings(clean_text)
    real = [s for s in flat if (s["level"] >= 1) or (s["level"] == 0 and s["body"].strip())]
    if not real:
        return {}
    root = _build_tree(real)
    # If a document has exactly one top-level heading that contains all the
    # others (e.g. the title "My Doc / ==" followed by its chapters), unwrap it:
    # its own content becomes the preamble and its children become the real
    # top-level chapters.
    if (len(root["children"]) == 1 and root["children"][0]["level"] == 1
            and root["children"][0]["children"]):
        top = root["children"][0]
        if top["ul"] not in {c["ul"] for c in top["children"]}:
            root["children"] = top["children"]
            if top["body"].strip():
                root["body"] = (root["body"] + "\n\n" + top["body"]).strip()

    used = set()
    files = {}

    def assign_stems(node):
        for ch in node["children"]:
            if ch["level"] >= 1:
                ch["stem"] = make_stem(ch["title"], used)
                assign_stems(ch)

    assign_stems(root)

    def is_folder(node):
        """A chapter becomes a folder when it sits *above* the chosen level
        and actually has sub-chapters to hold. At level=1 this is always False."""
        return node["level"] < level and any(
            c["level"] >= 1 for c in node["children"])

    # Top-level index: a chapter is referenced as a folder ("{stem}/index") when
    # it is rendered as one, otherwise as a ``.rst`` file.
    top_items = []
    for ch in root["children"]:
        if ch["level"] >= 1:
            if is_folder(ch):
                top_items.append("   " + ch["stem"] + "/index")
            else:
                top_items.append("   " + ch["stem"] + ".rst")

    parts = [title, "=" * max(len(title), 4)]
    if root["body"].strip():
        parts.append("")
        parts.append(root["body"])
    if top_items:
        parts.append("")
        parts.append(".. toctree::")
        parts.extend(top_items)
    files["index.rst"] = "\n".join(parts).rstrip() + "\n"

    # Dispatch each chapter. ``prefix`` is the path prefix of the *parent*
    # folder (""). A chapter rendered as a folder puts its index at
    # ``<prefix>/<stem>/index.rst`` and its children at ``<prefix>/<stem>/``;
    # a chapter rendered as a file goes at ``<prefix>/<stem>.rst``.
    def render(node, prefix):
        if is_folder(node):
            files[prefix + node["stem"] + "/index.rst"] = _index_content(node, is_folder)
            for ch in node["children"]:
                if ch["level"] >= 1:
                    render(ch, prefix + node["stem"] + "/")
        else:
            files[prefix + node["stem"] + ".rst"] = _file_content(node)

    for ch in root["children"]:
        if ch["level"] >= 1:
            render(ch, "")
    # Make each generated file self-contained: RST substitutions only resolve
    # within the file that defines them, so prepend the image definitions each
    # file actually references (the toctree-only index.rst gets none).
    for path in list(files):
        if path == "index.rst":
            continue
        files[path] = _prepend_defs(files[path], _used_defs(files[path], image_defs))
    return files


def _file_content(node):
    """A single .rst file: the node's own content with sub-sections re-indented
    under its heading (they remain part of this file when not split out)."""
    lines = []
    t = node["title"]
    ch = node["ul"] or "="
    lines.append(t)
    lines.append(ch * len(t))
    if _body_text(node):
        lines.append("")
        lines.append(_body_text(node))
    for sub in node["children"]:
        if sub["level"] >= 1:
            lines.append("")
            lines.extend(_render_section(sub))
    return "\n".join(lines).rstrip() + "\n"


def _render_section(node):
    """Render a section (heading + body + sub-sections) as plain RST lines."""
    out = []
    ch = node["ul"] or "="
    out.append(node["title"])
    out.append(ch * len(node["title"]))
    if _body_text(node):
        out.append("")
        out.extend(_body_text(node).split("\n"))
    for sub in node["children"]:
        if sub["level"] >= 1:
            out.append("")
            out.extend(_render_section(sub))
    return out


def _index_content(node, is_folder):
    """An index.rst for a folder chapter: own content + a toctree of children."""
    parts = []
    t = node["title"]
    ch = node["ul"] or "="
    parts.append(t)
    parts.append(ch * len(t))
    if _body_text(node):
        parts.append("")
        parts.append(_body_text(node))
    items = []
    for sub in node["children"]:
        if sub["level"] >= 1:
            if is_folder(sub):
                items.append("   " + sub["stem"] + "/index")
            else:
                items.append("   " + sub["stem"] + ".rst")
    if items:
        parts.append("")
        parts.append(".. toctree::")
        parts.extend(items)
    return "\n".join(parts).rstrip() + "\n"


def split_by_chapters(rst_text, title=None):
    """Back-compat: one file per top-level chapter + index (level 1)."""
    files = split_by_levels(rst_text, level=1, title=title or "Documentation")
    out = []
    for path, content in files.items():
        if path == "index.rst":
            continue
        out.append((os.path.basename(path).rsplit(".rst", 1)[0], content))
    return out
