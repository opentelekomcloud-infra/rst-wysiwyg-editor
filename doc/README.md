# RST WYSIWYG Editor

A web-based WYSIWYG editor for **reStructuredText** that edits files directly on
a git remote over **SSH** and opens **Pull/Merge Requests** (GitHub & Gitea) from
your changes.

## Features

- **Live WYSIWYG** — split view with a rendered RST preview (via docutils) beside
  the source, plus a formatting toolbar (headings, bold, italic, code, links,
  lists, quotes, notes, code blocks, grid tables).
- **Grid tables** — the **table** button inserts an RST *grid table* (`+---+`
  borders with a `+===+` header separator), not a simple table. When the cursor
  is inside a table cell, six buttons add/remove **rows** (above, below, delete)
  and **columns** (left, right, delete). As you type in any cell, the whole
  table re-aligns live so column widths always fit the widest cell — the classic
  grid-table proportioning is maintained automatically.
- **Undo / redo** — the toolbar ↶ / ↷ buttons (and `Ctrl/Cmd+Z`,
  `Ctrl/Cmd+Shift+Z` / `Ctrl/Cmd+Y`) walk your editing history, including
  toolbar actions.
- **Add a picture** — the 🖼 button uploads an image next to the file (into its
  `<dir>/_static/images/`) and inserts an `.. image::` / `.. figure::` directive
  at the cursor; the picture shows in the live preview.
- **Live warnings** — docutils warnings/errors surface in the preview as you type.
- **Table of contents** — generated from your section headings.
- **Edit files on the remote via SSH** — clone the repo using a pasted private key
  (or your default `~/.ssh` key), browse the file tree, and edit any `.rst` file.
  The left panel is a **hierarchical tree**: folders nest with indentation,
  directories sort before files, and each folder has an expand/collapse triangle
  (top-level folders open by default) plus a **📑 generate-index** button on
  hover. Newly imported / created files appear immediately — the tree refetches
  the file list, so no commit is needed first.
- **Two-branch workflow for protected defaults** — pick a **source** branch
  (the base, usually protected `main`) and a **target** branch (where your commits
  land). The target is **auto-created** from the source if it doesn't exist, so you
  never push straight to a protected branch.
- **Full git workflow** — stage, commit, push, and create feature branches.
- **Configurable work directory** — choose where cloned working copies are cached
  (default `<app>/work`, one subfolder per repo) via the **Working directory**
  field in ⚙ Settings. It's saved in the YAML config.
- **Reset & re-clone** — a one-click escape hatch (top bar) that discards the
  local working copy and re-clones fresh from the remote. When you "Clone / Open"
  a repo that already has a local working copy (e.g. after a server restart /
  browser refresh), the app detects it and **asks** whether to *re-fetch the
  existing* copy (keep local changes, fetch latest) or *reset & re-clone* (discard
  local, fetch fresh) — so you're never surprised by a silent re-use, and re-open
  no longer crashes with "branch already exists".
- **Create Pull/Merge Requests** — from the UI against GitHub or Gitea (auto-detected,
  with self-hosted / GitHub Enterprise override).
- **File management in the file tree** — create a new file or an empty
  **directory** (**＋ New** — tick "create as directory" or end the path with
  `/`), upload one or more files (**⬆ Upload**, with a destination folder),
  per-file **rename/move (→)** and **delete (×)** (shown on hover), and
  per-folder **generate an `index.rst`** (**📑**, shown on folder hover) that
  writes a `.. toctree::` listing every `.rst` file and sub-folder in that
  directory (title prefilled from the folder name). If an `index.rst` already
  exists it prompts to confirm overwriting. Everything is staged, so you review
  it before committing. Empty directories get a `.gitkeep` placeholder so git
  tracks them.
- **Live toctree preview** — the preview renders each `.. toctree::` as a list of
  **clickable links** to the referenced documents (Sphinx-style: `foo` →
  `foo.rst`, `foo/index` → the folder's `index.rst`), so an `index.rst` shows
  exactly where each entry leads. Missing entries are flagged; the in-page
  section list (headings) links to the real section anchors in the rendered
  preview.
- **Word-safe inline formatting** — bold / italic / inline-code / hyperlink apply
  to the selected *word only*, not the trailing space a double-click includes, so
  RST markup never swallows the space and merges the next word.
- **Autosave** — a toggle next to Save. When on, edits save automatically (debounced
  after a pause) and are saved when you switch files, so changes aren't lost when
  you click between files. When off, switching files prompts to save first.
- **Image & binary file views** — clicking an image (`.png`, `.jpg`, `.svg`, …)
  in the file list renders it inline; **EMF** Windows vector images are
  converted to SVG server-side (via the Linux `emf2svg-conv` tool, installed in
  the Docker image) so they preview just like an SVG. **WMF** and other binary
  files show a "binary file" notice instead of dumping raw bytes.
- **Token check** — verify your API token before creating a PR.
- **Browse for the working directory** — a directory browser over the server
  filesystem, instead of typing the clone-cache path by hand.
- **Import documentation** — convert a **docx / HTML / Markdown** file to RST
  (via pandoc), either as a **single file** or **split by heading level**,
  written to a doc folder's `source/` with images extracted to
  `source/_static/images/`. At heading level ≥ 2 the upper chapters become
  **folders** with an `index.rst` (their own content + a toctree) and their
  sub-chapters become the files.

## Stack

- **Backend:** Python 3.8, Flask, docutils, `git` (via `GIT_SSH_COMMAND`); `pandoc` (system binary, for doc import)
- **Frontend:** vanilla HTML/CSS/JS (no build step)
- **Git/PR transport:** SSH for repo ops; GitHub/Gitea REST API for PRs

## Quick start

```bash
cd rst-editor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py                 # http://127.0.0.1:8000
```

Open `http://127.0.0.1:8000`.

## Usage

1. Paste your repo URL (e.g. `git@github.com:owner/repo.git` or an `https://` URL).
2. Set the **source branch** (base — the protected default, e.g. `main`) and the
   **target branch** (head — where edits land). Leave target blank to auto-generate
   a name like `docs/update-2026-09-09`. The target branch is **created from the
   source automatically** if it doesn't already exist.
3. Open the **⚙ Settings** drawer to paste your:
   - **SSH private key** (or leave blank to use your default key)
   - **Commit name / email**
   - **API token** + provider (auto-detect / GitHub / Gitea) and optional
     **API base URL** for self-hosted instances
   - Click **Test token** to verify.
4. Click **Clone / Open** — the repo is cloned from the source branch and checked
   out on the (auto-created) target branch.
5. Pick a `.rst` file from the tree and edit it. The preview updates live.
6. Use **Save** (or `Ctrl/Cmd+S`) to write + stage the file.
7. **Commit**, then **Push** — this pushes the **target** branch (not the protected
   default).
8. **Pull Request** — opens a PR/MR from your **head** branch (your edits, from
   your fork) into the **base** branch of the **Base repository**. For a **fork**,
   set the **Base repository** to the upstream (e.g. `opentelekomcloud-docs/test-repo`)
   to open a PR against the original project. Click the **upstream** button to fill
   it from the saved *Upstream / original repo* setting, then create.

   **How a cross-repo (fork → upstream) PR is created** — this is the shape that
   works on GitHub:
   - The app `POST`s to the **upstream** repo's endpoint:
     `POST /repos/{upstream}/{repo}/pulls`.
   - `head` = `forkowner:your-branch` (your branch, in the fork).
   - `base` = the base branch **name only** (e.g. `main`, in the upstream).

   This requires a **classic personal access token** (`repo` scope). A **fine-grained
   token** that lacks permission on the upstream repo will be rejected (403) even
   though the same request works in the browser/curl. The **Base repository** field
   accepts `owner/repo` or a full URL (https or `git@host:`). Same-repo PRs (no
   fork) post to your own repo with plain branch names.

### Saving your configuration (browser-side)

The configuration (repo URL, source/target branches, SSH key, commit identity,
API token, provider, API base, proxy, work dir) is stored **in your browser**
(`localStorage`), not on the server. The server keeps no copy, so your secrets
never persist on the machine that hosts the app. In **⚙ Settings** →
**Configuration (stored in your browser)**:

- **Save** — writes the current form to this browser's `localStorage`. On the
  next load (or refresh) the form is auto-filled from it.
- **Export** — downloads a `config.yaml` file. This is how you move the config
  to another machine or back it up. Keep it secret (it holds your key and
  token) — never commit it to a repository or share it.
- **Import** — picks a `config.yaml` (e.g. one you exported on another machine),
  loads it into the form, and saves it to this browser.

Example `config.yaml` (the format Export/Import uses):

```yaml
url: git@github.com:owner/repo.git
source_branch: main
target_branch: docs/update-2026-09-09
ssh_key: |-
  your-ssh-private-key-here   # paste your private key here
ssh_host: ""
ssh_port: "22"
commit_name: Your Name
commit_email: you@example.com
api_token: "your-token-here"
provider: github
api_base: ""
proxy: http://proxy.example.com:3128
work_root: ""          # optional: where clones are cached (blank = <app>/work)
```

> The config is a local secret. The browser stores it in `localStorage` (plain
> text, same-origin only) — fine on your own machine, but don't expose the app
> on a shared/public host.

### Working directory & re-cloning

Cloned working copies are cached in the **Working directory** (⚙ Settings →
*Working directory*, or the `work_root` key in the YAML config). The default is
`<app>/work`; each repo gets its own subfolder (a hash of its URL). Set a custom
path (e.g. `~/git-cache`) to keep the cache somewhere you control — it's saved in
the config and pre-filled on load. Use the **Browse…** button next to the field to
pick a folder.

**Idle work-dir cleanup** — to avoid a pile of abandoned clones filling the disk,
a background reaper deletes a cloned work dir once it has been idle (no read,
write, commit, or push) for **`RST_WORK_DIR_TTL` seconds** (default **2 hours**).
Every access to a repo refreshes its "last activity" marker, so an open, in-use
clone is never touched; the interval can be changed with `RST_WORK_DIR_REAP_INTERVAL`
(default 1 h). Only directories the app itself created (marked internally) are ever
removed — files you drop into the work root are left alone.

**Re-opening after a restart** no longer crashes. If you "Clone / Open" a repo
whose working copy already exists locally, the app shows a small dialog with two
safe options:

- **Re-fetch existing** — keep the local copy, fetch the latest from the remote,
  and switch to your target branch. This is the normal choice; it preserves any
  uncommitted local edits.
- **Reset & re-clone** — discard the local working copy and clone fresh from the
  remote (unpushed commits / uncommitted edits are lost; pushed work is safe).
- **Cancel** — do nothing.

There's also a dedicated **Reset & re-clone** button in the top bar if you want to
go straight to a clean slate.

### Saving & viewing files

**Autosave** (checkbox next to the Save button, in the editor toolbar) prevents
losing edits:

- **On** — the current file is saved automatically a moment after you stop
  typing, and it's always saved when you click a different file in the list.
  Your changes are never silently discarded.
- **Off** — if you switch to another file while the current one has unsaved
  changes, the app asks you to save before switching (so you can't lose work,
  but you control exactly when it saves).

The preference is remembered between sessions.

**Viewing files**: clicking a file in the left-hand list opens it.

- **Text files** (`.rst`, `.md`, `.txt`, code, …) open in the editor for editing.
- **Images** (`.png`, `.jpg`, `.jpeg`, `.gif`, `.svg`, `.bmp`, `.webp`, …) render
  inline as a picture. **EMF** (Windows vector metafiles, common in imported
  Word docs) is converted to SVG on the fly using the Linux `emf2svg-conv` tool
  (installed in the Docker image) and shown the same way. **WMF** has no
  reliable Linux converter, so it (like any conversion failure) falls back to
  the binary notice.
- **Other binary files** show a "binary file" notice (with its size) instead of
  dumping unreadable bytes. To change one, edit it outside the app or replace it
  via **Upload**.

**toctree in the preview**: RST `.. toctree::` directives are a *Sphinx* feature
that plain docutils silently drops, so an `index.rst` would otherwise render as
blank. The editor instead surfaces each toctree as a boxed list of **clickable
links** to the documents it references. Entries resolve the way Sphinx does —
relative to the `index.rst` that contains them — so `chapter` links to
`chapter.rst` next to it, and `overview/index` (or just `overview`) links to a
sub-folder's `index.rst`. Clicking a link opens that file. An entry that doesn't
match any file is shown in italics so you can spot a typo; `:caption:` is
honored as the box heading. The in-page section list (the headings of the open
file) also links to the real anchors in the rendered preview.

### Generating an `index.rst` for a folder

Hover a folder in the file tree and click **📑**. The app:

1. Asks for a title. The prompt is prefilled with the folder name — dash
   separators become spaces and the first letter of the first word is capitalized
   (`my-new-thing` → `My new thing`). Blank = "Contents".
2. Scans that folder for every `.rst` file (except `index.rst` itself) and every
   sub-folder, and writes an `index.rst` there containing a `.. toctree::` with
   one entry per item (sub-folders are referenced by name so Sphinx follows them
   to their own `index.rst`).
3. If an `index.rst` already exists in the folder, it asks whether to overwrite.
4. Opens the generated file so you can review and rename it.

The result is staged like every other change (nothing is committed until you are).

### Grid tables

The **table** button inserts a proper RST **grid table**:

```
+--------------+---------------------+
| Parameter    | Type                |
+==============+=====================+
| warn_config  | List data structure |
+--------------+---------------------+
```

- **Insert** — click **table** at any position (it replaces a selection, or
  inserts a fresh 3×3 table with a header row and two data rows).
- **Add / remove rows & columns** — place the caret inside any cell, then use
  **\+row↑ / \+row↓ / −row** (row above / below / delete) or
  **\+col← / \+col→ / −col** (column left / right / delete).
- **Live re-alignment** — as you type in a cell, the whole table is re-rendered
  so every column is as wide as its widest cell and all rows line up. Typing a
  longer value into one cell widens that column across every row, keeping the
  `+---+` / `|` grid in sync automatically (exactly the behaviour the spec asks
  for: editing `warn_config` re-aligns the surrounding rows and columns).
- **Enter in a cell** — instead of inserting a raw newline that breaks the grid,
  **Enter** extends the current cell with a new line and re-renders the table so
  the other columns grow a matching line. Mid-word, it splits the text at the
  caret (e.g. `Header 1` → `Head` / `er 1`). Outside a table, Enter behaves
  normally.
- **Backspace on a blank line** — if every cell in the row is blank on that cell
  line, **Backspace** removes the whole line from the row (the inverse of
  Enter). It works from *any* cell in the row; if any cell still has content on
  that line, Backspace just deletes the preceding character as usual.

The row/column buttons only act when the caret is inside a grid-table cell;
they're no-ops elsewhere.

### Browsing for the working directory

The clone cache lives on the **server** filesystem, so a browser file picker can't
produce a server path. Click **Browse** next to the *Working directory* field to
open a small directory browser over the server's filesystem (the app runs on the
same machine). Navigate with **../**, click a subfolder to step into it, and
press **Use this folder** to set the working directory.

### Importing documentation (docx / html / md → RST)

The **Import** button (top bar) converts an uploaded **Word / HTML / Markdown**
file into reStructuredText and writes it into a documentation folder in the repo,
following the standard layout:

```
umn/          # one folder per document (e.g. umn, api-ref, doc)
  source/
    <files>.rst
    _static/images/   # extracted image assets
```

- **Source file** — a `.docx`, `.html`/`.htm`, or `.md`/`.markdown` file.
- **Doc folder** — where it goes (`umn`, `api-ref`, `doc`, …). The dropdown is
  pre-filled with the folders already in the repo; type a new name to create it.
  The file is written to `<folder>/source/`.
- **Mode** —
  - **Split by chapters** — the document is broken into files by heading level,
    plus a root `index.rst` with a `toctree` (the title is taken from the
    first chapter). A **Heading level** selector controls how deep the split
    goes:
    - **Level 1** — one `.rst` file per top-level chapter (the original
      behaviour; no folders).
    - **Level 2** (or higher) — chapters *above* the chosen level that contain
      sub-chapters become **folders** (e.g. `overview/`), each holding an
      `index.rst` with the chapter's own content **and** a `toctree` to its
      sub-chapters. The sub-chapters at exactly the chosen level become the
      `.rst` files inside that folder (`overview/background.rst`,
      `overview/details.rst`, …). A chapter with no sub-chapters stays a single
      `.rst` file. This mirrors how a Sphinx doc is laid out: a `toctree` of
      folders, each folder an `index.rst`.
  - **Single file** — the whole document in one `.rst` file.
- **Safe filenames** — chapter/file names are slugified and **truncated** to fit
  the filesystem's per-component limit (255 bytes). If two long titles truncate to
  the same prefix, an incrementing index is appended (`…-contents-but`,
  `…-contents-2`, `…-contents-3`) so nothing overwrites another.
- **Images** — embedded images (docx) and local image references are extracted to
  `<folder>/source/_static/images/` and the RST references are rewritten to
  `/_static/images/…` so Sphinx finds them. Remote (http) image URLs are left as-is.

Conversion uses the system **`pandoc`** binary (must be installed — the app shows
a clear error if it's missing). After import the files are **staged** in git — you
still **Commit** and **Push** (and open a PR) as normal.

> Pandoc is only required for import: `apt install pandoc` / `brew install pandoc`.
> The RST editor itself (editing/preview/git/PR) does not need it.

## Configuration (env vars)

| Variable              | Default          | Meaning                              |
|-----------------------|------------------|--------------------------------------|
| `RST_EDITOR_PORT`     | `8000`           | HTTP port                            |
| `RST_WORK_ROOT`       | `<app>/work`     | Default where clones are cached (override via the UI Working directory field) |
| `RST_WORK_DIR_TTL`    | `7200` (2 h)     | How long a cloned work dir may sit idle before the background reaper deletes it |
| `RST_WORK_DIR_REAP_INTERVAL` | `3600` (1 h) | How often the reaper scans for idle work dirs |
| `http_proxy` / `https_proxy` | *(your env)*  | Used for GitHub/Gitea API calls if no proxy is set in the UI |

### Corporate networks / proxies

The GitHub/Gitea **API** calls (token check + Pull Request) go out over HTTPS. If
your network requires a proxy, set it in **⚙ Settings → "Proxy for API calls"**
(e.g. `http://proxy.example.com:3128`) — or click **auto** to fill it from the app's
`http_proxy`/`https_proxy` env vars. Leave it blank to use the process environment
or a direct connection.

> Note: git/SSH clone/push is unaffected by this — SSH doesn't use the HTTP proxy.
> Only the REST API calls (creating PRs, checking tokens) do.

## Security notes

- Clones are cached per-repo under `work/`; the SSH key, when pasted, is written to
  a private (0600) temp file and deleted on `GitRepo.close()`.
- File paths are validated to stay inside the repo (no path traversal).
- `GIT_TERMINAL_PROMPT=0` ensures git never hangs waiting for credentials.
- This is a single-user local tool. Put it behind auth/reverse proxy before exposing.

## Tests

```bash
.venv/bin/python test_e2e.py
```

Runs a full end-to-end flow against a local git repo and a mock GitHub/Gitea API
server: preview → clone → read → edit → save → commit → push → PR →
token-check → proxy → YAML config save/load → path validation → doc import
(docx split + single + image extraction, chapter split, long-filename truncation,
image routing to `_static/images`) → add-picture (upload + collision suffix +
preview inlining) → config-file browse → re-open after restart (no 'branch already
exists' crash), reset & re-clone, custom work directory, word-level formatting
(excludes the trailing space a double-click includes), and file CRUD (new /
upload / rename / delete).

## Project layout

```
rst-editor/
  app.py              Flask app + JSON API
  git_ops.py          Git operations over SSH (GitRepo)
  pr_provider.py      GitHub/Gitea PR client + provider auto-detect (+ proxy)
  rst_render.py       RST -> HTML via docutils (+ warnings, TOC)
  importer.py         docx/html/md -> RST (pandoc) + image extraction + chapter split
  config_io.py        YAML config helpers (client-side export/import)
  mock_api.py         Mock PR API for tests
  test_e2e.py         End-to-end test
  static/
    index.html        Single-page UI
    css/styles.css    Styling
    js/app.js         Frontend logic
  requirements.txt
```
