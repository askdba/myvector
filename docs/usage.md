# Usage & API Reference

## Declaring a Vector Column

A vector column carries its index options: the index **type**, the dimension, the
distance metric and the HNSW settings. There are two ways to declare it.

**Plugin builds, `MYVECTOR(...)` column type.** The plugin rewrites this DDL into a
binary column plus a comment:

```sql
CREATE TABLE docs (
  id           INT PRIMARY KEY,
  title        VARCHAR(200),
  category     VARCHAR(32),
  tenant_id    INT,
  published_at DATE,
  embedding    MYVECTOR(type=HNSW,dim=768,size=100000,dist=Cosine,M=16,ef=100)
);
```

**Component builds, or any build: a `MYVECTOR COLUMN` comment.** Components have no DDL
rewrite (#144), so declare the options in the column comment yourself:

```sql
CREATE TABLE docs (
  id           INT PRIMARY KEY,
  title        VARCHAR(200),
  category     VARCHAR(32),
  tenant_id    INT,
  published_at DATE,
  embedding    VARBINARY(3100)
    COMMENT 'MYVECTOR COLUMN type=HNSW,dim=768,size=100000,dist=Cosine,M=16,ef=100,idcol=id'
);
```

On MySQL 9.x you can use the native `VECTOR(n)` type with the same comment.

### Index types

| `type=` | Index | Use it for |
|---|---|---|
| `KNN` | Brute-force exact search | Small tables, or as a reference to check recall |
| `HNSW` | HNSW graph (approximate) | Most workloads |
| `HNSW_BV` | HNSW on binary vectors, Hamming distance | Vectors built with `myvector_construct(..., 'i=string,o=bv')` |

`type` is case-insensitive (`hnsw` and `HNSW` both work). Other option names are
case-sensitive: write `M=16`, not `m=16`. See [Known limitations](LIMITATIONS.md).

### The type must be spelled correctly

A misspelled or missing `type` is an error. MyVector does not build a brute-force KNN
index in its place:

```sql
-- Plugin DDL: rejected at CREATE TABLE
CREATE TABLE t (id INT PRIMARY KEY, v MYVECTOR(type=hnws,dim=3,size=100));
-- ERROR 1644 (HY000): MYVECTOR column type invalid: hnws (use KNN, HNSW or HNSW_BV)

-- Comment form: the table is created, but the index build fails
CREATE TABLE t (id INT PRIMARY KEY,
  v VARBINARY(64) COMMENT 'MYVECTOR COLUMN type=hnws,dim=3,size=100,idcol=id');
CALL mysql.myvector_index_build('db.t.v', 'id');
-- ERROR: unknown index type 'hnws' for db.t.v. Use type=KNN, HNSW or HNSW_BV

-- Comment form with no type at all
CREATE TABLE t2 (id INT PRIMARY KEY,
  v VARBINARY(64) COMMENT 'MYVECTOR COLUMN dim=3,size=100,idcol=id');
CALL mysql.myvector_index_build('db.t2.v', 'id');
-- ERROR: missing index type for db.t2.v. Use type=KNN, HNSW or HNSW_BV
```

`MYVECTOR(...)` without a type still defaults to `KNN`: the plugin writes `type=KNN` into
the comment for you. A hand-written comment must name the type, including `type=KNN`.

!!! note "Upgrading"
    Before this check, a misspelled or missing type silently built a KNN index and the
    build returned `SUCCESS`. If an index built on an older version fails with one of the
    errors above, correct the comment (`ALTER TABLE ... MODIFY ... COMMENT '...'`) and
    rebuild it. For an `online=Y` column, see
    [Online index updates](ONLINE_INDEX_UPDATES.md#troubleshooting).

### Long comments can span lines

A comment may continue on the next line, or use a tab after `MYVECTOR COLUMN`:

```sql
CREATE TABLE docs (
  id        INT PRIMARY KEY,
  embedding VARBINARY(3100) COMMENT 'MYVECTOR COLUMN
    type=HNSW,dim=768,size=100000,
    dist=Cosine,M=16,ef=100,idcol=id'
);
```

The comment must still *start* with `MYVECTOR COLUMN`: the `MYVECTOR_INDEX_*`
procedures reject a comment with leading spaces or a leading line break as
"not a MYVECTOR column".

### Check which index was built

`myvector_index_status` reports the type actually in use. Check it after the first
build:

```sql
CALL mysql.myvector_index_status('db.docs.embedding');
-- Vector Index : db.docs.embedding
-- Type : HNSW
-- Dimension : 768
-- Distance : Cosine
-- ...
```

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

**Example: nearest books only.** The `docs` table from
[Declaring a Vector Column](#declaring-a-vector-column) holds 100,000 rows in 20
categories, and 500 of them are `books`:

```sql
SET @q = myvector_construct('[...]');   -- the query embedding

-- Wrong: finds the 10 nearest rows of any category, then keeps the books among
-- them. Can return fewer than 10 rows, or none.
SELECT id FROM docs
WHERE category = 'books'
  AND MYVECTOR_IS_ANN('db.docs.embedding', 'id', @q, 10);

-- Right: searches only the 500 books. Returns the 10 nearest books.
SELECT id FROM docs
WHERE MYVECTOR_IS_ANN('db.docs.embedding', 'id', @q, 10,
      (SELECT JSON_ARRAYAGG(id) FROM docs WHERE category = 'books'));
```

The filter can be any query that returns keys, for example one tenant's rows, or rows
the current user may see:

```sql
SELECT d.id, d.title
FROM docs d
WHERE MYVECTOR_IS_ANN('db.docs.embedding', 'id', @q, 10,
      (SELECT JSON_ARRAYAGG(id) FROM docs
       WHERE tenant_id = 42 AND published_at >= '2026-01-01'));
```

Building the key list reads every matching row. When the filter matches most of a large
table, that step costs more than the search; there, a plain unfiltered search may be
the better choice.

On component builds, which have no `MYVECTOR_IS_ANN` rewrite, call the function
directly. It returns a JSON array of keys:

```sql
SELECT myvector_ann_set('db.t.v', 'id', @q, 'nn=10',
       (SELECT JSON_ARRAYAGG(id) FROM t WHERE category = 'books'));
-- [412,87,1033,...]
```

The keys come back nearest first. To get the rows in that order, unpack the array with
`JSON_TABLE` in a derived table (JSON_TABLE does not accept the function call directly):

```sql
SELECT t.*
FROM (SELECT myvector_ann_set('db.t.v', 'id', @q, 'nn=10',
             (SELECT JSON_ARRAYAGG(id) FROM t WHERE category = 'books')) AS js) src,
     JSON_TABLE(src.js, '$[*]'
                COLUMNS (rank_no FOR ORDINALITY, id BIGINT PATH '$')) nn
JOIN t ON t.id = nn.id
ORDER BY nn.rank_no;
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
