# Benchmark report - v1.26.9 baselines

**Source:** CI run [35611446916](https://github.com/askdba/myvector/actions/runs/35611446916), `main` at `d597399`.
This is the first run with the fixed benchmark harness (#123) and the HNSW fixes (#117).
**Workload:** synthetic data, 10,000 rows, dim 128, M=16, ef_construction=200, 200 KNN queries,
50 recall queries. GitHub `ubuntu` runners, MySQL 8.4 / 9.7 / 26.7.

## Results (now the CI baselines on the `benchmarks` branch)

| Cell | Index build | Insert QPS | KNN QPS | ANN QPS | recall@10 |
|---|---|---|---|---|---|
| plugin 8.4 | 2.75 s | 3,840 | 13.0 | 14.0 | **0.978** |
| component 8.4 | 2.79 s | 3,812 | 5.9 | n/a | n/a |
| component 9.7 | 2.67 s | 3,844 | 5.7 | n/a | n/a |
| component 26.7 | 2.36 s | 4,397 | 6.4 | n/a | n/a |

On the component cells ANN is inactive, because the query-rewrite service is not available there,
so there is no ANN or recall number for them.

## Change from rc1 (same runners, KNN-based)

| Cell | Metric | rc1 | Now |
|---|---|---|---|
| plugin 8.4 | index build | 0.40 s | 2.75 s |
| plugin 8.4 | recall@10 | 0.0 | **0.978** |
| plugin 8.4 | insert QPS | 2,644 | 3,840 |
| plugin 8.4 | KNN QPS | 18.9 | 13.0 |
| component 8.4 | KNN QPS | 9 | 5.9 |
| component 9.7 | KNN QPS | 8 | 5.7 |
| component 26.7 | KNN QPS | 6 | 6.4 |

## What this shows

1. **The plugin now builds a real HNSW index.** Build time went from 0.4 s to 2.75 s and recall from
   0.0 to 0.978. The same 0.978 was reproduced on a locally built aarch64 plugin (10k x 128).
2. **Every earlier plugin number measured an empty index**, including the v1.26.5.2 baseline and rc1.
   The plugin never wrote `myvector.cnf`, so the index build failed silently. Those numbers are not
   comparable with these.
3. **The component build times are now real too.** Under rc1's silent KNN fallback they were meaningless.
4. **The KNN QPS differences are within noise for this harness.** KNN is brute force and untouched by
   these changes, and the rc1 comparison mixes different runner instances.

## Limits: how to read the numbers

- **QPS and latency are weak signals.** A trivial `SELECT 1` through `docker exec mysql` costs about
  67 ms here (p50, n=30), so KNN and ANN QPS are mostly process spawn. That is why ANN (14) looks no
  faster than KNN (13) even with a real index (#124).
- **Meaningful metrics:** recall, index build time and batched insert throughput.
- **Comparisons:** use only same-runner-type results. The v1.26.5.2 baselines came from local Docker
  and are not comparable.
- **rc2:** the tag-triggered `myvectorbench` run is the first real comparison against these baselines.
