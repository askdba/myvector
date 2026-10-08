# RFC-005: Storing HNSW Indexes in InnoDB

**Author:** MyVector Engineering  
**Status:** Draft  
**Target Version:** Component builds (MySQL 8.4, 9.7, 26.7) first; plugin decided in Phase 3  
**Last Updated:** 2026-10-07  

---

## 1. Abstract

Today MyVector saves each HNSW index as a set of files in `myvector_index_dir`.
Its own code makes those saves crash-safe: a two-pass checkpoint, a status file,
and a recovery replay. This RFC proposes a second storage backend that keeps the
index in InnoDB tables instead.

With the new backend, a checkpoint is a single InnoDB transaction. It writes the
changed parts of the graph and the binlog position they correspond to, so both
commit together or not at all. The index is then covered by the tools DBAs already
use (xtrabackup, the clone plugin, `mysqldump`), and nothing needs a separate
directory on the file system.

Searches don't change: the graph is still loaded into memory and searched there.
Only persistence moves.

The index rows replicate like any other InnoDB data. Replicas load the primary's
graph from them instead of building their own (§4.8).

### 1.1 Scope

- **In scope:** HNSW indexes (`HNSWMemoryIndex`, backed by
  `hnswlib::HierarchicalDiskNSW`), component build.
- **Out of scope:** brute-force KNN indexes (`KNNIndex`), which have no
  persistence today. The plugin build is a Phase 3 decision (§8).

---

## 2. Motivation

1. **No file system state outside the datadir.** Operators must currently provide,
   size, back up and secure `myvector_index_dir` separately from MySQL. Managed and
   containerized deployments often have no good place for it.
2. **Backups miss the index.** A physical or logical backup of the server does not
   include the index files. After a restore, every index must be rebuilt from scratch.
3. **Custom crash recovery.** `doCheckPoint()` and `doRecovery()`
   (`include/hnswdisk.i`) implement a small write-ahead log by hand: a
   `.ckpt.state` file, a `.status` file holding `CKPT_*` states, and a replay.
   InnoDB already provides atomic, durable writes, which is everything this
   machinery exists to provide.
4. **Checkpoint state is not queryable.** `myvector_index_status` reports an
   index's state, but the checkpoint position and its history live in files and log
   lines, not in tables.

---

## 3. Current Design (for reference)

| Item | Where it lives today |
|---|---|
| Header (M, ef_construction, max level, entry point, counts…) | First 96 bytes of `<index name>.hnsw.index` (`saveIndexHeader`) |
| Vectors + level-0 links + labels | `.hnsw.index`, one fixed-size record per node (`size_data_per_element_`) |
| Upper-level links (level > 0) | `.hnsw.index.links` (directory: node id, size) + `.hnsw.index.links.data` |
| Checkpoint id (binlog file:pos or timestamp) | Embedded in the index, read on load |
| Checkpoint progress | `.hnsw.index.status` (`CKPT_BEGIN_INCR_PASS1` … `CKPT_CONSISTENT`) |
| In-flight checkpoint | `.hnsw.index.ckpt.state` |

Each node's level-0 record is laid out as
`[level-0 links | vector | label]`. The links part is small
(`4 + 4·M0` bytes, e.g. 132 bytes for M=16). The vector is usually most of
the record (e.g. 3,072 bytes for 768 float dimensions).

**Full save** (`saveIndex`, action `build`/`save`): one large sequential write of
all level-0 data, then the upper-level link files.

**Incremental checkpoint** (`doCheckPoint`): the graph tracks three sets of dirty
internal node ids (`include/hnswdisk.i:88`):

- **whole node:** a new or updated vector (`addNodeToFlushList`);
- **level-0 links only:** a node whose neighbor list changed because *another*
  vector was inserted next to it (`addNodeLinksLevel0ToFlushList`);
- **upper links:** the same for levels > 0.

For a links-only node the file backend writes just the links, not the vector.
That matters because **each insert changes the links of up to M0 existing
neighbors, which can be anywhere in the graph** (`include/hnswdisk.h:768`).

Checkpoints run at every binlog rotation: `FlushOnlineVectorIndexes()` waits until
the online-update queue is empty (`gqueue_.wait_until_empty()`), then calls
`myvector_checkpoint_index()` for each index. No online update runs during a
checkpoint. Indexes are saved at build and at rotation only; nothing is saved at
shutdown.

The key point for this RFC: **persistence is already node-granular and driven by
dirty sets**. The work is to change where those bytes go, not to change the
algorithm.

---

## 4. Proposal

### 4.1 Choosing the backend per index

Storage is an **option of each index**, in the existing options string
(`MyVectorOptions`):

```text
type=HNSW,dim=768,size=1000000,M=16,ef=100,storage=innodb
```

`storage=file` (the default) keeps today's behavior exactly. The choice is recorded
in `index_meta` (§4.2) and used by every later checkpoint, save, load and drop of
that index. A global setting was considered and rejected: changing it while an
index is in use would leave unclear where its next checkpoint goes.

A system variable `myvector_index_storage_default = file | innodb` sets the value
used when an index's options don't name one.

### 4.2 Schema

The schema and tables are created by the install SQL (with the UDFs and
procedures), never on first use at runtime.

Vectors and links are stored **separately**, because they change at very different
rates (§3): a vector changes only when its own row changes, while links change
whenever a nearby vector is inserted.

Chunks are **versioned**: a checkpoint never overwrites a chunk in place, it adds
a new version, and `index_meta.ckpt_seq` says which versions are current (§4.4).

```sql
CREATE TABLE myvector.index_meta (
  index_name      VARCHAR(192)      NOT NULL,   -- db.table.column
  format_version  SMALLINT UNSIGNED NOT NULL,
  generation      BIGINT UNSIGNED   NOT NULL,   -- live generation, see 4.6
  ckpt_seq        BIGINT UNSIGNED   NOT NULL,   -- last committed checkpoint, see 4.4
  header          VARBINARY(256)    NOT NULL,   -- saveIndexHeader() bytes
  vec_chunk_nodes INT UNSIGNED      NOT NULL,   -- nodes per vector chunk
  lnk_chunk_nodes INT UNSIGNED      NOT NULL,   -- nodes per links chunk
  ckpt_id         VARCHAR(512)      NOT NULL,   -- "Checkpoint:binlog:<file>:<pos>"
  ckpt_gtids      MEDIUMTEXT        NULL,       -- GTID set at the checkpoint, see 4.8
  ckpt_time       TIMESTAMP(6)      NOT NULL,
  node_count      BIGINT UNSIGNED   NOT NULL,
  PRIMARY KEY (index_name)
) ENGINE=InnoDB;

-- Vector + label of each node, in large chunks. Rewritten only when one of its
-- nodes is new or has a new vector.
CREATE TABLE myvector.index_vectors (
  index_name   VARCHAR(192)    NOT NULL,
  generation   BIGINT UNSIGNED NOT NULL,
  chunk_id     INT UNSIGNED    NOT NULL,
  version      BIGINT UNSIGNED NOT NULL,  -- ckpt_seq of the checkpoint that wrote it
  data         LONGBLOB        NOT NULL,  -- (vector | label) per node
  crc32        INT UNSIGNED    NOT NULL,
  PRIMARY KEY (index_name, generation, chunk_id, version)
) ENGINE=InnoDB;

-- Level-0 links of each node, plus upper-level links, in smaller chunks.
CREATE TABLE myvector.index_links (
  index_name   VARCHAR(192)    NOT NULL,
  generation   BIGINT UNSIGNED NOT NULL,
  chunk_id     INT UNSIGNED    NOT NULL,
  version      BIGINT UNSIGNED NOT NULL,
  level0       MEDIUMBLOB      NOT NULL,  -- level-0 links per node
  upper        MEDIUMBLOB      NULL,      -- (node_id, len, bytes)* for level > 0
  crc32        INT UNSIGNED    NOT NULL,
  PRIMARY KEY (index_name, generation, chunk_id, version)
) ENGINE=InnoDB;
```

### 4.3 Chunking

hnswlib internal node ids are dense (`0 … cur_element_count-1`), so a chunk is a
fixed range of ids: chunk `k` covers `[k·C, (k+1)·C)`. Saving copies the two parts
of each node's level-0 record into the two tables; loading copies them back. This
is a memory copy per node, with no format conversion.

Why chunks rather than one row per node:

- **Bulk speed.** A full save writes a few thousand rows instead of millions.
- **Row size.** One row per node goes off-page above roughly 8 KB per node
  (about 1,900 float dimensions), which makes every row two page reads.
- **Load speed.** Loading is a primary-key range scan of large blobs: close to a
  sequential file read.

Starting defaults, both configurable and tuned by benchmark in Phase 3 (D5):

| Table | Default chunk size | Example (1M nodes, dim 768, M=16) |
|---|---|---|
| `index_vectors` | `myvector_index_vec_chunk_bytes` = 4 MB | ~1,360 nodes/chunk, ~740 chunks, ~3.1 GB |
| `index_links` | `myvector_index_lnk_chunk_bytes` = 256 KB | ~2,000 nodes/chunk, ~500 chunks, ~132 MB |

Both are clamped below `max_allowed_packet`.

**Write volume.** In the example above, 1,000 inserts dirty up to 32,000
neighbors scattered across the graph, so nearly every links chunk is dirty.

| Backend | Writes per checkpoint (1,000 inserts) |
|---|---|
| `file` (today) | ~3 MB vectors + ≤ ~4 MB links ≈ 7 MB |
| `innodb`, vectors and links in one chunk (rejected) | ~all chunks ≈ 3.2 GB |
| `innodb`, split (proposed) | new vector chunks (~4 MB) + ≤ all links (~132 MB) |

The worst case for the proposed design is bounded by the size of all links, about
4% of the index here. It is still larger than the file backend's; Phase 3 measures
whether that is acceptable or links need finer chunks.

### 4.4 Incremental checkpoint

Dirty node sets map to dirty chunks:

- whole node → its vector chunk **and** its links chunk;
- level-0 or upper links only → its links chunk.

A checkpoint with sequence number `s = ckpt_seq + 1` writes every dirty chunk as
**version `s`**, then commits `index_meta.ckpt_seq = s` last:

1. Delete any rows with `version > ckpt_seq` for this index. Only a crash during
   an earlier checkpoint leaves these behind.
2. Insert the dirty chunks as version `s`, **one statement per chunk** (so no
   statement exceeds `max_allowed_packet`), in transactions of at most
   `myvector_checkpoint_txn_bytes` (default 64 MB).
3. In the last transaction, update `index_meta`:

```sql
SET SESSION binlog_row_image = 'MINIMAL';   -- see 4.8: don't log before-images
START TRANSACTION;
  -- for each dirty chunk in this batch:
  INSERT INTO myvector.index_vectors
         (index_name, generation, chunk_id, version, data, crc32)
  VALUES (?, ?, ?, ?, ?, ?);
  INSERT INTO myvector.index_links (...) VALUES (...);
  -- last transaction only:
  UPDATE myvector.index_meta
     SET ckpt_seq = ?, header = ?, ckpt_id = ?, ckpt_gtids = ?,
         ckpt_time = NOW(6), node_count = ?
   WHERE index_name = ? AND generation = ?;
COMMIT;
```

4. After the commit, delete the older versions of the chunks just written
   (`version < s`), in batches.

**Readers** (load §4.7, followers §4.8) see, for each chunk, the newest version
`≤ ckpt_seq`, read in the same consistent snapshot as `index_meta`. Rows of an
unfinished checkpoint have `version > ckpt_seq`, so they are invisible.

Why versions rather than one transaction per checkpoint:

- **Atomic at any size.** A crash partway through leaves only invisible rows. The
  index is exactly as of the last committed checkpoint. Splitting into several
  transactions *without* versions would leave some chunks new and some old under
  the old `ckpt_seq`: a corrupt graph.
- **Bounded transactions.** No checkpoint is ever one large transaction, so none
  exceeds Group Replication's `group_replication_transaction_size_limit` (150 MB by
  default), and none puts pressure on the redo log or makes replicas lag behind one
  large event.
- **One path.** Every checkpoint takes the same path whatever its size; there is no
  size threshold to tune.

Checkpoints run at binlog rotation and on a timer (§4.11). The new chunk versions
and `ckpt_id` become visible together, with the commit of `index_meta`. After a
crash, the index is exactly as of the last committed checkpoint, and the binlog
listener resumes from its `ckpt_id`. Nothing needs replaying on our side:
`.ckpt.state`, `.status`, `WriteCheckPointStatus()` and `doRecovery()` are not
used in this mode.

**Concurrency.** The checkpoint relies on the existing rule that the online-update
queue is empty while it runs (§3), so the graph doesn't change under it. The dirty
sets are cleared only after the `index_meta` commit succeeds. If the commit fails, the binlog
position is rolled back, as `myvector_checkpoint_index()` already does for a failed
file save, and the next checkpoint retries. A manual `save` or `refresh` running at
the same time as a checkpoint is an existing gap with the file backend too, and is
out of scope here.

### 4.5 How MyVector runs SQL

MyVector code runs inside UDFs and background threads. It cannot run SQL on the
caller's session. Options:

| Option | Pros | Cons |
|---|---|---|
| **A. Client connection** (`mysql_real_connect`, as the binlog listener does) | Already used and tested; works for plugin and component | Needs credentials (`myvector.cnf`); goes through the network layer |
| **B. `mysql_command_services`** (component only) | In-process, no network | New code path; needs a session user and security context |

**Decision (D4):** start with A. It reuses the binlog listener's connection settings,
with a separate connection for storage (the listener's own connection is busy
streaming the binlog).

**New requirement:** today only online updates need `myvector.cnf`. With
`storage=innodb`, *every* build, save and load of that index needs it, including
indexes that never use online updates. A `storage=innodb` index without a working
connection fails to build, with a clear error.

Privileges for the MyVector user: `SELECT, INSERT, UPDATE, DELETE` on
`myvector.*`, plus `SESSION_VARIABLES_ADMIN` to set `binlog_row_image` for its
session.

### 4.6 Full save and generations

A **full save of an existing graph** (`save`) is a checkpoint in which every chunk
is dirty: all chunks get version `s` (§4.4). No separate mechanism is needed.

A **build** (`build`) replaces the graph: internal node ids are reassigned, and the
chunk count and chunk sizes can change. It writes a **new generation**:

1. `g = live generation + 1`.
2. Write all vector and links chunks for `g` (version 1) in bounded transactions.
3. In one transaction, update `index_meta` to point at `g`, with `ckpt_seq = 1`
   and the new header and `ckpt_id`.
4. Delete generation `g-1` in batches, in the background.

Readers only ever see the generation that `index_meta` points to. A crash during
step 2 leaves orphan rows for `g`, which are removed on the next load.

### 4.7 Load and startup

**Steps:**

1. `START TRANSACTION WITH CONSISTENT SNAPSHOT`. Every read below is in this one
   snapshot, so a checkpoint or replication applier running at the same time can't
   give a mix of two checkpoints.
2. Read `index_meta`. Check `format_version` and that the header matches the
   index definition (dimension, metric, M).
3. Scan `index_vectors` and `index_links` for `(index_name, generation)` in
   `chunk_id` order, taking the newest version `≤ ckpt_seq` of each chunk. Check
   each chunk's `crc32` and copy the parts into place. Rebuild `linkLists_` and
   `element_levels_` from `upper`.
4. Rebuild `label_lookup_` (row key → node) and the deleted-node count, as the file
   loader does today (`include/hnswdisk.i:967`).
5. Set the checkpoint coordinates from `ckpt_id` (and `ckpt_gtids`), as
   `loadIndex()` does today.

On any check failure (version, header, CRC, missing chunk), the load fails with a
clear error and the index must be rebuilt. This follows RFC-004's principle that
indexes are secondary, rebuildable structures.

**When loading happens:** components are loaded during server start, before the
server accepts connections, so a load over SQL cannot run in component init. The
binlog listener already solves this: it retries its connection until the server
accepts it, then loads online indexes (`OpenAllOnlineVectorIndexes()`, #186). The
storage connection follows the same pattern:

- Online indexes load from the listener thread once it connects, as today.
- Other indexes load on `MYVECTOR_INDEX_LOAD`, also as today.
- Until an index has loaded, searches on it fail with the same error as today
  (`ER_MYVECTOR_INDEX_NOT_FOUND`).
- If the connection keeps failing, the error log says so on every retry (rate
  limited), and `myvector_index_status` shows the index as not loaded with the
  reason.

### 4.8 Replication

**Decision (D1):** index rows replicate. Replicas load the primary's graph from the
replicated rows; they don't build their own index from table data. This also makes
read-only servers work: replicas never write index rows, so `read_only` and
`super_read_only` don't get in the way.

**Roles.** For `storage=innodb` indexes, a server is:

- a **writer** while `read_only = OFF`: it applies table row events to the index
  (as today) and writes checkpoints;
- a **follower** while `read_only = ON`: it ignores table row events for these
  indexes and follows the replicated index rows instead.

**Writer side.**

- Checkpoint and full-save writes are logged normally, so they replicate.
- The writer's listener must ignore events on the `myvector` schema, or it would
  read its own checkpoints back.
- The storage session uses `binlog_row_image = MINIMAL`. With the default
  `FULL`, every chunk update would also log the old 4 MB or 256 KB row.

**Follower side.** The follower's listener already reads the follower's own binlog,
which contains the replicated transactions (`log_replica_updates`, `ON` by default
since 8.0). It ignores the chunk transactions and acts only on a change to an
`index_meta` row, which marks a committed checkpoint:

1. `START TRANSACTION WITH CONSISTENT SNAPSHOT`, then read `index_meta`. The
   replication applier may already be past the checkpoint that triggered this, so
   the follower uses whatever checkpoint the snapshot shows; it may skip some.
2. If `generation` changed (a build, §4.6), reload the whole index (§4.7).
3. Otherwise read every chunk with `last applied seq < version ≤ ckpt_seq` (newest
   version per chunk), check the CRCs, and copy them into the in-memory graph
   under the index's exclusive lock (`lockExclusive()`). Update the header and
   rebuild the `label_lookup_` entries for the changed chunks. Searches wait during
   the copy: a memory copy of at most the links plus new vectors (~136 MB in the
   §4.3 example).
4. Remember `ckpt_seq` as the last applied seq.

The snapshot matters: without it, a follower reading chunks while its applier is
applying the next checkpoint (and deleting old versions, §4.4 step 4) could read a
mix of two checkpoints.

A follower's graph is never a mix of its own changes and the primary's. HNSW
inserts are not deterministic (random levels, parallel workers), so a graph built
on the replica would not match the primary's node for node, and copying the
primary's chunks into it would corrupt it.

**Freshness.** A follower's index is as of the primary's last checkpoint. With the
timer (§4.11), it lags by up to `myvector_checkpoint_interval` plus replication
delay. Rows inserted since then are in the table but not yet in the replica's ANN
results. Today a replica's index follows each row; this is a real change and must
be in the docs.

**Decision (D7):** keep the 300 s default and document the lag. Lowering it is
expensive: under steady writes each checkpoint costs about the same whatever its
interval, because ~1,000 inserts already dirty nearly all links chunks (§4.3). In
that example 60 s means ~136 MB/min of binlog to every replica, 300 s ~27 MB/min.

The real fix is **catch-up in memory** (Phase 2c), built on the GTID start below:

1. The follower loads a checkpoint, then applies the table changes after its
   `ckpt_gtids` in memory, as a writer does. Replicas are current again.
2. At the next checkpoint it reloads the chunks the primary changed **plus the
   chunks it changed locally**; the existing dirty-node sets track the second group.
   Its graph then equals the primary's exactly, with no local changes left.
3. It restarts its binlog stream from the new `ckpt_gtids` and continues.

With catch-up, the interval only affects backup freshness and crash replay.

**Promotion.** When a follower becomes the writer (`read_only` turns off), its
graph is the old primary's last checkpoint. It must apply every table change after
that point, but `ckpt_id` holds the old primary's binlog file and position, which
mean nothing on the new primary.

**Decision (D8):** follower mode **requires GTID mode**, and promotion support ships
in the same phase as follower mode (Phase 2b). Replicas exist for failover; an index
that silently stops updating after a promotion, or needs hours of rebuild then, is
worse than no follower mode. GTID mode is standard in current topologies, and Group
Replication and InnoDB Cluster already require it.

- Every `storage=innodb` checkpoint records `ckpt_gtids` when GTID mode is on.
  This is the set of transactions the listener has applied up to the checkpoint's
  position, **not** `@@gtid_executed` at that moment (the server is already ahead).
  The listener builds it from its start set plus each GTID event it reads; it
  doesn't parse GTID events today, so this is new code.
- On promotion, and on any restart, the listener starts from `ckpt_gtids`.
  `mysql_binlog_open()`, which the listener already uses
  (`myvector_binlog_service.cc:2201`), supports this with the `MYSQL_RPL_GTID`
  flag and an encoded GTID set, so the stream setup changes but the listener does
  not.
- Follower mode refuses to start with GTID mode off, with a clear error.
- **File backend:** recording GTIDs there too would give one resume path for both
  backends, but its checkpoint id lives in a 256-byte status line
  (`include/hnswdisk.i:217`), too small for a GTID set. That needs a file format
  change and is a follow-up, not part of this RFC.

**Binlog volume.** Every checkpoint now goes through the binlog and over the network
to every replica: up to ~136 MB per checkpoint in the §4.3 example, and the whole
index (~3.2 GB) for a full save or build. Every checkpoint is written in
transactions of at most `myvector_checkpoint_txn_bytes` (§4.4, **D9**), so none
exceeds Group Replication's transaction size limit.

### 4.9 Security

Index rows are copies of the user's vectors, in another schema. Anyone granted
`SELECT` on `myvector.*` can read every indexed vector, whatever their grants on the
source tables. Embeddings can reveal information about the text or images they came
from. Today the files are protected by OS permissions instead, which is a different
exposure.

Rules:

- Grant `myvector.*` only to the MyVector user. The install SQL grants nothing
  else, and the docs say why.
- `mysqldump` of the whole server includes `myvector`; dumps must be protected like
  the source data.
- Index rows are now in binlogs and relay logs on every server.
  `binlog_encryption` covers them if it is on.
- Anyone who can write to `myvector.*` can corrupt an index. The CRC catches
  accidental changes; it is not a defense against deliberate ones.

### 4.10 Migration and drop

- **file → innodb:** load the index from its files, change its options to
  `storage=innodb`, then save it; the save is a full save (§4.6) and records the
  new backend in `index_meta`. The files are left in place until the user removes
  them. The internal `save` action exists today but has no user-facing procedure;
  Phase 2 adds one (`MYVECTOR_INDEX_SAVE`, or a `migrate` option on
  `MYVECTOR_INDEX_LOAD`).
- **innodb → file:** the same in reverse.
- **Drop** (`MYVECTOR_INDEX_DROP`): delete the `index_meta` row in one transaction,
  so the index is gone at once, then delete its chunks in batches.

### 4.11 Checkpoint timer

**Decision (D3):** checkpoints also run on a timer, not only at binlog rotation.

```text
myvector_checkpoint_interval = 300      (seconds; 0 = at rotation only)
```

The listener's binlog session already asks for a heartbeat every second
(`@master_heartbeat_period`), so its loop wakes often enough to check the timer even
when no rows change. When the interval has passed, it runs the same path as a
rotation (`FlushOnlineVectorIndexes()`): wait for the update queue to empty, take
the current position, checkpoint every online index. Indexes with no dirty nodes
are skipped. The timer applies to both backends; with `file` it shortens recovery
after a crash.

The interval bounds three things: how stale a backup's index is (§5), how far a
follower lags (§4.8), and how much binlog must be replayed after a crash.

---

## 5. Backup and Restore

A physical backup (xtrabackup, clone) or a consistent `mysqldump`
(`--single-transaction --hex-blob`) now contains the index with its `ckpt_id`.
Restoring a dump needs `max_allowed_packet` at least as large as the vector chunk
size.

Backups taken on a replica now include the index too, which is often where backups
are taken.

**Limitation:** the index is only as fresh as its last checkpoint: at most
`myvector_checkpoint_interval` old (§4.11), or one binlog file with the timer off. After a restore, the listener catches up from `ckpt_id`, so the
binlog from that position onward must still exist on the restored server. If it
doesn't (common after a restore to a new host), the index is missing the changes
between `ckpt_id` and the backup point. Detect this on start (the checkpoint's
binlog file is not in `SHOW BINARY LOGS`) and say clearly that an incremental
`refresh` on a tracking column or a rebuild is needed.

Binary chunks use the native layout. All supported platforms (amd64, arm64) are
little-endian, so a backup can be restored across architectures. `format_version`
guards against future layout changes.

---

## 6. Trade-offs

| | `file` (today) | `innodb` (proposed) |
|---|---|---|
| Full save speed | Fastest (one sequential write; about 10 s per 10 GB per the code comment) | Slower: SQL, redo log, doublewrite. To be measured |
| Checkpoint write volume | Smallest (per node) | Larger: dirty chunks, worst case all links (§4.3) |
| Checkpoint | Custom two-pass WAL | One transaction |
| Crash recovery | `doRecovery()` | InnoDB |
| Backups | Not included | Included |
| Replicas | Build their own index from the binlog; current per row | Load the primary's graph; current as of its last checkpoint until catch-up (Phase 2c) |
| Binlog / network | Index never in the binlog | Checkpoints and full saves replicate (§4.8) |
| Promotion | Works with file and position | Requires GTID mode; listener starts from `ckpt_gtids` (§4.8) |
| Disk space during full save | ~1× | ~2× until the old generation is deleted |
| Buffer pool | Not used | Index pages pass through during load and save |
| Credentials | Only for online updates | For every build, save and load |
| Who can read vectors | OS users with file access | SQL users granted `myvector.*` |

---

## 7. Test Plan

1. **Phase 3 lifecycle tests** (`scripts/pre-release-test.sh`) with
   `storage=innodb` on 8.4, 9.7 and 26.7: reload persistence,
   uninstall/reinstall, DROP stability, concurrent reads during install.
2. **Startup:** restart mysqld with online `storage=innodb` indexes. Check that
   they load once the server accepts connections, and that searches before that
   fail with `ER_MYVECTOR_INDEX_NOT_FOUND`. Repeat with wrong credentials: clear log errors,
   no crash.
3. **Crash consistency:** kill mysqld with `simulate_vector_crash` and with
   `kill -9` during a checkpoint and during a full save. On restart the index must
   match the last committed checkpoint, and the listener must catch up to the
   table. Compare KNN results with brute force.
4. **Corruption:** change one byte of a vector chunk and of a links chunk; the load
   must fail on the CRC and report it.
5. **Replication:** primary + replica with `super_read_only` on the replica.
   - After a checkpoint, the replica's ANN results match the primary's for the
     same query.
   - The replica writes no index rows and ignores table row events for
     `storage=innodb` indexes.
   - A full save (new generation) makes the replica reload.
   - The primary's listener ignores its own `myvector` writes.
   - With `binlog_row_image = MINIMAL`, binlog bytes per checkpoint are close to
     the chunk bytes written (no before-images).
6. **Promotion:** stop the primary, promote the replica, insert rows; the new
   writer applies every change after the last checkpoint's GTID set, and none
   twice.
7. **Versioned checkpoints:** kill the primary between two checkpoint
   transactions; on restart the index is as of the previous checkpoint and the
   leftover rows are removed. Set `myvector_checkpoint_txn_bytes` small enough to
   force several transactions per checkpoint, and check the result equals a
   single-transaction one. On a Group Replication cluster, a checkpoint larger than
   `group_replication_transaction_size_limit` succeeds.
8. **Follower snapshot:** have the replica's applier apply checkpoints while the
   follower is reading (throttle the follower); the follower never loads a mix of
   two checkpoints (compare with the primary's graph at the same `ckpt_seq`).
9. **GTID mode off:** follower mode refuses to start, with a clear error.
10. **Checkpoint timer:** with no binlog rotation, a checkpoint happens every
   `myvector_checkpoint_interval`; with `0`, only at rotation. Idle indexes are
   not rewritten.
11. **Backup:** xtrabackup or clone, restore, start; check that the index loads and
   catches up, and that the missing-binlog case is reported. Repeat with a backup
   taken on a replica.
12. **Write volume:** count bytes written per checkpoint (file vs innodb) for
   1k, 10k and 100k inserts per rotation, to confirm §4.3.
13. **Performance:** `myvectorbench.py` and `bench-concurrent-stress.py`, file vs
   innodb: full build time, checkpoint time under write load, reload time and
   QPS/recall (QPS and recall should be unchanged). The Movie Finder dataset
   (~1M vectors) is a realistic size.

---

## 8. Rollout

1. **Phase 1:** install SQL schema, full save and load, per-index `storage`
   option, deferred load at startup. No incremental checkpoints yet: a checkpoint
   does a full save. Component only.
2. **Phase 2:** versioned incremental checkpoints (§4.4), generations for builds
   (§4.6), checkpoint timer (§4.11), migration.
3. **Phase 2b:** replication (§4.8): follower mode with snapshot reads,
   `ckpt_gtids`, GTID-based listener start, promotion. Shipped together.
4. **Phase 2c:** catch-up in memory on followers (§4.8, D7).
5. **Phase 3:** write-volume and performance measurements, chunk-size defaults,
   docs. Decide whether `innodb` becomes the default and whether to port it to the
   plugin build.

---

## 9. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | Replicas and read-only servers | Replicate the index rows; replicas follow the primary's graph (§4.8) |
| D2 | Links write volume | Keep 256 KB links chunks; choose finer chunks or per-node rows only if Phase 3 measurements call for it |
| D3 | Backup freshness | Configurable checkpoint timer, `myvector_checkpoint_interval`, default 300 s (§4.11) |
| D4 | SQL path | Client connection first; `mysql_command_services` may come later |
| D5 | Chunk sizes | Start at 4 MB (vectors) and 256 KB (links); tune in Phase 3 |
| D6 | Schema name | Fixed: `myvector` |
| D7 | Replica freshness | Keep 300 s and document the lag; catch-up in memory follows in Phase 2c (§4.8) |
| D8 | Promotion | Follower mode requires GTID mode; promotion ships with follower mode; GTID for the file backend is a follow-up (§4.8) |
| D9 | Large checkpoints | Versioned chunks, transactions ≤ 64 MB, `index_meta` committed last, snapshot reads (§4.4) |

## 10. Open Questions

None at this stage. Phase 3 measurements may reopen D2 (links layout), D5 (chunk
sizes) and D7 (default interval).
