# /x86/ — cross-ISA kernel validation page

Unlike the other `bench-dashboard-*` dirs, this page is **hand-authored HTML**
(`index.html`) rather than rendered from engine `result-*.json` by
`generate_page.py` — its content is a kernel A/B table + x86 end-to-end table +
method notes, not an engine-vs-engine ef_search comparison, so it doesn't fit
the generator's schema. It reuses the shared `<head>`/style block from a
generator-produced sibling page (e.g. `glove/index.html`) for visual
consistency.

Deploy: `sudo cp index.html /data/movie-finder-public/bench-site/x86/index.html`
(served by Caddy at https://bench.myvector.online/x86/).

## Data provenance (all measured this session, 2026-11-06)

- **§1 kernel A/B** (scalar → SSE/AVX2/AVX-512), L2Sqr 784-d, Mdist/s — a
  self-contained microbench of the verbatim kernel loops, compiled at each
  opt level and run **back-to-back on one runner** (valid A/B):
  - AMD EPYC 9V74 (GitHub x86 runner): 1.7 → 16.0 (SSE) → 16.1 (AVX2) = 9.4×
  - Intel Xeon Platinum 8573C (GitHub x86 runner): 2.1 → 8.8 → 8.8 → 10.0 (AVX-512) = 4.2×
  - Ampere Neoverse-N1 (A1 dev host, shipped build): 1.9 → 7.2 (NEON) = 3.8×
- **§2 x86 end-to-end** — optimized post-#278 component built + benchmarked on
  a GitHub x86 runner (AMD EPYC, 4c, MySQL 8.4.13) via `myvectorbench.yml`,
  ANN rewrite active, recall vs exact brute-force:
  - synthetic 10k×128: build 2.1s, insert 6334, ANN 2192 qps @ recall 0.98
  - synthetic 20k×784: build 41.9s, insert 1646, ANN 358 qps @ recall 0.89

## Honesty notes (reflected on the page)

- The OCI tenancy is a post-trial Free-Tier account and **cannot launch an x86
  host** until a console PAYG upgrade; GitHub-hosted runners were the available
  real-x86 silicon.
- A cross-**dispatch** end-to-end A/B on GHA is **invalid** (separate dispatches
  land on different, differently-loaded machines) and was discarded as
  confounded. The end-to-end PR #278 gain is shown instead by the **same-host
  A/B on the A1 dev box** (+9–37%, ef-dependent). Only the §1 kernel A/B is a
  valid same-machine A/B.
- §2 uses synthetic data; the engine-vs-engine pages use real ann-benchmarks
  datasets with published ground truth.
