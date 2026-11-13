# /versions/ — superseded, redirects to /real/

The old `/versions/` page was a **synthetic 50k×128 MySQL-version comparison**
(generated 2026-11-02, pre-optimization). It was orphaned (no source `result-*.json`
feeding it) and redundant with `/real/`, which now covers the MyVector
8.4/9.7/26.10 version comparison on a **real** dataset with current `main`
(#278 + #280) — and shows the versions are consistent.

`index.html` here is a **static redirect** to `/real/`. Deploy:
`sudo cp index.html /data/movie-finder-public/bench-site/versions/index.html`.
The `/versions/` link was removed from every page's nav.
