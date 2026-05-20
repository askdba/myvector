# Pre-Release Test Suite Design

**Date:** 2026-05-20
**Target version:** v1.26.5.1 and beyond
**Script:** `scripts/pre-release-test.sh`

---

## Goal

A single local command that gates every release tag. Runs the full happy-path smoke suite plus targeted RFC-004 feature tests and error/edge-case coverage against both supported MySQL versions (8.4 LTS and 9.7 LTS).

Exit zero = safe to tag. Non-zero = do not tag.

---

## Scope

- Component build path only. Plugin path is being retired.
- Local execution before `git tag`. Not a CI job.
- Does not build components itself — expects pre-built artifacts.

---

## Phases

| Phase | What | Abort on failure? |
|-------|------|-------------------|
| 1a | `smoke-component.sh 8.4 50000` (full happy path, 8.4) | Yes |
| 1b | `smoke-component.sh 9.7 50000` (full happy path, 9.7) | Yes |
| 2a | RFC-004 + edge cases, `mysql:8.4` container | No (collect, continue) |
| 2b | RFC-004 + edge cases, `mysql:9.7` container | No (collect, continue) |

Phases 1a/1b abort the entire run on failure — no point running Phase 2 against a broken component. Phases 2a/2b collect all failures and report at the end so the full picture is visible in one run.

---

## Artifact layout

Pre-built artifacts must be present before running:

```
build/component-8.4/
  libmyvector_component.so   # Linux arm64, built via build-component-8.4-docker.sh
  myvector.json

build/component-9.7/
  libmyvector_component.so   # Linux arm64, built via build-component-9.7-docker.sh
  myvector.json
```

If either directory is missing or the `.so` is absent, the script prints the exact build command and exits 1.

Build commands:
```bash
./scripts/build-component-8.4-docker.sh mysql-8.4.8   # outputs to build/component-8.4/
./scripts/build-component-9.7-docker.sh mysql-9.7.0   # outputs to build/component-9.7/
```

The existing build scripts currently write to `build/component/`. They will be updated to accept an optional output-dir argument so each version lands in its own subdirectory.

---

## Usage

```bash
./scripts/pre-release-test.sh          # runs all four phases
./scripts/pre-release-test.sh 8.4      # phases 1a + 2a only
./scripts/pre-release-test.sh 9.7      # phases 1b + 2b only
```

---

## Phase 2 test cases

The same test suite runs for 2a (8.4) and 2b (9.7). Each test calls `pass()`, `fail()`, or `skip()`.

### RFC-004 — Zero-vector rejection

| Test | Expected |
|------|----------|
| INSERT `[0.0,0.0,0.0]` into cosine-metric HNSW index via UDF | MySQL error (`*error=1`); INSERT rejected |
| INSERT same zero vector into L2-metric index | Success (L2 does not reject zero vectors) |
| INSERT valid non-zero vector into cosine index | Success (regression check) |

### RFC-004 — `myvector_max_vector_dim` sysvar

| Test | Expected |
|------|----------|
| `SELECT @@myvector_max_vector_dim` | `4096` |
| `SET GLOBAL myvector_max_vector_dim = 8192` | Error (`Variable is read only`) |
| Restart container with `--myvector-max-vector-dim=8192`, build 6000-dim index | Success (above old hardcoded 4096 limit) |

The high-dimension test requires a container restart with a custom MySQL option passed as a Docker `CMD` argument. A small synthetic table (100 rows of random 6000-dim vectors) is used — no external dataset needed.

### RFC-004 — Crash injection

| Test | Expected |
|------|----------|
| `SET SESSION debug='+d,simulate_vector_crash'` during index flush | Server abort (only in debug builds) |

Detection: attempt `SET SESSION debug=''`; if it returns an error, the build is release-mode. Print `SKIP: crash injection (release build)` and continue. SKIP does not affect exit code.

### Error / edge cases

| Test | Expected |
|------|----------|
| `myvector_construct(NULL)` | Returns NULL |
| `myvector_construct('{"a":1}')` — non-array JSON | NULL or error (must not return a valid binary vector) |
| `myvector_construct('[]')` — empty array | NULL or error (must not return a valid binary vector) |
| Insert 5d vector into 3d index | Error (dimension mismatch) |
| `myvector_distance(2d_vec, 3d_vec)` — mismatched dims | NULL or error (must not return a numeric distance) |
| `myvector_is_valid(vec3d, 5)` — wrong dim arg | Returns `0` |

---

## Output format

```
=== MyVector Pre-Release Test Suite ===
MySQL versions : 8.4, 9.7
Component dirs : build/component-8.4, build/component-9.7

--- Phase 1a: Smoke (8.4) ---
... smoke-component.sh output ...
--- Phase 1b: Smoke (9.7) ---
... smoke-component.sh output ...
--- Phase 2a: RFC-004 + Edge Cases (8.4) ---
PASS: zero-vector rejected on cosine index
PASS: zero-vector accepted on L2 index
PASS: valid vector accepted on cosine index
PASS: myvector_max_vector_dim default is 4096
PASS: myvector_max_vector_dim is read-only
PASS: 6000-dim index builds with max_vector_dim=8192
SKIP: crash injection (release build)
PASS: myvector_construct(NULL) returns NULL
...
--- Phase 2b: RFC-004 + Edge Cases (9.7) ---
...
=== Results: 34 passed, 0 failed, 1 skipped ===
```

Exit code: `0` if failed count is zero; `1` otherwise. Skips do not affect exit code.

---

## Build script changes

`build-component-8.4-docker.sh` and `build-component-9.7-docker.sh` currently write to `build/component/`. They will be updated to accept an optional second argument:

```bash
./scripts/build-component-8.4-docker.sh mysql-8.4.8 build/component-8.4
./scripts/build-component-9.7-docker.sh mysql-9.7.0 build/component-9.7
```

When the argument is omitted, the scripts default to `build/component/` (backward-compatible).

---

## Files changed

| File | Change |
|------|--------|
| `scripts/pre-release-test.sh` | New |
| `scripts/build-component-8.4-docker.sh` | Add optional output-dir arg |
| `scripts/build-component-9.7-docker.sh` | Add optional output-dir arg |
