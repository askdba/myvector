# Issue #79 Benchmark Baseline (Before Fix)

**Date:** 2026-03-13  
**Config:** rows=500, dim=768, runs=3  
**Image:** ghcr.io/askdba/myvector:mysql8.4 (prebuilt, no fix)

## Results

| Query | Run 1 | Run 2 | Run 3 | Avg (µs) |
|-------|-------|-------|-------|----------|
| Nested myvector_construct | 99694 | 180259 | 105390 | 128447 |
| Literal 0x... | 101686 | 103537 | 94969 | 100064 |

**Ratio (nested/literal): 1.28x**

Nested is ~28% slower. With more rows (e.g. 54k as in the original issue), the gap would widen significantly.

## Raw Output

```text
=== Issue #79 Benchmark (rows=500, dim=768, runs=3) ===
--- Nested myvector_construct (issue #79 pattern) ---
  Run 1: 99694 µs
  Run 2: 180259 µs
  Run 3: 105390 µs

--- Precomputed 0x literal (baseline) ---
  Run 1: 101686 µs
  Run 2: 103537 µs
  Run 3: 94969 µs

=== Results ===
Nested myvector_construct: 128447 µs (avg)
Literal 0x...:             100064 µs (avg)
Ratio (nested/literal):   1.28x
```
