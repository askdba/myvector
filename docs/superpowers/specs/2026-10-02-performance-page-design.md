# Performance Page Design

## Summary

Add a **Performance** page to the GitHub Pages docs site for people evaluating MyVector.
The numbers live in one curated data file in the repo, `docs/data/performance.json`, which a
person updates by PR after each release. A small MkDocs build hook turns that file into the
page's tables, headline figures and a recall-vs-throughput chart when the site builds.

Layout: three headline figures, one chart (the ef_search curve), and tables for everything
else (layout C of the reviewed mockups). The page includes fine print that explains
ef_search.

## Decisions (agreed in brainstorming, 2026-10-02)

| Question | Decision |
|---|---|
| Audience | People evaluating MyVector, not maintainers tracking regressions |
| Update model | Curated data file in `docs/`, updated by a reviewed PR per release; nothing is published automatically from CI |
| Content | GloVe ef_search sweep; per-release CI results; the 2025 MariaDB comparison; test environment and method |
| Layout | Headline figures + one chart + tables |
| ef_search | Explained in fine print under the chart |
| Existing pages | `ANN_BENCHMARKS.md` and `EF_SEARCH_SWEEP.md` stay as detail pages, nested under Performance in the nav, URLs unchanged |
| Rendering | MkDocs build hook (built into MkDocs, no new dependency) |
| Raw data | `performance.json` is not served on the site |
| Helper script | Yes: `scripts/update-performance-data.py` |

## 1. Data file: `docs/data/performance.json`

One file holds every number on the page.

```json
{
  "sweep": {
    "dataset": "GloVe 6B 50d",
    "indexed_rows": 100000,
    "held_out_queries": 1000,
    "distance": "Cosine",
    "k": 10,
    "build": "plugin, MySQL 8.4",
    "commit": "4b0789f",
    "measured": "2026-09-29",
    "host": "8-core Arm Neoverse-N1 (aarch64), 46 GB RAM",
    "index_build_s": 36.4,
    "brute_force_qps": 12.7,
    "points": [
      {"ef_search": 10,  "recall_at_10": 0.809, "qps": 1204, "p50_ms": 0.8, "p99_ms": 1.0},
      {"ef_search": 20,  "recall_at_10": 0.907, "qps": 1124, "p50_ms": 0.9, "p99_ms": 1.1},
      {"ef_search": 50,  "recall_at_10": 0.974, "qps": 1002, "p50_ms": 1.0, "p99_ms": 1.2},
      {"ef_search": 100, "recall_at_10": 0.992, "qps": 832,  "p50_ms": 1.2, "p99_ms": 1.4},
      {"ef_search": 200, "recall_at_10": 0.999, "qps": 612,  "p50_ms": 1.7, "p99_ms": 1.9},
      {"ef_search": 400, "recall_at_10": 1.000, "qps": 481,  "p50_ms": 2.1, "p99_ms": 2.6}
    ]
  },
  "release": {
    "tag": "v1.26.9",
    "workload": "synthetic, 10,000 rows × 128 dimensions",
    "cells": [
      {"mysql": "8.4",  "build": "plugin",    "index_build_s": 2.95, "insert_qps": 3707, "recall_at_10": 0.978},
      {"mysql": "8.4",  "build": "component", "index_build_s": 2.67, "insert_qps": 3942, "recall_at_10": null},
      {"mysql": "9.7",  "build": "component", "index_build_s": 2.64, "insert_qps": 3916, "recall_at_10": null},
      {"mysql": "26.7", "build": "component", "index_build_s": 2.53, "insert_qps": 4062, "recall_at_10": null}
    ]
  }
}
```

The sweep values are run 1 from `docs/EF_SEARCH_SWEEP.md`. The release values are the v1.26.9
files on the `benchmarks` branch.

- **Headline figures are derived by the hook, never typed in:**
  - recall@10 at ef_search 100 (the hook fails the build if there is no ef_search 100 point);
  - ANN speed-up = QPS at the highest ef_search ÷ `brute_force_qps`, shown rounded as `~N×`;
  - `index_build_s`, shown with the row count.
- **`null` recall** renders as "—" with a footnote: the component ANN rewrite isn't in that
  release (it arrived in #156).
- **Not in the file:**
  - the MariaDB comparison, which stays only in `ANN_BENCHMARKS.md`;
  - search QPS for release cells: v1.26.9 search QPS is dominated by Docker overhead (#124).
    A column can be added once a release has post-#124 numbers.

## 2. Page: `docs/PERFORMANCE.md`

Hand-written Markdown with markers that the hook replaces. From top to bottom:

1. **Title and one-line intro:** what was measured and when, using `measured` and `commit` from
   the data.
2. **`<!-- perf:headline -->`:** three headline figures.
3. **"Recall vs throughput":**
   - `<!-- perf:sweep-chart -->`: the SVG curve;
   - `<!-- perf:sweep-table -->`: ef_search, recall@10, QPS, p50 ms, p99 ms.
4. **Fine print under the chart:**
   > *ef_search* is how many candidate neighbours the HNSW search keeps while it walks the
   > index graph. Higher values find more of the true nearest neighbours (higher recall) but
   > take longer per query. Set it per query with
   > `MYVECTOR_IS_ANN(index, key, vector, 'nn=10,ef_search=N')`; queries without it use the
   > index's own setting. QPS here is measured through the `mysql` client in Docker, one query
   > at a time, so treat it as relative between ef_search values (#133).
5. **Release results:** `<!-- perf:release-table -->` (MySQL / build, index build, insert QPS,
   recall@10) under a heading that names the tag and workload, plus the "—" footnote.
6. **Collapsed `??? info "Comparison with MariaDB (2025)"`:** two sentences (one-off, pre-1.0,
   different datasets and hardware, not comparable with the numbers above) and a link to
   `ANN_BENCHMARKS.md`.
7. **Collapsed `??? info "Test environment and method"`:**
   - host, dataset, held-out queries and parameters;
   - the reproduce command;
   - links to `EF_SEARCH_SWEEP.md` and the `myvectorbench` workflow.

**Nav (`mkdocs.yml`):** under Docs, replace `Benchmarks: ANN_BENCHMARKS.md` and
`ef_search Sweep: EF_SEARCH_SWEEP.md` with:

```yaml
- Performance:
    - Overview: PERFORMANCE.md
    - ef_search Sweep: EF_SEARCH_SWEEP.md
    - MariaDB Comparison (2025): ANN_BENCHMARKS.md
```

The file names and URLs of the existing pages don't change.

## 3. Build hook: `docs/hooks/performance.py`

- **Registration:**
  - `hooks: [docs/hooks/performance.py]` in `mkdocs.yml`;
  - `docs/hooks/` and `docs/data/` added to `exclude_docs`, so neither is published.
- **`on_page_markdown`** runs only for `PERFORMANCE.md`. It loads and validates the JSON, then
  replaces each marker. Other pages pass through unchanged.
- **Rendering:**
  - **tables:** Markdown, with numbers formatted with thousands separators;
  - **headline:** an HTML grid (`md_in_html`), styled in `docs/stylesheets/extra.css` with the
    site's existing colours;
  - **chart:** an inline SVG drawn to scale.
    - Axes: x = QPS, y = recall@10. Tick values are computed from the data range and every
      tick label names a value on the axis.
    - Each point is labelled with its ef_search.
    - Colours are site CSS variables, so the chart matches the theme.
    - No JavaScript, no library.
- **Errors (fail the build, so `mkdocs build --strict` and `deploy-docs.yml` stop):**
  - a missing file or invalid JSON;
  - a missing required field, or a wrong type;
  - no ef_search 100 point (needed for the headline);
  - an unknown marker, or a marker still present after replacement.

  Each error message names the file and the field.

## 4. Helper: `scripts/update-performance-data.py`

- **`--release <tag>`:** reads `<mysql>/<build>/<tag>-*.json` from the `benchmarks` branch
  (`git show origin/benchmarks:…`) for the four cells, and rewrites the `release` block. If one
  tag has several files for a cell, it uses the newest by timestamp in the file name.
- **`--sweep <result.json>`:** replaces the `sweep` block from a `myvectorbench` GloVe result
  (the `ef_search_sweep`, `index_build_time_s`, `knn_qps` and `workload_params` fields).
  Descriptive fields (`build`, `host`) are kept from the existing file unless they are passed
  as options.
- It only writes `performance.json`. The PR diff is the review step.

## 5. Testing

- **`tests/test_performance_hook.py`** (pytest, same loading style as the existing bench
  tests), written before the hook:
  - markers become tables with the right values and formatting;
  - the headline figures are derived correctly (0.992; ~38×; 36.4 s for the current data);
  - null recall renders as "—";
  - chart points land at the coordinates the scale gives, and tick labels lie on the axis;
  - pages other than `PERFORMANCE.md` are unchanged;
  - each error case fails with a message that names the field.
- **`tests/test_update_performance_data.py`:** builds the release block from sample
  `benchmarks`-branch JSON (including picking the newest file), and builds the sweep block from
  a sample result.
- **Integration:**
  - `mkdocs build --strict` passes;
  - the built `PERFORMANCE/index.html` contains the values, the SVG and the fine print;
  - the nav shows the three nested entries, and the old page URLs still exist;
  - `hooks/` and `data/` are absent from `site/`.
- **Visual:** one look at the built page, shared as a private preview page.
- **Lint:** markdownlint (no new errors compared with `main`) and actionlint.

## Out of scope / follow-ups

- Running `tests/*.py` in CI (no workflow runs them today).
- A search-QPS column in the release table, once a release has post-#124 numbers.
- Automating the update PR from `myvectorbench.yml`.
- Release-over-release trend charts for maintainers.
