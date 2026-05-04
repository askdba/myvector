# RC1 Status - v1.26.5

## 1) Release metadata

- Release version: `v1.26.5`
- Candidate tag: `v1.26.5-rc1`
- Previous release: `v1.26.3`
- Release manager: `askdba`
- Date opened: `2026-05-04`
- Last updated: `2026-05-04`

## 2) Candidate commit and branch

- Working branch: `main`
- **RC1 validation baseline commit:** `1864941e93478d4c2d3366094a812c8b966f7974`
- Scope range: `v1.26.3..HEAD` on `main`.

## 3) Scope and freeze status

- Scope lock completed: Done.
- Hard freeze active: Active at RC1 cut.
- Must-ship owners assigned: `askdba`.

## 4) Validation status

### Build and CI

- CI status: pending RC1 tag.
- Release workflow (`release.yml`): pending RC1 tag.
- Docker publish (`docker-publish.yml`): pending RC1 tag.
- Lint status: Green on main (pre-tag).

### Functional checks

- Plugin smoke (`smoke-published-images.sh`): not yet run.
- Component smoke (`smoke-component.sh`): not yet run.
- Online index flow: optional for RC1.
- Regression: CI coverage pre-tag is green.

### Performance smoke

- Startup sanity: pending.
- Query latency: pending (README smoke).
- Memory: not measured for RC1.

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
- Blockers: None identified at cut time.
