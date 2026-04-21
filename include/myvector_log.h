#pragma once
// Logging abstraction for myvector — works in both plugin and component builds.
// Plugin builds:    wraps my_plugin_log_message via extern MYSQL_PLUGIN gplugin.
// Component builds: writes directly to stderr.

#ifdef MYVECTOR_COMPONENT_BUILD
#  include <cstdio>
#  define MYVEC_LOG_DEBUG(fmt, ...) fprintf(stderr, "[MYVEC DBG] " fmt "\n", ##__VA_ARGS__)
#  define MYVEC_LOG_INFO(fmt, ...)  fprintf(stderr, "[MYVEC INF] " fmt "\n", ##__VA_ARGS__)
#  define MYVEC_LOG_ERROR(fmt, ...) fprintf(stderr, "[MYVEC ERR] " fmt "\n", ##__VA_ARGS__)
#  define MYVEC_LOG_WARN(fmt, ...)  fprintf(stderr, "[MYVEC WRN] " fmt "\n", ##__VA_ARGS__)
#else
#  include "mysql/service_my_plugin_log.h"
   extern MYSQL_PLUGIN gplugin;
#  define MYVEC_LOG_DEBUG(...) my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL, __VA_ARGS__)
#  define MYVEC_LOG_INFO(...)  my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL, __VA_ARGS__)
#  define MYVEC_LOG_ERROR(...) my_plugin_log_message(&gplugin, MY_ERROR_LEVEL,       __VA_ARGS__)
#  define MYVEC_LOG_WARN(...)  my_plugin_log_message(&gplugin, MY_WARNING_LEVEL,     __VA_ARGS__)
#endif
