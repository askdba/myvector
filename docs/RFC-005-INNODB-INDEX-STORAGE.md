# RFC-005: Storing HNSW Indexes in InnoDB

**Author:** MyVector Engineering  
**Status:** Draft, revision 2  
**Target Version:** Component builds (MySQL 8.4, 9.7, 26.10) first; plugin decided in Phase 3  
**Last Updated:** 2026-11-03  

Revision 2 rewrites the first draft (merged in #218) after a max-effort review found
design flaws in it: a checkpoint feedback loop, replicas colliding with replicated
rows, broken promotion and fencing, checkpoints taken in the middle of a
transaction, and more. §11 lists what changed and why.

---

## 1. Abstract

Today MyVector saves each HNSW index as a set of files in `myvector_index_dir`.
Its own code makes those saves crash-safe: a two-pass checkpoint, a status file,
and a recovery replay. This RFC proposes a second storage backend that keeps the
index in InnoDB tables instead.

With the new backend, a checkpoint writes new versions of the changed parts of the
graph and then, in one final transaction, the GTID set they correspond to. The
graph and its position become visible together, or not at all. The index is then
covered by the tools DBAs already use (xtrabackup, the clone plugin, `mysqldump`),
and nothing needs a separate directory on the file system.

Searches don't change: the graph is still loaded into memory and searched there.
Only persistence moves.

The work comes in two stages:

- **Standalone (Phase 1–2).** Each server keeps its own index rows, written
  outside the binlog. Replicas build their own index from table changes, as they
  do today. Nothing replicates, so nothing can collide.
- **Replicated (Phase 2b).** Opt-in: index rows replicate, and replicas follow the
  primary's graph instead of building their own (§4.10).

### 1.1 Scope

- **In scope:** HNSW indexes (`HNSWMemoryIndex`, backed by
  `hnswlib::HierarchicalDiskNSW`), component build.
- **Out of scope:** brute-force KNN indexes (`KNNIndex`), which have no
  persistence today. The plugin build is a Phase 3 decision (§8).

### 1.2 Requirements

- **Writable servers in standalone mode.** Until replicated mode (Phase 2b), a
  server with `read_only` or `super_read_only` on can't hold a `storage=innodb`
  index (§4.8). Topologies with read-only replicas should keep `storage=file` until
  Phase 2b (**D13**).
- **GTID mode.** `storage=innodb` requires `gtid_mode = ON` on every server
  (**D11**). Checkpoints, resume after a restart, restore and promotion all use GTID
  sets. File names and positions are never compared across servers. An index with
  `storage=innodb` on a server with GTID mode off fails to build or load, with a
  clear error.
- **Prerequisite fix (Phase 0).** Today an index can be registered without a graph,
  and a search on it dereferences a null pointer, and a failed load reports SUCCESS
  ([#265](https://github.com/askdba/myvector/issues/265)). This RFC's deferred
  loads make that window longer, so #265 must be fixed first: a search on an index
  with no graph returns an error, and a failed load is reported as a failure.

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
| Header (M, ef_construction, max level, entry point, counts…) | First 96 bytes of `<index name>.hnsw.index` (`saveIndexHeader`). No metric field. |
| Vectors + level-0 links + labels | `.hnsw.index`, one fixed-size record per node (`size_data_per_element_`) |
| Upper-level links (level > 0) | `.hnsw.index.links` (directory: node id, size) + `.hnsw.index.links.data` |
| Checkpoint id (binlog file:pos or timestamp) | The `ckptid=` field of the 256-byte line in `.hnsw.index.status` (`include/hnswdisk.i:215`) |
| Checkpoint progress | Same `.status` line (`CKPT_BEGIN_INCR_PASS1` … `CKPT_CONSISTENT`) |
| In-flight checkpoint | `.hnsw.index.ckpt.state` |

Each node's level-0 record is laid out as `[level-0 links | vector | label]`. The
links part is small (`4 + 4·M0` bytes, where M0 = 2·M; e.g. 132 bytes for M=16).
The vector is usually most of the record (e.g. 3,072 bytes for 768 float
dimensions).

**Saves today:**

- **Build** (`build`): a full save, one large sequential write of all level-0 data,
  then the upper-level link files.
- **`save` action and checkpoints:** `saveIndex(dir)` with any option other than
  `build` runs `doCheckPoint()`, which writes only the header and the **dirty** nodes
  (`src/myvector.cc`, `HNSWMemoryIndex::saveIndex`). Right after a load nothing is
  dirty, so `save` writes almost nothing. There is no user-facing full save of an
  existing graph.

**Dirty tracking** (`include/hnswdisk.i:101`): the graph tracks three sets of dirty
internal node ids:

- **whole node:** a new or updated vector (`addNodeToFlushList`);
- **level-0 links only:** a node whose neighbor list changed because *another*
  vector was inserted next to it (`addNodeLinksLevel0ToFlushList`);
- **upper links:** the same for levels > 0.

For a links-only node the file backend writes just the links, not the vector. That
matters because **each insert changes the links of up to M existing neighbors per
level, and they can be anywhere in the graph** (`include/hnswdisk.h:620`, `:775`).

**When checkpoints run:** at every binlog rotation, `FlushOnlineVectorIndexes()`
waits until the online-update queue is empty (`gqueue_.wait_until_empty()`), then
calls `myvector_checkpoint_index()` for each online index. No online update runs
during a checkpoint. Nothing is saved at shutdown. The listener resumes from the
oldest checkpoint of any index (#190), comparing binlog file names and positions
(`isAfter()`, `src/myvector.cc:3182`).

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

`storage=file` keeps today's behavior exactly.

**Where the choice is stored:** in the options string, which lives in the vector
column's comment (`MYVECTOR(...)`) and is read back through the
`mysql.myvector_columns` view, like `type`, `dim` and `M`. That comment is the only
source of truth for which backend an index uses. `index_meta` holds the stored data
for `storage=innodb` indexes; it never decides the backend.

**No global switch.** There is no server setting that decides the backend of
existing indexes. If an index is created without `storage=`, the creation path writes
`storage=file` into the comment, as it already fills in a default type
(`MYVECTOR_DEFAULT_INDEX_TYPE`, `src/myvector.cc:1438`). So every index's comment
names its backend from the start, and changing a server default later can't move
existing indexes. A server default (`myvector_index_storage_default`) may exist
only for *new* indexes, and only because it is resolved into the comment at
creation.

**Downgrade.** A MyVector build older than this RFC doesn't know `storage=innodb`
and would treat such an index as file-backed, with no files. Before downgrading,
migrate every `storage=innodb` index back to `file` (§4.11). The release notes say
so.

### 4.2 Schema

The install SQL creates the schema, the tables and an internal account (§4.5). It
uses `CREATE ... IF NOT EXISTS` with `sql_log_bin = 0`. The install SQL runs on
every server (`INSTALL COMPONENT` isn't replicated), so each server creates its own
copy, and a re-run of the script neither fails nor wipes indexes.

Vectors and links are stored **separately**, because they change at very different
rates (§3): a vector changes only when its own row changes, while links change
whenever a nearby vector is inserted.

Chunks are **versioned**: a checkpoint never overwrites a chunk in place. It adds a
new version, and `index_meta.ckpt_seq` says which versions are current (§4.4).

```sql
CREATE TABLE IF NOT EXISTS myvector.index_meta (
  index_id        BIGINT UNSIGNED   NOT NULL AUTO_INCREMENT,
  index_name      VARBINARY(776)    NOT NULL,   -- db.table.column, exact bytes
  state           ENUM('active','dropped','migrating_from_file') NOT NULL,
  format_version  SMALLINT UNSIGNED NOT NULL,
  generation      BIGINT UNSIGNED   NOT NULL,   -- never reset, see 4.6
  ckpt_seq        BIGINT UNSIGNED   NOT NULL,   -- last committed checkpoint, see 4.4
  ckpt_chunks     INT UNSIGNED      NOT NULL,   -- chunk rows written by ckpt_seq, see 4.10
  header          VARBINARY(256)    NOT NULL,   -- saveIndexHeader() bytes
  header_crc32    INT UNSIGNED      NOT NULL,
  metric          TINYINT UNSIGNED  NOT NULL,   -- the header has no metric field
  dim             INT UNSIGNED      NOT NULL,
  vec_chunk_nodes INT UNSIGNED      NOT NULL,   -- nodes per vector chunk
  lnk_chunk_nodes INT UNSIGNED      NOT NULL,   -- nodes per links chunk
  ckpt_gtids      MEDIUMTEXT        NOT NULL,   -- GTID set the graph includes, see 4.4
  writer_uuid     CHAR(36)          NOT NULL,   -- server_uuid allowed to write, see 4.10
  ckpt_time       DATETIME(6)       NOT NULL,
  node_count      BIGINT UNSIGNED   NOT NULL,
  PRIMARY KEY (index_id),
  name_sha256     BINARY(32)        NOT NULL,   -- SHA2(index_name, 256), see notes
  UNIQUE KEY (name_sha256)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

-- Vector + label of each node, in large chunks. Rewritten only when one of its
-- nodes is new or has a new vector.
CREATE TABLE IF NOT EXISTS myvector.index_vectors (
  index_id     BIGINT UNSIGNED NOT NULL,
  generation   BIGINT UNSIGNED NOT NULL,
  chunk_id     INT UNSIGNED    NOT NULL,
  version      BIGINT UNSIGNED NOT NULL,  -- ckpt_seq of the checkpoint that wrote it
  data         LONGBLOB        NOT NULL,  -- (vector | label) per node
  crc32        INT UNSIGNED    NOT NULL,
  PRIMARY KEY (index_id, generation, chunk_id, version)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;

-- Level-0 links of each node, plus upper-level links, in smaller chunks.
CREATE TABLE IF NOT EXISTS myvector.index_links (
  index_id     BIGINT UNSIGNED NOT NULL,
  generation   BIGINT UNSIGNED NOT NULL,
  chunk_id     INT UNSIGNED    NOT NULL,
  version      BIGINT UNSIGNED NOT NULL,
  level0       LONGBLOB        NOT NULL,  -- level-0 links per node
  upper        LONGBLOB        NULL,      -- (node_id, len, bytes)* for level > 0
  crc32        INT UNSIGNED    NOT NULL,
  PRIMARY KEY (index_id, generation, chunk_id, version)
) ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
```

Schema notes:

- **`index_name` is binary.** In memory, index names are compared as exact strings.
  Under the default case- and accent-insensitive collation, `shop.Items.emb` and
  `shop.items.emb` would share one row. `VARBINARY` compares bytes, like the
  in-memory map. 776 bytes covers the longest name: 64 + 1 + 64 + 1 + 64
  characters at up to 4 bytes each.
- **Uniqueness is on a hash of the name.** A 776-byte key is too long for some
  InnoDB configurations: with `innodb_page_size = 4096` the key limit is 768 bytes,
  and the install SQL would fail. So the unique key is `name_sha256`, the SHA-256
  of the exact name bytes (32 bytes, fine with any page size and row format). Every
  lookup is by hash **and** compares `index_name` byte for byte, so even a hash
  collision can't make two indexes share a row: the second one fails to build with a
  clear error.
- **Chunk tables are keyed by `index_id`,** a small integer, not the name.
- **`DATETIME(6)`, not `TIMESTAMP`,** which fails after 2038-01-19.
- **Drop leaves a tombstone** (`state = 'dropped'`) instead of deleting the row, so
  the generation counter is never reset (§4.6, §4.11).

### 4.3 Chunking and write volume

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

Both are clamped so a **whole row** still fits in one statement when dumped with
`mysqldump --hex-blob`, which doubles every blob: the sum of all blobs in a row
(`data`; or `level0` plus `upper`) is at most
`(max_allowed_packet − 64 KB) / 2`, leaving 64 KB for the `INSERT` syntax and the
other columns. `upper` has no fixed size, so the links chunk builder stops adding
nodes when the row would pass the limit.

**Write volume per checkpoint**, in the example above:

| Workload since the last checkpoint | `file` (today) | `innodb` |
|---|---|---|
| 1,000 inserts | ~3 MB vectors + ≤ ~2 MB links ≈ 5 MB | new vector chunks (~4 MB) + nearly all links (~132 MB) |
| 1,000 random vector updates | ~3 MB vectors + links ≈ 5 MB | ~545 of ~740 vector chunks (~2.2 GB) + links |
| 10,000 random vector updates | ~32 MB + links | nearly all vector chunks (~3.1 GB) + links |

- **Inserts:** 1,000 inserts dirty up to 16,000 neighbors spread over the graph,
  so nearly every links chunk is dirty. The cost is bounded by the size of all links,
  about 4% of the index here.
- **Vector updates:** an `UPDATE` of a vector rewrites that node's vector in place,
  which dirties its 4 MB vector chunk. Random updates touch most vector chunks
  quickly, so the worst case is **the whole index**.

**Recommendation:** use `storage=innodb` for indexes that are mostly appended to.
Keep `storage=file` for indexes with frequent vector updates, or use smaller vector
chunks for them (Phase 3 measures the trade-off).

#### 4.3.1 Sizing

Operators need these numbers before turning `storage=innodb` on:

- **Redo log.** InnoDB fully redo-logs large blob writes. Splitting a checkpoint
  into bounded transactions (§4.4) limits the size of each transaction, not the
  redo volume: every checkpoint byte goes through the redo log. Size
  `innodb_redo_log_capacity` to several times the largest expected checkpoint (in
  the update-heavy example, gigabytes; the 8.0 default is 100 MB).
- **Tablespace growth.** Each checkpoint inserts new chunk versions and deletes old
  ones. Deleted versions take space until InnoDB purge removes them, and purge
  waits for the oldest open transaction. A long `mysqldump --single-transaction`
  therefore lets `myvector` tables grow by about one checkpoint's worth of data per
  checkpoint interval while it runs. Freed space is reused, but the files don't
  shrink without `OPTIMIZE TABLE`.
- **Binlog** (replicated mode only, §4.10): every checkpoint goes through the
  binlog. At ~136 MB per checkpoint and one checkpoint every 300 s, that's about
  39 GB per day, about 1.2 TB at the default 30-day `binlog_expire_logs_seconds`,
  on the primary and again on each follower.
- **Memory.** The checkpoint copy buffer (§4.4) holds up to one checkpoint's worth of
  chunks.

### 4.4 Checkpoints

**When:** `storage=innodb` checkpoints run **only on the timer** (§4.12), never at
binlog rotation. (In replicated mode a checkpoint writes to the binlog; if rotation
still triggered checkpoints, a large checkpoint would rotate the binlog and trigger
the next one, forever.) File-backed indexes keep today's rotation trigger.

**At a transaction boundary:** the listener takes a checkpoint only between
transactions, after a transaction's commit event (XID) has been applied, or while
idle. `ckpt_gtids` then contains only transactions the graph has applied in full.
A checkpoint in the middle of a large `INSERT ... SELECT` would record its GTID as
done while most of its rows were not, and those rows would be lost on restart or
promotion.

**Dirty node sets map to dirty chunks:**

- whole node → its vector chunk **and** its links chunk;
- level-0 or upper links only → its links chunk.

**Copy, then write.** The checkpoint holds the online-update queue paused (as today,
§3) only while it takes a snapshot of the graph's state. While paused, at a
transaction boundary, it:

1. copies the dirty chunks and the header into a buffer (a memory copy);
2. records the listener's applied GTID set **at that moment** as this checkpoint's
   `ckpt_gtids`;
3. **swaps the dirty sets**: it takes the current sets for this checkpoint and
   starts empty ones.

Then it lets updates continue and writes from the buffer. Updates that arrive during
the SQL writes go into the new, empty sets, so the next checkpoint persists them. The
buffer, the GTID set and the dirty sets it took all describe the same instant, so
the published `ckpt_gtids` never claims a change that isn't in the published
chunks. If the checkpoint fails, the sets it took are merged back into the current
ones, and the next checkpoint retries them.

Online updates pause for the copy, not for the SQL writes. If the dirty chunks don't
fit in `myvector_checkpoint_buffer_bytes` (default 512 MB), the checkpoint writes
with the queue still paused, as today, and logs that it did.

**Steps.** A checkpoint with sequence number `s = ckpt_seq + 1`:

1. **Clean up.** Delete rows with `version > ckpt_seq` for this index (left only by
   a crash during an earlier checkpoint), and any old versions an earlier
   checkpoint didn't finish deleting (step 5).
2. **Write the chunks** as version `s`, one statement per chunk (so no statement
   exceeds `max_allowed_packet`), in transactions of at most
   `myvector_checkpoint_txn_bytes` (default 64 MB).
3. **Publish**, in the last transaction, with a compare-and-set:

```sql
UPDATE myvector.index_meta
   SET ckpt_seq = ?, ckpt_chunks = ?, header = ?, header_crc32 = ?,
       ckpt_gtids = ?, ckpt_time = NOW(6), node_count = ?
 WHERE index_id = ? AND generation = ? AND ckpt_seq = ?      -- s - 1
   AND writer_uuid = @@server_uuid;                          -- see 4.10
```

4. **Check** that exactly one row changed. Zero rows means another build,
   checkpoint or server got there first: roll back, log, and retry at the next
   interval.
5. **Delete** the older versions of the chunks just written (`version < s`), in
   batches. If this is interrupted, step 1 of the next checkpoint finishes it.

**Readers** (load §4.7, followers §4.10) see, for each chunk, the newest version
`≤ ckpt_seq`, read in the same consistent snapshot as `index_meta`. Rows of an
unfinished checkpoint have `version > ckpt_seq`, so they are invisible.

Why versions rather than one transaction per checkpoint:

- **Atomic at any size.** A crash partway through leaves only invisible rows, and
  the index is exactly as of the last committed checkpoint. Splitting into several
  transactions *without* versions would leave some chunks new and some old under
  the old `ckpt_seq`: a corrupt graph.
- **Bounded transactions.** No checkpoint is one large transaction, so none
  exceeds Group Replication's `group_replication_transaction_size_limit` (150 MB by
  default), and replicas never lag behind one huge event.
- **One path.** Every checkpoint takes the same path whatever its size.

The new chunk versions and `ckpt_gtids` become visible together, with the commit of
step 3. After a crash, the index is exactly as of the last committed checkpoint, and
the listener resumes after `ckpt_gtids` (§4.7). `.ckpt.state`, `.status`,
`WriteCheckPointStatus()` and `doRecovery()` are not used in this mode.

**`ckpt_gtids`** is the set of transactions the graph includes. The listener keeps
it as its start set plus the GTID of each transaction it has applied in full; it
doesn't parse GTID events today, so this is new code. It is **not**
`@@gtid_executed` at checkpoint time, because the server is already ahead of the
listener. GTID sets with tags (MySQL 8.3 and later) must be handled.

**Concurrency: one persistence operation per index at a time.** Each index has a
persistence mutex (the per-index persistence lock RFC-004 §3.3 already calls for).
Every operation that writes the index's rows (timer checkpoint, full save, build,
migration, drop) holds it from step 1 (cleanup) through publication and failure
handling. Without it, a full save starting while a timer checkpoint is mid-batch
would pick the same `s`, and its step 1 would delete chunks the first one already
committed, which could then still publish. The timer skips an index whose mutex is
held and tries again at the next interval. The dirty sets a checkpoint took are
discarded only after step 3 commits (see **Copy, then write**).

### 4.5 How MyVector runs SQL

MyVector code runs inside UDFs and background threads. It cannot run SQL on the
caller's session.

**Decision (D4, revised):** use the component's in-process command service
(`mysql_command_factory`), which the MCP endpoint already uses
(`src/mcp/endpoint/component_mcp.cc:71`). No network connection and no password
file.

- **Account.** Sessions run as an internal account the install SQL creates, for
  example `'mysql.myvector'@'localhost'`, with `ACCOUNT LOCK` so nobody can log in
  as it. It has `SELECT, INSERT, UPDATE, DELETE` on `myvector.*`,
  `SESSION_VARIABLES_ADMIN` (for `sql_log_bin = 0` in standalone mode and
  `binlog_row_image` in replicated mode), and `SELECT` on the `performance_schema`
  tables in §4.10. The `myvector.cnf` credentials get no new privileges.
- **Short-lived sessions.** A session is opened for one operation (a checkpoint, a
  load, a build's writes) and closed after it. No storage session stays open, so
  none blocks `UNINSTALL COMPONENT` or the uninstall checks
  (`MYVECTOR_PREPARE_UNINSTALL`), which today allow only the listener's
  `Binlog Dump` sessions. Uninstall waits for in-flight storage operations to finish.
- **Isolation.** Every session sets `TRANSACTION ISOLATION LEVEL REPEATABLE READ`
  (§4.7).

### 4.6 Full save, builds and generations

**Full save.** A new user-facing full save of an existing graph writes every chunk,
as a checkpoint in which every chunk is dirty: all chunks get version `s` (§4.4).
Today's `save` action writes only dirty nodes (§3), so the full save is a new action.
Migration (§4.11) uses it.

**Build** (`build`) replaces the graph: internal node ids are reassigned, and the
chunk count and chunk sizes can change. It writes a **new generation**:

1. Read the index's `index_meta` row, including a tombstone. `g` = its generation
   + 1. With no row at all, insert one first with `state = 'dropped'`,
   `generation = 0` and `writer_uuid = @@server_uuid` (in replicated mode, only a
   writer may do this, §4.10), and use `g = 1`. The generation counter never goes
   back: a drop followed by a rebuild gets a new number, so a follower can always
   tell it apart from the old graph.
2. Delete rows with `generation ≥ g` for this index (left by an earlier build that
   stopped before publishing, possibly in the same server process).
3. Capture the starting point: read `@@gtid_executed` **before** opening the
   consistent snapshot the build reads the table in. Transactions that commit in
   between are replayed by the listener afterward. Most replays are harmless:
   inserts of existing keys update in place, and deletes of missing keys do nothing.
   **One case isn't:** a key inserted and then deleted inside that window is absent
   from the snapshot, so replaying its insert adds a node. If the snapshot already
   filled the index's capacity (`size=`), that insert fails before its delete is
   reached. So during the replay window (until the listener has passed the GTID
   set read right after the snapshot opened), an insert that fails for capacity is
   **deferred**, not reported: it is retried when the window ends, after the
   window's deletes have been applied, and reported only if it still fails.
4. Write all vector and links chunks for `g` (version 1) in bounded transactions.
5. Publish with a compare-and-set:

```sql
UPDATE myvector.index_meta
   SET generation = ?, ckpt_seq = 1, state = 'active', header = ?, ...
 WHERE index_id = ? AND generation = ?                      -- g - 1
   AND writer_uuid = @@server_uuid;
```

   Exactly one row must change; otherwise roll back and fail the build.
6. Delete generation `g-1` in batches, in the background.

Readers only ever see the generation that `index_meta` points to.

### 4.7 Load and startup

**Steps:**

1. `START TRANSACTION WITH CONSISTENT SNAPSHOT`. The session is at
   `REPEATABLE READ` (§4.5); under `READ COMMITTED`, `WITH CONSISTENT SNAPSHOT` has
   no effect and each read gets a fresh snapshot. Every read below is in this one
   snapshot, so a checkpoint or replication applier running at the same time can't
   give a mix of two checkpoints.
2. Read `index_meta`. Check `state`, `format_version`, `header_crc32`, and that
   `dim`, `metric` and the header's M match the index definition.
3. **Validate the header** with the same structural checks the file loader applies
   (`include/hnswdisk.i`): counts within `max_elements`, the entry point and max
   level within range, sizes consistent with `dim` and M. The stored data is
   untrusted: anyone who can write to `myvector.*` could otherwise point the graph
   at memory outside it.
4. Scan `index_vectors` and `index_links` for `(index_id, generation)` in
   `chunk_id` order, taking the newest version `≤ ckpt_seq` of each chunk. Check
   each chunk's CRC, that no chunk is missing, and that every node id in `upper` is
   below `node_count`, with sizes matching its level. **Validate every adjacency
   list**, level 0 and upper: its count is at most `maxM0_` (level 0) or `maxM_`
   (upper levels), every neighbor id is below `node_count`, and every neighbor
   exists at that level (its level ≥ the list's level). The search code reads as
   many entries as a list's count says, without checking the allocation, so a
   well-formed but oversized count must never reach it. Copy the parts into place,
   allocate `linkLists_`, and rebuild `element_levels_`.
5. Rebuild `label_lookup_` (row key → node) and the deleted-node count
   (`num_deleted_`), as the file loader does (`include/hnswdisk.i:1098`).
6. Set the index's resume point to `ckpt_gtids`.

On any check failure, the load **fails**: the index is not registered with an empty
graph, the error is returned, and `myvector_index_status` shows it as not loaded with
the reason. The index must then be rebuilt, following RFC-004's principle that
indexes are secondary, rebuildable structures. (This depends on the Phase 0 fix,
#265.)

**When loading happens:** components are loaded during server start, before the
server accepts connections. Loading follows the listener's pattern: it retries until
the server is ready (#186), then loads online indexes (`OpenAllOnlineVectorIndexes()`).

- Other indexes load on `MYVECTOR_INDEX_LOAD`, as today.
- Until an index has loaded, searches on it return an error (#265), never a crash.
- Loading a `storage=innodb` index does not depend on the listener's
  `binlog_state.json` or its `server_uuid` check. A server started from a copied
  datadir (new `server_uuid`) still loads its InnoDB-stored indexes; their resume
  point is `ckpt_gtids`, which is valid on any server. In standalone mode it then
  rebinds `writer_uuid` to itself (§4.8).

### 4.8 Standalone mode (Phase 1–2)

In standalone mode, the default, **every write to `myvector.*` runs with
`sql_log_bin = 0`:**

- Each server, replicas included, keeps its own index rows. A replica builds its own
  graph from table changes through its listener, exactly as today, and checkpoints
  it into its own local rows.
- Nothing about the index enters the binlog, so a primary's rows never reach a
  replica, and nothing can collide or stop a replication applier.
- A backup taken on any server includes that server's index.
- `writer_uuid` is always the local `server_uuid`. A server whose rows name
  another UUID, after a restore, a copied datadir or a clone, rebinds them: when it
  loads an index in standalone mode and finds `writer_uuid ≠ @@server_uuid`, it sets
  `writer_uuid = @@server_uuid` (with `sql_log_bin = 0`) and logs it. Otherwise
  every checkpoint's compare-and-set would fail. Replicated mode never rebinds
  automatically; that's what takeover is for (§4.10).

The cost: every server does its own build and checkpoints, as today. Replicated mode
(§4.10) removes that cost and brings its own rules.

**Read-only servers are not supported in standalone mode (D13).** Standalone mode
writes on every server, but replicas usually run with `super_read_only = ON`, which
blocks writes from every session, and plain `read_only = ON` blocks the internal
account too (it has no `CONNECTION_ADMIN`). The column comment replicates, so a
replica can't pick another backend for itself. So:

- A server with `read_only` or `super_read_only` on **refuses** to build, load or
  checkpoint a `storage=innodb` index in standalone mode. The error names the
  setting and says to use `storage=file` until replicated mode.
- The same check runs when the setting changes at runtime: if a server becomes
  read-only, its `storage=innodb` indexes stop checkpointing and report it in
  `myvector_index_status`; they keep serving searches from memory until restart.
- Creating or migrating an index to `storage=innodb` warns if any replication
  channel is configured on this server, because its replicas will refuse the index.
- Replicated mode (Phase 2b) is the supported path for read-only replicas:
  followers never write.

Making read-only replicas work in standalone mode (for example with a per-server
file fallback) was considered and rejected for now, to keep one storage path per
index.

### 4.9 Security

Index rows are copies of the user's vectors, in another schema. Anyone who can read
`myvector.*` can read every indexed vector, whatever their grants on the source
tables. Embeddings can reveal information about the text or images they came from.
Today the files are protected by OS permissions instead, which is a different
exposure.

Rules:

- Grant `myvector.*` only to the internal account (§4.5). The install SQL grants
  nothing else, and the docs say why.
- **Global grants include it.** An account with global `SELECT` (`ON *.*`) can read
  `myvector.*`. With `partial_revokes = ON`, revoke it explicitly
  (`REVOKE SELECT ON myvector.* FROM ...`); otherwise avoid global `SELECT` grants.
- `mysqldump` of the whole server includes `myvector`; dumps must be protected like
  the source data.
- In replicated mode, index rows are in binlogs and relay logs on every server.
  `binlog_encryption` covers them if it is on.
- Anyone who can write to `myvector.*` can corrupt an index. The CRCs catch
  accidental changes, and the structural checks (§4.7) stop corrupt data from
  reaching memory outside the graph. Neither is a defense against a deliberately
  wrong but well-formed graph.

### 4.10 Replicated mode (Phase 2b)

```text
myvector_index_replication = OFF | ON      (default: OFF; same on every server)
```

With `ON`, index writes are logged normally (no `sql_log_bin = 0`), so index rows
replicate, and replicas follow the primary's graph instead of building their own.
It applies to all `storage=innodb` indexes on the server. The setting must match
across the topology; a follower that finds a primary-written index while its own
setting is `OFF` (or the reverse) logs an error and refuses to use the index.

#### Who writes: compare-and-set on `writer_uuid` (D10)

`index_meta.writer_uuid` names the one server allowed to write an index's rows.
**Every** write to an index's rows (checkpoint, build, full save, migration, drop,
version cleanup) runs in a transaction that first changes `index_meta` with
`WHERE ... AND writer_uuid = @@server_uuid` (§4.4, §4.6). If that changes zero
rows, the transaction rolls back and the server writes nothing.

So the role comes from the data, not from guesses about the topology:

- A server is the **writer** for an index if `writer_uuid` is its own `server_uuid`.
- Otherwise it is a **follower** for that index.

A replica whose `read_only` was left off, whose replication SQL thread stopped, or
whose settings are wrong still can't write: `writer_uuid` holds the primary's UUID,
so every compare-and-set fails. This also avoids the GTID-UUID problem: Group
Replication stamps every member's transactions with the group name, but
`writer_uuid` compares `@@server_uuid`, which is per member.

**Claiming the writer role:**

- **New index:** the server that runs the first build creates the row with its own
  UUID (§4.6 step 1). In replicated mode a server refuses to create index rows if it
  has a replication channel configured (`performance_schema.replication_connection_configuration`,
  Group Replication channels excluded), or if it is a Group Replication secondary.
- **Group Replication (single-primary):** the primary claims automatically: when the
  GR member role (`performance_schema.replication_group_members`) says it is
  PRIMARY, it sets `writer_uuid = @@server_uuid` for each index. GR certification
  orders competing claims. The old primary's compare-and-sets then fail, and it
  becomes a follower.
- **Multi-primary Group Replication:** not supported in replicated mode. The server
  refuses to start replicated mode with a clear error. (Standalone mode works.)
- **Asynchronous replication: explicit takeover.** After promoting a replica, the
  operator runs `CALL mysql.MYVECTOR_INDEX_TAKEOVER();` on the new primary. It sets
  `writer_uuid` to this server for every index and switches the server to writer.
  There is no automatic takeover: two servers that both believe they were promoted
  must not both claim.

  **The old primary must be fenced first.** `writer_uuid` is checked against each
  server's own copy of `index_meta`. If the old primary is still running but
  unreachable, its copy still names itself, so it would keep checkpointing while the
  new primary does too. Async replication has no consensus, so no mechanism inside
  the database can prevent this; the same partition would also let both servers
  accept user writes. So the rule is the one async failover already needs:
  **stop or fence the old primary** (`super_read_only = ON`, stop mysqld, or
  isolate it) **before** running the takeover. The takeover procedure's
  documentation and its output say so. If an unfenced old primary later rejoins as a
  replica, the replicated takeover changes its `writer_uuid`, its compare-and-sets
  then fail and it becomes a follower; its own divergent checkpoints are errant
  transactions to clean up, like any other writes it took during the partition.
  Group Replication avoids all of this, because its claims go through certification
  (above).

**Role changes:**

- **follower → writer** (claim or takeover): the server's graph is the old
  primary's last checkpoint. The listener resumes from `ckpt_gtids` (below).
- **writer → follower** (its claim was overtaken): drop in-memory changes since the
  last checkpoint (the new writer's graph is the authority) and reload (§4.7).

#### Writer side

- The writer's listener ignores events on the `myvector` schema (its own
  checkpoints coming back).
- The storage session sets `binlog_row_image = MINIMAL`. Checkpoints insert new
  chunk versions, which log the new row under any setting, but they also delete old
  versions and old generations. Under the default `FULL`, each row delete logs the
  whole deleted blob again; `MINIMAL` logs only the key.
- **Never set `binlog_row_image = MINIMAL` globally.** MyVector's own listener
  needs every column of user-table row events and skips rows events that lack them
  (`src/component_src/myvector_binlog_service.cc:1083`), so deletes and vector
  updates would stop reaching every online index, on both backends.
- **Followers' own binlogs** log the replicated deletes with the follower's own row
  image, normally `FULL`, so the deleted blobs land in every follower's binlog (and
  must stay there, given the point above). Size follower binlogs accordingly
  (§4.3.1).

#### Follower side

A follower ignores user-table row events for indexes it doesn't write, and **polls
`index_meta` once a second** instead of reading its own binlog. Following doesn't
need `log_replica_updates` or a binlog event to arrive.

**Promotion does.** A promoted follower resumes from `ckpt_gtids` by reading its
*own* binlog (*Resume and promotion*, below). That binlog contains the replicated
user transactions only with `log_replica_updates = ON`, and only while they are
retained. So a follower that may be promoted needs `log_replica_updates = ON` and
binlogs kept for at least the checkpoint interval plus the longest expected
replication delay. `MYVECTOR_INDEX_TAKEOVER` checks that every transaction after
`ckpt_gtids` is still in the local binlog (the `@@gtid_purged` check in §5); if not,
it refuses and says the index must be rebuilt.

1. Open a consistent snapshot (§4.7) and read `index_meta`. The replication applier
   may be ahead; the follower uses whatever checkpoint the snapshot shows and may
   skip some.
2. If `state` is `dropped`, unload the index. If `generation` changed, reload the
   whole index (§4.7).
3. Otherwise, if `ckpt_seq` advanced: count the chunk rows with
   `version = ckpt_seq`. If the count is less than `ckpt_chunks`, the applier hasn't
   committed them all yet: wait and retry, and log an error if it stays short for
   longer than `myvector_follower_wait_timeout`.
4. Read every chunk with `last applied seq < version ≤ ckpt_seq` (newest version
   per chunk). Validate the header and the chunks (§4.7 steps 3–4). Copy them into
   the in-memory graph under the index's exclusive lock (`lockExclusive()`). Update
   the header, rebuild `label_lookup_` and `num_deleted_`, and allocate `linkLists_`
   for new upper-level nodes. Searches wait during the copy.
5. Remember `ckpt_seq` as the last applied seq.

**Requirements a follower checks at start, refusing replicated mode with an error
if any fails:**

- `replica_preserve_commit_order = ON` when `replica_parallel_workers > 1`.
  Otherwise the applier could commit the `index_meta` transaction before an earlier
  chunk transaction, and step 3's count only checks the latest checkpoint. (`ON` is
  the default since 8.0.27.)
- No replication filter drops `myvector.*` (`replicate_do_db`,
  `replicate_ignore_db`, `replicate_wild_*_table`). Otherwise the index would
  freeze silently.

A follower's graph is never a mix of its own changes and the primary's. HNSW inserts
are not deterministic (random levels, parallel workers), so a graph built on the
replica would not match the primary's node for node.

#### Freshness (D7)

A follower's index is as of the primary's last checkpoint: up to
`myvector_checkpoint_interval` plus replication delay behind. Rows inserted since
are in the table but not yet in the replica's ANN results. Today a replica's index
follows each row; this is a real change and must be in the docs. Lowering the
interval is expensive, because each checkpoint costs about the same whatever its
interval (§4.3).

The real fix is **catch-up in memory** (Phase 2c):

1. The follower loads a checkpoint, then applies the table changes after its
   `ckpt_gtids` in memory, as a writer does. Replicas are current again.
2. At the next checkpoint it reloads the chunks the primary changed **plus the
   chunks it changed locally** (the dirty sets track them), takes the primary's
   header, and rebuilds `label_lookup_` in full, which drops the keys of nodes it
   had inserted locally. Its graph then equals the primary's exactly.
3. It restarts its stream from the new `ckpt_gtids` and continues.

#### Resume and promotion

This applies in standalone mode too (from Phase 1): every `storage=innodb` index
resumes from its `ckpt_gtids`.

**One stream, both backends.** The listener reads one binlog stream for all online
indexes. File-backed indexes resume from a file and position on this server's
binlog; InnoDB-stored indexes from a GTID set. The stream must start early enough
for **every** index, whichever checkpoint is older:

1. For each InnoDB-stored index, find the earliest binlog file on this server that
   contains a transaction not in its `ckpt_gtids`. Each binlog file starts with a
   `Previous_gtids` event listing everything before it, so this is the first file
   whose successor's `Previous_gtids` is not contained in `ckpt_gtids`. The index's
   start is that file, at position 4.
2. For each file-backed index, its start is its saved file and position.
3. The stream starts at the **earliest** of all these positions, by file and
   position. Positions on the same server's binlog are comparable.
4. Each event is then applied per index: file-backed indexes skip it by `isAfter()`
   as today, InnoDB-stored ones skip it if its GTID is in their `ckpt_gtids`.

If an InnoDB-stored index's start file was purged, the §5 check reports it and the
index needs a rebuild; the other indexes are unaffected.

- On any restart, takeover or claim, the listener starts its stream from
  `ckpt_gtids`, using GTID-based `mysql_binlog_open()` (`MYSQL_RPL_GTID` and an
  encoded GTID set; the listener calls `mysql_binlog_open()` at
  `src/component_src/myvector_binlog_service.cc:2363` today, with file and
  position).
- For `storage=innodb` indexes, whether an event is already in the index is
  decided by GTID-set membership, not by `isAfter()`. `isAfter()` compares binlog
  file names as strings, which is meaningless across servers: on a promoted replica
  it would skip every event, and the index would freeze.

### 4.11 Migration and drop

A new procedure, `CALL mysql.MYVECTOR_INDEX_MIGRATE('db.t.v', 'innodb' | 'file')`,
does both directions. It runs in the **caller's** session: the `ALTER TABLE` that
changes the column comment uses the caller's privileges and metadata locks, not the
internal account's.

**file → innodb:**

1. Load the index from its files (if not loaded).
2. Full save into InnoDB (§4.6, as a new generation), with
   `state = 'migrating_from_file'`.
3. Change the comment to `storage=innodb`.
4. Set `state = 'active'`.

**innodb → file:**

1. A **real full save** to files (a build-style write of every node, then fsync). Not
   today's `save` action, which would write only a header (§3).
2. Change the comment to `storage=file`.
3. Set the `index_meta` row to `state = 'dropped'` and delete its chunks.

**Other servers.** The comment change replicates, but the full save in step 1 ran
only on the server that ran the procedure. Every other server finds the new comment
with its own index still in InnoDB, so the load rules below make each server export
its own InnoDB index to files before it uses them. A server never switches to files
it didn't just write.

**Rules for loading during or after a crash:**

| Comment says | `index_meta` | Load from |
|---|---|---|
| `innodb` | `active` | InnoDB |
| `innodb` | `migrating_from_file`, or no row at all, and files exist | the files, then finish the migration. Standalone mode only: this server's files are its own. This also covers a replica that received the comment change through replication. |
| `innodb` | `dropped` | nothing: the index was dropped |
| `file` | `active` | InnoDB; then a real full save to this server's files, then set the row to `dropped` and use the files. Covers a replica that received the comment change, and a migration that stopped after step 2. In replicated mode a follower exports the writer's graph the same way and leaves the row to the writer. |
| `file` | `migrating_from_file` | the files (a file → innodb migration that stopped before step 3); then set the row to `dropped` |
| `file` | none or `dropped` | the files |

A load never falls back to files when the comment says `innodb` and the row is
`dropped` or `active`. So a dropped index can't come back from stale files left by an
earlier migration.

**Drop** (`MYVECTOR_INDEX_DROP`): set `state = 'dropped'` first (one transaction, so
the index is gone at once and the generation counter is kept), then delete its chunks
in batches, then remove any index files.

### 4.12 Checkpoint timer

```text
myvector_checkpoint_interval = 300      (seconds; storage=innodb only)
```

- **Applies only to `storage=innodb`.** File-backed indexes keep today's
  rotation-triggered checkpoints, so this RFC doesn't change how they behave.
- **Checked often enough.** The listener's binlog session asks for a heartbeat
  every second (`@master_heartbeat_period`), but under write load no heartbeats
  arrive, so the timer is also checked after every applied event. When it fires, the
  checkpoint waits for the next transaction boundary (§4.4).
- **Idle indexes still advance.** An index with no dirty nodes writes no chunks, but
  still updates `ckpt_gtids` and `ckpt_time` in `index_meta` (a one-row
  compare-and-set). Its resume point keeps up with the listener, so binlog expiry
  can't leave it pointing at purged transactions.

The interval bounds how stale a backup's index is (§5), how far a follower lags
(§4.10) and how much must be replayed after a crash.

**Indexes without online updates** have no listener and no timer checkpoints. They
are persisted by a build or a full save only, and their `ckpt_gtids` is the
`@@gtid_executed` captured when the build started (§4.6 step 3).

---

## 5. Backup and Restore

A physical backup (xtrabackup, clone) or a consistent `mysqldump`
(`--single-transaction --hex-blob`) now contains the index with its `ckpt_gtids`.
Restoring a dump needs a `max_allowed_packet` at least as large as the source
server's; the row-size clamp (§4.3) guarantees that every dumped row fits in it.

After a restore, the listener resumes from `ckpt_gtids`. The transactions after it
must still be in the restored server's binlog. **Detect it exactly:** if
`@@gtid_purged` contains any transaction not in `ckpt_gtids`, changes the index
needs are gone. Report it clearly and require a **rebuild**. An incremental
`refresh` on a tracking column is not enough: it picks up inserted and updated rows,
but not deletes, so rows deleted in the gap would stay in the index. (A clone
doesn't copy the donor's binlogs, so this is the usual case after cloning.) (A
file-name check would pass for an unrelated binlog with the same name.)

The index is only as fresh as its last checkpoint: at most
`myvector_checkpoint_interval` old.

Binary chunks use the native layout. All supported platforms (amd64, arm64) are
little-endian, so a backup can be restored across architectures. `format_version`
guards against future layout changes.

---

## 6. Trade-offs

| | `file` (today) | `innodb` (proposed) |
|---|---|---|
| Full save speed | Fastest (one sequential write; about 10 s per 10 GB per the code comment) | Slower: SQL, redo log, doublewrite. To be measured |
| Checkpoint write volume | Smallest (per node) | Larger: dirty chunks. All links for inserts; up to the whole index for scattered vector updates (§4.3) |
| Checkpoint trigger | Binlog rotation | Timer, at a transaction boundary (§4.4, §4.12) |
| Crash recovery | `doRecovery()` | InnoDB |
| Backups | Not included | Included |
| GTID mode | Not required | Required (D11) |
| Replicas, standalone mode | Build their own index | Build their own index, in their own rows |
| Replicas, replicated mode | n/a | Follow the primary's graph; current as of its last checkpoint until catch-up (Phase 2c) |
| Promotion, replicated mode | n/a | GR: automatic. Async: `MYVECTOR_INDEX_TAKEOVER` |
| Redo and binlog volume | Small | Size them (§4.3.1) |
| Disk space during a build | ~1× | ~2× until the old generation is deleted |
| Buffer pool | Not used | Index pages pass through during load and save |
| Credentials | `myvector.cnf` for the listener | Unchanged; storage uses an internal locked account |
| Who can read vectors | OS users with file access | SQL users with read access to `myvector.*` (§4.9) |

---

## 7. Test Plan

**Phase 0**

1. **#265:** restart, `MYVECTOR_INDEX_STATUS`, then an ANN query: an error, no
   crash. A load from missing or corrupt files reports failure.

**Phase 1–2 (standalone)**

2. **Lifecycle** (`scripts/pre-release-test.sh` Phase 3 tests) with
   `storage=innodb` on 8.4, 9.7 and 26.10: reload persistence,
   uninstall/reinstall (with storage operations in flight), DROP stability,
   concurrent reads during install. Re-running the install SQL keeps every index.
3. **Startup:** restart with online `storage=innodb` indexes; they load once the
   server accepts connections, and searches before that return an error. A server
   started from a copied datadir (new `server_uuid`) loads them, rebinds
   `writer_uuid` and keeps checkpointing.
4. **Crash consistency:** `kill -9` during each step of a checkpoint (§4.4), a build
   and a full save. On restart the index matches the last committed checkpoint, the
   leftovers are cleaned up, and the listener catches up. Compare KNN results with
   brute force. (The file backend's `simulate_vector_crash` hook doesn't cover this
   path, so the tests add their own crash points.)
5. **Transaction boundary:** fire the timer during a 10,000-row
   `INSERT ... SELECT`; kill and restart; every row is in the index.
6. **Corruption and validation:** change one byte of a vector chunk, a links chunk
   and the header; set the header's entry point and an `upper` node id out of range.
   Each load fails with a clear error, and nothing reads outside the graph.
7. **Versioned checkpoints:** with `myvector_checkpoint_txn_bytes` small enough to
   force several transactions, the result equals a single-transaction checkpoint.
8. **No feedback loop:** a checkpoint larger than `max_binlog_size` (replicated
   mode) triggers no further checkpoint; standalone checkpoints write nothing to the
   binlog.
9. **Copy buffer:** online-update latency during a checkpoint, with the dirty chunks
   inside and outside `myvector_checkpoint_buffer_bytes`. Updates that arrive during
   the SQL writes are persisted by the next checkpoint: kill after it, restart, and
   every row is in the index. A failed checkpoint loses no dirty nodes.
10. **Idle index:** an index with no writes for longer than binlog expiry still
    resumes after a restart.
11. **Standalone replication:** primary + replica; neither sees the other's index
    rows, and both answer correctly. With `read_only` and with `super_read_only` on
    the replica, it refuses the index with the documented error; the primary warns
    when the index is created; a server turned read-only at runtime stops
    checkpointing and reports it.
12. **Schema:** two indexes whose names differ only in case stay separate; a
    194-character name works; `ckpt_time` past 2038 works; the install SQL works
    with `innodb_page_size = 4096`. A `mysqldump --hex-blob` of the largest
    possible rows restores with the source's `max_allowed_packet`.
13. **Serialization:** start a full save while a timer checkpoint is mid-batch;
    one waits for the other, and the published index is consistent.
14. **Build replay at capacity:** fill an index to `size=`, then insert and
    delete one key between the build's GTID capture and its snapshot; the build
    and the replay succeed.
15. **Adjacency validation:** a links chunk with an oversized list count, a
    neighbor id out of range, or a neighbor at a lower level fails the load.
16. **Mixed backends:** a file-backed and an InnoDB-stored online index, with the
    InnoDB checkpoint older; restart; neither index misses a change.
17. **Migration:** both directions, with a kill after each step; the load follows
    the table in §4.11 every time. A drop during a migration leaves nothing. A
    file → innodb → file round trip never loads stale files. On a replica that
    receives the comment change, innodb → file exports its own index first.
18. **GTID mode off:** `storage=innodb` refuses to build or load, with a clear
    error.

**Phase 2b (replicated)**

19. **Followers:** after a checkpoint, a replica's ANN results match the primary's.
    A build (new generation) and a drop then rebuild (generation keeps rising) make
    the replica reload, and a drop unloads.
20. **Commit order:** with parallel workers and `replica_preserve_commit_order = OFF`,
    replicated mode refuses to start; with `ON`, a throttled follower never loads a
    mix of two checkpoints.
21. **Filters:** a replication filter on `myvector.*` makes replicated mode refuse
    to start.
22. **Writer protection:** a replica with `read_only = OFF`, with its SQL thread
    stopped, or running build, drop or migrate, writes no index rows, and replication
    doesn't stop.
23. **Group Replication:** only the primary writes; after a primary switch the new
    primary claims and the old one follows; multi-primary mode is refused.
24. **Takeover without logs:** with `log_replica_updates = OFF` (or the needed
    binlogs purged), `MYVECTOR_INDEX_TAKEOVER` refuses and says a rebuild is needed.
25. **Async promotion:** fence the primary, promote the replica, run
    `MYVECTOR_INDEX_TAKEOVER`, insert rows. The new writer applies every change after
    `ckpt_gtids`, none twice, and checkpoints. Repeat with replica binlog file names
    that sort lower than the old primary's.
26. **Binlog volume:** with `MINIMAL` on the storage session, the primary's binlog
    bytes per checkpoint are close to the chunk bytes inserted. Measure the
    followers' binlogs too.

**Phase 3 (measurement)**

27. **Restore:** xtrabackup or clone, restore, start; the index loads and catches up,
    and the purged-GTID case is reported and requires a rebuild (including a delete
    inside the gap). Repeat with a backup from a replica.
28. **Write volume:** bytes written per checkpoint (file vs innodb) for 1k, 10k and
    100k inserts and for random vector updates, to confirm §4.3.
29. **Performance:** `myvectorbench.py` and `bench-concurrent-stress.py`, file vs
    innodb: build time, checkpoint time and update stalls under write load, reload
    time, QPS and recall (which should be unchanged). The Movie Finder dataset
    (~1M vectors) is a realistic size.

---

## 8. Rollout

0. **Phase 0:** fix [#265](https://github.com/askdba/myvector/issues/265) (search
   on an index with no graph; failed loads reported as SUCCESS).
1. **Phase 1 (standalone core):** install SQL (schema, internal account),
   command-service sessions, `storage` option resolved into the comment, GTID
   requirement, `ckpt_gtids` tracking and the GTID-based listener start and skip
   check (§4.10, *Resume and promotion*; standalone resume and restore need them),
   build, full save, load with validation, versioned checkpoints on the timer at
   transaction boundaries, copy buffer, deferred load at startup,
   `sql_log_bin = 0` for every write. Component only.
2. **Phase 2:** `MYVECTOR_INDEX_MIGRATE`, crash-safe migration and drop, sizing docs
   (§4.3.1).
3. **Phase 2b (replicated mode):** `myvector_index_replication`, `writer_uuid`
   compare-and-set, GR claim, `MYVECTOR_INDEX_TAKEOVER`, follower polling. Shipped
   together.
4. **Phase 2c:** catch-up in memory on followers (§4.10).
5. **Phase 3:** write-volume and performance measurements, chunk-size defaults,
   docs. Decide whether `innodb` becomes the default for new indexes, and whether to
   port it to the plugin build.

---

## 9. Decisions

| # | Question | Decision |
|---|---|---|
| D1 | Replicas | Standalone by default: each server keeps its own rows, outside the binlog (§4.8). Replicating index rows is opt-in, in Phase 2b (§4.10) |
| D2 | Links write volume | Keep 256 KB links chunks; choose finer chunks or per-node rows only if Phase 3 measurements call for it |
| D3 | Checkpoint trigger | `storage=innodb`: timer only, default 300 s, at transaction boundaries; idle indexes still advance. File backend unchanged (§4.4, §4.12) |
| D4 | SQL path | In-process command service with an internal locked account, short-lived sessions (§4.5). *Revised: was a client connection* |
| D5 | Chunk sizes | Start at 4 MB (vectors) and 256 KB (links), clamped to `max_allowed_packet / 2`; tune in Phase 3 |
| D6 | Schema name | Fixed: `myvector` |
| D7 | Replica freshness | Keep 300 s and document the lag; catch-up in memory follows in Phase 2c (§4.10) |
| D8 | Promotion | GTID-based resume and skip check; ships with replicated mode (§4.10) |
| D9 | Large checkpoints | Versioned chunks, transactions ≤ 64 MB, compare-and-set publish of `index_meta` last, snapshot reads (§4.4) |
| D10 | Writer role | `writer_uuid` compare-and-set on every write. GR primary claims automatically; async promotion uses `MYVECTOR_INDEX_TAKEOVER`; multi-primary GR unsupported in replicated mode (§4.10). *Revised: was read_only and channel heuristics* |
| D11 | GTID mode | Required for every `storage=innodb` index (§1.2) |
| D12 | Online-update stalls | Copy dirty chunks to a buffer under the pause, write after it (§4.4) |
| D13 | Read-only servers, standalone mode | Unsupported until replicated mode: a read-only server refuses `storage=innodb` indexes with a clear error (§4.8) |

## 10. Open Questions and Known Limitations

Phase 3 measurements may reopen D2 (links layout), D5 (chunk sizes) and D7 (default
interval).

Known limitations, to be revisited:

- Update-heavy indexes write far more with `innodb` than with `file` (§4.3).
- Replicated mode doesn't support multi-primary Group Replication.
- In standalone mode, read-only servers (usually replicas) can't hold
  `storage=innodb` indexes, so Phase 1–2 suits topologies without read-only
  replicas (D13).
- GTIDs for the file backend would need a file format change (its checkpoint id
  lives in a 256-byte status line); not part of this RFC.

---

## 11. Revision History

**Revision 1** (2026-10-08, #218): first draft.

**Revision 2** (2026-11-03): rewritten after a max-effort review of revision 1 that
found 15 verified issues. Main changes:

| Problem in revision 1 | Revision 2 |
|---|---|
| Checkpoints at binlog rotation, writing to the binlog: a large checkpoint rotated the binlog and triggered the next one, forever | Timer-only checkpoints for `storage=innodb`; standalone writes are outside the binlog (§4.4, §4.8) |
| Index rows replicated from Phase 1, but roles came in Phase 2b: replicas' own checkpoints collided with the primary's rows and stopped replication | Standalone Phase 1; replication is opt-in in Phase 2b (§4.8, §4.10) |
| Promotion compared binlog file names across servers, so a promoted replica skipped every event | GTID sets for resume and skip (§4.10), GTID mode required (§1.2) |
| Fencing by GTID UUID fenced Group Replication primaries and missed real second writers; the role came from `read_only` guesses | `writer_uuid` compare-and-set on every write; explicit takeover (§4.10) |
| Timer checkpoints could fall inside a transaction; idle indexes stopped advancing | Transaction-boundary rule; idle indexes update `ckpt_gtids` (§4.4, §4.12) |
| Followers read their binlog and could mix checkpoints under parallel replication, wait forever for an event, or freeze under filters | Polling, chunk-count check, required commit order, filter check (§4.10) |
| Generation reset to 1 after a drop | Tombstones; generations never reset (§4.2, §4.6) |
| Header and links data reached memory unchecked; followers missed `num_deleted_` | Shared structural validation; header CRC; full derived-state rebuild (§4.7, §4.10) |
| Suggested global `binlog_row_image = MINIMAL`, which breaks online updates | Session-only, and a warning against global (§4.10) |
| `myvector_index_storage_default` acted as a global backend switch | Resolved into the comment at creation (§4.1) |
| Migration relied on `save`, which writes only dirty nodes; unclear crash rules | `MYVECTOR_INDEX_MIGRATE`, real full save, state column, load table (§4.11) |
| Long-lived storage connection blocked uninstall; plain `CREATE TABLE` broke re-runs and replicas | Short-lived command-service sessions; `IF NOT EXISTS` under `sql_log_bin = 0` (§4.2, §4.5) |
| Write volume ignored vector updates; redo, binlog and purge not sized | Corrected table and a sizing section (§4.3) |
| `VARCHAR(192)` names with an insensitive collation; `TIMESTAMP` | Binary 776-byte names, surrogate key, `DATETIME(6)` (§4.2) |
| Search on an unloaded index crashes; failed load reports SUCCESS | Phase 0 prerequisite, [#265](https://github.com/askdba/myvector/issues/265) |

Also corrected: M vs M0 in neighbor counts, the checkpoint id's location (§3), the
stale code citations, and the claim that only online updates need `myvector.cnf`.
