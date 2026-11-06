"""Unit test for the idle work-dir reaper (git_ops.touch_activity /
reap_idle_work_dirs / prune_repos). Hermetic: uses a temp work root and a
short TTL, so it runs in well under a second.
"""
import os
import sys
import time
import shutil
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import git_ops

ok = True

def check(name, cond, extra=""):
    global ok
    print(("PASS" if cond else "FAIL"), name, ("" if cond else str(extra)[:200]))
    if not cond:
        ok = False

root = tempfile.mkdtemp(prefix="rst_reaper_")
ttl = 30  # seconds — but we simulate age with mtime, not real waiting

# --- Set up three work dirs under root ---
active = os.path.join(root, "aaa111")
idle = os.path.join(root, "bbb222")
unmarked = os.path.join(root, "ccc333")
for d in (active, idle, unmarked):
    os.makedirs(d)
    with open(os.path.join(d, "index.rst"), "w") as f:
        f.write("x\n")

# Touch the 'active' dir now (fresh marker).
git_ops.touch_activity(active, root)
# 'idle' dir: give it an old marker (older than ttl).
git_ops.touch_activity(idle, root)
old = time.time() - (ttl + 120)
idle_marker = git_ops._activity_path(idle, root)
os.utime(idle_marker, (old, old))
# 'unmarked' dir: no marker at all (a user dir / abandoned-without-marker).
check("unmarked has no marker", not os.path.isfile(git_ops._activity_path(unmarked, root)))

# 1) prune_repos identifies only the idle one among [active, idle]
stale = git_ops.prune_repos([active, idle, unmarked], root, ttl=ttl)
check("prune_repos flags only idle", stale == [idle], stale)
check("prune_repos does not delete", os.path.isdir(idle) and os.path.isdir(active))

# 2) reap_idle_work_dirs deletes the idle one, keeps active + unmarked
removed = git_ops.reap_idle_work_dirs(root, ttl=ttl)
check("reap removed exactly the idle dir", removed == [idle], removed)
check("active dir kept", os.path.isdir(active))
check("unmarked dir kept (no marker -> never touched)", os.path.isdir(unmarked))
check("idle dir gone", not os.path.isdir(idle))
check("idle marker cleaned up", not os.path.isfile(git_ops._activity_path(idle, root)))

# 3) markers live OUTSIDE the repo (not visible to git / file lists)
check("no marker file inside a work dir",
      not any(f == ".rst_workdir_activity" or f.endswith(".act")
              for f in os.listdir(active)))
check("meta dir is hidden under root", os.path.isdir(os.path.join(root, ".rst_workdir_meta")))

# 4) a fresh dir is never reaped (just-touched, ttl far in the future)
fresh = os.path.join(root, "ddd444")
os.makedirs(fresh)
git_ops.touch_activity(fresh, root)
removed2 = git_ops.reap_idle_work_dirs(root, ttl=ttl)
check("fresh dir not reaped", fresh not in removed2 and os.path.isdir(fresh))

# 5) root itself is never deleted, and reap is idempotent
check("root still present", os.path.isdir(root))
check("reap is idempotent (nothing left to remove)", git_ops.reap_idle_work_dirs(root, ttl=ttl) == [])

shutil.rmtree(root, ignore_errors=True)

print()
print("=== REAPER UNIT TEST:", "ALL PASS" if ok else "FAILURES ABOVE", "===")
sys.exit(0 if ok else 1)
