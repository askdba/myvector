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

## 2. Connect to MySQL

```bash
mysql -h 127.0.0.1 -u root -pmyvector vectordb
```

## 3. Create a Table and Insert Data

Let's use a simple example with 50-dimensional word vectors.

```sql
-- Create a table for our word vectors
CREATE TABLE words50d (
  wordid INT PRIMARY KEY,
  word VARCHAR(50),
  wordvec VARBINARY(200) COMMENT 'MYVECTOR(type=HNSW,dim=50,size=100000,dist=L2)'
);

-- Download and insert the data (from the examples/stanford50d directory)
-- In a real-world scenario, you would generate your own vectors.
-- wget https://raw.githubusercontent.com/askdba/myvector/main/examples/stanford50d/insert50d.sql.gz
-- gunzip insert50d.sql.gz
-- mysql -h 127.0.0.1 -u root -pmyvector vectordb < insert50d.sql
```

## 4. Build the Vector Index

```sql
CALL mysql.myvector_index_build('vectordb.words50d.wordvec', 'wordid');
```

## 5. Run a Similarity Search

Find words similar to "school":

```sql
SET @school_vec = (SELECT wordvec FROM words50d WHERE word = 'school');

SELECT word, myvector_row_distance() as distance
FROM words50d
WHERE MYVECTOR_IS_ANN('vectordb.words50d.wordvec', 'wordid', @school_vec, 10);
```

You should see results like "university," "student," "teacher," etc. It's
that easy!

## Manual Installation

If you are running your own MySQL instance (not using the Docker images), install the plugin manually:

```bash
mysql -u root -p -e "INSTALL PLUGIN myvector SONAME 'myvector.so';"
mysql -u root -p < sql/install_functions.sql
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
