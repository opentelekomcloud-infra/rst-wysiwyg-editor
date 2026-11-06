# rst-wysiwyg-editor

A web-based WYSIWYG editor for **reStructuredText** documents that lives inside a
git repository. Clone a branch (or a protected-branch workflow: work on a target
branch, never the protected default), edit RST with a live preview, manage files
(create/rename/delete/upload, generate `index.rst` toctrees), import
docx/html/markdown via pandoc, and open Pull/Merge Requests against GitHub or
Gitea directly from the UI.

## Layout

```
src/            application code (Flask app + vanilla-JS frontend)
  app.py            HTTP layer (Flask routes)
  git_ops.py        git subprocess wrapper (SSH key, clone, commit, push)
  pr_provider.py    GitHub/Gitea REST client
  rst_render.py     RST -> HTML (docutils), TOC, toctree extraction
  importer.py       docx/html/md -> RST (pandoc) + multi-level heading split
  config_io.py      YAML config helpers (client-side export/import)
  mock_api.py       fake GitHub/Gitea API used by the e2e tests
  static/           frontend (index.html, css, js)
  requirements.txt  Python dependencies
test/           test suites (e2e + frontend JS harnesses)
doc/            project documentation (README, SUMMARY)
Dockerfile      hardened-Debian-13 container image
run.sh          container entrypoint
```

## Requirements

- Python 3.13 (the app is tested on 3.13; Flask >= 3 is required on 3.13)
- git
- pandoc (doc import)
- node (frontend JS tests only)

## Run locally

```sh
python3 -m venv .venv
.venv/bin/pip install -r src/requirements.txt
RST_WORK_ROOT=/tmp/rstwork .venv/bin/python src/app.py
# -> http://127.0.0.1:8000/
```

Environment variables: `RST_EDITOR_PORT` (default 8000), `RST_EDITOR_HOST`
(default 127.0.0.1), `RST_WORK_ROOT` (clone cache, default `src/work`),
`RST_WORK_DIR_TTL` (seconds a work dir may sit idle before the background
reaper deletes it, default 7200 = 2h), `RST_WORK_DIR_REAP_INTERVAL` (how often
the reaper scans, default 3600 = 1h).

## Run tests

```sh
.venv/bin/python test/test_e2e.py   # end-to-end (191 checks; needs git + pandoc)
node test/test_table.js             # grid-table engine (62 checks)
node test/test_tree.js              # file-tree / toctree resolver (24 checks)
```

## Run in Docker

Build. The base image is a hardened Debian 13 "dev" variant; its registry
address is **not** stored in this repo — you supply it as the
`ARTIFACTORY_URL` build arg (env var or `--build-arg`). The base is only used
in the *build* stage; the final container runs as a **non-root** user:

```sh
ARTIFACTORY_URL=<your-registry> docker build -t rst-wysiwyg-editor .
# (equivalently: docker build --build-arg ARTIFACTORY_URL=<your-registry> -t rst-wysiwyg-editor .)
docker run --rm -p 8000:8000 -v rst-work:/work rst-wysiwyg-editor
# -> http://127.0.0.1:8000/
```

Need a non-corporate base? Pass `--build-arg BASE=<image>` (e.g. the public
`python:3.13-slim`); `BASE` takes precedence over `ARTIFACTORY_URL`.

### Continuous delivery (GitHub Actions → SWR)

`.github/workflows/` contains the delivery + quality-gate + automerge
workflows (mirroring the circle-partner-navigator-frontend setup):

- **`docker-build.yml`** — on pull requests: builds the image (no push) as a
  PR check; when the PR is merged, builds **and pushes** to SWR.
- **`docker-build-push-on-tag-or-release.yaml`** — on tags/releases: builds
  and pushes to SWR.
- **`python-quality.yml`** — on PRs / pushes to the default branch:
  `python -m compileall` (syntax), `ruff check --select F,E9` (real defects:
  unused imports, undefined names, redefinitions, runtime errors), and the full
  hermetic e2e suite (`test/test_e2e.py` + the JS suites).
- **`js-quality.yml`** — `node --check` on every JS file plus the three
  pure-logic JS test suites (`test_url_parse.js`, `test_table.js`,
  `test_tree.js`).
- **`yaml-quality.yml`** — parses every YAML file in the repo (all workflows,
  config fixtures) with `yaml.safe_load_all`.
- **`automerge.yaml`** — when a PR is labeled `gate`, automatically merges it
  (squash) as soon as **all** checks pass, all required reviews are approved,
  and branch-protection rules are satisfied.

Images are tagged by `docker/metadata-action` (branch / PR / semver / sha) and
pushed as `<SWR_URL>/t-cloud-public/rst-wysiwyg-editor:<tag>`.

Required **repository secrets** (Settings → Secrets and variables → Actions):

| Secret                     | Value                                    |
|----------------------------|------------------------------------------|
| `SWR_URL`                  | SWR registry domain (host part only, e.g. `swr.<region>.<your-domain>`) |
| `SWR_USERNAME`             | SWR username (from `~/.swr` → `swr_username`) |
| `SWR_PASSWORD`             | SWR login key (from `~/.swr` → `login_key`) |
| `ARTIFACTORY_URL`          | DHI Artifactory registry domain (host part, no image path) |
| `ARTIFACTORY_AUTH_USERNAME`| Artifactory username                     |
| `ARTIFACTORY_AUTH_PASSWORD`| Artifactory password                     |
| `APP_ID`                   | GitHub App ID (for the `automerge.yaml` workflow) |
| `APP_KEY`                  | GitHub App private key (PEM, for the `automerge.yaml` workflow) |

> Never commit registry credentials; the workflows reference them only via
> GitHub secrets. The Dockerfile itself contains no registry address (the base
> image is chosen via `--build-arg ARTIFACTORY_URL`).
>
> **Automerge** uses a GitHub App (`APP_ID` + `APP_KEY`), not a PAT — the
> `create-github-app-token` action mints a short-lived token that can bypass
> branch-protection and satisfy required reviews, which a fine-grained PAT
> cannot do. Create the App (Settings → Developer settings → GitHub Apps) with
> `Contents: Read` + `Pull requests: Read/Write` and install it on this repo.
> Then label a ready PR `gate` and it merges automatically once every check is
> green and all required reviews are approved.

Repo clones are cached in `/work`; attach a named volume (or `-v`) to persist them
across container runs. No credentials are baked into the image — SSH keys and
API tokens are entered in the UI at runtime (or saved to your local config file).

## Security notes

- No credentials in this repository. SSH keys, API tokens, and the YAML config
  are user-supplied at runtime; the app writes them 0600 and never echoes them back.
- The container runs as a dedicated non-root user and binds 0.0.0.0 only inside
  the container (exposed port 8000).
- `requirements.txt` pins Flask >= 3: Flask 2.x is broken on Python 3.13 (its
  test client reads `werkzeug.__version__`, removed in Werkzeug 3.1).
