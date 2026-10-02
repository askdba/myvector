# Demo

A MyVector session from start to finish, replayed in your browser. It starts
the published MySQL 9.7 component image, loads the 50,000 most common English
words as 50-dimensional vectors, builds an HNSW index, and searches it. It
checks the results against an exact search, inserts a row to show the index
updating online, and restarts MySQL to show the index loading from disk.

Every command in the recording ran for real; only the typing is simulated. Use
the markers on the progress bar to jump to a chapter.

<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/asciinema-player@3.17.0/dist/bundle/asciinema-player.css">
<div id="myvector-demo-player"></div>
<script src="https://cdn.jsdelivr.net/npm/asciinema-player@3.17.0/dist/bundle/asciinema-player.min.js"></script>
<script>
  AsciinemaPlayer.create(
    '../assets/demo/myvector-demo.cast',
    document.getElementById('myvector-demo-player'),
    { autoPlay: true, loop: true, idleTimeLimit: 2, fit: 'width', terminalFontSize: 'small' }
  );
</script>

[Download the recording](assets/demo/myvector-demo.cast) to play it in a
terminal with `asciinema play myvector-demo.cast`.

## Run it yourself

From a clone of the repository (the word vectors are in
[`examples/stanford50d`](https://github.com/askdba/myvector/tree/main/examples/stanford50d)):

```bash
docker run -d --name myvector-demo -e MYSQL_ROOT_PASSWORD=myvector -e MYSQL_DATABASE=demo \
    -v $PWD/scripts/demo/00-myvector-cnf.sh:/docker-entrypoint-initdb.d/00-myvector-cnf.sh:ro \
    ghcr.io/askdba/myvector:mysql9.7-component
```

`scripts/demo/00-myvector-cnf.sh` writes `myvector.cnf` into the data directory
before the image installs the component. The component reads it to connect
back to MySQL for index builds and for online updates. It reuses the root
password, which is fine for a local demo only; see
[Online Index Updates](ONLINE_INDEX_UPDATES.md) for a dedicated user.

Wait about 30 seconds for the first start, then connect with
`docker exec -it myvector-demo mysql -uroot -pmyvector demo` and create the
table:

```sql
CREATE TABLE words50d (
  wordid  INT AUTO_INCREMENT PRIMARY KEY,
  word    VARCHAR(200),
  wordvec VARBINARY(208) COMMENT
    'MYVECTOR COLUMN type=HNSW,dim=50,size=100000,M=32,ef=100,dist=L2,online=Y,idcol=wordid'
);
```

Load the words from the shell, then build the index:

```bash
(zcat examples/stanford50d/insert50d.sql.gz | head -n 50001; echo 'COMMIT;') \
    | docker exec -i myvector-demo mysql -uroot -pmyvector demo
```

```sql
CALL mysql.myvector_index_build('demo.words50d.wordvec', 'wordid');
CALL mysql.myvector_index_status('demo.words50d.wordvec')\G
```

Turn on online updates: after the build, reinstall the component and load the
saved index into the new instance (the note below explains why):

```sql
UNINSTALL COMPONENT 'file://myvector';
INSTALL COMPONENT 'file://myvector';
CALL mysql.myvector_index_load('demo.words50d.wordvec');
```

Search. `myvector_ann_set()` returns the ids of the nearest rows as a JSON
array, nearest first, and `JSON_TABLE` turns them back into rows:

```sql
SET @q = (SELECT wordvec FROM words50d WHERE word = 'school');

SELECT nn.rank_no, w.word,
       ROUND(myvector_distance(w.wordvec, @q, 'L2'), 3) AS distance
FROM (SELECT myvector_ann_set('demo.words50d.wordvec', 'wordid', @q, 'nn=8') AS js) src,
     JSON_TABLE(src.js, '$[*]' COLUMNS (rank_no FOR ORDINALITY, id BIGINT PATH '$')) nn
JOIN words50d w ON w.wordid = nn.id ORDER BY nn.rank_no;
```

Insert a row and search again: with `online=Y` the new row shows up after a
moment, without a rebuild. After a restart, load the saved index with
`CALL mysql.myvector_index_load('demo.words50d.wordvec');`. When you are done,
remove the container with `docker rm -f myvector-demo`.

!!! note "Component images up to v1.26.9"
    The published component images (v1.26.9 and earlier) differ from the
    plugin images and from newer builds. The demo works around this:

    - The vector column is declared with a `MYVECTOR COLUMN` comment. The
      `MYVECTOR(...)` column type is not available on these images.
    - Searches call `myvector_ann_set()`. `WHERE MYVECTOR_IS_ANN(...)` needs a
      plugin image or a component build that includes #156; see
      [Usage & API](usage.md#vector-search).
    - The binlog listener for online updates only starts if the component is
      installed while MySQL accepts TCP connections. The image installs it
      during first-start initialization, when it doesn't, and MySQL loads it
      at every restart before it does. Reinstall the component once the server
      is up, as above, or online updates stay off.
    - Only INSERTs are applied online. A DELETE or UPDATE does not change the
      index, so the search can still return a deleted row's id, or rank a row
      by its old vector. Rebuild the index after them.
    - Run `myvector_index_build` before the reinstall, while the listener is
      off. Building an `online=Y` index while the listener runs crashed the
      server in our tests.

To re-record the demo, for example for a new image, run
`python3 scripts/record-demo.py --image <image>`. It writes
`docs/assets/demo/myvector-demo.cast`.

## Large dataset: Amazon product catalog

A bigger example: load a real-world product catalog (from Amazon) into a MySQL
database, build an HNSW index and then perform vector search to find
semantically relevant results. This walkthrough uses the plugin syntax
(`MYVECTOR(...)` column type and `MYVECTOR_IS_ANN`), so run it on a plugin image
such as `ghcr.io/askdba/myvector:mysql8.4`.

Dataset Source : <https://www.kaggle.com/datasets/piyushjain16/amazon-product-data>

Rows : 2000000 (2 Million)

Embeddings : 768 Dimension using SentenceTransformer model **all-mpnet-base-v2**

<https://huggingface.co/sentence-transformers/all-mpnet-base-v2>

- Create the table using the following SQL statement

Please review the following parameters : ```M, ef, threads``` in the MYVECTOR
column specification. On our benchmark VM, it took 2 minutes to create the
vector index with the following parameters : ```M=64,ef=128,threads=48```.
That's right, only 2 minutes! The SQL script below has ```threads=4```,
please increase if your VM has more compute.

```sql
create table amazon_products
 (
  id int primary key auto_increment,
  product_listing varchar(8192),
  vec MYVECTOR(type=HNSW,dim=768,size=2100000,M=64,ef=128,ef_search=64,threads=4,dist=L2)
 );
```

- Load the dataset

SQL scripts for INSERT'ing the 2 million rows are available in Google Drive.
Both files are around 4.6 GB each (compressed). Their uncompressed sizes are
around 11.5 GB each. Please download them using below commands :-

```bash
wget "https://drive.usercontent.google.com/download?id=1Uwcalzh_yuTukkJfDRcoAm0Tp-ZSqV2t&export=download&authuser=0&confirm=a" -O insert1.sql.gz

wget "https://drive.usercontent.google.com/download?id=1EnpCL7kqc1xT5HSPyxBqRlh27Br1CT7R&export=download&authuser=0&confirm=a" -O insert2.sql.gz
```

Both the scripts run the INSERTS under a single transaction and COMMIT at the end. It should take around a couple of minutes each to execute the load scripts.

Example command to run the INSERT scripts

```bash
gunzip insert1.sql.gz

mysql -u <user> -p<password> < insert1.sql

gunzip insert2.sql.gz

mysql -u <user> -p<password> < insert2.sql
```

- Create the Vector Index

```sql
 call mysql.myvector_index_build('test.amazon_products.vec','id');
```

This step requires around 9GB of memory if 2 million rows were loaded into the table, around 4.5GB if only a single INSERT script was run.

- Vector Search Examples

The process of semantic search is simple - Accept a search query from user in
natural language -> Embed the search query using the embedding model -> Search
the query embedding vector in the vector index to find the nearest neighbours
-> Retrieve rows from the MySQL operation table corresponding to the
neighbours

A complete Python script is provided below. Make sure to edit the connection properties.

```python
# search.py - MyVector Demo Script
#
import sys

import mysql.connector

from sentence_transformers import SentenceTransformer
import numpy as np

np.set_printoptions(suppress=True, threshold=np.inf, linewidth=np.inf, formatter={'float_kind':'{:1.10f}'.format})


model = SentenceTransformer('all-mpnet-base-v2')

input_query = sys.argv[1]

print("Generating vector embedding for search query : '", input_query, "' ...", flush=True,end='\n')

embeddings = model.encode(input_query)

mysql_query = "select id, substr(product_listing,1, 200) from amazon_products"
" WHERE MYVECTOR_IS_ANN('test.amazon_products.vec','id',myvector_construct('"
+ str(embeddings) + "'));"

print("Connecting to MySQL ....", flush=True)

mydb = mysql.connector.connect(
  host="localhost",
  user="<user>",
  password="<password>",
  unix_socket="/tmp/mysql.sock",
  database="test"

)

print("Executing semantic search using MyVector and fetching results...", flush=True)

mycursor = mydb.cursor()

mycursor.execute(mysql_query)

myresult = mycursor.fetchall()

for x in myresult:
  print(x)
```
