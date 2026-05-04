/**
 * Component-only definitions for configuration variables.
 * When built as a plugin, these are defined in myvector_plugin.cc and
 * bound to MYSQL_SYSVAR. When built as a component, there are no plugin
 * sysvars, so we provide definitions here with defaults so the component links
 * and runs. Values can be overridden at runtime if the component later
 * supports a config API or manifest options.
 */

static char g_myvector_index_dir[512] = "";
static char g_myvector_config_file[512] = "myvector.cnf";

long myvector_feature_level = 2;   /* Matches plugin default (2). Bit 0 set = binlog disabled; bit 0 clear = binlog enabled. Value 2 (bit 0 clear) enables binlog monitoring. */
long myvector_index_bg_threads = 2;
bool myvector_rebuild_on_start = false;  /* When true, rebuild indexes on startup if .bin load fails (plugin uses sysvar; component uses this default). */
char* myvector_index_dir = g_myvector_index_dir;
char* myvector_config_file = g_myvector_config_file;
