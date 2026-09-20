# Release Notes - v1.26.9

Release date: TBD
Previous release: v1.26.5.2

## Summary

v1.26.9 adds support for the MySQL 26.7 Innovation release (component-only)
and turns Docker publishing into a proper component pipeline, so the
`INSTALL COMPONENT` build is published alongside the legacy plugin images.
It also completes the component install/uninstall SQL and fixes
`UNINSTALL COMPONENT` failing with ERROR 3538.

## Added

- **MySQL 26.7 Innovation support** (component only): new
  `scripts/build-component-26.7-docker.sh`, CI jobs `build-component-26-7` /
  `test-component-26-7`, and wiring in `release.yml`, `myvectorbench.yml`,
  `pre-release-test.sh` (opt-in: `./scripts/pre-release-test.sh 26.7`) and
  `smoke-published-images.sh`.
- **Component Docker images**: new `Dockerfile.component` and a
  `build-and-publish-component` job in `docker-publish.yml` publishing
  `mysql8.4-component`, `mysql9.7-component` and `mysql26.7`. Existing plugin
  tags (`mysql8.0`, `mysql8.4`, `mysql9.7`, `latest`) are unchanged.
- **`sql/myvector_install_component.sql` / `sql/myvector_uninstall_component.sql`**:
  complete component install (supplemental UDFs, `MYVECTOR_INDEX_*`
  procedures, `myvector_columns` view) and uninstall scripts. Release
  artifacts now bundle the component SQL instead of the plugin SQL.
- **`DOCKER_PLATFORM`** option on the 8.4 / 9.7 / 26.7 component build scripts
  for multi-arch builds.
- **Docs site** (MkDocs Material, myvector.online) and `docs/CONTRIBUTING.md`.

## Changed

- Component build scripts fall back to the MySQL CDN archive for pinned point
  releases that are no longer on the primary download path.
- Docker publish jobs apply a `v*` version-tag guard to both the plugin and
  component publish paths; `dry_run` input added to exercise the component
  publish without pushing.

## Fixed

- **`UNINSTALL COMPONENT` ERROR 3538**: the binlog service's stop path
  returned non-zero on success, which `myvector_component_deinit()`
  propagated as a failure whenever the binlog thread had been running.
  It now returns 0.
- **Component images missing SQL surface**: published `*-component` images
  previously exposed only auto-registered core UDFs; HNSW index build and ANN
  workflows failed. Fixed by the expanded install SQL above.
- **Uninstallable release artifact**: `release.yml` packaged the plugin SQL
  with a component `.so`.
- Two publish-workflow failures exposed by the dry run.

## Documentation updates

- `README.md`, `docs/DOCKER_IMAGES.md`, `docs/quickstart.md`,
  `docs/COMPONENT_MIGRATION_PLAN.md`: corrected tag tables, plugin-vs-component
  image contents, and a pre-existing 9.0 / 9.7 contradiction.
- New docs site pages: index, quickstart, usage.

## Upgrade / migration notes

- No migration action required for existing plugin or component users.
- MySQL 26.7 is component-only; there is no plugin build for 26.7.
- Component installs require `myvector.cnf` to be present before
  `INSTALL COMPONENT` (the binlog listener reads it at startup); see the
  header of `sql/myvector_install_component.sql`.

## Known issues

- MySQL 26.7 is a new Innovation release with a short track record. Its
  `pre-release-test.sh` run is opt-in by default but is a **blocking** gate
  for this release.
- No 26.7 benchmark baseline exists yet; the RC1 run establishes it.
