"""Git operations over SSH with a configurable key.

All git commands run in an isolated environment:
  * GIT_SSH_COMMAND pinned to the user's key (and optional host/port overrides)
  * GIT_TERMINAL_PROMPT=0 so git never hangs waiting for credentials
  * a per-repo working directory under WORK_ROOT

File paths are resolved and validated to stay inside the repository.
"""
import os
import re
import shlex
import subprocess
import hashlib
import tempfile
import time
from typing import Optional

# Directory where cloned working copies live.
WORK_ROOT = os.environ.get("RST_WORK_ROOT", os.path.join(os.path.dirname(__file__), "work"))

# ---------------------------------------------------------------------------
# Abandoned work-dir reaping
#
# Working copies accumulate under the work root (one dir per cloned repo). If a
# dir is not read/written/pushed to for RST_WORK_DIR_TTL seconds it is deleted
# so abandoned clones don't fill the disk. Only directories that carry an
# activity marker file are ever removed, so a user file dropped into the work
# root is never touched.
# ---------------------------------------------------------------------------
# Idle (no access) threshold before a work dir is considered abandoned.
# Override with RST_WORK_DIR_TTL (seconds). Default: 2 hours.
WORK_DIR_TTL = int(os.environ.get("RST_WORK_DIR_TTL", 2 * 60 * 60))
# How often the reaper scans (seconds). Override with RST_WORK_DIR_REAP_INTERVAL.
REAP_INTERVAL = int(os.environ.get("RST_WORK_DIR_REAP_INTERVAL", 60 * 60))

# Activity markers live OUTSIDE each repo (in <root>/.rst_workdir_meta/<name>)
# so they never appear in `git status` / file lists of the working copy.
_META_DIRNAME = ".rst_workdir_meta"


def _meta_dir(work_root: str = None) -> str:
    root = work_root or WORK_ROOT
    return os.path.join(root, _META_DIRNAME)


def _activity_path(repo_dir: str, work_root: str = None) -> str:
    """Marker path for a work dir, stored alongside the work root (not inside
    the repo). Keyed by the root-relative work-dir path so the same repo cloned
    under two different roots gets two independent markers."""
    root = os.path.normpath(work_root or WORK_ROOT)
    rel = os.path.relpath(os.path.normpath(repo_dir), root)
    if rel.startswith("..") or os.path.isabs(rel):
        # Not under this root — fall back to the absolute path.
        rel = os.path.normpath(repo_dir).lstrip(os.sep)
    return os.path.join(_meta_dir(root), rel.replace(os.sep, "_") + ".act")


def touch_activity(repo_dir: str, work_root: str = None) -> None:
    """Mark a work dir as active (called on every access to it).

    Best-effort: a failure to refresh the marker must never break a git
    operation.
    """
    if not repo_dir or not os.path.isdir(repo_dir):
        return
    try:
        md = _meta_dir(work_root)
        os.makedirs(md, exist_ok=True)
        with open(_activity_path(repo_dir, work_root), "w",
                  encoding="utf-8") as fh:
            fh.write("%d\n" % int(time.time()))
    except Exception:
        pass


def _is_app_managed(repo_dir: str, work_root: str = None) -> bool:
    """True only for a dir that is one of our work dirs (it has a marker)."""
    return os.path.isfile(_activity_path(repo_dir, work_root))


def reap_idle_work_dirs(work_root: str = None, ttl: int = WORK_DIR_TTL,
                        now: float = None) -> list:
    """Delete app-managed work dirs idle for longer than ``ttl`` seconds.

    Scans ``work_root`` (default: WORK_ROOT) for immediate sub-directories that
    have a matching activity marker (in the sibling meta dir) and whose marker
    mtime is older than ``ttl``; those are removed. Returns the list of removed
    directories. Safe to call at any time; never touches a directory without a
    marker, nor the work root or its meta dir itself.
    """
    root = work_root or WORK_ROOT
    removed = []
    if not os.path.isdir(root):
        return removed
    now = time.time() if now is None else now
    try:
        entries = os.listdir(root)
    except OSError:
        return removed
    for name in entries:
        if name == _META_DIRNAME:
            continue
        d = os.path.join(root, name)
        try:
            if not os.path.isdir(d):
                continue
            if not _is_app_managed(d, root):
                continue  # not one of our work dirs — leave it alone
            marker = _activity_path(d, root)
            marker_mtime = os.path.getmtime(marker)
            if now - marker_mtime > ttl:
                import shutil
                shutil.rmtree(d, ignore_errors=True)
                if not os.path.isdir(d):
                    removed.append(d)
                try:
                    os.remove(marker)  # drop its marker too
                except OSError:
                    pass
        except OSError:
            continue
    return removed


def prune_repos(known: list, work_root: str = None, ttl: int = WORK_DIR_TTL,
                now: float = None) -> list:
    """Return the repo dirs in ``known`` idle for longer than ``ttl`` seconds.

    Uses the same activity markers as :func:`reap_idle_work_dirs` but does NOT
    delete anything — it lets the caller (which owns the in-memory repo
    registry) decide whether to drop a session. ``known`` is an iterable of
    absolute work-dir paths.
    """
    root = work_root or WORK_ROOT
    now = time.time() if now is None else now
    stale = []
    for d in known:
        if not d or not os.path.isdir(d):
            continue
        marker = _activity_path(d, root)
        try:
            if not os.path.isfile(marker):
                continue
            if now - os.path.getmtime(marker) > ttl:
                stale.append(d)
        except OSError:
            continue
    return stale



class GitError(Exception):
    """Raised when a git command fails; carries stderr for the UI."""

    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def normalize_scp_url(url: str) -> str:
    """Rewrite an SCP-like SSH URL that carries a port into ``ssh://`` form.

    ``git@host:2222/owner/repo.git`` is *invalid* to git: in the ``git@host:
    path`` form everything after the first ``:`` is the repo *path*, so git
    reads ``2222/owner/repo.git`` as the path on the default port 22 and fails
    with "Invalid repo name". The ``ssh://git@host:2222/owner/repo.git`` form
    is the correct way to specify a non-22 port.

    Only the ``git@host:PORT/...`` shape (a digit directly after the colon) is
    rewritten — a normal ``git@host:owner/repo.git`` (no port) is left alone,
    as are any URLs that already use an explicit scheme.
    """
    if not url:
        return url
    if "://" in url:
        return url  # already an explicit scheme (ssh://, git://, https://, ...)
    m = re.match(r"^git@([^:/]+):(\d+)(/.+)$", url)
    if m:
        host, port, path = m.group(1), m.group(2), m.group(3).strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return f"ssh://git@{host}:{port}/{path}.git"
    return url


def workdir_for_url(url: str, work_root: str = None) -> str:
    """Return the per-repo working directory for ``url`` under ``work_root``.

    Pure (no git, no SSH key materialization) so cheap pre-checks can ask where
    a clone lives without building a full GitRepo.
    """
    root = work_root or WORK_ROOT
    digest = hashlib.sha1(normalize_scp_url(url).encode("utf-8")).hexdigest()[:16]
    return os.path.join(root, digest)


def clone_status(url: str, work_root: str = None) -> dict:
    """Cheap probe of the local working copy for ``url`` (no git fetch).

    Returns keys:
      exists      — the workdir exists on disk
      is_repo     — it is a git work tree
      dirty       — has uncommitted changes (staged or unstaged)
      branch      — the checked-out branch name ("" if none)
      head_sha    — short HEAD sha ("" if none)
      has_remote  — an 'origin' remote is configured
    Lets the UI decide whether to reuse (re-fetch) or reset & re-clone.
    """
    d = workdir_for_url(url, work_root)
    res = {"exists": os.path.isdir(d), "is_repo": False, "dirty": False,
           "branch": "", "head_sha": "", "has_remote": False}
    if not res["exists"]:
        return res
    res["is_repo"] = os.path.isdir(os.path.join(d, ".git")) or \
        os.path.isfile(os.path.join(d, ".git"))
    if not res["is_repo"]:
        return res

    def _run(*args) -> str:
        try:
            r = subprocess.run(["git", "-C", d] + list(args), capture_output=True,
                               text=True, timeout=30)
            return (r.stdout or "").strip()
        except Exception:
            return ""

    res["branch"] = _run("rev-parse", "--abbrev-ref", "HEAD")
    res["head_sha"] = _run("rev-parse", "--short", "HEAD")
    remotes = _run("remote")
    res["has_remote"] = "origin" in remotes
    # dirty = any unstaged or staged change, or untracked files.
    status = _run("status", "--porcelain")
    res["dirty"] = bool(status)
    return res


class GitRepo:
    def __init__(
        self,
        url: str,
        ssh_key: str = "",
        work_root: str = WORK_ROOT,
        user_name: str = "",
        user_email: str = "",
        host: str = "",
        port: int = 22,
        branch: str = "main",
        source_branch: str = "",
        target_branch: str = "",
        https_token: str = "",
    ):
        # SCP-like syntax (git@host:path) cannot carry a port, so a URL like
        # git@host:2222/owner/repo.git makes git treat "2222/owner/repo.git"
        # as the repo PATH on port 22 -> "Invalid repo name". Rewrite the
        # common `git@host:PORT/owner/repo.git` form to the explicit
        # ssh:// form, which supports custom ports.
        url = normalize_scp_url(url)
        self.url = url
        self.ssh_key = (ssh_key or "").strip()
        self.https_token = (https_token or "").strip()
        self.user_name = user_name
        self.user_email = user_email
        self.host = host
        self.port = port or 22
        self.branch = branch  # legacy single-branch alias (== source)
        # source_branch = base branch we edit FROM (usually protected main).
        # target_branch = branch we commit/push TO (auto-created if missing).
        self.source_branch = (source_branch or branch or "main").strip() or "main"
        self.target_branch = (target_branch or self.source_branch).strip() or self.source_branch
        self.work_root = work_root
        self._dir = self._workdir_for(url)
        self._key_file = None
        self._key_tmpdir = None
        self._maybe_materialize_key()
        self._authed_url = self._build_authed_url()

    @property
    def is_https(self) -> bool:
        return self.url.lower().startswith(("http://", "https://"))

    def _build_authed_url(self) -> str:
        """For HTTPS remotes, embed the API token so git can authenticate
        non-interactively (HTTPS uses a token as password, not an SSH key).
        SSH remotes are returned unchanged (the key handles auth)."""
        if not self.is_https or not self.https_token:
            return self.url
        m = re.match(r"^(https?://)([^/@]*@)?([^/@]+)(/.*)$", self.url)
        if not m:
            return self.url
        scheme, _, host, path = m.groups()
        # GitHub & Gitea accept any username when a token is used as the password.
        from urllib.parse import quote
        userpass = "x-access-token:" + quote(self.https_token, safe="")
        return f"{scheme}{userpass}@{host}{path}"

    def _maybe_materialize_key(self):
        """Turn the provided key into a usable file path for ``ssh -i``.

        If the value already points to an existing file, use it as-is.
        Otherwise treat it as key *content* and write it to a private temp file
        (0600) that git/ssh can read.
        """
        key = self.ssh_key
        if not key:
            return
        # Already a path to a readable file?
        if os.path.isfile(key) and not key.startswith("-") and "\n" not in key:
            self._key_file = key
            return
        # Otherwise materialize key content to a temp file.
        self._key_tmpdir = tempfile.mkdtemp(prefix="rstsshkey.")
        path = os.path.join(self._key_tmpdir, "id_ed25519")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(key if key.endswith("\n") else key + "\n")
        os.chmod(path, 0o600)
        self._key_file = path

    def close(self):
        """Remove the materialized key file, if we created one."""
        if self._key_tmpdir and os.path.isdir(self._key_tmpdir):
            import shutil
            shutil.rmtree(self._key_tmpdir, ignore_errors=True)
            self._key_tmpdir = None
            self._key_file = None

    # ------------------------------------------------------------------ util

    def _workdir_for(self, url: str) -> str:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.work_root, digest)

    @property
    def dir(self) -> str:
        return self._dir

    def _env(self):
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        ssh_command = self._build_ssh_command()
        if ssh_command:
            env["GIT_SSH_COMMAND"] = ssh_command
        return env

    def _build_ssh_command(self) -> Optional[str]:
        """Compose a GIT_SSH_COMMAND that forces our key, disables prompts,
        and (optionally) pins the host/port."""
        base = "ssh -o StrictHostKeyChecking=no"
        parts = [base]
        if self._key_file:
            parts.append(f"-i {shlex.quote(self._key_file)}")
        if self.port and self.port != 22:
            parts.append(f"-p {int(self.port)}")
        parts.append("-o BatchMode=yes")
        parts.append("-o IdentitiesOnly=yes")
        return " ".join(parts)

    def run(self, *args, cwd: Optional[str] = None, check: bool = True) -> str:
        """Run a git command and return stdout. Raises GitError on failure."""
        cmd = ["git"] + list(args)
        cwd = cwd or self._dir
        # cwd must exist (e.g. git clone into a not-yet-created target dir).
        if not os.path.isdir(cwd):
            cwd = self.work_root if os.path.isdir(self.work_root) else os.getcwd()
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                env=self._env(),
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError("Command timed out: " + " ".join(cmd)) from exc
        if check and proc.returncode != 0:
            raise GitError(
                "git " + " ".join(args) + " failed",
                stderr=proc.stderr or proc.stdout,
            )
        return proc.stdout

    # ---------------------------------------------------------------- setup

    def clone_or_pull(self, mode: str = "reuse") -> str:
        """Clone the repo (from the source/base branch) if the working dir is
        missing/stale, else fetch. Then switch onto the target branch, creating
        it from the source if it doesn't exist yet.

        mode:
          "reuse"  (default) — reuse an existing working copy; just fetch + switch.
          "reset"            — throw away the local copy and re-clone fresh from
                               the remote (discards uncommitted + unpushed local work).
        Returns the working directory path.
        """
        if mode == "reset":
            self.reset()
            os.makedirs(self.work_root, exist_ok=True)
            self.run("clone", "--branch", self.source_branch, self._authed_url, self._dir)
        elif not os.path.isdir(os.path.join(self._dir, ".git")):
            # No working copy (or a corrupt one that isn't a git repo).
            if os.path.exists(self._dir):
                import shutil
                shutil.rmtree(self._dir)
            os.makedirs(self.work_root, exist_ok=True)
            # Clone the source (base) branch with the authed URL so origin
            # (and thus fetch/push) carries credentials. The source branch is
            # the protected default that is guaranteed to exist.
            self.run("clone", "--branch", self.source_branch, self._authed_url, self._dir)
        else:
            # Reuse: re-apply credentials in case the key/token changed, then
            # fetch the latest refs. A failed fetch (e.g. transient) won't block
            # us from working with the existing local copy.
            self.run("remote", "set-url", "origin", self._authed_url)
            self.run("fetch", "origin", check=False)

        # Switch onto (or create) the target branch.
        self.switch_target()
        return self._dir

    def reset(self):
        """Delete the local working copy entirely (work root kept). The next
        clone_or_pull(mode="reset") or missing-dir clone will re-create it."""
        if os.path.exists(self._dir):
            import shutil
            shutil.rmtree(self._dir, ignore_errors=True)

    def switch_target(self) -> str:
        """Checkout the target branch. Resolution order (so a re-open after a
        server restart / browser refresh never crashes on 'branch already exists'):

          1. exists on the remote (origin/<target>)  -> reset it onto the remote.
          2. exists locally only (created last session) -> reuse it as-is.
          3. doesn't exist anywhere                  -> branch it off the source.

        Returns the target branch name.
        """
        remote_exists = self.run(
            "rev-parse", "--verify", "--quiet", f"origin/{self.target_branch}", check=False
        ).strip()
        local_exists = self.run(
            "rev-parse", "--verify", "--quiet", self.target_branch, check=False
        ).strip()
        if remote_exists:
            # Target already exists on the remote -> keep/refresh its work.
            self.run("checkout", "-B", self.target_branch, f"origin/{self.target_branch}")
        elif local_exists:
            # Re-created across a restart (remote doesn't have it yet) -> reuse it.
            # (Previously this path did `checkout -b`, which failed with
            # "A branch named X already exists" on a second open.)
            self.run("checkout", self.target_branch)
        else:
            # Brand-new target: branch off the (fetched) source branch.
            self.run("checkout", "-b", self.target_branch, self.source_branch)
        return self.target_branch

    def _checkout_branch(self, branch: str):
        # Make sure the remote-tracking ref exists; create local branch on first pull.
        exists = self.run("rev-parse", "--verify", "--quiet", f"origin/{branch}", check=False).strip()
        if exists:
            self.run("checkout", branch, check=False)
            self.run("reset", "--hard", f"origin/{branch}")
        else:
            # local-only or new branch
            self.run("checkout", "-B", branch, f"origin/{branch}", check=False)

    # ------------------------------------------------------------- read files

    def list_files(self, pattern: str = "*.rst") -> list:
        """List files in the repo (relative paths) matching a glob pattern."""
        out = self.run("ls-files", "-z", pattern)
        if not out:
            # ls-files only shows tracked files; fall back to all tracked
            out = self.run("ls-files", "-z")
        files = [f for f in out.split("\0") if f]
        return files

    def list_all_files(self) -> list:
        out = self.run("ls-files", "-z")
        return [f for f in out.split("\0") if f]

    def read_file(self, path: str) -> str:
        full = self._safe_path(path)
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def file_exists(self, path: str) -> bool:
        return os.path.isfile(self._safe_path(path))

    def _safe_path(self, path: str) -> str:
        """Resolve a repo-relative path and ensure it stays inside the repo."""
        rel = path.replace("\\", "/").lstrip("/")
        full = os.path.normpath(os.path.join(self._dir, rel))
        if not (full == self._dir or full.startswith(self._dir + os.sep)):
            raise GitError("Path escapes the repository: " + path)
        return full

    # ------------------------------------------------------------- write files

    def write_file(self, path: str, content: str):
        full = self._safe_path(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)

    def write_file_binary(self, path: str, data: bytes):
        """Write raw bytes (used for imported images). Path is kept inside the repo."""
        full = self._safe_path(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)

    def create_dir(self, rel_dir: str) -> str:
        """Create a repository-relative directory and make it visible to git by
        dropping a ``.gitkeep`` placeholder inside it (git cannot track empty
        directories). Idempotent: an existing ``.gitkeep`` is left as-is.

        Parent directories are created as needed. Returns the placeholder path.
        """
        rel = rel_dir.replace("\\", "/").strip().strip("/")
        if not rel:
            raise GitError("Cannot create a directory at the repository root.")
        keep = rel + "/.gitkeep"
        full_keep = self._safe_path(keep)
        if not os.path.exists(full_keep):
            os.makedirs(os.path.dirname(full_keep), exist_ok=True)
            with open(full_keep, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n")
        self.stage_file(keep)
        return keep

    def stage_dir(self, rel_dir: str):
        """`git add` everything under a repo-relative directory (e.g. 'umn')."""
        self.run("add", "-A", "--", rel_dir)

    def stage_file(self, rel_path: str):
        """`git add` a single repo-relative file (e.g. an imported image)."""
        self.run("add", "--", rel_path)

    def delete_file(self, rel_path: str):
        """Delete a file from the working copy and stage the deletion.

        Works for both tracked and untracked files. The path is kept inside the
        repo. Returns True if the file existed (and is now removed).
        """
        rel = rel_path.replace("\\", "/").lstrip("/")
        full = self._safe_path(rel)  # enforces no path traversal
        if not os.path.isfile(full):
            return False
        os.remove(full)
        # Stage the removal. `git rm` stages tracked files; untracked ones just
        # disappear (nothing to stage). -f forces even if it's staged/modified.
        self.run("rm", "-f", "--", rel, check=False)
        # Clean up now-empty parent directories (cosmetic; git ignores empty
        # dirs anyway, but it keeps the working copy tidy).
        self._prune_empty_dirs(rel)
        return True

    def _prune_empty_dirs(self, rel_path: str):
        """Remove empty ancestor directories up to the repo root (best effort)."""
        rel = rel_path.replace("\\", "/").lstrip("/")
        parts = rel.split("/")[:-1]
        while parts:
            d = os.path.join(self._dir, *parts)
            try:
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
                else:
                    break
            except OSError:
                break
            parts.pop()

    def rename_file(self, old_path: str, new_path: str):
        """Rename / move a file (tracked or untracked) and stage the change.

        Returns the new path. Refuses to clobber an existing file.
        """
        old = old_path.replace("\\", "/").lstrip("/")
        new = new_path.replace("\\", "/").lstrip("/")
        if not old or not new:
            raise GitError("Both a source and target path are required.")
        if os.path.basename(old) == os.path.basename(new) and old == new:
            return new  # no-op
        # Validate both endpoints stay inside the repo.
        full_old = self._safe_path(old)
        full_new = self._safe_path(new)
        if not os.path.isfile(full_old):
            raise GitError("File not found: " + old)
        if os.path.exists(full_new):
            raise GitError("A file already exists at: " + new)
        # Ensure the target directory exists.
        os.makedirs(os.path.dirname(full_new), exist_ok=True)
        # Stage the old file first (in case it's a tracked rename), then move.
        self.run("add", "--", old, check=False)
        os.replace(full_old, full_new)
        self.stage_dir(os.path.dirname(new) or ".")
        self._prune_empty_dirs(old)
        return new

    def list_dir(self, rel_dir: str):
        """List files (relative paths) present on disk under a repo-relative dir."""
        base = self._safe_path(rel_dir)
        if not os.path.isdir(base):
            return []
        out = []
        for root, _dirs, files in os.walk(base):
            for f in files:
                full = os.path.join(root, f)
                out.append(os.path.relpath(full, self._dir).replace(os.sep, "/"))
        return out

    def list_top_dirs(self, rel_dir: str):
        """Top-level subfolders under a repo-relative dir (used to offer doc
        folders like umn / api-ref / doc to the import dialog)."""
        base = self._safe_path(rel_dir)
        if not os.path.isdir(base):
            return []
        return sorted(
            d for d in os.listdir(base)
            if os.path.isdir(os.path.join(base, d)) and not d.startswith(".")
        )

    # --------------------------------------------------------------- branches

    def list_branches(self) -> list:
        """Return list of (name, is_local, is_current) for remote branches."""
        out = self.run("branch", "-r", "--list")
        branches = []
        cur = (self.run("rev-parse", "--abbrev-ref", "HEAD")).strip()
        for line in out.splitlines():
            name = line.strip()
            if not name or name.endswith(" -> ") or "HEAD" in name:
                continue
            name = name.replace("origin/", "")
            branches.append({"name": name, "current": name == cur})
        return branches

    def create_branch(self, name: str, from_branch: Optional[str] = None):
        base = from_branch or self.branch
        self.run("checkout", base, check=False)
        # If a local branch already exists, reuse it; otherwise create.
        exists = self.run("rev-parse", "--verify", "--quiet", name, check=False).strip()
        if exists:
            # Reset an existing local branch onto the base so the new work is
            # based on the requested base (avoids stale-branch non-fast-forwards).
            self.run("checkout", name)
            self.run("reset", "--hard", base)
        else:
            self.run("checkout", "-b", name, base)
        return name

    def current_branch(self) -> str:
        return self.run("rev-parse", "--abbrev-ref", "HEAD").strip()

    # ---------------------------------------------------------------- commit

    def stage_all(self):
        self.run("add", "-A")

    def commit(self, message: str):
        if not self.user_name:
            self.run("config", "user.name", "RST Editor")
        if not self.user_email:
            self.run("config", "user.email", "rst-editor@example.com")
        else:
            self.run("config", "user.email", self.user_email)
        # Use a dedicated identity if provided so commits are attributable.
        if self.user_name:
            self.run("config", "user.name", self.user_name)
        self.run("-c", "user.name=" + (self.user_name or "RST Editor"),
                 "-c", "user.email=" + (self.user_email or "rst-editor@example.com"),
                 "commit", "-m", message)

    def push(self, branch: Optional[str] = None):
        branch = branch or self.current_branch()
        self.run("push", "-u", "origin", branch)
        return branch

    def status_changed(self) -> list:
        """Return list of paths with uncommitted changes."""
        out = self.run("status", "--porcelain")
        changed = []
        for line in out.splitlines():
            if not line.strip():
                continue
            p = line[3:].strip()
            # Handle "renamed: old -> new"
            if " -> " in p:
                p = p.split(" -> ")[-1]
            changed.append(p)
        return changed

    # ------------------------------------------------------------------ diff

    def diff(self, staged: bool = True) -> str:
        args = ["diff", "--"]
        if staged:
            args = ["diff", "--cached", "--"]
        out = self.run(*args, check=False)
        return out

    def diff_file(self, path: str) -> str:
        """Show the diff for a path, preferring staged changes (the app stages
        on save), falling back to unstaged."""
        staged = self.run("diff", "--cached", "--", path, check=False)
        if staged.strip():
            return staged
        return self.run("diff", "--", path, check=False)

    # ------------------------------------------------------------- metadata

    def remote_url(self) -> str:
        """Return the clean (credential-free) remote URL for display."""
        return self.url

    @staticmethod
    def mask_url(url: str) -> str:
        """Redact any user:pass@ in a URL before it reaches the UI/logs."""
        import re as _re
        return _re.sub(r"(https?://)([^/@]+)@", r"\1<token>@", url)

    def default_branch(self) -> str:
        # Try to detect the repo's default branch from the origin HEAD ref.
        out = self.run("ls-remote", "--symref", "origin", "HEAD", check=False).strip()
        for line in out.splitlines():
            if line.startswith("ref:"):
                m = re.search(r"refs/heads/(\S+)", line)
                if m:
                    return m.group(1)
        return self.branch
