# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

MyVector is a native MySQL plugin (shared library `.so`) that adds vector similarity search (ANN/KNN) to MySQL 8.0, 8.4, and 9.0. It is written in C++ and exposes UDFs and stored procedures. No external vector database is required.

## Build commands

**Quick build** (requires `mysql_config` in PATH — installed MySQL dev headers):
```bash
make          # produces myvector.so
make clean
```

**Full build against MySQL source tree** (used in CI):
```bash
cmake .. && make myvector -j$(nproc)
```

**Lint / static analysis:**
```bash
cppcheck --enable=warning,style,performance -I include src/
```

**CI lint (actionlint, mirrors CI exactly):**
```bash
# Install actionlint via official script, then:
./actionlint -color .github/workflows/*.yml
```

**Install plugin into a running MySQL instance:**
```bash
mysql -u root -p -e "INSTALL PLUGIN myvector SONAME 'myvector.so';"
mysql -u root -p < sql/install_functions.sql
```

## Testing

Integration tests run against a live MySQL server. There is no unit test runner — correctness is validated through SQL smoke tests.

**Smoke test (Docker images from GHCR — do this after any release tag):**
```bash
./scripts/smoke-published-images.sh
# Heavier variant with Stanford dataset:
MYVECTOR_SMOKE_STANFORD=1 ./scripts/smoke-published-images.sh
```

**Per-image manual smoke:**
```bash
bash scripts/smoke-readme.sh ghcr.io/askdba/myvector:mysql8.4
```

**Online index updates test:**
```bash
./scripts/test-online-updates.sh ghcr.io/askdba/myvector:mysql8.4
```

Docker images are only pushed to GHCR on `v*` git tags or published releases. PR builds build but do not push.

## Architecture

```
MySQL Server
├── UDFs (myvector.cc)
│   myvector_construct / myvector_display / myvector_distance / myvector_is_valid
├── Stored Procedures (myvector.cc)
│   myvector_index_build / myvector_index_status / myvector_index_drop / myvector_index_load
├── Plugin lifecycle (myvector_plugin.cc)
└── Binlog listener (myvector_binlog.cc) — keeps indexes in sync on INSERT/UPDATE/DELETE
```

**Key source files:**

| File | Role |
|---|---|
| `src/myvector.cc` | UDFs, index management, search logic (~2500 lines) |
| `src/myvector_binlog.cc` | Binlog event listener for real-time index updates |
| `src/myvector_plugin.cc` | Plugin init/deinit, system variable registration |
| `include/hnswdisk.h` | HNSW algorithm with disk persistence |
| `include/hnswalg.h` | In-memory HNSW implementation |
| `include/bruteforce.h` | Exact brute-force KNN fallback |

**Index abstraction:** `AbstractVectorIndex` is the interface; `VectorIndexCollection` manages all open indexes with thread-safe access. Row keys are `KeyTypeInteger` (INT/BIGINT only). Vector dimensions use `FP32` (32-bit float). Supported distance metrics: L2 (Euclidean), Cosine, Inner Product.

**Multi-arch:** CI produces `linux/amd64` and `linux/arm64` artifacts. macOS (Apple Silicon) is supported — see `docs/BUILDING_MACOS.md`.

## CI / Release workflow

Workflows under `.github/workflows/`:
- **ci.yml** — builds against MySQL 8.0, 8.4, 9.0 and runs integration tests
- **docker-publish.yml** — builds and pushes multi-arch Docker images on `v*` tags
- **linter.yml** — actionlint only (no super-linter); see lessons below
- **release.yml** — release automation

After a release tag, follow `release/POST_RC_DOCKER_SMOKE_PLAN.md` and record results in the corresponding `release/RC*_STATUS_*.md` file.

## Agent workflow (tasks/)

- Use `tasks/todo.md` for checkable plans; `tasks/lessons.md` for patterns learned from corrections.
- Review `tasks/lessons.md` at session start — it contains hard-won CI and tooling lessons.
- Enter plan mode for any work with 3+ steps or architectural decisions.
- Never mark a task complete without proof (test output, logs, CI green).
- On any user correction, append the pattern to `tasks/lessons.md`.

## Known pitfalls (from lessons.md)

- **actionlint**: Use the official download script to install; `actionlint -shellcheck` (without a path) is invalid in v1.7+. Optionally run **zizmor** with `.github/linters/zizmor.yaml`. Always verify locally before pushing.
- **Thread safety**: Non-Windows paths must use `gmtime_r`/`asctime_r` (not `gmtime`) for thread-safe time formatting.
