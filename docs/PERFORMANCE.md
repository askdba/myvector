# Performance

How fast MyVector's HNSW search is, and how much accuracy you trade for speed.

<!-- perf:sweep-summary -->

All numbers on this page come from
[`docs/data/performance.json`](https://github.com/askdba/myvector/blob/main/docs/data/performance.json).

<!-- perf:headline -->

## Recall vs throughput

Each point is one `ef_search` setting: the lowest is the fastest, the highest the most
accurate. Recall@10 is the share of the true 10 nearest neighbours that the search returns.

<!-- perf:sweep-chart -->

<!-- perf:sweep-table -->

<p class="perf-fineprint" markdown>
*ef_search* is how many candidate neighbours the HNSW search keeps while it walks the index
graph. Higher values find more of the true nearest neighbours (higher recall) but take longer
per query. Set it per query with `MYVECTOR_IS_ANN(index, key, vector, 'nn=10,ef_search=N')`;
queries without it use the index's own setting. QPS here is measured through the `mysql`
client in Docker, one query at a time, so treat it as relative between ef_search values
([#133](https://github.com/askdba/myvector/issues/133)).
</p>

## Latest release

<!-- perf:release-summary -->

<!-- perf:release-table -->

<p class="perf-fineprint" markdown>
"—" means no recall was measured: component builds from v1.26.9 and earlier don't have the
query rewrite that `MYVECTOR_IS_ANN` needs ([#156](https://github.com/askdba/myvector/pull/156)).
Search QPS from these CI runs isn't shown, because up to v1.26.9 it mostly measured Docker
overhead ([#124](https://github.com/askdba/myvector/issues/124)).
</p>

??? info "Comparison with MariaDB (2025)"

    A one-off comparison with MariaDB on the ann-benchmarks `gist-960-euclidean` and
    `dbpedia-openai-1000k-angular` datasets, run in February 2025 on a pre-1.0 build. It used
    different data, hardware and settings, so its numbers can't be compared with the ones
    above. See [MariaDB Comparison (2025)](ANN_BENCHMARKS.md).

??? info "Test environment and method"

    - **Queries:** sent one at a time. They are held out from the dataset by a seeded
      shuffle and never inserted, so no query finds itself.
    - **Index:** HNSW, M = 16, ef_construction = 200. Recall is measured
      against an exact brute-force top 10.
    - **Release results:** the `myvectorbench` workflow on each `v*` tag, stored on the
      [`benchmarks` branch](https://github.com/askdba/myvector/tree/benchmarks).

    To reproduce the sweep:

    ```bash
    python3 scripts/myvectorbench.py --config myvectorbench-glove.yml \
        --mysql-version 8.4 --build-path plugin --artifact-dir dist/plugin-8.4 \
        --image ghcr.io/askdba/myvector:mysql8.4 --output glove-sweep.json
    ```

    Full write-up, including both runs and their latency: [ef_search Sweep](EF_SEARCH_SWEEP.md).
