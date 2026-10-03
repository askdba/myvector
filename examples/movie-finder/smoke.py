#!/usr/bin/env python3
"""Smoke test for a running Movie Finder (any profile; the 10k profile is enough).

    docker compose up -d        # with MOVIES=10k for a quick run
    python3 smoke.py            # or: python3 smoke.py --url http://host:8080

Exit 0 = every check passed. Standard library only.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HARRY_POTTER = 671     # Harry Potter and the Philosopher's Stone
THE_MATRIX = 603


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    args = ap.parse_args()
    results = []

    def get(path, **params):
        q = urllib.parse.urlencode(params, doseq=True)
        with urllib.request.urlopen(f"{args.url}{path}?{q}", timeout=120) as r:
            return json.load(r)

    def post(path, body):
        req = urllib.request.Request(f"{args.url}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)

    def check(name, ok, detail):
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    deadline = time.time() + 300
    while True:
        try:
            meta = get("/api/meta")
            break
        except (urllib.error.URLError, ConnectionError) as e:
            if time.time() > deadline:
                sys.exit(f"app not reachable at {args.url}: {e}")
            time.sleep(3)
    print(f"{meta['movies']:,} movies, loaded: {meta['loaded']}")

    out = get("/api/search", q="a boy wizard at a school of magic", k=10)
    ids = [r["id"] for r in out["ann"]["rows"]]
    titles = [r["title"] for r in out["ann"]["rows"]]
    check("1. description search finds Harry Potter", HARRY_POTTER in ids, titles[:5])

    out = get("/api/search", q="a haunted house", k=10, genres=["Horror"])
    rows = out["ann"]["rows"]
    check("2. genre filter returns k rows, all Horror",
          len(rows) == 10 and all("Horror" in (r["genres"] or "") for r in rows),
          f"{len(rows)} rows, path={out['ann']['path']['kind']}")

    out = get("/api/search", q="a romantic comedy in Paris", k=10, genres=["Drama"])
    check("3. a broad filter (Drama) returns k rows", len(out["ann"]["rows"]) == 10,
          f"{len(out['ann']['rows'])} rows, path={out['ann']['path']['kind']}")

    out = get(f"/api/similar/{THE_MATRIX}", k=10)
    ids = [r["id"] for r in out["ann"]["rows"]]
    check("4. more like The Matrix excludes itself", len(ids) == 10 and THE_MATRIX not in ids,
          [r["title"] for r in out["ann"]["rows"]][:5])

    out = get("/api/search", q="astronauts stranded on a desert planet", k=10, mode="compare")
    check("5. HNSW vs exact recall@10 >= 0.9", (out["recall"] or 0) >= 0.9,
          f"recall={out['recall']}, hnsw {out['ann']['ms']:.1f} ms, exact {out['exact']['ms']:.1f} ms")

    title = f"Smoke Test Film {int(time.time())}"
    new = post("/api/movies", {"title": title, "year": 2026,
                               "overview": "A lighthouse keeper's cat solves a smuggling mystery "
                                           "on a remote island during a winter storm."})
    t, found = time.time(), False
    while time.time() - t < 5 and not found:
        found = get(f"/api/indexed/{new['id']}")["indexed"]
        if not found:
            time.sleep(0.2)
    check("6. an added movie is searchable within 5 s (online=Y)", found,
          f"id {new['id']} after {time.time() - t:.1f}s")

    ok = bool(results) and all(results)
    print("PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
