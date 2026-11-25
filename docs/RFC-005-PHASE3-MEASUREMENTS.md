# RFC-005 Phase 3: measurements of `storage=innodb` against `storage=file`

Companion to [RFC-005](RFC-005-INNODB-INDEX-STORAGE.md) (§7 items 27–29, §8 step 5).
Phases 1a and 1b put HNSW indexes, online ones included, into InnoDB. This is what
that costs next to the file backend, what the numbers say about the questions the
RFC left open (D2, D5, D7, D12), and the evidence for two product decisions that
are not made here: whether `innodb` becomes the default, and whether to port it to
the plugin.

## Summary

At 1,000,000 × 768 (MySQL 8.4.13, 16 GB buffer pool, one run):

| | `file` | `innodb` |
|---|---|---|
| Build and save | 268 s | 329 s (the save is 86 s of it) |
| Stored size | 3.2 GB | 3.3 GB |
| Load on a running server | 2.0 s | 36.8 s |
| Clean restart until loaded and caught up | 5.6 s | 211 s |
| Search, 8 sessions | 3,061 queries/s | 3,103 queries/s |
| Checkpoint after 1,000 inserts | 4.9 s | 3.5 s |
| Checkpoint after 100,000 inserts | 76.8 s | 10.5 s |
| Checkpoint after 1,000 scattered vector updates | 11.9 s | 53.1 s |
| Checkpoint after 10,000 scattered vector updates | 63.5 s | 117 s |
| Restart after `kill -9`, 100,000 changes not checkpointed | 203 s | 380 s |

1. **Searching is the same.** Throughput and recall do not depend on where the
   index is stored, and a reloaded index returns the same rows as before the
   reload, on every tier and both backends.
2. **Loading is where `innodb` is clearly worse:** 18 times slower for a reload at
   1M rows, and 38 times slower for a clean restart (the same load with a cold
   buffer pool). At 100k rows the ratios are 5 and 5.
3. **Inserts favour `innodb` at scale.** Its checkpoint writes the chunks that
   changed; the file backend's wrote about as much as the whole index at every
   checkpoint (3.2 GB after 1,000 inserts at 1M rows).
4. **Scattered updates are the weak case for `innodb`.** 1,000 updates spread over
   a 1M-row index changed 563 of 735 vector chunks, so the checkpoint rewrote
   2.5 GB and took 53 s. Both backends apply no online updates while they write a
   checkpoint.
5. **On most of what was measured, all or nearly all link chunks are rewritten
   at a checkpoint, whatever their size:** after scattered updates on every
   tier, and after inserts on the uniform random tiers. The exception is
   inserts on fashion-mnist (23 and then 6 of 31). Where the changed rows'
   neighbors are spread over the graph, chunking links by node range does not
   bound what a checkpoint writes (§ Chunk sizes).
6. **`innodb` needs a sized server.** With MySQL's default 128 MB buffer pool the
   checkpoint after 1,000 updates at 100k rows took 10.8 s; with 16 GB, 2.6 s.
7. The work found five bugs, four of them in code older than RFC-005
   (§ Bugs found).

## What the numbers say about the open decisions

| Decision | Finding | Recommendation |
|---|---|---|
| **D2** links layout | The store writes every link chunk that holds a changed node. On the uniform random tiers that was all or nearly all of them. In the single checkpoint after 1,000 inserts at 1M rows: 505 with 256 KB chunks (the build stored 504; the index had grown), 2,011 with 64 KB (2,017 at the build), 127 with 1 MB (126 at the build). Where a change took several checkpoints the counts in the tables are sums over them. After scattered updates it was the same on every tier, fashion-mnist included (36 in one checkpoint, of 31 at the build plus the chunks added since). After inserts on fashion-mnist it was 23 and then 6 of 31: the inserted rows there are uniform random vectors, unlike the dataset, and mostly become each other's neighbors. So the write set for links follows how far the changed rows' neighbors are spread, and smaller chunks do not help when they are spread everywhere. | Reopen D2, and first measure inserts drawn from the data's own distribution, which neither kind of tier here does. If they behave like the uniform tiers, store level-0 links per node, or in groups small enough that most stay clean. An upper bound for per-node rows after 1,000 inserts is about 33,000 lists (each insert and up to 32 neighbors), 4.4 MB, against the 132 MB written at 1M rows now. That bound is an estimate, not a measurement. |
| **D5** chunk sizes | Link chunk size changes row counts, not bytes written. Vector chunk size matters only for scattered updates at scale: at 1M rows, 1,000 updates wrote 1.1 GB in 19 s with 1 MB vector chunks, 2.5 GB in 53 s with the 4 MB default, 3.5 GB in 79 s with 16 MB. Save and load times barely move (save 65 / 86 / 79 s, load 37 / 37 / 42 s). At 100k rows nothing changes: the updates touched every vector chunk at every size. | Lower the vector chunk default from 4 MB to 1 MB. The evidence is one run per size at 1M rows, with the link size changed alongside, so it is a recommendation, not yet a change. Keep 256 KB for links until D2 is settled. |
| **D7** checkpoint interval | With a 10 s interval and steady inserts the longest checkpoint was 0.3–0.5 s at 60k–100k rows and 4.2 s at 1M. For scattered updates a checkpoint costs about the same whatever it accumulated (most of the index), so fewer checkpoints write less. | Keep 300 s. |
| **D12** asynchronous checkpoints | The listener is paused for the whole checkpoint: seconds for inserts at 1M rows, 53–117 s for scattered updates. The file backend pauses too (4.9–77 s at 1M rows). | Still needed for update-heavy large indexes, but after D2: writing less shortens the pause for every workload, and a copy buffer of today's write set would hold up to the whole index. |

**Loading** is not one of the RFC's open decisions but is the largest gap. The
store reads every chunk as `HEX(col)` (twice the bytes, then decoded) on one
session, a workaround for the command service returning binary values with zero
bytes as empty strings on 8.4 and 9.7. Reading binary where the server allows it,
and reading chunks on several sessions, are the obvious things to try. Not
investigated further here.

**Default storage and the plugin port.** The evidence above argues against making
`innodb` the default now: it is crash-safe and in backups, and cheaper for
insert-heavy large indexes, but it restarts 38 times slower at 1M rows, writes
several times the index per checkpoint under scattered updates, writes its own
size in redo at every build (3.4 GB for a 3.2 GB index), and needs a buffer pool
sized for it. Revisiting after D2 and the load path would be the time to decide.
Nothing here gives a reason to port it to the plugin yet. Both decisions are the
maintainers'.

## Method

- **Tool:** `scripts/bench-innodb-storage.py`; tables below are generated from the
  files in `results/rfc005-phase3/` by `scripts/bench-innodb-storage-report.py`.
- **Host:** one OCI Ampere A1 instance (Neoverse-N1, 16 cores, 94 GB), Docker,
  data on a block volume; nothing else under load during the runs.
- **Server:** one fresh `mysql:<version>` container per backend and run, GTID mode
  on, the component built from `main` at a4a6c0a with the Docker build scripts.
  "Defaults" is the image's configuration; "sized" adds
  `--innodb-buffer-pool-size=16G --innodb-redo-log-capacity=8G`.
- **Index:** HNSW, `M=16`, `ef=100`, `dist=L2`, `online=Y`, built with 16 threads.
- **Tiers:** 100,000 × 128 and 1,000,000 × 768 of uniform random vectors in
  (−1, 1); the 60,000 × 784 fashion-mnist set of ann-benchmarks.
- **Steps, the same for both backends:** build; three reloads; searches (2,000
  queries, best of three, 1 and 8 sessions; recall against the dataset's exact
  neighbors or a brute-force scan); a checkpoint after each of several batches
  of inserts and of updates to randomly chosen rows; a steady insert load with a
  10 s checkpoint interval; a clean restart; `kill -9` with changes that no
  checkpoint holds.
- **Checkpoint time:** for `innodb`, the duration the component logs for each
  checkpoint; for `file`, the span of each checkpoint in the log's own
  timestamps. Every checkpoint from the start of a change to the point where the
  stored index includes it is counted. The file backend checkpoints at a binlog
  rotation and whenever the listener reconnects; `innodb` on its timer (15 s in
  these steps).
- **Bytes written:** the growth of `write_bytes` in `/proc/<mysqld>/io` from just
  before the change until its checkpoint is done, so the binlog and redo of the
  change itself are included, the same for both backends. "Chunk bytes" in the
  detail tables are what the store wrote as chunk and upper-row data.

Limits of these numbers:

- One host, one run per configuration except the 100k tier on 8.4 (three runs,
  median shown). The three runs were close; single runs carry no error bars.
- Recall on the uniform random tiers is low (0.4–0.5 at 100k × 128, about 0.1
  at 1M × 768) and says nothing about MyVector: uniform random high-dimensional
  vectors have no neighbor structure. Fashion-mnist is the tier with a meaningful
  recall figure (1.000 for both).
- **Insert-to-searchable delay at 1M rows is not reported.** The probe times a
  marker row from its commit until a search with its own vector returns it. On
  the 1M uniform tier one marker per backend was never returned although it was
  indexed (the row counts matched), which the recall on that data explains. The
  longest checkpoint during the load stands in for it. At 100k rows and on
  fashion-mnist every marker was found.
- The file backend's write volume per checkpoint (about the size of the whole
  index) is an observation; why it writes that much was not investigated.
- "Clean restart" and "restart after kill" include the server's own startup.
- Restore was tested with `CLONE`, not xtrabackup (not in the images).

## Chunk sizes

`innodb` only; each row is a fresh server and build. Columns are checkpoint time and the chunk bytes it wrote. `17 MB` is 16 MiB.

Both tiers below are uniform random data. Link chunk size did not change the
bytes written there because all or nearly all link chunks held a changed node
at each checkpoint; see D2 above for where that does not hold.

### 100,000 × 128, MySQL 8.4 defaults (the 4 MB / 256 KB row is the default)

| Vector chunk | Link chunk | Save | Load | 1,000 ins: time / chunk bytes | 10,000 ins: time / chunk bytes | 100,000 ins: time / chunk bytes | 1,000 upd: time / chunk bytes | 10,000 upd: time / chunk bytes |
|---|---|---|---|---|---|---|---|---|
| 1 MB | 64 KB | 1.7 s | 0.86 s | 0.5 s / 15 MB | 0.4 s / 20 MB | 3.6 s / 81 MB | 11.8 s / 138 MB | 49.4 s / 414 MB |
| 1 MB | 256 KB | 1.7 s | 0.86 s | 0.3 s / 15 MB | 0.4 s / 20 MB | 2.4 s / 81 MB | 10.8 s / 138 MB | 48.2 s / 414 MB |
| 1 MB | 1,024 KB | 1.7 s | 0.86 s | 0.3 s / 15 MB | 0.4 s / 20 MB | 2.7 s / 81 MB | 10.9 s / 138 MB | 47.1 s / 414 MB |
| 4 MB | 64 KB | 2.1 s | 0.87 s | 0.4 s / 16 MB | 0.4 s / 22 MB | 2.9 s / 84 MB | 10.3 s / 138 MB | 51.7 s / 414 MB |
| 4 MB | 256 KB | 1.9 s | 0.87 s | 0.4 s / 16 MB | 0.4 s / 22 MB | 2.7 s / 84 MB | 10.7 s / 138 MB | 48.7 s / 414 MB |
| 4 MB | 1,024 KB | 1.9 s | 0.88 s | 0.3 s / 16 MB | 0.4 s / 22 MB | 2.5 s / 84 MB | 9.7 s / 138 MB | 44.4 s / 414 MB |
| 17 MB | 64 KB | 1.9 s | 1.00 s | 0.4 s / 16 MB | 0.4 s / 22 MB | 3.7 s / 88 MB | 11.5 s / 138 MB | 48.7 s / 414 MB |
| 17 MB | 256 KB | 1.9 s | 0.99 s | 0.4 s / 16 MB | 0.4 s / 22 MB | 3.3 s / 88 MB | 11.9 s / 138 MB | 48.5 s / 414 MB |
| 17 MB | 1,024 KB | 1.9 s | 0.99 s | 0.4 s / 16 MB | 0.4 s / 22 MB | 3.4 s / 88 MB | 11.0 s / 138 MB | 46.6 s / 414 MB |


### 1,000,000 × 768, MySQL 8.4 sized (default sizes: save 85.6 s, load 36.80 s, 1,000 updates 53.1 s / 2,507 MB; see the 1M tier below)

| Vector chunk | Link chunk | Save | Load | 1,000 ins: time / chunk bytes | 10,000 ins: time / chunk bytes | 100,000 ins: time / chunk bytes | 1,000 upd: time / chunk bytes | 10,000 upd: time / chunk bytes |
|---|---|---|---|---|---|---|---|---|
| 1 MB | 64 KB | 64.8 s | 36.84 s | 3.6 s / 135 MB | 2.7 s / 165 MB | 11.0 s / 599 MB | 19.3 s / 1,051 MB | 94.1 s / 5,384 MB |
| 17 MB | 1,024 KB | 78.6 s | 41.55 s | 3.5 s / 145 MB | 2.7 s / 178 MB | 11.6 s / 619 MB | 78.7 s / 3,485 MB | 117.9 s / 6,970 MB |


## Restore by clone

`CLONE LOCAL DATA DIRECTORY` of a server with an online `storage=innodb` index
(25,000 × 32, MySQL 8.4.13), then a server started on the clone. A clone carries
no binary logs; the new server has a new UUID, the source's executed set in
`gtid_purged`, and an empty `Previous_gtids` in its first binlog file.

| State of the index when cloned | On the clone |
|---|---|
| Fully checkpointed | Loaded with all 25,000 rows; 1,000 inserts on the clone reach it. |
| Checkpointed at 20,000 rows, table at 25,000 | With the fix for [#329](https://github.com/askdba/myvector/issues/329): not loaded, the log says the binary logs since its last checkpoint are no longer on this server; after `MYVECTOR_INDEX_BUILD` it follows. On `main` at the time of the measurements it was loaded with 20,000 rows, followed new inserts and never received the other 5,000. |

The second row is the purged-GTID case of RFC-005 §7 item 27. The clone itself
took under a second at this size; restore time at larger sizes was not measured.

## Bugs found

| Issue | What | Where |
|---|---|---|
| [#322](https://github.com/askdba/myvector/issues/322) | `mysqld` crash when a search runs while the index is rebuilt | hnswlib search entry, older than RFC-005 |
| [#325](https://github.com/askdba/myvector/issues/325) | File-backed online index corrupted by the first checkpoint after a restart | file backend, older than RFC-005 |
| [#327](https://github.com/askdba/myvector/issues/327) | File-backed online index stops following the binlog after a crash during a checkpoint | file backend, older than RFC-005 |
| [#255](https://github.com/askdba/myvector/issues/255) | Flaky CI check counted log lines instead of checkpoints | test |
| [#329](https://github.com/askdba/myvector/issues/329) | Online `storage=innodb` index loaded with changes missing after a clone or restore | RFC-005 Phase 1b |

#325 and #327 were found by this benchmark's restart steps, #329 by the clone
experiment, #322 by CI while the tests for Phase 1b were extended.

## Results by tier

### 1,000,000 × 768 synthetic, MySQL 8.4, sized

1,000,000 x 768, synthetic uniform(-1, 1); MySQL 8.4.13, --innodb-buffer-pool-size=16G --innodb-redo-log-capacity=8G; 1 run.

| | file | innodb |
|---|---|---|
| Build and save | 268.3 s | 329.2 s |
| of which the save | n/a | 85.6 s |
| Stored size | 3,217 MB | 3,276 MB |
| Redo written by the build | 0 MB | 3,424 MB |
| Load of the stored index | 2.04 s | 36.80 s |
| Queries/s, 1 session | 409 | 412 |
| Queries/s, 8 sessions | 3,061 | 3,103 |
| Recall@10 | 0.090 | 0.120 |
| Checkpoint after 1,000 inserts: time | 4.9 s | 3.5 s |
| Checkpoint after 1,000 inserts: bytes written by mysqld, change included | 3,245 MB | 1,014 MB |
| Checkpoint after 1,000 inserts: redo, change included | 3 MB | 162 MB |
| Checkpoint after 10,000 inserts: time | 29.2 s | 2.4 s |
| Checkpoint after 10,000 inserts: bytes written by mysqld, change included | 3,512 MB | 1,130 MB |
| Checkpoint after 10,000 inserts: redo, change included | 33 MB | 229 MB |
| Checkpoint after 100,000 inserts: time | 76.8 s | 10.5 s |
| Checkpoint after 100,000 inserts: bytes written by mysqld, change included | 6,106 MB | 5,238 MB |
| Checkpoint after 100,000 inserts: redo, change included | 329 MB | 1,021 MB |
| Checkpoint after 1,000 vector updates: time | 11.9 s | 53.1 s |
| Checkpoint after 1,000 vector updates: bytes written by mysqld, change included | 3,330 MB | 9,232 MB |
| Checkpoint after 1,000 vector updates: redo, change included | 8 MB | 2,691 MB |
| Checkpoint after 10,000 vector updates: time | 63.5 s | 117.4 s |
| Checkpoint after 10,000 vector updates: bytes written by mysqld, change included | 4,017 MB | 32.1 GB |
| Checkpoint after 10,000 vector updates: redo, change included | 78 MB | 7,615 MB |
| Marker rows a search never returned (probe unusable if not 0) | 1 | 1 |
| Longest checkpoint during that load (10 s interval) | n/a | 4.2 s |
| Clean restart until loaded and caught up | 5.6 s | 211.2 s |
| Restart after kill -9, changes not checkpointed | 203.1 s | 379.6 s |

InnoDB checkpoints in detail (the build stored 735 vector chunks and 504 link chunks; first run):

| Change | Checkpoints | Vector chunks | Link chunks | Upper rows | Chunk bytes |
|---|---|---|---|---|---|
| 1,000 inserts | 1 | 2 | 505 | 978 | 138 MB |
| 10,000 inserts | 1 | 8 | 510 | 6,142 | 167 MB |
| 100,000 inserts | 2 | 76 | 1,100 | 32,217 | 606 MB |
| 1,000 updates | 1 | 563 | 560 | 1,828 | 2,507 MB |
| 10,000 updates | 2 | 1,611 | 1,120 | 13,021 | 7,042 MB |


### 60,000 × 784 fashion-mnist, MySQL 8.4, sized

60,000 x 784, fashion-mnist-784-euclidean.hdf5; MySQL 8.4.13, --innodb-buffer-pool-size=16G --innodb-redo-log-capacity=8G; 1 run.

| | file | innodb |
|---|---|---|
| Build and save | 48.4 s | 52.1 s |
| of which the save | n/a | 4.2 s |
| Stored size | 197 MB | 205 MB |
| Redo written by the build | 0 MB | 209 MB |
| Load of the stored index | 0.21 s | 1.34 s |
| Queries/s, 1 session | 1,230 | 1,233 |
| Queries/s, 8 sessions | 8,957 | 8,965 |
| Recall@10 | 1.000 | 1.000 |
| Checkpoint after 1,000 inserts: time | 0.1 s | 0.3 s |
| Checkpoint after 1,000 inserts: bytes written by mysqld, change included | 103 MB | 104 MB |
| Checkpoint after 1,000 inserts: redo, change included | 3 MB | 18 MB |
| Checkpoint after 10,000 inserts: time | 0.3 s | 1.0 s |
| Checkpoint after 10,000 inserts: bytes written by mysqld, change included | 295 MB | 382 MB |
| Checkpoint after 10,000 inserts: redo, change included | 34 MB | 72 MB |
| Checkpoint after 1,000 vector updates: time | 0.0 s | 4.4 s |
| Checkpoint after 1,000 vector updates: bytes written by mysqld, change included | 319 MB | 1,240 MB |
| Checkpoint after 1,000 vector updates: redo, change included | 8 MB | 257 MB |
| Checkpoint after 10,000 vector updates: time | 3.1 s | 13.0 s |
| Checkpoint after 10,000 vector updates: bytes written by mysqld, change included | 925 MB | 3,474 MB |
| Checkpoint after 10,000 vector updates: redo, change included | 79 MB | 1,076 MB |
| Insert to searchable under 100 inserts/s, median | 4.9 ms | 4.9 ms |
| Insert to searchable under 100 inserts/s, 99th percentile | 13 ms | 12 ms |
| Insert to searchable under 100 inserts/s, worst | 13 ms | 296 ms |
| Marker rows a search never returned (probe unusable if not 0) | 0 | 0 |
| Longest checkpoint during that load (10 s interval) | n/a | 0.3 s |
| Clean restart until loaded and caught up | 3.5 s | 15.8 s |
| Restart after kill -9, changes not checkpointed | 25.5 s | 37.9 s |

InnoDB checkpoints in detail (the build stored 45 vector chunks and 31 link chunks; first run):

| Change | Checkpoints | Vector chunks | Link chunks | Upper rows | Chunk bytes |
|---|---|---|---|---|---|
| 1,000 inserts | 1 | 2 | 23 | 121 | 13 MB |
| 10,000 inserts | 1 | 9 | 6 | 717 | 36 MB |
| 1,000 updates | 1 | 54 | 36 | 1,204 | 233 MB |
| 10,000 updates | 4 | 216 | 144 | 8,634 | 931 MB |


### 100,000 × 128 synthetic, MySQL 8.4, defaults

100,000 x 128, synthetic uniform(-1, 1); MySQL 8.4.13, defaults; 3 runs (median).

| | file | innodb |
|---|---|---|
| Build and save | 6.0 s | 7.6 s |
| of which the save | n/a | 1.9 s |
| Stored size | 66 MB | 82 MB |
| Redo written by the build | 0 MB | 71 MB |
| Load of the stored index | 0.17 s | 0.87 s |
| Queries/s, 1 session | 1,219 | 1,230 |
| Queries/s, 8 sessions | 8,301 | 8,333 |
| Recall@10 | 0.465 | 0.445 |
| Checkpoint after 1,000 inserts: time | 0.5 s | 0.9 s |
| Checkpoint after 1,000 inserts: bytes written by mysqld, change included | 72 MB | 117 MB |
| Checkpoint after 1,000 inserts: redo, change included | 1 MB | 19 MB |
| Checkpoint after 10,000 inserts: time | 0.5 s | 0.4 s |
| Checkpoint after 10,000 inserts: bytes written by mysqld, change included | 121 MB | 159 MB |
| Checkpoint after 10,000 inserts: redo, change included | 6 MB | 33 MB |
| Checkpoint after 100,000 inserts: time | 1.9 s | 2.7 s |
| Checkpoint after 100,000 inserts: bytes written by mysqld, change included | 587 MB | 750 MB |
| Checkpoint after 100,000 inserts: redo, change included | 60 MB | 155 MB |
| Checkpoint after 1,000 vector updates: time | 2.5 s | 10.8 s |
| Checkpoint after 1,000 vector updates: bytes written by mysqld, change included | 212 MB | 856 MB |
| Checkpoint after 1,000 vector updates: redo, change included | 2 MB | 153 MB |
| Checkpoint after 10,000 vector updates: time | 1.1 s | 48.0 s |
| Checkpoint after 10,000 vector updates: bytes written by mysqld, change included | 1,062 MB | 4,134 MB |
| Checkpoint after 10,000 vector updates: redo, change included | 24 MB | 638 MB |
| Insert to searchable under 500 inserts/s, median | 4.3 ms | 4.3 ms |
| Insert to searchable under 500 inserts/s, 99th percentile | 29 ms | 32 ms |
| Insert to searchable under 500 inserts/s, worst | 3,288 ms | 476 ms |
| Marker rows a search never returned (probe unusable if not 0) | 0 | 0 |
| Longest checkpoint during that load (10 s interval) | n/a | 0.4 s |
| Clean restart until loaded and caught up | 1.9 s | 10.4 s |
| Restart after kill -9, changes not checkpointed | 68.9 s | 73.8 s |

InnoDB checkpoints in detail (the build stored 13 vector chunks and 51 link chunks; first run):

| Change | Checkpoints | Vector chunks | Link chunks | Upper rows | Chunk bytes |
|---|---|---|---|---|---|
| 1,000 inserts | 1 | 1 | 51 | 925 | 16 MB |
| 10,000 inserts | 1 | 2 | 56 | 4,082 | 22 MB |
| 100,000 inserts | 1 | 14 | 107 | 11,771 | 84 MB |
| 1,000 updates | 1 | 27 | 107 | 1,927 | 138 MB |
| 10,000 updates | 3 | 81 | 321 | 11,579 | 414 MB |


### 100,000 × 128 synthetic, MySQL 8.4, sized

100,000 x 128, synthetic uniform(-1, 1); MySQL 8.4.13, --innodb-buffer-pool-size=16G --innodb-redo-log-capacity=8G; 1 run.

| | file | innodb |
|---|---|---|
| Build and save | 6.0 s | 7.6 s |
| of which the save | n/a | 1.9 s |
| Stored size | 66 MB | 82 MB |
| Redo written by the build | 0 MB | 71 MB |
| Load of the stored index | 0.14 s | 0.87 s |
| Queries/s, 1 session | 1,239 | 1,253 |
| Queries/s, 8 sessions | 8,387 | 8,484 |
| Recall@10 | 0.415 | 0.450 |
| Checkpoint after 1,000 inserts: time | 0.7 s | 0.4 s |
| Checkpoint after 1,000 inserts: bytes written by mysqld, change included | 72 MB | 117 MB |
| Checkpoint after 1,000 inserts: redo, change included | 1 MB | 19 MB |
| Checkpoint after 10,000 inserts: time | 0.5 s | 0.4 s |
| Checkpoint after 10,000 inserts: bytes written by mysqld, change included | 123 MB | 160 MB |
| Checkpoint after 10,000 inserts: redo, change included | 6 MB | 33 MB |
| Checkpoint after 100,000 inserts: time | 1.9 s | 1.7 s |
| Checkpoint after 100,000 inserts: bytes written by mysqld, change included | 584 MB | 744 MB |
| Checkpoint after 100,000 inserts: redo, change included | 60 MB | 155 MB |
| Checkpoint after 1,000 vector updates: time | 2.6 s | 2.6 s |
| Checkpoint after 1,000 vector updates: bytes written by mysqld, change included | 192 MB | 945 MB |
| Checkpoint after 1,000 vector updates: redo, change included | 2 MB | 153 MB |
| Checkpoint after 10,000 vector updates: time | 1.3 s | 6.4 s |
| Checkpoint after 10,000 vector updates: bytes written by mysqld, change included | 636 MB | 1,753 MB |
| Checkpoint after 10,000 vector updates: redo, change included | 23 MB | 477 MB |
| Insert to searchable under 500 inserts/s, median | 4.3 ms | 4.2 ms |
| Insert to searchable under 500 inserts/s, 99th percentile | 30 ms | 70 ms |
| Insert to searchable under 500 inserts/s, worst | 32 ms | 597 ms |
| Marker rows a search never returned (probe unusable if not 0) | 0 | 0 |
| Longest checkpoint during that load (10 s interval) | n/a | 0.5 s |
| Clean restart until loaded and caught up | 3.5 s | 11.4 s |
| Restart after kill -9, changes not checkpointed | 71.3 s | 80.2 s |

InnoDB checkpoints in detail (the build stored 13 vector chunks and 51 link chunks; first run):

| Change | Checkpoints | Vector chunks | Link chunks | Upper rows | Chunk bytes |
|---|---|---|---|---|---|
| 1,000 inserts | 1 | 1 | 51 | 918 | 16 MB |
| 10,000 inserts | 1 | 2 | 56 | 4,086 | 22 MB |
| 100,000 inserts | 1 | 14 | 107 | 11,755 | 84 MB |
| 1,000 updates | 1 | 27 | 107 | 1,736 | 138 MB |
| 10,000 updates | 3 | 81 | 321 | 12,130 | 414 MB |


### 100,000 × 128 synthetic, MySQL 9.7, defaults

100,000 x 128, synthetic uniform(-1, 1); MySQL 9.7.4, defaults; 1 run.

| | file | innodb |
|---|---|---|
| Build and save | 6.0 s | 7.7 s |
| of which the save | n/a | 2.1 s |
| Stored size | 66 MB | 82 MB |
| Redo written by the build | 0 MB | 71 MB |
| Load of the stored index | 0.15 s | 0.89 s |
| Queries/s, 1 session | 1,246 | 1,274 |
| Queries/s, 8 sessions | 8,296 | 8,505 |
| Recall@10 | 0.470 | 0.465 |
| Checkpoint after 1,000 inserts: time | 0.5 s | 0.8 s |
| Checkpoint after 1,000 inserts: bytes written by mysqld, change included | 72 MB | 116 MB |
| Checkpoint after 1,000 inserts: redo, change included | 1 MB | 19 MB |
| Checkpoint after 10,000 inserts: time | 0.6 s | 0.5 s |
| Checkpoint after 10,000 inserts: bytes written by mysqld, change included | 124 MB | 162 MB |
| Checkpoint after 10,000 inserts: redo, change included | 6 MB | 33 MB |
| Checkpoint after 100,000 inserts: time | 1.9 s | 2.8 s |
| Checkpoint after 100,000 inserts: bytes written by mysqld, change included | 572 MB | 735 MB |
| Checkpoint after 100,000 inserts: redo, change included | 59 MB | 153 MB |
| Checkpoint after 1,000 vector updates: time | 2.6 s | 9.4 s |
| Checkpoint after 1,000 vector updates: bytes written by mysqld, change included | 209 MB | 928 MB |
| Checkpoint after 1,000 vector updates: redo, change included | 2 MB | 151 MB |
| Checkpoint after 10,000 vector updates: time | 1.1 s | 32.1 s |
| Checkpoint after 10,000 vector updates: bytes written by mysqld, change included | 983 MB | 3,035 MB |
| Checkpoint after 10,000 vector updates: redo, change included | 22 MB | 475 MB |
| Insert to searchable under 500 inserts/s, median | 4.2 ms | 4.1 ms |
| Insert to searchable under 500 inserts/s, 99th percentile | 30 ms | 26 ms |
| Insert to searchable under 500 inserts/s, worst | 303 ms | 492 ms |
| Marker rows a search never returned (probe unusable if not 0) | 0 | 0 |
| Longest checkpoint during that load (10 s interval) | n/a | 0.5 s |
| Clean restart until loaded and caught up | 2.0 s | 10.6 s |
| Restart after kill -9, changes not checkpointed | 67.8 s | 75.4 s |

InnoDB checkpoints in detail (the build stored 13 vector chunks and 51 link chunks; first run):

| Change | Checkpoints | Vector chunks | Link chunks | Upper rows | Chunk bytes |
|---|---|---|---|---|---|
| 1,000 inserts | 1 | 1 | 51 | 883 | 16 MB |
| 10,000 inserts | 1 | 2 | 56 | 4,038 | 22 MB |
| 100,000 inserts | 1 | 14 | 107 | 11,761 | 84 MB |
| 1,000 updates | 1 | 27 | 107 | 1,719 | 138 MB |
| 10,000 updates | 2 | 54 | 214 | 8,785 | 276 MB |


### 100,000 × 128 synthetic, MySQL 26.10, defaults

100,000 x 128, synthetic uniform(-1, 1); MySQL 26.10.0, defaults; 1 run.

| | file | innodb |
|---|---|---|
| Build and save | 6.0 s | 7.7 s |
| of which the save | n/a | 2.0 s |
| Stored size | 66 MB | 82 MB |
| Redo written by the build | 0 MB | 71 MB |
| Load of the stored index | 0.14 s | 0.89 s |
| Queries/s, 1 session | 1,283 | 1,262 |
| Queries/s, 8 sessions | 8,356 | 8,348 |
| Recall@10 | 0.430 | 0.450 |
| Checkpoint after 1,000 inserts: time | 0.5 s | 0.8 s |
| Checkpoint after 1,000 inserts: bytes written by mysqld, change included | 72 MB | 116 MB |
| Checkpoint after 1,000 inserts: redo, change included | 1 MB | 19 MB |
| Checkpoint after 10,000 inserts: time | 0.6 s | 0.5 s |
| Checkpoint after 10,000 inserts: bytes written by mysqld, change included | 124 MB | 162 MB |
| Checkpoint after 10,000 inserts: redo, change included | 6 MB | 33 MB |
| Checkpoint after 100,000 inserts: time | 1.9 s | 2.9 s |
| Checkpoint after 100,000 inserts: bytes written by mysqld, change included | 572 MB | 734 MB |
| Checkpoint after 100,000 inserts: redo, change included | 59 MB | 153 MB |
| Checkpoint after 1,000 vector updates: time | 2.7 s | 10.3 s |
| Checkpoint after 1,000 vector updates: bytes written by mysqld, change included | 205 MB | 902 MB |
| Checkpoint after 1,000 vector updates: redo, change included | 2 MB | 151 MB |
| Checkpoint after 10,000 vector updates: time | 1.1 s | 30.7 s |
| Checkpoint after 10,000 vector updates: bytes written by mysqld, change included | 977 MB | 2,926 MB |
| Checkpoint after 10,000 vector updates: redo, change included | 22 MB | 475 MB |
| Insert to searchable under 500 inserts/s, median | 4.2 ms | 4.2 ms |
| Insert to searchable under 500 inserts/s, 99th percentile | 30 ms | 66 ms |
| Insert to searchable under 500 inserts/s, worst | 14,007 ms | 2,585 ms |
| Marker rows a search never returned (probe unusable if not 0) | 0 | 0 |
| Longest checkpoint during that load (10 s interval) | n/a | 0.5 s |
| Clean restart until loaded and caught up | 2.0 s | 10.9 s |
| Restart after kill -9, changes not checkpointed | 63.7 s | 73.1 s |

InnoDB checkpoints in detail (the build stored 13 vector chunks and 51 link chunks; first run):

| Change | Checkpoints | Vector chunks | Link chunks | Upper rows | Chunk bytes |
|---|---|---|---|---|---|
| 1,000 inserts | 1 | 1 | 51 | 924 | 16 MB |
| 10,000 inserts | 1 | 2 | 56 | 4,100 | 22 MB |
| 100,000 inserts | 1 | 14 | 107 | 11,702 | 84 MB |
| 1,000 updates | 1 | 27 | 107 | 1,982 | 138 MB |
| 10,000 updates | 2 | 54 | 214 | 8,457 | 276 MB |
