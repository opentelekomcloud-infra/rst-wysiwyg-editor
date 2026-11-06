# RST Editor — Solution / Architecture / Stack

A web-based WYSIWYG editor for reStructuredText (`.rst`) that edits real files
on a GitHub/Gitea repo over SSH and opens Pull/Merge Requests. No build step,
no frontend framework, ~2,200 lines of code.

## Software stack

- **Backend**
  - Python 3.13 + Flask >=3.0 (Flask 2.x breaks on 3.13 via Werkzeug 3.1)
  - docutils 0.23 — renders RST to HTML (live), with warnings + TOC
  - PyYAML 6.0 — config export/import format
  - system `git` — repo ops, via subprocess
  - system `pandoc` — doc import (docx/html/md → RST, image extraction)
  - `urllib` (stdlib) — REST calls to GitHub/Gitea (no `requests` lib)
- **Frontend** — vanilla HTML + CSS + JS (no framework, no bundler)
  - `index.html` (378 lines), `styles.css` (~324), `app.js` (~1870),
    `config_yaml.js` (YAML subset encoder/decoder for config export/import)
  - **Git / PR transport** — SSH for clone/read/edit/commit/push (key pasted in
    UI or `~/.ssh`); REST API for PRs + token check
  - **Tests** — `test_e2e.py` (186 checks) + `mock_api.py` (local fake GitHub/Gitea);
   `test_table.js` (62 checks) drives the grid-table engine in Node;
   `test_tree.js` (24 checks) drives the hierarchical file-tree renderer + toctree
   entry resolver in Node; `test_url_parse.js` (15 checks) drives the git-URL
   owner/repo parsers (regression: SCP-like URL with a port)

## Architecture (layered, one file per concern)

| File | Lines | Responsibility |
|------|-------|----------------|
| `app.py` | ~1070 | HTTP layer — 24 Flask routes: preview (with image inlining), clone (reuse/reset, mode-normalized), clone-status (existing-copy probe), files, file get/save (**with image/binary classification** via `_classify_file`, incl. **EMF → SVG** via the Linux `emf2svg-conv` tool; WMF falls back to binary), **file new (file *or* empty directory via `.gitkeep`) / upload / rename / delete**, **index-generate (build `index.rst` toctree from a directory, title default from folder name)**, status, commit, push, pr, branches, token-check (probes `/repos/{owner}/{repo}` — works with repo-scoped tokens that lack `read:user`), detect-proxy, browse-dir (work-dir picker), doc-folders, **import (single-file or split by heading level)**, image, work-root. Keeps a registry of cloned repos keyed by id. No config endpoints — the config lives in the browser. |
| `git_ops.py` | ~730 | `GitRepo` class — wraps the git subprocess. SSH key materialization (writes pasted key to a 0600 temp file, builds `GIT_SSH_COMMAND`), `normalize_scp_url()` (rewrites an SCP-like URL with a port, `git@host:2222/o/r.git` → `ssh://`, which git can't parse as-is), clone/switch-branch (re-open-safe), `reset()`, `delete_file()` / `rename_file()` (path-traversal-safe, staged), `create_dir()` (empty dir via `.gitkeep` placeholder, since git can't track empty dirs), module-level `clone_status()`/`workdir_for_url()`, path-traversal-safe read/write (text + binary), stage file/dir/commit/push/diff/status. **Idle work-dir reaping** — `touch_activity()`/`reap_idle_work_dirs()`/`prune_repos()` track per-dir activity markers (stored *outside* each repo) and delete cloned work dirs idle for `RST_WORK_DIR_TTL` (default 2h); a background thread in `app.py` runs it. Separate handling for SSH vs HTTPS (token-in-URL) remotes. |
| `pr_provider.py` | ~355 | `PRClient` — provider-agnostic GitHub/Gitea REST client. Auto-detects provider + host + owner/repo from the repo URL (port-aware), builds the api/v1 base, handles proxy. `check_token()` probes `/repos/{owner}/{repo}` (repo scope is enough, no `read:user` needed). Rich error surfacing (403/401/404/422 hints + per-field validation + raw body). Pre-flight branch-existence check that turns an opaque 422 into "branch X not found in owner/repo". |
| `importer.py` | ~453 | Doc import — converts docx/html/md → RST via `pandoc`, extracts embedded + local images to `_static/images/` and rewrites refs. **`split_by_levels()`** parses RST headings into a tree (level = underline character, in order of first appearance) and writes: level 1 → one `.rst` per top chapter + `index.rst`; level ≥ 2 → upper chapters become **folders** with an `index.rst` (own content + `toctree`) and their sub-chapters as files. Or a single file. |
| `rst_render.py` | ~215 | docutils wrapper — HTML, warnings, errors, title, TOC (anchors from the section's real `ids`). **Extracts `.. toctree::`** directives (docutils' html writer drops them) and returns them as `toc_entries` for the client to render as clickable links. |
| `config_io.py` | 81 | YAML config helpers — kept for the client-side export/import format; the server no longer persists config. |
| `mock_api.py` | — | Fake GitHub/Gitea for the e2e suite. |

## Key workflow

1. **Preview** RST live (no repo needed).
2. **Clone** from a source branch (e.g. `main`) into a configurable **Working
   directory** (default `<app>/work`, one subfolder per repo), auto-create +
   checkout a target branch, so you never push to a protected default. If a working
   copy already exists, the UI **asks** to re-fetch (keep local) or reset & re-clone
   (fresh); re-open is crash-safe (no "branch already exists").
3. **Edit** `.rst` files (split view: source + rendered preview + warnings + TOC).
   - Toolbar: **undo/redo** (buttons + `Ctrl+Z`/`Shift+Z`), **add a picture**
     (uploads to `<dir>/_static/images/`, inserts an `.. image::`/`.. figure::`,
     shown in the live preview), and a **code-block** button that *wraps* the
     selected text (indented) instead of replacing it.
   - **Word-safe inline formatting**: bold / italic / inline-code / hyperlink
     format only the selected *word*, excluding the trailing space a double-click
     includes, so the RST markup never merges the next word.
   - **Hierarchical file navigation**: the left panel is a real tree — folders
     nest with indentation, directories sort before files, and each folder has
     an expand/collapse triangle (top-level folders open by default). Clicking a
     file opens it.
   - **File management**: **＋ New**, **⬆ Upload** (multi, with a destination
     folder), and per-file **rename (→)** / **delete (×)** — all staged for review.
     After any of these (and after import) the tree **refetches the file list** so
     newly added files appear immediately — no commit required.
   - **Autosave** (toolbar toggle, remembered): on = auto-save after a pause + on
     file switch (no lost edits); off = prompt to save before switching files.
   - **File views**: text files open in the editor; **images** render inline
     (incl. **EMF** converted to SVG via the Linux `emf2svg-conv` tool); **other
     binary** files (and WMF) show a "binary file" notice instead of raw bytes.
   - **Grid tables**: **table** inserts an RST grid table (`+---+` with a
     `+===+` header); with the caret in a cell, **\+row↑/↓/−row** and
     **\+col←/→/−col** add/remove rows & columns; typing in a cell **re-aligns
     the whole table live** so column widths always fit the widest cell; **Enter
     in a cell** extends it with a new line (re-rendered) and **Backspace** on a
     fully-blank row line removes it again (inverse of Enter).
   - Optional: **Import** a docx/html/md file (pandoc) into a doc folder,
     single-file or split by heading level — at level ≥ 2 the upper chapters
     become folders with an `index.rst` (own content + toctree) and their
     sub-chapters become the files; images → `_static/images/`.
4. **Save** (write + stage), **Commit**, **Push** — pushes the target branch.
5. **Pull Request.**

## Cross-repo (fork → upstream) PR — the hard part

After several 403/422 dead ends, the working form (confirmed by curl) is:

```
POST /repos/{UPSTREAM}/{repo}/pulls
  head: fork-owner:your-branch   (your branch, in the fork)
  base: main                     (plain branch name, in the upstream)
```

- **Endpoint** = the upstream repo (not the fork).
- **head** = fork-owner-prefixed; **base** = plain branch name.
- Requires a **classic PAT** with `repo` scope. A **fine-grained token** without
  upstream permission gets 403 (root cause of the first 403).
- The UI has a "Base repository" field + "upstream" quick-fill button so you
  choose who owns the base (your fork vs the upstream org).

## Robustness / problem-solving features

- **Proxy support** (corporate network) for both API calls and git, with
  auto-detect from env vars.
- **Diagnostics that show the real cause**: full GitHub per-field errors + raw
  body, plus a pre-flight that names exactly which branch is missing.
- **Secrets handled safely**: SSH key + token written 0600, never echoed back,
  traceback kept server-side.
- **Config in the browser (not on the server)**: the form (repo URL, branches,
  SSH key, token, proxy, work dir) is saved to `localStorage` and auto-filled
  on load; **Export**/Import move a `config.yaml` between machines (handled by
  `config_yaml.js`, PyYAML-compatible). The server keeps no copy — secrets never
  persist on the machine that hosts the app.
- **Doc import (pandoc)**: docx/html/md → RST, single-file or split by heading
  level (level ≥ 2 makes upper chapters into folders with an `index.rst` +
  `toctree`), images extracted to `source/_static/images/`.
- **toctree preview + generate index**: `.. toctree::` directives (a Sphinx
  feature docutils drops) are extracted and rendered in the preview as a boxed
  list of **clickable links** to the referenced files, resolved Sphinx-style
  relative to the `index.rst` (so `foo` → `foo.rst`, `overview/index` → the
  sub-folder's index); missing entries are flagged. A per-folder **📑** button in
  the file tree (`/api/index-generate`) writes an `index.rst` with a toctree of
  every `.rst` file + sub-folder in that directory, prompting to confirm
  overwrite if one already exists. The title prompt is prefilled from the folder
  name (dashes → spaces, first letter of the first word capitalized). In-page
  section-list anchors use each section's real rendered id so they actually scroll.
- **Create files *and* empty directories** (**＋ New**): the new-file dialog has
  a "create as directory" checkbox (or just end the path with `/`), which creates
  the directory — with parents as needed — and a `.gitkeep` placeholder so git
  tracks the empty dir (git can't track empty directories on its own). Normal
  file creation is unchanged; existing paths and traversal are rejected.
- **Configurable work directory + re-open robustness**: choose the clone cache
  location (UI field / `work_root` config key). A "Clone / Open" of an existing
  working copy prompts to **re-fetch** (keep local) or **reset & re-clone** (fresh)
  via a `/api/clone-status` probe, instead of auto-reusing or crashing with "branch
  already exists". A malformed `mode` (e.g. a leaked click-event object) is
  normalized server-side so it can't 500 the endpoint.

## How to run

```
cd <project-root>            # the folder that holds src/ (or the app files)
.venv/bin/python app.py        # then open the served URL
.venv/bin/python test_e2e.py   # 186 checks
node test_table.js            # 62 grid-table checks
node test_tree.js             # 24 file-tree + toctree-resolver checks
node test_url_parse.js        # 15 git-URL owner/repo parsing checks
```

## Not built (out of scope)

- No user accounts / auth (it is a single-user local tool).
- No HTTPS-with-token PR path beyond what is present.
- No multi-repo / branch-diff UI.
- Doc import needs `pandoc` installed (the rest of the editor does not).
- **EMF preview** needs the `emf2svg` Debian package (`emf2svg-conv`), which is
  installed in the Docker image and on the dev host. When it's absent, `.emf`
  files fall back to the "binary file" notice. **WMF** has no reliable Linux
  converter and always falls back to "binary".

Everything requested — WYSIWYG RST editing, SSH-based GitHub/Gitea file
editing, PRs including fork → upstream, browser-side config with
`config.yaml` export/import, docx/html/md import (single or chapter-split,
`_static/images/`), a configurable working directory, and robust re-cloning
(re-open reuses a local branch; reset & re-clone fetches fresh) — is working
and tested.
