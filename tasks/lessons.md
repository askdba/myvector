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

| 2026-03-20 | Super-linter / CI | `github/super-linter@v6` runs **actionlint** + **zizmor** (+ many other linters); zizmor/config drift caused repeated CI failures vs local `actionlint`. | **PR lint** uses the **official actionlint download script** + `./actionlint -color` (no Docker). `actionlint -shellcheck` alone is invalid in v1.7+ (needs a path). Optional: **zizmor** with `.github/linters/zizmor.yaml`. Verify locally before push. |

<!-- Add rows above this line -->
