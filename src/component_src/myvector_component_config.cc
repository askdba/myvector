/**
 * Component-only definitions for configuration variables.
 * When built as a plugin, these are defined in myvector_plugin.cc and
 * bound to MYSQL_SYSVAR. When built as a component, there are no plugin
 * sysvars, so we provide definitions here with defaults so the component links
 * and runs. Values can be overridden at runtime if the component later
 * supports a config API or manifest options.
 */
#include <mysql/plugin.h>
#include <mysql/service_my_plugin_log.h>
#include <cstdarg>
#include <cstdio>

/** Dummy plugin handle when built as standalone component (not used). */
MYSQL_PLUGIN gplugin = nullptr;

/** Stub for plugin log when built as standalone component (no server error log). */
int my_plugin_log_message(MYSQL_PLUGIN* plugin,
                          enum plugin_log_level level,
                          const char* format, ...) {
  (void)plugin;
  (void)level;
  (void)format;
  return 0;
}

static char g_myvector_index_dir[512] = "";
static char g_myvector_config_file[512] = "myvector.cnf";

long myvector_feature_level = 2;   /* match plugin default; bit 0 = disable binlog */
long myvector_index_bg_threads = 2;
char* myvector_index_dir = g_myvector_index_dir;
char* myvector_config_file = g_myvector_config_file;
