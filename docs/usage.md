# Usage & API Reference

## Index Management

**Build an Index:**

```sql
CALL mysql.myvector_index_build('database.table.column', 'primary_key_column');
```

**Check Index Status:**

```sql
CALL mysql.myvector_index_status('database.table.column');
```

**Drop an Index:**

```sql
CALL mysql.myvector_index_drop('database.table.column');
```

**Save and Load an Index:**

```sql
-- Indexes are automatically saved. To load an index on startup:
CALL mysql.myvector_index_load('database.table.column');
```

## Vector Functions

**Create a Vector:**

```sql
SET @my_vector = myvector_construct('[1.2, 3.4, 5.6]');
```

**Calculate Distance:**

```sql
SET @vec1 = myvector_construct('[1.2, 3.4, 5.6]');
SET @vec2 = myvector_construct('[1.0, 3.0, 5.0]');
SELECT myvector_distance(@vec1, @vec2, 'L2');
```

**Display a Vector:**

```sql
SELECT myvector_display(wordvec) FROM words50d LIMIT 1;
```

## Vector Search

**Nearest neighbours (ANN):** `MYVECTOR_IS_ANN(index, key column, query vector, k)`
returns the `k` rows nearest to the query vector (plugin builds only, see
[Known limitations](LIMITATIONS.md)).

```sql
SET @q = myvector_construct('[1.2, 3.4, 5.6]');
SELECT id FROM t WHERE MYVECTOR_IS_ANN('db.t.v', 'id', @q, 10);
```

**Filtered search:** pass the keys of the rows that may be returned as a fifth
argument, usually a `JSON_ARRAYAGG` subquery. The search returns the `k` nearest rows
among those keys, so it returns `k` rows whenever at least `k` rows match.

```sql
SELECT id FROM t
WHERE MYVECTOR_IS_ANN('db.t.v', 'id', @q, 10,
      (SELECT JSON_ARRAYAGG(id) FROM t WHERE category = 'books'));
```

If the filter matches no rows, the result is empty. For an HNSW index with 10,000 or
fewer matching keys, MyVector computes exact distances over just those rows. For more
matching keys, it walks the HNSW graph and skips rows that are not in the list.

Do **not** put the filter next to `MYVECTOR_IS_ANN` in the `WHERE` clause instead
(`WHERE category = 'books' AND MYVECTOR_IS_ANN(..., 10)`). That form finds the 10
nearest rows first and filters them afterwards, so it can return fewer than 10 rows.

On component builds, which have no `MYVECTOR_IS_ANN` rewrite, call the function
directly. It returns a JSON array of keys:

```sql
SELECT myvector_ann_set('db.t.v', 'id', @q, 'nn=10',
       (SELECT JSON_ARRAYAGG(id) FROM t WHERE category = 'books'));
```

## Docker Compose

!!! warning "Local trial only"
    As with the [Quick Start](quickstart.md), this uses a fixed password with
    the port bound to all interfaces. Fine for local trial use; use a real
    secret and bind to `127.0.0.1` for anything beyond that.

```yaml
version: '3.8'

services:
  myvector:
    image: ghcr.io/askdba/myvector:mysql8.4
    ports:
      - "3306:3306"
    environment:
      MYSQL_ROOT_PASSWORD: myvector
      MYSQL_DATABASE: vectordb
    volumes:
      - myvector-data:/var/lib/mysql

volumes:
  myvector-data:
```

See [Docker Images](DOCKER_IMAGES.md) for the full list of available tags and
[Configuration](CONFIGURATION.md) for the full configuration reference.
