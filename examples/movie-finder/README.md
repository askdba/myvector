# Movie Finder

Search about a million real movies by describing them in your own words, with the vector
search running inside MySQL through MyVector.

- **Describe a movie** ("a heist that goes wrong in a snowy town") and get matching films,
  with posters.
- **Filter** by genre, year, rating and language: ordinary SQL, combined with the
  vector search. The page explains which filtered-search path ran and why.
- **Rank** by similarity alone, or (the default) with a small boost for films many
  people have rated, so the famous match beats an obscure one at almost the same
  distance. Both are plain SQL `ORDER BY` expressions.
- **More like this**: a film's stored vector becomes the query, in pure SQL.
- **HNSW vs exact**: run both and see the speed difference and the recall.
- **Add a movie**: it becomes searchable within about a second, with no index rebuild
  (`online=Y`).

Every result panel shows the SQL that ran.

## Run it

```bash
cd examples/movie-finder
MOVIES=100k docker compose up        # then open http://localhost:8080
```

The page and MySQL (port 3307) listen on localhost only. The demo uses MySQL's root
account with a known password and lets anyone with the page add movies, so share it
with `APP_BIND=0.0.0.0` only on a network you trust.

Running it on a remote server? Forward the port over SSH instead of opening it:
`ssh -N -L 8080:127.0.0.1:8080 <server>`, then browse to http://localhost:8080 on your
own machine.

`MOVIES` picks how many films to load, most-voted first:

| Profile | Movies | Notes |
|---|---|---|
| `10k` | 10,000 | Quick first look |
| `100k` | 100,000 | Default |
| `full` | 1,035,695 | Every movie in the dataset; give Docker about 12 GB of memory and set `MYSQL_BUFFER_POOL=6G` |

Measured on a 16-core Arm (Neoverse-N1) Linux host with MySQL 9.7.2:

| Step | 10k | 100k | full |
|---|---|---|---|
| Download (about 7 GB, depends on your connection) and convert, once | 290 s | 290 s | 290 s |
| Insert | 7 s | 41 s | 407 s |
| Build the HNSW index (16 threads) | 29 s | 33 s | 545 s |
| HNSW search | 3–5 ms | 4–7 ms | 5–20 ms |
| Exact search (scans every row) | 30 ms | 280 ms | 3 s warm; 9–22 s with the default 4G buffer pool |
| Recall@10, HNSW vs exact | 100% | 100% | 100% |

Recall is for the unfiltered smoke-test query. Filtered searches can score lower: a broad
genre filter (Drama) at full size gave 80% at `ef_search` 100; raise `ef_search` on the
page to trade time for recall.

The exact search reads the whole table, so it is only fast when the table fits in
InnoDB's buffer pool (`MYSQL_BUFFER_POOL`, default `4G`). The full profile's table is
about 4 GB.

The first start downloads the dataset (about 7 GB, once) and keeps a 3 GB local copy
in the `tmdb-cache` volume. To switch profiles, start again with another `MOVIES`
(`MOVIES=full docker compose up`): the loader sees the change, replaces the tables and
rebuilds the index from the cached copy. `docker compose down -v` removes everything,
including the cached download.

### MyVector version

The demo needs MyVector features that are newer than the v1.26.9 images: filtered
search, `MYVECTOR_ANN_FILTERED`, per-query `ef_search`, and online updates that survive
a restart. Until a release includes them, build a local image from `main`:

```bash
# from the repository root; ARCH is arm64 or amd64, matching your machine
ARCH=arm64
./scripts/build-component-9.7-docker.sh mysql-9.7.0 build/component-local
cp build/component-local/libmyvector_component.so myvector-component-$ARCH.so
cp build/component-local/myvector.json sql/myvector_install_component.sql sql/myvector_uninstall_component.sql .
docker build -f Dockerfile.component --build-arg MYSQL_VERSION=9.7 \
    --build-arg TARGETARCH=$ARCH -t myvector-local:mysql9.7-component .

cd examples/movie-finder
MYVECTOR_IMAGE=myvector-local:mysql9.7-component docker compose up
```

## How it works

```
 browser ── FastAPI app ── MySQL 9.7 + MyVector component
              │                 movies (768-dim vectors, HNSW index, online=Y)
              │                 movie_genres
   nomic-embed-text-v1.5
   (query text only, CPU)
```

- **Data**: [TMDB](https://www.themoviedb.org/) movie metadata, via the Hugging Face
  mirror
  [`remsky/Embeddings__Ultimate_1Million_Movies_Dataset`](https://huggingface.co/datasets/remsky/Embeddings__Ultimate_1Million_Movies_Dataset).
  Each film comes with a 768-dimension vector made by
  [nomic-embed-text-v1.5](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5) from
  "title: tagline: overview". The loader keeps those vectors, so nothing is re-embedded.
- **Query text** is embedded by the same model, locally on the CPU (no API key), in
  20–70 ms. The same model embeds movies you add.
- **The loader** (`loader/load.py`) downloads only the columns it needs, inserts the
  profile's films with the binlog off for the bulk load, and builds the index:

  ```sql
  embedding VARBINARY(3080) COMMENT
    'MYVECTOR COLUMN type=HNSW,dim=768,size=...,M=16,ef=100,dist=Cosine,online=Y,idcol=id,threads=N'
  ```

- **Search** (`app/main.py`) uses `myvector_ann_set()` unpacked with `JSON_TABLE`, which
  keeps the nearest-first order:

  ```sql
  SELECT m.title, myvector_distance(m.embedding, @q, 'Cosine') AS distance
  FROM (SELECT myvector_ann_set('movies.movies.embedding', 'id', @q, 'nn=10,ef_search=100') AS js) src,
       JSON_TABLE(src.js, '$[*]' COLUMNS (rank_no FOR ORDINALITY, id INT PATH '$')) nn
  JOIN movies.movies m ON m.id = nn.id
  ORDER BY nn.rank_no;
  ```

- **Filters**: the app first counts each filter on its own, from its own index, and
  takes the smallest count as an upper bound on the matches. (Counting the combined
  filter at once reads every matching row, which took two minutes at 1M movies.)
  - At most 50,000: it passes the matching keys to the search, the fifth argument of
    `myvector_ann_set`, so the search returns the nearest films among them.
  - More than that: it calls `MYVECTOR_ANN_FILTERED`, which takes the nearest candidates
    and keeps those that pass.

  See [Filtered search](../../docs/usage.md) in the usage guide.
- **Add a movie** is a plain `INSERT`. MyVector's binlog listener adds the row to the
  HNSW index; the page polls until the film is found by the index.

## Test

With the app running (any profile; `10k` is enough):

```bash
python3 smoke.py
```

It checks a known query (Harry Potter for "a boy wizard at a school of magic"), a narrow
and a broad genre filter, "more like this", HNSW recall against exact search, and that an
added movie is searchable within 5 seconds.

## Data and licensing

This is a **non-commercial demo**.

- The movie data and posters come from TMDB and are subject to the
  [TMDB terms of use](https://www.themoviedb.org/api-terms-of-use).
- The Hugging Face mirror is labelled Apache 2.0, but that does not change TMDB's terms.
- Your machine downloads the data when you start the demo. MyVector does not
  redistribute it, and none of it is in this repository or in any image.
- The loader downloads the data again once the cached copy is 6 months old, the longest
  TMDB allows.
- Posters are loaded from TMDB's image server when the page shows them; they are never
  stored.
- nomic-embed-text-v1.5 is Apache 2.0.

This demo uses TMDB and the TMDB APIs but is not endorsed, certified, or otherwise
approved by TMDB.
