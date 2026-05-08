# RC3 Status - v1.26.5

## 1) Release metadata

- Release version: `v1.26.5`
- Candidate tag: `v1.26.5-rc3`
- Previous candidate: `v1.26.5-rc2`
- Release manager: `askdba`
- Date opened: `2026-05-08`
- Last updated: `2026-05-08`

## 2) Candidate commit and branch

- Working branch: `main`
- **RC3 validation baseline commit:** `3e790870bfc1badae900261029bd0cd1616408cf`
- Scope range: `v1.26.5-rc2..HEAD` on `main`.

## 3) RC2 → RC3 delta

| Commit | Description |
| :----- | :---------- |
| `3e79087` | fix(component): fix binlog WRITE_ROWS handler for multi-column tables |

**Root causes fixed:**

1. **FDE reconnect crash loop** (`myvector_binlog_service.cc`): MySQL 8.4/9.7 sends FORMAT_DESCRIPTION_EVENT (type=15) at reconnect with a non-zero `next_log_pos` pointing past EOF. Applying it as the current position caused `mysql_binlog_fetch` to time out immediately on every reconnect. Fixed by skipping the position update for FDE events.

2. **`KNNIndex` missing binlog coordinate tracking** (`myvector.cc`): `KNNIndex` inherited no-op `setLastUpdateCoordinates`/`getLastUpdateCoordinates` from the abstract base, so `isAfter()` always returned true for any real binlog file name. Added `m_binlogFile`/`m_binlogPosition` storage with sentinel initialization in `initIndex()`.

3. **BUILD saves stale listener position** (`myvector_binlog_service.cc`): `BuildMyVectorIndexSQL` was saving `currentBinlogPos` (the listener's reconnect position, potentially before the rows being built) as the index's "last update" coordinate. Replaced with `SHOW BINARY LOG STATUS` on the open BUILD connection so the saved position is guaranteed to be ≥ every row read during the build.

## 4) Validation status

### Build and CI

- CI status: **Green** (MyVector CI run id=25571134015).
- Release workflow (`release.yml`): **Success** (run id=25571135302, tag v1.26.5-rc3).
- Docker publish (`docker-publish.yml`): **Success** (run id=25571408041, all 3 matrix jobs).
- Lint status: Green on main.

### Functional checks

- Plugin smoke (`smoke-published-images.sh`): **PASS** — all 3 tags (mysql8.0 / mysql8.4 / mysql9.7).
- Component smoke (`smoke-component.sh` 8.4): **PASS** (local, pre-tag — all 15 checks).
- Component smoke (`smoke-component.sh` 9.7): **PASS** (local, pre-tag — all 15 checks).
- Online index flow: PASS (ov_test + mc_test both passing).
- Regression: CI coverage pre-tag is green.

### Performance smoke

- Startup sanity: pending.
- Query latency: pending (README smoke).
- Memory: not measured for RC3.

## 5) GHCR images smoke-tested

Images published 2026-05-08 via run id=25571408041.

| Tag | Image digest (pulled) |
| :-- | :-- |
| `mysql8.0` | `sha256:0c94a258e0dc20fbf9c65e4fdc1932bfe578620dc21527ecfe29fda0c7391301` |
| `mysql8.4` | `sha256:0593ebf09600e5bea6f4f13b18f2c4d7be232e9de45f09b225caf07b30ab47dd` |
| `mysql9.7` | `sha256:563c0c3a82d6f8f2e2df3884aa1dad7e47ef9e42995b44fff588b1a9603ceeb2` |

## 6) Component smoke results (published images)

Smoke run completed 2026-05-08 against published RC3 images.

| Tag | Result |
| :-- | :-- |
| `mysql8.0` | **PASS** |
| `mysql8.4` | **PASS** |
| `mysql9.7` | **PASS** |

## 7) Go/No-Go

- Decision: **Go** — all CI green, all 3 published images pass smoke, component smoke passes on 8.4 and 9.7.
- Blockers: None.
