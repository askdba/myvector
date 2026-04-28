# myvector_log.h Logging Abstraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce `include/myvector_log.h` so `myvector.cc` and `myvector_udf_service.cc` use a unified logging abstraction instead of plugin-specific `my_plugin_log_message(&gplugin, ...)` calls, eliminating the current `#ifdef MYVECTOR_COMPONENT_BUILD` logging workarounds.

**Architecture:** A single header defines `MYVEC_LOG_DEBUG/INFO/ERROR/WARN` macros with two implementations — plugin build wires to `my_plugin_log_message`, component build wires to `fprintf(stderr, ...)`. `myvector.cc` replaces all ~45 logging call sites with the new macros and removes its `#ifdef` logging block. `myvector_udf_service.cc` drops its local workaround macros in favour of the header.

**Tech Stack:** C++17, MySQL plugin/component SDK, CMake (`MYVECTOR_COMPONENT_BUILD` already defined for component target)

**Spec:** `docs/superpowers/specs/2026-04-21-myvector-log-abstraction-design.md`

---

## File Map

| Action | File | Change |
|---|---|---|
| Create | `include/myvector_log.h` | New logging abstraction header |
| Modify | `src/myvector.cc` lines 70–92 | Replace `#ifdef` logging block with `#include "myvector_log.h"` |
| Modify | `src/myvector.cc` lines 132–154 | Collapse `SET_UDF_ERROR_AND_RETURN` to single definition |
| Modify | `src/myvector.cc` (body) | ~45 call-site substitutions |
| Modify | `src/component_src/myvector_udf_service.cc` | Remove workaround block, add include |

---

## Task 1: Create `include/myvector_log.h`

**Files:**
- Create: `include/myvector_log.h`

- [ ] **Step 1: Create the header**

```cpp
#pragma once
// Logging abstraction for myvector — works in both plugin and component builds.
// Plugin builds:    wraps my_plugin_log_message via extern MYSQL_PLUGIN gplugin.
// Component builds: writes directly to stderr.

#ifdef MYVECTOR_COMPONENT_BUILD
#  include <cstdio>
#  define MYVEC_LOG_DEBUG(fmt, ...) fprintf(stderr, "[MYVEC DBG] " fmt "\n", ##__VA_ARGS__)
#  define MYVEC_LOG_INFO(fmt, ...)  fprintf(stderr, "[MYVEC INF] " fmt "\n", ##__VA_ARGS__)
#  define MYVEC_LOG_ERROR(fmt, ...) fprintf(stderr, "[MYVEC ERR] " fmt "\n", ##__VA_ARGS__)
#  define MYVEC_LOG_WARN(fmt, ...)  fprintf(stderr, "[MYVEC WRN] " fmt "\n", ##__VA_ARGS__)
#else
#  include "mysql/service_my_plugin_log.h"
   extern MYSQL_PLUGIN gplugin;
#  define MYVEC_LOG_DEBUG(...) my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL, __VA_ARGS__)
#  define MYVEC_LOG_INFO(...)  my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL, __VA_ARGS__)
#  define MYVEC_LOG_ERROR(...) my_plugin_log_message(&gplugin, MY_ERROR_LEVEL,       __VA_ARGS__)
#  define MYVEC_LOG_WARN(...)  my_plugin_log_message(&gplugin, MY_WARNING_LEVEL,     __VA_ARGS__)
#endif
```

- [ ] **Step 2: Commit**

```bash
git add include/myvector_log.h
git commit -m "feat(logging): add myvector_log.h abstraction header"
```

---

## Task 2: Clean up `myvector.cc` header section

**Files:**
- Modify: `src/myvector.cc` lines 70–154

This task removes the two `#ifdef` logging workaround blocks and replaces them with
the new header and a single `SET_UDF_ERROR_AND_RETURN` definition.

- [ ] **Step 1: Replace the first `#ifdef` logging block (lines 70–92)**

Find this exact block in `src/myvector.cc`:

```cpp
#ifndef MYVECTOR_COMPONENT_BUILD
#include "mysql/service_my_plugin_log.h"
extern MYSQL_PLUGIN gplugin;

#define debug_print(...)                                                       \
    my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL, __VA_ARGS__)
#define info_print(...)                                                        \
    my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL, __VA_ARGS__)
#define error_print(...)                                                       \
    my_plugin_log_message(&gplugin, MY_ERROR_LEVEL, __VA_ARGS__)
#define warning_print(...)                                                     \
    my_plugin_log_message(&gplugin, MY_WARNING_LEVEL, __VA_ARGS__)
#else
// Component build: redirect all logging to stderr; gplugin is unused.
#include <cstdio>
[[maybe_unused]] static void* gplugin = nullptr;
// Redirect direct my_plugin_log_message() calls scattered throughout this TU
#define my_plugin_log_message(plugin, level, ...) fprintf(stderr, "[MYVEC] " __VA_ARGS__)
#define debug_print(fmt, ...) fprintf(stderr, "[MYVEC DBG] " fmt "\n", ##__VA_ARGS__)
#define info_print(fmt, ...)  fprintf(stderr, "[MYVEC INF] " fmt "\n", ##__VA_ARGS__)
#define error_print(fmt, ...) fprintf(stderr, "[MYVEC ERR] " fmt "\n", ##__VA_ARGS__)
#define warning_print(fmt, ...) fprintf(stderr, "[MYVEC WRN] " fmt "\n", ##__VA_ARGS__)
#endif
```

Replace with:

```cpp
#include "myvector_log.h"
```

- [ ] **Step 2: Collapse `SET_UDF_ERROR_AND_RETURN` (lines ~132–154)**

Find this block:

```cpp
#ifndef MYVECTOR_COMPONENT_BUILD
extern REQUIRES_SERVICE_PLACEHOLDER(mysql_udf_metadata);
extern REQUIRES_SERVICE_PLACEHOLDER(mysql_string_converter);
extern REQUIRES_SERVICE_PLACEHOLDER(mysql_string_factory);

#include <mysql/service_plugin_registry.h>

#define SET_UDF_ERROR_AND_RETURN(...)                                          \
    {                                                                          \
        my_plugin_log_message(&gplugin, MY_ERROR_LEVEL, __VA_ARGS__);          \
        *error = 1;                                                            \
        return (result);                                                       \
    }

extern my_service<SERVICE_TYPE(mysql_udf_metadata)>* h_udf_metadata_service;
#else
#define SET_UDF_ERROR_AND_RETURN(...)                                          \
    {                                                                          \
        error_print(__VA_ARGS__);                                              \
        *error = 1;                                                            \
        return (result);                                                       \
    }
#endif
```

Replace with:

```cpp
#ifndef MYVECTOR_COMPONENT_BUILD
extern REQUIRES_SERVICE_PLACEHOLDER(mysql_udf_metadata);
extern REQUIRES_SERVICE_PLACEHOLDER(mysql_string_converter);
extern REQUIRES_SERVICE_PLACEHOLDER(mysql_string_factory);

#include <mysql/service_plugin_registry.h>

extern my_service<SERVICE_TYPE(mysql_udf_metadata)>* h_udf_metadata_service;
#endif

#define SET_UDF_ERROR_AND_RETURN(...)                                          \
    {                                                                          \
        MYVEC_LOG_ERROR(__VA_ARGS__);                                          \
        *error = 1;                                                            \
        return (result);                                                       \
    }
```

- [ ] **Step 3: Verify plugin build compiles**

```bash
make clean && make
```

Expected: build succeeds with no errors. Warnings about unused variables are acceptable; errors are not.

- [ ] **Step 4: Commit**

```bash
git add src/myvector.cc
git commit -m "refactor(logging): replace myvector.cc ifdef logging block with myvector_log.h"
```

---

## Task 3: Replace logging call sites in `myvector.cc` body

**Files:**
- Modify: `src/myvector.cc` (function body call sites only — not definitions/guards)

There are two categories of call site to update:

**Category A — shorthand macros** (`debug_print`, `info_print`, `error_print`, `warning_print`):

Rename the macro at each call site:

| Old | New |
|---|---|
| `debug_print(` | `MYVEC_LOG_DEBUG(` |
| `info_print(` | `MYVEC_LOG_INFO(` |
| `error_print(` | `MYVEC_LOG_ERROR(` |
| `warning_print(` | `MYVEC_LOG_WARN(` |

**Category B — direct calls** (`my_plugin_log_message(&gplugin, LEVEL, ...)`):

Each direct call is multi-line and uses one of three levels. Replace by level:

| Level argument | New macro |
|---|---|
| `MY_INFORMATION_LEVEL` | `MYVEC_LOG_INFO` |
| `MY_ERROR_LEVEL` | `MYVEC_LOG_ERROR` |
| `MY_WARNING_LEVEL` | `MYVEC_LOG_WARN` |

Example — before:
```cpp
my_plugin_log_message(&gplugin,
                      MY_WARNING_LEVEL,
                      "KNN Memory Index (%s) - Save Index to disk is no-op",
                      m_name.c_str());
```

After:
```cpp
MYVEC_LOG_WARN("KNN Memory Index (%s) - Save Index to disk is no-op",
               m_name.c_str());
```

Example — before:
```cpp
my_plugin_log_message(
    &gplugin,
    MY_ERROR_LEVEL,
    "MyVector unknown index type for %s options = %s, using KNN",
    col.c_str(), options.c_str());
```

After:
```cpp
MYVEC_LOG_ERROR("MyVector unknown index type for %s options = %s, using KNN",
                col.c_str(), options.c_str());
```

- [ ] **Step 1: Find all Category A call sites**

```bash
grep -n "debug_print\|info_print\|error_print\|warning_print" src/myvector.cc | grep -v "^[0-9]*:#"
```

Expected: ~20 lines. All are function-body calls — none should be `#define` lines (those were removed in Task 2).

- [ ] **Step 2: Replace all Category A call sites**

Using Edit tool, rename each occurrence. The argument list is unchanged — only the macro name changes:

- `debug_print(` → `MYVEC_LOG_DEBUG(`
- `info_print(` → `MYVEC_LOG_INFO(`
- `error_print(` → `MYVEC_LOG_ERROR(`
- `warning_print(` → `MYVEC_LOG_WARN(`

- [ ] **Step 3: Find all Category B call sites**

```bash
grep -n "my_plugin_log_message" src/myvector.cc | grep -v "^[0-9]*:#"
```

Expected: ~26 lines (some calls span multiple lines so count of lines > count of calls).

- [ ] **Step 4: Replace all Category B call sites**

For each call: remove `my_plugin_log_message(\n    &gplugin,\n    MY_*_LEVEL,` and replace with the corresponding `MYVEC_LOG_*(` keeping all format string and variadic arguments unchanged.

- [ ] **Step 5: Verify no old call patterns remain**

```bash
grep -n "my_plugin_log_message\|debug_print\|info_print\|error_print\|warning_print" src/myvector.cc | grep -v "^[0-9]*:#"
```

Expected: zero lines. Any remaining hit is a missed call site — fix it.

- [ ] **Step 6: Verify plugin build compiles**

```bash
make clean && make
```

Expected: success.

- [ ] **Step 7: Commit**

```bash
git add src/myvector.cc
git commit -m "refactor(logging): replace all myvector.cc logging call sites with MYVEC_LOG_*"
```

---

## Task 4: Update `myvector_udf_service.cc`

**Files:**
- Modify: `src/component_src/myvector_udf_service.cc`

- [ ] **Step 1: Remove the workaround macro block**

Find this block near the top of the file (appears between `using std::string;` and `#include "hnswdisk.h"`):

```cpp
using std::string;
#ifndef debug_print
#include <cstdio>
#define debug_print(fmt, ...) fprintf(stderr, "[MYVEC DBG] " fmt "\n", ##__VA_ARGS__)
#define info_print(fmt, ...)  fprintf(stderr, "[MYVEC INF] " fmt "\n", ##__VA_ARGS__)
#define error_print(fmt, ...) fprintf(stderr, "[MYVEC ERR] " fmt "\n", ##__VA_ARGS__)
#define warning_print(fmt, ...) fprintf(stderr, "[MYVEC WRN] " fmt "\n", ##__VA_ARGS__)
#endif
```

Replace with:

```cpp
using std::string;
#include "myvector_log.h"
```

- [ ] **Step 2: Check for any remaining call sites in this file**

```bash
grep -n "debug_print\|info_print\|error_print\|warning_print\|my_plugin_log_message" \
  src/component_src/myvector_udf_service.cc | grep -v "^[0-9]*:#"
```

Expected: zero lines. If any appear, rename them to `MYVEC_LOG_*` following the same pattern as Task 3.

- [ ] **Step 3: Commit**

```bash
git add src/component_src/myvector_udf_service.cc
git commit -m "refactor(logging): replace myvector_udf_service.cc workaround with myvector_log.h"
```

---

## Task 5: Push and verify CI

**Files:** none — CI verification only

- [ ] **Step 1: Push branch**

```bash
git push origin feature/myvector-component
```

- [ ] **Step 2: Confirm plugin jobs pass**

Check GitHub Actions. All of these must be green:
- `build (mysql-8.0.40, 8.0)` ✅
- `build (mysql-8.4.8, 8.4)` ✅
- `build (mysql-9.0.0, 9.0)` ✅
- `test (8.0)` ✅
- `test (8.4)` ✅
- `test (9.0)` ✅
- `lint` ✅

A regression in any plugin job means a call site was changed incorrectly — read the build log, find the compile error, fix it.

- [ ] **Step 3: Confirm component jobs pass**

- `build-component (mysql-8.4.8, 8.4)` ✅
- `test-component (8.4)` ✅ — this is the primary target; no `undefined symbol` at dlopen

`build-component-9-6` and `test-component-9-6` have `continue-on-error: true` and may fail due to transient infrastructure issues — this is expected and acceptable.

- [ ] **Step 4: If `test-component (8.4)` fails with a new `undefined symbol`**

Run:

```bash
grep -n "extern " src/component_src/myvector_udf_service.cc src/myvector.cc | \
  grep -v "MYVECTOR_COMPONENT_BUILD\|ifndef\|endif\|ifdef\|//"
```

Any `extern` that references a symbol defined only in `src/myvector_plugin.cc` is the culprit.
Fix: either guard it with `#ifndef MYVECTOR_COMPONENT_BUILD` (if it's plugin-only) or define it locally in the component TU (if it's shared data with the wrong linkage).
