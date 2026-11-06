"""Create Pull Requests / Merge Requests on GitHub or Gitea.

Uses the GitHub-compatible REST API. Gitea is API-compatible with GitHub for
the ``/repos/{owner}/{repo}/pulls`` endpoint, so one code path covers both.

Provider + base URL are auto-detected from the clone URL, but can be
overridden (e.g. GitHub Enterprise, self-hosted Gitea).
"""
import json
import re
import urllib.request
import urllib.error
import urllib.parse
from typing import Optional


class ProviderError(Exception):
    def __init__(self, message: str, detail: str = "", code: int = 0):
        super().__init__(message)
        self.detail = detail
        self.code = code  # HTTP status (403/404/422/...) when the API failed, else 0


def detect_provider(url: str):
    """Return (provider, host, owner, repo, default_base_url).

    provider is 'github' or 'gitea'.
    """
    host = owner = repo = ""
    # SCP-like syntax can't carry a port (git@host:2222/o/r.git is invalid);
    # normalize it to ssh:// so the host/port/path are parsed consistently
    # (otherwise the port digits get read as the repo owner).
    from git_ops import normalize_scp_url
    url = normalize_scp_url(url)
    # SSH form: git@host:owner/repo(.git)  OR  ssh://git@host:port/owner/repo(.git)
    m = re.match(r"^git@([^:/]+):(.+?)(?:\.git)?$", url)
    if m:
        host = m.group(1)
        path = m.group(2)
    else:
        # Explicit-scheme form: ssh://git@host:port/owner/repo(.git), or
        # HTTPS: scheme://host/owner/repo(.git)
        m = re.match(r"^[a-z]+://(?:[^/@]*@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?$", url)
        if m:
            host = m.group(1)
            path = m.group(2)
        else:
            raise ProviderError("Could not parse repository URL: " + url)

    path = path.strip("/")
    if path.count("/") < 1:
        raise ProviderError("Repository URL must be owner/repo: " + url)
    owner, repo = path.split("/", 1)
    repo = repo.strip("/")

    host_l = host.lower()
    if "github.com" in host_l or host_l.endswith(".github.com"):
        provider = "github"
        base = "https://api.github.com"
    else:
        provider = "gitea"
        scheme = "https" if not host.startswith(("http://", "https://")) and "localhost" not in host else "http"
        # If the URL already carried a scheme, preserve it.
        if "://" in host:
            scheme, host = host.split("://", 1)
        base = f"{scheme}://{host}/api/v1"

    return provider, host, owner, repo, base


class PRClient:
    def __init__(
        self,
        url: str,
        token: str,
        provider: str = "",
        api_base: str = "",
        owner: str = "",
        repo: str = "",
        proxy: str = "",
    ):
        self.url = url
        self.token = (token or "").strip()
        self.proxy = (proxy or "").strip()
        try:
            (
                detected_provider,
                self.host,
                self.owner,
                self.repo,
                self.api_base,
            ) = detect_provider(url)
        except ProviderError:
            # Local / unparseable URL: rely on overrides.
            detected_provider = "gitea"
            self.host = ""
            self.owner = ""
            self.repo = ""
            self.api_base = ""
        self.provider = provider or detected_provider
        if api_base:
            self.api_base = api_base.rstrip("/")
        if owner:
            self.owner = owner
        if repo:
            self.repo = repo
        if not self.api_base:
            raise ProviderError("No API base URL available (URL not parseable and no api_base given).")
        self._opener = self._build_opener()

    def _headers(self):
        return {
            "Accept": "application/json",
            "Authorization": "token " + self.token,
            "Content-Type": "application/json",
        }

    def _build_opener(self):
        """Build a urllib opener.

        - If an explicit proxy was given (UI field), use it for http+https —
          but never for localhost (local mock APIs, local dev servers).
        - Otherwise return None and let urllib fall back to the process's
          http_proxy/https_proxy env vars (or a direct connection).
        """
        if not self.proxy:
            return None
        no_proxy = "localhost,127.0.0.1,::1"
        handler = urllib.request.ProxyHandler(
            {"http": self.proxy, "https": self.proxy, "no": no_proxy}
        )
        return urllib.request.build_opener(handler)

    def _urlopen(self, req, timeout):
        # Never route loopback hosts through a proxy (a configured corporate
        # proxy can't reach our localhost) — this keeps local mock APIs and
        # local dev servers reachable. Standard no_proxy behavior.
        host = (urllib.parse.urlparse(req.full_url).hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1"):
            return urllib.request.urlopen(req, timeout=timeout)
        if self._opener is not None:
            try:
                return self._opener.open(req, timeout=timeout)
            except urllib.error.URLError:
                # The explicit proxy couldn't complete the connection (timeout,
                # refused, dead proxy). Fall back to a direct connection rather
                # than failing outright.
                return urllib.request.urlopen(req, timeout=timeout)
        return urllib.request.urlopen(req, timeout=timeout)

    def _request(self, method: str, path: str, payload: Optional[dict] = None):
        url = self.api_base + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with self._urlopen(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                status = resp.getcode()
                return status, (json.loads(body) if body else {}), body
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            try:
                detail = json.loads(body)
            except Exception:
                detail = {}
            # GitHub top-level message, plus the per-field `errors` array it
            # returns on 422/422-style validation failures.
            msg = detail.get("message") or detail.get("error") or (body or "no detail")
            errs = detail.get("errors")
            if isinstance(errs, list) and errs:
                fields = []
                for er in errs:
                    if isinstance(er, dict):
                        # Show EVERY key/value so a sparse error can't hide info.
                        parts = [f"{k}={v}" for k, v in er.items() if v not in ("", None)]
                        fields.append(" ".join(parts))
                if fields:
                    msg = msg + " | " + " ; ".join(fields)
            # Always append the raw body if it carries more than we already show
            # (e.g. GitHub omits `message`/`field` in some 422 bodies).
            if body and detail and body.strip() not in msg:
                msg = msg + " | raw: " + body.strip()
            # Add an actionable hint for the common failure modes.
            hint = ""
            low = (msg or "").lower()
            if e.code == 403:
                if "personal access token" in low or "not accessible" in low:
                    hint = (" — the token lacks write/repo scope. Reissue it with the "
                            "'repo' scope (GitHub) or full access (Gitea). A read-only "
                            "token lets you clone but not open PRs.")
                elif "restricted" in low or "not found" in low:
                    hint = " — the repo may be private/invisible to this token, or the token lacks access to it."
                else:
                    hint = " — forbidden. Check the token's scopes and repo access."
            elif e.code == 401:
                hint = " — bad or expired token."
            elif e.code == 404:
                hint = " — repo not found (wrong owner/repo, or token has no access)."
            elif e.code == 422:
                if "different" in low:
                    hint = (" — GitHub says head and base must be different (they're not "
                            "in a fork PR, this is usually a branch-mismatch/already-merged issue).")
                elif "no commits" in low or "between" in low:
                    hint = (" — no commits between base and head. Make sure your target "
                            "branch actually contains new commits vs the upstream base.")
                elif "branch" in low or "not" in low:
                    hint = (" — a referenced branch could not be resolved. Check that "
                            "the head branch exists in your fork (owner-prefixed) and "
                            "the base branch exists in the upstream (plain name), and "
                            "that the branch is actually pushed. Re-run Push, then "
                            "retry the PR.")
                else:
                    hint = " — GitHub rejected the PR request; see the field detail above."
            raise ProviderError(
                f"API {method} {path} failed ({e.code})",
                detail=((msg or "no detail") + hint),
                code=e.code,
            )
        except urllib.error.URLError as e:
            reason = str(e.reason)
            hint = ""
            if "timed out" in reason or "timeout" in reason.lower():
                hint = (" (connection timed out — if you're on a corporate "
                        "network, set the Proxy in ⚙ Settings, e.g. "
                        "http://host:port)")
            raise ProviderError(
                "Could not reach " + self.api_base,
                detail=reason + hint,
            )

    def branch_exists(self, owner: str, repo: str, branch: str):
        """Check a branch exists in owner/repo.

        Returns:
          True  — branch exists (GitHub 200).
          False — branch does not exist (GitHub 404).
          None  — could not determine (network error, 403/no read access, 5xx).
                  The caller should treat None as "unknown" and proceed, so a
                  read-permission quirk never blocks a PR that would succeed.
        """
        try:
            status, data, _ = self._request(
                "GET", f"/repos/{owner}/{repo}/branches/{urllib.parse.quote(branch, safe='')}"
            )
            return status in (200, 204)
        except ProviderError as e:
            if e.code == 404:
                return False
            return None

    def create_pull_request(
        self,
        head: str,
        base: str,
        title: str,
        body: str = "",
        head_branch: str = "",
        head_repo: str = "",
        base_branch: str = "",
        base_repo: str = "",
    ) -> dict:
        """Open a PR/MR. Returns dict with 'url', 'html_url', 'number', 'state'.

        head/base are the full API values (optionally owner-prefixed, e.g.
        'forkowner:feature'). head_branch/head_repo and base_branch/base_repo
        identify which repo each branch must exist in, so we can pre-flight them
        and return a precise error when one is missing — which is what GitHub
        only reports as an opaque 422."""
        # Pre-flight: confirm each branch exists where it should, and say which
        # one is missing. Only abort on a definitive "not found" (False); if we
        # can't determine (None — e.g. no read access to the upstream), proceed
        # and let GitHub's own validation give the final answer.
        if head_branch and head_repo:
            h_owner, h_repo = head_repo.split("/", 1)
            if self.branch_exists(h_owner, h_repo, head_branch) is False:
                raise ProviderError(
                    f"Head branch '{head_branch}' not found in {head_repo}",
                    detail=(
                        "Your target branch must exist on your fork — it's the 'head' "
                        "of the PR. Run Push to push it to your fork, then retry. If "
                        "the branch name has a slash (e.g. docs/update), make sure it "
                        "is a real branch on your fork, not just a tag or path."
                    ),
                )
        if base_branch and base_repo:
            b_owner, b_repo = base_repo.split("/", 1)
            if self.branch_exists(b_owner, b_repo, base_branch) is False:
                raise ProviderError(
                    f"Base branch '{base_branch}' not found in {base_repo}",
                    detail=(
                        "The base branch (usually the upstream's default) must exist "
                        "in that repo. Check the Base branch name — e.g. use 'master' "
                        "if the upstream's default is master, not 'main'."
                    ),
                )
        payload = {
            "head": head,
            "base": base,
            "title": title,
            "body": body or "",
        }
        status, data, _ = self._request(
            "POST", f"/repos/{self.owner}/{self.repo}/pulls", payload
        )
        # Prefer html_url (human link). Fall back to url.
        html_url = data.get("html_url") or data.get("url") or ""
        return {
            "provider": self.provider,
            "number": data.get("number"),
            "title": data.get("title", title),
            "html_url": html_url,
            "raw_url": data.get("url", ""),
            "state": data.get("state", "open"),
            "head": head,
            "base": base,
        }

    def check_token(self) -> dict:
        """Auth / permission check. Returns {'ok': bool, 'user': str, 'provider': str,
        'error'?: str}.

        Probes the endpoint the PR workflow actually needs — ``/repos/{owner}/
        {repo}`` — which only requires ``read:repository`` (present on the common
        GitHub "repo" scope and Gitea repo tokens). This is more meaningful than
        ``/user``, which demands the ``read:user`` scope: a token that can clone
        and open PRs but lacks ``read:user`` (e.g. a Gitea token scoped to
        ``write:repository``) would be wrongly reported as bad by a ``/user`` check.

        Falls back to ``/user`` only when owner/repo aren't known (unparseable
        local URLs), where a ``/user`` 200 still confirms the token is valid.
        """
        try:
            if self.owner and self.repo:
                status, data, _ = self._request(
                    "GET", f"/repos/{self.owner}/{self.repo}"
                )
                user = data.get("full_name") or f"{self.owner}/{self.repo}"
                note = "repo accessible"
            else:
                status, data, _ = self._request("GET", "/user")
                user = data.get("login") or data.get("username") or "unknown"
                note = "token valid"
            return {
                "ok": True,
                "user": user,
                "provider": self.provider,
                "note": note,
            }
        except ProviderError as e:
            # A 403 on /repos/... can still be a usable token if the failure is
            # purely about /user-style scope (rare) — but most /repos 403s mean
            # the token genuinely lacks repo access, so surface it as-is with
            # the actionable hint _request already attached.
            return {"ok": False, "user": "", "provider": self.provider, "error": str(e)}
