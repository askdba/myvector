# Changelog

All notable changes to MyVector will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Reset `shutdown_binlog_thread` on plugin initialization to prevent immediate thread exit on plugin reload.
- Centralize `MYSQL` connection closing in `plugin_deinit` and ensure `binlog_mysql_conn` is nulled on early exits from `myvector_binlog_loop` to prevent double-free and use-after-free issues.
- Break connection retry loop on shutdown: The binlog connection retry loop now checks the `shutdown_binlog_thread` flag, allowing for graceful termination if the plugin is unloaded while MySQL is down.
- Implement `plugin_deinit` to gracefully shut down the binlog thread and release plugin resources, preventing leaks and hangs during plugin unload/reload. This includes closing the MySQL connection used by the binlog thread and properly deleting allocated services.
- Include `<mysql.h>` in `myvector_plugin.cc` to resolve compilation errors related to `MYSQL` type and `mysql_close` function.
- Fix typo in DEMO.md that broke user setup (`.dist=L2` → `,dist=L2`) (#1)
- Fix Windows `strcasecmp` to be case-insensitive using `_stricmp` (#2)
- Fix memory leak in thread-local storage `tls_distances` by using static thread_local object (#3)
- Replace unsafe `sprintf` with `snprintf` to prevent buffer overflows (#6)
- Add `getIntOption()` helper for safe integer parsing with validation (#12)
- Restore `h_udf_metadata_service` initialization required for UDF character set handling.

### Changed
- Restructured repository layout to match `mysql-mcp-server` standard (#21)
  - Moved source files to `src/`
  - Moved headers to `include/`
  - Moved SQL files to `sql/`
  - Moved documentation to `docs/`
  - Renamed `demo/` to `examples/`
- Fixed broken documentation links caused by restructuring (#13)
- Added Makefile for simplified build process (#16)
- Added `docs/CODE_OF_CONDUCT.md` (#9)
- Added `docs/SECURITY.md` (#10)
- Added `docs/STRUCTURE.md` for project layout documentation (#19)
- Fixed typos in README.md (#7)
- Added `myvector_version` status variable (#4, #8)
- Replaced `fprintf(stderr)` with MySQL's `my_plugin_log_message` service (#5)

### Changed
- Removed `using namespace std` from codebase for better standards compliance (#11)

### Added
- Automated test suite using the MySQL Test Framework (MTR) covering core UDFs, indexing, and search functionality.
- GitHub Actions CI workflow for automated builds against MySQL 8.4 (#14)
- Initial open source release
- HNSW index support for approximate nearest neighbor search
- KNN index support for exact brute-force search
- Binary vector support with Hamming distance (HNSW_BV)
- Multiple distance metrics: L2 (Euclidean), Cosine, Inner Product
- Online index updates via MySQL binlog streaming
- Incremental index persistence and checkpointing
- Parallel index building with configurable thread count
- Query rewrite support for `MYVECTOR()`, `MYVECTOR_IS_ANN()`, `MYVECTOR_SEARCH[]`

### SQL Functions
- `myvector_construct()` - Convert embedding strings to binary vector format
- `myvector_display()` - Display binary vectors as readable strings
- `myvector_distance()` - Calculate distance between two vectors
- `myvector_hamming_distance()` - Calculate Hamming distance for binary vectors
- `myvector_ann_set()` - Perform ANN search and return neighbor IDs
- `myvector_is_valid()` - Validate vector format and checksum
- `myvector_row_distance()` - Get distance for a specific row from last search

### Stored Procedures
- `mysql.myvector_index_build()` - Build or rebuild a vector index
- `mysql.myvector_index_refresh()` - Incrementally refresh an index
- `mysql.myvector_index_load()` - Load a persisted index into memory
- `mysql.myvector_index_save()` - Persist an index to disk
- `mysql.myvector_index_drop()` - Drop a vector index
- `mysql.myvector_index_status()` - Display index statistics

### Documentation
- README.md with build and installation instructions
- DEMO.md with Amazon Product Catalog example
- demo/stanford50d/ with GloVe word embeddings example
- Updated README.md with modern branding (#28)
  - Added SVG banner with vector network visualization
  - Added SVG logo representing vector search
  - Added CI status, license, and MySQL version badges
  - Restructured with feature tables, quick start, and Docker sections
  - Added `assets/` directory for branding images

## [1.0.0] - 2025-02-01

### Added
- FOSDEM'25 MySQL Devroom presentation release
- Support for MySQL 8.0.x and MySQL 9.0.x (native VECTOR type)
- Docker image support

---

## Version History Summary

| Version | Date | Highlights |
|---------|------|------------|
| 1.0.0 | 2025-02-01 | FOSDEM'25 release, MySQL 9.0 VECTOR support |

[Unreleased]: https://github.com/askdba/myvector/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/askdba/myvector/releases/tag/v1.0.0
