# RC1 Checklist - v1.26.3

Target RC: `v1.26.3-rc1`
Previous release: `v1.26.1`
Policy: minor release, include merged changes since `v1.26.1`, no Component PRs

## 1) RC1 scope lock

- [ ] Confirm release scope is `v1.26.1..HEAD`.
- [ ] Confirm no Component PRs are included in RC1 scope.
- [ ] Confirm hard freeze is active (only blocker fixes allowed).
- [ ] Confirm owners assigned for all must-ship items.

## 2) Branch and tag prep

- [ ] Create release branch:
  - `git checkout -b release/v1.26.3-rc1`
- [ ] Verify clean state:
  - `git status`
- [ ] Record commit SHA for candidate build.

## 3) Build and CI gates

- [ ] CI is green for release branch.
- [ ] Build succeeds from clean checkout.
- [ ] Required test suites pass.
- [ ] No release-blocking lint/test failures.

## 4) Functional validation

- [ ] Install/load workflow verified.
- [ ] Core vector create/query flow verified.
- [ ] Update/delete behavior verified.
- [ ] Online index update flow verified (if in scope).
- [ ] Regression checks for touched modules completed.

## 5) Performance smoke

- [ ] Startup sanity check.
- [ ] Representative query latency sanity check.
- [ ] Memory sanity under repeated operations.

## 6) Documentation and release notes gates

- [ ] `release/RELEASE_NOTES_DRAFT.md` reviewed for RC1.
- [ ] `CHANGELOG.md` entry for `1.26.3` reviewed.
- [ ] User-facing docs updated for behavior/config changes.
- [ ] Upgrade/migration notes validated (or "none required" confirmed).

## 7) RC1 decision

- [ ] Blocker bug count = 0, OR blockers explicitly tracked for RC2.
- [ ] Go/No-Go review completed.
- [ ] RC1 tag created:
  - `git tag -a v1.26.3-rc1 -m "Release candidate 1 for v1.26.3"`
- [ ] RC1 handoff note posted (scope, risks, known issues).

## 8) If RC2 is required

- [ ] Capture blockers with owners and ETAs.
- [ ] Restrict merges to blocker fixes only.
- [ ] Prepare `v1.26.3-rc2` cut date and validation window.

## 9) After CI green and images on GHCR (local smoke)

PR CI builds images but **does not push** to GHCR until a `v*` tag or release triggers publish. After publish completes:

- [ ] Follow `release/POST_RC_DOCKER_SMOKE_PLAN.md`.
- [ ] Run `./scripts/smoke-published-images.sh` (optionally `MYVECTOR_SMOKE_STANFORD=1`).
- [ ] Optionally run `./scripts/test-online-updates.sh` against `mysql8.4` (see plan).
- [ ] Record tags tested and results in `release/RC1_STATUS_v1.26.3.md`.

