# ef_search Sweep - GloVe 50d

How recall and throughput trade off as you raise `ef_search`, measured on a real dataset
with held-out queries. This answers [#131](https://github.com/askdba/myvector/issues/131).

> **QPS here is provisional.** Queries go through the `mysql` client inside a Docker container,
> one at a time, so QPS and latency include client and container overhead, not only the search.
> Treat them as relative numbers (one `ef_search` against another) until
> [#133](https://github.com/askdba/myvector/issues/133) (benchmarks on a real MySQL server) lands.
> **Recall does not have this problem.**

## Results

GloVe 6B 50d, 100,000 indexed words, 1,000 held-out queries, Cosine distance, k = 10.
Two runs, back to back. Recall was identical in both runs, to four decimals.

| ef_search | recall@10 | QPS (run 1 / run 2) | p50 ms | p99 ms |
|-----------|-----------|---------------------|--------|--------|
| 10        | 0.809     | 1,204 / 1,237       | 0.8    | 1.0    |
| 20        | 0.907     | 1,124 / 1,123       | 0.9    | 1.1    |
| 50        | 0.974     | 1,002 / 983         | 1.0    | 1.2    |
| 100       | 0.992     | 832 / 787           | 1.2    | 1.4    |
| 200       | 0.999     | 612 / 607           | 1.7    | 1.9    |
| 400       | 1.000     | 481 / 434           | 2.1    | 2.6    |

Latency is from run 1; run 2 was within 0.25 ms at every point.

For comparison, on the same index and queries:

| Metric                                             | Run 1  | Run 2  |
|----------------------------------------------------|--------|--------|
| Index build (100k rows)                            | 36.4 s | 36.9 s |
| Brute-force KNN QPS (`ORDER BY myvector_distance`) | 12.7   | 12.9   |

**Reading it:** ef_search 50 already finds 97% of the true top 10. ef_search 100-200 reaches
99%+ at about half to two-thirds of the ef_search 10 throughput. Past 200 you pay more
throughput for almost no extra recall. Even at ef_search 400, ANN runs about 38x faster than
brute force here.

## Method

| Item         | Setting                                                                                                  |
|--------------|----------------------------------------------------------------------------------------------------------|
| Dataset      | [GloVe 6B](https://nlp.stanford.edu/projects/glove/) 50d, the first 101,000 words of `glove.6B.50d.txt`  |
| Queries      | 1,000 of those words, chosen by a seeded shuffle and **never inserted**, so no query finds itself        |
| Ground truth | Exact top 10 by `myvector_distance(..., 'Cosine')` over all 100,000 indexed rows                         |
| Index        | HNSW, `dist=Cosine`, M = 16, ef_construction = 200                                                       |
| Search       | `MYVECTOR_IS_ANN(..., 'nn=10,ef_search=N')`, same 1,000 queries at every point; per query since #167     |
| Build        | MyVector plugin for MySQL 8.4 (`ghcr.io/askdba/myvector:mysql8.4` image), built from `main` at `4b0789f` |
| Host         | 8-core Arm Neoverse-N1 (aarch64), 46 GB RAM, one query at a time                                         |
| Date         | 2026-09-29                                                                                               |

To reproduce:

```bash
python3 scripts/myvectorbench.py --config myvectorbench-glove.yml \
    --mysql-version 8.4 --build-path plugin --artifact-dir dist/plugin-8.4 \
    --image ghcr.io/askdba/myvector:mysql8.4 --output glove-sweep.json
```

The first run downloads GloVe 6B (about 860 MB) to `~/.cache/myvectorbench/`.

## Limits

- **Measured on the plugin only.** Component builds have had `MYVECTOR_IS_ANN` since
  [#156](https://github.com/askdba/myvector/pull/156) (not in v1.26.9 or earlier releases),
  but they aren't measured here.
- **Not comparable with ann-benchmarks' `glove-100-angular`.** That set is 1.2M Twitter GloVe
  vectors at 100 dimensions. This one is 100k GloVe 6B (Wikipedia + Gigaword) vectors at 50 dimensions, which
  is generally an easier search problem.
- **Not comparable with the CI baselines** in `release/BENCHMARK_*.md` either. Those use
  synthetic vectors and don't hold out their queries.
- **One host, one client thread.** No concurrency and no other load.
