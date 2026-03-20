# RC1 Status - v1.26.3

## 1) Release metadata

- Release version: `v1.26.3`
- Candidate tag: `v1.26.3-rc1` (exists on remote)
- Previous release: `v1.26.1`
- Release manager: `askdba`
- Date opened: `2026-03-19`
- Last updated: `2026-03-20`

## 2) Candidate commit and branch

- Release branch: `release/v1.26.3-rc1`
- Working branch: `release/v1.26.3-rc1`
- **RC1 validation baseline (code/CI/smoke):** `5a5ce7f` — actionlint-based lint job green; MyVector CI green; GHCR smoke passed for all published tags (see below).
- Scope range: `v1.26.1..HEAD` on the release branch (minor delta since `v1.26.1`, no Component PRs per policy).

## 3) Scope and freeze status

- Scope lock completed: **Done** (RC1 scope as on branch; merge to `main` next).
- Hard freeze active: **Lifted for RC1 merge** (post sign-off).
- Merge exception process active: **N/A** (no open exceptions).
- Must-ship owners assigned: **Done** (`askdba`).

## 4) Validation status

### Build and CI

- CI status: **Green** on release branch / PR.
- Clean checkout build: **Pass** (CI build matrix).
- Test suite pass status: **Pass** (CI integration tests).
- Lint status: **Green** — `Lint Code Base` uses [actionlint](https://github.com/rhysd/actionlint) (official download script; no Super-Linter).

### Functional checks

- Install/load workflow: **Pass** — `./scripts/smoke-published-images.sh` (README smoke per image).
- Core create/query flow: **Pass** — `smoke-readme.sh` vec / distance checks on each GHCR tag.
- Update/delete flow: **Not explicitly re-run for RC1** (scope: rely on CI + smoke; track if needed for GA).
- Online index flow: **Optional** — `test-online-updates.sh` not required for RC1 sign-off (see `POST_RC_DOCKER_SMOKE_PLAN.md`).
- Regression checks: **Pass** (CI coverage on branch).

### Performance smoke

- Startup sanity: **Pass** (containers healthy in smoke script).
- Query latency sanity: **Light** — README smoke only; not a benchmark run.
- Memory sanity: **Not measured** for RC1 (no regression reported in smoke).

## 5) GHCR images smoke-tested (2026-03-20)

Pulled from `ghcr.io/askdba/myvector` and ran `./scripts/smoke-published-images.sh` — **all completed OK**.

| Tag | Image digest (pulled) |
| :-- | :-- |
| `mysql8.0` | `sha256:e2b0a1cc2461a437ad23ca6504580ff5c38ebf8e16640b7447830050d7e9c6d7` |
| `mysql8.4` | `sha256:787f0301415a459f48b7e8557c804c6f8a3dc92e9bb257fc4cba1e07365eda81` |
| `mysql9.6` | `sha256:4e53ee4749f998802c32c38b43ef89c6348030f5abfc5daaf2c70d88e9ffad44` |

Optional heavier check not run for RC1: `MYVECTOR_SMOKE_STANFORD=1 ./scripts/smoke-published-images.sh`.

## 6) Release notes and documentation

- `release/RELEASE_NOTES_DRAFT.md` reviewed: **Done** (RC1 merge readiness).
- `CHANGELOG.md` updated/reviewed: **Done** (`1.26.3` section present).
- User-facing docs updated: **Done** (platform/config/release notes aligned with branch).
- Upgrade/migration notes reviewed: **None required** for this minor RC (per draft).

## 7) Blockers and risks

### Open blockers

| ID | Title | Owner | Severity | ETA | Status |
| :-- | :-- | :-- | :-- | :-- | :-- |
| None | None | — | — | — | — |

### Key risks

- **Supply chain / CI:** Addressed by pinning lint to actionlint; registry images smoke-tested at digests above.
- **Residual:** GA may add Stanford/heavier smoke and online-update tests if desired.

## 8) Go/No-Go decisions

| Date | Decision | Participants | Notes |
| :-- | :-- | :-- | :-- |
| 2026-03-19 | In progress | askdba | RC1 tracking opened. |
| 2026-03-20 | **Go** | askdba | CI green, GHCR smoke OK on `mysql8.0` / `mysql8.4` / `mysql9.6`; **RC1 ready to merge to `main`**. |

## 9) Outcome

- RC1 outcome: **Go** — merge release branch to `main` when PR is approved.
- If `No-Go`, RC2 target date: **N/A**
- Notes: Post-merge: tag final `v1.26.3` when ready for GA; run **Publish Docker Image** for GA tag if images need refresh.
