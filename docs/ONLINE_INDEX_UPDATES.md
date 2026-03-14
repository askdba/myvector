# Online Updates of Vector Index

This document describes how to create and configure **online updates** for MyVector indexes—the real-time, automatic synchronization of vector indexes when data changes (INSERT, UPDATE, DELETE) in the base table.

## Overview

**Online updates** mean that when you add, modify, or delete rows in a table with a vector column, the corresponding vector index is updated automatically. Without online updates, you must call `myvector_index_build` or `myvector_index_refresh` manually (or via triggers) to keep the index in sync.

MyVector implements online updates by monitoring the MySQL binary log (binlog). A background thread reads row-based binlog events and applies INSERT/UPDATE/DELETE changes to registered vector indexes.

## Prerequisites

### 1. Row-Based Binary Logging

Online updates require **row-based** binlog format. The plugin parses row events to extract vector and primary key values.

```sql
-- Check current format
SHOW VARIABLES LIKE 'binlog_format';

-- Set row format (add to my.cnf or run at runtime)
SET GLOBAL binlog_format = 'ROW';
```

For MySQL 8.0.20+, you may need to set `binlog_row_metadata` for full column metadata. See your MySQL version's documentation.

### 2. Binary Logging Enabled

```sql
SHOW VARIABLES LIKE 'log_bin';
-- Should be ON
```

### 3. User Privileges for Binlog Connection

The plugin opens a separate MySQL connection to read the binlog. This connection requires:

- **`REPLICATION CLIENT`** (or `REPLICATION SLAVE` on older MySQL) — to read binlog events
- **`SELECT`** on the base table — to read vector data when building/rebuilding the index

> **Note:** Root access is not mandatory. A dedicated user with these privileges is sufficient (see [GitHub issue #81](https://github.com/askdba/myvector/issues/81)).

Example user setup:

```sql
CREATE USER 'myvector_binlog'@'localhost' IDENTIFIED BY 'your_password';

-- For reading binlogs
GRANT REPLICATION CLIENT ON *.* TO 'myvector_binlog'@'localhost';

-- For building/refreshing indexes (read base table)
GRANT SELECT ON your_database.* TO 'myvector_binlog'@'localhost';

FLUSH PRIVILEGES;
```

### 4. Configuration File

The binlog thread uses a config file for connection parameters. Set the path via the `myvector_config_file` system variable (default: `myvector.cnf`).

Create a config file (e.g. `/etc/myvector.cnf` or `myvector.cnf` in the data directory) with connection options in key=value format:

```
myvector_user_id=myvector_binlog
myvector_user_password=your_password
myvector_host=127.0.0.1
myvector_port=3306
myvector_socket=/tmp/mysql.sock
```

For local socket connection, you can use:

```
myvector_user_id=myvector_binlog
myvector_user_password=your_password
myvector_socket=/tmp/mysql.sock
```

Then point the plugin to it:

```sql
SET GLOBAL myvector_config_file = '/path/to/myvector.cnf';
```

Or in `my.cnf`:

```ini
[mysqld]
myvector_config_file=/path/to/myvector.cnf
```

### 5. Feature Level

The binlog thread is disabled when `myvector_feature_level` is 1. Ensure it is 0 or 2+ (default is 2):

```sql
SHOW VARIABLES LIKE 'myvector_feature_level';
-- Default: 2 (binlog enabled)
```

## Creating an Online-Updated Index

### Step 1: Define the Column with `online=Y` and `idcol`

Add `online=Y` and `idcol=<primary_key_column>` to the MYVECTOR column options. The `idcol` tells the binlog processor which column is the primary key for mapping rows to index entries.

**Example: VARBINARY column (MySQL 8.0)**

```sql
CREATE TABLE products (
  id INT PRIMARY KEY,
  name VARCHAR(255),
  embedding VARBINARY(3072) COMMENT 'MYVECTOR(type=HNSW,dim=768,size=100000,online=Y,idcol=id,dist=L2)'
);
```

**Example: VECTOR column (MySQL 8.4+)**

```sql
CREATE TABLE products (
  id INT PRIMARY KEY,
  name VARCHAR(255),
  embedding VECTOR(768) COMMENT 'MYVECTOR(type=HNSW,dim=768,size=100000,online=Y,idcol=id,dist=L2)'
);
```

### Step 2: Build the Initial Index

Build the index once before relying on online updates:

```sql
CALL mysql.myvector_index_build('your_database.products.embedding', 'id');
```

### Step 3: Verify Online Registration

On plugin startup, MyVector discovers all columns with `online=Y` from the `myvector_columns` view and registers them for binlog updates. The view is created by the plugin installation script (`sql/myvectorplugin.sql`) in the `mysql` database. Ensure the installation script has been run so that `mysql.myvector_columns` exists.

## How It Works

1. **Plugin init:** A background thread starts and connects to MySQL using the config file credentials.
2. **Discovery:** The thread queries `myvector_columns` (or equivalent) for columns with `online=Y` and `idcol` set.
3. **Binlog stream:** The thread opens a binlog stream and processes WRITE_ROWS, UPDATE_ROWS, and DELETE_ROWS events.
4. **Index updates:** For each event affecting a registered table, the plugin adds, updates, or removes the corresponding vector entry in memory.
5. **Checkpointing:** Progress is tracked via binlog file and position so the index can be recovered after restart.

## Complete Example

```sql
-- 1. Ensure row-based binlog
SET GLOBAL binlog_format = 'ROW';

-- 2. Create table with online=Y
CREATE TABLE embeddings (
  id INT PRIMARY KEY,
  text VARCHAR(512),
  vec VARBINARY(3072) COMMENT 'MYVECTOR(type=HNSW,dim=768,size=100000,online=Y,idcol=id,dist=L2)'
);

-- 3. Build initial index
CALL mysql.myvector_index_build('your_db.embeddings.vec', 'id');

-- 4. Insert data — index updates automatically
INSERT INTO embeddings (id, text, vec) VALUES (1, 'hello', myvector_construct('[0.1, 0.2, ...]'));

-- 5. Update — index updates automatically
UPDATE embeddings SET vec = myvector_construct('[0.2, 0.3, ...]') WHERE id = 1;

-- 6. Delete — index updates automatically
DELETE FROM embeddings WHERE id = 1;

-- 7. Search (works immediately after DML)
SELECT id, text FROM embeddings
WHERE MYVECTOR_IS_ANN('your_db.embeddings.vec', 'id', myvector_construct('[0.1, 0.2, ...]'), 10);
```

## Without Online Updates

If you omit `online=Y`:

- The index is **not** updated automatically when you INSERT/UPDATE/DELETE.
- You must call `myvector_index_build` to rebuild, or use `myvector_index_refresh` with a `track=` column for incremental refresh.
- No binlog connection or REPLICATION privileges are required.

## Troubleshooting

| Symptom | Possible cause |
|--------|----------------|
| Index not updating after DML | `online=Y` or `idcol` missing in column options; binlog format not ROW; `myvector_feature_level=1` |
| "Binlog thread failed to connect" | Wrong credentials in config file; user lacks REPLICATION CLIENT |
| Index not found after restart | Index not saved; ensure `myvector_index_save` or automatic save runs; check `myvector_index_dir` |
| Wrong database for myvector_columns | The `myvector_columns` view is created in `mysql` by the installation script; ensure it exists and the plugin can query it |

## Testing with Docker

A test script validates the online updates flow:

```bash
./scripts/test-online-updates.sh                    # Use prebuilt ghcr.io image
./scripts/test-online-updates.sh myvector:local     # Use locally built image
```

To build the Docker image locally (required if the prebuilt image has config issues):

```bash
./scripts/build-docker-local.sh 8.4   # ~15–30 min first run
./scripts/test-online-updates.sh myvector:mysql8.4-local
```

For Docker, ensure `myvector.cnf` has `myvector_host=127.0.0.1` and `myvector_port=3306` so the index build connection works. See [DOCKER_IMAGES.md](DOCKER_IMAGES.md).

## Related Documentation

- [DEMO.md](DEMO.md) — Full demo with embeddings
- [README.md](../README.md) — Quick start and architecture
- [COMPONENT_MIGRATION_PLAN.md](COMPONENT_MIGRATION_PLAN.md) — Technical details on binlog monitoring
