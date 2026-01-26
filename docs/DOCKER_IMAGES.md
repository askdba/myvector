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
  --name myvector-db \
  -e MYSQL_ROOT_PASSWORD=myvector \
  -e MYSQL_DATABASE=vectordb \
  -p 3306:3306 \
  ghcr.io/askdba/myvector:mysql8.4
```

Verify the plugin is loaded:

```sql
mysql -uroot -pmyvector -e \
  "SELECT PLUGIN_NAME, PLUGIN_STATUS FROM INFORMATION_SCHEMA.PLUGINS WHERE PLUGIN_NAME='myvector';"
```

If the plugin/UDFs are missing (e.g., reused data directory), reinstall:

```sql
mysql -uroot -pmyvector -e "SOURCE /docker-entrypoint-initdb.d/myvectorplugin.sql;"
```

## Stanford50d Sample Dataset (Working Example)

Create a table with a MYVECTOR column (note: use the MYVECTOR column syntax,
not a COMMENT, so the plugin can rewrite the DDL properly):

```sql
mysql -uroot -pmyvector vectordb <<'SQL'
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
gunzip -c /tmp/insert50d.sql.gz | mysql -uroot -pmyvector vectordb
```

Build the vector index:

```sql
mysql -uroot -pmyvector -e \
  "CALL mysql.myvector_index_build('vectordb.words50d.wordvec','wordid');" vectordb
```

Run a similarity search (note: `myvector_row_distance` requires the id):

```sql
mysql -uroot -pmyvector -e \
  "SET @school_vec = (SELECT wordvec FROM words50d WHERE word = 'school');
   SELECT word, myvector_row_distance(wordid) AS distance
   FROM words50d
   WHERE MYVECTOR_IS_ANN('vectordb.words50d.wordvec','wordid',@school_vec,10);" vectordb
```

## Index Build Connection Notes

If index build fails with a socket connection error, configure MyVector to use
TCP inside the container and restart it:

```bash
docker exec myvector-db bash -lc "cat >/var/lib/mysql/myvector.cnf <<'EOF'
myvector_host=127.0.0.1
myvector_port=3306
myvector_user_id=root
myvector_user_password=myvector
EOF"
docker restart myvector-db
```

Then reinstall the plugin/UDFs once after restart:

```sql
mysql -uroot -pmyvector -e "SOURCE /docker-entrypoint-initdb.d/myvectorplugin.sql;"
```

After the plugin is installed/registered above, please follow the usage
instructions in the main README:

<https://github.com/askdba/myvector#-usage-examples>

## MyVector Demos

<https://github.com/askdba/myvector/tree/main/examples/stanford50d>

## Versions

Docker images for MySQL 8.0.x, 8.4.x, and 9.0.x are available.
