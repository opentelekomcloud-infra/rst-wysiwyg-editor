"""RST WYSIWYG editor with GitHub/Gitea SSH + Pull Request support.

A small Flask app: a single-page frontend plus a JSON API that wraps
git (over SSH) and the GitHub/Gitea REST API.
"""
import os
import re
import base64
import hashlib
import mimetypes
import shutil
import subprocess
import tempfile
import threading
import time
import traceback

from flask import Flask, jsonify, request, send_from_directory

import git_ops
from git_ops import GitRepo, GitError
from pr_provider import PRClient, ProviderError
from rst_render import render_rst
import importer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"))

# In-memory registry of open repos: repo_id -> GitRepo.
_REPOS = {}


def _get_repo(repo_id):
    repo = _REPOS.get(repo_id)
    if repo is None:
        raise GitError("Unknown or expired repo session. Re-clone it.")
    # Every access to a repo counts as activity — keeps the idle reaper from
    # deleting a work dir the user is still working in.
    git_ops.touch_activity(repo.dir, getattr(repo, "work_root", None))
    return repo


# ---------------------------------------------------------------------------
# Idle work-dir reaper
#
# A background daemon thread periodically scans each known work root and deletes
# app-managed working copies that have been idle (no read/write/commit/push)
# for longer than git_ops.WORK_DIR_TTL (default 2h, RST_WORK_DIR_TTL). It also
# drops the matching entries from the in-memory _REPOS so stale sessions are
# released. Only directories carrying an activity marker are ever removed.
# ---------------------------------------------------------------------------
_reaper_thread = None
_reaper_lock = threading.Lock()


def _reaper_loop():
    while True:
        try:
            # Collect every distinct work root currently in use (plus the
            # default), and reap each of them.
            roots = {git_ops.WORK_ROOT}
            for repo in list(_REPOS.values()):
                wr = getattr(repo, "work_root", None)
                if wr:
                    roots.add(wr)
            # Drop in-memory sessions whose work dir is idle (release memory
            # before the dir is removed from disk).
            for wr in roots:
                stale = git_ops.prune_repos(list(_REPOS.keys()), wr)
                for d in stale:
                    _REPOS.pop(d, None)
            removed = []
            for wr in roots:
                removed += git_ops.reap_idle_work_dirs(wr)
            if removed:
                import logging
                logging.getLogger("rst-editor").info(
                    "reaper: deleted %d idle work dir(s): %s",
                    len(removed), removed)
        except Exception:
            pass
        time.sleep(git_ops.REAP_INTERVAL)


def start_reaper():
    """Start the idle-work-dir reaper thread exactly once (idempotent)."""
    global _reaper_thread
    with _reaper_lock:
        if _reaper_thread is not None and _reaper_thread.is_alive():
            return
        _reaper_thread = threading.Thread(target=_reaper_loop,
                                          name="rst-workdir-reaper", daemon=True)
        _reaper_thread.start()


def _err(e):
    tb = traceback.format_exc()
    # Surface the rich provider reason (ProviderError.detail) — e.g. GitHub's
    # per-field validation errors — alongside the generic message. Keep stderr
    # for git errors. Drop the traceback from the client response (it may
    # contain tokens or local paths); log it instead.
    detail = getattr(e, "detail", "") or getattr(e, "stderr", "") or ""
    try:
        app.logger.warning("API error: %s\n%s", str(e), tb)
    except Exception:
        pass
    return jsonify({"error": str(e), "detail": detail}), 500


def _clean_work_root(raw):
    """Normalize an optional work-directory override to an absolute path.

    Blank / missing -> the app default (RST_WORK_ROOT env or <app>/work).
    Otherwise expand ~ and return an absolute path (created on demand).
    """
    default = os.environ.get(
        "RST_WORK_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")
    )
    p = (raw or "").strip()
    if not p:
        return default
    p = os.path.expanduser(p)
    p = os.path.normpath(os.path.abspath(p))
    os.makedirs(p, exist_ok=True)
    return p


def _norm_mode(raw):
    """Coerce the clone mode to a known value.

    Accepts 'reuse' (default) or 'reset'. Anything else — including a non-string
    like the browser's click-event object that can leak through — falls back to
    'reuse' so a bad value never 500s the endpoint.
    """
    m = raw
    if not isinstance(m, str):
        return "reuse"
    m = m.strip().lower()
    return m if m in ("reuse", "reset") else "reuse"


# --------------------------------------------------------------------- pages

@app.route("/")
def index():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "index.html")


# ------------------------------------------------------------------- preview

@app.route("/api/preview", methods=["POST"])
def api_preview():
    data = request.get_json(silent=True) or {}
    source = data.get("source", "")
    try:
        result = render_rst(source)
    except Exception as e:  # docutils is very tolerant, but be safe
        return jsonify({"html": "", "warnings": [str(e)], "errors": [], "title": None, "toc": None})
    # Inline any relative image whose file exists in the repo, so the live
    # preview actually shows pictures added via the Add-picture button.
    repo_id = data.get("repo_id")
    path = data.get("path")
    if repo_id and path and result.get("html"):
        try:
            result["html"] = _inline_images(result["html"], _get_repo(repo_id), path)
        except Exception:
            pass  # preview is best-effort; never break it over an image
    return jsonify(result)


_IMG_SRC_RE = re.compile(r'<img[^>]*\bsrc="([^"]+)"')


def _inline_images(html, repo, rst_path):
    """Replace relative <img src=".."> with base64 data URIs when the image
    exists next to the .rst file (resolved relative to the .rst's directory)."""
    if "<img" not in html:
        return html

    def _sub(m):
        src = m.group(1)
        # Only local refs: skip remote / data URIs / anchors.
        if src.startswith(("http://", "https://", "data:", "mailto:", "#")):
            return m.group(0)
        # Candidate paths to try, in order.
        candidates = []
        if src.startswith("/"):
            # Sphinx-absolute ("/_static/images/x.png"): resolved against the
            # doc *source* root, which is the repo root for a flat import but
            # "<doc>/source/" for a chapter split. The importer always puts
            # images in "<doc>/source/_static/images/", so try each ancestor
            # of the .rst's directory (which includes both the repo root and
            # the source root) as the base.
            rel = src.lstrip("/").replace(os.sep, "/")
            base = os.path.dirname(rst_path).replace(os.sep, "/")
            while True:
                candidates.append((base + "/" + rel).lstrip("/"))
                if not base or "/" not in base:
                    break
                base = base.rsplit("/", 1)[0]
        else:
            base_dir = os.path.dirname(rst_path) or ""
            candidates.append(os.path.normpath(
                os.path.join(base_dir, src.lstrip("./").replace(os.sep, "/"))
            ).replace(os.sep, "/"))
        full = None
        for cand in candidates:
            try:
                p = repo._safe_path(cand)
            except GitError:
                continue
            if os.path.isfile(p):
                full = p
                break
        if not full:
            return m.group(0)
        with open(full, "rb") as fh:
            raw = fh.read()
        b64 = base64.b64encode(raw).decode("ascii")
        mime = mimetypes.guess_type(full)[0]
        if not mime:
            sniffed = _sniff_image_mime(raw[:16])
            mime = sniffed[0] if sniffed else "image/png"
        return m.group(0).replace('src="' + src + '"',
                                  'src="data:' + mime + ';base64,' + b64 + '"')

    return _IMG_SRC_RE.sub(_sub, html)


# --------------------------------------------------------------------- clone

@app.route("/api/work-root", methods=["GET"])
def api_work_root():
    """Return the default work directory (so the UI can pre-fill the field)."""
    return jsonify({"work_root": _clean_work_root("")})


@app.route("/api/clone-status", methods=["POST"])
def api_clone_status():
    """Cheap probe of whether a working copy for this URL already exists locally.

    Called by the UI *before* cloning so it can offer "re-fetch existing" vs
    "reset & re-clone" instead of silently auto-reusing (or crashing). No git
    network access, no SSH key needed — it just inspects the local directory.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Repository URL is required."}), 400
    work_root = _clean_work_root(data.get("work_root"))
    st = git_ops.clone_status(url, work_root)
    st["work_dir"] = git_ops.workdir_for_url(url, work_root)
    st["work_root"] = work_root
    return jsonify(st)


@app.route("/api/clone", methods=["POST"])
def api_clone():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Repository URL is required."}), 400
    token = (data.get("token") or "").strip()
    is_https = url.lower().startswith(("http://", "https://"))
    # HTTPS remotes authenticate with a token (not an SSH key). Fail fast with
    # a clear message if one was entered without a token.
    if is_https and not token:
        return jsonify({
            "error": "That's an HTTPS URL, which needs a token to push "
                     "(SSH keys only work with git@ URLs). Either paste your "
                     "API token in ⚙ Settings and retry, or switch the URL to "
                     "its SSH form: git@host:owner/repo.git",
        }), 400
    source_branch = (data.get("source_branch") or data.get("branch") or "main").strip() or "main"
    target_branch = (data.get("target_branch") or "").strip() or source_branch
    mode = _norm_mode(data.get("mode"))
    # Optional work-directory override (where working copies are cached). Blank
    # falls back to the app default (RST_WORK_ROOT env or <app>/work).
    work_root = _clean_work_root(data.get("work_root"))
    repo = GitRepo(
        url=url,
        ssh_key=data.get("ssh_key", ""),
        user_name=data.get("user_name", ""),
        user_email=data.get("user_email", ""),
        host=data.get("host", ""),
        port=int(data.get("port", 22) or 22),
        branch=source_branch,
        source_branch=source_branch,
        target_branch=target_branch,
        https_token=token,
        work_root=work_root,
    )
    try:
        repo.clone_or_pull(mode=mode)
    except GitError as e:
        msg = e.stderr or ""
        if "could not read Username" in msg or "Authentication failed" in msg:
            msg = ("Authentication failed. If this is an HTTPS URL, make sure a "
                   "valid token is saved in ⚙ Settings. If it's an SSH URL, "
                   "check your private key. " + msg)
        return jsonify({"error": str(e), "detail": msg}), 500
    _REPOS[repo.dir] = repo
    # Mark the freshly-cloned working copy as active so the idle reaper gives it
    # a full TTL window before considering it abandoned.
    git_ops.touch_activity(repo.dir, repo.work_root)
    return jsonify({
        "repo_id": repo.dir,
        "branch": repo.target_branch,          # branch you're editing/committing to
        "source_branch": repo.source_branch,    # base (usually protected main)
        "target_branch": repo.target_branch,    # head (auto-created if missing)
        "files": repo.list_files(),
        "all_files": repo.list_all_files(),
        "remote_url": repo.remote_url(),
        "work_root": repo.work_root,
    })


@app.route("/api/branches", methods=["POST"])
def api_branches():
    data = request.get_json(silent=True) or {}
    try:
        repo = _get_repo(data.get("repo_id"))
        return jsonify({"branches": repo.list_branches(), "current": repo.current_branch()})
    except GitError as e:
        return _err(e)


@app.route("/api/branch", methods=["POST"])
def api_branch():
    data = request.get_json(silent=True) or {}
    try:
        repo = _get_repo(data.get("repo_id"))
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Branch name required."}), 400
        repo.create_branch(name, from_branch=data.get("from_branch"))
        return jsonify({"branch": repo.current_branch(), "created": name})
    except GitError as e:
        return _err(e)


# --------------------------------------------------------------- file access

@app.route("/api/file", methods=["GET"])
def api_file_get():
    repo_id = request.args.get("repo_id")
    path = request.args.get("path", "")
    try:
        repo = _get_repo(repo_id)
        if not repo.file_exists(path):
            return jsonify({"error": "File not found: " + path}), 404
        content = repo.read_file(path)
        # Best-effort preview alongside content
        preview = render_rst(content) if path.lower().endswith((".rst", ".rest")) else None
        # Detect binary (non-text) content so the UI can render images / show a
        # "binary file" notice instead of dumping raw bytes.
        kind, data_uri = _classify_file(repo, path, content)
        return jsonify({
            "path": path, "content": content, "preview": preview, "size": len(content),
            "kind": kind,          # "text" | "image" | "binary"
            "data_uri": data_uri,  # set only for images
        })
    except GitError as e:
        return _err(e)


# Image extensions we know how to render as <img> in the browser.
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
# Files with an unrecognized extension that are really images, by magic bytes.
# Pandoc extracts docx media under its raw media name, and docx media can carry
# a ".tmp" extension while actually being a PNG (or other format) — detect it
# from the content so it previews like any other picture.
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"RIFF", "image/webp"),   # RIFF....WEBP
    (b"\x00\x00\x01\x00", "image/x-icon"),
)


def _sniff_image_mime(raw):
    """Return (mime, ext) when *raw* starts with a known image magic, else None.

    ``RIFF`` needs the ``WEBP`` fourcc at offset 8 to avoid matching other
    RIFF containers (AVI, WAV).
    """
    for magic, mime in _IMAGE_MAGIC:
        if raw[:len(magic)] == magic:
            if magic == b"RIFF" and raw[8:12] != b"WEBP":
                continue
            ext = {
                "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                "image/bmp": ".bmp", "image/tiff": ".tiff", "image/webp": ".webp",
                "image/x-icon": ".ico",
            }[mime]
            return mime, ext
    return None
# Windows vector metafiles: browsers can't render .emf/.wmf in <img>, so we
# convert them server-side. EMF is handled natively on Linux via the
# `emf2svg-conv` tool (Debian package `emf2svg`) -> SVG; WMF has no reliable
# Linux converter and falls back to the "binary file" notice.
_METAFILE_EXT = {".emf", ".wmf"}
# EMF-only: this is what `emf2svg-conv` actually decodes.
_EMF_EXT = {".emf"}

# Where converted metafiles are cached (keyed by content hash) so repeated
# views of the same image don't re-run the (somewhat slow) converter.
_METAFILE_CACHE = os.path.join(tempfile.gettempdir(), "rst_metafile_cache")


def _render_metafile_to_svg(raw, ext):
    """Render an EMF metafile to SVG bytes using `emf2svg-conv` (Linux-native).

    Returns SVG bytes, or None when the converter isn't installed, the file
    isn't a recognized EMF, or conversion fails — the UI then shows the
    "binary file" notice instead. Results are cached by content hash.

    Only EMF is supported (WMF has no dependable Linux converter). `ext` is the
    lower-cased file extension (".emf"/".wmf") and is used as the temp-file
    suffix so the converter reads the right format.
    """
    if ext not in _EMF_EXT:
        return None
    if len(raw) < 4 or raw[0:4] != b"\x01\x00\x00\x00":
        # Not a recognizable EMF header (WMF starts with a different magic);
        # don't bother running the converter.
        return None
    conv = shutil.which("emf2svg-conv")
    if not conv:
        return None

    digest = hashlib.sha256(raw).hexdigest()
    cache_dir = os.path.join(_METAFILE_CACHE, digest[:2])
    cache_path = os.path.join(cache_dir, digest + ".svg")
    try:
        if os.path.isfile(cache_path):
            with open(cache_path, "rb") as fh:
                return fh.read()
    except Exception:
        pass

    tmp_in = os.path.join(tempfile.gettempdir(), "rst_emf_" + digest + ext)
    tmp_out = os.path.join(tempfile.gettempdir(), "rst_emf_" + digest + ".svg")
    try:
        with open(tmp_in, "wb") as fh:
            fh.write(raw)
        subprocess.run(
            [conv, "-i", tmp_in, "-o", tmp_out],
            capture_output=True, text=True, timeout=60,
        )
        if not os.path.isfile(tmp_out):
            return None
        with open(tmp_out, "rb") as fh:
            svg = fh.read()
        if not svg:
            return None
        # Cache it.
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path, "wb") as fh:
            fh.write(svg)
        return svg
    except Exception:
        return None
    finally:
        for p in (tmp_in, tmp_out):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass


def _classify_file(repo, path, text_content):
    """Return (kind, data_uri).

    kind is "image" for known image types (data_uri = base64 data: URI),
    "binary" for other non-text files, or "text" for normal text files.
    EMF metafiles are converted to SVG (via `emf2svg-conv`, Linux-native) and
    returned as "image" so they preview like any other picture; WMF and any
    conversion failure fall back to "binary".
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in _IMAGE_EXT:
        try:
            full = repo._safe_path(path)
            with open(full, "rb") as fh:
                raw = fh.read()
        except Exception:
            return "binary", None
        if not raw:
            return "binary", None
        mime = mimetypes.guess_type(path)[0]
        if not mime:
            # Unknown extension but the content is an image (e.g. a docx media
            # file named "image1.tmp" that is really a PNG): sniff the magic
            # bytes so the picture still previews.
            sniffed = _sniff_image_mime(raw)
            mime = sniffed[0] if sniffed else (
                "image/svg+xml" if ext == ".svg" else None)
        if not mime:
            return "binary", None
        data_uri = "data:%s;base64,%s" % (
            mime, base64.b64encode(raw).decode("ascii"))
        return "image", data_uri
    if ext in _METAFILE_EXT:
        try:
            full = repo._safe_path(path)
            with open(full, "rb") as fh:
                raw = fh.read()
        except Exception:
            return "binary", None
        svg = _render_metafile_to_svg(raw, ext) if raw else None
        if svg:
            return "image", "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")
        return "binary", None
    # Unknown extension: sniff the bytes — docx media extracted by pandoc can
    # be a real PNG/JPEG/etc. under a name like "image1.tmp".
    if ext not in _IMAGE_EXT and ext not in _METAFILE_EXT:
        try:
            full = repo._safe_path(path)
            with open(full, "rb") as fh:
                raw = fh.read(16)
        except Exception:
            raw = b""
        sniffed = _sniff_image_mime(raw) if raw else None
        if sniffed:
            try:
                full = repo._safe_path(path)
                with open(full, "rb") as fh:
                    raw = fh.read()
            except Exception:
                return "binary", None
            return "image", "data:%s;base64,%s" % (
                sniffed[0], base64.b64encode(raw).decode("ascii"))
    # Heuristic: does the content contain a NUL byte or look non-UTF8?
    if "\x00" in text_content:
        return "binary", None
    try:
        text_content.encode("utf-8")
    except UnicodeEncodeError:
        return "binary", None
    return "text", None


@app.route("/api/files", methods=["GET"])
def api_files():
    repo_id = request.args.get("repo_id")
    try:
        repo = _get_repo(repo_id)
        return jsonify({"rst": repo.list_files(), "all": repo.list_all_files()})
    except GitError as e:
        return _err(e)


@app.route("/api/save", methods=["POST"])
def api_save():
    data = request.get_json(silent=True) or {}
    try:
        repo = _get_repo(data.get("repo_id"))
        path = data.get("path", "")
        if not path:
            return jsonify({"error": "Path required."}), 400
        content = data.get("content", "")
        repo.write_file(path, content)
        repo.stage_file(path)
        diff = repo.diff_file(path)
        preview = render_rst(content) if path.lower().endswith((".rst", ".rest")) else None
        return jsonify({"saved": True, "path": path, "staged": repo.status_changed(), "diff": diff, "preview": preview})
    except GitError as e:
        return _err(e)


@app.route("/api/file-new", methods=["POST"])
def api_file_new():
    """Create a new file (or an empty directory) in the repo and stage it.

    Body: {repo_id, path, content?, is_dir?}. A request is treated as a
    directory when ``is_dir`` is true, or when ``path`` ends with ``/`` — in that
    case a ``.gitkeep`` placeholder is dropped inside it (git can't track empty
    directories). Refuses to overwrite an existing file/dir.
    """
    data = request.get_json(silent=True) or {}
    try:
        repo = _get_repo(data.get("repo_id"))
        raw_path = (data.get("path") or "").strip()
        if not raw_path:
            return jsonify({"error": "Path is required."}), 400
        is_dir = bool(data.get("is_dir")) or raw_path.endswith("/")
        path = raw_path.rstrip("/")
        if not path:
            return jsonify({"error": "Path is required."}), 400
        if is_dir:
            if os.path.isdir(repo._safe_path(path)):
                return jsonify({"error": "A directory already exists at: " + path}), 400
            if repo.file_exists(path):
                return jsonify({"error": "A file already exists at: " + path}), 400
            keep = repo.create_dir(path)
            return jsonify({
                "created": True, "path": path, "is_dir": True,
                "placeholder": keep, "changed": repo.status_changed(),
                "files": repo.list_files(), "all_files": repo.list_all_files(),
            })
        if repo.file_exists(path):
            return jsonify({"error": "A file already exists at: " + path}), 400
        content = data.get("content", "")
        repo.write_file(path, content)
        repo.stage_file(path)
        return jsonify({
            "created": True, "path": path, "changed": repo.status_changed(),
            "files": repo.list_files(), "all_files": repo.list_all_files(),
        })
    except GitError as e:
        return _err(e)


@app.route("/api/file-upload", methods=["POST"])
def api_file_upload():
    """Upload a file (or several) into the repo at a chosen destination folder.

    Multipart: file(s) -> 'files' (one or more); form field 'dest_dir' = the
    repo-relative folder to place them in (e.g. 'umn/source' or ''). Existing
    files are not clobbered: on a name clash a numeric suffix is added.
    """
    data = request.form.to_dict()
    uploads = request.files.getlist("files")
    if not uploads or all(not (f and f.filename) for f in uploads):
        return jsonify({"error": "No file uploaded."}), 400
    try:
        repo = _get_repo(data.get("repo_id"))
    except GitError as e:
        return _err(e)

    dest_dir = (data.get("dest_dir") or "").replace("\\", "/").strip().strip("/")
    written = []
    for up in uploads:
        if not (up and up.filename):
            continue
        payload = up.read()
        if not payload:
            return jsonify({"error": "Uploaded file is empty: " + up.filename}), 400
        if len(payload) > 25 * 1024 * 1024:
            return jsonify({"error": "File too large (max 25 MB): " + up.filename}), 400
        # Sanitise the basename (drop any directory the browser sent).
        base = os.path.basename(up.filename.replace("\\", "/"))
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"
        stem, ext = os.path.splitext(safe)
        stem = stem[:180]
        dest = (dest_dir + "/" + safe) if dest_dir else safe
        n = 2
        while repo.file_exists(dest):
            dest = (dest_dir + "/" if dest_dir else "") + f"{stem}-{n}{ext}"
            n += 1
        repo.write_file_binary(dest, payload)
        repo.stage_file(dest)
        written.append(dest)
    return jsonify({
        "uploaded": True, "files": written, "changed": repo.status_changed(),
        "all_files": repo.list_all_files(),
    })


@app.route("/api/file-delete", methods=["POST"])
def api_file_delete():
    """Delete a file from the repo and stage the deletion.

    Body: {repo_id, path}.
    """
    data = request.get_json(silent=True) or {}
    try:
        repo = _get_repo(data.get("repo_id"))
        path = (data.get("path") or "").strip()
        if not path:
            return jsonify({"error": "File path is required."}), 400
        existed = repo.delete_file(path)
        if not existed:
            return jsonify({"error": "File not found: " + path}), 404
        return jsonify({
            "deleted": True, "path": path, "changed": repo.status_changed(),
            "files": repo.list_files(), "all_files": repo.list_all_files(),
        })
    except GitError as e:
        return _err(e)


def _gen_index_content(directory, all_files, title):
    """Build the contents of an ``index.rst`` *toctree* for ``directory``.

    For every ``.rst`` file directly under ``directory`` (except ``index.rst``
    itself) and every sub-directory, emit a toctree entry. Sub-directories are
    referenced by their name (Sphinx resolves ``<name>`` to ``<name>/index``),
    so a folder containing its own ``index.rst`` is linked as a child document.
    Files are referenced by their bare stem (Sphinx resolves ``<stem>`` to
    ``<stem>.rst``).
    """
    d = (directory or "").replace("\\", "/").strip().strip("/")
    prefix = (d + "/") if d else ""
    # Collect direct children from the file list (avoids a separate scan).
    rst_files = []
    subdirs = set()
    for f in all_files:
        f = f.replace("\\", "/")
        if not f.startswith(prefix):
            continue
        rest = f[len(prefix):]
        if "/" in rest:  # something deeper than the top level of this dir
            subdirs.add(rest.split("/", 1)[0])
            continue
        base = rest
        if base.lower().endswith(".rst"):
            if base.lower() != "index.rst":
                rst_files.append(base[:-4])  # bare stem
        # non-rst files are ignored for the toctree

    entries = sorted(subdirs) + sorted(rst_files)
    parts = []
    t = (title or "").strip() or ("Contents" if d else "Contents")
    parts.append(t)
    parts.append("=" * max(3, len(t)))
    if not entries:
        parts.append("")
        parts.append("_Nothing to list yet._")
    else:
        parts.append("")
        parts.append(".. toctree::")
        for e in entries:
            parts.append("   " + e)
    return "\n".join(parts).rstrip() + "\n", entries


def _dir_default_title(directory):
    """Default index title from a directory name: the basename, with dash
    separators replaced by spaces and the first letter of the first word
    capitalized (``my-cool-dir`` -> ``My cool dir``; ``umn`` -> ``Umn``).
    Falls back to ``Contents`` for the repo root / empty name."""
    base = (directory or "").replace("\\", "/").strip().strip("/").split("/")[-1]
    if not base:
        return "Contents"
    words = [w for w in base.split("-") if w]
    if not words:
        return base
    first = words[0][0].upper() + words[0][1:]
    return " ".join([first] + words[1:])


@app.route("/api/index-generate", methods=["POST"])
def api_index_generate():
    """Generate an ``index.rst`` with a ``.. toctree::`` over the contents of a
    directory, and stage it.

    Body: {repo_id, dir, title?, overwrite?}. If ``index.rst`` already exists in
    ``dir`` and ``overwrite`` is not true, returns a conflict (409) so the client
    can prompt the user first. A blank/omitted ``title`` defaults to the
    directory name (dashes -> spaces, first word capitalized).
    """
    data = request.get_json(silent=True) or {}
    try:
        repo = _get_repo(data.get("repo_id"))
        directory = (data.get("dir") or "").strip()
        title = (data.get("title") or "").strip()
        if not title:
            title = _dir_default_title(directory)
        overwrite = bool(data.get("overwrite"))
        d = directory.replace("\\", "/").strip().strip("/")
        index_path = (d + "/index.rst") if d else "index.rst"
        all_files = repo.list_all_files()
        if repo.file_exists(index_path) and not overwrite:
            return jsonify({"conflict": True, "path": index_path}), 409
        content, entries = _gen_index_content(directory, all_files, title)
        repo.write_file(index_path, content)
        repo.stage_file(index_path)
        return jsonify({
            "created": True, "path": index_path, "content": content,
            "entries": entries, "changed": repo.status_changed(),
            "files": repo.list_files(), "all_files": repo.list_all_files(),
        })
    except GitError as e:
        return _err(e)


@app.route("/api/file-rename", methods=["POST"])
def api_file_rename():
    """Rename / move a file and stage the change.

    Body: {repo_id, path, new_path}.
    """
    data = request.get_json(silent=True) or {}
    try:
        repo = _get_repo(data.get("repo_id"))
        old = (data.get("path") or "").strip()
        new = (data.get("new_path") or "").strip()
        if not old or not new:
            return jsonify({"error": "Both a current and a new path are required."}), 400
        if repo.file_exists(new):
            return jsonify({"error": "A file already exists at: " + new}), 400
        new = repo.rename_file(old, new)
        return jsonify({
            "renamed": True, "old": old, "new": new, "changed": repo.status_changed(),
            "files": repo.list_files(), "all_files": repo.list_all_files(),
        })
    except GitError as e:
        return _err(e)


# ------------------------------------------------------------------- import

@app.route("/api/doc-folders", methods=["GET"])
def api_doc_folders():
    """List top-level folders that look like documentation folders (contain a
    'source' subfolder), so the import dialog can offer them. Returns e.g.
    ["api-ref", "doc", "umn"]."""
    repo_id = request.args.get("repo_id")
    try:
        repo = _get_repo(repo_id)
    except GitError as e:
        return _err(e)
    out = []
    for d in repo.list_top_dirs("."):
        if os.path.isdir(repo._safe_path(os.path.join(d, "source"))):
            out.append(d)
    return jsonify({"folders": out})


@app.route("/api/import", methods=["POST"])
def api_import():
    """Convert an uploaded docx / html / md file to RST and write it into a
    documentation folder, following the project layout:

        <doc_folder>/source/<slug>.rst          (single)
        <doc_folder>/source/<slug>.rst + index.rst  (split by chapter)
        <doc_folder>/source/_static/images/<img>  (extracted images)

    The converted files are staged (not committed) so the user can review the
    generated RST in the editor before committing/pushing.
    """
    data = request.form.to_dict()
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "No file uploaded."}), 400
    try:
        repo = _get_repo(data.get("repo_id"))
    except GitError as e:
        return _err(e)

    doc_folder = (data.get("doc_folder") or "doc").strip()
    slug = (data.get("slug") or "imported").strip()
    mode = (data.get("mode") or "single").strip()
    doc_title = (data.get("title") or "Documentation").strip() or "Documentation"
    # Heading level to split on: 1 = one file per top-level chapter (no folders);
    # >=2 = chapters above that level become folders with an index.rst.
    try:
        split_level = int(data.get("split_level") or 1)
    except (TypeError, ValueError):
        split_level = 1
    split_level = max(1, min(split_level, 6))
    fmt = os.path.splitext(file.filename)[1].lstrip(".").lower()

    # Validate the doc folder: single segment, no traversal.
    if not re.match(r"^[A-Za-z0-9._-]+$", doc_folder):
        return jsonify({"error": "Invalid doc folder name."}), 400
    source_rel = f"{doc_folder}/source"
    # Absolute source dir inside the working copy (images land here during
    # conversion, then get written into the repo via GitRepo).
    source_abs = repo._safe_path(source_rel)

    src_bytes = file.read()
    if not src_bytes:
        return jsonify({"error": "Uploaded file is empty."}), 400

    # Convert + extract images into source_abs/_static/images.
    try:
        with tempfile.TemporaryDirectory() as media:
            rst, image_names = importer.convert_to_rst(
                src_bytes, fmt, media, source_abs
            )
    except importer.ImportError as e:
        return jsonify({"error": str(e), "detail": getattr(e, "stderr", "")}), 400

    # Build the set of (repo-relative path, content) to write.
    files = {}
    if mode == "split":
        split_files = importer.split_by_levels(rst, level=split_level, title=doc_title)
        if not split_files:
            return jsonify({"error": "Could not find any content to import."}), 400
        for rel, content in split_files.items():
            files[f"{source_rel}/{rel}"] = content
    else:
        # Slugify + truncate the single-file name so an over-long uploaded
        # filename can't blow the filesystem's per-component limit.
        files[f"{source_rel}/{importer.make_stem(slug)}.rst"] = rst.strip() + "\n"

    # Write RST files into the repo.
    for rel, content in files.items():
        repo.write_file(rel, content)

    # Copy extracted images from the temp source dir into the repo.
    images = []
    if image_names:
        img_dir_abs = os.path.join(source_abs, "_static", "images")
        for name in image_names:
            src = os.path.join(img_dir_abs, name)
            if os.path.isfile(src):
                with open(src, "rb") as fh:
                    repo.write_file_binary(
                        f"{source_rel}/_static/images/{name}", fh.read()
                    )
                images.append(f"{source_rel}/_static/images/{name}")

    # Stage the whole doc folder so it's ready to commit.
    repo.stage_dir(doc_folder)
    return jsonify({
        "imported": True,
        "mode": mode,
        "doc_folder": doc_folder,
        "files": sorted(files.keys()),
        "images": images,
        "changed": repo.status_changed(),
    })


@app.route("/api/image", methods=["POST"])
def api_image():
    """Upload an image next to the edited file (its <dir>/_static/images) and
    return the relative ref so the RST snippet renders in the live preview.

    Form fields:
      repo_id   — the cloned repo
      file      — the image (multipart)
      rel_path  — the current .rst file (relative); images go to its sibling
                  <dir>/_static/images/ so a relative ref renders in the preview
      name      — optional explicit filename (sanitised); default = upload name
    """
    data = request.form.to_dict()
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "No image uploaded."}), 400
    try:
        repo = _get_repo(data.get("repo_id"))
    except GitError as e:
        return _err(e)

    rel_path = (data.get("rel_path") or "index.rst").strip()
    base_dir = os.path.dirname(rel_path) or ""
    img_dir = (base_dir + "/_static/images") if base_dir else "_static/images"

    # Sanitise the chosen/uploaded name to a safe filename.
    raw_name = (data.get("name") or file.filename or "image").strip()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._") or "image"
    stem, ext = os.path.splitext(safe)
    stem = stem[:180]
    dest_name = stem + (ext or "")
    existing = {os.path.basename(p) for p in repo.list_dir(img_dir)}
    n = 2
    while dest_name in existing:
        dest_name = f"{stem}-{n}{ext}"
        n += 1

    payload = file.read()
    if not payload:
        return jsonify({"error": "Uploaded image is empty."}), 400
    if len(payload) > 15 * 1024 * 1024:
        return jsonify({"error": "Image too large (max 15 MB)."}), 400

    img_rel = f"{img_dir}/{dest_name}"
    repo.write_file_binary(img_rel, payload)
    repo.stage_file(img_rel)

    # Relative ref from the .rst's directory to the image, so the preview
    # (whose source_link is the .rst) inlines it as a data URI.
    ref = _relpath(base_dir, img_dir, dest_name)

    return jsonify({
        "ok": True,
        "path": img_rel,
        "name": dest_name,
        "ref": ref,
        "changed": repo.status_changed(),
    })


def _relpath(from_dir, to_dir, name):
    """A relative path from `from_dir` to `to_dir/name` (no leading ./)."""
    from_parts = [p for p in from_dir.split("/") if p]
    to_parts = [p for p in to_dir.split("/") if p]
    common = 0
    while (common < len(from_parts) and common < len(to_parts)
           and from_parts[common] == to_parts[common]):
        common += 1
    ups = [".."] * (len(from_parts) - common)
    rest = to_parts[common:] + [name]
    return "/".join(ups + rest)


# ------------------------------------------------------------------- commit

@app.route("/api/status", methods=["GET"])
def api_status():
    repo_id = request.args.get("repo_id")
    try:
        repo = _get_repo(repo_id)
        return jsonify({"changed": repo.status_changed(), "branch": repo.current_branch()})
    except GitError as e:
        return _err(e)


@app.route("/api/commit", methods=["POST"])
def api_commit():
    data = request.get_json(silent=True) or {}
    try:
        repo = _get_repo(data.get("repo_id"))
        msg = (data.get("message") or "").strip()
        if not msg:
            return jsonify({"error": "Commit message required."}), 400
        if not repo.status_changed():
            return jsonify({"error": "Nothing to commit."}), 400
        repo.commit(msg)
        return jsonify({"committed": True, "branch": repo.current_branch(), "changed": repo.status_changed()})
    except GitError as e:
        return _err(e)


@app.route("/api/push", methods=["POST"])
def api_push():
    data = request.get_json(silent=True) or {}
    try:
        repo = _get_repo(data.get("repo_id"))
        branch = (data.get("branch") or "").strip() or repo.current_branch()
        pushed = repo.push(branch)
        return jsonify({"pushed": True, "branch": pushed})
    except GitError as e:
        return _err(e)


# --------------------------------------------------------------------- pull

@app.route("/api/pr", methods=["POST"])
def api_pr():
    data = request.get_json(silent=True) or {}
    try:
        repo = _get_repo(data.get("repo_id"))
        token = (data.get("token") or "").strip()
        if not token:
            return jsonify({"error": "API token required to create a Pull Request."}), 400
        # head = the branch we edited (target); base = the branch we branched
        # from (source, usually protected main). This is a normal protected-main
        # PR: head -> base.
        head = (data.get("head") or "").strip() or repo.target_branch
        base = (data.get("base") or "").strip() or repo.source_branch
        title = (data.get("title") or "").strip()
        if not title:
            return jsonify({"error": "PR title required."}), 400
        body = data.get("body", "")
        client = PRClient(
            url=repo.url,
            token=token,
            provider=data.get("provider", ""),
            api_base=data.get("api_base", ""),
            owner=data.get("owner", ""),
            repo=data.get("repo", ""),
            proxy=data.get("proxy", ""),
        )
        result = client.create_pull_request(
            head=head,
            base=base,
            title=title,
            body=body,
            # Pre-flight branch existence (turns an opaque 422 into a precise
            # "branch X not found in owner/repo"). head_repo = the fork (where
            # head must exist); base_repo = the target project (where base must
            # exist, i.e. the upstream for a fork PR).
            head_branch=data.get("head_branch", ""),
            head_repo=data.get("head_repo", ""),
            base_branch=data.get("base_branch", ""),
            base_repo=data.get("base_repo", ""),
        )
        return jsonify(result)
    except (GitError, ProviderError) as e:
        return _err(e)


@app.route("/api/detect-proxy", methods=["GET"])
def api_detect_proxy():
    """Report the proxy the app's process would use (from env), so the UI can
    pre-fill the proxy field. urllib reads these case-insensitively."""
    env = os.environ
    candidates = ["https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"]
    for name in candidates:
        val = (env.get(name) or "").strip()
        if val:
            return jsonify({"proxy": val, "source": name})
    return jsonify({"proxy": "", "source": None})


@app.route("/api/browse-dir", methods=["GET"])
def api_browse_dir():
    """List a server-side directory for the working-directory picker.
    Returns the resolved dir, its parent, and its subdirectories."""
    d = request.args.get("dir") or os.path.expanduser("~")
    try:
        d = os.path.abspath(os.path.expanduser(d))
    except Exception:
        return jsonify({"error": "Invalid directory."}), 400
    if not os.path.isdir(d):
        return jsonify({"error": "Not a directory: " + d}), 400
    try:
        subdirs = sorted(
            x for x in os.listdir(d)
            if os.path.isdir(os.path.join(d, x)) and not x.startswith(".")
        )
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    parent = os.path.dirname(d) if d != os.path.dirname(d) else ""
    return jsonify({
        "dir": d,
        "parent": parent,
        "subdirs": subdirs,
    })


@app.route("/api/token-check", methods=["POST"])
def api_token_check():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    token = (data.get("token") or "").strip()
    try:
        client = PRClient(
            url=url,
            token=token,
            provider=data.get("provider", ""),
            api_base=data.get("api_base", ""),
            owner=data.get("owner", ""),
            repo=data.get("repo", ""),
            proxy=data.get("proxy", ""),
        )
        return jsonify(client.check_token())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


if __name__ == "__main__":
    # Start the idle-work-dir reaper (deletes abandoned clones after
    # RST_WORK_DIR_TTL of inactivity, default 2h). Daemon thread; exits with
    # the process.
    start_reaper()
    port = int(os.environ.get("RST_EDITOR_PORT", "8000"))
    # Bind host: defaults to loopback (local use). Set RST_EDITOR_HOST=0.0.0.0
    # when running in a container so published ports are reachable from outside.
    host = os.environ.get("RST_EDITOR_HOST", "127.0.0.1")
    app.run(host=host, port=port, debug=False)
