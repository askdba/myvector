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

| 2026-10-04 | Commands for the user | A ~190-char `cd /abs/path && A=… B=… docker compose …` line, given to the user to paste, was cut at the terminal wrap twice; the partial command ran and was rejected. | Commands for the user to paste: one short line (about 80 characters at most), relative paths, no `cd … &&` prefix or chained inline env vars; otherwise several short commands or a script file run with one short line. |
| 2026-10-04 | Dev host / demos | Told the user to "open http://localhost:8080" for a demo on the remote OCI dev host after binding it to 127.0.0.1; their browser is on their own machine, so it can't reach it. | When a service on the dev host must be viewed, give the SSH tunnel (`ssh -N -L 8080:127.0.0.1:8080 <host>`) or the VS Code port forward. Keep localhost-only binding as the repo default; ask before binding to 0.0.0.0. |
| 2026-09-26 | Build scripts / worktrees | `scripts/build-*-docker.sh` bind-mount the repo as `/workspace` and run git there. In a `git worktree`, `.git` is a file pointing at a host path the container can't see. From that cwd even `git config --global --add safe.directory ...` fails with `fatal: not a git repository: .../.git/worktrees/<name>` (exit 128), and `set -e` aborts the build right after "Cloning MySQL source". | Build from a plain copy (`rsync --exclude .git` plus a `cp -al` of `mysql-server-mysql-<ver>/`, with its `bld-*` removed), not from a worktree. Parallel sessions share `/home/ubuntu/myvector`, so do branch work in a worktree anyway. |
| 2026-03-20 | Super-linter / CI | `github/super-linter@v6` runs **actionlint** + **zizmor** (+ many other linters); zizmor/config drift caused repeated CI failures vs local `actionlint`. | **PR lint** uses the **official actionlint download script** + `./actionlint -color` (no Docker). `actionlint -shellcheck` alone is invalid in v1.7+ (needs a path). Optional: **zizmor** with `.github/linters/zizmor.yaml`. Verify locally before push. |

<!-- Add rows above this line -->
