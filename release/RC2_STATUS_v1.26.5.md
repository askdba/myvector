# RC2 Status - v1.26.5

## 1) Release metadata

- Release version: `v1.26.5`
- Candidate tag: `v1.26.5-rc2`
- Previous candidate: `v1.26.5-rc1`
- Release manager: `askdba`
- Date opened: `2026-05-04`
- Last updated: `2026-05-04`

## 2) Candidate commit and branch

- Working branch: `main`
- **RC2 validation baseline commit:** `99c3f24a82b4309b223968b712a6be62fe8fd26a`
- Scope range: `v1.26.5-rc1..HEAD` on `main`.

## 3) RC1 → RC2 delta

| Commit | Description |
| :----- | :---------- |
| `7c46cc7` | fix: static-link libstdc++/libgcc in plugin build for OracleLinux 8 compat |
| `99c3f24` | test: expand smoke-component.sh with broader coverage |

**Root cause of RC1 docker-publish failure:** Plugin `.so` compiled with GCC 10
on Ubuntu 22.04 required `GLIBCXX_3.4.30`, absent from OracleLinux 8. Fixed by
adding `-static-libstdc++ -static-libgcc` to the `MYSQL_ADD_PLUGIN` CMake target.

## 4) Validation status

### Build and CI

- CI status: pending RC2 tag.
- Release workflow (`release.yml`): pending RC2 tag.
- Docker publish (`docker-publish.yml`): pending RC2 tag.
- Lint status: Green on main (pre-tag).

### Functional checks

- Plugin smoke (`smoke-published-images.sh`): not yet run.
- Component smoke (`smoke-component.sh`): not yet run.
- Online index flow: optional for RC2.
- Regression: CI coverage pre-tag is green.

### Performance smoke

- Startup sanity: pending.
- Query latency: pending (README smoke).
- Memory: not measured for RC2.

## 5) GHCR images smoke-tested

Fill in after images are published.

| Tag | Image digest (pulled) |
| :-- | :-- |
| `mysql8.0` | TBD |
| `mysql8.4` | TBD |
| `mysql9.7` | TBD |

## 6) Component smoke results

Fill in after `./scripts/smoke-component.sh` runs.

| Tag | Result |
| :-- | :-- |
| `mysql8.4` | TBD |
| `mysql9.7` | TBD |

## 7) Go/No-Go

- Decision: Pending smoke results.
- Blockers: None identified at RC2 cut time.
