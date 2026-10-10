# Benchmark dashboard (bench.myvector.online)

Tooling behind the public cross-version benchmark dashboard: MyVector's native
HNSW vector search on **MySQL 8.4 / 9.7 / 26.7**, plus **MariaDB 11.8** native
`VECTOR`/HNSW as a cross-engine reference. All four run the **same**
seed-deterministic synthetic workload (50 000 × 128-d, L2, HNSW M=16) so the
numbers line up.

## Files

| File | Role |
|---|---|
| `bench_run.py` | Run the MyVector workload against one running MySQL container and emit a result JSON. Reuses `scripts/myvectorbench.py` workload functions via a no-op `Container` subclass (local `docker exec`). |
| `bench_mariadb.py` | Same workload against MariaDB native vector (`VECTOR(n)` + `VECTOR INDEX`, `VEC_FromText`, `VEC_DISTANCE_EUCLIDEAN`, session `mhnsw_ef_search`). |
| `redeploy.py` | Recreate one bench container from a **base** `mysql:X` image and install a freshly-built MyVector component (clean datadir, full install). |
| `generate_page.py` | Render `results/bench-dashboard/*.json` into a standalone `index.html` (static table + cards always visible; Chart.js charts as a guarded enhancement). |

Result JSONs live in [`results/bench-dashboard/`](../../results/bench-dashboard/).

## Reproduce

1. **Build fresh component artifacts** (the published GHCR `-component` images
   predate the component query-rewrite fix, so `MYVECTOR_IS_ANN` / ANN search is
   inactive on them — rebuild from source to get it):
   ```bash
   scripts/build-component-8.4-docker.sh  mysql-8.4.8  dist/component-8.4
   scripts/build-component-9.7-docker.sh  mysql-9.7.0  dist/component-9.7
   scripts/build-component-26.7-docker.sh mysql-26.7.0 dist/component-26.7
   ```

2. **Deploy the four pinned containers** (identical 4 vCPU / 24 GB slices):
   ```bash
   python3 scripts/bench-dashboard/redeploy.py --container bench-mysql97 --version 9.7 \
     --base-image mysql:9.7 --comp-dir dist/component-9.7 \
     --port 3309 --cpuset 0-3 --mem 24g --datadir /data/mysql/bench/97 --root-pw "$PW"
   # ...likewise 8.4 (port 3306) and 26.7 (port 3310, cpuset 4-7)
   docker run -d --name bench-mariadb --cpuset-cpus 8-11 --memory 24g --memory-swap 24g \
     -p 127.0.0.1:3311:3306 -e MARIADB_ROOT_PASSWORD="$PW" -e MARIADB_DATABASE=bench \
     -v /data/mysql/bench/mariadb:/var/lib/mysql mariadb:11.8
   ```

3. **Run the benchmarks:**
   ```bash
   python3 scripts/bench-dashboard/bench_run.py --container bench-mysql97 --version 9.7 \
     --root-pw "$PW" --out results/bench-dashboard/result-9.7.json
   python3 scripts/bench-dashboard/bench_mariadb.py --root-pw "$PW" \
     --out results/bench-dashboard/result-mariadb.json
   ```

4. **Generate the page** (self-host Chart.js next to it — the cdnjs path 404s):
   ```bash
   python3 scripts/bench-dashboard/generate_page.py results/bench-dashboard site/index.html
   curl -fsSL https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js \
     -o site/chart.umd.min.js
   ```
   Served publicly via a Caddy `reverse_proxy` block to a `caddy:2 file-server`
   container (`bench.myvector.online`, DNS A → the demo host).

## Caveats

- **Cross-engine, not cross-build:** MariaDB 11.8 uses its own native vector
  index — a different implementation from MyVector-on-MySQL. MariaDB builds the
  HNSW index **online during insert** (no separate build phase or
  `ef_construction`), so its "build time" is load time.
- **Hardware:** 8.4 runs on Ampere A2; 9.7, 26.7 and MariaDB on Ampere A1. The
  A1 instances are directly comparable; 8.4's absolute numbers are on newer-gen
  silicon. Recorded per-instance in each result JSON.
- Recall is honest (held-out queries never inserted); ground truth is
  brute-force top-10 on an unindexed copy.
