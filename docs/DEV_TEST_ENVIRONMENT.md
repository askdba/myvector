# Dev/test environment: from-scratch host setup

This doc is for setting up a fresh Linux host to build and test MyVector
locally, without the published `ghcr.io/askdba/myvector` Docker images.
Every command and number below was actually run on a real host in the
session that wrote this doc — see `docs/RELIABLE_TEST_ENV_PLAN.md` for the
investigation that produced it. Nothing here is copied from CLAUDE.md
without independent verification.

## 1. Prerequisites

### Already on a typical dev host

| Tool | Version observed on the reference host this doc was verified against |
|---|---|
| Docker | 29.1.3 |
| cmake | 3.28.3 |
| gcc | 13.3.0 (Ubuntu 13.3.0-6ubuntu2~24.04.1) |
| `gh` CLI | authenticated (`gh auth status` → logged in) |
| Host resources | 8 cores, 46GiB RAM, ~22GiB free disk (`df -h /`: 48G total, 54% used) |

All of the build/test recipes in this doc are **Docker-based** — they run
inside `oraclelinux:9` or `mysql:<ver>` containers, so only Docker itself
(plus `bash`, `git`, `gh`) is strictly required on the host.

### NOT installed by default — install these

**`cppcheck`** (used by CLAUDE.md's lint step) is not present on a fresh
host. Fix:
```bash
sudo apt-get install -y cppcheck
```
This succeeded with no sudo password prompt on the reference host.

**`mysql_config`** is commonly absent on a fresh host, and installing
MySQL dev headers just to get it is unnecessary if you're going to use the
Docker-based scripts below anyway. Without it, CLAUDE.md's "Quick build"
path (`make`, which requires `mysql_config` in `PATH`) **does not work** —
only the Docker-based scripts (`scripts/build-component-*-docker.sh`,
`scripts/build-plugin-8.4-docker.sh`) produce a working `.so` on a host
without MySQL dev headers installed. Don't chase the `make` path on such a
host; use the Docker scripts below.

### `git push` fails with "could not read Username for 'https://github.com'"

This is a real, reproducible gotcha on a fresh host even when `gh auth
status` already shows you logged in: `gh`'s own authentication doesn't
automatically wire itself into `git`'s credential helper. Fix:
```bash
gh auth setup-git
```
This adds a `credential.https://github.com.helper` entry (`!/usr/bin/gh
auth git-credential`) to `~/.gitconfig`, confirmed present after running it
this session. Run it once per host before the first `git push`.

## 2. Two build variants, and when to use which

MyVector ships as two different distribution forms, built by two different
script families. They are **not interchangeable** — pick based on what
you're testing.

| | **component** | **plugin** |
|---|---|---|
| Build scripts | `scripts/build-component-8.4-docker.sh`, `scripts/build-component-9.7-docker.sh`, `scripts/build-component-26.7-docker.sh` | `scripts/build-plugin-8.4-docker.sh` |
| Install method | `INSTALL COMPONENT 'file://myvector'` | `INSTALL PLUGIN myvector SONAME 'myvector.so'` |
| Artifact | `libmyvector_component.so` + `myvector.json` | `myvector.so` + `sql/myvectorplugin.sql` |
| UDFs, KNN brute-force, HNSW index build/status/load/drop, online updates (binlog) | Yes | Yes |
| Inline `col MYVECTOR(...)` DDL annotation | **No — fails on every currently-supported MySQL version** | Yes (verified) |
| `WHERE MYVECTOR_IS_ANN(...)` query rewrite | **No — fails on every currently-supported MySQL version** | Yes (verified) |
| Distribution used by published `ghcr.io/askdba/myvector:mysql8.4` images | No — component images are published as separate `-component` tags (e.g. `mysql8.4-component`) | Yes — this is the classic/original distribution form and still the default published tags (`mysql8.0`, `mysql8.4`, `mysql9.7`, `latest`; see `docs/DOCKER_IMAGES.md`). See `docs/COMPONENT_MIGRATION_PLAN.md` for the migration direction |

There is currently only one local plugin-build script, `scripts/build-plugin-8.4-docker.sh`
(MySQL 8.4). There is no `build-plugin-9.7-docker.sh` or `build-plugin-8.0-docker.sh` in
this repo — if you need to test the plugin build against 8.0 or 9.7, use the
published `ghcr.io/askdba/myvector:mysql8.0` / `:mysql9.7` images instead of
looking for a local script that does not exist.

**Why the split exists — this is a known, tracked gap, not test-environment
flakiness.** The component's query-rewrite source
(`src/component_src/myvector_query_rewrite_service.cc`) is only compiled
when `${MYSQL_SOURCE_DIR}/include/mysql/components/services/query_rewrite.h`
exists in the MySQL source tree. That header does not exist in current
MySQL server source at all (confirmed against fresh `mysql-8.4.8` and
`mysql-9.7.0` checkouts) — so the component build silently has *no*
query-rewrite support on any version this project currently builds
against. This is tracked as **GitHub issue #144**
(https://github.com/askdba/myvector/issues/144 — or `gh issue view 144`
for the full root-cause writeup if you have the repo checked out and `gh`
authenticated). The plugin build is unaffected because it rewrites queries
through a completely different, stable mechanism — the classic Audit
Plugin pre-parse hook (`plugin_audit.h`) in `src/myvector_plugin.cc`.
Issue #144 proposes porting query rewrite to the Event Tracking parse
service; if/when that lands, the component build gains DDL-annotation and
`MYVECTOR_IS_ANN` support too, so the table above is a current state, not
a permanent architectural split.

**Rule of thumb:**
- Testing UDFs, KNN, HNSW index lifecycle, online (binlog) updates, or
  running the pre-release/benchmark/stress scripts → **component** build is
  sufficient and is what those scripts already default to.
- Testing the inline `MYVECTOR(...)` DDL annotation or
  `WHERE MYVECTOR_IS_ANN(...)` → you need the **plugin** build (or a
  published image, which also uses the plugin form).

## 3. Quickstart

### 3a. Build both variants

```bash
cd /path/to/myvector

# Component builds (existing scripts) — build-component-8.4-docker.sh
# measured 3m42.126s (real time bash scripts/build-component-8.4-docker.sh
# ...) on a cold run on this host (full clone + full MySQL header
# configure + component build; build-component-9.7-docker.sh was not
# separately timed this session but follows the same recipe, so expect a
# similar order of magnitude).
./scripts/build-component-8.4-docker.sh mysql-8.4.8 dist/component-8.4
./scripts/build-component-9.7-docker.sh mysql-9.7.0 dist/component-9.7

# Plugin build (new script from this plan's Task 1) — measured 4m17.638s
# (real time bash scripts/build-plugin-8.4-docker.sh ...) on a cold run on
# this host. MySQL 8.4.8's source tree vendors its own boost
# (extra/boost/boost_1_84_0), so no boost network download happens even
# on a cold run.
./scripts/build-plugin-8.4-docker.sh mysql-8.4.8 dist/plugin-8.4
```

Each script assumes a matching `mysql-server-mysql-<ver>/` source checkout
already exists under the repo root (a real `git clone` of
`mysql/mysql-server` at that tag) — that's how they resolve
`MYSQL_SOURCE_DIR`; they don't re-clone if it's already present at the
right tag.

Resulting artifacts, confirmed present on this host after building:
```
dist/component-8.4/libmyvector_component.so   dist/component-8.4/myvector.json
dist/component-9.7/libmyvector_component.so   dist/component-9.7/myvector.json
dist/plugin-8.4/myvector.so                    dist/plugin-8.4/myvectorplugin.sql
```

### 3b. Run the test suites against them

```bash
# Component smoke test — spins up a fresh mysql:<ver> container, installs
# the component, runs UDF/KNN/HNSW/online-update checks. COMPONENT_DIR
# defaults to build/component, so point it at the dist/ artifact you built:
COMPONENT_DIR=dist/component-8.4 ./scripts/smoke-component.sh 8.4
COMPONENT_DIR=dist/component-9.7 ./scripts/smoke-component.sh 9.7

# Full pre-release gate (both 8.4 and 9.7; expects dist/component-8.4 and
# dist/component-9.7 to already exist, same convention as CLAUDE.md):
./scripts/pre-release-test.sh
```

`scripts/smoke-component.sh` intentionally tolerates the
`MYVECTOR_IS_ANN` query failing as a non-fatal `WARNING: ANN query failed
(query rewrite may not be active)` — that's issue #144, expected on the
component build, not a bug in the smoke script.

There is currently no equivalent automated smoke script for the *plugin*
build; Task 1's verification (see below and `docs/RELIABLE_TEST_ENV_PLAN.md`)
was done by hand against a raw `mysql:8.4` container. If you need to
re-verify the plugin build, follow the manual recipe in 3c plus the two
checks in section 2's table (DDL annotation, `MYVECTOR_IS_ANN`).

### 3c. Minimal manual recipe (poke at a single query, no test-suite scripts)

Useful when you just want to run one query against a built artifact
without the full smoke/pre-release machinery. This mirrors what
`scripts/smoke-component.sh` and Task 1's verification do internally.

```bash
# 1. Start a fresh, unmodified MySQL image (swap :8.4 for :9.7 as needed)
docker run -d --name myv-manual -e MYSQL_ROOT_PASSWORD=myvector mysql:8.4
# wait for it to accept connections:
until docker exec myv-manual mysqladmin ping -uroot -pmyvector --silent 2>/dev/null; do sleep 2; done

# 2. If the image doesn't already have libmysqlclient (plain mysql:<ver>
#    server images are server-only and may omit it — the component .so may
#    link against libmysqlclient depending on how it was built; readelf -d
#    on this host's dist/component-8.4/libmyvector_component.so shows it
#    statically linking libmysqlclient.a instead, so this step is a
#    just-in-case, not a guaranteed requirement), install it from the MySQL CDN:
docker exec myv-manual bash -c '
  SRV_VER=$(mysqld --version | grep -oE "[0-9]+\.[0-9]+\.[0-9]+" | head -1)
  ARCH=$(uname -m)
  BASE="https://cdn.mysql.com/Downloads/MySQL-${SRV_VER%.*}"
  VER="${SRV_VER}-1.el9"
  rpm -ivh --nodeps "${BASE}/mysql-community-common-${VER}.${ARCH}.rpm" 2>/dev/null || true
  rpm -ivh --nodeps "${BASE}/mysql-community-client-plugins-${VER}.${ARCH}.rpm" 2>/dev/null || true
  rpm -ivh --nodeps "${BASE}/mysql-community-libs-${VER}.${ARCH}.rpm" 2>/dev/null
'

# 3a. Component: copy the .so + json into plugin_dir, then run the full
#     installer script -- INSTALL COMPONENT alone only auto-registers the
#     core UDFs (myvector_construct, myvector_display, myvector_distance,
#     ...); sql/myvector_install_component.sql also adds the supplemental
#     UDFs and MYVECTOR_INDEX_* procedures that index build/status/search
#     actually need.
PLUGIN_DIR=$(docker exec myv-manual mysql -uroot -pmyvector -N -s -e "SELECT @@plugin_dir;")
docker cp dist/component-8.4/libmyvector_component.so myv-manual:"$PLUGIN_DIR/myvector.so"
docker cp dist/component-8.4/myvector.json             myv-manual:"$PLUGIN_DIR/myvector.json"
docker exec -i myv-manual mysql -uroot -pmyvector < sql/myvector_install_component.sql

# 3b. OR plugin: copy myvector.so, then load the SQL script -- it does its
#     own INSTALL PLUGIN (sql/myvectorplugin.sql line 32) as well as
#     registering UDFs/procedures, so don't INSTALL PLUGIN separately first
#     (that would just make the script's own INSTALL PLUGIN fail as a dup).
docker cp dist/plugin-8.4/myvector.so myv-manual:"$PLUGIN_DIR/myvector.so"
docker exec myv-manual chmod 755 "$PLUGIN_DIR/myvector.so"
docker cp dist/plugin-8.4/myvectorplugin.sql myv-manual:/tmp/myvectorplugin.sql
docker exec myv-manual bash -c "mysql -uroot -pmyvector < /tmp/myvectorplugin.sql"

# 4. Index dir: on a RAW mysql:<ver> image (not a published myvector
#    image), myvector_index_dir defaults to /mysqldata, which doesn't
#    exist. Point it at a writable, existing directory instead:
docker exec myv-manual mysql -uroot -pmyvector -e \
  "SET GLOBAL myvector_index_dir='/var/lib/mysql';"

# 5. myvector.cnf: required for the background index-build thread's
#    self-connection (myvector_host/myvector_user_id/myvector_user_password/
#    myvector_port). Published images bake this in; a raw image needs it
#    written by hand. See docs/CONFIGURATION.md and docs/DOCKER_IMAGES.md
#    for the full variable reference — this is their documented recipe:
docker exec myv-manual bash -lc "cat >/var/lib/mysql/myvector.cnf <<'EOF'
myvector_host=127.0.0.1
myvector_port=3306
myvector_user_id=root
myvector_user_password=myvector
EOF
chown mysql:mysql /var/lib/mysql/myvector.cnf; chmod 600 /var/lib/mysql/myvector.cnf"
docker exec myv-manual mysql -uroot -pmyvector -e \
  "SET GLOBAL myvector_config_file='myvector.cnf';"

# 6. Now run whatever single query you wanted, e.g. (plugin build only):
docker exec myv-manual mysql -uroot -pmyvector -e "
CREATE DATABASE IF NOT EXISTS vtest; USE vtest;
CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY,
  v MYVECTOR(type=HNSW,dim=3,size=100,m=16,ef=50));
SHOW CREATE TABLE t\G"

# Clean up:
docker rm -fv myv-manual
```

This exact sequence (steps 4-6, against `dist/plugin-8.4`) was verified
end-to-end in this plan's Task 1 against a real `mysql:8.4` (8.4.11)
container: `SHOW CREATE TABLE` echoed the `MYVECTOR(...)` column annotation
back unchanged (proving the DDL rewrite ran), and a `WHERE
MYVECTOR_IS_ANN(...)` query against a populated, indexed table returned
correct, nearest-neighbor-ordered rows instead of a `FUNCTION ... does not
exist` error. See `docs/RELIABLE_TEST_ENV_PLAN.md`'s Task 1 section for the
scope of what was asked.

## Verification of this doc

The quickstart's `scripts/smoke-component.sh` invocation from section 3b
was actually run against this doc's own `dist/component-8.4` and
`dist/component-9.7` artifacts before this doc was committed:
```bash
COMPONENT_DIR=dist/component-8.4 ./scripts/smoke-component.sh 8.4
COMPONENT_DIR=dist/component-9.7 ./scripts/smoke-component.sh 9.7
```
Both completed with `=== Smoke test complete ===`, all `PASS:` lines for
UDFs/KNN/HNSW build/status/persist-reload/online-updates/multi-column
binlog/uninstall, and the expected, tolerated
`WARNING: ANN query failed (query rewrite may not be active)` for the
`MYVECTOR_IS_ANN` step (issue #144, not a failure of this doc's
instructions).
