# MyVector Build Modes: Plugin vs Component

This document describes the two build modes (plugin and component) and their behavioral differences for the going-forward plan.

## Identical Behavior

- **UDF names and signatures:** Same UDFs (`myvector_construct`, `myvector_display`, `myvector_distance`, etc.) with identical SQL surface.
- **Default config:** Same configuration file and sysvars (`myvector_index_dir`, `myvector_config_file`, etc.).
- **Persistence:** Binlog state and index directory layout are the same.

## Differences

| Aspect | Plugin | Component |
|--------|--------|-----------|
| **Activation** | `INSTALL PLUGIN myvector SONAME 'myvector.so'` | `INSTALL COMPONENT 'file://myvector'` |
| **UDF registration** | Manual `CREATE FUNCTION ... SONAME 'myvector.so'` per UDF | Automatic on component init |
| **Deactivation** | `UNINSTALL PLUGIN myvector` (drops UDFs) | `UNINSTALL COMPONENT 'file://myvector'` |
| **Init timing** | Plugin load hooks | Component service init |
| **Binlog Events** | Simple queue; no `request_shutdown()`/`clear_shutdown()` | Full shutdown/restart support via `request_shutdown()` and `clear_shutdown()` |

## Reinstall Behavior (Going-Forward Plan)

**Component:** Supports in-process reinstall (`UNINSTALL COMPONENT` then `INSTALL COMPONENT`). The binlog service uses `EventsQ::clear_shutdown()` in `start_binlog_monitoring()` to reset the queue; without it, worker threads would exit immediately after reinstall. CI tests this path in `test-component` and `test-component-9-7`.

**Plugin:** Uses a different `EventsQ` in `src/myvector_binlog.cc` (no `request_shutdown`/`shutting_down_`). Load/unload semantics differ; reinstall requires `UNINSTALL PLUGIN` then `INSTALL PLUGIN` plus re-registering UDFs. CI tests plugin reinstall in the `test` job.

**Going forward:** When adding features that affect lifecycle (e.g., shutdown, restart, reinstall), consider both build modes and document divergence here. Component is the preferred path for new development.
