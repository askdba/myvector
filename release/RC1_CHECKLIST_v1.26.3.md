# RC1 Checklist - v1.26.3

Target RC: `v1.26.3-rc1`
Previous release: `v1.26.1`
Policy: minor release, include merged changes since `v1.26.1`, no Component PRs

## 1) RC1 scope lock

- [x] Confirm release scope is `v1.26.1..HEAD`.
- [x] Confirm no Component PRs are included in RC1 scope.
- [x] Confirm hard freeze is active (only blocker fixes allowed). → **Released for RC1 merge (2026-03-20).**
- [x] Confirm owners assigned for all must-ship items.

## 2) Branch and tag prep

- [x] Create release branch:
  - `git checkout -b release/v1.26.3-rc1`
- [x] Verify clean state:
  - `git status`
- [x] Record commit SHA for candidate build. → See `release/RC1_STATUS_v1.26.3.md` (baseline `5a5ce7f` for validation).

## 3) Build and CI gates

- [x] CI is green for release branch.
- [x] Build succeeds from clean checkout.
- [x] Required test suites pass.
- [x] No release-blocking lint/test failures.

## 4) Functional validation

- [x] Install/load workflow verified. → GHCR smoke (`smoke-published-images.sh`).
- [x] Core vector create/query flow verified. → `smoke-readme.sh` per tag.
- [ ] Update/delete behavior verified. → **Deferred:** CI + scope; optional before GA.
- [ ] Online index update flow verified (if in scope). → **Optional** for RC1; see `test-online-updates.sh` for GA if needed.
- [x] Regression checks for touched modules completed. → CI on branch.

## 5) Performance smoke

- [x] Startup sanity check. → Smoke script / container health.
- [x] Representative query latency sanity check. → Light (README smoke only).
- [ ] Memory sanity under repeated operations. → **Not run** for RC1.

## 6) Documentation and release notes gates

- [x] `release/RELEASE_NOTES_DRAFT.md` reviewed for RC1.
- [x] `CHANGELOG.md` entry for `1.26.3` reviewed.
- [x] User-facing docs updated for behavior/config changes.
- [x] Upgrade/migration notes validated (or "none required" confirmed).

## 7) RC1 decision

- [x] Blocker bug count = 0, OR blockers explicitly tracked for RC2.
- [x] Go/No-Go review completed. → **Go** (2026-03-20).
- [x] RC1 tag created:
  - `git tag -a v1.26.3-rc1 -m "Release candidate 1 for v1.26.3"`
- [x] RC1 handoff note posted (scope, risks, known issues). → This checklist + `RC1_STATUS_v1.26.3.md`.

## 8) If RC2 is required

- [ ] Capture blockers with owners and ETAs.
- [ ] Restrict merges to blocker fixes only.
- [ ] Prepare `v1.26.3-rc2` cut date and validation window.

## 9) Next step after CI checks complete (registry smoke)

**Order:** (1) All PR/branch checks green → (2) **Publish Docker Image** green for a **`v*`** tag so images exist on GHCR → (3) local pull + smoke.

PR CI builds images in the runner but **does not push** to GHCR until a **`v*`** tag or release triggers publish.

- [x] All required GitHub Actions checks completed successfully on the release line.
- [x] **Publish Docker Image** workflow succeeded for the tag that ships images (verify `mysql8.0`, `mysql8.4`, `mysql9.6` on GHCR).
- [x] Follow `release/POST_RC_DOCKER_SMOKE_PLAN.md` (next-step sequence at top).
- [x] Run `./scripts/smoke-published-images.sh` (optionally `MYVECTOR_SMOKE_STANFORD=1`).
- [ ] Optionally run `./scripts/test-online-updates.sh` against `mysql8.4` (see plan). → **Not run** for RC1.
- [x] Record tags tested and results in `release/RC1_STATUS_v1.26.3.md`.
