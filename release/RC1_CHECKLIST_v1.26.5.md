# RC1 Checklist - v1.26.5

Target RC: `v1.26.5-rc1`
Previous release: `v1.26.3`
Policy: minor release — includes all merged changes since `v1.26.3` (plugin + component).

## 1) RC1 scope lock

- [x] Confirm release scope is `v1.26.3..HEAD` on `main`.
- [x] Confirm Component PRs **are** included (policy change from v1.26.3; component is now merged).
- [x] Confirm hard freeze is active (only blocker fixes allowed).
- [x] Confirm owners assigned for all must-ship items.

## 2) Branch and tag prep

- [ ] Verify `main` is clean and CI is green.
- [ ] Record HEAD commit SHA for candidate build (update RC1_STATUS_v1.26.5.md).
- [ ] Create and push RC tag:
  - `git tag -a v1.26.5-rc1 -m "Release candidate 1 for v1.26.5"`
  - `git push origin v1.26.5-rc1`

## 3) Build and CI gates

- [ ] CI green for `main` at RC1 commit.
- [ ] `release.yml` triggered by `v1.26.5-rc1` tag — all build jobs pass.
- [ ] `docker-publish.yml` triggered after release workflow — all image builds pass.
- [ ] No release-blocking lint/test failures.

## 4) Functional validation

- [ ] Install/load workflow verified — `smoke-published-images.sh`.
- [ ] Core vector create/query flow verified — `smoke-readme.sh` per tag.
- [ ] Component install/query flow verified — `smoke-component.sh` (new for 1.26.5).
- [ ] Update/delete behavior — rely on CI; optional explicit run for GA.
- [ ] Online index update flow — optional for RC1; see `test-online-updates.sh`.
- [ ] Regression checks for touched modules — CI coverage.

## 5) Performance smoke

- [ ] Startup sanity — containers healthy in smoke script.
- [ ] Query latency sanity — README smoke only (not a benchmark run).
- [ ] Memory sanity — not measured for RC1.

## 6) Documentation and release notes gates

- [ ] `release/RELEASE_NOTES_v1.26.5.md` reviewed.
- [ ] `CHANGELOG.md` entry for `1.26.5` reviewed.
- [ ] Docker tag table in `README.md` shows `mysql9.7` (not `mysql9.6`).
- [ ] `release/POST_RC_DOCKER_SMOKE_PLAN.md` updated for `mysql9.7`.
- [ ] Upgrade notes mention `:mysql9.6` → `:mysql9.7` tag change.

## 7) RC1 decision

- [ ] Blocker bug count = 0, OR blockers explicitly tracked for RC2.
- [ ] Go/No-Go review completed.
- [ ] RC1 tag created and pushed (see step 2).
- [ ] RC1 handoff note posted — this checklist + `RC1_STATUS_v1.26.5.md`.

## 8) If RC2 is required

- [ ] Capture blockers with owners and ETAs.
- [ ] Restrict merges to blocker fixes only.
- [ ] Prepare `v1.26.5-rc2` cut date and validation window.

## 9) Next steps after CI/registry smoke

**Order:** CI green → Release workflow green for `v1.26.5-rc1` → Publish Docker green → pull + smoke.

- [ ] All required GitHub Actions checks green on `main` at RC1 tag.
- [ ] **Publish Docker Image** workflow succeeded — verify `mysql8.0`, `mysql8.4`, `mysql9.7` on GHCR.
- [ ] Follow `release/POST_RC_DOCKER_SMOKE_PLAN.md`.
- [ ] Run `./scripts/smoke-published-images.sh` (optionally `MYVECTOR_SMOKE_STANFORD=1`).
- [ ] Run `./scripts/smoke-component.sh` against `mysql8.4` and `mysql9.7` tags.
- [ ] Optionally run `./scripts/test-online-updates.sh` against `mysql8.4`.
- [ ] Record tags tested and results in `release/RC1_STATUS_v1.26.5.md`.
