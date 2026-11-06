// Regression test for git URL owner/repo parsing (frontend).
//
// A long-standing bug: an SCP-like URL with a port — git@host:2222/owner/repo.git —
// was parsed with the port digits as the repo OWNER ("2222/docs/testservice").
// That broke both the PR dialog pre-fill (base repository showed "2222/...") and
// the pre-flight branch check (looked up /repos/2222/docs/... -> 404 -> "branch
// not found"). The parsers must drop the port and return the real owner/repo.
//
// These mirror the functions in src/static/js/app.js (parseOwnerRepo and
// baseRepoOwnerRepo). Keep them in sync when editing app.js.

function parseOwnerRepo(url) {
  let u = (url || "").trim();
  const scp = u.match(/^git@([^:/]+):(\d+)(\/.+)$/);
  if (scp) u = "ssh://git@" + scp[1] + ":" + scp[2] + scp[3];
  let path = "";
  let m = u.match(/^git@[^:]+:(.+?)(?:\.git)?$/);
  if (m) path = m[1];
  else { m = u.match(/^[a-z]+:\/\/(?:[^/]*@)?[^/:]+(?::\d+)?\/(.+?)(?:\.git)?$/); if (m) path = m[1]; }
  path = path.replace(/\.git$/, "").replace(/\/+$/, "");
  const i = path.indexOf("/");
  if (i === -1) return { owner: "", repo: "" };
  return { owner: path.slice(0, i), repo: path.slice(i + 1) };
}

function baseRepoOwnerRepo(v) {
  v = (v || "").trim();
  let path = v;
  const scpPort = v.match(/^git@[^:/]+:\d+\/(.+)$/);
  const scpNoPort = v.match(/^git@[^:/]+:([^/]+\/.+)$/);
  const scheme = v.match(/^[a-z]+:\/\/(?:[^/]*@)?[^/:]+(?::\d+)?\/(.+)$/i);
  if (scpPort) path = scpPort[1];
  else if (scpNoPort) path = scpNoPort[1];
  else if (scheme) path = scheme[1];
  path = path.replace(/\.git$/, "").replace(/^\/+/, "").replace(/\/+$/, "");
  const i = path.indexOf("/");
  if (i === -1) return { owner: "", repo: "" };
  return { owner: path.slice(0, i), repo: path.slice(i + 1) };
}

let pass = 0, fail = 0;
function t(name, got, exp) {
  const g = JSON.stringify(got), e = JSON.stringify(exp);
  if (g === e) { pass++; console.log("PASS", name, g); }
  else { fail++; console.log("FAIL", name, "got", g, "expected", e); }
}

const U = "git@gitea.example.com:2222/docs/testservice.git";

// --- parseOwnerRepo (the fork / base pre-fill) ---
t("parse scp-port", parseOwnerRepo(U), { owner: "docs", repo: "testservice" });
t("parse scp-noport", parseOwnerRepo("git@github.com:testuser/test-repo.git"), { owner: "testuser", repo: "test-repo" });
t("parse ssh-port", parseOwnerRepo("ssh://git@gitea.example.com:2222/docs/testservice.git"), { owner: "docs", repo: "testservice" });
t("parse https", parseOwnerRepo("https://gitea.example.com/owner/repo.git"), { owner: "owner", repo: "repo" });
t("parse ssh-port-no-suffix", parseOwnerRepo("ssh://git@h:2222/a/b"), { owner: "a", repo: "b" });
t("parse scp-port-no-suffix", parseOwnerRepo("git@h:2222/a/b"), { owner: "a", repo: "b" });
t("parse empty", parseOwnerRepo(""), { owner: "", repo: "" });
t("parse no-slash", parseOwnerRepo("git@h:onlyrepo.git"), { owner: "", repo: "" });

// --- baseRepoOwnerRepo (the PR base-repo field) ---
t("base scp-port", baseRepoOwnerRepo(U), { owner: "docs", repo: "testservice" });
t("base scp-noport", baseRepoOwnerRepo("git@github.com:testuser/test-repo.git"), { owner: "testuser", repo: "test-repo" });
t("base plain owner/repo", baseRepoOwnerRepo("opentelekomcloud-docs/test-repo"), { owner: "opentelekomcloud-docs", repo: "test-repo" });
t("base plain owner/repo (upstream value)", baseRepoOwnerRepo("docs/testservice"), { owner: "docs", repo: "testservice" });
t("base https", baseRepoOwnerRepo("https://gitea.example.com/owner/repo.git"), { owner: "owner", repo: "repo" });
t("base ssh-url", baseRepoOwnerRepo("ssh://git@h:2222/a/b.git"), { owner: "a", repo: "b" });
t("base scp-noport .git", baseRepoOwnerRepo("git@h:owner/repo.git"), { owner: "owner", repo: "repo" });

console.log("\n" + pass + " passed, " + fail + " failed");
process.exit(fail === 0 ? 0 : 1);
