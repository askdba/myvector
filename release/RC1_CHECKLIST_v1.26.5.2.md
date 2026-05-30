# RC1 Checklist - v1.26.5.2

Target RC: `v1.26.5.2-rc1`
Previous release: `v1.26.5.1`
Policy: patch release — tooling, reliability tests, and two correctness fixes (HNSW case, ERROR 3540).

## 1) RC1 scope lock

- [x] Confirm release scope is `v1.26.5.1..HEAD` on `main`.
- [x] No breaking plugin or component API changes.
- [x] Hard freeze active (only blocker fixes allowed).

## 2) Branch and tag prep

- [ ] Verify `main` is clean and CI is green.
- [ ] Record HEAD commit SHA in `RC1_STATUS_v1.26.5.2.md`.
- [ ] Create and push RC tag:
  ```bash
  git tag -a v1.26.5.2-rc1 -m "Release candidate 1 for v1.26.5.2"
  git push origin v1.26.5.2-rc1
  ```

## 3) Build and CI gates

- [ ] CI green for `main` at RC1 commit (MyVector CI — 12 jobs).
- [ ] `release.yml` triggered by `v1.26.5.2-rc1` tag — all build jobs pass.
- [ ] `docker-publish.yml` triggered — `mysql8.0`, `mysql8.4`, `mysql9.7` images built and pushed to GHCR.
- [ ] No lint/test failures.

## 4) Pre-release test gate

```bash
./scripts/build-component-8.4-docker.sh mysql-8.4.8 dist/component-8.4
./scripts/build-component-9.7-docker.sh mysql-9.7.0 dist/component-9.7
./scripts/pre-release-test.sh
```

- [ ] Phase 1 (smoke): PASS
- [ ] Phase 2 (online updates): PASS
- [ ] Phase 3 (lifecycle regression): PASS on 8.4
- [ ] Phase 3 (lifecycle regression): PASS on 9.7
- [ ] Exit 0 on both versions.

## 5) Functional validation

- [ ] `./scripts/smoke-published-images.sh` — all GHCR images pass.
- [ ] `bash scripts/smoke-readme.sh ghcr.io/askdba/myvector:mysql8.4`
- [ ] `bash scripts/smoke-readme.sh ghcr.io/askdba/myvector:mysql9.7`
- [ ] HNSW type case-insensitivity fix verified:
  ```sql
  -- Column COMMENT with lowercase 'hnsw' must build HNSW (not brute-force)
  CALL mysql.MYVECTOR_INDEX_STATUS('db.table.col');
  -- Expect: Type: HNSW (not Brute Force)
  ```
- [ ] ERROR 3540 fix verified: `UNINSTALL COMPONENT 'file://myvector'` succeeds
  without `ER_COMPONENTS_UNLOAD_CANT_DEINITIALIZE` (covered by Phase 3 subtest 3.1).

## 6) Benchmark gate

```bash
python3 scripts/myvectorbench.py \
    --mysql-version 8.4 --build-path plugin \
    --artifact-dir dist/plugin-8.4 \
    --image ghcr.io/askdba/myvector:mysql8.4 \
    --output results/rc1-bench-plugin-8.4.json
python3 scripts/myvectorbench-compare.py \
    results/bench-plugin-8.4.json results/rc1-bench-plugin-8.4.json \
    --config myvectorbench.yml
```

- [ ] All metrics within threshold (exit 0), OR variance explained (local Docker noise).
- [ ] Record results in `RC1_STATUS_v1.26.5.2.md`.

## 7) Concurrent stress gate (optional for RC1)

```bash
python3 scripts/bench-concurrent-stress.py \
    --mysql-version 8.4 --build component \
    --artifact-dir dist/component-8.4 \
    --threads-knn 50 --threads-write 50 --threads-ann 20 \
    --duration 60 --output results/rc1-stress-8.4.json
```

- [ ] `passed=true` in output JSON.

## 8) Documentation and release notes gates

- [ ] `release/RELEASE_NOTES_v1.26.5.2.md` reviewed and accurate.
- [ ] `CHANGELOG.md` entry for `[1.26.5.2]` reviewed.
- [ ] No stale version references in README or docs.

## 9) RC1 decision

- [ ] Blocker count = 0.
- [ ] Go/No-Go review completed.
- [ ] RC tag pushed (step 2).
- [ ] Results recorded in `RC1_STATUS_v1.26.5.2.md`.

## 10) Post-RC: final tag

After smoke passes on published GHCR images:
```bash
git tag -a v1.26.5.2 -m "Release v1.26.5.2"
git push origin v1.26.5.2
```
