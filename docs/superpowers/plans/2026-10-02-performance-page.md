# Performance Page Implementation Plan

> Steps use checkbox (`- [ ]`) syntax for tracking. Work in the worktree
> `/home/ubuntu/myvector-wt-perf` (branch `feat/performance-page`), never the shared checkout.

**Goal:** add a Performance page to the docs site that renders benchmark numbers from a
curated `docs/data/performance.json`, as specified in
`docs/superpowers/specs/2026-10-02-performance-page-design.md`.

**Architecture:**

- A MkDocs hook (`docs/hooks/performance.py`) handles `on_page_markdown` for
  `PERFORMANCE.md` only. It validates the JSON and replaces four markers with Markdown tables,
  an HTML headline grid and an inline SVG chart.
- A helper script, `scripts/update-performance-data.py`, refreshes the JSON from the
  `benchmarks` branch or from a `myvectorbench` result.

**Tech stack:** Python 3 (standard library only), MkDocs Material (already used by
`deploy-docs.yml`), pytest (local only).

---

## File map

| File | Change |
|---|---|
| `docs/data/performance.json` | New: the curated numbers (spec §1) |
| `docs/hooks/performance.py` | New: validation and rendering, `on_page_markdown` |
| `docs/PERFORMANCE.md` | New: page text and the four markers (spec §2) |
| `docs/stylesheets/extra.css` | Add styles for the headline grid, the chart and the fine print |
| `mkdocs.yml` | `hooks:`, `exclude_docs` (`hooks/`, `data/`), nested Performance nav |
| `scripts/update-performance-data.py` | New: `--release`, `--sweep` (spec §4) |
| `tests/test_performance_hook.py` | New |
| `tests/test_update_performance_data.py` | New |

The hook's public functions, which the tests call directly:

- `load_data(path) -> dict`: parses and validates the file. Raises `PerformanceDataError`
  (a subclass of `mkdocs.exceptions.PluginError`, or a plain `Exception` if mkdocs isn't
  importable) whose message names the file and the field.
- `render_headline(data) -> str`
- `render_sweep_table(data) -> str`
- `render_release_table(data) -> str`
- `render_sweep_chart(data) -> str`, plus `chart_scale(points)`, which returns the axis ranges
  and tick values so the tests can check point placement.
- `render_page(markdown, data) -> str`: replaces the markers. It fails on an unknown
  `<!-- perf:… -->` marker, and fails if any marker remains afterwards.
- `on_page_markdown(markdown, page, config, files)`: calls `render_page` only when
  `page.file.src_uri == "PERFORMANCE.md"`. It reads `docs_dir/data/performance.json`.

---

### Task 1: Data file and validation

- [ ] Write `tests/test_performance_hook.py` with a `sample_data()` fixture equal to the spec
      §1 JSON, plus tests:
  - the valid sample loads;
  - invalid JSON, a missing file, a missing `sweep.points`, a non-numeric `qps`, and a
    `release.cells` entry missing `insert_qps` each raise `PerformanceDataError`, and the
    message names the field;
  - `recall_at_10: null` is accepted in release cells but **not** in sweep points.
- [ ] Run the tests and confirm they fail (no module yet).
- [ ] Create `docs/data/performance.json` (spec §1 values) and `load_data` in the hook.
- [ ] Run the tests until they pass.

### Task 2: Tables

- [ ] Tests:
  - the sweep table has the header `ef_search | recall@10 | QPS | p50 ms | p99 ms` and the row
    `| 10 | 0.809 | 1,204 | 0.8 | 1.0 |`;
  - recall is always 3 decimals, QPS uses thousands separators, latency 1 decimal;
  - the release table renders `8.4 plugin` / `2.95 s` / `3,707` / `0.978`, and `—` for null
    recall.
- [ ] Implement `render_sweep_table` and `render_release_table`.

### Task 3: Headline figures

- [ ] Tests:
  - the output contains `0.992` with "recall@10 at ef_search 100", `~38×` (481 / 12.7), and
    `36.4 s` with "100,000 rows";
  - no ef_search 100 point raises `PerformanceDataError`.
- [ ] Implement `render_headline` as `<div class="perf-headline" markdown>` with three
      `perf-figure` items (value + label).

### Task 4: Chart

- [ ] Tests:
  - `chart_scale` gives x ticks that bracket the min and max QPS, with "nice" steps
    (1/2/5 × 10ⁿ), and y ticks covering the recall range rounded down to a 0.05 step, up to
    1.00;
  - each `<circle>` sits at the coordinates the scale predicts (±0.5 px);
  - every tick label value lies inside the axis range;
  - every point has a label `ef N`;
  - the SVG has `role="img"`, an `aria-label`, and a `viewBox` with room for the outermost
    labels;
  - the SVG has no `<script>`.
- [ ] Implement `chart_scale` and `render_sweep_chart`. Colours come from CSS classes
      (`perf-line`, `perf-point`, `perf-grid`, `perf-tick`), styled in `extra.css` using the
      site palette (cyan `#19e2dc` line, `#2a3547` grid, muted `#9aa7b5` text).

### Task 5: Markers and the hook entry point

- [ ] Tests:
  - `render_page` replaces all four markers;
  - an unknown `<!-- perf:nope -->` raises;
  - a page without markers comes back unchanged;
  - `on_page_markdown` leaves other pages untouched (fake `page` and `config` objects) and
    reads the data from `docs_dir`.
- [ ] Implement `render_page` and `on_page_markdown`.

### Task 6: Page, styles and site config

- [ ] Write `docs/PERFORMANCE.md` (spec §2):
  - intro;
  - the markers;
  - the ef_search fine print, word for word from the spec, in
    `<p class="perf-fineprint" markdown>`;
  - the release footnote;
  - the two `??? info` collapsed blocks with links to `ANN_BENCHMARKS.md`,
    `EF_SEARCH_SWEEP.md` and the workflow.
- [ ] `extra.css`: styles for the headline grid (three columns, stacked below 600 px), the
      chart classes and the fine print (smaller, muted).
- [ ] `mkdocs.yml`:
  - `hooks: [docs/hooks/performance.py]`;
  - add `hooks/` and `data/` to `exclude_docs`;
  - replace the Benchmarks and ef_search Sweep nav entries with the nested Performance block
    (spec §2).
- [ ] Run `mkdocs build --strict` and check that it passes.

### Task 7: Helper script

- [ ] `tests/test_update_performance_data.py`:
  - fake `git show` and `git ls-tree` output (inject a reader function) holding two v1.26.9
    files for one cell → the newest timestamp wins;
  - the release block matches the spec's cells;
  - a `--sweep` sample result produces the spec's sweep block, keeping `build` and `host` from
    the existing file;
  - an unknown tag → a clear error and a non-zero exit.
- [ ] Implement it with the standard library only (`subprocess` for `git`). It writes the JSON
      with 2-space indentation and a trailing newline, and makes no other changes.
- [ ] Run it for real: `--release v1.26.9` against `origin/benchmarks`. It must reproduce the
      committed JSON exactly (`git diff --exit-code docs/data/performance.json`).

### Task 8: Verify

- [ ] `pytest tests/`: all pass, including the 52 existing tests.
- [ ] `mkdocs build --strict -d <tmp>`, then check:
  - `PERFORMANCE/index.html` contains `0.992`, `~38×`, `<svg`, "ef_search is how many";
  - `ANN_BENCHMARKS/index.html` and `EF_SEARCH_SWEEP/index.html` still exist;
  - no `hooks/` or `data/` directory under the site output;
  - the nav HTML shows Performance with three children.
- [ ] markdownlint on the new and changed `.md` files: no new errors compared with `main`.
- [ ] actionlint: clean (no workflow changes expected).
- [ ] Visual check: publish the built `PERFORMANCE/index.html` content as a private preview
      page for the user, and look once at the chart's labels and spacing.

### Task 9: Ship (the user's PR workflow)

- [ ] Commit in logical steps (data + hook + tests, page + config, helper).
- [ ] Push, open the PR with the spec link, the verification evidence and the preview link.
- [ ] Autopilot: watch CI, check each review claim against the code before changing anything,
      and reply on every thread with evidence.
- [ ] Critical self-review, then ask before merging. After the merge: worktree, branch, and a
      check that `deploy-docs.yml` succeeded and the live page renders.
