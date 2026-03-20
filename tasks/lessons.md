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

| 2026-03-20 | Super-linter / CI | `github/super-linter@v6` runs **actionlint** + **zizmor** (+ many other linters); zizmor/config drift caused repeated CI failures vs local `actionlint`. | **PR lint workflow** uses **docker `actionlint` only** on `.github/workflows/*.yml`. Optional: run **zizmor** locally with `.github/linters/zizmor.yaml`. **Do not push** until `docker run … actionlint …` exits 0 locally. |

<!-- Add rows above this line -->
