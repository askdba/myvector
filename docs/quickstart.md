# Quick Start

Get up and running with MyVector in minutes using our official Docker images.

## 1. Start the Docker Container

```bash
docker run -d \
  --name myvector-db \
  -p 3306:3306 \
  -e MYSQL_ROOT_PASSWORD=myvector \
  -e MYSQL_DATABASE=vectordb \
  ghcr.io/askdba/myvector:mysql8.4
```

!!! warning "Local trial only"
    This uses a fixed, publicly-known password with the port bound to all
    interfaces — fine for trying MyVector out on your own machine, but don't
    run it this way on a shared or internet-facing host. Use a real secret
    and bind to `127.0.0.1:3306:3306` for anything beyond local testing.

## 2. Connect to MySQL

```bash
mysql -h 127.0.0.1 -u root -pmyvector vectordb
```

## 3. Create a Table and Insert Data

Let's use a simple example with 50-dimensional word vectors.

Use the native `MYVECTOR` column type (not a `COMMENT`-based declaration) so
the plugin can rewrite the table's DDL correctly:

```sql
-- Create a table for our word vectors
CREATE TABLE words50d (
  wordid INT AUTO_INCREMENT PRIMARY KEY,
  word VARCHAR(200),
  wordvec MYVECTOR(type=HNSW,dim=50,size=400000,dist=L2,m=64,ef=100)
);
```

Download and load the sample vectors (50-dimensional GloVe word vectors, from
the [`examples/stanford50d`](https://github.com/askdba/myvector/tree/main/examples/stanford50d)
directory). In a real-world scenario, you would generate your own vectors.

```bash
curl -L -o /tmp/insert50d.sql.gz \
  https://raw.githubusercontent.com/askdba/myvector/main/examples/stanford50d/insert50d.sql.gz
gunzip -c /tmp/insert50d.sql.gz | mysql -h 127.0.0.1 -u root -pmyvector vectordb
```

## 4. Build the Vector Index

```sql
CALL mysql.myvector_index_build('vectordb.words50d.wordvec', 'wordid');
```

## 5. Run a Similarity Search

Find words similar to "school":

```sql
SET @school_vec = (SELECT wordvec FROM words50d WHERE word = 'school');

SELECT word, myvector_row_distance(wordid) AS distance
FROM words50d
WHERE MYVECTOR_IS_ANN('vectordb.words50d.wordvec', 'wordid', @school_vec, 10);
```

`myvector_row_distance()` requires the row's id column as its argument.

You should see results like "university," "student," "teacher," etc. It's
that easy!

## Manual Installation

If you are running your own MySQL instance (not using the Docker images), install the plugin manually:

```bash
mysql -u root -p -e "INSTALL PLUGIN myvector SONAME 'myvector.so';"
mysql -u root -p < sql/myvectorplugin.sql
```

!!! note "Plugin vs. Component"
    The **plugin** (`INSTALL PLUGIN`) is the current stable path and supports MySQL 8.0, 8.4, and 9.0.
    The **component** (`INSTALL COMPONENT`) is the forward path for MySQL 8.4 and 9.7 (LTS).
    MySQL 8.0 plugin support will be maintained through MySQL 8.0 EOL; no component build is planned for 8.0.

See [Docker Images](DOCKER_IMAGES.md) for the full list of available image tags.

## Next Steps

- [Usage & API Reference](usage.md) — full SQL function and stored procedure reference
- [Demo](DEMO.md) — a complete walkthrough with the Amazon Product Catalog dataset
- [Configuration](CONFIGURATION.md) — configuration file reference
