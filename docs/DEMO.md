***Amazon Product Catalog Dataset with Embeddings***

This is the best MyVector Demo! We are going to load a real world product catalog (from Amazon) into a MySQL database, build an HNSW index and then perform vector search to find semantically relevant results.

Sneak Peek : https://asciinema.org/a/709302

Dataset Source : https://www.kaggle.com/datasets/piyushjain16/amazon-product-data

Rows : 2000000 (2 Million)

Embeddings : 768 Dimension using SentenceTransformer model **all-mpnet-base-v2**

https://huggingface.co/sentence-transformers/all-mpnet-base-v2    

- Create the table using the following SQL statement

Please review the following parameters : ```M, ef, threads``` in the MYVECTOR column specification. On our benchmark VM, it took 2 minutes to create the vector index with the following parameters : ```M=64,ef=128,threads=48```. That's right, only 2 minutes! The SQL script below has ```threads=4```, please increase if your VM has more compute.

```
create table amazon_products
 (
  id int primary key auto_increment,
  product_listing varchar(8192),
  vec MYVECTOR(type=HNSW,dim=768,size=2100000,M=64,ef=128,ef_search=64,threads=4,dist=L2)
 );


```
- Load the dataset

SQL scripts for INSERT'ing the 2 million rows are available in Google Drive. Both files are around 4.6 GB each (compressed). Their uncompressed sizes are around 11.5 GB each. Please download them using below commands :-
```
$ wget "https://drive.usercontent.google.com/download?id=1Uwcalzh_yuTukkJfDRcoAm0Tp-ZSqV2t&export=download&authuser=0&confirm=a" -O insert1.sql.gz

$ wget "https://drive.usercontent.google.com/download?id=1EnpCL7kqc1xT5HSPyxBqRlh27Br1CT7R&export=download&authuser=0&confirm=a" -O insert2.sql.gz                     
```

Both the scripts run the INSERTS under a single transaction and COMMIT at the end. It should take around a couple of minutes each to execute the load scripts.

Example command to run the INSERT scripts

```
$ gunzip insert1.sql.gz

$ mysql -u <user> -p<password> < insert1.sql

$ gunzip insert2.sql.gz

$ mysql -u <user> -p<password> < insert2.sql

```

- Create the Vector Index

```
 call mysql.myvector_index_build('test.amazon_products.vec','id');
```

This step requires around 9GB of memory if 2 million rows were loaded into the table, around 4.5GB if only a single INSERT script was run.

- Vector Search Examples

The process of semantic search is simple - Accept a search query from user in natural language -> Embed the search query using the embedding model -> Search the query embedding vector in the vector index to find the nearest neighbours -> Retrieve rows from the MySQL operation table corresponding to the neighbours

A complete Python script is provided below. Make sure to edit the connection properties.

```
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

mysql_query = "select id, substr(product_listing,1, 200) from amazon_products WHERE MYVECTOR_IS_ANN('test.amazon_products.vec','id',myvector_construct('" + str(embeddings) + "'));"

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

