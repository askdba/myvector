# Reliable local test environment — plan

## Context (no separate spec; this plan is self-contained)

This session verified #119 and #130 using MyVector's **component** build
(`INSTALL COMPONENT`, `scripts/build-component-*-docker.sh`). Everything
worked reliably *except* query-rewrite-dependent features (the inline
`col MYVECTOR(...)` DDL annotation and `WHERE MYVECTOR_IS_ANN(...)`), which
failed with `ERROR 1064` / `FUNCTION ... does not exist` on fresh MySQL
8.4.8 and 9.7.0 source checkouts, in both this session's ad hoc testing
*and* the project's own `scripts/smoke-component.sh` (which already
tolerates it as a non-fatal `WARNING`).

Root cause (see issue #144): the component's query-rewrite service
(`src/component_src/myvector_query_rewrite_service.cc`) is only compiled
when `${MYSQL_SOURCE_DIR}/include/mysql/components/services/query_rewrite.h`
exists. That header does not exist in current MySQL server source at all
(confirmed against fresh `mysql/mysql-server` checkouts for both
`mysql-8.4.8` and `mysql-9.7.0`) — it's dead code on every version this
project currently builds against. The **legacy plugin** build
(`src/myvector_plugin.cc`, `INSTALL PLUGIN`, e.g. the published
`ghcr.io/askdba/myvector:mysql8.4` image used to reproduce #119 and #130)
rewrites queries via a completely different, unaffected mechanism — the
classic Audit Plugin pre-parse hook (`plugin_audit.h`) — which is why it
worked in the original issue reports.

**Conclusion:** a test environment on this host that only builds the
component variant cannot exercise query-rewrite features at all, on any
currently-supported MySQL version. A *reliable* environment needs to build
and verify both variants, and document which one to reach for.

Host prerequisites already confirmed present in this session: Docker
29.1.3, cmake 3.28.3, gcc 13.3.0, 8 cores / 46GiB RAM / 22GiB free disk,
`gh` authenticated. `cppcheck` was missing and was installed via
`apt-get install -y cppcheck` (no sudo password prompt). `mysql_config` is
**not** installed (the CLAUDE.md "quick build" `make` path is unavailable
here; only the Docker-based component/plugin build paths work). Local
source checkouts already present: `mysql-server-mysql-8.4.8/`,
`mysql-server-mysql-9.7.0/`, `mysql-server-mysql-26.7.0/` (each a real
`git clone` of `mysql/mysql-server`, not a tarball).

## Global Constraints

- Every new script must follow the existing `scripts/build-component-*-docker.sh`
  pattern: a single self-contained script, mounting the repo at `/workspace`
  inside an `oraclelinux:9` container, taking `MYSQL_TAG` and `OUTPUT_DIR`
  positional args, producing a versioned artifact under `dist/`.
- Do not modify `src/`, `CMakeLists.txt`, or any existing test script's
  pass/fail semantics in this plan — this plan is tooling/docs only, not a
  fix for #144 (that's separate, tracked work).
- Every script and doc claim must be verified against a real run in this
  session before being written down — no untested instructions.
- Match the project's existing bash style: `set -e` (or `set -euo pipefail`
  where the file already does), the `pass`/`fail`/`skip`/`die` helper
  convention where a script produces test-like output, comments explaining
  *why* not just *what* (see `scripts/pre-release-test.sh` for the house
  style).
- The new doc's home is `docs/DEV_TEST_ENVIRONMENT.md`. Cross-link it from
  `CLAUDE.md`'s Testing section (one line, "see docs/DEV_TEST_ENVIRONMENT.md
  for a from-scratch host setup") — do not restructure CLAUDE.md further.

## Task 1: Docker-based plugin build script + end-to-end verification

Add `scripts/build-plugin-8.4-docker.sh`, mirroring
`scripts/build-component-8.4-docker.sh` (same oraclelinux:9 base, same
MySQL CDN devel-RPM install approach, same `MYSQL_TAG`/`OUTPUT_DIR` args)
but building the **in-tree plugin** target instead of the standalone
component:

- Inside the container, clone/use a MySQL server source tree at the given
  tag (the script can assume `mysql-server-mysql-<ver>` already exists
  under the repo root, same as the component scripts do via
  `MYSQL_SOURCE_DIR` — do not re-implement source fetching; read how
  `scripts/build-component-8.4-docker.sh` locates/uses
  `mysql-server-mysql-8.4.8/` and follow the same approach for consistency).
- Copy this repo's plugin sources into `<mysql-server>/plugin/myvector/`
  (`myvector_plugin.cc`, `myvector_binlog.cc`, `myvector.cc`,
  `myvectorutils.cc`, `CMakeLists.txt`, `include/`) — this is the "In-tree
  plugin build" mode `CMakeLists.txt` already supports (the
  `MYSQL_ADD_PLUGIN` branch at the top of the file).
- Configure MySQL's own build (`cmake .. -DCMAKE_BUILD_TYPE=Release` at the
  MySQL source root — this needs `-DDOWNLOAD_BOOST=1 -DWITH_BOOST=<path>`
  or an already-present boost tree; check whether one of the existing
  `mysql-server-mysql-*/bld-aarch64` directories on this host already has
  boost resolved and can be reused as a build-dir seed to avoid a full
  MySQL server build — a full from-scratch MySQL server build is expensive
  and likely unnecessary if only the plugin target needs building; if it
  turns out a full server build is unavoidable, say so plainly in the
  report rather than letting it run silently for a very long time — check
  in with a progress note past 10 minutes).
- `make myvector -j$(nproc)`, then package `myvector.so` (and
  `sql/myvectorplugin.sql`) into `OUTPUT_DIR`, matching the component
  scripts' `dist/plugin-8.4/` output convention.
- **Verify end-to-end in a fresh `mysql:8.4` container** (same pattern as
  this session's manual container tests): run `sql/myvectorplugin.sql`
  against it (it does its own `INSTALL PLUGIN` — don't run that separately
  first, or the script's own `INSTALL PLUGIN` fails as a duplicate), then
  confirm BOTH of these succeed:
  1. `CREATE TABLE ... (id INT PRIMARY KEY, v MYVECTOR(type=HNSW,dim=3,size=100,m=16,ef=50))` —
     the inline DDL annotation.
  2. A `WHERE MYVECTOR_IS_ANN(...)` query against a built index returns a
     result instead of `FUNCTION ... does not exist`.
- Report file: which of the two verifications passed/failed, the exact
  commands run, and how long the full build took (wall clock) — this
  number belongs in Task 2's doc.

If the plugin build turns out to require resources or time this host
plausibly can't spare (e.g., a full MySQL server build exceeding ~20
minutes, or exhausting the ~22GiB free disk), stop, report DONE_WITH_CONCERNS
with the measured cost, and do not let Task 2 assume the script is fast to
run casually.

## Task 2: `docs/DEV_TEST_ENVIRONMENT.md`

Write a new doc, read-verified against this session's actual commands (not
copied from CLAUDE.md without checking), covering:

1. **Prerequisites** — what's already on this host (see Context above) vs.
   what a fresh host needs installed, with the exact install commands used
   this session (`apt-get install -y cppcheck`, `gh auth setup-git` to fix
   `git push` "Username for 'https://github.com'" failures under the `gh`
   credential helper).
2. **Two build variants, and when to use which** — a short table:
   component (`scripts/build-component-*-docker.sh`) for everything except
   query-rewrite; plugin (Task 1's new script) for DDL annotation /
   `MYVECTOR_IS_ANN`; link issue #144 as why the split exists and that it's
   not test-environment flakiness.
3. **Quickstart** — copy-pasteable commands to build both variants into
   `dist/component-8.4`, `dist/component-9.7`, `dist/plugin-8.4`, and run
   `scripts/smoke-component.sh` / `scripts/pre-release-test.sh` against
   them, plus the minimal manual container recipe (`docker run` a
   `mysql:<ver>` image, install libmysqlclient from the CDN if missing,
   `docker cp` the artifact, `INSTALL COMPONENT`/`INSTALL PLUGIN`,
   `myvector.cnf` for `myvector_host`/`myvector_user_id`/
   `myvector_user_password`/`myvector_port`) for anyone who wants to poke
   at a single query without the full test-suite scripts.
4. Cross-link from `CLAUDE.md`'s Testing section (one line, per Global
   Constraints).

Verify by actually running the quickstart's `scripts/smoke-component.sh`
invocation from the doc (not from memory) before calling this task done.
