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

| 2026-03-20 | Super-linter / CI | `github/super-linter@v6` runs **actionlint** + **zizmor** under `GITHUB_ACTIONS`. zizmor is strict (pins, permissions) and can fail in CI while `actionlint` is clean; config path/version drift caused a push/retry loop. | Keep `VALIDATE_GITHUB_ACTIONS_ZIZMOR: false` in `linter.yml` unless you pin workflows and run `zizmor` locally first. Rule files belong under default `LINTER_RULES_PATH` (`.github/linters`). **Stop and re-plan** after a failed lint run—verify with `actionlint` / optional `zizmor` before pushing. |

<!-- Add rows above this line -->
