/*
   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License, version 2.0, for more details.
*/

// component_mcp_guard: server-side guard for mysql-mcp-server sessions
// (RFC 0001 §5.2, askdba/mysql-mcp-server docs/rfc).
//
// MCP session: a connection whose account holds the MCP_USER dynamic privilege (and not
// MCP_ADMIN, so GRANT ALL to an administrator does not put it under the guard). It is
// decided at connect and change-user time and kept in a per-THD slot. Every statement
// and table access of an MCP session is checked against the mcp.* policy:
//
//   mcp.enabled              OFF: refuse MCP connections and statements (kill switch).
//   mcp.read_only            ON (default): only read statements, by sql_command, plus
//                            SELECT without INTO OUTFILE/DUMPFILE or row locks, session
//                            SET, and CALL into the mysql_mcp tool pack; no table writes.
//   mcp.deny_system_schemas  ON (default): no table access in mysql and sys.
//   mcp.allowed_schemas      Comma-separated schemas MCP sessions may read; empty: all.
//
// Denials fail with ER_OPTION_PREVENTS_STATEMENT ("... running with the mcp.read_only
// option ..."), like the server's own read_only. SET GLOBAL / PERSIST of mcp.* needs
// MCP_ADMIN on top of SYSTEM_VARIABLES_ADMIN. Status variables: mcp.sessions_connected,
// mcp.statements_total, mcp.statements_denied, mcp.table_access_denied.

#include <mysql/components/component_implementation.h>
#include <mysql/components/service_implementation.h>
#include <mysql/components/services/component_status_var_service.h>
#include <mysql/components/services/component_sys_var_service.h>
#include <mysql/components/services/dynamic_privilege.h>
#include <mysql/components/services/mysql_current_thread_reader.h>
#include <mysql/components/services/mysql_runtime_error_service.h>
#include <mysql/components/services/mysql_thd_store_service.h>
#include <mysql/components/services/security_context.h>
#include <mysql/components/util/event_tracking/event_tracking_connection_consumer_helper.h>
#include <mysql/components/util/event_tracking/event_tracking_global_variable_consumer_helper.h>
#include <mysql/components/util/event_tracking/event_tracking_query_consumer_helper.h>
#include <mysql/components/util/event_tracking/event_tracking_table_access_consumer_helper.h>

#include <atomic>
#include <cstring>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "mcp_guard_policy.h"

REQUIRES_SERVICE_PLACEHOLDER(dynamic_privilege_register);
REQUIRES_SERVICE_PLACEHOLDER(global_grants_check);
REQUIRES_SERVICE_PLACEHOLDER(mysql_current_thread_reader);
REQUIRES_SERVICE_PLACEHOLDER(mysql_thd_security_context);
REQUIRES_SERVICE_PLACEHOLDER(mysql_thd_store);
REQUIRES_SERVICE_PLACEHOLDER(component_sys_variable_register);
REQUIRES_SERVICE_PLACEHOLDER(component_sys_variable_unregister);
REQUIRES_SERVICE_PLACEHOLDER(status_variable_registration);
REQUIRES_SERVICE_PLACEHOLDER(mysql_runtime_error);

namespace {

// ER_OPTION_PREVENTS_STATEMENT: "The MySQL server is running with the %s option so it
// cannot execute this statement". Same code as the server's read_only; stable since 5.0.
constexpr int kErOptionPreventsStatement = 1290;
// ER_SPECIFIC_ACCESS_DENIED_ERROR: "Access denied; you need (at least one of) the %-.128s
// privilege(s) for this operation".
constexpr int kErSpecificAccessDenied = 1227;

constexpr const char *kMcpUser = "MCP_USER";
constexpr const char *kMcpAdmin = "MCP_ADMIN";

// ----- Dynamic privileges -------------------------------------------------------------

// MCP_USER marks accounts used by mysql-mcp-server; MCP_ADMIN may change mcp.* policy.
// Both names are reserved for this component (see sql/mcp/README.md).
constexpr const char *kPrivileges[] = {kMcpUser, kMcpAdmin};
constexpr size_t kPrivilegeCount = sizeof(kPrivileges) / sizeof(kPrivileges[0]);

// Which privileges this load of the component registered. The server registry has no
// owner or reference count (registering an existing name reports success), so, like
// Oracle's component_mysql_rest_service, unregister only what this component's own
// calls registered, and never a name whose registration failed.
bool registered[kPrivilegeCount] = {};

bool unregister_privileges() {
  bool failed = false;
  for (size_t i = 0; i < kPrivilegeCount; ++i) {
    if (!registered[i]) continue;
    if (mysql_service_dynamic_privilege_register->unregister_privilege(
            kPrivileges[i], std::strlen(kPrivileges[i]))) {
      failed = true;
      continue;
    }
    registered[i] = false;
  }
  return failed;
}

bool register_privileges() {
  for (size_t i = 0; i < kPrivilegeCount; ++i) {
    if (mysql_service_dynamic_privilege_register->register_privilege(
            kPrivileges[i], std::strlen(kPrivileges[i]))) {
      unregister_privileges();  // roll back only the ones registered above
      return true;
    }
    registered[i] = true;
  }
  return false;
}

// True when the session running on thd holds the global privilege priv.
bool thd_has_grant(MYSQL_THD thd, const char *priv) {
  Security_context_handle ctx = nullptr;
  if (mysql_service_mysql_thd_security_context->get(thd, &ctx) || ctx == nullptr)
    return false;
  return mysql_service_global_grants_check->has_global_grant(ctx, priv, std::strlen(priv));
}

MYSQL_THD current_thd_or_null() {
  MYSQL_THD thd = nullptr;
  if (mysql_service_mysql_current_thread_reader->get(&thd)) return nullptr;
  return thd;
}

// ----- System variables ---------------------------------------------------------------

bool opt_enabled = true;
bool opt_read_only = true;
bool opt_deny_system_schemas = true;
char *opt_allowed_schemas = nullptr;  // server-managed (PLUGIN_VAR_MEMALLOC)

// Parsed mcp.allowed_schemas, swapped as a whole on update; readers copy the pointer.
struct AllowedSchemas {
  bool restrict = false;  // the value is not blank
  std::vector<std::string> names;
};
std::mutex allowed_mutex;
std::shared_ptr<const AllowedSchemas> allowed_schemas = std::make_shared<const AllowedSchemas>();

std::shared_ptr<const AllowedSchemas> current_allowed_schemas() {
  std::lock_guard<std::mutex> lock(allowed_mutex);
  return allowed_schemas;
}

void set_allowed_schemas(const char *v) {
  const std::string_view sv = v ? std::string_view(v) : std::string_view();
  auto parsed = std::make_shared<AllowedSchemas>();
  parsed->restrict = mcp_guard::restricts_schemas(sv);
  parsed->names = mcp_guard::parse_schema_list(sv);
  std::lock_guard<std::mutex> lock(allowed_mutex);
  allowed_schemas = parsed;
}

void update_allowed_schemas(MYSQL_THD, SYS_VAR *, void *var_ptr, const void *save) {
  const char *v = *static_cast<const char *const *>(save);
  *static_cast<const char **>(var_ptr) = v;
  set_allowed_schemas(v);
}

struct BoolVar {
  const char *name;
  const char *comment;
  bool *value;
  bool def;
};

constexpr size_t kBoolVarCount = 3;
const BoolVar kBoolVars[kBoolVarCount] = {
    {"enabled", "Allow mysql-mcp-server (MCP_USER) sessions; OFF refuses them all",
     &opt_enabled, true},
    {"read_only", "MCP sessions may only read, whatever their grants", &opt_read_only, true},
    {"deny_system_schemas", "MCP sessions may not access tables in mysql and sys",
     &opt_deny_system_schemas, true},
};

bool bool_registered[kBoolVarCount] = {};
bool allowed_registered = false;

// Returns true if a variable could not be unregistered; it then stays marked as
// registered and deinit fails, so the server keeps this library (and the update callback
// the variable points to) loaded.
bool unregister_sysvars() {
  bool failed = false;
  for (size_t i = 0; i < kBoolVarCount; ++i) {
    if (!bool_registered[i]) continue;
    if (mysql_service_component_sys_variable_unregister->unregister_variable("mcp",
                                                                            kBoolVars[i].name))
      failed = true;
    else
      bool_registered[i] = false;
  }
  if (allowed_registered) {
    if (mysql_service_component_sys_variable_unregister->unregister_variable("mcp",
                                                                            "allowed_schemas"))
      failed = true;
    else
      allowed_registered = false;
  }
  return failed;
}

// lower_case_table_names (read-only, set at initialization): names in table access
// events are lowercase when it is 1 or 2, so mcp.allowed_schemas is then compared
// case-insensitively.
bool case_insensitive_names = false;

void read_lower_case_table_names() {
  char buf[16] = {0};
  void *p = buf;
  size_t len = sizeof(buf) - 1;
  // get_variable() is deprecated in 9.x in favour of mysql_system_variable_reader, which
  // 8.4 LTS does not have; it is still provided by every supported server.
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
  const bool err = mysql_service_component_sys_variable_register->get_variable(
      "mysql_server", "lower_case_table_names", &p, &len);
#pragma GCC diagnostic pop
  if (!err && len > 0)
    case_insensitive_names = !(len == 1 && static_cast<const char *>(p)[0] == '0');
}

bool register_sysvars() {
  for (size_t i = 0; i < kBoolVarCount; ++i) {
    BOOL_CHECK_ARG(bool) arg;
    arg.def_val = kBoolVars[i].def;
    if (mysql_service_component_sys_variable_register->register_variable(
            "mcp", kBoolVars[i].name, PLUGIN_VAR_BOOL | PLUGIN_VAR_RQCMDARG,
            kBoolVars[i].comment, nullptr, nullptr, &arg, kBoolVars[i].value))
      return true;
    bool_registered[i] = true;
  }
  STR_CHECK_ARG(str) str_arg;
  str_arg.def_val = nullptr;
  if (mysql_service_component_sys_variable_register->register_variable(
          "mcp", "allowed_schemas", PLUGIN_VAR_STR | PLUGIN_VAR_MEMALLOC | PLUGIN_VAR_RQCMDARG,
          "Comma-separated schemas whose tables MCP sessions may access; blank: all",
          nullptr, update_allowed_schemas, &str_arg, &opt_allowed_schemas))
    return true;
  allowed_registered = true;
  // A persisted or command-line value is stored without the update callback: parse it.
  set_allowed_schemas(opt_allowed_schemas);
  return false;
}

// ----- Status variables ---------------------------------------------------------------

std::atomic<long long> sessions_connected{0};
std::atomic<long long> statements_total{0};
std::atomic<long long> statements_denied{0};
std::atomic<long long> table_access_denied{0};
static_assert(sizeof(std::atomic<long long>) == sizeof(long long),
              "status variables read the atomics as plain long long");

SHOW_VAR status_vars[] = {
    {"mcp.sessions_connected", reinterpret_cast<char *>(&sessions_connected), SHOW_LONGLONG,
     SHOW_SCOPE_GLOBAL},
    {"mcp.statements_total", reinterpret_cast<char *>(&statements_total), SHOW_LONGLONG,
     SHOW_SCOPE_GLOBAL},
    {"mcp.statements_denied", reinterpret_cast<char *>(&statements_denied), SHOW_LONGLONG,
     SHOW_SCOPE_GLOBAL},
    {"mcp.table_access_denied", reinterpret_cast<char *>(&table_access_denied), SHOW_LONGLONG,
     SHOW_SCOPE_GLOBAL},
    {nullptr, nullptr, SHOW_LONGLONG, SHOW_SCOPE_GLOBAL}};
bool status_registered = false;

// ----- MCP session marker (per THD) ---------------------------------------------------

mysql_thd_store_slot session_slot = nullptr;
char mcp_session_marker;  // the slot holds &mcp_session_marker for MCP sessions

int free_session_slot(void *) { return 0; }  // the marker is static

bool is_mcp_session(MYSQL_THD thd) {
  return thd != nullptr && session_slot != nullptr &&
         mysql_service_mysql_thd_store->get(thd, session_slot) == &mcp_session_marker;
}

void mark_session(MYSQL_THD thd, bool mcp) {
  if (is_mcp_session(thd) == mcp) return;
  mysql_service_mysql_thd_store->set(thd, session_slot, mcp ? &mcp_session_marker : nullptr);
  sessions_connected += mcp ? 1 : -1;
}

bool deny(const char *option, std::atomic<long long> &counter) {
  ++counter;
  mysql_error_service_emit_printf(mysql_service_mysql_runtime_error, kErOptionPreventsStatement,
                                  0, option);
  return true;
}

}  // namespace

// ----- Event tracking -----------------------------------------------------------------

namespace Event_tracking_implementation {

// Connection events: decide (and on change-user re-decide) whether this is an MCP session.
mysql_event_tracking_connection_subclass_t
    Event_tracking_connection_implementation::filtered_sub_events =
        EVENT_TRACKING_CONNECTION_PRE_AUTHENTICATE;

bool Event_tracking_connection_implementation::callback(
    const mysql_event_tracking_connection_data *data) {
  MYSQL_THD thd = current_thd_or_null();
  if (thd == nullptr) return false;
  switch (data->event_subclass) {
    case EVENT_TRACKING_CONNECTION_CONNECT:
    case EVENT_TRACKING_CONNECTION_CHANGE_USER: {
      if (data->status != 0) return false;  // failed login: nothing to track
      const bool mcp = thd_has_grant(thd, kMcpUser) && !thd_has_grant(thd, kMcpAdmin);
      if (mcp && !opt_enabled) {
        mark_session(thd, false);  // refused: no disconnect event follows
        return deny("mcp.enabled", statements_denied);
      }
      mark_session(thd, mcp);
      return false;
    }
    case EVENT_TRACKING_CONNECTION_DISCONNECT:
      mark_session(thd, false);
      return false;
    default:
      return false;
  }
}

// Global variable events: SET GLOBAL / SET PERSIST of mcp.* needs MCP_ADMIN, on top of
// the SYSTEM_VARIABLES_ADMIN the server requires. Only a client's SET statement
// (sql_command "set_option") is checked: the server applies persisted values through the
// same path at startup and at INSTALL COMPONENT, with a bootstrap account that has no
// MCP_ADMIN, and those values were checked when they were persisted.
mysql_event_tracking_global_variable_subclass_t
    Event_tracking_global_variable_implementation::filtered_sub_events =
        EVENT_TRACKING_GLOBAL_VARIABLE_GET;

bool Event_tracking_global_variable_implementation::callback(
    const mysql_event_tracking_global_variable_data *data) {
  if (data->event_subclass != EVENT_TRACKING_GLOBAL_VARIABLE_SET) return false;
  const std::string_view name(data->variable_name.str ? data->variable_name.str : "",
                              data->variable_name.str ? data->variable_name.length : 0);
  if (name.size() < 4 || !mcp_guard::ieq(name.substr(0, 4), "mcp.")) return false;
  if (data->sql_command == nullptr || std::strcmp(data->sql_command, "set_option") != 0)
    return false;
  MYSQL_THD thd = current_thd_or_null();
  if (thd != nullptr && thd_has_grant(thd, kMcpAdmin)) return false;
  mysql_error_service_emit_printf(mysql_service_mysql_runtime_error, kErSpecificAccessDenied,
                                  0, kMcpAdmin);
  return true;
}

// Query events: statements, including those nested in stored programs and those run by
// EXECUTE, which raise their own event with their own sql_command.
mysql_event_tracking_query_subclass_t Event_tracking_query_implementation::filtered_sub_events =
    EVENT_TRACKING_QUERY_STATUS_END | EVENT_TRACKING_QUERY_NESTED_STATUS_END;

bool Event_tracking_query_implementation::callback(const mysql_event_tracking_query_data *data) {
  MYSQL_THD thd = current_thd_or_null();
  if (!is_mcp_session(thd)) return false;
  if (data->event_subclass == EVENT_TRACKING_QUERY_START) ++statements_total;
  if (!opt_enabled) return deny("mcp.enabled", statements_denied);
  if (opt_read_only) {
    const char *cmd = data->sql_command ? data->sql_command : "";
    const char *query = data->query.str ? data->query.str : "";
    const size_t len = data->query.str ? data->query.length : 0;
    const char *why = mcp_guard::read_only_violation(cmd, query, len);
    if (why != nullptr) return deny(why, statements_denied);
  }
  return false;
}

// Table access events: writes under read_only (for example from a stored function or a
// trigger), and the schema rules. They carry the exact database and table.
mysql_event_tracking_table_access_subclass_t
    Event_tracking_table_access_implementation::filtered_sub_events = 0;

bool Event_tracking_table_access_implementation::callback(
    const mysql_event_tracking_table_access_data *data) {
  MYSQL_THD thd = current_thd_or_null();
  if (!is_mcp_session(thd)) return false;
  if (!opt_enabled) return deny("mcp.enabled", table_access_denied);
  const auto allowed = current_allowed_schemas();
  mcp_guard::TablePolicy policy;
  policy.read_only = opt_read_only;
  policy.deny_system_schemas = opt_deny_system_schemas;
  policy.restrict_schemas = allowed->restrict;
  policy.allowed_schemas = &allowed->names;
  policy.case_insensitive_names = case_insensitive_names;
  const std::string_view db(data->table_database.str ? data->table_database.str : "",
                            data->table_database.str ? data->table_database.length : 0);
  const bool write = data->event_subclass != EVENT_TRACKING_TABLE_ACCESS_READ;
  const char *why = mcp_guard::table_violation(policy, db, write);
  if (why != nullptr) return deny(why, table_access_denied);
  return false;
}

}  // namespace Event_tracking_implementation

// ----- Component lifecycle ------------------------------------------------------------

namespace {

mysql_service_status_t mcp_guard_deinit() {
  // Variables first: if one cannot be unregistered the library stays loaded (its update
  // callback is still referenced), so keep everything else, including the session marker
  // the event callbacks enforce with, in place and fail.
  if (unregister_sysvars()) return 1;
  bool failed = false;
  if (status_registered) {
    if (mysql_service_status_variable_registration->unregister_variable(status_vars))
      failed = true;
    else
      status_registered = false;
  }
  if (session_slot != nullptr) {
    if (mysql_service_mysql_thd_store->unregister_slot(session_slot))
      failed = true;
    else
      session_slot = nullptr;
  }
  if (unregister_privileges()) failed = true;
  return failed ? 1 : 0;
}

mysql_service_status_t mcp_guard_init() {
  if (register_privileges() ||
      mysql_service_mysql_thd_store->register_slot("component_mcp_guard", free_session_slot,
                                                   &session_slot) ||
      register_sysvars() ||
      mysql_service_status_variable_registration->register_variable(status_vars)) {
    mcp_guard_deinit();
    return 1;
  }
  status_registered = true;
  read_lower_case_table_names();
  return 0;
}

}  // namespace

IMPLEMENTS_SERVICE_EVENT_TRACKING_CONNECTION(mcp_guard);
IMPLEMENTS_SERVICE_EVENT_TRACKING_GLOBAL_VARIABLE(mcp_guard);
IMPLEMENTS_SERVICE_EVENT_TRACKING_QUERY(mcp_guard);
IMPLEMENTS_SERVICE_EVENT_TRACKING_TABLE_ACCESS(mcp_guard);

BEGIN_COMPONENT_PROVIDES(mcp_guard)
PROVIDES_SERVICE_EVENT_TRACKING_CONNECTION(mcp_guard),
    PROVIDES_SERVICE_EVENT_TRACKING_GLOBAL_VARIABLE(mcp_guard),
    PROVIDES_SERVICE_EVENT_TRACKING_QUERY(mcp_guard),
    PROVIDES_SERVICE_EVENT_TRACKING_TABLE_ACCESS(mcp_guard), END_COMPONENT_PROVIDES();

BEGIN_COMPONENT_REQUIRES(mcp_guard)
REQUIRES_SERVICE(dynamic_privilege_register), REQUIRES_SERVICE(global_grants_check),
    REQUIRES_SERVICE(mysql_current_thread_reader), REQUIRES_SERVICE(mysql_thd_security_context),
    REQUIRES_SERVICE(mysql_thd_store), REQUIRES_SERVICE(component_sys_variable_register),
    REQUIRES_SERVICE(component_sys_variable_unregister),
    REQUIRES_SERVICE(status_variable_registration), REQUIRES_SERVICE(mysql_runtime_error),
    END_COMPONENT_REQUIRES();

BEGIN_COMPONENT_METADATA(mcp_guard)
METADATA("mysql.author", "askdba"), METADATA("mysql.license", "GPL"),
    METADATA("mcp_guard.phase", "2"), END_COMPONENT_METADATA();

DECLARE_COMPONENT(mcp_guard, "file://component_mcp_guard")
mcp_guard_init, mcp_guard_deinit END_DECLARE_COMPONENT();

DECLARE_LIBRARY_COMPONENTS &COMPONENT_REF(mcp_guard) END_DECLARE_LIBRARY_COMPONENTS
