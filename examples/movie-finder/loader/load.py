#!/usr/bin/env python3
"""Load TMDB movies and their vectors into MySQL, then build the MyVector index.

Runs once from docker compose (the `loader` service) and exits. Steps:

  1. Download the dataset once into the cache volume: the metadata columns plus the
     768-dim nomic-embed-text-v1.5 vectors, as a compact Parquet file. A cache older
     than 180 days is downloaded again (TMDB's terms allow caching for 6 months).
  2. Pick the profile's movies: the N with the most votes (MOVIES=10k / 100k / full).
  3. Create the schema, insert in batches, and build the HNSW index.

If the database already holds the requested profile with a built index, it exits at
once, so `docker compose up` after the first run is fast.
"""

import json
import os
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pymysql

SOURCE_URL = os.environ.get(
    "SOURCE_URL",
    "https://huggingface.co/datasets/remsky/Embeddings__Ultimate_1Million_Movies_Dataset"
    "/resolve/main/movies_with_embeddings.parquet")
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/cache"))
CACHE_FILE = CACHE_DIR / "tmdb-movies.parquet"
CACHE_INFO = CACHE_DIR / "tmdb-movies.json"
CACHE_MAX_AGE_DAYS = 180

PROFILES = {"10k": 10_000, "100k": 100_000, "full": None}
PROFILE = os.environ.get("MOVIES", "100k")
DIM = 768
DB = os.environ.get("MYSQL_DATABASE", "movies")
INDEX = f"{DB}.movies.embedding"
BATCH = int(os.environ.get("BATCH", "1000"))
BUILD_THREADS = int(os.environ.get("BUILD_THREADS", "0")) or min(os.cpu_count() or 2, 16)
# Room for movies added from the page, above the profile's size.
SIZE_HEADROOM = 50_000

COLUMNS = ("id, title, release_date, overview, tagline, genres, vote_average, vote_count, "
           "popularity, original_language, runtime, director, poster_path")


def log(msg):
    print(f"[loader {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── 1. download ──────────────────────────────────────────────────────────────

def cache_is_fresh():
    if not (CACHE_FILE.exists() and CACHE_INFO.exists()):
        return False
    info = json.loads(CACHE_INFO.read_text())
    age = datetime.now(timezone.utc) - datetime.fromisoformat(info["downloaded_at"])
    if age.days >= CACHE_MAX_AGE_DAYS:
        log(f"cache is {age.days} days old; TMDB allows 6 months, downloading again")
        return False
    return info.get("source") == SOURCE_URL


def download():
    if cache_is_fresh():
        log(f"using cached dataset {CACHE_FILE}")
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_FILE.with_suffix(".part")
    log(f"downloading {SOURCE_URL}")
    log("about 7 GB, once; later runs and other profiles reuse the cache")
    t = time.time()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"""
        COPY (SELECT {COLUMNS}, CAST(embedding AS FLOAT[{DIM}]) AS embedding
              FROM read_parquet('{SOURCE_URL}')
              WHERE embedding IS NOT NULL)
        TO '{tmp}' (FORMAT parquet, COMPRESSION zstd)""")
    tmp.rename(CACHE_FILE)
    rows = con.execute(f"SELECT count(*) FROM '{CACHE_FILE}'").fetchone()[0]
    CACHE_INFO.write_text(json.dumps({
        "source": SOURCE_URL, "rows": rows,
        "downloaded_at": datetime.now(timezone.utc).isoformat()}))
    log(f"downloaded {rows:,} movies in {time.time() - t:.0f}s")


def select_movies():
    """The profile's rows from the cache: the N most-voted movies, in id order.

    Inserting in primary-key order fills InnoDB pages; in vote order, the inserts land
    all over the index, split pages, and leave the table about twice its size.
    """
    limit = PROFILES[PROFILE]
    sql = f"SELECT * FROM '{CACHE_FILE}'"
    if limit:
        sql = (f"SELECT * FROM ({sql} ORDER BY vote_count DESC NULLS LAST, "
               f"popularity DESC NULLS LAST, id LIMIT {limit})")
    return duckdb.connect().execute(sql + " ORDER BY id")


# ── 2. MySQL ─────────────────────────────────────────────────────────────────

def connect(db=None):
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "mysql"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "root"),
        password=os.environ["MYSQL_PASSWORD"],
        database=db, autocommit=False, charset="utf8mb4",
        read_timeout=None, write_timeout=None)


def wait_for_myvector():
    """Wait until MySQL accepts connections and the MyVector component is installed."""
    deadline = time.time() + 600
    last = None
    while time.time() < deadline:
        try:
            with connect() as c, c.cursor() as cur:
                cur.execute("SELECT myvector_construct('[1]') IS NOT NULL, VERSION()")
                _, version = cur.fetchone()
                return version
        except pymysql.MySQLError as e:
            last = e
            time.sleep(3)
    sys.exit(f"MySQL with MyVector not ready after 10 minutes: {last}")


def already_loaded(cur):
    try:
        cur.execute(f"SELECT v FROM {DB}.demo_state WHERE k = 'loaded'")
        row = cur.fetchone()
    except pymysql.MySQLError:
        return False
    return bool(row) and json.loads(row[0]).get("profile") == PROFILE


def create_schema(cur, size):
    log(f"creating {DB}.movies (index size {size:,}, build threads {BUILD_THREADS})")
    cur.execute(f"CREATE DATABASE IF NOT EXISTS {DB}")
    # A previous profile's index: drop it with the table, so its files do not linger.
    try:
        cur.execute(f"CALL mysql.myvector_index_drop('{INDEX}')")
        while cur.nextset():
            pass
    except pymysql.MySQLError:
        pass
    cur.execute(f"DROP TABLE IF EXISTS {DB}.movie_genres, {DB}.movies, {DB}.demo_state")
    cur.execute(f"""
        CREATE TABLE {DB}.movies (
          id           INT PRIMARY KEY,
          title        VARCHAR(512) NOT NULL,
          year         SMALLINT NULL,
          release_date DATE NULL,
          overview     TEXT,
          tagline      VARCHAR(1024),
          vote_average DECIMAL(3,1),
          vote_count   INT,
          popularity   DOUBLE,
          language     VARCHAR(8),
          runtime      SMALLINT NULL,
          director     VARCHAR(512),
          poster_path  VARCHAR(128),
          embedding    VARBINARY({DIM * 4 + 8}) COMMENT
            'MYVECTOR COLUMN type=HNSW,dim={DIM},size={size},M=16,ef=100,dist=Cosine,online=Y,idcol=id,threads={BUILD_THREADS}',
          -- Each filter can be counted from its own index, without reading the rows.
          KEY (year), KEY (vote_average, vote_count), KEY (language)
        )""")
    cur.execute(f"""
        CREATE TABLE {DB}.movie_genres (
          movie_id INT NOT NULL,
          genre    VARCHAR(32) NOT NULL,
          PRIMARY KEY (genre, movie_id),
          KEY (movie_id)
        )""")
    cur.execute(f"CREATE TABLE {DB}.demo_state (k VARCHAR(32) PRIMARY KEY, v JSON)")


def to_int(x, lo, hi):
    if x is None:
        return None
    x = int(x)
    return x if lo <= x <= hi else None


def year_of(date):
    """release_date is a 'YYYY-MM-DD' string; some are blank or far out of range."""
    if not date or len(date) < 10:
        return None, None
    y = to_int(date[:4], 1870, 2100)
    return y, (date[:10] if y else None)


def insert_movies(con, rows, raw_vectors):
    """Insert movies and genres in batches; returns the row count.

    On MySQL 9.x MyVector stores FP32 vectors as raw little-endian floats, so the
    bytes go in as they are. On 8.x a stored vector has a header, so build it with
    myvector_construct() from text instead.
    """
    vec_sql = "%s" if raw_vectors else "myvector_construct(%s)"
    movie_sql = (f"INSERT INTO {DB}.movies (id, title, year, release_date, overview, tagline, "
                 "vote_average, vote_count, popularity, language, runtime, director, "
                 f"poster_path, embedding) VALUES "
                 f"(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,{vec_sql})")
    genre_sql = f"INSERT IGNORE INTO {DB}.movie_genres (movie_id, genre) VALUES (%s,%s)"
    pack = struct.Struct(f"<{DIM}f").pack
    n, t = 0, time.time()
    with con.cursor() as cur:
        # The bulk load does not go to the binlog: the index build reads the table, and
        # the online listener only needs to follow changes made after the build.
        cur.execute("SET SESSION sql_log_bin = 0")
        while True:
            batch = rows.fetchmany(BATCH)
            if not batch:
                break
            movies, genres = [], []
            for (mid, title, release_date, overview, tagline, genre_str, vote_avg, votes,
                 popularity, lang, runtime, director, poster, emb) in batch:
                year, date = year_of(release_date)
                vec = pack(*emb) if raw_vectors else "[" + ",".join(f"{x:.7g}" for x in emb) + "]"
                movies.append((mid, (title or "")[:512], year, date, overview,
                               (tagline or None) and tagline[:1024],
                               None if vote_avg is None else round(min(max(vote_avg, 0), 10), 1),
                               to_int(votes, 0, 2**31 - 1), popularity, (lang or "")[:8] or None,
                               to_int(runtime, 1, 32000), (director or None) and director[:512],
                               poster, vec))
                for g in (genre_str or "").split(","):
                    if g.strip():
                        genres.append((mid, g.strip()[:32]))
            cur.executemany(movie_sql, movies)
            if genres:
                cur.executemany(genre_sql, genres)
            con.commit()
            n += len(batch)
            if n % (BATCH * 50) == 0:
                log(f"  {n:,} rows, {n / (time.time() - t):,.0f} rows/s")
    log(f"inserted {n:,} movies in {time.time() - t:.0f}s")
    return n


def build_index(cur):
    log(f"building the HNSW index on {INDEX} ({BUILD_THREADS} threads)")
    t = time.time()
    cur.execute(f"CALL mysql.myvector_index_build('{INDEX}', 'id')")
    out = cur.fetchall()
    while cur.nextset():
        pass
    log(f"index built in {time.time() - t:.0f}s: {out}")
    return time.time() - t


def main():
    if PROFILE not in PROFILES:
        sys.exit(f"MOVIES must be one of {', '.join(PROFILES)}, not {PROFILE!r}")
    version = wait_for_myvector()
    raw_vectors = int(version.split(".")[0]) >= 9
    log(f"MySQL {version}, MyVector installed; profile {PROFILE}")

    with connect() as con, con.cursor() as cur:
        if already_loaded(cur):
            log(f"{DB} already holds the {PROFILE} profile; nothing to do")
            return

    t0 = time.time()
    download()
    t1 = time.time()
    rows = select_movies()
    with connect() as con:
        with con.cursor() as cur:
            limit = PROFILES[PROFILE]
            total = limit or duckdb.connect().execute(f"SELECT count(*) FROM '{CACHE_FILE}'").fetchone()[0]
            create_schema(cur, total + SIZE_HEADROOM)
        con.commit()
        n = insert_movies(con, rows, raw_vectors)
        t2 = time.time()
        with con.cursor() as cur:
            build_s = build_index(cur)
            state = {"profile": PROFILE, "rows": n, "mysql": version,
                     "download_s": round(t1 - t0), "insert_s": round(t2 - t1),
                     "build_s": round(build_s), "build_threads": BUILD_THREADS,
                     "loaded_at": datetime.now(timezone.utc).isoformat()}
            cur.execute(f"INSERT INTO {DB}.demo_state VALUES ('loaded', %s)", (json.dumps(state),))
        con.commit()
    log(f"done: {json.dumps(state)}")


if __name__ == "__main__":
    main()
