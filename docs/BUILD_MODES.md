# MyVector Build Modes: Plugin vs Component

This document describes the two build modes (plugin and component) and their behavioral differences for the going-forward plan.

## Identical Behavior

- **UDF names and signatures:** Same UDFs (`myvector_construct`, `myvector_display`, `myvector_distance`, etc.) with identical SQL surface.
- **Default config:** Same configuration file and sysvars (`myvector_index_dir`, `myvector_config_file`, etc.).
- **Persistence:** Binlog state and index directory layout are the same.

## Differences

| Aspect | Plugin | Component |
|--------|--------|-----------|
| **Activation** | `mysql < sql/myvectorplugin.sql` (`INSTALL PLUGIN` + UDFs + procedures) | `mysql < sql/myvector_install_component.sql` (`INSTALL COMPONENT` + supplemental UDFs + procedures + view) |
| **UDF registration** | Manual `CREATE FUNCTION ... SONAME 'myvector.so'` per UDF | Core UDFs automatic on component init; supplemental UDFs (`myvector_row_distance`, `myvector_is_valid`, `myvector_search_open_udf`) + `MYVECTOR_INDEX_*` procedures + view created by the install script |
| **Deactivation** | `UNINSTALL PLUGIN myvector` (drops UDFs) | `mysql < sql/myvector_uninstall_component.sql` (drops procedures/view/supplemental UDFs, then `UNINSTALL COMPONENT`) — see the constraint below |
| **Init timing** | Plugin load hooks | Component service init |
| **Binlog Events** | Simple queue; no `request_shutdown()`/`clear_shutdown()` | Full shutdown/restart support via `request_shutdown()` and `clear_shutdown()` |
| **Query rewrite** (inline `MYVECTOR(...)` DDL, `WHERE MYVECTOR_IS_ANN(...)`) | Yes | Yes, via the Event Tracking Parse service — fixed in [#144](https://github.com/askdba/myvector/issues/144). Requires an exact MySQL point-release match between build headers and running server; see [#174](https://github.com/askdba/myvector/issues/174) if the rewrite appears to silently no-op. |

### Component: `UNINSTALL COMPONENT` requires no other active connections

Because the component provides the `event_tracking_parse` service (for query
rewrite), `UNINSTALL COMPONENT` fails with `ERROR 3540` whenever *any other*
session has run at least one query since the component was installed and is
still connected — this is a MySQL server-side reference-caching limitation
(see [#155](https://github.com/askdba/myvector/issues/155) for the full
trace), not something specific to this component. In practice, the binlog
listener's own long-lived connection is almost always the one that trips
this, so: **`DROP` all `online=Y` indexes first** (which stops the binlog
listener), and make sure no other client connection is idling nearby, before
running `UNINSTALL COMPONENT`.

## Reinstall Behavior (Going-Forward Plan)

**Component:** Supports in-process reinstall (`UNINSTALL COMPONENT` then `INSTALL COMPONENT`). The binlog service uses `EventsQ::clear_shutdown()` in `start_binlog_monitoring()` to reset the queue; without it, worker threads would exit immediately after reinstall. CI tests this path in `test-component` and `test-component-9-7`.

**Plugin:** Uses a different `EventsQ` in `src/myvector_binlog.cc` (no `request_shutdown`/`shutting_down_`). Load/unload semantics differ; reinstall requires `UNINSTALL PLUGIN` then `INSTALL PLUGIN` plus re-registering UDFs. CI tests plugin reinstall in the `test` job.

**Going forward:** When adding features that affect lifecycle (e.g., shutdown, restart, reinstall), consider both build modes and document divergence here. Component is the preferred path for new development.
