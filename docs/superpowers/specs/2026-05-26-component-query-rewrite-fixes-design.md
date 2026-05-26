# Component Query Rewrite Fixes Design

## Summary

Two targeted fixes for the `fix/component-query-rewrite` branch to resolve the two remaining
failures in the pre-release smoke test suite:

1. **ERROR 1210** (`Incorrect arguments to JSON_TABLE`): `MYVECTOR_IS_ANN` passes a plain integer
   `k` as its 4th argument, but `myvector_ann_set` expects an options string like `'nn=5'`.
2. **ERROR 3540** (`Unregistration of service implementation failed`): `UNINSTALL COMPONENT` fails
   because the binlog monitoring thread holds a reference to `event_tracking_parse.myvector` that
   is never released before the reference-count check.

Both fixes are surgical — no new abstractions, no new files, no behavior changes outside the
specific failure paths.

---

## Fix 1 — ANN Query Rewrite: 4th-Arg Integer Normalization

### Problem

`rewriteMyVectorIsANN()` in `src/myvector.cc` rewrites:
```sql
WHERE MYVECTOR_IS_ANN('db.tbl.col', 'id', vec_expr, 5)
```
to:
```sql
WHERE id IN (SELECT myvecid FROM JSON_TABLE(
    myvector_ann_set('db.tbl.col', 'id', vec_expr, 5), ...))
```

`myvector_ann_set` (in `src/component_src/myvector_udf_service.cc`, lines 156-226) only parses
the 4th arg when `args->lengths[3] > 0`. MySQL sets `lengths[3] = 0` for integer UDF arguments.
The options string is never built, so `myvector_ann_set` uses a default `nn` value (or 0), which
causes the JSON_TABLE call to fail with ERROR 1210.

### Fix

In `rewriteMyVectorIsANN()`, after splitting `strparams` into `annparams`, detect a plain integer
4th argument and replace it in `strparams` with a properly quoted `'nn=N'` options string:

```cpp
// After: vector<string> annparams = split(strparams, ",");
if (annparams.size() == 4) {
    string last = annparams[3];
    // strip leading/trailing whitespace
    size_t s = last.find_first_not_of(" \t\r\n");
    if (s != string::npos) last = last.substr(s);
    size_t e = last.find_last_not_of(" \t\r\n");
    if (e != string::npos) last = last.substr(0, e + 1);
    // if it's a bare integer (no quotes, no = sign), convert to 'nn=N'
    if (!last.empty() &&
        last.find_first_not_of("0123456789") == string::npos) {
        size_t last_comma = strparams.rfind(',');
        if (last_comma != string::npos) {
            strparams = strparams.substr(0, last_comma + 1) + " 'nn=" + last + "'";
        }
    }
}
```

**Why `rfind(',')` is safe:** The first three args may contain subquery expressions with commas
(e.g. `(SELECT wordvec FROM words50d WHERE word='the')`). The `strparams` raw string preserves
those. `rfind(',')` finds the last top-level comma which is always the delimiter before the
integer `k` arg.

**Scope:** Only triggered when: (a) exactly 4 annparams, AND (b) the 4th is all decimal digits.
Quoted strings like `'nn=5,ef=200'` pass through unchanged.

**Files changed:** `src/myvector.cc` only.

---

## Fix 2 — Clean Component Uninstall: Unload Notification Service

### Problem

When `UNINSTALL COMPONENT 'file://myvector'` is called:

1. MySQL calls `unload_do_lock_provided_services()` which fires the
   `dynamic_loader_services_unload_notification` before acquiring the write lock.
2. MySQL calls `unload_do_check_provided_services_reference_count()` — fails if any service
   reference count > 0.
3. MySQL calls `unload_do_deinitialize_components()` — calls our `myvector_component_deinit()`.

The binlog monitoring thread (started in `myvector_component_init()`) creates a MySQL client
connection and runs setup SQL (`SET @master_binlog_checksum = 'NONE'`, etc.). This triggers
PREPARSE events on the server side. The server-side THD acquires a reference to
`event_tracking_parse.myvector` via `reference_caching`. The thread then enters
`mysql_binlog_fetch()` (COM_BINLOG_DUMP) and never runs more SQL — so the reference cache is
never flushed, and the reference count stays > 0 at step 2.

MySQL's pre-unload notification (step 1) only flushes `current_thd` (the UNINSTALL connection),
not the binlog thread's server-side THD.

### Fix

Provide `dynamic_loader_services_unload_notification` in the component. The `notify()` handler
checks if `event_tracking_parse.myvector` is in the services-being-unloaded list, and if so
calls `stop_binlog_monitoring()` synchronously before returning.

`stop_binlog_monitoring()` closes the MySQL client connection, which causes the server-side THD
to be destroyed and its reference cache to be freed — releasing the
`event_tracking_parse.myvector` reference.

This runs **before** step 2 (the reference count check), so the count drops to 0 and uninstall
succeeds.

**Why it's safe to provide this service:** `dynamic_loader.cc` line 1169 comment confirms:
> "Otherwise, a component that provides dynamic_loader_services_unload_notification can never be
> unloaded."
The scoped handle that calls `notify()` is released **before** the write lock is acquired and
before the reference count check — no deadlock.

### Implementation

**In `src/component_src/myvector_component.cc`:**

```cpp
// 1. Free function implementing the service
static bool myvector_unload_notify(const char **services,
                                   unsigned int count) {
  for (unsigned int i = 0; i < count; ++i) {
    if (strcmp(services[i], "event_tracking_parse.myvector") == 0) {
      myvector_component::get_binlog_service().stop_binlog_monitoring();
      break;
    }
  }
  return false;
}

// 2. Service implementation struct
BEGIN_SERVICE_IMPLEMENTATION(myvector,
                             dynamic_loader_services_unload_notification)
myvector_unload_notify END_SERVICE_IMPLEMENTATION();
```

Add to `BEGIN_COMPONENT_PROVIDES`:
```cpp
PROVIDES_SERVICE(myvector, dynamic_loader_services_unload_notification),
```

No new `REQUIRES_SERVICE` is needed — we are providing this service, not consuming it.

**Files changed:** `src/component_src/myvector_component.cc` only.

---

## Testing

After implementing both fixes:

1. Build 8.4 component: `./scripts/build-component-8.4-docker.sh mysql-8.4.8 dist/component-8.4`
2. Build 9.7 component: `./scripts/build-component-9.7-docker.sh mysql-9.7.0 dist/component-9.7`
3. Run pre-release suite: `./scripts/pre-release-test.sh`

Success criteria:
- ANN search section: `PASS: ANN search (MYVECTOR_IS_ANN)` (not `WARNING: ANN query failed`)
- Uninstall section: no `ERROR 3540`
- Exit code 0 from `pre-release-test.sh`

---

## Files Changed

| File | Change |
|------|--------|
| `src/myvector.cc` | Add integer-k normalization in `rewriteMyVectorIsANN()` |
| `src/component_src/myvector_component.cc` | Add `dynamic_loader_services_unload_notification` service implementation and PROVIDES entry |
