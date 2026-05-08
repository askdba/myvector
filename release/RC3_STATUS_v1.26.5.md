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
- **RC3 validation baseline commit:** TBD (post-tag)
- Scope range: `v1.26.5-rc2..HEAD` on `main`.

## 3) RC2 → RC3 delta

| Commit | Description |
| :----- | :---------- |
| TBD | fix(component): fix binlog WRITE_ROWS handler for multi-column tables |

**Root causes fixed:**

1. **FDE reconnect crash loop** (`myvector_binlog_service.cc`): MySQL 8.4/9.7 sends FORMAT_DESCRIPTION_EVENT (type=15) at reconnect with a non-zero `next_log_pos` pointing past EOF. Applying it as the current position caused `mysql_binlog_fetch` to time out immediately on every reconnect. Fixed by skipping the position update for FDE events.

2. **`KNNIndex` missing binlog coordinate tracking** (`myvector.cc`): `KNNIndex` inherited no-op `setLastUpdateCoordinates`/`getLastUpdateCoordinates` from the abstract base, so `isAfter()` always returned true for any real binlog file name. Added `m_binlogFile`/`m_binlogPosition` storage with sentinel initialization in `initIndex()`.

3. **BUILD saves stale listener position** (`myvector_binlog_service.cc`): `BuildMyVectorIndexSQL` was saving `currentBinlogPos` (the listener's reconnect position, potentially before the rows being built) as the index's "last update" coordinate. Replaced with `SHOW BINARY LOG STATUS` on the open BUILD connection so the saved position is guaranteed to be ≥ every row read during the build.

## 4) Validation status

### Build and CI

- CI status: TBD
- Release workflow (`release.yml`): TBD
- Docker publish (`docker-publish.yml`): TBD
- Lint status: TBD

### Functional checks

- Plugin smoke (`smoke-published-images.sh`): TBD
- Component smoke (`smoke-component.sh` 8.4): **PASS** (local, pre-tag)
- Component smoke (`smoke-component.sh` 9.7): **PASS** (local, pre-tag)
- Online index flow: PASS (ov_test + mc_test both passing)
- Regression: CI coverage pre-tag TBD

### Performance smoke

- Startup sanity: pending.
- Query latency: pending (README smoke).
- Memory: not measured for RC3.

## 5) GHCR images smoke-tested

Fill in after `docker-publish.yml` completes for `v1.26.5-rc3`.

| Tag | Image digest (pulled) |
| :-- | :-- |
| `mysql8.0` | TBD |
| `mysql8.4` | TBD |
| `mysql9.7` | TBD |

## 6) Component smoke results (published images)

Fill in after `./scripts/smoke-published-images.sh` runs against RC3 images.

| Tag | Result |
| :-- | :-- |
| `mysql8.4` | TBD |
| `mysql9.7` | TBD |

## 7) Go/No-Go

- Decision: Pending CI and published-image smoke results.
- Blockers: None identified at RC3 cut time.
