# Movie Finder Demo App Design

## Summary

A demo web app that searches about a million real movies by meaning, with the vector
search running inside MySQL through MyVector. A user types a description ("a heist that
goes wrong in a snowy town") and gets matching films with posters. They can narrow the
results with ordinary SQL filters, click a film for "more like this", compare HNSW with
exact search, and add a movie that becomes searchable at once.

It lives in `examples/movie-finder/` and starts with one `docker compose up`. The repo
holds code and docs only: each user's machine downloads the TMDB data at setup time.

## Decisions (agreed in brainstorming, 2026-10-03)

| Question | Decision |
|---|---|
| Use case | Movie finder |
| Data | TMDB, about 1.04M movies, from the Hugging Face mirror `remsky/Embeddings__Ultimate_1Million_Movies_Dataset` (no account needed); the source URL is configurable |
| Redistribution | None: the data is downloaded by the user and never committed or baked into an image |
| Embeddings | Local CPU model, no API key (see "Change after measuring" below) |
| App shape | FastAPI backend + one page + docker compose with the published MyVector image |
| Scale | 1M+ rows, with smaller profiles for quick starts |
| Posters | Loaded from TMDB's image CDN (`image.tmdb.org`) at view time, never stored |

### Change after measuring: vectors from the dataset, not re-embedded

The approved design embedded every film locally with all-MiniLM-L6-v2 (384 dims).
Measured on the 16-core Neoverse-N1 development host, that runs at about 120 documents
per second: about 2 hours for the 808k films that have an overview. That is too slow for
a demo setup.

The dataset already carries a 768-dim vector for every film. Embedding the same text
locally with **nomic-embed-text-v1.5** (via `fastembed`, ONNX on CPU, no prefix)
reproduces the stored vectors exactly: cosine 1.0000 on 5 of 5 sampled films. So the
loader keeps the dataset's vectors, and the app embeds only the user's query, with the
same model, locally, in 20–70 ms in the app (6 ms in a tight loop). This keeps "local
model, no API key". It removes the corpus-embedding step, and it makes all 1,035,695 films searchable instead of 808k.

Costs: a larger download (the vector column is most of the 7 GB file), and 768 dims
instead of 384 (3.2 GB of raw vectors at full size).

## Data

Measured on the mirror (2026-10-03):

- 1,035,695 rows, unique `id` (TMDB id, max 5,180,730: fits INT).
- 807,903 rows have an overview of 40+ characters; median 37 words, p90 97.
- Genres are a comma-separated string ("Comedy, Drama, Romance").
- `popularity` is a 2024 snapshot that favours new releases, so profiles order films by
  `vote_count` (well-known films first; 87,531 films have 10+ votes).
- Parquet column projection over HTTP works: the metadata columns (235 MB) download in
  about 7 seconds without the vector column.

## Profiles

`MOVIES=10k | 100k | full` (default `100k`): the top N films by `vote_count`, then
`popularity`. `full` loads every row. The 10k profile is for a first look and for the
smoke test; `full` is the scale demo.

## Services (docker compose)

- **mysql**: `ghcr.io/askdba/myvector:mysql9.7-component`, with `myvector.cnf` written
  before the component is installed (as in `docs/DEMO.md`). The image tag is a variable,
  so the demo can run against a local build.
- **loader** (runs once, exits): downloads the chosen profile to a named volume (Parquet,
  metadata + vectors), creates the schema, inserts in batches, builds the index with
  `threads=` sized to the machine, and records what it loaded. Rerunning with the same
  profile skips the download. Files older than 6 months are re-downloaded (TMDB terms).
- **app**: FastAPI + one HTML page (no frontend build step). It loads nomic-embed-text-v1.5
  at start and embeds query text only.

## Schema

```sql
CREATE TABLE movies (
  id            INT PRIMARY KEY,           -- TMDB id
  title         VARCHAR(512) NOT NULL,
  year          SMALLINT NULL,
  release_date  DATE NULL,
  overview      TEXT,
  tagline       VARCHAR(1024),
  vote_average  DECIMAL(3,1),
  vote_count    INT,
  popularity    DOUBLE,
  language      CHAR(8),
  runtime       SMALLINT NULL,
  director      VARCHAR(512),
  poster_path   VARCHAR(128),
  embedding     VARBINARY(3072) COMMENT
    'MYVECTOR COLUMN type=HNSW,dim=768,size=1100000,M=16,ef=100,dist=Cosine,online=Y,idcol=id,threads=N',
  KEY (year), KEY (vote_average), KEY (language)
);
CREATE TABLE movie_genres (
  movie_id INT NOT NULL, genre VARCHAR(32) NOT NULL,
  PRIMARY KEY (genre, movie_id), KEY (movie_id)
);
```

On MySQL 9.x, MyVector stores a vector as raw little-endian FP32 (no header), so the
loader sends the bytes directly. On 8.x a stored vector has an 8-byte header, so the
loader falls back to `myvector_construct('[...]')`. The demo targets 9.7.

## The page

Every result panel has a "SQL" toggle showing the exact statement that ran, with timing.

1. **Describe a movie**: query text → nomic embedding → `MYVECTOR_IS_ANN(..., k)`.
   Results show poster, title, year, genres, rating, overview and distance.
2. **Filters**: genre, year range, minimum rating, language.
   - A narrow filter passes the matching keys as `JSON_ARRAYAGG` (fifth argument of
     `myvector_ann_set`).
   - A broad filter (over 50,000 matches) uses `MYVECTOR_ANN_FILTERED`.
   - To pick the path, the app counts each filter on its own, from its own index
     (`movie_genres` primary key, `year`, `(vote_average, vote_count)`, `language`), and
     takes the smallest count as an upper bound. Counting the combined predicate at
     once took 130 s at 1M rows, because it read every matching row; the per-filter
     counts take milliseconds.
   - The panel says which path ran and why. This is the "vectors inside MySQL" point.
   - **Rank**: "+ popularity" (default) or "Similarity". At 1M films, many obscure films
     sit as close to a description as the famous one: "a boy wizard at a school of
     magic" put "The Magic School" first and left Harry Potter out of the top 10. The
     default ranks 5×k HNSW candidates by
     `distance - 0.04 * LOG10(1 + vote_count)`, in SQL. The weight was picked on the six
     example queries at 1M: 0.02 still put vote-less near-duplicates first, 0.05 began
     to override the description. Compare mode always ranks by distance alone.
3. **More like this**: the clicked film's stored embedding is the query; pure SQL, no
   model call.
4. **HNSW vs exact**: the same query as `myvector_ann_set` and as
   `ORDER BY myvector_distance(...) LIMIT k`, showing both times and the overlap of the
   two top-k lists (recall@k).
   - The exact scan finds the k nearest ids first, then fetches their details: with
     the details in the scan, MySQL computed them for every row before sorting.
   - It reads the whole table, so the table must fit in the buffer pool. Cold, a 1M-row
     scan took over 2 minutes on the development host; warm, 3 s. Compose sets
     `innodb_buffer_pool_size` (default 4G, `MYSQL_BUFFER_POOL`) and dumps 100% of the
     pool at shutdown so a restart stays warm.
   - The loader inserts in primary-key order. Inserting most-voted first split pages
     and doubled the table to 6.7 GB.
5. **Add a movie**: title, year, genres and overview, embedded by the app and INSERTed;
   the `online=Y` listener adds it to the index, and the page re-runs the search to show
   it appear.
6. **Stats strip**: rows, index status (`MYVECTOR_INDEX_STATUS`), last query time, and an
   `ef_search` slider (passed per query: `nn=10,ef_search=N`).

The footer shows the TMDB logo and the notice TMDB's terms require: "This website uses
TMDB and the TMDB APIs but is not endorsed, certified, or otherwise approved by TMDB."

## Licensing

- TMDB content is under TMDB's terms (non-commercial use with attribution; no caching
  beyond 6 months). The Hugging Face mirror is labelled Apache 2.0, but that does not
  change TMDB's terms, so the README treats the data as TMDB's.
- The README says: non-commercial demo; the user downloads the data; MyVector does not
  redistribute it.
- nomic-embed-text-v1.5 is Apache 2.0.

## Dependency on a release

The published images are v1.26.9. Most of what the page shows is on `main` but not
released:

- the key-list filter, the fifth argument of `myvector_ann_set` (#157);
- `MYVECTOR_ANN_FILTERED`;
- the component `MYVECTOR_IS_ANN` rewrite (#156);
- per-query `ef_search` (#165);
- online inserts after a restart (#186, #190), and online DELETE/UPDATE (#188, #194).

On v1.26.9 only the unfiltered search, "more like this" and exact search work, and
"Add a movie" works only until the container restarts.

So the demo is built and tested against `main`. The compose file reads the image from
`MYVECTOR_IMAGE`, which defaults to `ghcr.io/askdba/myvector:mysql9.7-component`. That
default is correct once a release ships; until then, the README shows how to build the
image locally. The app uses `myvector_ann_set()` with `JSON_TABLE` (which keeps the rank
order) rather than `MYVECTOR_IS_ANN`, because that works on every build. Tag a release
before recording or announcing the demo.

## Testing

- `examples/movie-finder/smoke.sh`:
  - Runs the 10k profile end to end.
  - Checks known queries: "a boy wizard at a school of magic" must return Harry Potter
    and the Philosopher's Stone (id 671) in the top 10.
  - Checks that a genre filter returns only that genre and still returns k rows.
  - Checks that "more like this" on The Matrix (603) excludes the film itself.
  - Checks that HNSW vs exact recall@10 is ≥ 0.9.
  - Checks that an added movie is found within 5 seconds.
- Full profile timed on the development host: download, insert and index build. The
  numbers are recorded in the README.
- Not in CI: the data is a large external download, and TMDB's terms are a reason not
  to fetch it in CI.

## Out of scope

- A hosted public instance.
- Re-embedding with other models (the source and model are configurable, but only nomic
  is tested).
- RAG or LLM answers.
