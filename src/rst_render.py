"""Render reStructuredText to HTML using docutils, capturing warnings."""
import io
import re
from typing import Optional

from docutils import core as rst_core
from docutils import nodes
from docutils.parsers.rst import roles as _roles  # noqa: F401  (ensures builtins)


def _extract_tocentries(source: str):
    """Find ``.. toctree::`` directives in RST source.

    Returns ``(cleaned_source, toc_entries)`` where ``toc_entries`` is a list of
    ``{"entries": [str, ...], "caption": str | None}`` in source order. Docutils'
    ``html4css1`` writer silently drops toctree directives (it is a Sphinx
    feature), so we strip them from the source — the preview then renders them
    itself as clickable links. The caller resolves each entry against the repo
    (entry ``foo`` -> ``foo.rst``; entry ``foo/index`` -> directory).
    """
    lines = source.splitlines()
    toc_entries = []
    out = []
    # RST directive option line: ":name: value" (option name is alnum/underscore,
    # followed by a colon and the value). Note it does NOT end with a colon.
    opt_re = re.compile(r"^:([A-Za-z0-9_]+):\s?(.*)$")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip() == ".. toctree::":
            base = len(line) - len(line.lstrip())  # directive's own indent
            j = i + 1
            entries = []
            caption = None
            while j < n:
                raw = lines[j]
                stripped = raw.strip()
                if not stripped:
                    j += 1  # blank lines are allowed within a directive block
                    continue
                ind = len(raw) - len(raw.lstrip())
                if ind <= base:
                    break  # returned to the base indent -> directive ends
                m = opt_re.match(stripped)
                if m:
                    # Option line, e.g. ":maxdepth: 2" or ":caption: Foo".
                    name, value = m.group(1), m.group(2)
                    if name.lower() == "caption":
                        caption = value.strip()
                    j += 1
                    continue
                entries.append(stripped)
                j += 1
            toc_entries.append({"entries": entries, "caption": caption})
            out.append("")  # keep a blank line so surrounding paragraphs stay
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out), toc_entries


def render_rst(source: str, settings_overrides=None) -> dict:
    """Render RST source to an HTML fragment.

    Returns a dict:
      {
        "html": "<div class='document'>...</div>",   # rendered body
        "warnings": [str, ...],                       # docutils warnings
        "errors": [str, ...],                         # docutils system messages
        "title": str | None,
        "toc": "<ul>...</ul>" | None,                # table of contents (html)
        "toc_entries": [{"entries": [...], "caption": str|None}, ...],
      }

    ``toc_entries`` carries the raw ``.. toctree::`` entries (stripped from the
    body, since docutils' html writer drops them). The client resolves each entry
    to a file path (``foo`` -> ``foo.rst``; ``foo/index`` -> a directory) and
    renders them as clickable links.
    """
    if settings_overrides is None:
        settings_overrides = {}

    overrides = {
        "report_level": 5,          # suppress warnings in output; we collect them
        "halt_level": 5,            # don't halt on error
        "warning_file": "",         # we capture programmatically
        "initial_header_level": 2,  # so doc title isn't h1 by default in fragment
        "syntax_highlight": "short",
    }
    overrides.update(settings_overrides)

    # Collect messages via a custom writer-independent approach.
    errors_io = io.StringIO()
    overrides["error_stream"] = errors_io
    overrides["warning_stream"] = errors_io

    # Strip ``.. toctree::`` directives up front: docutils' html4css1 writer
    # drops them, and we want to surface them as links instead.
    source, toc_entries = _extract_tocentries(source)

    doctree = rst_core.publish_doctree(source, settings_overrides=overrides)

    # Extract title
    title = None
    for node in doctree.findall(nodes.title):
        title = node.astext()
        break

    # Extract table of contents (headings)
    toc_html = _build_toc(doctree)

    # Render HTML body
    result = rst_core.publish_parts(
        source,
        writer_name="html4css1",
        settings_overrides=overrides,
    )
    # Older docutils returns (parts_dict, system_message); newer returns dict.
    if isinstance(result, tuple):
        parts = result[0]
    else:
        parts = result

    body = parts.get("html_body") or parts.get("fragment", "")

    warnings = []
    if doctree.get("warnings"):
        warnings = list(doctree["warnings"])

    # Also parse the error stream for any system messages
    err_text = errors_io.getvalue().strip()
    if err_text:
        for line in err_text.splitlines():
            line = line.strip()
            if line and line not in warnings:
                warnings.append(line)

    return {
        "html": body,
        "warnings": warnings,
        "errors": [],
        "title": title,
        "toc": toc_html,
        "toc_entries": toc_entries,
    }


def _build_toc(doctree) -> Optional[str]:
    """Build a simple HTML table of contents from section headings.

    Anchors use the section's real rendered id (``ids[0]``), not a re-derived
    slug — docutils generates ids like ``chapter-one`` (hyphenated), so a
    separate ``_slug`` (underscored) would point at anchors that don't exist.
    """
    items = []
    for sec in doctree.findall(nodes.section):
        titles = sec.traverse(nodes.title)
        if not titles:
            continue
        text = titles[0].astext()
        depth = _section_depth(sec)
        ids = sec.get("ids") or []
        anchor = ids[0] if ids else (sec.get("id") or _slug(text))
        items.append((depth, text, anchor))

    if not items:
        return None

    out = ["<ul class='toc'>"]
    prev_depth = 0
    for depth, text, anchor in items:
        if depth > prev_depth:
            out.append("<ul>")
        elif depth < prev_depth:
            out.append("</li>" * (prev_depth - depth))
            out.append("</ul>" * (prev_depth - depth))
        else:
            if prev_depth > 0:
                out.append("</li>")
        out.append(
            f"<li><a href='#{anchor}'>{_esc(text)}</a>"
        )
        prev_depth = depth
    # close remaining
    if prev_depth > 0:
        out.append("</li>")
        out.append("</ul>" * prev_depth)
    out.append("</ul>")
    return "".join(out)


def _section_depth(sec) -> int:
    depth = 1
    parent = sec.parent
    while parent is not None:
        if isinstance(parent, nodes.section):
            depth += 1
        parent = parent.parent
    return depth


def _slug(text: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
