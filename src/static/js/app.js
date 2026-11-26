/* RST WYSIWYG Editor frontend */
(function () {
  "use strict";

  // ------------------------------------------------------------- state
  const state = {
    repoId: null,
    currentFile: null,
    currentKind: "text",   // "text" | "image" | "binary" (last opened file)
    dirty: false,
    autosave: false,
    view: "split",
    branches: [],
    currentBranch: "main",   // target/head branch we commit+push to
    sourceBranch: "main",    // base branch we branched from (usually main)
    files: [],
    allFiles: [],
    changed: [],
  };

  // ------------------------------------------------------------- helpers
  const $ = (id) => document.getElementById(id);
  function fetchJSON(url, opts) {
    return fetch(url, Object.assign({ headers: { "Content-Type": "application/json" } }, opts))
      .then((r) => r.json().then((d) => ({ ok: r.ok, d })));
  }
  function toast(status, text, kind) {
    status.textContent = text;
    status.className = "status " + (kind || "");
  }

  // ------------------------------------------------------------- clone
  // "Clone / Open" first probes for an existing working copy; if one exists it
  // asks the user to re-fetch (keep local) or reset & re-clone (discard local).
  // A click event object must NOT be forwarded as a mode arg (it isn't a string
  // and would crash the server).
  $("btn-clone").addEventListener("click", startClone);
  $("repo-url").addEventListener("keydown", (e) => { if (e.key === "Enter") startClone(); });

  // Entry point for "Clone / Open": if a working copy for this URL already
  // exists locally, ask what to do; otherwise just clone.
  async function startClone() {
    const url = $("repo-url").value.trim();
    if (!url) return;
    let st = null;
    try {
      const { ok, d } = await fetchJSON("/api/clone-status", {
        method: "POST",
        body: JSON.stringify({ url, work_root: $("work-root").value.trim() }),
      });
      if (ok) st = d;
    } catch (e) {
      // Probe failed — fall through to a normal clone, which will surface any
      // real error (network, auth, etc.).
      st = null;
    }
    if (st && st.exists && st.is_repo) {
      showCloneChoice(st);
      return;
    }
    doClone("reuse");
  }

  // Show the "existing working copy" choice dialog (re-fetch vs reset).
  function showCloneChoice(st) {
    const info = $("clone-choice-info");
    const lines = ["Local working copy: " + (st.work_dir || "(unknown)")];
    if (st.branch) lines.push("Branch: " + st.branch + (st.head_sha ? "  (at " + st.head_sha + ")" : ""));
    if (st.dirty) lines.push("Uncommitted changes present.");
    info.textContent = lines.join("\n");
    info.style.whiteSpace = "pre-line";
    $("clone-choice-status").textContent = "";
    $("clone-choice-dialog").classList.remove("hidden");
  }
  // "Re-fetch existing": keep the local copy, fetch latest, switch to the branch.
  $("clone-choice-refetch").addEventListener("click", () => {
    $("clone-choice-dialog").classList.add("hidden");
    doClone("reuse");
  });
  // "Reset & re-clone": discard the local copy and clone fresh from the remote.
  $("clone-choice-reset").addEventListener("click", () => {
    $("clone-choice-dialog").classList.add("hidden");
    doClone("reset");
  });
  $("clone-choice-cancel").addEventListener("click", () => {
    $("clone-choice-dialog").classList.add("hidden");
  });

  async function doClone(mode) {
    const url = $("repo-url").value.trim();
    if (!url) return;
    mode = mode || "reuse";
    // Source = base branch to branch from. Target = where edits land; auto-create
    // a sensible name (docs/<short-hash>) if the user left it blank, so commits
    // never go straight to a protected branch.
    let sourceBranch = $("source-branch").value.trim() || "main";
    let targetBranch = $("target-branch").value.trim();
    if (!targetBranch) {
      targetBranch = suggestTargetBranch(sourceBranch);
      $("target-branch").value = targetBranch;
    }
    const btn = mode === "reset" ? $("btn-reset-clone") : $("btn-clone");
    const wasPrimary = btn.classList.contains("primary");
    btn.textContent = mode === "reset" ? "Resetting…" : "Cloning…"; btn.disabled = true;
    try {
      const { ok, d } = await fetchJSON("/api/clone", {
        method: "POST",
        body: JSON.stringify({
          url,
          source_branch: sourceBranch,
          target_branch: targetBranch,
          mode,
          work_root: $("work-root").value.trim(),
          ssh_key: $("ssh-key").value,
          host: $("ssh-host").value,
          port: parseInt($("ssh-port").value || "22", 10),
          user_name: $("commit-name").value,
          user_email: $("commit-email").value,
          token: $("api-token").value,
        }),
      });
      if (!ok) { alert((mode === "reset" ? "Reset & re-clone failed: " : "Clone failed: ") + (d.detail || d.error)); return; }
      state.repoId = d.repo_id;
      state.repoUrl = url;
      state.currentBranch = d.target_branch || d.branch;
      state.sourceBranch = d.source_branch || sourceBranch;
      state.branches = [{ name: state.currentBranch, current: true }];
      state.files = d.files;
      state.allFiles = d.all_files;
      state.changed = [];
      // New repo: reset the file-tree expand state so top-level folders
      // auto-open again and the previous repo's collapsed dirs don't carry over.
      state.openDirs = new Set();
      state.treeInit = false;
      showMain();
      renderFileTree();
      refreshStatus();
    } catch (e) {
      alert((mode === "reset" ? "Reset & re-clone failed: " : "Clone failed: ") + e.message);
    } finally {
      btn.textContent = mode === "reset" ? "Reset & re-clone" : "Clone / Open"; btn.disabled = false;
    }
  }

  // "Reset & re-clone": discard the local working copy and re-clone fresh from
  // the remote. This is the escape hatch after a server restart / browser refresh
  // when the local content is stale, or when re-opening fails.
  $("btn-reset-clone").addEventListener("click", () => {
    if (!state.repoId && !$("repo-url").value.trim()) {
      alert("Enter a repository URL first.");
      return;
    }
    if (!confirm(
      "Reset & re-clone?\n\nThis deletes the local working copy for this repo and " +
      "clones fresh from the remote. Any UNPUSHED local commits and uncommitted " +
      "edits in the working copy will be lost (pushed work is safe)."
    )) return;
    doClone("reset");
  });

  // Suggest a branch name for a new PR. If the source is the default (main/master),
  // prefix with "docs/"; otherwise keep it short.
  function suggestTargetBranch(sourceBranch) {
    const ts = new Date().toISOString().slice(0, 10); // YYYY-MM-DD
    const isDefault = (sourceBranch === "main" || sourceBranch === "master");
    return isDefault ? "docs/update-" + ts : sourceBranch + "-update-" + ts;
  }

  function showMain() {
    $("main").classList.remove("hidden");
    $("actionbar").classList.remove("hidden");
    $("branch-label").textContent = state.currentBranch + "  ←  " + state.sourceBranch;
    $("branch-label").title = "Commits go to target branch '" + state.currentBranch + "' (branched from '" + state.sourceBranch + "'), not to the protected default.";
  }

  // ------------------------------------------------------------- settings
  function toggleSettings() {
    $("settings-drawer").classList.toggle("hidden");
  }
  $("btn-settings").addEventListener("click", toggleSettings);
  $("btn-settings-top").addEventListener("click", toggleSettings);
  $("btn-save-settings").addEventListener("click", () => {
    // settings are read at clone time; for token, store in memory
    window.__settings = {
      token: $("api-token").value,
      provider: $("provider").value,
      api_base: $("api-base").value,
      proxy: $("proxy").value,
    };
    $("settings-drawer").classList.add("hidden");
    toast($("token-status"), "Settings saved.", "ok");
  });
  // "auto" proxy: pull the proxy from the app process's environment.
  $("btn-detect-proxy").addEventListener("click", async () => {
    const { d } = await fetchJSON("/api/detect-proxy");
    if (d.proxy) {
      $("proxy").value = d.proxy;
      toast($("token-status"), "Proxy auto-filled from env (" + d.source + ")", "ok");
    } else {
      toast($("token-status"), "No proxy found in the app's environment.", "err");
    }
  });
  $("btn-check-token").addEventListener("click", async () => {
    const url = $("repo-url").value.trim();
    if (!url) return;
    const { d } = await fetchJSON("/api/token-check", {
      method: "POST",
      body: JSON.stringify({
        url, token: $("api-token").value, provider: $("provider").value, api_base: $("api-base").value, proxy: $("proxy").value,
      }),
    });
    if (d.ok) toast($("token-status"), "OK — " + (d.user || "token valid") + (d.note ? " (" + d.note + ")" : ""), "ok");
    else toast($("token-status"), "Failed: " + (d.error || "bad token"), "err");
  });

  // ------------------------------------------------------------- config file
  // Gather the full configuration from the form (incl. the top-bar fields).
  function gatherConfig() {
    return {
      url: $("repo-url").value,
      source_branch: $("source-branch").value,
      target_branch: $("target-branch").value,
      upstream: $("upstream").value,
      ssh_key: $("ssh-key").value,
      ssh_host: $("ssh-host").value,
      ssh_port: $("ssh-port").value,
      commit_name: $("commit-name").value,
      commit_email: $("commit-email").value,
      api_token: $("api-token").value,
      provider: $("provider").value,
      api_base: $("api-base").value,
      proxy: $("proxy").value,
      work_root: $("work-root").value,
    };
  }

  function applyConfig(cfg, keepExisting) {
    if (!cfg) return;
    // If keepExisting, only fill fields that are still blank (used on startup
    // so a saved config pre-fills the form without overwriting anything typed).
    const set = (id, v) => {
      if (v === undefined || v === null) return;
      if (keepExisting && $(id).value.trim() !== "") return;
      $(id).value = v;
    };
    set("repo-url", cfg.url);
    set("source-branch", cfg.source_branch);
    set("target-branch", cfg.target_branch);
    set("upstream", cfg.upstream);
    set("ssh-key", cfg.ssh_key);
    set("ssh-host", cfg.ssh_host);
    set("ssh-port", cfg.ssh_port || "22");
    set("commit-name", cfg.commit_name);
    set("commit-email", cfg.commit_email);
    set("api-token", cfg.api_token);
    set("provider", cfg.provider);
    set("api-base", cfg.api_base);
    set("proxy", cfg.proxy);
    // work_root always applies when present (overrides the default shown on load).
    if (cfg.work_root) $("work-root").value = cfg.work_root;
  }

  // The configuration lives in the BROWSER (localStorage), not on the server:
  // the server keeps no copy, so secrets never persist on the machine hosting
  // the app. Export/Import (config.yaml) moves the config between machines.
  const CONFIG_LS_KEY = "rst_config";

  function loadStoredConfig() {
    try {
      const raw = localStorage.getItem(CONFIG_LS_KEY);
      if (raw) applyConfig(JSON.parse(raw));
    } catch (e) { /* corrupt stored config — ignore */ }
  }

  $("btn-save-config").addEventListener("click", () => {
    try {
      localStorage.setItem(CONFIG_LS_KEY, JSON.stringify(gatherConfig()));
      toast($("config-status"), "Saved to this browser.", "ok");
    } catch (e) {
      toast($("config-status"), "Save failed: " + e, "err");
    }
  });

  $("btn-export-config").addEventListener("click", () => {
    try {
      const text = cym.toYaml(gatherConfig());
      const blob = new Blob([text], { type: "text/yaml" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "rst-editor-config.yaml";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
      toast($("config-status"), "Exported config.yaml — keep it secret (it holds your key and token).", "ok");
    } catch (e) {
      toast($("config-status"), "Export failed: " + e, "err");
    }
  });

  $("btn-import-config").addEventListener("click", () => {
    $("config-import-file").click();
  });
  $("config-import-file").addEventListener("change", (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const cfg = cym.parseYaml(reader.result);
        applyConfig(cfg);
        // Persist the imported config so it's ready next time.
        localStorage.setItem(CONFIG_LS_KEY, JSON.stringify(gatherConfig()));
        toast($("config-status"), "Imported " + f.name + " and saved to this browser.", "ok");
      } catch (err) {
        toast($("config-status"), "Import failed: " + err, "err");
      }
      e.target.value = "";
    };
    reader.readAsText(f);
  });

  // ------------------------------------------------------------- file tree
  // A "file list" (all repo files) drives the tree so create / upload / delete
  // / rename all have real targets. Falls back to the .rst-only list if absent.
  function fileList() {
    return (state.allFiles && state.allFiles.length) ? state.allFiles : state.files;
  }
  function renderFileTree() {
    const tree = $("file-tree");
    tree.innerHTML = "";

    // Build a nested tree from the flat path list. Each node:
    //   { name, path, isDir, children: [ ... ] }
    // Root is a synthetic node. Directories are created on demand; a file is
    // attached under the deepest matching directory prefix.
    const root = { name: "", path: "", isDir: true, children: [] };
    function ensureDir(parts, i) {
      let node = root;
      for (let k = 0; k < i; k++) {
        const part = parts[k];
        let child = node.children.find((c) => c.isDir && c.name === part);
        if (!child) {
          child = { name: part, path: parts.slice(0, k + 1).join("/"),
                    isDir: true, children: [] };
          node.children.push(child);
        }
        node = child;
      }
      return node;
    }
    fileList().forEach((f) => {
      const parts = f.split("/");
      const name = parts[parts.length - 1];
      const parent = ensureDir(parts, parts.length - 1);
      parent.children.push({ name, path: f, isDir: false, children: [] });
    });

    // Sort: directories first (alphabetical), then files (alphabetical).
    function sortNode(node) {
      node.children.sort((a, b) => {
        if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
        return a.name.localeCompare(b.name);
      });
      node.children.forEach(sortNode);
    }
    sortNode(root);

    // Recursive render. ``depth`` drives the left indent. Open/collapse state
    // is persisted in state.openDirs (a Set of directory paths) because the
    // node objects are rebuilt on every render. Top-level folders (depth 0)
    // are open by default; deeper folders start collapsed.
    if (!state.openDirs) { state.openDirs = new Set(); state.treeInit = false; }
    const DIR_INDENT = 14;
    function makeRow(node, depth) {
      const el = document.createElement("div");
      el.className = "file-item" + (node.isDir ? " dir" : "") +
                     (node.path === state.currentFile ? " active" : "");
      el.style.paddingLeft = (10 + depth * DIR_INDENT) + "px";

      const icon = document.createElement("span");
      icon.className = "tree-icon";
      if (node.isDir) {
        const open = state.openDirs.has(node.path);
        icon.textContent = open ? "▾" : "▸";
        icon.title = "Expand / collapse";
      } else {
        icon.textContent = "📄";
      }
      el.appendChild(icon);

      const fname = document.createElement("span");
      fname.className = "fname";
      fname.textContent = node.name;
      fname.title = node.path;
      el.appendChild(fname);

      if (!node.isDir) {
        const isChanged = state.changed.includes(node.path);
        if (isChanged) {
          const dot = document.createElement("span");
          dot.className = "dot";
          el.insertBefore(dot, icon);
        }
        // Per-file actions: rename (→) and delete (×), revealed on hover.
        const actions = document.createElement("span");
        actions.className = "file-actions";
        const renameBtn = document.createElement("button");
        renameBtn.title = "Rename / move"; renameBtn.textContent = "→";
        renameBtn.addEventListener("click", (e) => { e.stopPropagation(); renameFile(node.path); });
        const delBtn = document.createElement("button");
        delBtn.title = "Delete"; delBtn.textContent = "×"; delBtn.className = "del";
        delBtn.addEventListener("click", (e) => { e.stopPropagation(); deleteFile(node.path); });
        actions.appendChild(renameBtn);
        actions.appendChild(delBtn);
        el.appendChild(actions);
        el.addEventListener("click", () => openFile(node.path));
      }

      tree.appendChild(el);

      if (node.isDir) {
        // First render: open top-level folders by default.
        if (depth === 0 && !state.treeInit) {
          state.openDirs.add(node.path);
        }
        const open = state.openDirs.has(node.path);
        el.addEventListener("click", (e) => {
          e.stopPropagation();
          if (state.openDirs.has(node.path)) state.openDirs.delete(node.path);
          else state.openDirs.add(node.path);
          renderFileTree();
        });
        // "Generate index.rst" action for this folder (revealed on hover),
        // appended at the right so the triangle / name never shift.
        const idxBtn = document.createElement("button");
        idxBtn.className = "gen-index";
        idxBtn.title = "Generate index.rst (toctree) from the files in this folder";
        idxBtn.textContent = "📑";
        idxBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          generateIndex(node.path);
        });
        el.appendChild(idxBtn);
        if (open) {
          node.children.forEach((c) => makeRow(c, depth + 1));
        }
      }
      return el;
    }
    root.children.forEach((c) => makeRow(c, 0));
    state.treeInit = true; // top-level folders are now open by default

    renderChanged();
  }

  function renderChanged() {
    const box = $("changed-files");
    if (!state.changed.length) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    box.innerHTML = "<h4>Uncommitted changes</h4>" +
      state.changed.map((c) => "<div class='cf'>• " + c + "</div>").join("");
  }

  async function openFile(path) {
    // Guard: don't switch away from a dirty text file and lose edits. (Image /
    // binary files are never editable, so they can't be dirty.)
    if (!await ensureSaved()) return;
    const { ok, d } = await fetchJSON("/api/file?repo_id=" + state.repoId + "&path=" + encodeURIComponent(path));
    if (!ok) { alert("Could not open: " + d.error); return; }
    state.currentFile = path;
    state.dirty = false;
    state.currentKind = d.kind || "text";   // "text" | "image" | "binary"
    $("current-file").textContent = path;
    if (d.kind === "image") {
      showImageView(d.data_uri, path);
      hideTextView();
      resetHistory("");
      renderFileTree();
    } else if (d.kind === "binary") {
      showBinaryView(path, d.size);
      hideTextView();
      resetHistory("");
      renderFileTree();
    } else {
      hideFileView();
      showTextView();          // re-show source/preview/toolbar (may be hidden from an image/binary view)
      $("source").value = d.content;
      resetHistory(d.content);
      renderFileTree();
      renderPreview();
    }
  }

  // ------------------------------------------------------------- text vs image/binary views
  function hideTextView() {
    $("source-wrap").classList.add("hidden");
    $("preview-wrap").classList.add("hidden");
    $("toolbar").classList.add("hidden");
  }
  function showTextView() {
    $("source-wrap").classList.remove("hidden");
    $("preview-wrap").classList.remove("hidden");
    $("toolbar").classList.remove("hidden");
    applyView();
  }
  function hideFileView() {
    $("file-view").classList.add("hidden");
  }
  function showImageView(dataUri, path) {
    const fv = $("file-view");
    fv.innerHTML =
      '<div class="fv-title">' + escapeHtml(path) + '</div>' +
      '<img alt="' + escapeHtml(path) + '" src="' + dataUri + '" />' +
      '<div class="fv-meta">Image preview (read-only)</div>';
    fv.classList.remove("hidden");
  }
  function showBinaryView(path, size) {
    const fv = $("file-view");
    const kb = (size != null) ? (size / 1024).toFixed(1) + " KB" : "unknown size";
    fv.innerHTML =
      '<div class="fv-bad">🚫</div>' +
      '<div class="fv-title">' + escapeHtml(path) + '</div>' +
      '<div class="fv-meta">This is a binary file (' + kb + ') and cannot be displayed as text.</div>' +
      '<div class="fv-meta">To change it, edit it outside the app (git) — or replace it via Upload.</div>';
    fv.classList.remove("hidden");
  }
  function escapeHtml(s) {
    return (s == null ? "" : String(s))
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // ------------------------------------------------------------- file CRUD
  // Pull the file lists out of a CRUD response and refresh the tree + status.
  function applyFileList(d) {
    if (d.files) state.files = d.files;
    if (d.all_files) state.allFiles = d.all_files;
    if (d.changed) state.changed = d.changed;
    renderFileTree();
  }

  // "＋ New": open the new-file / directory dialog.
  function newFile() {
    if (!state.repoId) { alert("Clone a repository first."); return; }
    $("new-path").value = (state.currentFile
      ? state.currentFile.slice(0, state.currentFile.lastIndexOf("/") + 1)
      : "") + "new.rst";
    $("new-isdir").checked = false;
    $("new-status").textContent = "";
    $("new-status").className = "status";
    $("new-dialog").classList.remove("hidden");
    $("new-path").focus();
    $("new-path").select();
  }
  // Keep the "create as directory" checkbox in sync with a trailing slash.
  function syncNewIsdir() {
    const v = $("new-path").value.trim();
    $("new-isdir").checked = v.endsWith("/") || $("new-isdir").checked;
  }
  function createNew() {
    const raw = $("new-path").value.trim();
    if (!raw) {
      $("new-status").textContent = "Path is required.";
      $("new-status").className = "status";
      return;
    }
    const isDir = $("new-isdir").checked || raw.endsWith("/");
    const path = raw.replace(/\/+$/, "");
    if (!path) {
      $("new-status").textContent = "Path is required.";
      $("new-status").className = "status";
      return;
    }
    fetchJSON("/api/file-new", { method: "POST",
      body: JSON.stringify({ repo_id: state.repoId, path: path,
                             is_dir: isDir, content: "" }) })
      .then(({ ok, d }) => {
        if (!ok) {
          $("new-status").textContent = "Create failed: " + (d.detail || d.error);
          $("new-status").className = "status";
          return;
        }
        applyFileList(d);          // refresh tree (new dir/file appears)
        $("new-dialog").classList.add("hidden");
        if (d.is_dir) {
          // A directory: leave whatever file is open as-is; just expand the new
          // folder so the user can see it appear.
          (function expand(p) {
            const parts = p.split("/");
            for (let k = 1; k <= parts.length; k++) {
              state.openDirs.add(parts.slice(0, k).join("/"));
            }
            renderFileTree();
          })(path);
        } else {
          openFile(path);          // opens it, refreshing the tree again
        }
      })
      .catch((e) => {
        $("new-status").textContent = "Create failed: " + e.message;
        $("new-status").className = "status";
      });
  }

  // Derive a default index title from a directory path: use the directory's
  // basename; if it's dash-separated, replace the dashes with spaces and
  // capitalize the first letter of the first word (e.g. "my-cool-dir" -> "My
  // cool dir", "umn" -> "Umn").
  function dirToTitle(dir) {
    const base = (dir || "").replace(/\\/g, "/").replace(/\/+$/, "").split("/").pop() || "";
    if (!base) return "Contents";
    const words = base.split("-").filter(Boolean);
    if (!words.length) return base;
    const first = words[0][0].toUpperCase() + words[0].slice(1);
    return [first].concat(words.slice(1)).join(" ");
  }

  // "📑 Generate index.rst": build a toctree index for a folder from the .rst
  // files and sub-folders it contains. Prompts for a title (defaulting to the
  // directory name); if index.rst already exists, prompts to confirm
  // overwriting. Then opens the result.
  function generateIndex(dir) {
    if (!state.repoId) { alert("Clone a repository first."); return; }
    const displayName = dir || "(repository root)";
    const defaultTitle = dirToTitle(dir);
    const title = prompt(
      "Generate index.rst in \"" + displayName + "\"\n\n" +
      "Title for the index page (blank = \"" + defaultTitle + "\"):", defaultTitle);
    if (title === null) return;   // user cancelled
    const finalTitle = (title.trim() || defaultTitle);
    const doGenerate = (overwrite) => {
      const body = { repo_id: state.repoId, dir: dir, title: finalTitle };
      if (overwrite) body.overwrite = true;
      fetchJSON("/api/index-generate", { method: "POST", body: JSON.stringify(body) })
        .then(({ ok, d }) => {
          if (d.conflict) {
            // index.rst already exists -> confirm overwrite.
            const yes = confirm(
              "index.rst already exists in \"" + displayName + "\".\n\n" +
              "Regenerate and overwrite it?");
            if (yes) doGenerate(true);
            return;
          }
          if (!ok) { alert("Generate index failed: " + (d.detail || d.error)); return; }
          applyFileList(d);
          openFile(d.path);  // opens the generated index.rst
        })
        .catch((e) => alert("Generate index failed: " + e.message));
    };
    doGenerate(false);
  }

  // "⬆ Upload": pick file(s), choose a destination folder, upload.
  let pendingUpload = null;
  $("btn-new-file").addEventListener("click", newFile);
  // New-file / directory dialog.
  $("new-cancel").addEventListener("click", () => $("new-dialog").classList.add("hidden"));
  $("new-go").addEventListener("click", createNew);
  $("new-path").addEventListener("input", syncNewIsdir);
  $("new-isdir").addEventListener("change", () => {
    // Checking "create as directory" appends a trailing slash so the intent is
    // visible; unchecking removes it.
    const v = $("new-path").value.trim();
    if ($("new-isdir").checked && !v.endsWith("/")) $("new-path").value = v + "/";
    else if (!$("new-isdir").checked && v.endsWith("/")) $("new-path").value = v.replace(/\/+$/, "");
  });
  $("new-path").addEventListener("keydown", (e) => { if (e.key === "Enter") createNew(); });
  $("new-dialog").addEventListener("click", (e) => {
    if (e.target.id === "new-dialog") $("new-dialog").classList.add("hidden");
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("new-dialog").classList.contains("hidden")) {
      $("new-dialog").classList.add("hidden");
    }
  });
  $("btn-upload-file").addEventListener("click", () => {
    if (!state.repoId) { alert("Clone a repository first."); return; }
    $("file-upload-input").value = "";
    $("file-upload-input").click();
  });
  $("file-upload-input").addEventListener("change", () => {
    const files = $("file-upload-input").files;
    if (!files || !files.length) return;
    pendingUpload = files;
    $("upload-count").textContent = files.length + (files.length === 1 ? " file" : " files") + " selected";
    $("upload-dest").value = "";
    $("upload-status").textContent = "";
    $("upload-dialog").classList.remove("hidden");
  });
  $("upload-cancel").addEventListener("click", () => { $("upload-dialog").classList.add("hidden"); });
  $("upload-go").addEventListener("click", () => {
    if (!pendingUpload || !pendingUpload.length) return;
    const fd = new FormData();
    for (let i = 0; i < pendingUpload.length; i++) fd.append("files", pendingUpload[i]);
    fd.append("repo_id", state.repoId);
    fd.append("dest_dir", $("upload-dest").value.trim());
    $("upload-go").disabled = true;
    fetch("/api/file-upload", { method: "POST", body: fd })
      .then((r) => r.json().then((d) => ({ ok: r.ok, d })))
      .then(({ ok, d }) => {
        if (!ok) { alert("Upload failed: " + (d.detail || d.error)); return; }
        $("upload-dialog").classList.add("hidden");
        applyFileList(d);
        toast($("upload-status"), d.files.length + " file(s) uploaded.", "ok");
      })
      .catch((e) => alert("Upload failed: " + e.message))
      .finally(() => { $("upload-go").disabled = false; });
  });

  function deleteFile(path) {
    if (!state.repoId) return;
    if (!confirm("Delete " + path + "?\n\nThis removes it from the working copy and stages the deletion. Commit + push to make it permanent.")) return;
    fetchJSON("/api/file-delete", { method: "POST",
      body: JSON.stringify({ repo_id: state.repoId, path }) })
      .then(({ ok, d }) => {
        if (!ok) { alert("Delete failed: " + (d.detail || d.error)); return; }
        applyFileList(d);
        if (state.currentFile === path) {
          // The open file is gone; clear the editor.
          state.currentFile = null; state.dirty = false;
          $("current-file").textContent = "— select a file —";
          $("source").value = ""; resetHistory("");
          renderFileTree();
        }
      })
      .catch((e) => alert("Delete failed: " + e.message));
  }

  function renameFile(path) {
    if (!state.repoId) return;
    const np = prompt("Rename / move " + path + " to:", path);
    if (np === null) return;
    const p = np.trim();
    if (!p || p === path) return;
    fetchJSON("/api/file-rename", { method: "POST",
      body: JSON.stringify({ repo_id: state.repoId, path, new_path: p }) })
      .then(({ ok, d }) => {
        if (!ok) { alert("Rename failed: " + (d.detail || d.error)); return; }
        applyFileList(d);
        if (state.currentFile === path) {
          state.currentFile = d.new;
          $("current-file").textContent = d.new;
          openFile(d.new);
        }
      })
      .catch((e) => alert("Rename failed: " + e.message));
  }

  // ------------------------------------------------------------- preview
  // Resolve a toctree entry to a repo file path (or null if none matches).
  //
  // Bare entries in a toctree are *relative to the index.rst that contains
  // them* (that's how Sphinx resolves them), so ``base`` is the directory of
  // the index file ("" for the repo root). Resolution order:
  //   "foo/index.rst"  -> literal file path, if present
  //   "foo/index"      -> foo/index.rst
  //   "foo"            -> foo.rst  OR  foo/index.rst (a folder with its own index)
  //   "a/b" (no .rst)  -> a/b.rst  OR  a/b/index.rst
  //   "x.rst"          -> x.rst only (an explicit file)
  // A "base" prefix is prepended to bare entries; absolute (leading /) and
  // already-qualified paths are left as-is.
  function resolveTocEntry(entry, files, base) {
    const b = (base || "").replace(/\\/g, "/").replace(/^\.\//, "").replace(/\/+$/, "");
    let raw = entry.replace(/\\/g, "/").replace(/^\.\//, "").replace(/^\/+/, "");
    if (!raw) return null;
    const hasExt = /\.rst$/i.test(raw);
    const candidates = [];
    if (hasExt) {
      candidates.push(raw);
    } else {
      // "foo/index" -> dir foo; "foo" -> dir foo.
      const dir = raw.replace(/\/index$/i, "");
      candidates.push(dir + ".rst", (dir ? dir + "/" : "") + "index.rst");
    }
    // Try under the base directory first, then verbatim (already-qualified
    // repo paths resolve to the second variant).
    const variants = [(b ? b + "/" : "") + raw, raw];
    for (const v of variants) {
      for (const c of candidates) {
        const full = (b && v === raw) ? (b + "/" + c) : c;
        if (files.includes(full)) return full;
      }
    }
    return null;
  }

  // Build the clickable toctree block for the preview (docutils' html writer
  // drops ``.. toctree::``, so we surface it ourselves).
  function toctreeHtml(tocEntries, files, base) {
    if (!tocEntries || !tocEntries.length) return "";
    const linkFor = (entry) => {
      const p = resolveTocEntry(entry, files, base);
      if (p) {
        return '<a href="#" class="tocfile" data-path="' + escapeHtml(p) +
               '">' + escapeHtml(entry) + '</a>';
      }
      return '<span class="tocfile-missing" title="No matching file found">' +
             escapeHtml(entry) + '</span>';
    };
    return tocEntries.map((t) => {
      const cap = t.caption ? '<div class="tocfile-cap">' + escapeHtml(t.caption) + '</div>' : "";
      const items = (t.entries || []).map((e) => '<li>' + linkFor(e) + '</li>').join("");
      return '<div class="tocfile">' + cap + '<ul>' + items + '</ul></div>';
    }).join("");
  }

  function renderPreview() {
    const src = $("source").value;
    const body = { source: src };
    if (state.repoId && state.currentFile) {
      body.repo_id = state.repoId;
      body.path = state.currentFile;
    }
    // toctree entries are relative to the directory of the file being previewed.
    const base = state.currentFile
      ? state.currentFile.replace(/\\/g, "/").replace(/[^/]*$/, "") : "";
    fetchJSON("/api/preview", { method: "POST", body: JSON.stringify(body) })
      .then(({ d }) => {
        $("preview").innerHTML = d.html || "<em>(empty)</em>";
        // TOC (in-page section links)
        const tocBox = $("toc-box");
        if (d.toc) { tocBox.innerHTML = d.toc; tocBox.classList.remove("hidden"); }
        else tocBox.classList.add("hidden");
        // toctree -> clickable links to the referenced files
        const pEl = $("preview");
        const tBlock = toctreeHtml(d.toc_entries, fileList(), base);
        if (tBlock) {
          const wrap = document.createElement("div");
          wrap.className = "tocfile-wrap";
          wrap.innerHTML = tBlock;
          pEl.appendChild(wrap);
          wrap.querySelectorAll("a.tocfile").forEach((a) => {
            a.addEventListener("click", (e) => { e.preventDefault(); openFile(a.dataset.path); });
          });
        }
        // Warnings
        const wBox = $("warnings");
        if (d.warnings && d.warnings.length) {
          wBox.innerHTML = d.warnings.map((w) => "<div class='w'>⚠ " + w + "</div>").join("");
          wBox.classList.remove("hidden");
        } else wBox.classList.add("hidden");
      });
  }

  // ------------------------------------------------------------- toolbar
  function selStart() { return $("source").selectionStart; }
  function selEnd() { return $("source").selectionEnd; }
  function getSel() {
    const t = $("source");
    return t.value.slice(t.selectionStart, t.selectionEnd);
  }

  // ----- undo / redo history -----------------------------------------------
  // Native textarea undo is wiped whenever we set .value programmatically, so
  // we keep our own snapshot history and take over Ctrl/Cmd+Z / Shift+Z.
  let hist = [];          // [{value, selStart, selEnd}]
  let histIndex = -1;
  let suppressEdit = false; // true while we set .value programmatically

  function commit(value) {
    const top = hist[histIndex];
    if (top && top.value === value) return;
    hist.length = histIndex + 1; // drop any redo tail
    hist.push({ value, selStart: $("source").selectionStart, selEnd: $("source").selectionEnd });
    if (hist.length > 300) hist.shift();
    histIndex = hist.length - 1;
    updateUndoRedoUI();
  }
  function resetHistory(value) {
    hist = [{ value, selStart: 0, selEnd: 0 }];
    histIndex = 0;
    updateUndoRedoUI();
  }
  function doUndo() {
    if (histIndex <= 0) return;
    histIndex--;
    restoreHist(hist[histIndex]);
  }
  function doRedo() {
    if (histIndex >= hist.length - 1) return;
    histIndex++;
    restoreHist(hist[histIndex]);
  }
  function restoreHist(s) {
    const t = $("source");
    suppressEdit = true;
    t.value = s.value;
    t.selectionStart = s.selStart;
    t.selectionEnd = s.selEnd;
    suppressEdit = false;
    state.dirty = true;
    renderPreview();
    updateUndoRedoUI();
  }
  function updateUndoRedoUI() {
    $("btn-undo").classList.toggle("hidden", histIndex <= 0);
    $("btn-redo").classList.toggle("hidden", histIndex >= hist.length - 1);
  }

  // Write a new value (and selection) into the editor, recording it in history.
  function setEditorValue(value, start, end) {
    const t = $("source");
    suppressEdit = true;
    t.value = value;
    t.focus();
    t.selectionStart = start;
    t.selectionEnd = (end == null) ? start : end;
    suppressEdit = false;
    commit(value);
    state.dirty = true;
    renderPreview();
  }
  function replaceSel(text) {
    const t = $("source");
    const start = t.selectionStart, end = t.selectionEnd;
    const value = t.value.slice(0, start) + text + t.value.slice(end);
    setEditorValue(value, start + text.length, start + text.length);
  }
  function wrapSel(prefix, suffix) {
    const t = $("source");
    const pfx = prefix, sfx = (suffix || prefix);
    const start = t.selectionStart, end = t.selectionEnd;
    const inner = t.value.slice(start, end);
    // A browser double-click selects "word " (the trailing space is part of the
    // selection). If we wrapped that, the RST markup would swallow the space and
    // the words would merge (e.g. "**bold**next"). So trim the surrounding
    // whitespace out of the formatting — only the pure word gets the markup,
    // and any leading/trailing space is left outside the markup.
    const leadWs = (inner.match(/^\s*/) || [""])[0];
    const trailWs = (inner.match(/\s*$/) || [""])[0];
    const core = inner.slice(leadWs.length, inner.length - trailWs.length);
    if (!core) {
      // Nothing but whitespace selected: insert an empty pair so the user can
      // type inside it.
      setEditorValue(t.value.slice(0, start) + pfx + sfx + t.value.slice(end),
                     start + pfx.length, start + pfx.length);
      return;
    }
    const value = t.value.slice(0, start) + leadWs + pfx + core + sfx + trailWs + t.value.slice(end);
    // Re-select the formatted core so chained edits (bold -> italic) work.
    setEditorValue(value, start + leadWs.length + pfx.length,
                   start + leadWs.length + pfx.length + core.length);
  }
  function linePrefix(prefix) {
    const t = $("source");
    const lines = t.value.split("\n");
    const startLine = t.value.slice(0, t.selectionStart).split("\n").length - 1;
    const endLine = t.value.slice(0, t.selectionEnd).split("\n").length - 1;
    for (let i = startLine; i <= endLine; i++) {
      lines[i] = prefix + lines[i];
    }
    const value = lines.join("\n");
    const newEnd = t.selectionEnd + (endLine - startLine + 1) * prefix.length;
    setEditorValue(value, t.selectionStart + prefix.length, newEnd);
  }

  // =============================================================== RST grid tables
  // A grid table is a block of border lines (`+----+----+`) and cell lines
  // (`| a    | b    |`). Column widths are computed from the widest cell, so
  // editing one cell re-aligns the whole table (the "dynamic adaptation").

  function isBorderLine(s) {
    return /^\+(?:[-=]+\+)+$/.test(s) && s.length >= 5;
  }
  function borderColPos(s) {
    const p = [];
    for (let i = 0; i < s.length; i++) if (s[i] === "+") p.push(i);
    return p;
  }
  function colOf(ci, p) {
    for (let c = 0; c < p.length - 1; c++) {
      if (ci >= p[c] && ci < p[c + 1]) return c;
    }
    if (ci >= p[p.length - 1]) return p.length - 2;
    return -1;
  }
  function lineOf(text, pos) {
    let ln = 0;
    for (let i = 0; i < pos && i < text.length; i++) if (text[i] === "\n") ln++;
    return ln;
  }
  // Render a table: rows = [ [cell, cell, ...], ... ], each cell = [line, line, ...].
  // Returns {text, lines, colPos, rowCellLines}.
  function renderBlock(rows, colWidths) {
    const ncol = colWidths.length;
    const lines = [];
    const rowCellLines = [];
    const border = (fill) => {
      let s = "+";
      for (let c = 0; c < ncol; c++) s += fill.repeat(colWidths[c]) + "+";
      return s;
    };
    const cellLine = (r, i) => {
      let s = "|";
      for (let c = 0; c < ncol; c++) {
        const cell = rows[r][c];
        const ln = (i < cell.length) ? cell[i] : "";
        const w = colWidths[c];
        // Interior width is `w`: one leading space + content + trailing pad.
        // computeWidths guarantees w >= len + 2, so there is always >=1
        // trailing space (required for RST grid syntax).
        const content = ln.length > (w - 1)
          ? ln
          : (" " + ln + " ".repeat((w - 1) - ln.length));
        s += content + "|";
      }
      return s;
    };
    lines.push(border("-"));
    for (let r = 0; r < rows.length; r++) {
      const cl = Math.max(1, ...rows[r].map((cell) => cell.length));
      rowCellLines.push(cl);
      for (let i = 0; i < cl; i++) lines.push(cellLine(r, i));
      lines.push(border(r === 0 ? "=" : "-"));
    }
    return { text: lines.join("\n"), lines, colPos: borderColPos(lines[0]), rowCellLines };
  }
  function computeWidths(rows) {
    const ncol = rows[0].length;
    const w = new Array(ncol).fill(4); // min interior width
    for (const row of rows) {
      for (let c = 0; c < ncol; c++) {
        for (const cellLine of row[c]) w[c] = Math.max(w[c], cellLine.length + 2);
      }
    }
    return w;
  }
  // Split a cell line into cell strings (lenient to malformed lines).
  function extractCells(line, ncol) {
    const parts = line.split("|");
    const cells = [];
    for (let c = 0; c < ncol; c++) cells.push((parts[c + 1] || "").trim());
    return cells;
  }
  function parseRows(lines, top, bottom, p) {
    const ncol = p.length - 1;
    const rows = [];
    const lineMap = [];
    let k = top + 1;
    let r = 0;
    let cellLine = 0;
    while (k <= bottom) {
      // Any border line (there is none above the top) closes the current row
      // and opens the next one.
      if (isBorderLine(lines[k])) { r++; cellLine = 0; k++; continue; }
      const cells = extractCells(lines[k], ncol);
      if (!rows[r]) rows[r] = [];
      for (let c = 0; c < ncol; c++) {
        if (!rows[r][c]) rows[r][c] = [];
        rows[r][c].push(cells[c]);
      }
      lineMap[k] = { row: r, cellLine: cellLine };
      cellLine++;
      k++;
    }
    return { rows, lineMap };
  }
  // A grid line is either a border line or a cell line.
  function isTableLine(s) {
    if (isBorderLine(s)) return true;
    return s.length >= 2 && s[0] === "|" && s[s.length - 1] === "|";
  }
  // Find the grid table containing char position `pos`. Returns
  // {top, bottom, p, lines, lineStarts, ln, ci} or null. The table is the
  // maximal contiguous run of grid lines (borders + cell lines) that contains
  // the cursor line and is bounded by border lines.
  function findTable(text, pos) {
    const lines = text.split("\n");
    const lineStarts = [0];
    for (let k = 0; k < lines.length; k++) lineStarts[k + 1] = lineStarts[k] + lines[k].length + 1;
    const ln = lineOf(text, pos);
    const ci = pos - lineStarts[ln];
    if (ln >= lines.length) return null;
    // Expand up to the top of the contiguous table block.
    let top = ln;
    while (top - 1 >= 0 && isTableLine(lines[top - 1])) top--;
    // Expand down to the bottom of the contiguous table block.
    let bottom = ln;
    while (bottom + 1 < lines.length && isTableLine(lines[bottom + 1])) bottom++;
    // The block must be bounded by border lines and have >= 2 columns.
    if (!isBorderLine(lines[top]) || !isBorderLine(lines[bottom])) return null;
    if (top + 1 > bottom) return null;
    const p = borderColPos(lines[top]);
    if (p.length < 3) return null;
    if (ln < top || ln > bottom) return null;
    return { top, bottom, p, lines, lineStarts, ln, ci };
  }
  // Parse the table at the cursor. Returns {ft, rows, r, cl, c} or null
  // (null if no table or the cursor is on a border line).
  function tableAtCaret() {
    const t = $("source");
    const text = t.value;
    const pos = t.selectionStart;
    const ft = findTable(text, pos);
    if (!ft) return null;
    const pr = parseRows(ft.lines, ft.top, ft.bottom, ft.p);
    const lm = pr.lineMap[ft.ln];
    if (!lm) return null;
    const c = colOf(ft.ci, ft.p);
    if (c < 0) return null;
    return { ft, rows: pr.rows, r: lm.row, cl: lm.cellLine, c };
  }
  // Track which cell the caret is in across keystrokes. When content overflows
  // a column (you typed a long string into a narrow column), the caret position
  // no longer lines up with the column boundaries, so we remember the cell.
  let activeCell = null;
  // Re-render a table (recomputed widths) and set the caret to a target cell.
  function applyTableEdit(ft, rows, target, contentOffset) {
    const t = $("source");
    const text = t.value;
    const block = renderBlock(rows, computeWidths(rows));
    const before = text.slice(0, ft.lineStarts[ft.top]);
    const after = text.slice(ft.lineStarts[ft.bottom] + ft.lines[ft.bottom].length);
    const newVal = before + block.text + after;
    let newLineIdx = 1;
    for (let r2 = 0; r2 < rows.length; r2++) {
      const cl2 = block.rowCellLines[r2];
      if (r2 < target.row) newLineIdx += cl2 + 1;
      else if (r2 === target.row) { newLineIdx += Math.min(target.cellLine, cl2 - 1); break; }
    }
    let blkOff = 0;
    for (let j = 0; j < newLineIdx; j++) blkOff += block.lines[j].length + 1;
    const p2 = block.colPos;
    const c2 = Math.min(target.col, p2.length - 2);
    const newCaret = ft.lineStarts[ft.top] + blkOff + (p2[c2] + 2 + (contentOffset || 0));
    activeCell = { row: target.row, col: c2, cellLine: target.cellLine };
    setEditorValue(newVal, newCaret, newCaret);
  }
  // Determine which column the caret is in, given the table's borders (p), the
  // caret's column index (ci), the row's cell contents, and the remembered cell.
  // Handles: in-range caret, first overflow char, and ongoing overflow (where
  // caret position is ambiguous so the active cell wins if it spans the caret).
  function detectColumn(p, ci, row, cl, contentOf) {
    let col = -1;
    for (let c = 0; c < p.length - 1; c++) {
      if (p[c] + 2 <= ci && ci <= p[c + 1] - 1) { col = c; break; }
    }
    if (col < 0) {
      const prev = ci - 1;
      for (let c = 0; c < p.length - 1; c++) {
        if (p[c] + 2 <= prev && prev <= p[c + 1] - 1) { col = c; break; }
      }
    }
    if (col < 0 && activeCell && activeCell.row === row && activeCell.col < p.length - 1) {
      const c = activeCell.col;
      if (p[c] + 2 <= ci && ci <= p[c] + 2 + contentOf(c).length) col = c;
    }
    if (col < 0) {
      let best = 0, bd = Infinity;
      for (let c = 0; c < p.length - 1; c++) {
        const d = Math.abs(ci - (p[c] + 1));
        if (d < bd) { bd = d; best = c; }
      }
      col = best;
    }
    return col;
  }
  // Live re-align: if the cursor is in a grid table cell, re-render the whole
  // table with widths recomputed from content (the "dynamic adaptation").
  function maybeRealignTable() {
    if (state.currentKind !== "text") return false;
    const t = $("source");
    const text = t.value;
    const pos = t.selectionStart;
    const ft = findTable(text, pos);
    if (!ft) { activeCell = null; return false; }
    const pr = parseRows(ft.lines, ft.top, ft.bottom, ft.p);
    const lm = pr.lineMap[ft.ln];
    if (!lm) return false; // caret on a border line — no cell
    const p = ft.p, ci = ft.ci, row = lm.row, cl = lm.cellLine;
    const contentOf = (c) => (pr.rows[row][c] && pr.rows[row][c][cl]) || "";
    const col = detectColumn(p, ci, row, cl, contentOf);
    const cellText = contentOf(col);
    // Content offset measured from the cell's first content char (p[col] + 2).
    let contentOffset = ci - (p[col] + 2);
    contentOffset = Math.max(0, Math.min(contentOffset, cellText.length));
    applyTableEdit(ft, pr.rows, { row, cellLine: cl, col }, contentOffset);
    return true;
  }
  // Enter inside a cell: split the cell into two lines at the caret (extending
  // the cell, not breaking the grid), then re-render so the other columns grow
  // to match. Returns true if handled (the keydown handler should preventDefault).
  function enterInCell() {
    if (state.currentKind !== "text") return false;
    const t = $("source");
    const text = t.value;
    const pos = t.selectionStart;
    const ft = findTable(text, pos);
    if (!ft) return false;
    const pr = parseRows(ft.lines, ft.top, ft.bottom, ft.p);
    const lm = pr.lineMap[ft.ln];
    if (!lm) return false; // caret on a border line — let the default happen
    const p = ft.p, ci = ft.ci, row = lm.row, cl = lm.cellLine;
    const contentOf = (c) => (pr.rows[row][c] && pr.rows[row][c][cl]) || "";
    const col = detectColumn(p, ci, row, cl, contentOf);
    // Offset of the caret within the cell's current line of text.
    let offset = ci - (p[col] + 2);
    const lineText = contentOf(col);
    offset = Math.max(0, Math.min(offset, lineText.length));
    // Split this cell line into [before, after]; both become part of the cell.
    const cell = pr.rows[row][col];
    cell[cl] = lineText.slice(0, offset);
    cell.splice(cl + 1, 0, lineText.slice(offset));
    applyTableEdit(ft, pr.rows, { row, cellLine: cl + 1, col }, 0);
    return true;
  }
  // Backspace inside a cell: if the current cell line is a "blank" line (line
  // > 0) and EVERY other cell in the same row is also blank on that cell line,
  // remove the whole line from all cells in the row. This is the inverse of
  // Enter-in-cell. Works from any cell in the row. Returns true if handled.
  function backspaceInCell() {
    if (state.currentKind !== "text") return false;
    const t = $("source");
    const text = t.value;
    const pos = t.selectionStart;
    const ft = findTable(text, pos);
    if (!ft) return false;
    const pr = parseRows(ft.lines, ft.top, ft.bottom, ft.p);
    const lm = pr.lineMap[ft.ln];
    if (!lm) return false; // caret on a border line — default backspace
    const p = ft.p, ci = ft.ci, row = lm.row, cl = lm.cellLine;
    if (cl === 0) return false; // never remove a cell's first line
    const ncol = p.length - 1;
    // Remove the line only if every cell in this row is blank (no non-space
    // character) on that cell line.
    for (let c = 0; c < ncol; c++) {
      const ln = (pr.rows[row][c] && pr.rows[row][c][cl]) || "";
      if (ln.replace(/\s/g, "") !== "") return false; // has content -> keep
    }
    const col = detectColumn(p, ci, row, cl,
      (c) => (pr.rows[row][c] && pr.rows[row][c][cl]) || "");
    // Drop cell line `cl` from every cell in this row.
    for (let c = 0; c < ncol; c++) pr.rows[row][c].splice(cl, 1);
    // Caret -> end of the previous cell line in this column.
    const prevLen = ((pr.rows[row][col] && pr.rows[row][col][cl - 1]) || "").length;
    applyTableEdit(ft, pr.rows, { row, cellLine: cl - 1, col }, prevLen);
    return true;
  }
  // Perform a row/column operation on the table at the cursor.
  function tableEditOp(op) {
    if (state.currentKind !== "text") return;
    const t = $("source");
    const text = t.value;
    const pos = t.selectionStart;
    const ft = findTable(text, pos);
    if (!ft) return;
    const pr = parseRows(ft.lines, ft.top, ft.bottom, ft.p);
    const lm = pr.lineMap[ft.ln];
    if (!lm) return;
    const c = colOf(ft.ci, ft.p);
    if (c < 0) return;
    const rows = pr.rows;
    const r = lm.row, cl = lm.cellLine;
    const ncol = ft.p.length - 1;
    const emptyCell = () => [""];
    let targetRow = r, targetCellLine = cl, targetCol = c;
    if (op === "row-above") {
      if (rows.length <= 1) { alert("Cannot remove the last row"); return; }
      rows.splice(r, 0, new Array(ncol).fill(emptyCell()));
      targetRow = r;
    } else if (op === "row-below") {
      rows.splice(r + 1, 0, new Array(ncol).fill(emptyCell()));
      targetRow = r + 1;
    } else if (op === "col-left") {
      rows.forEach((row) => row.splice(c, 0, emptyCell()));
      targetCol = c;
    } else if (op === "col-right") {
      rows.forEach((row) => row.splice(c + 1, 0, emptyCell()));
      targetCol = c + 1;
    } else if (op === "row-del") {
      if (rows.length <= 1) { alert("Cannot remove the last row"); return; }
      rows.splice(r, 1);
      targetRow = Math.min(r, rows.length - 1);
    } else if (op === "col-del") {
      if (ncol <= 1) { alert("Cannot remove the last column"); return; }
      rows.forEach((row) => row.splice(c, 1));
      targetCol = Math.min(c, (ft.p.length - 2));
    }
    applyTableEdit(ft, rows, { row: targetRow, cellLine: targetCellLine, col: targetCol }, 0);
  }

  const toolbarActions = {
    // RST inline markup: **strong** = bold, *emphasis* = italic,
    // ``literal`` = inline code, `text`_ = interpreted/hyperlinked text
    undo: doUndo,
    redo: doRedo,
    bold: () => wrapSel("**"),
    italic: () => wrapSel("*"),
    code: () => wrapSel("``"),
    link: () => {
      const url = prompt("URL:", "https://");
      if (!url) return;
      // A double-click selects "word " (trailing space included); a trailing
      // space in the RST hyperlink label breaks the markup, so trim it.
      const label = getSel().trim() || "link";
      // RST inline hyperlink: `label <url>`__
      replaceSel("`" + label + " <" + url + ">`__");
    },
    ul: () => linePrefix("- "),
    ol: () => linePrefix("1. "),
    quote: () => linePrefix("> "),
    h1: () => insertHeading("==="),
    h2: () => insertHeading("---"),
    h3: () => insertHeading("~~~"),
    note: () => replaceSel("\n.. note::\n\n    Your note here\n"),
    // Wrap the CURRENT SELECTION in a code-block directive (indented 4 spaces)
    // instead of replacing it. If nothing is selected, insert a template with
    // the cursor on the code line.
    codeblock: () => {
      const t = $("source");
      const lang = prompt("Language (blank = none):", "python");
      const codeLang = (lang || "").trim();
      const start = t.selectionStart, end = t.selectionEnd;
      const sel = t.value.slice(start, end);
      const header = ".. code-block::" + (codeLang ? " " + codeLang : "");
      let block;
      if (sel) {
        const indented = sel.replace(/\r?\n/g, "\n")
          .split("\n").map((l) => "    " + l).join("\n");
        block = header + "\n\n" + indented;
      } else {
        block = header + "\n\n    ";
      }
      const value = t.value.slice(0, start) + block + t.value.slice(end);
      // Put the cursor inside the (empty) code area for a fresh block.
      const caret = sel ? (start + block.length) : (start + header.length + 2 + 4);
      setEditorValue(value, caret, caret);
    },
    image: openImageDialog,
    // ---- Grid tables -------------------------------------------------
    // Insert a fresh 3-column grid table (header + 2 rows) where the selection
    // is. Each cell is an ARRAY of lines (here a single line each).
    gridTable: () => {
      const rows = [
        [["Header 1"], ["Header 2"], ["Header 3"]],
        [["Text"], ["Text"], ["Text"]],
        [["Text"], ["Text"], ["Text"]],
      ];
      const block = renderBlock(rows, computeWidths(rows));
      replaceSel("\n" + block.text + "\n");
    },
    // ---- Row / column operations (act on the table at the cursor) ----
    addRowAbove: () => tableEditOp("row-above"),
    addRowBelow: () => tableEditOp("row-below"),
    addColLeft: () => tableEditOp("col-left"),
    addColRight: () => tableEditOp("col-right"),
    deleteRow: () => tableEditOp("row-del"),
    deleteCol: () => tableEditOp("col-del"),
    save: () => saveFile(),
  };

  function insertHeading(char) {
    const sel = (getSel() || "New section").trim();
    const underline = char.repeat(Math.max(sel.length, 2));
    const block = "\n" + sel + "\n" + underline + "\n\n";
    replaceSel(block);
  }

  document.querySelectorAll("#toolbar button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const act = btn.getAttribute("data-act");
      if (toolbarActions[act]) toolbarActions[act]();
    });
  });

  // ------------------------------------------------------------- view toggle
  document.querySelectorAll(".view-toggle button").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".view-toggle button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.view = btn.getAttribute("data-view");
      applyView();
    });
  });
  // Parse owner/repo from a git URL (git@host:owner/repo or https://host/owner/repo).
  // Handles an SCP-like URL with a port (git@host:2222/owner/repo.git): without
  // this the port digits are read as the repo owner ("2222/docs/testservice").
  function parseOwnerRepo(url) {
    let u = (url || "").trim();
    // Normalize SCP-like-with-port to ssh:// so the port is not mistaken for the
    // owner (git@host:2222/owner/repo.git -> ssh://git@host:2222/owner/repo.git).
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
  function applyView() {
    const srcWrap = $("source-wrap");
    const prevWrap = $("preview-wrap");
    if (state.view === "source") { srcWrap.classList.remove("hidden"); prevWrap.classList.add("hidden"); }
    else if (state.view === "preview") { srcWrap.classList.add("hidden"); prevWrap.classList.remove("hidden"); }
    else { srcWrap.classList.remove("hidden"); prevWrap.classList.remove("hidden"); }
  }

  // ------------------------------------------------------------- save
  // Every editor change (manual or programmatic) funnels through commit(); it
  // dedupes consecutive identical values, so one logical edit = one history step.
  // Typing also refreshes the preview — debounced, since it's a server round-trip
  // and we don't want to render on every keystroke.
  let previewTimer = null;
  function schedulePreview() {
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(() => {
      previewTimer = null;
      renderPreview();
    }, 250);
  }
  $("source").addEventListener("input", () => {
    state.dirty = true;
    commit($("source").value);
    // If the edit landed inside a grid table, re-align the whole table so
    // column widths stay consistent (the "dynamic adaptation"). This happens
    // after commit() so the history stores the final, aligned text.
    maybeRealignTable();
    schedulePreview();
    scheduleAutosave();
  });
  $("source").addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && !e.shiftKey && e.key.toLowerCase() === "s") { e.preventDefault(); saveFile(); return; }
    // Take over undo / redo so our history (not the browser's) is used.
    if (mod && !e.altKey && e.key.toLowerCase() === "z") {
      e.preventDefault();
      if (e.shiftKey) doRedo(); else doUndo();
      return;
    }
    if (mod && e.key.toLowerCase() === "y") { e.preventDefault(); doRedo(); return; }
    // Enter inside a table cell: extend the cell with a new line (re-rendering
    // the grid) instead of breaking it with a raw newline.
    if (e.key === "Enter" && !mod && !e.altKey && enterInCell()) {
      e.preventDefault();
      return;
    }
    // Backspace inside a table cell: if the whole row is blank on this cell
    // line, remove that line from every cell in the row (inverse of Enter).
    if (e.key === "Backspace" && !mod && !e.altKey &&
        $("source").selectionStart === $("source").selectionEnd && backspaceInCell()) {
      e.preventDefault();
      return;
    }
  });

  // Autosave: a short debounce so we don't hit the API on every keystroke.
  let autosaveTimer = null;
  function scheduleAutosave() {
    if (!state.autosave || state.currentKind !== "text" || !state.currentFile) return;
    if (autosaveTimer) clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(() => { autosaveTimer = null; if (state.dirty) saveFile(); }, 1200);
  }

  // Before leaving a dirty text file: autosave if enabled, else ask.
  // Returns true if it's OK to proceed, false if the user wants to stay.
  async function ensureSaved() {
    if (!state.currentFile || !state.dirty) return true;
    if (state.currentKind !== "text") return true;
    if (state.autosave) { await saveFile(); return true; }
    if (!confirm("You have unsaved changes in " + state.currentFile + ". Save before switching?")) {
      return false;
    }
    await saveFile();
    return true;
  }

  async function saveFile() {
    if (!state.currentFile) return;
    const { ok, d } = await fetchJSON("/api/save", {
      method: "POST",
      body: JSON.stringify({
        repo_id: state.repoId,
        path: state.currentFile,
        content: $("source").value,
      }),
    });
    if (!ok) { alert("Save failed: " + (d.detail || d.error)); return; }
    state.dirty = false;
    state.changed = d.staged;
    renderFileTree();
    renderPreview();
  }

  // ------------------------------------------------------------- autosave toggle
  $("autosave").addEventListener("change", () => {
    state.autosave = $("autosave").checked;
    try { localStorage.setItem("rst_autosave", state.autosave ? "1" : "0"); } catch (e) {}
    // Turning autosave on with a dirty file: save now so nothing is lost.
    if (state.autosave && state.dirty && state.currentKind === "text") saveFile();
  });

  // ------------------------------------------------------------- status
  // Refetch the file lists so newly added files (import, new-file, commit)
  // appear in the left panel. This is the piece import was missing: the
  // status endpoint only reports changed/branch, not the file inventory.
  async function refreshFiles() {
    if (!state.repoId) return;
    try {
      const { d } = await fetchJSON("/api/files?repo_id=" + state.repoId);
      // /api/files returns {rst, all}; the clone response used {files, all_files}.
      if (d.rst) state.files = d.rst;
      if (d.all) state.allFiles = d.all;
    } catch (e) { /* keep the previous list on a transient failure */ }
    renderFileTree();
  }

  async function refreshStatus() {
    if (!state.repoId) return;
    const { d } = await fetchJSON("/api/status?repo_id=" + state.repoId);
    state.changed = d.changed;
    state.currentBranch = d.branch;
    $("branch-label").textContent = state.currentBranch + "  ←  " + state.sourceBranch;
    const badge = $("dirty-count");
    if (d.changed.length) { badge.textContent = d.changed.length + " changed"; badge.classList.remove("hidden"); }
    else badge.classList.add("hidden");
    // Pull the current file inventory too (imported / newly-created files
    // only show up in the panel once the list is refreshed).
    await refreshFiles();
  }

  // ------------------------------------------------------------- commit / push
  $("btn-commit").addEventListener("click", async () => {
    const msg = $("commit-msg").value.trim();
    if (!msg) return;
    const { ok, d } = await fetchJSON("/api/commit", {
      method: "POST",
      body: JSON.stringify({ repo_id: state.repoId, message: msg }),
    });
    if (!ok) { alert("Commit failed: " + (d.detail || d.error)); return; }
    $("commit-msg").value = "";
    state.changed = d.changed;
    refreshStatus();
  });

  $("btn-push").addEventListener("click", async () => {
    const { ok, d } = await fetchJSON("/api/push", {
      method: "POST",
      body: JSON.stringify({ repo_id: state.repoId, branch: state.currentBranch }),
    });
    if (!ok) { alert("Push failed: " + (d.detail || d.error)); return; }
    alert("Pushed " + d.branch);
  });

  // ------------------------------------------------------------- PR
  // The base repo is where the PR is created. Default = your own (fork) repo.
  // For a cross-repo/fork PR, set it to the upstream, e.g.
  // "opentelekomcloud-docs/test-repo". The head branch is then prefixed
  // "forkowner:branch" so the API knows it comes from the fork.
  function forkOwnerRepo() {
    return parseOwnerRepo(state.repoUrl || "");
  }
  function baseRepoOwnerRepo() {
    const v = $("pr-base-repo").value.trim();
    if (!v) return forkOwnerRepo();
    // Accept "owner/repo", or a full URL (https://…, git@host:owner/repo, or an
    // SCP-like URL with a port like git@host:2222/owner/repo). Strip any scheme
    // / git@host[:port] prefix down to "owner/repo" so a port is never mistaken
    // for the owner.
    let path = v;
    // scp-like WITH a port: git@host[:PORT]/owner/repo  (optionally .git)
    const scpPort = v.match(/^git@[^:/]+:\d+\/(.+)$/);
    // scp-like WITHOUT a port: git@host:owner/repo  (optionally .git)
    const scpNoPort = v.match(/^git@[^:/]+:([^/]+\/.+)$/);
    // explicit scheme: scheme://[user@]host[:PORT]/owner/repo  (optionally .git)
    const scheme = v.match(/^[a-z]+:\/\/(?:[^/]*@)?[^/:]+(?::\d+)?\/(.+)$/i);
    if (scpPort) path = scpPort[1];
    else if (scpNoPort) path = scpNoPort[1];
    else if (scheme) path = scheme[1];
    // A plain "owner/repo" (or one already extracted) has no host prefix.
    path = path.replace(/\.git$/, "").replace(/^\/+/, "").replace(/\/+$/, "");
    const i = path.indexOf("/");
    if (i === -1) return { owner: "", repo: "" };
    return { owner: path.slice(0, i), repo: path.slice(i + 1) };
  }

  $("btn-pr").addEventListener("click", () => {
    $("pr-dialog").classList.remove("hidden");
    // Pre-fill: base repo = own fork, base branch = source, head = target.
    const forkr = forkOwnerRepo();
    $("pr-base-repo").value = forkr.owner + "/" + forkr.repo;
    $("pr-base").value = state.sourceBranch;
    $("pr-head-input").value = state.currentBranch;
    $("pr-head").textContent = state.currentBranch;
    $("pr-result-link").classList.add("hidden");
    const createBtn = $("pr-create");
    createBtn.disabled = false;
    createBtn.textContent = "Create";
    toast($("pr-status"), "", "");
  });

  // "upstream" button: fill the base repo from the saved upstream field.
  $("btn-pr-use-upstream").addEventListener("click", () => {
    const up = $("upstream").value.trim();
    if (!up) { toast($("pr-status"), "No upstream saved — set it in ⚙ Settings first.", "err"); return; }
    $("pr-base-repo").value = up;
  });

  $("pr-cancel").addEventListener("click", () => $("pr-dialog").classList.add("hidden"));
  // Close simply hides the dialog (e.g. after a successful create, where
  // "Cancel" would be misleading). Cancel + Create both remain available.
  $("pr-close").addEventListener("click", () => $("pr-dialog").classList.add("hidden"));

  $("pr-create").addEventListener("click", async () => {
    const s = window.__settings || {};
    const forkr = forkOwnerRepo();        // your fork
    const baseRepo = baseRepoOwnerRepo(); // upstream (or fork) — the PR target

    // CROSS-REPO (fork -> upstream) — matches the shape that actually works on
    // GitHub (verified via curl with a classic PAT):
    //   endpoint: /repos/{UPSTREAM}/{repo}/pulls   (the target project)
    //   head:     {forkowner}:{targetbranch}       (your branch, in the fork)
    //   base:     {basebranch}                     (plain branch, in the upstream)
    // A classic PAT can open a PR to the upstream this way; a fine-grained token
    // without upstream permission cannot (that returns 403). Same-repo PRs use
    // the fork endpoint with plain branch names.
    const headBranch = $("pr-head-input").value.trim();
    const baseBranch = $("pr-base").value.trim();
    const isCrossRepo = (baseRepo.owner && forkr.owner && baseRepo.owner !== forkr.owner);
    const head = isCrossRepo ? (forkr.owner + ":" + headBranch) : headBranch;
    const base = baseBranch; // always plain; cross-repo targets upstream endpoint
    const endpointOwner = isCrossRepo ? baseRepo.owner : forkr.owner;
    const endpointRepo = isCrossRepo ? baseRepo.repo : forkr.repo;

    const { ok, d } = await fetchJSON("/api/pr", {
      method: "POST",
      body: JSON.stringify({
        repo_id: state.repoId,
        head: head,
        base: base,
        // Pre-flight branch existence: the head branch must exist in the fork,
        // the base branch in the target project. Turns an opaque 422 into a
        // precise "branch X not found in owner/repo".
        head_branch: headBranch,
        head_repo: forkr.owner + "/" + forkr.repo,
        base_branch: baseBranch,
        base_repo: baseRepo.owner + "/" + baseRepo.repo,
        title: $("pr-title").value,
        body: $("pr-body").value,
        token: s.token || $("api-token").value,
        provider: s.provider || $("provider").value,
        api_base: s.api_base || $("api-base").value,
        proxy: s.proxy || $("proxy").value,
        owner: endpointOwner,   // upstream for a cross-repo PR, else the fork
        repo: endpointRepo,
      }),
    });
    if (!ok) { toast($("pr-status"), "Failed: " + (d.detail || d.error), "err"); return; }
    toast($("pr-status"), "PR created!", "ok");
    const link = $("pr-result-link");
    link.textContent = "Open PR #" + d.number + " →";
    link.href = d.html_url;
    link.classList.remove("hidden");
    // The PR exists now — Create is no longer an action. Close the window via
    // Close (or Cancel); the "Open PR" link stays for the user to follow.
    const createBtn = $("pr-create");
    createBtn.disabled = true;
    createBtn.textContent = "Created";
  });

  // ------------------------------------------------------------- import
  // Convert an uploaded docx/html/md file to RST inside the chosen doc folder.
  function loadDocFolders() {
    if (!state.repoId) return;
    fetchJSON("/api/doc-folders?repo_id=" + state.repoId).then(({ d }) => {
      const list = $("imp-folder-list");
      list.innerHTML = "";
      (d.folders || []).forEach((f) => {
        const o = document.createElement("option");
        o.value = f;
        list.appendChild(o);
      });
      // Sensible default: first known folder, else leave blank for the user.
      if (!$("imp-folder").value && d.folders && d.folders.length) {
        $("imp-folder").value = d.folders[0];
      }
    });
  }

  $("btn-import").addEventListener("click", () => {
    if (!state.repoId) { alert("Clone a repository first."); return; }
    $("import-dialog").classList.remove("hidden");
    $("imp-file").value = "";
    $("imp-status").textContent = ""; $("imp-status").className = "status";
    if (!$("imp-folder").value) $("imp-folder").value = "doc";
    syncImportLevelRow();
    loadDocFolders();
  });
  // Show the "heading level" control only in split mode.
  function syncImportLevelRow() {
    const split = $("imp-mode").value === "split";
    $("imp-level-row").classList.toggle("hidden", !split);
  }
  $("imp-mode").addEventListener("change", syncImportLevelRow);
  $("btn-imp-reload").addEventListener("click", loadDocFolders);
  $("imp-cancel").addEventListener("click", () => $("import-dialog").classList.add("hidden"));

  $("imp-import").addEventListener("click", async () => {
    const file = $("imp-file").files[0];
    if (!file) { toast($("imp-status"), "Choose a file to import.", "err"); return; }
    const folder = $("imp-folder").value.trim() || "doc";
    const mode = $("imp-mode").value;
    const splitLevel = $("imp-level").value || "1";
    const slug = $("imp-slug").value.trim() || file.name.replace(/\.[^.]+$/, "");
    const fd = new FormData();
    fd.append("repo_id", state.repoId);
    fd.append("file", file);
    fd.append("doc_folder", folder);
    fd.append("mode", mode);
    fd.append("split_level", splitLevel);
    fd.append("slug", slug);
    fd.append("title", slug);

    const btn = $("imp-import");
    btn.textContent = "Importing…"; btn.disabled = true;
    try {
      const r = await fetch("/api/import", { method: "POST", body: fd });
      const d = await r.json();
      if (!r.ok) { toast($("imp-status"), "Failed: " + (d.detail || d.error), "err"); return; }
      toast($("imp-status"),
        "Imported " + (d.files || []).length + " file(s)" +
        (d.images && d.images.length ? ", " + d.images.length + " image(s)" : ""), "ok");
      $("import-dialog").classList.add("hidden");
      // Refresh the tree + changed badge so the new .rst files are visible.
      refreshStatus();
    } catch (e) {
      toast($("imp-status"), "Import failed: " + e.message, "err");
    } finally {
      btn.textContent = "Import"; btn.disabled = false;
    }
  });

  // ------------------------------------------------------------- add picture
  // Upload an image next to the current file and insert an RST image directive.
  function openImageDialog() {
    if (!state.repoId) { alert("Clone a repository first."); return; }
    if (!state.currentFile) { alert("Open an .rst file first, then add a picture."); return; }
    $("image-dialog").classList.remove("hidden");
    $("img-file").value = "";
    $("img-alt").value = "";
    $("img-figure").checked = false;
    toast($("img-status"), "", "");
  }
  $("img-cancel").addEventListener("click", () => $("image-dialog").classList.add("hidden"));

  $("img-add").addEventListener("click", async () => {
    const file = $("img-file").files[0];
    if (!file) { toast($("img-status"), "Choose an image file.", "err"); return; }
    const alt = $("img-alt").value.trim();
    const asFigure = $("img-figure").checked;

    const fd = new FormData();
    fd.append("repo_id", state.repoId);
    fd.append("file", file);
    fd.append("rel_path", state.currentFile);

    const btn = $("img-add");
    btn.textContent = "Adding…"; btn.disabled = true;
    try {
      const r = await fetch("/api/image", { method: "POST", body: fd });
      const d = await r.json();
      if (!r.ok) { toast($("img-status"), "Failed: " + (d.error || d.detail), "err"); return; }

      // Build the RST snippet. `ref` is relative to the current .rst file, so
      // the live preview (which resolves against that file) inlines the image.
      const altLine = alt ? "\n   :alt: " + alt : "";
      const snippet = asFigure
        ? "\n.. figure:: " + d.ref + altLine + "\n\n    " + (alt || "caption") + "\n"
        : "\n.. image:: " + d.ref + altLine + "\n";
      replaceSel(snippet);

      toast($("img-status"), "Added " + d.name, "ok");
      $("image-dialog").classList.add("hidden");
      refreshStatus();   // shows the staged image in the changed list
    } catch (e) {
      toast($("img-status"), "Add failed: " + e.message, "err");
    } finally {
      btn.textContent = "Add"; btn.disabled = false;
    }
  });

  // ------------------------------------------------------------- workdir browse
  // The working directory (clone cache) lives on the SERVER filesystem, so a
  // browser <input type=file> can't produce a server path. We browse the
  // server's filesystem via /api/browse-dir (folders only).
  async function browseLoad(dir) {
    if (!dir) dir = $("browse-dir").value.trim() || "~";
    const { ok, d } = await fetchJSON("/api/browse-dir?dir=" + encodeURIComponent(dir));
    if (!ok) { toast($("browse-status"), d.error || "Can't read that folder.", "err"); return; }
    $("browse-dir").value = d.dir;
    const list = $("browse-list");
    list.innerHTML = "";
    const mk = (text, cls, title) => {
      const el = document.createElement("div");
      el.className = "browse-item " + cls;
      el.textContent = text;
      if (title) el.title = title;
      return el;
    };
    // Parent entry (up).
    if (d.parent) {
      const up = mk("..", "dir");
      up.addEventListener("click", () => browseLoad(d.parent));
      list.appendChild(up);
    }
    // Subdirectories.
    d.subdirs.forEach((s) => {
      const el = mk(s + "/", "dir");
      el.addEventListener("click", () => browseLoad(d.dir + "/" + s));
      list.appendChild(el);
    });
    if (!d.subdirs.length) {
      list.appendChild(mk("(no subfolders here)", "empty"));
    }
  }

  function openBrowse(currentDir) {
    $("browse-dialog").classList.remove("hidden");
    $("browse-status").textContent = ""; $("browse-status").className = "status";
    const title = $("browse-dialog").querySelector("h3");
    const pick = $("browse-pick");
    if (title) title.textContent = "Choose working directory";
    if (pick) { pick.textContent = "Use this folder"; pick.dataset.target = "work-root"; }
    browseLoad(currentDir || "~");
  }

  $("btn-browse-workroot").addEventListener("click", () => {
    const p = $("work-root").value.trim();
    openBrowse(p || "~");
  });
  $("browse-up").addEventListener("click", () => {
    const d = $("browse-dir").value.trim();
    const parent = d.slice(0, d.lastIndexOf("/")) || "/";
    browseLoad(parent);
  });
  $("browse-cancel").addEventListener("click", () => $("browse-dialog").classList.add("hidden"));
  // "Use this folder" -> sets the work-root field.
  $("browse-pick").addEventListener("click", () => {
    const d = $("browse-dir").value.trim().replace(/\/+$/, "");
    if (!d) return;
    $("work-root").value = d;
    $("browse-dialog").classList.add("hidden");
  });

  // ------------------------------------------------------------- init
  applyView();
  if (localStorage.getItem("rst_url")) $("repo-url").value = localStorage.getItem("rst_url");
  $("repo-url").addEventListener("change", (e) => localStorage.setItem("rst_url", e.target.value));
  if (localStorage.getItem("rst_workroot")) $("work-root").value = localStorage.getItem("rst_workroot");
  $("work-root").addEventListener("change", (e) => localStorage.setItem("rst_workroot", e.target.value));
  // Restore the autosave preference.
  if (localStorage.getItem("rst_autosave") === "1") {
    state.autosave = true;
    $("autosave").checked = true;
  }

  // Restore the saved configuration from the browser (localStorage) so the
  // form is ready without re-typing. Only fills blank fields.
  loadStoredConfig();
  // Pre-fill the default work dir only if the saved config didn't set one.
  if (!$("work-root").value) {
    fetchJSON("/api/work-root").then(({ d }) => {
      if (d.work_root && !$("work-root").value) $("work-root").value = d.work_root;
    }).catch(() => {});
  }

  // Pre-fill the proxy field from the server's environment (best-effort, only
  // if the user hasn't set one and config didn't provide one). This is what
  // makes the API call work on corporate networks without the proxy address.
  fetchJSON("/api/detect-proxy").then(({ d }) => {
    if (d.proxy && !$("proxy").value) $("proxy").value = d.proxy;
  }).catch(() => {});
})();
