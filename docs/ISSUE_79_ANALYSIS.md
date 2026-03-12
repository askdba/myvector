# Issue #79: myvector_distance with myvector_construct too slow

## Summary

When `myvector_construct('...')` is nested inside `myvector_distance(col, myvector_construct('...'), 'Cosine')`, MySQL invokes `myvector_construct` **once per row** instead of once per query. For 54k rows with 768-dim vectors, this causes ~12s vs <1s.

## Root Cause

- MySQL's `udf_handler::get_arguments()` is called every time the parent UDF (`myvector_distance`) is evaluated
- For each argument, it calls `args[i]->val_str()` → evaluates `myvector_construct` per row
- Constant UDF sub-expressions are **not cached** when used as arguments to another UDF

## Workaround

Precompute the vector and pass as hex literal:

```php
$vec = getValue("SELECT myvector_construct('{$searchTextVector}') AS v");
$hex = bin2hex($vec);
// Use 0x{$hex} in the distance query
```

## Recommended Fix

In `myvector_construct_init()`: when `args->args[0] != NULL` (constant argument), run the conversion once and cache the result. In `myvector_construct()`, return the cached result when present.

## Gaps (as of this analysis)

1. **Fix not implemented** – no constant-arg caching in plugin or component
2. **Component not updated** – `myvector_udf_service.cc` has no counter or caching
3. **MTR test never run** – `udf_construct_per_row.test` requires plugin build; mtr sandbox had build issues
4. **Debug UDF not in component** – only plugin has `myvector_debug_construct_calls`
5. **No documentation** – workaround not in README
6. **Debug code in production** – counter/UDF should be `#ifdef` guarded

## Benchmarking

- **Before fix:** `SELECT myvector_distance(v, myvector_construct('...'), 'Cosine')` → ~12s for 54k rows
- **After fix:** same query → should match literal (~1s)
- **Validation:** `myvector_debug_construct_calls()` should return 1 (not N) after fix
- **Script:** `scripts/benchmark-issue79.sh` (to be created)

## Files Added/Modified

- `src/myvector.cc` – debug counter + `myvector_debug_construct_calls` UDF
- `mysql-test/suite/myvector/t/udf_construct_per_row.test`
- `mysql-test/suite/myvector/r/udf_construct_per_row.result`
- `scripts/test-docker-image.sh` – fast test with prebuilt image
- `scripts/run-mtr-sandbox.sh` – full mtr in Docker (build issues to resolve)
