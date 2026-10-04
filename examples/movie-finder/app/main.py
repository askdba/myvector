"""MyVector Movie Finder: semantic movie search with the vectors inside MySQL.

The app embeds the user's text with nomic-embed-text-v1.5 (local, CPU) and asks MySQL
for the nearest movies through MyVector. Every response includes the SQL that ran, so
the page can show it.

Searches use myvector_ann_set() unpacked with JSON_TABLE, which keeps the nearest-first
order and works on every MyVector build (the MYVECTOR_IS_ANN rewrite is not in component
builds up to v1.26.9).
"""

import os
import re
import struct
import threading
import time
from contextlib import asynccontextmanager, contextmanager

import pymysql
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastembed import TextEmbedding
from pydantic import BaseModel, Field

DB = os.environ.get("MYSQL_DATABASE", "movies")
INDEX = f"{DB}.movies.embedding"
DIM = 768
MODEL = "nomic-ai/nomic-embed-text-v1.5"
# Above this many matching rows a filter is "broad": MYVECTOR_ANN_FILTERED is cheaper
# than building a key list of every match (see docs/usage.md, "Filtered search").
KEYLIST_MAX = int(os.environ.get("KEYLIST_MAX", "50000"))
# Movies added from the page get ids above every TMDB id.
ADDED_ID_BASE = 100_000_000
MIN_VOTES_FOR_RATING = 20
# "Popular" ranking: distance minus this times log10(1 + votes). 0.04 lifts a film with
# 10,000 votes by 0.16 over one with none. Tried on the example queries at 1M movies:
# 0.02 still put vote-less near-duplicates first, 0.05 began to override the description.
POP_WEIGHT = float(os.environ.get("RANK_POP_WEIGHT", "0.04"))
EXACT_TIMEOUT_S = int(os.environ.get("EXACT_TIMEOUT_S", "90"))

HERE = os.path.dirname(os.path.abspath(__file__))

_model = None
_model_lock = threading.Lock()
_raw_vectors = True          # MySQL 9.x stores FP32 vectors without a header
_genres: list[str] = []
_languages: list[str] = []
_pack = struct.Struct(f"<{DIM}f").pack


def connect():
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "mysql"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ.get("MYSQL_USER", "root"),
        password=os.environ["MYSQL_PASSWORD"],
        database=DB, autocommit=True, charset="utf8mb4")


@contextmanager
def cursor():
    con = connect()
    try:
        with con.cursor(pymysql.cursors.DictCursor) as cur:
            yield cur
    finally:
        con.close()


def model():
    global _model
    with _model_lock:
        if _model is None:
            _model = TextEmbedding(MODEL, cache_dir=os.environ.get("MODEL_CACHE", "/models"))
        return _model


def embed(text: str) -> list[float]:
    return list(next(iter(model().embed([text]))))


def set_query_vector(cur, vec: list[float]):
    """SET @q for this connection. Returns how the page shows it."""
    if _raw_vectors:
        cur.execute("SET @q = %s", (_pack(*vec),))
    else:
        cur.execute("SET @q = myvector_construct(%s)", ("[" + ",".join(f"{x:.7g}" for x in vec) + "]",))
    return f"SET @q = <{DIM}-dim query vector>;"


def startup():
    global _raw_vectors, _genres, _languages
    with cursor() as cur:
        cur.execute("SELECT VERSION() AS v")
        _raw_vectors = int(cur.fetchone()["v"].split(".")[0]) >= 9
        cur.execute("SELECT genre, COUNT(*) AS n FROM movie_genres GROUP BY genre ORDER BY n DESC")
        _genres = [r["genre"] for r in cur.fetchall()]
        cur.execute("SELECT language, COUNT(*) AS n FROM movies WHERE language IS NOT NULL "
                    "GROUP BY language ORDER BY n DESC LIMIT 40")
        _languages = [r["language"] for r in cur.fetchall()]
    threading.Thread(target=model, daemon=True).start()   # warm the model


@asynccontextmanager
async def lifespan(_app):
    startup()
    yield


app = FastAPI(title="MyVector Movie Finder", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


# ── filters ──────────────────────────────────────────────────────────────────

class Filter:
    """The page's filters, validated. SQL is built only from these checked values."""

    def __init__(self, genres, year_min, year_max, min_rating, language):
        bad = [g for g in genres if g not in _genres]
        if bad:
            raise HTTPException(400, f"unknown genre: {bad[0]}")
        if language and (language not in _languages or not re.fullmatch(r"[a-z]{2,3}", language)):
            raise HTTPException(400, f"unknown language: {language}")
        self.genres = list(dict.fromkeys(genres))
        # Conditions on `movies` columns, each answerable from one index.
        self.parts = {}
        if year_min is not None or year_max is not None:
            y = []
            if year_min is not None:
                y.append(f"year >= {int(year_min)}")
            if year_max is not None:
                y.append(f"year <= {int(year_max)}")
            self.parts["year"] = " AND ".join(y)
        if min_rating is not None:
            self.parts["rating"] = (f"vote_average >= {float(min_rating):.1f} "
                                    f"AND vote_count >= {MIN_VOTES_FOR_RATING}")
        if language:
            self.parts["language"] = f"language = '{language}'"

    def __bool__(self):
        return bool(self.genres or self.parts)

    def genre_list(self):
        return ", ".join(pymysql.converters.escape_item(g, "utf8mb4") for g in self.genres)

    def predicate(self):
        """The whole filter as a WHERE predicate on `movies`."""
        p = list(self.parts.values())
        if self.genres:
            p.insert(0, f"id IN (SELECT movie_id FROM {DB}.movie_genres WHERE genre IN ({self.genre_list()}))")
        return " AND ".join(p)

    def count_queries(self):
        """One count per filter, each served by an index without reading the movie rows.

        The smallest is an upper bound on the movies that pass the whole filter. Counting
        the whole predicate at once can read every matching row of a 1M-row table.
        """
        q = {}
        if self.genres:
            q["genre"] = (f"SELECT COUNT(DISTINCT movie_id) AS n FROM {DB}.movie_genres "
                          f"WHERE genre IN ({self.genre_list()})")
        for name, cond in self.parts.items():
            q[name] = f"SELECT COUNT(*) AS n FROM {DB}.movies WHERE {cond}"
        return q

    def keys_sql(self):
        """A subquery returning the matching keys as a JSON array."""
        if self.genres and not self.parts:
            return (f"(SELECT JSON_ARRAYAGG(movie_id) FROM (SELECT DISTINCT movie_id "
                    f"FROM {DB}.movie_genres WHERE genre IN ({self.genre_list()})) g)")
        return f"(SELECT JSON_ARRAYAGG(id) FROM {DB}.movies WHERE {self.predicate()})"


DETAIL_COLS = ("m.id, m.title, m.year, m.vote_average, m.vote_count, m.language, m.runtime, "
               "m.director, m.poster_path, m.overview, "
               "(SELECT GROUP_CONCAT(genre ORDER BY genre SEPARATOR ', ') "
               f"FROM {DB}.movie_genres g WHERE g.movie_id = m.id) AS genres")
DISTANCE = "myvector_distance(m.embedding, @q, 'Cosine')"


def order_by(rank):
    """Final ordering: pure similarity, or similarity with a small boost for films many
    people have rated, so a well-known match beats an obscure one at almost the same
    distance."""
    if rank == "similar":
        return "distance"
    return f"distance - {POP_WEIGHT} * LOG10(1 + COALESCE(m.vote_count, 0))"


def timed(cur, sql, args=None):
    t = time.perf_counter()
    cur.execute(sql, args)
    rows = cur.fetchall()
    while cur.nextset():
        pass
    return rows, (time.perf_counter() - t) * 1000


def ann_search(cur, k, ef, f, rank, exclude_id=None):
    """Nearest movies by HNSW, honouring the filter. Returns (rows, steps, path)."""
    steps, path = [], {"kind": "plain", "why": "no filter"}
    # Re-ranking by popularity needs more candidates than it returns.
    nn = (k if rank == "similar" else max(5 * k, 50)) + (1 if exclude_id else 0)
    excl = f"WHERE m.id <> {int(exclude_id)}\n" if exclude_id else ""
    keys = ""
    if f:
        counts = {}
        for name, sql in f.count_queries().items():
            rows, ms = timed(cur, sql)
            counts[name] = rows[0]["n"]
            steps.append({"sql": sql + ";", "ms": ms, "stage": "filter",
                          "note": f"{counts[name]:,} movies"})
        bound = min(counts.values())
        at_most = "" if len(counts) == 1 else "at most "
        if bound == 0:
            return [], steps, {"kind": "empty", "why": "no movie matches the filter"}
        if bound > KEYLIST_MAX:
            path = {"kind": "ann_filtered",
                    "why": f"{bound:,} movies match: a broad filter (over {KEYLIST_MAX:,}), so "
                           "MYVECTOR_ANN_FILTERED takes the nearest candidates and keeps those that "
                           "pass, instead of listing every matching key"}
            predicate = f.predicate()
            shown = (f"CALL mysql.MYVECTOR_ANN_FILTERED('{INDEX}', 'id', @q, {nn},\n"
                     f"     '{predicate.replace(chr(39), chr(39) * 2)}');")
            hits, ms = timed(cur, "CALL mysql.MYVECTOR_ANN_FILTERED(%s, 'id', @q, %s, %s)",
                             (INDEX, nn, predicate))
            steps.append({"sql": shown, "ms": ms, "stage": "myvector",
                          "note": f"{len(hits)} nearest movies that pass the filter"})
            ids = [h["id"] for h in hits if h["id"] != exclude_id]
            if not ids:
                return [], steps, path
            id_list = ", ".join(str(int(i)) for i in ids)
            sql = (f"SELECT {DETAIL_COLS},\n       {DISTANCE} AS distance\n"
                   f"FROM {DB}.movies m WHERE m.id IN ({id_list})\n"
                   f"ORDER BY {order_by(rank)}\nLIMIT {int(k)}")
            rows, ms = timed(cur, sql)
            steps.append({"sql": sql + ";", "ms": ms, "stage": "fetch"})
            return rows, steps, path
        path = {"kind": "key_list",
                "why": f"{at_most}{bound:,} movies match: a narrow filter (at most {KEYLIST_MAX:,}), "
                       "so their keys go to the search, which returns the nearest among them"}
        keys = ",\n         " + f.keys_sql()

    # Two statements, so the page can show the MyVector search's own time: the HNSW
    # search returns the nearest keys as a JSON array, then SQL fetches and ranks them.
    sql = f"SET @nn = myvector_ann_set('{INDEX}', 'id', @q, 'nn={nn},ef_search={ef}'{keys})"
    _, ms = timed(cur, sql)
    note = f"HNSW search for the {nn} nearest keys"
    if keys:
        note += ", among the filter's keys (building the key list is included)"
    steps.append({"sql": sql + ";", "ms": ms, "stage": "myvector", "note": note})
    sql = (f"SELECT {DETAIL_COLS},\n       {DISTANCE} AS distance\n"
           "FROM JSON_TABLE(@nn, '$[*]' COLUMNS (rank_no FOR ORDINALITY, id INT PATH '$')) nn\n"
           f"JOIN {DB}.movies m ON m.id = nn.id\n{excl}"
           f"ORDER BY {'nn.rank_no' if rank == 'similar' else order_by(rank)}\nLIMIT {int(k)}")
    rows, ms = timed(cur, sql)
    steps.append({"sql": sql + ";", "ms": ms, "stage": "fetch"})
    return rows, steps, path


def exact_search(cur, k, f, rank, exclude_id=None):
    where = []
    if f:
        where.append(f.predicate())
    if exclude_id:
        where.append(f"m.id <> {int(exclude_id)}")
    # Find the k nearest ids first, then fetch their details: details in the scan itself
    # would be computed for every row before the sort.
    sql = (f"SELECT /*+ MAX_EXECUTION_TIME({EXACT_TIMEOUT_S * 1000}) */ {DETAIL_COLS}, top.distance\n"
           f"FROM (SELECT m.id, {DISTANCE} AS distance, m.vote_count\n"
           f"      FROM {DB}.movies m\n" + (f"      WHERE {' AND '.join(where)}\n" if where else "") +
           f"      ORDER BY {order_by(rank)}\n      LIMIT {int(k)}) top\n"
           f"JOIN {DB}.movies m ON m.id = top.id\n"
           f"ORDER BY {order_by(rank).replace('m.vote_count', 'top.vote_count').replace('distance', 'top.distance')}")
    try:
        rows, ms = timed(cur, sql)
    except pymysql.err.OperationalError as e:
        if e.args[0] == 3024:   # ER_QUERY_TIMEOUT
            raise HTTPException(504, f"The exact search scans every row and took over "
                                     f"{EXACT_TIMEOUT_S} s. The table is probably not cached yet "
                                     "(the first scan after a start reads it from disk); try again, "
                                     "or raise MYSQL_BUFFER_POOL.")
        raise
    return rows, [{"sql": sql + ";", "ms": ms, "stage": "scan",
                   "note": "myvector_distance() on every row, no index"}]


def clean(rows):
    for r in rows:
        if r.get("vote_average") is not None:
            r["vote_average"] = float(r["vote_average"])
        if r.get("distance") is not None:
            r["distance"] = round(float(r["distance"]), 4)
    return rows


def run_search(set_q, k, ef, mode, f, rank, exclude_id=None):
    if mode not in ("ann", "exact", "compare"):
        raise HTTPException(400, "mode must be ann, exact or compare")
    if rank not in ("popular", "similar"):
        raise HTTPException(400, "rank must be popular or similar")
    if mode == "compare":
        rank = "similar"   # recall compares the index with exact search on distance alone
    out = {"mode": mode, "rank": rank}
    with cursor() as cur:
        first = set_q(cur)
        if mode in ("ann", "compare"):
            rows, steps, path = ann_search(cur, k, ef, f, rank, exclude_id)
            out["ann"] = {"rows": clean(rows), "steps": [first] + steps, "path": path,
                          "ms": sum(s["ms"] for s in steps)}
        if mode in ("exact", "compare"):
            rows, steps = exact_search(cur, k, f, rank, exclude_id)
            out["exact"] = {"rows": clean(rows), "steps": [first] + steps,
                            "ms": sum(s["ms"] for s in steps)}
    if mode == "compare":
        a = {r["id"] for r in out["ann"]["rows"]}
        e = {r["id"] for r in out["exact"]["rows"]}
        out["recall"] = round(len(a & e) / len(e), 3) if e else None
    return out


# ── API ──────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


@app.get("/api/meta")
def meta():
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM movies")
        n = cur.fetchone()["n"]
        cur.execute("SELECT v FROM demo_state WHERE k = 'loaded'")
        row = cur.fetchone()
    return {"movies": n, "genres": _genres, "languages": _languages,
            "loaded": row and row["v"], "index": INDEX, "model": MODEL,
            "keylist_max": KEYLIST_MAX, "added_id_base": ADDED_ID_BASE}


def parse_options(text):
    """'MYVECTOR COLUMN type=HNSW,dim=768,...' -> {'type': 'HNSW', 'dim': '768', ...}"""
    body = re.sub(r"^\s*MYVECTOR\s+COLUMN\s*", "", text or "", flags=re.I)
    return {k.strip().lower(): v.strip() for k, _, v in
            (p.partition("=") for p in body.replace("\n", ",").split(",")) if k.strip()}


@app.get("/api/index")
def index_info():
    """What MyVector reports about the index, for the page's index panel."""
    out = {"index": INDEX}
    with cursor() as cur:
        rows, ms = timed(cur, "CALL mysql.myvector_index_status(%s)", (INDEX,))
        text = "\n".join(str(v) for r in rows for v in r.values()).replace("\\n", "\n")
        status = {}
        for line in text.splitlines():
            key, sep, val = line.partition(" : ") if " : " in line else line.partition(" = ")
            if sep:
                status[key.strip()] = val.strip()
        out["status"] = status
        out["status_sql"] = f"CALL mysql.myvector_index_status('{INDEX}');"
        out["status_ms"] = ms
        cur.execute("SELECT info FROM mysql.myvector_columns WHERE db = %s AND tbl = 'movies' "
                    "AND col = 'embedding'", (DB,))
        row = cur.fetchone()
        out["options"] = parse_options(row and row["info"])
        cur.execute("SELECT component_urn FROM mysql.component WHERE component_urn LIKE '%myvector%'")
        out["build"] = "component" if cur.fetchone() else "plugin"
        cur.execute("SELECT VERSION() AS v")
        out["mysql"] = cur.fetchone()["v"]
        # The binlog listener follows the binlog like a replica: a Binlog Dump thread.
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.processlist "
                    "WHERE command LIKE 'Binlog Dump%'")
        out["listener"] = cur.fetchone()["n"] > 0
        cur.execute("SELECT data_length + index_length AS b FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = 'movies'", (DB,))
        out["table_bytes"] = int(cur.fetchone()["b"] or 0)
    out["model"] = MODEL
    out["dim"] = DIM
    return out


SWEEP_EF = (10, 20, 40, 80, 160, 320, 640)


@app.get("/api/sweep")
def sweep(q: str = Query(..., min_length=2, max_length=500), k: int = Query(10, ge=1, le=50)):
    """Recall and latency of the HNSW search over ef_search, against an exact scan.

    Unfiltered and ranked by distance alone, so recall measures the index itself.
    """
    vec = embed(q)
    with cursor() as cur:
        set_query_vector(cur, vec)
        exact_sql = (f"SELECT /*+ MAX_EXECUTION_TIME({EXACT_TIMEOUT_S * 1000}) */ m.id, "
                     f"{DISTANCE} AS distance FROM {DB}.movies m ORDER BY distance LIMIT {int(k)}")
        try:
            rows, exact_ms = timed(cur, exact_sql)
        except pymysql.err.OperationalError as e:
            if e.args[0] == 3024:
                raise HTTPException(504, f"The exact scan took over {EXACT_TIMEOUT_S} s; "
                                         "the table is not cached yet. Try again.")
            raise
        truth = {r["id"] for r in rows}
        points = []
        for ef in SWEEP_EF:
            times, found = [], set()
            for _ in range(3):
                res, ms = timed(cur, f"SELECT myvector_ann_set('{INDEX}', 'id', @q, "
                                     f"'nn={int(k)},ef_search={ef}') AS js")
                times.append(ms)
                found = {int(x) for x in re.findall(r"-?\d+", res[0]["js"] or "")}
            points.append({"ef": ef, "ms": sorted(times)[1],
                           "recall": round(len(found & truth) / len(truth), 3) if truth else None})
    return {"k": k, "exact_ms": exact_ms, "points": points,
            "sql": f"SELECT myvector_ann_set('{INDEX}', 'id', @q, 'nn={k},ef_search=<ef>');",
            "exact_sql": exact_sql + ";"}


@app.get("/api/search")
def search(q: str = Query(..., min_length=2, max_length=500),
           k: int = Query(10, ge=1, le=50), ef: int = Query(100, ge=10, le=1000),
           mode: str = "ann", rank: str = "popular", genres: list[str] = Query(default=[]),
           year_min: int | None = None, year_max: int | None = None,
           min_rating: float | None = Query(None, ge=0, le=10), language: str | None = None):
    f = Filter(genres, year_min, year_max, min_rating, language)
    t = time.perf_counter()
    vec = embed(q)
    embed_ms = (time.perf_counter() - t) * 1000
    out = run_search(lambda cur: set_query_vector(cur, vec), k, ef, mode, f, rank)
    out["embed_ms"] = embed_ms
    return out


@app.get("/api/similar/{movie_id}")
def similar(movie_id: int, k: int = Query(10, ge=1, le=50), ef: int = Query(100, ge=10, le=1000),
            mode: str = "ann", rank: str = "popular", genres: list[str] = Query(default=[]),
            year_min: int | None = None, year_max: int | None = None,
            min_rating: float | None = Query(None, ge=0, le=10), language: str | None = None):
    f = Filter(genres, year_min, year_max, min_rating, language)

    def set_q(cur):
        cur.execute(f"SELECT embedding INTO @q FROM {DB}.movies WHERE id = %s", (movie_id,))
        cur.execute("SELECT @q IS NOT NULL AS ok")
        if not cur.fetchone()["ok"]:
            raise HTTPException(404, "no such movie")
        return f"SELECT embedding INTO @q FROM {DB}.movies WHERE id = {movie_id};"

    out = run_search(set_q, k, ef, mode, f, rank, exclude_id=movie_id)
    with cursor() as cur:
        cur.execute(f"SELECT {DETAIL_COLS} FROM {DB}.movies m WHERE m.id = %s", (movie_id,))
        out["source"] = clean(cur.fetchall())[0]
    out["embed_ms"] = 0
    return out


class NewMovie(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    year: int | None = Field(None, ge=1870, le=2100)
    tagline: str = Field("", max_length=300)
    overview: str = Field(min_length=20, max_length=2000)
    genres: list[str] = []


@app.post("/api/movies")
def add_movie(m: NewMovie):
    bad = [g for g in m.genres if g not in _genres]
    if bad:
        raise HTTPException(400, f"unknown genre: {bad[0]}")
    # The same text layout the dataset's vectors were made from: "title: tagline: overview".
    vec = embed(f"{m.title}: {m.tagline}: {m.overview}")
    with cursor() as cur:
        set_query_vector(cur, vec)
        cur.execute(f"SELECT GREATEST({ADDED_ID_BASE}, COALESCE(MAX(id), 0) + 1) AS id "
                    f"FROM {DB}.movies WHERE id >= {ADDED_ID_BASE}")
        new_id = cur.fetchone()["id"]
        sql = (f"INSERT INTO {DB}.movies (id, title, year, tagline, overview, vote_count, embedding)\n"
               f"VALUES ({new_id}, %s, %s, %s, %s, 0, @q)")
        cur.execute(sql, (m.title, m.year, m.tagline or None, m.overview))
        for g in m.genres:
            cur.execute(f"INSERT INTO {DB}.movie_genres VALUES (%s, %s)", (new_id, g))
    shown = (f"INSERT INTO {DB}.movies (id, title, year, tagline, overview, vote_count, embedding)\n"
             f"VALUES ({new_id}, {pymysql.converters.escape_item(m.title, 'utf8mb4')}, "
             f"{m.year if m.year else 'NULL'}, ..., @q);")
    return {"id": new_id, "steps": [f"SET @q = <{DIM}-dim vector of the new movie>;", {"sql": shown, "ms": 0}]}


@app.get("/api/indexed/{movie_id}")
def indexed(movie_id: int):
    """Is this movie in the HNSW index yet? Search with its own vector and look for it."""
    with cursor() as cur:
        cur.execute(f"SELECT embedding INTO @q FROM {DB}.movies WHERE id = %s", (movie_id,))
        cur.execute(f"SELECT JSON_CONTAINS(myvector_ann_set('{INDEX}', 'id', @q, 'nn=5'), "
                    "CAST(%s AS JSON)) AS found", (movie_id,))
        return {"indexed": bool(cur.fetchone()["found"])}
