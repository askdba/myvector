# Benchmark dashboard (bench.myvector.online)

Tooling behind the public cross-version benchmark dashboard: MyVector's native
HNSW vector search on **MySQL 8.4 / 9.7 / 26.7**, plus **MariaDB 12.3** (current LTS) native
`VECTOR`/HNSW as a cross-engine reference. All four run the **same**
seed-deterministic synthetic workload (50 000 × 128-d, L2, HNSW M=16) so the
numbers line up.

## Files

| File | Role |
|---|---|
| `bench_run.py` | Run the MyVector workload against one running MySQL container and emit a result JSON. Reuses `scripts/myvectorbench.py` workload functions via a no-op `Container` subclass (local `docker exec`). |
| `bench_mariadb.py` | Same workload against MariaDB native vector (`VECTOR(n)` + `VECTOR INDEX`, `VEC_FromText`, `VEC_DISTANCE_EUCLIDEAN`, session `mhnsw_ef_search`). |
| `bench_real.py` | **Real datasets** (myvector#239). Run an ann-benchmarks HDF5 dataset against one MyVector container, scoring recall@10 against the dataset's **published** exact ground truth (not a brute-force self-check). Needs `numpy` + `h5py`. |
| `bench_mariadb_real.py` | The MariaDB counterpart of `bench_real.py` (same published-ground-truth recall, MariaDB native vector). |
| `redeploy.py` | Recreate one bench container from a **base** `mysql:X` image and install a freshly-built MyVector component (clean datadir, full install). |
| `generate_page.py` | Render a results dir's `*.json` into a standalone `index.html` (static table + cards always visible; Chart.js charts as a guarded enhancement). An optional `page.json` sidecar in the results dir overrides the title/subtitle/description/caveats, so the same generator renders both the synthetic and real-data pages. |

Result JSONs live in [`results/bench-dashboard/`](../../results/bench-dashboard/) (synthetic)
and [`results/bench-dashboard-real/`](../../results/bench-dashboard-real/) (real datasets).

## Real datasets (ann-benchmarks)

The synthetic workload is a fast internal sanity check, but synthetic vectors
don't predict recall on real embeddings. `bench_real.py` runs a published
[ann-benchmarks](https://github.com/erikbern/ann-benchmarks) dataset in HDF5
form (`train` / `test` / `neighbors`) and scores **recall@10 against the
dataset's published exact neighbors** — the ann-benchmarks methodology.

The full `train` set is indexed with row id == train index, so a published
neighbor index maps straight to a row id; the separate `test` set supplies
held-out queries. Metrics and JSON schema match the synthetic run, so results
render on the same dashboard.

```bash
# one-time: a venv with the dataset deps, and the dataset itself (on /data)
python3 -m venv ~/bench-venv && ~/bench-venv/bin/pip install numpy h5py
curl -L -o /data/bench-datasets/fashion-mnist-784-euclidean.hdf5 \
  http://ann-benchmarks.com/fashion-mnist-784-euclidean.hdf5

# run (restart the container first so no stale in-memory index lingers — see Caveats)
docker restart bench-mysql97
~/bench-venv/bin/python scripts/bench-dashboard/bench_real.py \
  --container bench-mysql97 --version 9.7 --root-pw "$PW" \
  --dataset /data/bench-datasets/fashion-mnist-784-euclidean.hdf5 \
  --dataset-label fashion-mnist-784 --ef-search-sweep 10,20,50,100,200,400,800 \
  --out results/bench-dashboard-real/result-9.7.json

# MariaDB counterpart
docker restart bench-mariadb
~/bench-venv/bin/python scripts/bench-dashboard/bench_mariadb_real.py --root-pw "$PW" \
  --dataset /data/bench-datasets/fashion-mnist-784-euclidean.hdf5 \
  --dataset-label fashion-mnist-784 --out results/bench-dashboard-real/result-mariadb.json

# page (the page.json sidecar supplies the real-data title/caveats)
python3 scripts/bench-dashboard/generate_page.py results/bench-dashboard-real site/index.html
```

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
     -v /data/mysql/bench/mariadb:/var/lib/mysql mariadb:12.3
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

- **MariaDB cache sizing:** MariaDB's `mhnsw_max_cache_size` defaults to 16 MB, smaller than
  the ~27 MB index for this dataset, which makes searches and the online build thrash to disk
  (~15× slower). `bench_mariadb.py` raises it to 1 GiB before the build so the comparison is fair
  (MyVector loads its whole index into memory). Size it above the index for your own data.
- **Cross-engine, not cross-build:** MariaDB (12.3 LTS) uses its own native vector
  index — a different implementation from MyVector-on-MySQL. MariaDB builds the
  HNSW index **online during insert** (no separate build phase or
  `ef_construction`), so its "build time" is load time.
- **Hardware:** all four engines run on the same Ampere A1 host, each pinned to an
  identical 4 vCPU / 24 GB slice, so the numbers are directly comparable. Recorded
  per-instance in each result JSON.
- Recall is honest (held-out queries never inserted); ground truth is
  brute-force top-10 on an unindexed copy (synthetic) or the dataset's
  **published** exact neighbors (real datasets, `bench_real.py`).
- **Restart the container before a fresh `bench_real.py` run on it.** MyVector
  keeps the HNSW index open in memory (`VectorIndexCollection`), keyed by
  column; `DROP TABLE` does not always evict it. If a previous run (e.g. a
  smaller smoke) left `bench.build_t.vec` open at a smaller `size=`, the next
  build adds more rows than that capacity and mysqld aborts with
  *"The number of elements exceeds the specified limit"*. A `docker restart`
  clears the in-memory index; the driver scripts here do this between engines.
