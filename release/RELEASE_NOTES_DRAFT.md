# Release Notes - v1.26.3

Release date: TBD
Previous release: v1.26.1

## Summary

This minor release focuses on configuration security hardening, binlog/config
stability, improved Docker/local validation workflows, and documentation
coverage for setup, testing, and operations.

## Added

- Security hardening for configuration handling and operational checks.
- Docker test scripts and online index update validation guidance.
- Benchmark workflow and baseline documentation for issue 79.
- Expanded licensing and compatibility documentation.
- MySQL 9.x setup examples and updated macOS build observations.

## Changed

- Binlog/config loading behavior improved for thread safety and deterministic
  failure handling.
- Local Docker smoke workflow updated for more stable validation behavior.
- Documentation expanded across configuration, Docker images, and online updates.
- Minor improvements to changelog and repository hygiene files.

## Fixed

- Lint issues resolved across scripts, docs, and workflows.
- Binlog retry and config rejection handling corrected to fail fast.
- `myvector_construct` constant-argument performance path optimized.
- Forward declaration fix for `myvector_construct_bv`.

## Documentation updates

- Updated `docs/CONFIGURATION.md` with security-oriented guidance.
- Updated `docs/ONLINE_INDEX_UPDATES.md` and `docs/DOCKER_IMAGES.md`.
- Updated `docs/BUILDING_MACOS.md` and MySQL 9.x setup references.
- Added benchmark baseline docs and licensing docs updates.

## Upgrade / migration notes

- No schema migration required for this release.
- Review configuration and file-permission expectations before rollout.
- For Apple Silicon local testing, prefer documented prebuilt images/workflows.

## Known issues

- No new release-blocking issues currently identified.
- Final known-issues list will be validated at RC sign-off.

## Excluded from this release scope

- Component PRs are excluded by RC policy.

