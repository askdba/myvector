# RFC-006: Returning ANN Results as a Native Row Set

**Author:** MyVector Engineering
**Status:** Draft
**Target Version:** Component builds (MySQL 8.4, 9.7, 26.10); plugin deferred (§8)
**Last Updated:** 2026-11-08

---

## 1. Abstract

A MyVector ANN query — `... WHERE MYVECTOR_IS_ANN('t.vec','id', myvector_construct('[…]'), 'nn=10,ef_search=N')` — is rewritten before parse into a form that returns the matching ids through a **JSON round-trip**: the scalar UDF `myvector_ann_set()` runs the HNSW search and serialises the result ids into a JSON array, which `JSON_TABLE` then parses back into rows for a semi-join. Profiling shows this round-trip (JSON build + `JSON_TABLE` parse + the semi-join scaffolding) costs **~0.28 ms/query**, and it dominates the *remaining* fixed per-query overhead at low `ef_search` now that [PR #280](https://github.com/askdba/myvector/pull/280) removed the ~0.5 ms argument-parsing cost.

This RFC proposes replacing the JSON round-trip with a path that yields the ANN result ids **as native rows**, eliminating the serialise→parse hop. It evaluates three approaches, recommends a **feasibility spike on the MySQL component Table Function service** as the bounded option, and records a **secondary-engine / index access method** as the long-term fully-native milestone. No change to the on-disk index format, recall, or query semantics.

### 1.1 Scope

- **In scope:** the ANN *query path* — the `MYVECTOR_IS_ANN` pre-parse rewrite (`rewriteMyVectorIsANN`, `src/myvector.cc`) and the `myvector_ann_set()` → `JSON_TABLE` result delivery. Component build, MySQL 8.4 / 9.7 / 26.10.
- **Out of scope (already done):** the distance kernels (vectorised, [PR #278](https://github.com/askdba/myvector/pull/278)); the rewrite's argument string-parsing (PR #280); the HNSW search algorithm itself.
- **Out of scope (inherent / fair):** MySQL's own lexing of the multi-KB text vector literal. This is paid equally by every engine benchmarked with text-literal queries (pgvector included) and is addressable only by changing the *client* to bound/binary parameters, not the engine.
- **Deferred:** the plugin build (§8).

---

## 2. Motivation

Server-side profiling of a MyVector ANN query (8.4 component, fashion-mnist 784-d) decomposes the per-query cost as:

| Component | ~cost at ef=10 | Status |
|---|---|---|
| Argument string-parse over the literal (`split()` regex) | ~0.50 ms | **Removed (PR #280)** |
| HNSW distance kernel (scalar → SIMD) | search-proportional | **Vectorised (PR #278)** |
| JSON build in `myvector_ann_set` + `JSON_TABLE` parse + semi-join | **~0.28 ms** | **This RFC** |
| MySQL lexing the text literal | remainder | Inherent / fair (out of scope) |
| HNSW search | grows with `ef_search` | algorithm |

After #278 and #280, the JSON round-trip is the **largest remaining engine-controlled fixed cost**, and it is the part that most visibly separates MyVector from pgvector at the low-`ef` (recall ≈ 0.98) operating points that dominate real workloads. A same-host A/B measured #280 alone at **+48–80 % QPS**; removing the JSON round-trip is the next lever on that same curve.

---

## 3. Background — the current ANN result path

`rewriteMyVectorIsANN` turns

```sql
SELECT id FROM t
 WHERE MYVECTOR_IS_ANN('t.vec','id', myvector_construct('[…]'), 'nn=10,ef_search=50')
 ORDER BY myvector_row_distance(id) LIMIT 10;
```

into

```sql
SELECT id FROM t
 WHERE id IN (
   SELECT `myvecid` FROM
     (SELECT myvector_ann_set('t.vec','id', myvector_construct('[…]'), 'nn=10,ef_search=50') `_mvjson`) `_mvsrc`,
     JSON_TABLE(`_mvsrc`.`_mvjson`, "$[*]" COLUMNS(`myvecid` BIGINT PATH "$")) `myvector_ann`)
 ORDER BY myvector_row_distance(id) LIMIT 10;
```

`myvector_ann_set()` is a **scalar UDF**: it looks up the index, runs the HNSW search, and returns a **JSON array string** of the result ids. `JSON_TABLE` parses that string back into a one-column derived table that the outer query semi-joins against.

**Why it is shaped this way:** the MySQL UDF API returns a single scalar per row. A UDF cannot return a *set*, so the only in-engine way to surface the N result ids to SQL is to pack them into one scalar (here, JSON) and expand it SQL-side. `JSON_TABLE` is the cleanest available expander — but the serialise→parse hop is pure overhead: the ids exist as integers inside the UDF, become text, and are parsed back to integers.

---

## 4. Proposed design

Three approaches, in increasing order of effort and payoff.

### 4.1 Option A — Native table function (component Table Function service) — *recommended spike*

MySQL's **component infrastructure** exposes service registries for extending the server. If a **table-function registration service** is available to components on the target versions, MyVector could register a table function, e.g.

```sql
SELECT id FROM t
 JOIN MYVECTOR_ANN('t.vec','id', myvector_construct('[…]'), 'nn=10,ef_search=50') AS a
   ON t.id = a.myvecid
 ORDER BY myvector_row_distance(id) LIMIT 10;
```

that yields rows **directly** — no JSON, no `JSON_TABLE`. The rewrite becomes a straight substitution to the table-function call (the top-level-comma parse from #280 is reused verbatim to extract the args).

- **Payoff:** removes the full ~0.28 ms round-trip; the ids never leave integer form.
- **Risk / unknown:** it is **not yet confirmed** that the component API supports registering an SQL-visible table function on 8.4 / 9.7 / 26.10 (as opposed to the legacy UDF path). **This is the first thing the spike must establish** (§8 Phase 0). If the service is absent or differs across versions, fall back to Option C or escalate to Option B.

### 4.2 Option B — Secondary engine / index access method — *long-term native milestone*

Implement MyVector as a **secondary storage engine** (or an index access method that the optimiser drives), so an ANN predicate becomes a real index scan returning rows natively, with the optimiser costing and ordering it. This is how a first-class vector index would integrate.

- **Payoff:** fully native — no rewrite, no JSON, optimiser-aware ordering/limit pushdown, and the cleanest `ORDER BY distance LIMIT k`.
- **Cost:** very large, version-sensitive, and interacts with [RFC-005](RFC-005-INNODB-INDEX-STORAGE.md) (storage). A multi-release effort, not an incremental PR. Recorded here as the direction, not proposed for immediate work.

### 4.3 Option C — Cheaper serialisation (fallback) — *marginal*

Keep the UDF-returns-a-scalar shape but replace JSON with a packed binary blob that a lighter SQL expression expands.

- Considered and largely **rejected**: MySQL has no generic cheap set-expander other than `JSON_TABLE`; `FIND_IN_SET` over a CSV is **O(rows-scanned)** and worse at table scale; a packed-binary + custom expander UDF reintroduces a per-row UDF call. The ids are tiny (k≈10), so the JSON *build* is already cheap — the cost is `JSON_TABLE`'s machinery, which Option C does not remove. Only worth revisiting if Option A proves infeasible and Option B is out of reach.

### 4.4 Recommendation

1. **Phase 0 spike** on Option A (component table-function feasibility across 8.4/9.7/26.10). Bounded, days not weeks.
2. If viable → implement Option A behind the existing rewrite (transparent to users; same `MYVECTOR_IS_ANN` surface).
3. Record Option B as the eventual native milestone; do not start it under this RFC.

---

## 5. Correctness & compatibility

- **Semantics preserved exactly:** `nn=K`, bare-integer-`k`, the `ef_search` option, and the optional 5th filter argument must behave identically. The arg extraction added in PR #280 (top-level-comma scan) is reused unchanged.
- **Recall is the gate:** recall@10 vs the datasets' **published** ground truth must be unchanged (within noise) on fashion-mnist-784, nytimes-256, and glove-100. A regression here blocks the change.
- **Index format unchanged:** this touches only result *delivery*, not the graph or its on-disk form; no migration, no interaction with a stored index version.
- **Test harness:** the existing live-server integration scripts (`scripts/test-filtered-ann.py`, `test-filtered-ann-broad.py`, `test-ef-search.py`, `test-ef-search-clamp.py`) already exercise the `MYVECTOR_IS_ANN` surface across builds and must pass unchanged. Add cases asserting the new path returns byte-identical result **sets** to the JSON path.
- **Multi-version:** must build and pass on 8.4, 9.7, and 26.10 (component CI matrix). If the table-function service differs by version, the rewrite selects the available delivery path per version behind one code path.

---

## 6. Expected impact

Removing the ~0.28 ms round-trip is estimated to add a further **~+20–40 % QPS at low `ef`** on top of PR #280 (the round-trip is ~0.28 ms of the ~0.85 ms post-#280 p50 at ef=10). This is an **estimate**; the same-host A/B method used for #280 (two component builds on one host, disjoint cores, recall-gated) is the acceptance measurement. Combined with #278 + #280, it would close most of the remaining low-`ef` per-query gap to pgvector that the benchmark dashboards document.

---

## 7. Risks & non-goals

- **Risk:** the component table-function service may not exist / may differ across 8.4/9.7/26.10 — Phase 0 must confirm before any implementation commitment.
- **Risk:** correctness surface is the query path (parsing/semijoin); mitigated by the recall gate + existing integration tests + byte-identical-set assertions.
- **Non-goal:** changing the user-facing `MYVECTOR_IS_ANN` syntax — the win must be transparent.
- **Non-goal:** the text-literal lexing cost (inherent, fair across engines) and the HNSW search cost (algorithmic; separate work).
- **Non-goal:** Option B (secondary engine) implementation — directional only.

---

## 8. Phasing

- **Phase 0 — Feasibility spike (days):** determine whether a component can register an SQL table function on 8.4 / 9.7 / 26.10. Deliverable: a yes/no with a minimal proof-of-concept table function, or a documented negative that routes to Option C/B.
- **Phase 1 — Implement (Option A):** table function + rewrite substitution; reuse #280 arg parsing; per-version delivery selection.
- **Phase 2 — Validate:** integration tests green on all versions; recall unchanged; same-host A/B QPS measured and published to `/x86/`-style notes.
- **Phase 3 — Plugin decision:** whether to port to the plugin build or leave it component-only (mirrors RFC-005 §8 phrasing).

---

## 9. Open questions

1. Does the component API on each target version expose table-function registration, and is the ABI stable across 8.4/9.7/26.10?
2. Can the table function advertise ordering (ANN order) so the optimiser avoids a redundant re-sort for `ORDER BY myvector_row_distance(id)`?
3. Should the 5th-argument filter (pre-filtered ANN) be pushed into the table function, or stay a semi-join?
4. Interaction with RFC-005 (InnoDB index storage) — any shared plumbing worth sequencing together?
