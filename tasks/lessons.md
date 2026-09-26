# Lessons learned

After user corrections, failed CI, or repeated mistakes, add a short entry here.
**Review at session start** when working on this repository.

Newest entries first.

---

## Template

| Date | Area | Pattern | Rule to apply |
| :--- | :--- | :--- | :--- |
| YYYY-MM-DD | e.g. lint | e.g. prefer full word `repository` in docs | Run terminology check on new docs |

---

## Log

| 2026-09-26 | Build scripts / worktrees | `scripts/build-*-docker.sh` bind-mount the repo as `/workspace` and run git there. In a `git worktree`, `.git` is a file pointing at a host path the container can't see. From that cwd even `git config --global --add safe.directory ...` fails with `fatal: not a git repository: .../.git/worktrees/<name>` (exit 128), and `set -e` aborts the build right after "Cloning MySQL source". | Build from a plain copy (`rsync --exclude .git` plus a `cp -al` of `mysql-server-mysql-<ver>/`, with its `bld-*` removed), not from a worktree. Parallel sessions share `/home/ubuntu/myvector`, so do branch work in a worktree anyway. |
| 2026-03-20 | Super-linter / CI | `github/super-linter@v6` runs **actionlint** + **zizmor** (+ many other linters); zizmor/config drift caused repeated CI failures vs local `actionlint`. | **PR lint** uses the **official actionlint download script** + `./actionlint -color` (no Docker). `actionlint -shellcheck` alone is invalid in v1.7+ (needs a path). Optional: **zizmor** with `.github/linters/zizmor.yaml`. Verify locally before push. |

<!-- Add rows above this line -->
