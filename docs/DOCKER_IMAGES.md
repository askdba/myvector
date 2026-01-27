# MyVector Docker Images

## Location

<https://github.com/askdba/myvector/pkgs/container/myvector>

We have our own dedicated image repository!

The MyVector docker images are built on top of community MySQL docker images
from -

<https://hub.docker.com/r/mysql/mysql-server/>

## What is in the docker image?

Pre-built MyVector plugin (myvector.so) and installation script (myvectorplugin.sql)

```bash
- /usr/lib/mysql/plugin/myvector.so
- /docker-entrypoint-initdb.d/myvectorplugin.sql
```

NOTE: The MySQL image entrypoint will run the SQL script automatically on first
startup. If you need to re-run it manually, use:
`mysql -u root -p < /docker-entrypoint-initdb.d/myvectorplugin.sql`

## Quick Start (MySQL 8.4)

Start a container with a fresh data directory:

```bash
docker run -d \
  --name myvector-test \
  -e MYSQL_ROOT_PASSWORD=myvector \
  -e MYSQL_DATABASE=vectordb \
  -p 3306:3306 \
  ghcr.io/askdba/myvector:mysql8.4
```

Verify the plugin is loaded:

```bash
docker exec myvector-test mysql -uroot -pmyvector -e \
  "SELECT PLUGIN_NAME, PLUGIN_STATUS FROM INFORMATION_SCHEMA.PLUGINS WHERE PLUGIN_NAME='myvector';"
```

If the plugin/UDFs are missing (e.g., reused data directory), reinstall:

```bash
docker exec myvector-test mysql -uroot -pmyvector -e \
  "SOURCE /docker-entrypoint-initdb.d/myvectorplugin.sql;"
```

## Stanford50d Sample Dataset (Working Example)

Create a table with a MYVECTOR column (note: use the MYVECTOR column syntax,
not a COMMENT, so the plugin can rewrite the DDL properly):

```bash
docker exec -i myvector-test mysql -uroot -pmyvector vectordb <<'SQL'
DROP TABLE IF EXISTS words50d;
CREATE TABLE words50d (
  wordid INT AUTO_INCREMENT PRIMARY KEY,
  word VARCHAR(200),
  wordvec MYVECTOR(type=HNSW,dim=50,size=400000,dist=L2,m=64,ef=100)
);
SQL
```

Load the sample vectors (50d GloVe):

```bash
curl -L -o /tmp/insert50d.sql.gz \
  https://raw.githubusercontent.com/askdba/myvector/main/examples/stanford50d/insert50d.sql.gz
gunzip -c /tmp/insert50d.sql.gz | docker exec -i myvector-test mysql -uroot -pmyvector vectordb
```

Build the vector index:

```bash
docker exec myvector-test mysql -uroot -pmyvector -e \
  "CALL mysql.myvector_index_build('vectordb.words50d.wordvec','wordid');" vectordb
```

Run a similarity search (note: `myvector_row_distance` requires the id):

```bash
docker exec myvector-test mysql -uroot -pmyvector -e \
  "SET @school_vec = (SELECT wordvec FROM words50d WHERE word = 'school');
   SELECT word, myvector_row_distance(wordid) AS distance
   FROM words50d
   WHERE MYVECTOR_IS_ANN('vectordb.words50d.wordvec','wordid',@school_vec,10);" vectordb
```

## MySQL 9.x (Native VECTOR Type Example)

Start a MySQL 9.x container (use `--platform linux/amd64` on Apple Silicon):

```bash
docker run -d \
  --platform linux/amd64 \
  --name myvector-test-96 \
  -e MYSQL_ROOT_PASSWORD=myvector \
  -e MYSQL_DATABASE=vectordb \
  -p 3309:3306 \
  ghcr.io/askdba/myvector:mysql9.6
```

Configure MyVector to use TCP and set the index directory, then restart:

```bash
docker exec myvector-test-96 bash -lc "cat >/var/lib/mysql/myvector.cnf <<'EOF'
myvector_host=127.0.0.1
myvector_port=3306
myvector_user_id=root
myvector_user_password=myvector
EOF"
docker restart myvector-test-96
docker exec myvector-test-96 mysql -uroot -pmyvector -e \
  "SET GLOBAL myvector_index_dir='/var/lib/mysql';"
```

Create the table and load the sample vectors:

```bash
docker exec -i myvector-test-96 mysql -uroot -pmyvector vectordb <<'SQL'
DROP TABLE IF EXISTS words50d;
CREATE TABLE words50d (
  wordid INT AUTO_INCREMENT PRIMARY KEY,
  word VARCHAR(200),
  wordvec MYVECTOR(type=HNSW,dim=50,size=400000,dist=L2,m=64,ef=100)
);
SQL

curl -L -o /tmp/insert50d.sql.gz \
  https://raw.githubusercontent.com/askdba/myvector/main/examples/stanford50d/insert50d.sql.gz
gunzip -c /tmp/insert50d.sql.gz | docker exec -i myvector-test-96 mysql -uroot -pmyvector vectordb
```

Build the vector index and run a similarity search:

```bash
docker exec myvector-test-96 mysql -uroot -pmyvector -e \
  "CALL mysql.myvector_index_build('vectordb.words50d.wordvec','wordid');" vectordb

docker exec myvector-test-96 mysql -uroot -pmyvector -e \
  "SET @school_vec = (SELECT wordvec FROM words50d WHERE word = 'school');
   SELECT word, myvector_row_distance(wordid) AS distance
   FROM words50d
   WHERE MYVECTOR_IS_ANN('vectordb.words50d.wordvec','wordid',@school_vec,10);" vectordb
```

Confirm the DDL was rewritten to the native `VECTOR` type:

```bash
docker exec myvector-test-96 mysql -uroot -pmyvector -e \
  "SHOW CREATE TABLE words50d;" vectordb
```

## Index Build Connection Notes

If index build fails with a socket connection error, configure MyVector to use
TCP inside the container and restart it:

```bash
docker exec myvector-test bash -lc "cat >/var/lib/mysql/myvector.cnf <<'EOF'
myvector_host=127.0.0.1
myvector_port=3306
myvector_user_id=root
myvector_user_password=myvector
EOF"
docker restart myvector-test
```

Then reinstall the plugin/UDFs once after restart:

```bash
docker exec myvector-test mysql -uroot -pmyvector -e \
  "SOURCE /docker-entrypoint-initdb.d/myvectorplugin.sql;"
```

After the plugin is installed/registered above, please follow the usage
instructions in the main README:

<https://github.com/askdba/myvector#-usage-examples>

## MyVector Demos

<https://github.com/askdba/myvector/tree/main/examples/stanford50d>

## Versions

Docker images for MySQL 8.0.x, 8.4.x, and 9.0.x are available.
