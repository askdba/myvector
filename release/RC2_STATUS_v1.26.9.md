# RC2 Status - v1.26.9

RC tag: `v1.26.9-rc2` (not yet pushed)
Source under test: `d0c0679` (main after PRs #117, #120-#123, #125, #126; the RC branch adds release docs only)
Final release tag: `v1.26.9`
Date: TBD

Gate policy: MySQL 26.7 is **blocking** for this RC.

Why rc2: the rc1 gate, smoke tests and benchmarks silently exercised KNN (the scripts' comment
format never parsed as HNSW), and a real HNSW build crashed mysqld on the component build.
The published rc1 component images predate the fix.

## Local pre-release gate (artifacts built from `d0c0679`)

_TBD_

## CI

_TBD_

## Release workflow

_TBD_

## Docker publish (automatic dispatch from `release.yml`, first real test of #122)

_TBD_

## Published-image smoke

_TBD_

## HNSW on published component images

_TBD_

## Benchmark (first run compared against the promoted baselines)

Baselines and how to read them: [`BENCHMARK_v1.26.9.md`](BENCHMARK_v1.26.9.md).

_Tag-run comparison: TBD_

## Known issues / follow-ups

- #119 `dist=cosine` (lower case) silently becomes L2.
- #124 benchmark QPS/latency are dominated by `docker exec` overhead.

## Blockers

_None recorded._

## Go / No-Go

_TBD_
