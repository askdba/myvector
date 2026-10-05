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
client in Docker, one query at a time, so treat it as relative between ef_search values.
For absolute numbers, run the benchmark against a host `mysqld` (below).
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

??? info "Run the benchmark on a host mysqld"

    By default `myvectorbench.py` runs MySQL in Docker. With `--server host` it downloads
    Oracle's generic Linux tarball for the exact patch release the MyVector build targets
    (8.4.8, 9.7.0, 26.7.0), starts an isolated `mysqld` from it (its own datadir, port and
    socket, listening on 127.0.0.1 only) and times each query over one persistent
    connection, measured on the client, so the numbers include the whole round trip and
    nothing else: a `SELECT 1` costs about 0.1 ms. Each result records the machine (CPU, cores, memory,
    kernel) and the round trip as `select1_p50_ms`.

    Host mode needs the MySQL Python driver (`pip install mysql-connector-python`; Docker
    mode doesn't). The harness checks for it before downloading anything.

    One command runs every cell in `myvectorbench.yml` (plugin 8.4, components 8.4, 9.7
    and 26.7). Put the tarball cache and the run directories on a data volume, as they
    take several GB:

    ```bash
    python3 scripts/myvectorbench.py --server host --all-cells \
        --artifact-root dist --cache-dir /data/mysql/tarballs \
        --workdir-root /data/mysql/bench --output-dir /data/build/bench-host
    ```

    - **Artifacts:** `dist/component-<ver>/` from `scripts/build-component-<ver>-docker.sh`.
      For the plugin use `scripts/build-plugin-8.4-docker.sh mysql-8.4.8 dist/plugin-8.4-ol9`,
      built with the same Oracle Linux 9 toolchain as Oracle's binaries, so it loads into
      the tarball `mysqld`; `--all-cells` prefers `plugin-8.4-ol9/` over `plugin-8.4/`.
    - **Results** go under `<output-dir>/<machine-key>/` (for example
      `aarch64-neoverse-n1-16c/`), so baselines from different machine types stay apart.
      `myvectorbench-compare.py` warns when the baseline comes from another machine type
      or from a Docker run.
    - **Single cell:** `--server host --mysql-version 9.7 --build-path component
      --artifact-dir dist/component-9.7`. `--mysql-basedir` uses an already extracted or
      source-built server instead of downloading; `--keep-workdir` keeps the datadir and
      error log.
    - **Ubuntu 24.04** only has `libaio.so.1t64`; the harness links it as `libaio.so.1` in
      the run directory for `mysqld`, without touching the system.
    - CI still uses Docker; GitHub runners are shared hardware, so their numbers are only
      relative.
