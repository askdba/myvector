# Component Query Rewrite Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two smoke test failures in the myvector component: ERROR 1210 (ANN query rewrite passes integer k instead of `'nn=k'` options string) and ERROR 3540 (UNINSTALL COMPONENT fails because the binlog thread holds a reference to `event_tracking_parse.myvector`).

**Architecture:** Fix 1 patches `rewriteMyVectorIsANN()` in `src/myvector.cc` to normalize a bare integer 4th arg to `'nn=N'` before generating the JSON_TABLE subquery. Fix 2 adds a `dynamic_loader_services_unload_notification` service implementation to `src/component_src/myvector_component.cc` that stops the binlog thread (releasing its server-side reference) before MySQL checks the reference count during UNINSTALL.

**Tech Stack:** C++17, MySQL 8.4 / 9.7 component API, Docker build scripts

---

## File Map

| File | Change |
|------|--------|
| `src/myvector.cc` | Add 10-line integer-k normalization block in `rewriteMyVectorIsANN()` after line 1334 |
| `src/component_src/myvector_component.cc` | Add `#include`, implement `myvector_unload_notify`, register `BEGIN_SERVICE_IMPLEMENTATION`, add to PROVIDES block |

No new files. No other files touched.

---

### Task 1: Fix ERROR 1210 — Normalize integer k in ANN query rewrite

**Spec:** `docs/superpowers/specs/2026-05-26-component-query-rewrite-fixes-design.md` — Fix 1 section

**Files:**
- Modify: `src/myvector.cc:1328-1348`

**Context:** `rewriteMyVectorIsANN()` lives at line 1298-1352. After `split(strparams, annparams)` (line 1329), if the 4th annparam is a plain integer (no `=`, no quotes), `myvector_ann_set` receives it as a SQL integer literal. MySQL sets `args->lengths[3]=0` for integer UDF args, so `myvector_ann_set` never parses `nn`. The fix replaces the trailing `,5` in `strparams` with `,'nn=5'` using `rfind(',')`.

- [ ] **Step 1: Observe the current failure**

  Build the 8.4 component and run the pre-release test to confirm ERROR 1210 exists:
  ```bash
  cd /path/to/myvector   # root of the worktree
  ./scripts/build-component-8.4-docker.sh mysql-8.4.8 dist/component-8.4
  ./scripts/pre-release-test.sh 8.4
  ```
  Expected output contains:
  ```
  ERROR 1210 (HY000) at line 2: Incorrect arguments to JSON_TABLE
  WARNING: ANN query failed (query rewrite may not be active)
  ```

- [ ] **Step 2: Add the integer-k normalization to `rewriteMyVectorIsANN()`**

  In `src/myvector.cc`, locate the block that starts at line 1326:
  ```cpp
      string strparams = newQuery.substr(spos, (epos - spos));

      vector<string> annparams;
      split(strparams, annparams);

      if (annparams.size() < 3) {
          error = true;
          break;
      }

      string idcolexpr = annparams[1];
  ```

  Replace with:
  ```cpp
      string strparams = newQuery.substr(spos, (epos - spos));

      vector<string> annparams;
      split(strparams, annparams);

      if (annparams.size() < 3) {
          error = true;
          break;
      }

      // If 4th arg is a bare integer (k neighbors), convert to 'nn=k' options string.
      // myvector_ann_set expects a string arg; MySQL sets lengths[3]=0 for integers,
      // causing the options to be silently skipped and JSON_TABLE to fail.
      if (annparams.size() == 4) {
          string last = annparams[3];
          size_t s = last.find_first_not_of(" \t\r\n");
          if (s != string::npos) last = last.substr(s);
          size_t e = last.find_last_not_of(" \t\r\n");
          if (e != string::npos) last = last.substr(0, e + 1);
          if (!last.empty() &&
              last.find_first_not_of("0123456789") == string::npos) {
              size_t last_comma = strparams.rfind(',');
              if (last_comma != string::npos)
                  strparams =
                      strparams.substr(0, last_comma + 1) + " 'nn=" + last + "'";
          }
      }

      string idcolexpr = annparams[1];
  ```

- [ ] **Step 3: Build the 8.4 component**

  ```bash
  ./scripts/build-component-8.4-docker.sh mysql-8.4.8 dist/component-8.4
  ```
  Expected: build completes with no errors. A warning about `myvector_display` returning a string literal (`-Wwrite-strings`) is pre-existing and acceptable.

- [ ] **Step 4: Verify ERROR 1210 is gone**

  ```bash
  ./scripts/pre-release-test.sh 8.4
  ```
  Expected: the ANN section now shows:
  ```
  PASS: ANN search (MYVECTOR_IS_ANN)
  ```
  NOT:
  ```
  ERROR 1210 (HY000) at line 2: Incorrect arguments to JSON_TABLE
  WARNING: ANN query failed (query rewrite may not be active)
  ```
  (The ERROR 3540 uninstall failure may still appear — that's Task 2.)

- [ ] **Step 5: Commit**

  ```bash
  git add src/myvector.cc
  git commit -m "fix: normalize integer k arg in MYVECTOR_IS_ANN query rewrite to 'nn=k'"
  ```

---

### Task 2: Fix ERROR 3540 — Stop binlog thread on component unload notification

**Spec:** `docs/superpowers/specs/2026-05-26-component-query-rewrite-fixes-design.md` — Fix 2 section

**Files:**
- Modify: `src/component_src/myvector_component.cc`

**Context:** During `UNINSTALL COMPONENT`, MySQL calls the `dynamic_loader_services_unload_notification` service **before** checking service reference counts. The binlog monitoring thread (started in `myvector_component_init()`) holds a reference to `event_tracking_parse.myvector` on the server's side because it ran setup SQL that triggered PREPARSE events. The thread then blocks in `mysql_binlog_fetch()` and never flushes the reference cache. By providing `dynamic_loader_services_unload_notification` and stopping the binlog thread inside `notify()`, the reference is released before MySQL's reference count check runs.

The service macro pattern comes from `mysql-server-mysql-8.4.8/components/reference_cache/component.cc` lines 250-262 and the notification service header at `mysql-server-mysql-8.4.8/include/mysql/components/services/dynamic_loader_service_notification.h`.

- [ ] **Step 1: Observe the current failure**

  If you haven't already, run:
  ```bash
  ./scripts/pre-release-test.sh 8.4
  ```
  Expected output near the end:
  ```
  === Uninstall ===
  ERROR 3540 (HY000) at line 1: Unregistration of service implementation
  'event_tracking_parse.myvector' provided by component 'file://myvector'
  failed during unloading of the component.
  ERROR: Phase 1 smoke failed for MySQL 8.4 — fix before proceeding
  ```

- [ ] **Step 2: Add the `#include` for the notification service header**

  In `src/component_src/myvector_component.cc`, the current includes are:
  ```cpp
  #include <mysql/components/component_implementation.h>
  #include <mysql/components/services/udf_metadata.h>
  #include <mysql/components/services/udf_registration.h>
  #include "mysql/components/util/event_tracking/event_tracking_parse_consumer_helper.h"
  #include "myvector.h"
  #include "myvector_binlog_service.h"
  #include "myvector_udf_service.h"
  ```

  Add one line after the udf_registration include:
  ```cpp
  #include <mysql/components/component_implementation.h>
  #include <mysql/components/services/udf_metadata.h>
  #include <mysql/components/services/udf_registration.h>
  #include <mysql/components/services/dynamic_loader_service_notification.h>
  #include "mysql/components/util/event_tracking/event_tracking_parse_consumer_helper.h"
  #include "myvector.h"
  #include "myvector_binlog_service.h"
  #include "myvector_udf_service.h"
  ```

- [ ] **Step 3: Add the notify function and service implementation before the PROVIDES block**

  The current `src/component_src/myvector_component.cc` at line 59 reads:
  ```cpp
  /* Define the service implementation struct (must be in same TU as PROVIDES) */
  IMPLEMENTS_SERVICE_EVENT_TRACKING_PARSE(myvector);
  ```

  Replace with:
  ```cpp
  /* Define the service implementation struct (must be in same TU as PROVIDES) */
  IMPLEMENTS_SERVICE_EVENT_TRACKING_PARSE(myvector);

  static mysql_service_status_t myvector_unload_notify(const char **services,
                                                       unsigned int count) {
    for (unsigned int i = 0; i < count; ++i) {
      if (strcmp(services[i], "event_tracking_parse.myvector") == 0) {
        myvector_component::get_binlog_service().stop_binlog_monitoring();
        // Server-side binlog dump THD teardown is asynchronous after mysql_close().
        // Sleep 5 s so the THD's events_cache_ is destroyed before dynamic_loader
        // checks the event_tracking_parse.myvector reference count.
        std::this_thread::sleep_for(std::chrono::milliseconds(5000));
        break;
      }
    }
    return false;
  }

  BEGIN_SERVICE_IMPLEMENTATION(myvector,
                               dynamic_loader_services_unload_notification)
  myvector_unload_notify END_SERVICE_IMPLEMENTATION();
  ```

- [ ] **Step 4: Add the service to the PROVIDES block**

  The current PROVIDES block in `src/component_src/myvector_component.cc` reads:
  ```cpp
  BEGIN_COMPONENT_PROVIDES(myvector)
  PROVIDES_SERVICE_EVENT_TRACKING_PARSE(myvector),
  END_COMPONENT_PROVIDES();
  ```

  Replace with:
  ```cpp
  BEGIN_COMPONENT_PROVIDES(myvector)
  PROVIDES_SERVICE_EVENT_TRACKING_PARSE(myvector),
  PROVIDES_SERVICE(myvector, dynamic_loader_services_unload_notification),
  END_COMPONENT_PROVIDES();
  ```

- [ ] **Step 5: Build the 8.4 component**

  ```bash
  ./scripts/build-component-8.4-docker.sh mysql-8.4.8 dist/component-8.4
  ```
  Expected: build completes with no errors.

- [ ] **Step 6: Verify ERROR 3540 is gone on 8.4**

  ```bash
  ./scripts/pre-release-test.sh 8.4
  ```
  Expected output near the end:
  ```
  === Uninstall ===
  myvector-smoke-component-XXXXX
  ```
  **No** `ERROR 3540` line. The smoke exit code should be 0.

  Full expected passing sections:
  ```
  PASS: ANN search (MYVECTOR_IS_ANN)
  PASS: MYVECTOR_INDEX_STATUS
  PASS: Index persist-and-reload (UNINSTALL → INSTALL → LOAD)
  PASS: Online updates: INSERT, UPDATE, DELETE
  PASS: Multi-column binlog: INSERT updated both vec1 and vec2 indexes
  ```
  And the suite should show `--- Phase 1 Smoke (8.4) --- PASSED`.

- [ ] **Step 7: Build the 9.7 component and verify**

  ```bash
  ./scripts/build-component-9.7-docker.sh mysql-9.7.0 dist/component-9.7
  ./scripts/pre-release-test.sh 9.7
  ```
  Expected: same pattern — no ERROR 3540, no ERROR 1210, exit 0.

- [ ] **Step 8: Run the full pre-release suite**

  ```bash
  ./scripts/pre-release-test.sh
  ```
  Expected: both 8.4 and 9.7 phases pass, final exit code 0.

- [ ] **Step 9: Commit**

  ```bash
  git add src/component_src/myvector_component.cc
  git commit -m "fix: provide unload notification service to drain binlog thread before UNINSTALL"
  ```
