/*  Copyright (c) 2024 - p3io.in / shiyer22@gmail.com */
/*
   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.

   This program is also distributed with certain software (including
   but not limited to OpenSSL) that is licensed under separate terms,
   as designated in a particular file or component or in included license
   documentation.  The authors of MySQL hereby grant you an additional
   permission to link the program and your derivative works with the
   separately licensed software that they have included with MySQL.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License, version 2.0, for more details.

   You should have received a copy of the GNU General Public License
   along with this program; if not, write to the Free Software
   Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301  USA
*/
#include <string>

#include <ctype.h>
#include <mysql/plugin.h>
#include <mysql/plugin_audit.h>
#include <mysql/service_mysql_alloc.h>

#include <thread>

#include <mysql/components/component_implementation.h>
#include <mysql/components/services/mysql_string.h>
#include <mysql/components/services/udf_metadata.h>
#include <mysql/components/my_service.h>

extern REQUIRES_SERVICE_PLACEHOLDER(mysql_udf_metadata);
extern REQUIRES_SERVICE_PLACEHOLDER(mysql_string_converter);
extern REQUIRES_SERVICE_PLACEHOLDER(mysql_string_factory);

#include <mysql/service_plugin_registry.h>

SERVICE_TYPE(registry) * h_registry = nullptr;

my_service<SERVICE_TYPE(mysql_udf_metadata)> * h_udf_metadata_service = nullptr;

#include "my_inttypes.h"
#include "my_thread.h" 
#include "plugin/myvector/myvector.h"

MYSQL_PLUGIN gplugin;

void myvector_binlog_loop(int id);

static std::thread *binlog_thread = nullptr;

static int plugin_init(MYSQL_PLUGIN plugin_info)
{
    gplugin = plugin_info;

    h_registry              = mysql_plugin_registry_acquire();
    h_udf_metadata_service  = new my_service<SERVICE_TYPE(mysql_udf_metadata)>(
                                  "mysql_udf_metadata", h_registry);

    binlog_thread = new std::thread(myvector_binlog_loop, 5);
    return 0; /* success */
}

/* Config variables of the MyVector plugin */
long myvector_feature_level;
long myvector_index_bg_threads;
char *myvector_index_dir;
char *myvector_config_file;

static MYSQL_SYSVAR_LONG(
    feature_level, myvector_feature_level, PLUGIN_VAR_RQCMDARG,
    "MyVector Feature Level.",
    nullptr, nullptr, 2L, 1L, 100L, 0);

static MYSQL_SYSVAR_LONG(
    index_bg_threads, myvector_index_bg_threads, PLUGIN_VAR_RQCMDARG,
    "MyVector Index Background Threads.",
    nullptr, nullptr, 2L, 1L, 100L, 0);

static MYSQL_SYSVAR_STR(
    index_dir, myvector_index_dir,
    PLUGIN_VAR_RQCMDARG | PLUGIN_VAR_MEMALLOC,
    "MyVector index files directory.",
    nullptr, nullptr, "/mysqldata");

static MYSQL_SYSVAR_STR(
    config_file, myvector_config_file,
    PLUGIN_VAR_RQCMDARG | PLUGIN_VAR_MEMALLOC,
    "MyVector config file.",
    nullptr, nullptr, "myvector.cnf");

static SYS_VAR * myvector_system_variables[] = {
    MYSQL_SYSVAR(feature_level), MYSQL_SYSVAR(index_bg_threads), MYSQL_SYSVAR(index_dir), MYSQL_SYSVAR(config_file), nullptr};

static int myvector_sql_preparse(MYSQL_THD, mysql_event_class_t event_class,
                       const void *event) {
  const struct mysql_event_parse *event_parse =
      static_cast<const struct mysql_event_parse *>(event);
  if (event_class != MYSQL_AUDIT_PARSE_CLASS ||
      event_parse->event_subclass != MYSQL_AUDIT_PARSE_PREPARSE)
    return 0;

  std::string rewritten_query;
  if (myvector_query_rewrite(std::string(event_parse->query.str), &rewritten_query)) {
    char *rewritten_query_buf = static_cast<char *>(my_malloc(
        PSI_NOT_INSTRUMENTED, rewritten_query.length() + 1, MYF(MY_WME)));
    strcpy(rewritten_query_buf, rewritten_query.c_str());
    event_parse->rewritten_query->str = rewritten_query_buf;
    event_parse->rewritten_query->length = rewritten_query.length();
    *(reinterpret_cast<int *>(event_parse->flags)) |=
        MYSQL_AUDIT_PARSE_REWRITE_PLUGIN_QUERY_REWRITTEN;
  }

  return 0;
}

/* MyVector plugin descriptor. */
static struct st_mysql_audit myvector_descriptor = {
    MYSQL_AUDIT_INTERFACE_VERSION, /* interface version */
    nullptr,                       /* release_thd()     */
    myvector_sql_preparse,         /* event_notify()    */
    {
        0,
        0,
        (unsigned long)MYSQL_AUDIT_PARSE_ALL,
    } /* class mask        */
};

/* Plugin descriptor */
mysql_declare_plugin(myvector){
    MYSQL_AUDIT_PLUGIN,           /* plugin type             */
    &myvector_descriptor,         /* type specific descriptor*/
    "myvector",                   /* plugin name             */
    "myvector/p3io",              /* author                  */
    "Vector Storage & Search Plugin for MySQL",/* description*/
    PLUGIN_LICENSE_GPL,           /* license                 */
    plugin_init,                  /* plugin initializer      */
    nullptr,                      /* plugin check uninstall  */
    nullptr,                      /* plugin deinitializer    */
    0x0100,                       /* version                 */
    nullptr,                      /* status variables        */
    myvector_system_variables,    /* system variables        */
    nullptr,                      /* reserved                */
    0                             /* flags                   */
} mysql_declare_plugin_end;
