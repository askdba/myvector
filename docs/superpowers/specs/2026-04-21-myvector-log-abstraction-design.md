# Design: myvector_log.h Logging Abstraction

**Date:** 2026-04-21  
**Branch:** feature/myvector-component  
**Status:** Approved

---

## Problem

`myvector.cc` is compiled into both the plugin `.so` and the component `.so`. The file uses
`my_plugin_log_message(&gplugin, LEVEL, ...)` for all logging — a plugin-framework call that
requires `gplugin` (defined in `myvector_plugin.cc`, which is not part of the component build).

This entanglement caused a chain of `undefined symbol` errors at `INSTALL COMPONENT` time,
fixed reactively one symbol at a time over several CI cycles. The current workaround
(`#define my_plugin_log_message(...)` redirect + `[[maybe_unused]] static void* gplugin` stub
inside `#ifdef MYVECTOR_COMPONENT_BUILD`) is functional but leaves `myvector.cc` with
`#ifdef` soup and hidden intent.

Root cause: logging is the only deep entanglement between the shared core logic in
`myvector.cc` and the plugin infrastructure. Everything else (`REQUIRES_SERVICE_PLACEHOLDER`,
`h_udf_metadata_service`) is already correctly guarded and isolated to plugin-only UDF init
functions that are never called in the component path.

---

## Goal

Introduce a single logging abstraction header so that:

- `myvector.cc` contains no build-context-specific logging macros in its body.
- Plugin and component builds produce correct logging behaviour automatically via the header.
- `myvector_udf_service.cc` discards its local workaround macros in favour of the same header.
- The `#ifdef MYVECTOR_COMPONENT_BUILD` blocks remaining in `myvector.cc` shrink to only
  the four genuinely plugin-only items.

---

## Design

### 1. New file: `include/myvector_log.h`

Defines four logging macros with two build-path implementations:

```
MYVEC_LOG_DEBUG(fmt, ...)
MYVEC_LOG_INFO(fmt, ...)
MYVEC_LOG_ERROR(fmt, ...)
MYVEC_LOG_WARN(fmt, ...)
```

**Plugin build** (`MYVECTOR_COMPONENT_BUILD` not defined):

```cpp
#include "mysql/service_my_plugin_log.h"
extern MYSQL_PLUGIN gplugin;

#define MYVEC_LOG_DEBUG(...) my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL, __VA_ARGS__)
#define MYVEC_LOG_INFO(...)  my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL, __VA_ARGS__)
#define MYVEC_LOG_ERROR(...) my_plugin_log_message(&gplugin, MY_ERROR_LEVEL,       __VA_ARGS__)
#define MYVEC_LOG_WARN(...)  my_plugin_log_message(&gplugin, MY_WARNING_LEVEL,     __VA_ARGS__)
```

**Component build** (`MYVECTOR_COMPONENT_BUILD` defined):

```cpp
#include <cstdio>

#define MYVEC_LOG_DEBUG(fmt, ...) fprintf(stderr, "[MYVEC DBG] " fmt "\n", ##__VA_ARGS__)
#define MYVEC_LOG_INFO(fmt, ...)  fprintf(stderr, "[MYVEC INF] " fmt "\n", ##__VA_ARGS__)
#define MYVEC_LOG_ERROR(fmt, ...) fprintf(stderr, "[MYVEC ERR] " fmt "\n", ##__VA_ARGS__)
#define MYVEC_LOG_WARN(fmt, ...)  fprintf(stderr, "[MYVEC WRN] " fmt "\n", ##__VA_ARGS__)
```

The header owns the `extern MYSQL_PLUGIN gplugin` declaration for the plugin path. No TU
needs to repeat it.

### 2. Changes to `src/myvector.cc`

**Remove:**
- `#include "mysql/service_my_plugin_log.h"` from the unconditional includes block.
- The entire `#ifndef MYVECTOR_COMPONENT_BUILD` / `#else` / `#endif` block that currently
  contains `extern MYSQL_PLUGIN gplugin`, the `debug_print`/`info_print`/`error_print`/
  `warning_print` macro definitions, `[[maybe_unused]] static void* gplugin = nullptr`,
  and `#define my_plugin_log_message(plugin, level, ...) fprintf(stderr, ...)`.
- The `#ifdef MYVECTOR_COMPONENT_BUILD` split of `SET_UDF_ERROR_AND_RETURN` (both variants
  become identical once logging is abstracted).

**Add:**
- `#include "myvector_log.h"` in place of the removed block.

**Replace (~30 call sites in function bodies):**

| Before | After |
|---|---|
| `my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL, ...)` | `MYVEC_LOG_INFO(...)` |
| `my_plugin_log_message(&gplugin, MY_ERROR_LEVEL, ...)` | `MYVEC_LOG_ERROR(...)` |
| `my_plugin_log_message(&gplugin, MY_WARNING_LEVEL, ...)` | `MYVEC_LOG_WARN(...)` |
| `debug_print(...)` | `MYVEC_LOG_DEBUG(...)` |
| `info_print(...)` | `MYVEC_LOG_INFO(...)` |
| `error_print(...)` | `MYVEC_LOG_ERROR(...)` |
| `warning_print(...)` | `MYVEC_LOG_WARN(...)` |

**`SET_UDF_ERROR_AND_RETURN` after change (single definition, no `#ifdef`):**

```cpp
#define SET_UDF_ERROR_AND_RETURN(...)   \
    {                                   \
        MYVEC_LOG_ERROR(__VA_ARGS__);   \
        *error = 1;                     \
        return (result);                \
    }
```

**`#ifndef MYVECTOR_COMPONENT_BUILD` blocks that remain (correct and expected):**

1. `extern REQUIRES_SERVICE_PLACEHOLDER(mysql_udf_metadata/string_converter/string_factory)`
2. `#include <mysql/service_plugin_registry.h>`
3. `extern my_service<SERVICE_TYPE(mysql_udf_metadata)>* h_udf_metadata_service`
4. Two `(*h_udf_metadata_service)->result_set(initid, "charset", latin1)` call sites

These are plugin-only UDF initialisation details that have no component equivalent.
They are in functions (`myvector_ann_set_init`, `myvector_display_init`) that are compiled
into the component `.so` but never registered or called there.

### 3. Changes to `src/component_src/myvector_udf_service.cc`

**Remove** the existing workaround block:
```cpp
using std::string;
#ifndef debug_print
#include <cstdio>
#define debug_print(fmt, ...)   fprintf(stderr, "[MYVEC DBG] " fmt "\n", ##__VA_ARGS__)
#define info_print(fmt, ...)    fprintf(stderr, "[MYVEC INF] " fmt "\n", ##__VA_ARGS__)
#define error_print(fmt, ...)   fprintf(stderr, "[MYVEC ERR] " fmt "\n", ##__VA_ARGS__)
#define warning_print(fmt, ...) fprintf(stderr, "[MYVEC WRN] " fmt "\n", ##__VA_ARGS__)
#endif
```

**Add** `#include "myvector_log.h"` before `#include "hnswdisk.h"`.

Replace any `debug_print`/`info_print`/`error_print`/`warning_print` call sites in this
file with `MYVEC_LOG_*`.

### 4. What does NOT change

- `CMakeLists.txt` — `MYVECTOR_COMPONENT_BUILD` is already defined for the component target.
- `src/component_src/myvector_binlog_service.cc` — uses `fprintf` directly; out of scope.
  Can adopt `myvector_log.h` in a future pass if consistency is desired.
- All plugin runtime behaviour — `MYVEC_LOG_*` expands to the same `my_plugin_log_message`
  calls as before.
- No logic changes anywhere; this is a pure naming/abstraction refactor.

---

## Verification

| Check | How |
|---|---|
| Plugin builds stay green | CI: `build (8.0/8.4/9.0)` + `test (8.0/8.4/9.0)` |
| Component build compiles | CI: `build-component (8.4)` |
| Component installs cleanly | CI: `test-component (8.4)` — no `undefined symbol` at dlopen |
| Lint passes | CI: `lint` job |

No new tests are required. The existing CI matrix is the validation gate.

---

## Out of Scope

- Migrating `myvector_binlog_service.cc` to `myvector_log.h` (future work).
- Extracting a `myvector_core.cc` to fully decouple plugin and shared code (Approach 3,
  deferred).
- Component-native logging via MySQL error log service (post-migration improvement).
