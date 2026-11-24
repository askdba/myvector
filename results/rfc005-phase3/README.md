# RFC-005 Phase 3 result files

Raw output of `scripts/bench-innodb-storage.py`, read by
`scripts/bench-innodb-storage-report.py`; the report is
`docs/RFC-005-PHASE3-MEASUREMENTS.md`.

- `t1-…`, `t2-…`, `t3-…`: the storage comparison, one file per run (both backends).
- `…-stall.json`: the steady-insert step of the run of the same name, measured in
  a separate run after its probe was corrected (marker rows with scattered
  vectors, found among the ten nearest results). The report takes that step from
  these files; the `stall` entries inside the main files are from the earlier
  probe and should be ignored.
- `sweep-<tier>-v<vector chunk bytes>-l<link chunk bytes>.json`: `storage=innodb`
  only, with those chunk-size settings.

Component built from `main` at a4a6c0a. Host: OCI Ampere A1, 16 cores, 94 GB.
