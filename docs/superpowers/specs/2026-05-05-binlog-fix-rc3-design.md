# Binlog Fix + RC3 Release Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Diagnose and fix the multi-column binlog INSERT failure in `smoke-component.sh`, then cut and validate v1.26.5-rc3.

**Architecture:** Add diagnostic trace points across the binlog event path, build via Docker, run the smoke test to identify the breaking step, apply a targeted fix, remove traces, then execute the RC3 release sequence.

**Tech Stack:** C++ (component binary), Docker (OracleLinux 9 build), Bash (smoke scripts), git tags, GitHub Actions CI.

---

## Scope

Only the binlog fix. No other changes (deferred PR #88 TODOs are out of scope).

---

## Known context

- `smoke-component.sh` multi-column test: `FAIL: vec1 not updated by binlog INSERT (rows=3, expected 4)`
- `mc_test` table: `id INT PK, tag VARCHAR(64), vec1 VARBINARY(256) online=Y, vec2 VARBINARY(256) online=Y`
- After `BUILD(vec1)` and `BUILD(vec2)`, `g_OnlineVectorIndexes["vectordb.mc_test"]` should contain entries for both columns.
- The binlog listener (`myvector_binlog_service.cc`) runs inside the component; the worker thread calls `myvector_table_op` (in `myvector.cc`).
- Column ordinal positions (1-indexed from INFORMATION_SCHEMA): id=1, tag=2, vec1=3, vec2=4 → passed to `parseRowsEvent` as 0-indexed pos1/pos2.
- Build macOS workaround: use `./scripts/build-component-8.4-docker.sh mysql-8.4.8` (avoids `explicit_bzero` missing on macOS).

---

## Phase 1 — Diagnostic tracing

Add six `fprintf(stderr, "[DBG] ...")` trace points to expose the full event path:

| ID | File | Location | Message |
|----|------|----------|---------|
| T1 | `myvector_binlog_service.cc` | After `parseTableMapEvent(...)` call (~line 1768) | `[DBG] TABLE_MAP: %s.%s tev.nColumns=%u` |
| T2 | `myvector_binlog_service.cc` | WRITE_ROWS handler — after `g_OnlineVectorIndexes.find()` | `[DBG] WRITE_ROWS: key=%s found=%d` |
| T3 | `myvector_binlog_service.cc` | After each `parseRowsEvent(...)` call | `[DBG] parseRows col=%s updates=%zu` |
| T4 | `myvector_binlog_service.cc` | Worker thread, before `myvector_table_op` call | `[DBG] worker: %s.%s.%s pkid=%u` |
| T5 | `myvector.cc` | `myvector_table_op` — after `g_indexes.get(vecid)` (line 2411) | `[DBG] table_op vecid=%s vi=%p` |
| T6 | `myvector.cc` | `myvector_table_op` — before `insertVector` (line 2419) | `[DBG] isAfter(%s,%zu > %s,%zu) = %d` |

Build (8.4): `./scripts/build-component-8.4-docker.sh mysql-8.4.8`
Build (9.7): `./scripts/build-component-9.7-docker.sh mysql-9.7.0`

Run: `bash scripts/smoke-component.sh 8.4 2>&1 | tee /tmp/smoke-diag.txt`

Analyze: the last `[DBG]` line before the failure pinpoints the broken step.

---

## Phase 2 — Fix

Apply a targeted fix to the identified root cause. Two primary suspects based on code review:

**Suspect A** — `tev.columnMetadata` miscalculation for `tag VARCHAR(64)` causing `parseRowsEvent` to misread byte offsets and miss the vector data.  
Fix: correct metadata handling or offset calculation in `parseRowsEvent` / `parseTableMapEvent`.

**Suspect B** — `g_indexes.get(vecid)` returns null in `myvector_table_op` because the index was not retained in `g_indexes` after BUILD.  
Fix: ensure the index remains registered.

After applying the fix, remove all `[DBG]` trace lines. Rebuild and re-run `smoke-component.sh 8.4` and `smoke-component.sh 9.7`. Both must show `PASS` for the mc_test section.

---

## Phase 3 — RC3 release

Once both smoke variants pass:

1. `git add` only the fix files (`src/component_src/myvector_binlog_service.cc`, `src/myvector.cc` if touched).
2. Commit: `fix(component): fix binlog WRITE_ROWS handler for multi-column tables`
3. Update `release/RC2_STATUS_v1.26.5.md`:
   - Section 6: mark `mysql8.4` and `mysql9.7` as **FAIL** (binlog INSERT not reflected; fixed in RC3)
   - Section 7: Decision = **No-Go** — binlog fix required; proceeding to RC3
4. Create `release/RC3_STATUS_v1.26.5.md` with the standard skeleton (metadata, candidate tag `v1.26.5-rc3`, RC2→RC3 delta, validation status sections).
5. Commit the release docs.
6. Tag `v1.26.5-rc3` → triggers `release.yml` and `docker-publish.yml` CI.
7. After all 3 `docker-publish` matrix jobs succeed, pull images and run `./scripts/smoke-published-images.sh`.
8. Record GHCR digests and smoke results in `RC3_STATUS_v1.26.5.md`.
9. Run `smoke-component.sh` against the published RC3 images for final confirmation.
10. Fill in Section 7 (Go/No-Go). If all pass → **Go** → tag `v1.26.5`.

---

## Success criteria

- `smoke-component.sh 8.4` and `smoke-component.sh 9.7` both pass the mc_test section (vec1=4, vec2=4 after INSERT).
- `smoke-published-images.sh` passes for all 3 RC3 tags.
- `RC3_STATUS_v1.26.5.md` Section 7 is Go.
