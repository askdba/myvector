# RC2 Status - v1.26.5

## 1) Release metadata

- Release version: `v1.26.5`
- Candidate tag: `v1.26.5-rc2`
- Previous candidate: `v1.26.5-rc1`
- Release manager: `askdba`
- Date opened: `2026-05-04`
- Last updated: `2026-05-04`

## 2) Candidate commit and branch

- Working branch: `main`
- **RC2 validation baseline commit:** `99c3f24a82b4309b223968b712a6be62fe8fd26a`
- Scope range: `v1.26.5-rc1..HEAD` on `main`.

## 3) RC1 → RC2 delta

| Commit | Description |
| :----- | :---------- |
| `7c46cc7` | fix: static-link libstdc++/libgcc in plugin build for OracleLinux 8 compat |
| `99c3f24` | test: expand smoke-component.sh with broader coverage |

**Root cause of RC1 docker-publish failure:** Plugin `.so` compiled with GCC 10
on Ubuntu 22.04 required `GLIBCXX_3.4.30`, absent from OracleLinux 8. Fixed by
adding `-static-libstdc++ -static-libgcc` to the `MYSQL_ADD_PLUGIN` CMake target.

## 4) Validation status

### Build and CI

- CI status: Green (MyVector CI run id=25335355491).
- Release workflow (`release.yml`): **Success** (run id=25335363538, tag v1.26.5-rc2).
- Docker publish (`docker-publish.yml`): **Success** (run id=25338779931, all 3 matrix jobs).
- Lint status: Green on main.

### Functional checks

- Plugin smoke (`smoke-published-images.sh`): not yet run.
- Component smoke (`smoke-component.sh`): not yet run.
- Online index flow: optional for RC2.
- Regression: CI coverage pre-tag is green.

### Performance smoke

- Startup sanity: pending.
- Query latency: pending (README smoke).
- Memory: not measured for RC2.

## 5) GHCR images smoke-tested

Images published 2026-05-04 via run id=25338779931.

| Tag | Image digest (pulled) |
| :-- | :-- |
| `mysql8.0` | `sha256:fcf32086416e0876394f18533611ea7fb6439e21ef07dfae7a527cec2dd1ddcb` |
| `mysql8.4` | `sha256:b2072f1dfe857b67f00565200cad8c60192ca6167ecac6894a392186451febf1` |
| `mysql9.7` | `sha256:e287b0846d6ea19f4b18b0c51b5ae79c4f0b5ad337b5719e62747ff49fe608aa` |

## 6) Component smoke results

Fill in after `./scripts/smoke-component.sh` runs.

| Tag | Result |
| :-- | :-- |
| `mysql8.4` | TBD |
| `mysql9.7` | TBD |

## 7) Go/No-Go

- Decision: Pending smoke results.
- Blockers: None identified at RC2 cut time.
