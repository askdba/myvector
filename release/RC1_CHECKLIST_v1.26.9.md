# RC1 Checklist - v1.26.9

Target RC: `v1.26.9-rc1`
Previous release: `v1.26.5.2`
Policy: minor line — MySQL 26.7 Innovation support (component-only) and component Docker publishing.
26.7 is a **blocking** gate for this RC (headline feature): any 26.7 failure = No-Go.

## 1) RC1 scope lock

- [ ] Confirm release scope is `v1.26.5.2..HEAD` on `main` (PR #102 docs site, PR #105 component install/uninstall, PR #104 26.7 + component publishing).
- [ ] No breaking plugin or component API changes.
- [ ] Hard freeze active (only blocker fixes allowed).

## 2) Branch and tag prep

- [ ] Verify `main` is clean and CI is green.
- [ ] Record HEAD commit SHA in `RC1_STATUS_v1.26.9.md`.
- [ ] Create and push RC tag (**only after sections 3-4 pass and Go/No-Go is recorded**):
  ```bash
  git tag -a v1.26.9-rc1 -m "Release candidate 1 for v1.26.9"
  git push origin v1.26.9-rc1
  ```

## 3) Build and CI gates

- [ ] CI green for `main` at RC1 commit (MyVector CI incl. `build-component-26-7` and `test-component-26-7`, which are **blocking**).
- [ ] `release.yml` triggered by `v1.26.9-rc1` tag — all build jobs pass (incl. Component 26.7).
- [ ] `docker-publish.yml` triggered — `mysql8.0`, `mysql8.4`, `mysql9.7`, `mysql8.4-component`, `mysql9.7-component`, `mysql26.7` built and pushed to GHCR.
- [ ] No lint/test failures.

## 4) Pre-release test gate

`pre-release-test.sh` with no argument runs **8.4 and 9.7 only**; 26.7 is opt-in and
must be run explicitly. Both invocations are required for this RC.

```bash
./scripts/build-component-8.4-docker.sh  mysql-8.4.8  dist/component-8.4
./scripts/build-component-9.7-docker.sh  mysql-9.7.0  dist/component-9.7
./scripts/build-component-26.7-docker.sh mysql-26.7.0 dist/component-26.7
./scripts/pre-release-test.sh          # 8.4 + 9.7
./scripts/pre-release-test.sh 26.7     # 26.7 (BLOCKING)
```

- [ ] Phase 1 (smoke): PASS on 8.4, 9.7, 26.7
- [ ] Phase 2 (online updates): PASS on 8.4, 9.7, 26.7
- [ ] Phase 3 (lifecycle regression): PASS on 8.4
- [ ] Phase 3 (lifecycle regression): PASS on 9.7
- [ ] Phase 3 (lifecycle regression): PASS on 26.7 (**blocking**)
- [ ] Phase 3 subtests 3.1-3.5 all executed on every version (3.5 = refused UNINSTALL leaves component intact); none aborted.
- [ ] Exit 0 on all three invocations.

## 5) Functional validation

- [ ] `./scripts/smoke-published-images.sh` — all six GHCR tags pass (8.0, 8.4, 9.7, 8.4-component, 9.7-component, 26.7).
- [ ] `bash scripts/smoke-readme.sh ghcr.io/askdba/myvector:mysql8.4`
- [ ] `bash scripts/smoke-readme.sh ghcr.io/askdba/myvector:mysql9.7`
- [ ] `bash scripts/smoke-readme.sh ghcr.io/askdba/myvector:mysql26.7` (**blocking**)
- [ ] Component image exposes full SQL surface (`sql/myvector_install_component.sql`):
  ```sql
  SELECT COUNT(*) FROM mysql.func;                          -- supplemental UDFs registered
  CALL mysql.MYVECTOR_INDEX_STATUS('db.table.col');         -- procedures present
  ```
- [ ] `UNINSTALL COMPONENT 'file://myvector'` succeeds with no ERROR 3538 on 8.4, 9.7, 26.7
  (covered by Phase 3 subtest 3.1).
- [ ] Reinstall after uninstall works (Phase 3 index reload persistence).

## 6) Benchmark gate

```bash
python3 scripts/myvectorbench.py \
    --mysql-version 26.7 --build component \
    --artifact-dir dist/component-26.7 \
    --output results/rc1-bench-component-26.7.json
```

- [ ] 26.7 benchmark completes; results recorded (no baseline exists yet — this run establishes it).
- [ ] 8.4 / 9.7 component benchmarks within threshold vs. saved baselines (exit 0), OR variance explained (local Docker noise).
- [ ] Record results in `RC1_STATUS_v1.26.9.md`.

## 7) Concurrent stress gate (optional for RC1)

```bash
python3 scripts/bench-concurrent-stress.py \
    --mysql-version 26.7 --build component \
    --artifact-dir dist/component-26.7 \
    --threads-knn 50 --threads-write 50 --threads-ann 20 \
    --duration 60 --output results/rc1-stress-26.7.json
```

- [ ] `passed=true` in output JSON.

## 8) Documentation and release notes gates

- [ ] `release/RELEASE_NOTES_v1.26.9.md` reviewed and accurate.
- [ ] `CHANGELOG.md` entry for `[1.26.9]` reviewed.
- [ ] `docs/DOCKER_IMAGES.md` tag table matches published tags.
- [ ] No stale version references in README or docs.

## 9) RC1 decision

- [ ] Blocker count = 0 (including 26.7).
- [ ] Go/No-Go review completed.
- [ ] RC tag pushed (step 2).
- [ ] Results recorded in `RC1_STATUS_v1.26.9.md`.

## 10) Post-RC: final tag

After smoke passes on published GHCR images:
```bash
git tag -a v1.26.9 -m "Release v1.26.9"
git push origin v1.26.9
```
