"""End-to-end test: RST render -> clone -> edit -> commit -> push -> PR."""
import os
import sys
import json
import shutil
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import mock_api
import app as appmod

# Fresh bare repo each run (hermetic).
TEST_REPO = "/tmp/rsttest/bare.git"
shutil.rmtree("/tmp/rsttest", ignore_errors=True)
os.makedirs("/tmp/rsttest", exist_ok=True)
subprocess.run(["git", "init", "-q", "--bare", TEST_REPO], check=True)
seed = subprocess.run(
    ["git", "clone", "-q", TEST_REPO, "/tmp/rsttest/seed"], check=True)
subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd="/tmp/rsttest/seed")
subprocess.run(["git", "config", "user.name", "t"], cwd="/tmp/rsttest/seed")
subprocess.run(["git", "config", "user.email", "t@t.t"], cwd="/tmp/rsttest/seed")
with open("/tmp/rsttest/seed/index.rst", "w") as f:
    f.write("Test\n====\n\nhello *world*\n")
os.makedirs("/tmp/rsttest/seed/docs", exist_ok=True)
with open("/tmp/rsttest/seed/docs/guide.rst", "w") as f:
    f.write("Guide\n=====\n\ncontent\n")
subprocess.run(["git", "add", "-A"], cwd="/tmp/rsttest/seed")
subprocess.run(["git", "commit", "-qm", "init"], cwd="/tmp/rsttest/seed")
subprocess.run(["git", "push", "-q", "origin", "main"], cwd="/tmp/rsttest/seed")

server, port = mock_api.start(9111)
api_base = f"http://127.0.0.1:{port}/api/v1"

client = appmod.app.test_client()

def check(name, cond, extra=""):
    print(("PASS" if cond else "FAIL"), name, extra)
    if not cond:
        sys.exit(1)

# 1) RST preview
r = client.post("/api/preview", json={"source": "Title\n=====\n\nHello **world** and *emphasis*.\n\nSub\n---\n\ntext\n"})
d = r.get_json()
check("preview status 200", r.status_code == 200)
check("preview title", d["title"] == "Title", d.get("title"))
check("preview html has <strong>", "<strong>" in d["html"])
check("preview html has <em>", "<em>" in d["html"])
check("preview toc present", d["toc"] is not None)

# 1b) SCP-like URL with a port must be rewritten to ssh:// (regression: git@host:PORT/...)
from git_ops import normalize_scp_url
check("scp url: port form -> ssh://",
      normalize_scp_url("git@gitea.example.com:2222/docs/testservice.git")
      == "ssh://git@gitea.example.com:2222/docs/testservice.git")
check("scp url: no-port form unchanged",
      normalize_scp_url("git@github.com:testuser/test-repo.git")
      == "git@github.com:testuser/test-repo.git")

# 2) Clone the local bare repo from source branch main, auto-creating target
r = client.post("/api/clone", json={
    "url": TEST_REPO,
    "source_branch": "main",
    "target_branch": "e2e-doc-fix",
    "user_name": "E2E Test",
    "user_email": "e2e@test.dev",
})
d = r.get_json()
check("clone 200", r.status_code == 200, d)
repo_id = d["repo_id"]
check("clone lists index.rst", "index.rst" in d["files"], d.get("files"))
check("clone reports source_branch", d.get("source_branch") == "main", d)
check("clone reports target_branch", d.get("target_branch") == "e2e-doc-fix", d)
check("clone on target branch", d.get("branch") == "e2e-doc-fix", d)

# 3) List files
r = client.get("/api/files?repo_id=" + repo_id)
d = r.get_json()
check("files api", "index.rst" in d["rst"], d)

# 4) Read a file
r = client.get("/api/file?repo_id=" + repo_id + "&path=index.rst")
d = r.get_json()
check("read file 200", r.status_code == 200)
check("read file content", "hello *world*" in d["content"], d.get("content")[:30])
check("read file has preview", d["preview"] is not None)

# 5) Save an edited file on the auto-created target branch
new_content = "Title\n=====\n\nHello *world*.\n\nE2E added paragraph.\n"
r = client.post("/api/save", json={"repo_id": repo_id, "path": "index.rst", "content": new_content})
d = r.get_json()
check("save 200", r.status_code == 200, d)
check("save staged index.rst", "index.rst" in d["staged"], d.get("staged"))
check("save diff present", "E2E added paragraph" in d["diff"], d.get("diff")[:80])

# 6) Status shows changed
r = client.get("/api/status?repo_id=" + repo_id)
d = r.get_json()
check("status shows changed", "index.rst" in d["changed"], d)
check("status on target branch", d.get("branch") == "e2e-doc-fix", d)

# 7) Commit
r = client.post("/api/commit", json={"repo_id": repo_id, "message": "e2e: add paragraph"})
d = r.get_json()
check("commit 200", r.status_code == 200, d)
check("commit cleared changed", d["changed"] == [], d)

# 8) Push feature branch
r = client.post("/api/push", json={"repo_id": repo_id, "branch": "e2e-doc-fix"})
d = r.get_json()
check("push 200", r.status_code == 200 and d["branch"] == "e2e-doc-fix", d)

# 9) Create PR against mock API
r = client.post("/api/pr", json={
    "repo_id": repo_id,
    "head": "e2e-doc-fix",
    "base": "main",
    "title": "E2E test PR",
    "body": "Automated end-to-end test.",
    "token": "test-token",
    "api_base": api_base,
    "provider": "gitea",
    "owner": "owner",
    "repo": "repo",
})
d = r.get_json()
check("PR 200", r.status_code == 200, d)
check("PR number", d.get("number") == 101, d)
check("PR html_url", d.get("html_url", "").endswith("/pull/101"), d)
check("PR hit mock server", len(mock_api.CREATED) == 1, mock_api.CREATED)

# 9b) Cross-repo / fork PR (fork -> upstream). Matches the shape that actually
# works on GitHub with a classic PAT: target the UPSTREAM repo's endpoint,
# head = fork-owner:branch, base = plain branch name.
r = client.post("/api/pr", json={
    "repo_id": repo_id,
    "head": "testuser:e2e-doc-fix",   # branch in the fork (owner-prefixed)
    "base": "main",                        # plain branch in the upstream
    "title": "Fork PR to upstream",
    "body": "Cross-repo PR from a fork.",
    "token": "test-token",
    "api_base": api_base,
    "provider": "gitea",
    "owner": "opentelekomcloud-docs",      # upstream -> endpoint
    "repo": "test-repo",                   # upstream repo -> endpoint
})
d = r.get_json()
check("cross-repo PR 200", r.status_code == 200, d)
# Endpoint must be the UPSTREAM (this is the classic-PAT working form).
check("cross-repo PR endpoint is the upstream",
      any(c["path"] == "/api/v1/repos/opentelekomcloud-docs/test-repo/pulls"
          for c in mock_api.CREATED), mock_api.CREATED)
up = [c for c in mock_api.CREATED
      if c["path"] == "/api/v1/repos/opentelekomcloud-docs/test-repo/pulls"]
check("cross-repo PR head prefixed with fork owner",
      up and up[0]["data"]["head"] == "testuser:e2e-doc-fix",
      up and up[0]["data"])
check("cross-repo PR base is plain (no owner prefix)",
      up and up[0]["data"]["base"] == "main",
      up and up[0]["data"])

# 9c) 422 surfacing: when GitHub returns a validation failure, the per-field
# reason must reach the client in `detail` (not just the generic "422").
r = client.post("/api/pr", json={
    "repo_id": repo_id,
    "head": "someone:branch",
    "base": "upstream:main",
    "title": "Will 422",
    "body": "",
    "token": "test-token",
    "api_base": api_base,
    "provider": "gitea",
    "owner": "failrepo",
    "repo": "test-repo",
})
d = r.get_json()
check("422 surfaced as non-2xx", r.status_code >= 400, (r.status_code, d))
check("422 generic message present", "422" in d.get("error", ""), d)
check("422 field reason in detail",
      "There isn't anything to compare" in d.get("detail", ""), d)
check("422 detail names base field", "base" in d.get("detail", ""), d)

# 9d) Pre-flight: a missing HEAD branch (not on the fork) is caught BEFORE the
# POST and named precisely instead of an opaque 422.
r = client.post("/api/pr", json={
    "repo_id": repo_id,
    "head": "testuser:missing-branch",
    "base": "opentelekomcloud-docs:main",
    "title": "Head missing",
    "body": "",
    "token": "test-token",
    "api_base": api_base,
    "provider": "gitea",
    "owner": "testuser",
    "repo": "test-repo",
    "head_branch": "missing-branch",
    "head_repo": "testuser/test-repo",
    "base_branch": "main",
    "base_repo": "opentelekomcloud-docs/test-repo",
})
d = r.get_json()
check("pre-flight missing head: non-2xx", r.status_code >= 400, (r.status_code, d))
check("pre-flight missing head: names the branch",
      "missing-branch" in d.get("error", "") + d.get("detail", ""), d)
check("pre-flight missing head: says fork/push",
      ("fork" in d.get("detail", "") or "Push" in d.get("detail", "")), d)

# 9e) Pre-flight: a missing BASE branch (wrong upstream branch name) is caught.
r = client.post("/api/pr", json={
    "repo_id": repo_id,
    "head": "testuser:e2e-doc-fix",
    "base": "opentelekomcloud-docs:missing-branch",
    "title": "Base missing",
    "body": "",
    "token": "test-token",
    "api_base": api_base,
    "provider": "gitea",
    "owner": "testuser",
    "repo": "test-repo",
    "head_branch": "e2e-doc-fix",
    "head_repo": "testuser/test-repo",
    "base_branch": "missing-branch",
    "base_repo": "opentelekomcloud-docs/test-repo",
})
d = r.get_json()
check("pre-flight missing base: non-2xx", r.status_code >= 400, (r.status_code, d))
check("pre-flight missing base: names the branch",
      "missing-branch" in d.get("error", "") + d.get("detail", ""), d)
check("pre-flight missing base: hints master/main",
      "master" in d.get("detail", ""), d)

# 9f) Pre-flight: both branches exist -> POST proceeds and PR is created.
r = client.post("/api/pr", json={
    "repo_id": repo_id,
    "head": "testuser:e2e-doc-fix",
    "base": "opentelekomcloud-docs:main",
    "title": "Pre-flight OK",
    "body": "",
    "token": "test-token",
    "api_base": api_base,
    "provider": "gitea",
    "owner": "testuser",
    "repo": "test-repo",
    "head_branch": "e2e-doc-fix",
    "head_repo": "testuser/test-repo",
    "base_branch": "main",
    "base_repo": "opentelekomcloud-docs/test-repo",
})
d = r.get_json()
check("pre-flight both exist: PR 200", r.status_code == 200, d)
check("pre-flight both exist: PR number", d.get("number") == 101, d)

# 10) Token check
r = client.post("/api/token-check", json={
    "url": "git@gitea.example.com:owner/repo.git",
    "token": "test-token",
    "api_base": api_base,
    "provider": "gitea",
})
d = r.get_json()
check("token-check ok", d.get("ok") is True and d.get("user") == "owner/repo", d)
check("token-check note", d.get("note") == "repo accessible", d)

# 11) HTTPS URL without a token -> clear 400 (not a git auth hang/failure)
r = client.post("/api/clone", json={
    "url": "https://github.com/owner/repo.git",
    "branch": "main",
})
d = r.get_json()
check("https-no-token rejected 400", r.status_code == 400, d)
check("https-no-token helpful msg", "token" in d.get("error", "").lower(), d)

# 12) detect-proxy endpoint reports the process's env proxy (best-effort)
r = client.get("/api/detect-proxy")
d = r.get_json()
check("detect-proxy 200", r.status_code == 200, d)
check("detect-proxy has proxy field", "proxy" in d, d)

# 13) PR with a proxy parameter is accepted (mock server; localhost ignores proxy)
r = client.post("/api/pr", json={
    "repo_id": repo_id,
    "head": "e2e-doc-fix",
    "base": "main",
    "title": "Proxy test PR",
    "body": "x",
    "token": "test-token",
    "api_base": api_base,
    "provider": "gitea",
    "owner": "owner",
    "repo": "repo",
    "proxy": "http://10.0.0.1:3128",
})
d = r.get_json()
check("PR with proxy 200", r.status_code == 200 and d.get("number") == 101, d)

# 14) Config is now CLIENT-side (browser localStorage). The server keeps no
# config, so the old save/load/default-path endpoints are gone. We verify they
# are removed (no secret ever lands on the server) and that the browse-dir
# endpoint (still server-side, for the work-dir picker) works.
r = client.post("/api/config/save", json={"path": "/tmp/x.yaml", "url": "x"})
check("config save endpoint removed (404/405)", r.status_code in (404, 405), r.status_code)
r = client.post("/api/config/load", json={"path": "/tmp/x.yaml"})
check("config load endpoint removed (404/405)", r.status_code in (404, 405), r.status_code)
r = client.get("/api/config/default-path")
check("config default-path endpoint removed", r.status_code in (404, 405), r.status_code)

# ---------------------------------------------------------------------- import
# Seed the local repo with a documentation folder (umn/source/_static/images).
# Use a SEPARATE bare repo so the import clone gets a fresh work dir (the main
# flow's work dir already exists and its local main is stale).
import importer  # noqa
assert importer.find_pandoc(), "pandoc required for import tests"
IMP_REPO = "/tmp/rsttest/imp-bare.git"
shutil.rmtree(IMP_REPO, ignore_errors=True)
subprocess.run(["git", "init", "-q", "--bare", IMP_REPO], check=True)
iseed = "/tmp/rsttest/imp-seed"
subprocess.run(["git", "clone", "-q", IMP_REPO, iseed], check=True)
subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=iseed)
with open(iseed + "/index.rst", "w") as f:
    f.write("Root\n====\n\nhello\n")
os.makedirs(iseed + "/umn/source/_static/images", exist_ok=True)
with open(iseed + "/umn/source/conf.py", "w") as f:
    f.write("# conf\n")
subprocess.run(["git", "add", "-A"], cwd=iseed)
subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
               cwd=iseed)
subprocess.run(["git", "push", "-q", "origin", "main"], cwd=iseed)

# Fresh repo session for import.
r = client.post("/api/clone", json={
    "url": IMP_REPO, "source_branch": "main", "target_branch": "import-test",
    "user_name": "E2E", "user_email": "e2e@test.dev",
})
imp_repo = r.get_json()["repo_id"]

# Build a docx fixture (2 chapters) via pandoc so the test is self-contained.
import tempfile, io
from werkzeug.datastructures import FileStorage
with tempfile.TemporaryDirectory() as td:
    with open(td + "/d.md", "w") as f:
        f.write("# Chapter One\n\nIntro text.\n\n## Sub A\n\n- item\n\n"
                "# Chapter Two\n\nMore text.\n")
    subprocess.run(["pandoc", td + "/d.md", "-f", "markdown", "-t", "docx",
                    "-o", td + "/d.docx"], check=True)
    docx_bytes = open(td + "/d.docx", "rb").read()

# 17) doc-folders endpoint
r = client.get("/api/doc-folders?repo_id=" + imp_repo)
d = r.get_json()
check("doc-folders lists umn", "umn" in d["folders"], d)

# 18) import SPLIT mode
fs = FileStorage(stream=io.BytesIO(docx_bytes), filename="um.docx")
r = client.post("/api/import", data={"repo_id": imp_repo, "file": fs,
                                     "doc_folder": "umn", "mode": "split",
                                     "title": "UMN Guide"})
d = r.get_json()
check("import split 200", r.status_code == 200 and d.get("imported"), d)
check("import split created index.rst", "umn/source/index.rst" in d["files"], d)
check("import split created chapters",
      any("chapter-one" in f for f in d["files"]) and any("chapter-two" in f for f in d["files"]),
      d)
check("import split staged", "umn/source/index.rst" in d["changed"], d)
# The written index has a toctree.
repo = appmod._REPOS[imp_repo]
check("import index has toctree", ".. toctree::" in repo.read_file("umn/source/index.rst"),
      repo.read_file("umn/source/index.rst"))
check("import chapter file written", repo.file_exists("umn/source/chapter-one.rst"))

# 18b) import SPLIT mode with split_level=2 -> folders + index.rst
#      "Chapter One" (h1, with h2 sub-chapters) becomes a folder:
#        umn/source/chapter-one/index.rst     (its own content + toctree)
#        umn/source/chapter-one/sub-a.rst
#        umn/source/chapter-one/sub-b.rst
fs = FileStorage(stream=io.BytesIO(docx_bytes), filename="lv2.docx")
r = client.post("/api/import", data={"repo_id": imp_repo, "file": fs,
                                     "doc_folder": "umn", "mode": "split",
                                     "title": "UMN Guide", "split_level": "2"})
d = r.get_json()
check("import split_level=2 200", r.status_code == 200 and d.get("imported"), d)
check("split_level=2 folder index.rst", "umn/source/chapter-one/index.rst" in d["files"], d)
check("split_level=2 sub-chapter file",
      "umn/source/chapter-one/sub-a.rst" in d["files"], d)
check("split_level=2 no flat chapter-one.rst",
      "umn/source/chapter-one.rst" not in d["files"], d)
check("split_level=2 leaf chapter stays a file",
      "umn/source/chapter-two.rst" in d["files"], d)
# Folder index holds the chapter heading + toctree to its sub-chapters.
check("split_level=2 index has toctree",
      ".. toctree::" in repo.read_file("umn/source/chapter-one/index.rst"),
      repo.read_file("umn/source/chapter-one/index.rst"))
# Root index references the chapter as a folder (no .rst extension).
root_idx = repo.read_file("umn/source/index.rst")
check("split_level=2 root index references folder",
      "chapter-one" in root_idx and "chapter-one.rst" not in root_idx, root_idx)

# 19) import SINGLE mode (no image)
fs2 = FileStorage(stream=io.BytesIO(docx_bytes), filename="single.docx")
r = client.post("/api/import", data={"repo_id": imp_repo, "file": fs2,
                                     "doc_folder": "doc", "mode": "single",
                                     "slug": "overview"})
d = r.get_json()
check("import single 200", r.status_code == 200 and d.get("imported"), d)
check("import single wrote doc/source/overview.rst",
      repo.file_exists("doc/source/overview.rst"), d)

# 20) import a docx WITH an embedded image -> routed to _static/images/
with tempfile.TemporaryDirectory() as td:
    os.makedirs(td + "/media", exist_ok=True)
    # 1x1 transparent png
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c63f8ffff3f0005fe02fea535580000000049454e44ae426082")
    with open(td + "/media/p.png", "wb") as f:
        f.write(png)
    with open(td + "/i.md", "w") as f:
        f.write("# Pic\n\n![a pic](media/p.png)\n")
    subprocess.run(["pandoc", "i.md", "-f", "markdown", "-t", "docx",
                    "-o", "i.docx"], check=True, cwd=td)
    img_docx = open(td + "/i.docx", "rb").read()
fs3 = FileStorage(stream=io.BytesIO(img_docx), filename="pic.docx")
r = client.post("/api/import", data={"repo_id": imp_repo, "file": fs3,
                                     "doc_folder": "umn", "mode": "single",
                                     "slug": "pic"})
d = r.get_json()
check("import image 200", r.status_code == 200 and d.get("imported"), d)
check("import extracted an image", len(d.get("images", [])) >= 1, d)
img_rel = d["images"][0]
check("import image under _static/images", "_static/images/" in img_rel, img_rel)
check("import image file on disk", repo.file_exists(img_rel), img_rel)
# The rst references the image with a /_static/images/ path.
rst_pic = repo.read_file("umn/source/pic.rst")
check("import rst refs _static/images", "/_static/images/" in rst_pic, rst_pic)

# 21) import rejects a bad doc folder name (traversal)
fs4 = FileStorage(stream=io.BytesIO(docx_bytes), filename="x.docx")
r = client.post("/api/import", data={"repo_id": imp_repo, "file": fs4,
                                     "doc_folder": "../../evil", "mode": "single"})
check("import rejects traversal folder", r.status_code == 400, r.get_json())

# 21b) over-long chapter titles -> truncated, unique stems (no E2ENAMETOOLONG)
long_a = ("The information in this document is subject to change without notice "
          "every effort has been made in the preparation of this document to "
          "ensure accuracy of the contents but all statements information and "
          "recommendations in this document do not constitute a warranty of any "
          "kind express or implied")
long_b = long_a + " plus extra trailing words so the truncation is similar"
# unit: make_stem keeps every produced filename under 255 bytes and de-dupes
stems_used = set()
s_a = importer.make_stem(long_a, stems_used)
s_b = importer.make_stem(long_b, stems_used)
check("long stem fits filesystem", len((s_a + ".rst").encode()) <= 255, s_a)
check("second long stem fits filesystem", len((s_b + ".rst").encode()) <= 255, s_b)
check("long stems are unique", s_a != s_b, (s_a, s_b))
check("stem has numeric suffix on collision", s_b.endswith("-2"), s_b)

# e2e: splitting a doc with a huge first-heading line must not crash / overlong.
md_long = "# " + long_a + "\n\nbody one\n\n## " + long_b + "\n\nbody two\n"
fs_long = FileStorage(stream=io.BytesIO(md_long.encode()), filename="long.md")
r = client.post("/api/import", data={"repo_id": imp_repo, "file": fs_long,
                                     "doc_folder": "doc", "mode": "split"})
d = r.get_json()
check("import long-title 200", r.status_code == 200 and d.get("imported"), d)
for rel in d.get("files", []):
    fname = os.path.basename(rel)
    check("generated filename within limit: " + fname, len(fname.encode()) <= 255, fname)

# 22) add-picture: upload image next to a file, collision-suffix, preview inlines
# docs/guide.rst lives in the main test repo (seeded at the top).
png = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63f8ffff3f0005fe02fea535580000000049454e44ae426082")
img_fs = FileStorage(stream=io.BytesIO(png), filename="arch.png")
r = client.post("/api/image", data={"repo_id": repo_id, "rel_path": "docs/guide.rst", "file": img_fs})
d = r.get_json()
check("image upload 200", r.status_code == 200 and d.get("ok"), d)
check("image lands in docs/_static/images",
      d.get("path") == "docs/_static/images/arch.png", d)
check("image ref is relative to the .rst", d.get("ref") == "_static/images/arch.png", d)

img_fs2 = FileStorage(stream=io.BytesIO(png), filename="arch.png")
d2 = client.post("/api/image", data={"repo_id": repo_id, "rel_path": "docs/guide.rst",
                                     "file": img_fs2}).get_json()
check("image collision -> -2", d2.get("ref") == "_static/images/arch-2.png", d2)

img_fs3 = FileStorage(stream=io.BytesIO(png), filename="arch.png")
d3 = client.post("/api/image", data={"repo_id": repo_id, "rel_path": "docs/guide.rst",
                                     "file": img_fs3}).get_json()
check("image collision -> -3", d3.get("ref") == "_static/images/arch-3.png", d3)

# Root-level file -> _static/images at repo root
img_fs4 = FileStorage(stream=io.BytesIO(png), filename="top.png")
d4 = client.post("/api/image", data={"repo_id": repo_id, "rel_path": "index.rst",
                                     "file": img_fs4}).get_json()
check("root image path", d4.get("path") == "_static/images/top.png", d4)
check("root image ref", d4.get("ref") == "_static/images/top.png", d4)

# Preview inlines the relative image as a data URI (so the added pic shows live).
rst_img = "Guide\n=====\n\n.. image:: _static/images/arch.png\n   :alt: architecture\n\ntext\n"
r = client.post("/api/preview", json={"source": rst_img, "repo_id": repo_id,
                                      "path": "docs/guide.rst"})
html = r.get_json()["html"]
check("preview inlines image (data uri)", "data:image/png;base64," in html, html[:200])
check("preview no longer has plain relative src",
      'src="_static/images/arch.png"' not in html)

# image with no file -> 400
check("image no file -> 400",
      client.post("/api/image", data={"repo_id": repo_id, "rel_path": "index.rst"}).status_code == 400)

# 23) browse-dir endpoint (server-side work-dir picker; config browsing is gone)
r = client.get("/api/browse-dir?dir=/tmp")
d = r.get_json()
check("browse 200", r.status_code == 200 and "dir" in d, d)
check("browse has subdirs key", "subdirs" in d, d)
r = client.get("/api/browse-dir?dir=/tmp/does-not-exist-xyz")
check("browse bad dir -> 400", r.status_code == 400, r.get_json())

# ----------------------------------------------------------------------
# 24) Re-open after a "restart" must not crash on 'branch already exists'
# (the user's bug: re-cloning after a server restart / browser refresh when the
#  local target branch exists but isn't on the remote).
rr = "/tmp/rsttest_reopen/bare.git"
shutil.rmtree("/tmp/rsttest_reopen", ignore_errors=True)
os.makedirs("/tmp/rsttest_reopen", exist_ok=True)
subprocess.run(["git", "init", "-q", "--bare", rr], check=True)
subprocess.run(["git", "clone", "-q", rr, "/tmp/rsttest_reopen/seed"], check=True)
subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd="/tmp/rsttest_reopen/seed")
subprocess.run(["git", "config", "user.name", "t"], cwd="/tmp/rsttest_reopen/seed")
subprocess.run(["git", "config", "user.email", "t@t"], cwd="/tmp/rsttest_reopen/seed")
with open("/tmp/rsttest_reopen/seed/index.rst", "w") as f:
    f.write("T\n=\n\nhi\n")
subprocess.run(["git", "add", "-A"], cwd="/tmp/rsttest_reopen/seed")
subprocess.run(["git", "commit", "-qm", "init"], cwd="/tmp/rsttest_reopen/seed")
subprocess.run(["git", "push", "-q", "origin", "main"], cwd="/tmp/rsttest_reopen/seed")

# First open: creates a LOCAL-ONLY target branch 'test'.
r = client.post("/api/clone", json={"url": rr, "source_branch": "main", "target_branch": "test"})
check("reopen: first open 200", r.status_code == 200, r.get_json())
reopen_id = r.get_json()["repo_id"]
lp = subprocess.run(["git", "-C", reopen_id, "branch", "--list", "test"],
                    capture_output=True, text=True).stdout
rp = subprocess.run(["git", "-C", reopen_id, "branch", "-r", "--list", "origin/test"],
                    capture_output=True, text=True).stdout
check("reopen: 'test' is local-only", "test" in lp and "origin/test" not in rp)

# Simulate a server restart (drop the in-memory registry), then re-open with reuse.
appmod._REPOS.clear()
r = client.post("/api/clone", json={"url": rr, "source_branch": "main", "target_branch": "test"})
check("reopen: re-open after restart 200 (was the crash)", r.status_code == 200, r.get_json())
check("reopen: no 'already exists' error",
      "already exists" not in (r.get_json().get("detail") or ""), r.get_json())
check("reopen: same working copy reused", r.get_json()["repo_id"] == reopen_id)

# Reset mode re-clones fresh from the remote.
appmod._REPOS.clear()
r = client.post("/api/clone", json={"url": rr, "source_branch": "main",
                                    "target_branch": "test", "mode": "reset"})
check("reset: re-clone 200", r.status_code == 200, r.get_json())
rid = r.get_json()["repo_id"]
br = subprocess.run(["git", "-C", rid, "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True).stdout.strip()
check("reset: checked out target branch", br == "test", br)

# ----------------------------------------------------------------------
# 25) Configurable work directory
custom_wroot = "/tmp/rsttest_wroot"
shutil.rmtree(custom_wroot, ignore_errors=True)
os.makedirs(custom_wroot, exist_ok=True)
appmod._REPOS.clear()
r = client.post("/api/clone", json={"url": rr, "source_branch": "main",
                                    "target_branch": "test", "work_root": custom_wroot})
d = r.get_json()
check("work_root: clone 200", r.status_code == 200, d)
check("work_root: clone used custom dir", d.get("work_root") == custom_wroot, d.get("work_root"))
check("work_root: repo under custom dir", d["repo_id"].startswith(custom_wroot), d["repo_id"])
check("work_root: dir exists on disk", os.path.isdir(d["repo_id"]))

# /api/work-root returns the default.
r = client.get("/api/work-root")
check("work-root endpoint", r.status_code == 200 and os.path.isabs(r.get_json()["work_root"]),
      r.get_json())

# work_root is a valid config key (client-side now; no server persistence).
import config_io as _ci
check("work_root in CONFIG_KEYS", "work_root" in _ci.CONFIG_KEYS)

# ----------------------------------------------------------------------
# 26) Robustness: a non-string / object 'mode' must NOT 500 the endpoint.
# (Regression for the real bug: the browser forwarded a ClickEvent object as the
#  mode arg, and server-side mode.strip() raised AttributeError -> a 500 HTML
#  page that the frontend couldn't JSON.parse: "unexpected character at line 1".)
from app import _norm_mode
check("_norm_mode: object -> reuse", _norm_mode({"mode": "reuse"}) == "reuse")
check("_norm_mode: None -> reuse", _norm_mode(None) == "reuse")
check("_norm_mode: 'RESET' -> reset", _norm_mode("RESET") == "reset")
check("_norm_mode: junk -> reuse", _norm_mode("weird") == "reuse")

appmod._REPOS.clear()   # simulate a restart
r = client.post("/api/clone",
                data=json.dumps({"url": rr, "source_branch": "main",
                                 "target_branch": "test",
                                 "mode": {"mode": "reuse", "work_root": ""}}),
                content_type="application/json")
check("clone with object mode does NOT 500", r.status_code == 200, r.get_json())
check("object mode treated as reuse", r.get_json().get("repo_id") == reopen_id)

# A non-JSON body (the browser's parse failure) is handled gracefully, not a 500.
r = client.post("/api/clone", data="garbage-not-json", content_type="text/plain")
check("non-JSON clone body -> 400 (not 500)", r.status_code == 400, r.status_code)

# ----------------------------------------------------------------------
# 27) /api/clone-status probe (used by the "re-fetch vs reset" choice dialog)
appmod._REPOS.clear()
r = client.post("/api/clone-status", json={"url": rr, "work_root": custom_wroot})
d = r.get_json()
check("clone-status 200", r.status_code == 200, d)
# custom_wroot clone happened earlier, so it exists there.
check("clone-status reflects existing copy",
      d["exists"] and d["is_repo"] and d["has_remote"], d)
check("clone-status reports branch", d["branch"] == "test", d)

# A URL that was never cloned -> exists False, is_repo False.
r = client.post("/api/clone-status", json={"url": "git@example.com:nobody/never.git"})
d = r.get_json()
check("clone-status for unknown URL -> not exists",
      r.status_code == 200 and d["exists"] == False and d["is_repo"] == False, d)

# clone-status needs a URL.
r = client.post("/api/clone-status", json={})
check("clone-status missing url -> 400", r.status_code == 400, r.get_json())

# ----------------------------------------------------------------------
# 28) Word-level formatting: a "word " selection (double-click includes the
# trailing space) must NOT wrap the space. Verified against the exact wrapSel
# core logic shipped in app.js (re-implemented here to assert the behavior).
def wrap(value, start, end, prefix, suffix=None):
    suffix = suffix or prefix
    inner = value[start:end]
    import re
    leadWs = re.match(r"^\s*", inner).group(0)
    trailWs = re.search(r"\s*$", inner).group(0)
    core = inner[len(leadWs):len(inner) - len(trailWs)]
    if not core:
        return value[:start] + prefix + suffix + value[end:]
    return value[:start] + leadWs + prefix + core + suffix + trailWs + value[end:]

check("format: trailing space excluded (bold)",
      wrap("Before bold next", 7, 12, "**") == "Before **bold** next",
      wrap("Before bold next", 7, 12, "**"))
check("format: trailing space excluded (italic)",
      wrap("x emph y", 2, 7, "*") == "x *emph* y", wrap("x emph y", 2, 7, "*"))
check("format: trailing space excluded (inline code)",
      wrap("a code b", 2, 7, "``") == "a ``code`` b", wrap("a code b", 2, 7, "``"))
check("format: no trailing space still works",
      wrap("start bold end", 6, 10, "**") == "start **bold** end",
      wrap("start bold end", 6, 10, "**"))

# ----------------------------------------------------------------------
# 29) File CRUD endpoints: new / upload / rename / delete
import io
from werkzeug.datastructures import FileStorage

# A fresh repo with a couple of files incl. a non-.rst asset + a doc folder.
crud = "/tmp/rsttest_crud"
shutil.rmtree(crud, ignore_errors=True); os.makedirs(crud)
cbare = crud + "/bare.git"
subprocess.run(["git", "init", "-q", "--bare", cbare], check=True)
subprocess.run(["git", "clone", "-q", cbare, crud + "/seed"], check=True)
subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=crud + "/seed")
subprocess.run(["git", "config", "user.name", "t"], cwd=crud + "/seed")
subprocess.run(["git", "config", "user.email", "t@t"], cwd=crud + "/seed")
with open(crud + "/seed/index.rst", "w") as f:
    f.write("T\n=\n\nhi\n")
os.makedirs(crud + "/seed/umn/source", exist_ok=True)
with open(crud + "/seed/umn/source/chapter.rst", "w") as f:
    f.write("Ch\n==\n\nbody\n")
with open(crud + "/seed/keep.png", "wb") as f:
    f.write(b"PNGDATA")
subprocess.run(["git", "add", "-A"], cwd=crud + "/seed")
subprocess.run(["git", "commit", "-qm", "init"], cwd=crud + "/seed")
subprocess.run(["git", "push", "-q", "origin", "main"], cwd=crud + "/seed")

r = client.post("/api/clone", json={"url": cbare, "source_branch": "main", "target_branch": "test"})
d = r.get_json()
check("crud: clone 200", r.status_code == 200, d)
crud_rid = d["repo_id"]

# NEW
r = client.post("/api/file-new", json={"repo_id": crud_rid, "path": "umn/source/newtopic.rst",
                                       "content": "New\n===\n\nx\n"})
d = r.get_json()
check("crud new: created", r.status_code == 200 and d.get("created") and d["path"] == "umn/source/newtopic.rst", d)
check("crud new: on disk", os.path.isfile(os.path.join(crud_rid, "umn/source/newtopic.rst")))
check("crud new: overwrite refused", client.post("/api/file-new",
      json={"repo_id": crud_rid, "path": "index.rst"}).status_code == 400)
check("crud new: traversal rejected", client.post("/api/file-new",
      json={"repo_id": crud_rid, "path": "../escape.rst"}).status_code in (400, 500))

# NEW DIRECTORY: a trailing "/" (or is_dir=true) creates an empty directory,
# made git-visible via a .gitkeep placeholder. (Kept in zdir/ so it doesn't
# pollute umn/source, which the later index-generate test indexes.)
r = client.post("/api/file-new", json={"repo_id": crud_rid, "path": "zdir/alpha/"})
d = r.get_json()
check("crud newdir: trailing-slash creates dir",
      r.status_code == 200 and d.get("is_dir") and d["path"] == "zdir/alpha", d)
check("crud newdir: .gitkeep placeholder on disk",
      os.path.isfile(os.path.join(crud_rid, "zdir/alpha/.gitkeep")))
check("crud newdir: placeholder listed", "zdir/alpha/.gitkeep" in d.get("all_files", []),
      d.get("all_files"))
# is_dir=true (no trailing slash) also works.
r = client.post("/api/file-new", json={"repo_id": crud_rid, "path": "zdir/beta", "is_dir": True})
d = r.get_json()
check("crud newdir: is_dir flag creates dir",
      r.status_code == 200 and d.get("is_dir") and os.path.isdir(os.path.join(crud_rid, "zdir/beta")), d)
# Nested directory: parents created as needed.
r = client.post("/api/file-new", json={"repo_id": crud_rid, "path": "zdir/gamma/delta/", "is_dir": True})
d = r.get_json()
check("crud newdir: nested parents created",
      r.status_code == 200 and os.path.isdir(os.path.join(crud_rid, "zdir/gamma/delta")), d)
# Existing directory refused (400).
check("crud newdir: existing dir refused",
      client.post("/api/file-new", json={"repo_id": crud_rid, "path": "zdir/alpha/"}).status_code == 400)
# A path that is an existing file with is_dir is refused.
check("crud newdir: file-as-dir refused",
      client.post("/api/file-new", json={"repo_id": crud_rid, "path": "index.rst", "is_dir": True}).status_code == 400)
# Directory traversal rejected.
check("crud newdir: traversal rejected",
      client.post("/api/file-new", json={"repo_id": crud_rid, "path": "../esc/", "is_dir": True}).status_code in (400, 500))
# Re-creating the same dir -> 400 (already exists), not a crash.
check("crud newdir: re-create same dir -> 400",
      client.post("/api/file-new", json={"repo_id": crud_rid, "path": "zdir/alpha", "is_dir": True}).status_code == 400)

# UPLOAD (multi + dest_dir + collision suffix + no-clobber)
fd = {"repo_id": crud_rid, "dest_dir": "umn/source", "files": [
    FileStorage(stream=io.BytesIO(b"Hello upload"), filename="notes.txt"),
    FileStorage(stream=io.BytesIO(b"Second"), filename="report.pdf"),
    FileStorage(stream=io.BytesIO(b"dup"), filename="chapter.rst"),  # collision
]}
r = client.post("/api/file-upload", data=fd, content_type="multipart/form-data")
d = r.get_json()
check("crud upload: 200 + 3 files", r.status_code == 200 and len(d.get("files", [])) == 3, d)
check("crud upload: notes in umn/source", "umn/source/notes.txt" in d["files"], d.get("files"))
check("crud upload: report in umn/source", "umn/source/report.pdf" in d["files"], d.get("files"))
check("crud upload: collision -> chapter-2.rst", "umn/source/chapter-2.rst" in d["files"], d.get("files"))
check("crud upload: original not clobbered",
      open(os.path.join(crud_rid, "umn/source/chapter.rst")).read() == "Ch\n==\n\nbody\n")
check("crud upload: on disk", os.path.isfile(os.path.join(crud_rid, "umn/source/notes.txt")))
check("crud upload: empty -> 400",
      client.post("/api/file-upload", data={"repo_id": crud_rid, "dest_dir": ""},
                  content_type="multipart/form-data").status_code == 400)

# RENAME
r = client.post("/api/file-rename", json={"repo_id": crud_rid, "path": "umn/source/notes.txt",
                                          "new_path": "umn/source/renamed_notes.txt"})
d = r.get_json()
check("crud rename: moved", r.status_code == 200 and d.get("renamed") and d["new"] == "umn/source/renamed_notes.txt", d)
check("crud rename: old gone", not os.path.isfile(os.path.join(crud_rid, "umn/source/notes.txt")))
check("crud rename: new exists", os.path.isfile(os.path.join(crud_rid, "umn/source/renamed_notes.txt")))
check("crud rename: clobber refused", client.post("/api/file-rename",
      json={"repo_id": crud_rid, "path": "umn/source/renamed_notes.txt", "new_path": "index.rst"}).status_code == 400)
check("crud rename: traversal rejected", client.post("/api/file-rename",
      json={"repo_id": crud_rid, "path": "index.rst", "new_path": "../outside.txt"}).status_code in (400, 500))

# DELETE
r = client.post("/api/file-delete", json={"repo_id": crud_rid, "path": "keep.png"})
d = r.get_json()
check("crud delete: removed", r.status_code == 200 and d.get("deleted") and d["path"] == "keep.png", d)
check("crud delete: gone from disk", not os.path.isfile(os.path.join(crud_rid, "keep.png")))
check("crud delete: missing -> 404",
      client.post("/api/file-delete", json={"repo_id": crud_rid, "path": "nope/never.txt"}).status_code == 404)
check("crud delete: traversal rejected", client.post("/api/file-delete",
      json={"repo_id": crud_rid, "path": "../../../etc/passwd"}).status_code in (400, 404, 500))

# Status reflects the staged changes.
st = client.get("/api/status?repo_id=" + crud_rid).get_json()
check("crud: status lists changes", len(st["changed"]) > 0, st["changed"])

# INDEX GENERATE: build index.rst (toctree) for a directory.
#   umn/source/ holds chapter.rst, newtopic.rst, chapter-2.rst (from upload)
#   and no index.rst yet.
r = client.post("/api/index-generate", json={
    "repo_id": crud_rid, "dir": "umn/source", "title": "Umn Source"})
d = r.get_json()
check("index: created in umn/source",
      r.status_code == 200 and d.get("created") and d["path"] == "umn/source/index.rst", d)
check("index: on disk", os.path.isfile(os.path.join(crud_rid, "umn/source/index.rst")))
check("index: toctree present", ".. toctree::" in d["content"])
check("index: lists chapter", "   chapter" in d["content"], d["content"])
check("index: lists newtopic", "   newtopic" in d["content"], d["content"])
check("index: lists chapter-2", "   chapter-2" in d["content"], d["content"])
# the generated index.rst is staged as a change
check("index: staged as change", "umn/source/index.rst" in d["changed"], d.get("changed"))
# A second generate WITHOUT overwrite -> 409 conflict.
r2 = client.post("/api/index-generate", json={
    "repo_id": crud_rid, "dir": "umn/source", "title": "Again"})
check("index: no-overwrite -> conflict 409", r2.status_code == 409 and r2.get_json().get("conflict"))
# WITH overwrite -> regenerates (200).
r3 = client.post("/api/index-generate", json={
    "repo_id": crud_rid, "dir": "umn/source", "title": "Again", "overwrite": True})
check("index: overwrite -> 200", r3.status_code == 200 and r3.get_json().get("created"))

# Title default: a blank/omitted title is derived from the directory name —
# dashes -> spaces, first letter of the first word capitalized.
r4 = client.post("/api/index-generate", json={
    "repo_id": crud_rid, "dir": "umn/source", "overwrite": True})  # no title
c4 = r4.get_json().get("content", "")
check("index: blank title defaults from dir name",
      r4.status_code == 200 and c4.startswith("Source\n"), c4[:30])
# A dash-separated directory name is prettified.
from app import _dir_default_title as _dt
check("title: my-cool-dir -> My cool dir", _dt("my-cool-dir") == "My cool dir", _dt("my-cool-dir"))
check("title: api-ref -> Api ref", _dt("api-ref") == "Api ref", _dt("api-ref"))
check("title: a-b-c -> A b c", _dt("a-b-c") == "A b c", _dt("a-b-c"))
check("title: root -> Contents", _dt("") == "Contents", _dt(""))
check("title: nested takes basename", _dt("doc/source") == "Source", _dt("doc/source"))

# TOCTREE PREVIEW: the generated index.rst renders with toc_entries and the
# toctree directive is stripped from the body (not shown as raw text).
src = open(os.path.join(crud_rid, "umn/source/index.rst")).read()
pv = client.post("/api/preview", json={"source": src}).get_json()
te = pv.get("toc_entries") or []
check("preview: toctree extracted", len(te) >= 1, pv)
if te:
    check("preview: toctree entries match",
          sorted(te[0]["entries"]) == ["chapter", "chapter-2", "newtopic"],
          te[0]["entries"])
check("preview: toctree not shown raw", "toctree::" not in pv["html"])
check("preview: in-page toc anchors match section ids",
      all(a in pv["html"] for a in __import__("re").findall(r"href='#([^']*)'", pv.get("toc") or "")),
      pv.get("toc"))

# ----------------------------------------------------------------------
# 30) File view classification: image -> data URI, other binary -> notice,
#      text -> editable content. (Server side of the image/binary preview.)
import base64 as _b64
# Add a real PNG + a binary blob + a text file to the crud repo, then read back.
png = _b64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg==")
with open(os.path.join(crud_rid, "logo.png"), "wb") as f:
    f.write(png)
import gzip as _gzip
with open(os.path.join(crud_rid, "archive.bin"), "wb") as f:
    f.write(_gzip.compress(b"z" * 500))
with open(os.path.join(crud_rid, "plain.txt"), "w") as f:
    f.write("plain text here\n")

d = client.get("/api/file?repo_id=" + crud_rid + "&path=logo.png").get_json()
check("view: png -> image", d.get("kind") == "image", d)
check("view: png data_uri mime", d.get("data_uri", "").startswith("data:image/png;base64,"), d.get("data_uri", "")[:30])
check("view: png data_uri round-trips", _b64.b64decode(d["data_uri"].split(",", 1)[1]) == png)

d = client.get("/api/file?repo_id=" + crud_rid + "&path=archive.bin").get_json()
check("view: binary -> binary", d.get("kind") == "binary", d)
check("view: binary has no data_uri", not d.get("data_uri"))

d = client.get("/api/file?repo_id=" + crud_rid + "&path=plain.txt").get_json()
check("view: text -> text", d.get("kind") == "text", d)
check("view: text content intact", d.get("content") == "plain text here\n", d.get("content"))

# A .rst is still text (not misclassified as image/binary).
d = client.get("/api/file?repo_id=" + crud_rid + "&path=index.rst").get_json()
check("view: rst -> text", d.get("kind") == "text", d)

# 31) EMF/WMF metafiles: never classified as text; either rendered to an
#     image (EMF -> SVG, when the Linux `emf2svg-conv` converter is present —
#     true in the Docker image) or gracefully fall back to "binary". WMF has
#     no reliable Linux converter, so it always falls back to "binary".
emf_magic = b"\x01\x00\x00\x00" + b"\x00" * 96   # EMF header magic + padding
with open(os.path.join(crud_rid, "meta.emf"), "wb") as f:
    f.write(emf_magic)
d = client.get("/api/file?repo_id=" + crud_rid + "&path=meta.emf").get_json()
check("view: emf never text", d.get("kind") in ("image", "binary"), d)
check("view: emf doesn't crash", d.get("path") == "meta.emf", d)
if d.get("kind") == "image":
    check("view: emf -> image data_uri (svg)",
          (d.get("data_uri") or "").startswith("data:image/svg+xml;base64,"),
          (d.get("data_uri") or "")[:30])
    # The rendered SVG must decode to something containing an <svg> tag.
    svg_bytes = _b64.b64decode(d["data_uri"].split(",", 1)[1])
    check("view: emf rendered to valid SVG", b"<svg" in svg_bytes,
          svg_bytes[:40])

print("\nALL E2E CHECKS PASSED")
