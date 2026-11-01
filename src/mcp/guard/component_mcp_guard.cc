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
//   mcp.max_execution_time_ms  Time limit for their SELECTs (session max_execution_time,
//                            set at connect), which they cannot raise; 0: none.
//   mcp.log_denials          ON (default): refusals go to the error log, 10 a minute.
//
// Denials fail with ER_OPTION_PREVENTS_STATEMENT ("... running with the mcp.read_only
// option ..."), like the server's own read_only. SET GLOBAL / PERSIST of mcp.* needs
// MCP_ADMIN on top of SYSTEM_VARIABLES_ADMIN. Status variables: mcp.sessions_connected,
// mcp.statements_total, mcp.statements_denied, mcp.table_access_denied.

#include <mysql/components/component_implementation.h>
#include <mysql/components/service_implementation.h>
#include <mysql/components/services/component_status_var_service.h>
#include <mysql/components/services/component_sys_var_service.h>
#include <mysql/components/services/audit_api_message_service.h>
#include <mysql/components/services/dynamic_privilege.h>
#include <mysql/components/services/log_builtins.h>
#include <mysql/components/services/mysql_current_thread_reader.h>
#include <mysql/components/services/mysql_runtime_error_service.h>
#include <mysql/components/services/mysql_string.h>
#include <mysql/components/services/mysql_system_variable.h>
#include <mysql/components/services/mysql_thd_store_service.h>
#include <mysql/components/services/pfs_plugin_table_service.h>
#include <mysql/components/services/security_context.h>
#include <mysql/components/util/event_tracking/event_tracking_command_consumer_helper.h>
#include <mysql/components/util/event_tracking/event_tracking_connection_consumer_helper.h>
#include <mysql/components/util/event_tracking/event_tracking_global_variable_consumer_helper.h>
#include <mysql/components/util/event_tracking/event_tracking_query_consumer_helper.h>
#include <mysql/components/util/event_tracking/event_tracking_table_access_consumer_helper.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstring>
#include <memory>
#include <new>
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
REQUIRES_SERVICE_PLACEHOLDER(mysql_system_variable_update_integer);
REQUIRES_SERVICE_PLACEHOLDER(mysql_string_factory);
REQUIRES_SERVICE_PLACEHOLDER(mysql_string_converter);
REQUIRES_SERVICE_PLACEHOLDER(log_builtins);
REQUIRES_SERVICE_PLACEHOLDER(log_builtins_string);
REQUIRES_SERVICE_PLACEHOLDER(mysql_security_context_options);
REQUIRES_SERVICE_PLACEHOLDER(mysql_audit_api_message);
REQUIRES_SERVICE_PLACEHOLDER(pfs_plugin_table_v1);
REQUIRES_SERVICE_PLACEHOLDER(pfs_plugin_column_string_v2);
REQUIRES_SERVICE_PLACEHOLDER(pfs_plugin_column_bigint_v1);
REQUIRES_SERVICE_PLACEHOLDER(pfs_plugin_column_timestamp_v2);

// LogEvent() uses these.
SERVICE_TYPE(log_builtins) *log_bi = nullptr;
SERVICE_TYPE(log_builtins_string) *log_bs = nullptr;

namespace {

// ER_OPTION_PREVENTS_STATEMENT: "The MySQL server is running with the %s option so it
// cannot execute this statement". Same code as the server's read_only; stable since 5.0.
constexpr int kErOptionPreventsStatement = 1290;
// ER_SPECIFIC_ACCESS_DENIED_ERROR: "Access denied; you need (at least one of) the %-.128s
// privilege(s) for this operation".
constexpr int kErSpecificAccessDenied = 1227;
// ER_LOG_PRINTF_MSG: the error log code for a free-form component message (MY-011071 on
// 8.4, 9.7 and 26.10).
constexpr int kErLogPrintfMsg = 11071;

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
bool opt_log_denials = true;
unsigned long opt_max_execution_time_ms = 0;  // 0: no floor
unsigned long opt_tool_calls_history_size = 1000;  // rows of performance_schema.mcp_tool_calls
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

constexpr size_t kBoolVarCount = 4;
const BoolVar kBoolVars[kBoolVarCount] = {
    {"enabled", "Allow mysql-mcp-server (MCP_USER) sessions; OFF refuses them all",
     &opt_enabled, true},
    {"read_only", "MCP sessions may only read, whatever their grants", &opt_read_only, true},
    {"deny_system_schemas", "MCP sessions may not access tables in mysql and sys",
     &opt_deny_system_schemas, true},
    {"log_denials", "Write refused MCP statements to the error log (at most 10 a minute)",
     &opt_log_denials, true},
};

bool bool_registered[kBoolVarCount] = {};
bool allowed_registered = false;
bool time_registered = false;
bool history_registered = false;

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
  if (time_registered) {
    if (mysql_service_component_sys_variable_unregister->unregister_variable(
            "mcp", "max_execution_time_ms"))
      failed = true;
    else
      time_registered = false;
  }
  if (history_registered) {
    if (mysql_service_component_sys_variable_unregister->unregister_variable(
            "mcp", "tool_calls_history_size"))
      failed = true;
    else
      history_registered = false;
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
  INTEGRAL_CHECK_ARG(ulong) time_arg;
  time_arg.def_val = 0;
  time_arg.min_val = 0;
  time_arg.max_val = 4294967295UL;
  time_arg.blk_sz = 0;
  if (mysql_service_component_sys_variable_register->register_variable(
          "mcp", "max_execution_time_ms",
          PLUGIN_VAR_LONG | PLUGIN_VAR_UNSIGNED | PLUGIN_VAR_RQCMDARG,
          "Time limit in milliseconds for SELECT statements of MCP sessions, which they cannot "
          "raise; 0: none",
          nullptr, nullptr, &time_arg, &opt_max_execution_time_ms))
    return true;
  time_registered = true;
  {
    INTEGRAL_CHECK_ARG(ulong) history_arg;
    history_arg.def_val = 1000;
    history_arg.min_val = 0;
    history_arg.max_val = 100000;
    history_arg.blk_sz = 0;
    if (mysql_service_component_sys_variable_register->register_variable(
            "mcp", "tool_calls_history_size",
            PLUGIN_VAR_LONG | PLUGIN_VAR_UNSIGNED | PLUGIN_VAR_RQCMDARG | PLUGIN_VAR_READONLY,
            "Tool calls kept in performance_schema.mcp_tool_calls (read-only; 0: none)", nullptr,
            nullptr, &history_arg, &opt_tool_calls_history_size))
      return true;
    history_registered = true;
  }
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

// Per-session state of an MCP session, in a THD slot (null for other sessions).
// applied_time_limit when no limit has been applied to the current session variables.
constexpr unsigned long kTimeLimitNotApplied = ~0UL;

struct SessionState {
  unsigned long applied_time_limit = kTimeLimitNotApplied;  // last mcp.max_execution_time_ms set
  std::string account;              // priv_user@priv_host
  unsigned long long call_seq = 0;  // performance_schema.mcp_tool_calls row of the current call
  std::string call_tool, call_req;  // its tool and request id
  bool in_call_statement = false;   // a labeled top-level statement is running
  std::chrono::steady_clock::time_point statement_start;
};

mysql_thd_store_slot session_slot = nullptr;

// The slot holds an MCP session's state, kNotMcp for a session classified as not MCP, or
// null for a session not classified yet. Sessions opened without connection events (the
// "local" sessions of the command services, which components such as component_mcp open
// with an impersonated account) are classified at their first statement.
SessionState not_mcp_marker;
SessionState *const kNotMcp = &not_mcp_marker;

// Frees an MCP session's state (the only allocated kind) and uncounts it.
void drop_state(SessionState *state) {
  if (state == nullptr || state == kNotMcp) return;
  delete state;
  --sessions_connected;
}

// Called by the server when the THD ends with a state still attached; command-service
// sessions end this way, without a disconnect event.
int free_session_slot(void *state) {
  drop_state(static_cast<SessionState *>(state));
  return 0;
}

SessionState *raw_session_state(MYSQL_THD thd) {
  if (thd == nullptr || session_slot == nullptr) return nullptr;
  return static_cast<SessionState *>(mysql_service_mysql_thd_store->get(thd, session_slot));
}

// The MCP session state of thd, or null if it is not an MCP session (or not classified).
SessionState *session_state(MYSQL_THD thd) {
  SessionState *state = raw_session_state(thd);
  return state == kNotMcp ? nullptr : state;
}


// Returns true on failure. An MCP session that cannot be marked would not be guarded, so
// the caller refuses it.
bool mark_session(MYSQL_THD thd, bool mcp) {
  if (session_slot == nullptr) return mcp;
  SessionState *state = session_state(thd);
  if ((state != nullptr) == mcp) {
    // Already right; an unclassified non-MCP session still gets the marker.
    if (!mcp && raw_session_state(thd) == nullptr)
      mysql_service_mysql_thd_store->set(thd, session_slot, kNotMcp);
    return false;
  }
  if (mcp) {
    state = new (std::nothrow) SessionState();
    if (state == nullptr) return true;
    if (mysql_service_mysql_thd_store->set(thd, session_slot, state)) {
      delete state;
      return true;
    }
    ++sessions_connected;
  } else {
    mysql_service_mysql_thd_store->set(thd, session_slot, kNotMcp);
    drop_state(state);
  }
  return false;
}

// ----- performance_schema.mcp_tool_calls ----------------------------------------------

// One tool call of mysql-mcp-server: the labeled statements (/* mcp:tool=... req=... */)
// with one request id on one connection.
struct CallRecord {
  unsigned long long seq = 0;  // 0: empty slot
  unsigned long long connection_id = 0;
  std::string account, tool, request_id;
  unsigned long long statements = 0, errors = 0, denied = 0, latency_us = 0;
  unsigned long long first_seen_us = 0, last_seen_us = 0;  // microseconds since the epoch
};

unsigned long long now_us() {
  return static_cast<unsigned long long>(std::chrono::duration_cast<std::chrono::microseconds>(
                                             std::chrono::system_clock::now().time_since_epoch())
                                             .count());
}

// A fixed-size ring of the most recent tool calls (mcp.tool_calls_history_size).
class CallLog {
 public:
  void resize(size_t n) {
    std::lock_guard<std::mutex> lock(mutex_);
    buf_.assign(n, CallRecord());
  }

  unsigned long long start(unsigned long long connection_id, const std::string &account,
                           const std::string &tool, const std::string &request_id) {
    std::lock_guard<std::mutex> lock(mutex_);
    if (buf_.empty()) return 0;
    const unsigned long long seq = next_seq_++;
    CallRecord &r = buf_[(seq - 1) % buf_.size()];
    r = CallRecord();
    r.seq = seq;
    r.connection_id = connection_id;
    r.account = account;
    r.tool = tool;
    r.request_id = request_id;
    r.first_seen_us = r.last_seen_us = now_us();
    return seq;
  }

  // Applies f to the record of seq, if it is still in the ring.
  template <typename F>
  void update(unsigned long long seq, F f) {
    if (seq == 0) return;
    std::lock_guard<std::mutex> lock(mutex_);
    if (buf_.empty()) return;
    CallRecord &r = buf_[(seq - 1) % buf_.size()];
    if (r.seq == seq) f(r);
  }

  std::vector<CallRecord> snapshot() {
    std::vector<CallRecord> out;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      for (const CallRecord &r : buf_)
        if (r.seq != 0) out.push_back(r);
    }
    std::sort(out.begin(), out.end(),
              [](const CallRecord &a, const CallRecord &b) { return a.seq < b.seq; });
    return out;
  }

  unsigned long long size() {
    std::lock_guard<std::mutex> lock(mutex_);
    unsigned long long n = 0;
    for (const CallRecord &r : buf_) n += r.seq != 0;
    return n;
  }

 private:
  std::mutex mutex_;
  std::vector<CallRecord> buf_;
  unsigned long long next_seq_ = 1;
};

CallLog call_log;

// The table handle holds a snapshot taken when the table is opened. pos is the current
// row and is also the position the server saves (m_ref_length bytes, exposed by
// open_table) and restores before rnd_pos; next_pos is where rnd_next continues.
struct CallTableHandle {
  std::vector<CallRecord> rows = call_log.snapshot();
  size_t pos = 0;
  size_t next_pos = 0;
};

unsigned long long call_table_row_count() { return call_log.size(); }

int call_table_rnd_init(PSI_table_handle *h, bool) {
  auto *t = reinterpret_cast<CallTableHandle *>(h);
  t->pos = t->next_pos = 0;
  return 0;
}

int call_table_rnd_next(PSI_table_handle *h) {
  auto *t = reinterpret_cast<CallTableHandle *>(h);
  t->pos = t->next_pos;
  if (t->pos >= t->rows.size()) return PFS_HA_ERR_END_OF_FILE;
  t->next_pos = t->pos + 1;
  return 0;
}

// The server has written a saved position into pos.
int call_table_rnd_pos(PSI_table_handle *h) {
  auto *t = reinterpret_cast<CallTableHandle *>(h);
  return t->pos < t->rows.size() ? 0 : PFS_HA_ERR_END_OF_FILE;
}

void call_table_reset_position(PSI_table_handle *h) {
  auto *t = reinterpret_cast<CallTableHandle *>(h);
  t->pos = t->next_pos = 0;
}

int call_table_read_column(PSI_table_handle *h, PSI_field *f, unsigned int index) {
  auto *t = reinterpret_cast<CallTableHandle *>(h);
  if (t->pos >= t->rows.size()) return PFS_HA_ERR_END_OF_FILE;
  const CallRecord &r = t->rows[t->pos];
  auto *str = mysql_service_pfs_plugin_column_string_v2;
  auto *big = mysql_service_pfs_plugin_column_bigint_v1;
  auto *ts = mysql_service_pfs_plugin_column_timestamp_v2;
  switch (index) {
    case 0: big->set_unsigned(f, {r.connection_id, false}); break;
    case 1: str->set_varchar_utf8mb4(f, r.account.c_str()); break;
    case 2: str->set_varchar_utf8mb4(f, r.tool.c_str()); break;
    case 3: str->set_varchar_utf8mb4(f, r.request_id.c_str()); break;
    case 4: big->set_unsigned(f, {r.statements, false}); break;
    case 5: big->set_unsigned(f, {r.errors, false}); break;
    case 6: big->set_unsigned(f, {r.denied, false}); break;
    case 7: big->set_unsigned(f, {r.latency_us, false}); break;
    case 8: ts->set2(f, r.first_seen_us); break;
    case 9: ts->set2(f, r.last_seen_us); break;
    default: break;
  }
  return 0;
}

PSI_table_handle *call_table_open(PSI_pos **pos) {
  auto *t = new (std::nothrow) CallTableHandle();
  if (t != nullptr) *pos = reinterpret_cast<PSI_pos *>(&t->pos);
  return reinterpret_cast<PSI_table_handle *>(t);
}

void call_table_close(PSI_table_handle *h) { delete reinterpret_cast<CallTableHandle *>(h); }

PFS_engine_table_share_proxy call_table_share;
PFS_engine_table_share_proxy *call_table_shares[1] = {&call_table_share};
bool call_table_registered = false;

bool register_call_table() {
  call_table_share.m_table_name = "mcp_tool_calls";
  call_table_share.m_table_name_length = sizeof("mcp_tool_calls") - 1;
  call_table_share.m_table_definition =
      "CONNECTION_ID BIGINT UNSIGNED NOT NULL, ACCOUNT VARCHAR(288) NOT NULL, "
      "TOOL VARCHAR(64) NOT NULL, REQUEST_ID VARCHAR(64) NOT NULL, "
      "STATEMENTS BIGINT UNSIGNED NOT NULL, ERRORS BIGINT UNSIGNED NOT NULL, "
      "DENIED BIGINT UNSIGNED NOT NULL, LATENCY_US BIGINT UNSIGNED NOT NULL, "
      "FIRST_SEEN TIMESTAMP(6) NOT NULL, LAST_SEEN TIMESTAMP(6) NOT NULL";
  call_table_share.m_ref_length = sizeof(size_t);
  call_table_share.m_acl = READONLY;
  call_table_share.get_row_count = call_table_row_count;
  call_table_share.delete_all_rows = nullptr;
  call_table_share.m_proxy_engine_table = {.rnd_next = call_table_rnd_next,
                                           .rnd_init = call_table_rnd_init,
                                           .rnd_pos = call_table_rnd_pos,
                                           .index_init = nullptr,
                                           .index_read = nullptr,
                                           .index_next = nullptr,
                                           .read_column_value = call_table_read_column,
                                           .reset_position = call_table_reset_position,
                                           .write_column_value = nullptr,
                                           .write_row_values = nullptr,
                                           .update_column_value = nullptr,
                                           .update_row_values = nullptr,
                                           .delete_row_values = nullptr,
                                           .open_table = call_table_open,
                                           .close_table = call_table_close};
  if (mysql_service_pfs_plugin_table_v1->add_tables(call_table_shares, 1)) return true;
  call_table_registered = true;
  return false;
}

// Returns true if the table could not be removed (the library must then stay loaded).
bool unregister_call_table() {
  if (!call_table_registered) return false;
  if (mysql_service_pfs_plugin_table_v1->delete_tables(call_table_shares, 1)) return true;
  call_table_registered = false;
  return false;
}

// Records the start of a top-level statement of an MCP session: a labeled statement
// opens a new call row (or continues the current one, same request id).
void track_statement_start(SessionState *state, unsigned long long connection_id,
                           const char *query, size_t len) {
  std::string tool, req;
  if (!mcp_guard::parse_call_label(query, len, &tool, &req)) {
    state->in_call_statement = false;
    return;
  }
  if (req != state->call_req || state->call_seq == 0) {
    state->call_seq = call_log.start(connection_id, state->account, tool, req);
    state->call_tool = tool;
    state->call_req = req;
  }
  state->in_call_statement = true;
  state->statement_start = std::chrono::steady_clock::now();
  const unsigned long long now = now_us();
  call_log.update(state->call_seq, [&](CallRecord &r) {
    ++r.statements;
    r.last_seen_us = now;
  });
}

void track_statement_end(SessionState *state, int status) {
  if (!state->in_call_statement) return;
  state->in_call_statement = false;
  const auto took = std::chrono::steady_clock::now() - state->statement_start;
  const unsigned long long us = static_cast<unsigned long long>(
      std::chrono::duration_cast<std::chrono::microseconds>(took).count());
  const unsigned long long now = now_us();
  call_log.update(state->call_seq, [&](CallRecord &r) {
    r.latency_us += us;
    if (status != 0) ++r.errors;
    r.last_seen_us = now;
  });
}

// The account of the session on thd, as priv_user@priv_host.
std::string session_account(MYSQL_THD thd) {
  Security_context_handle ctx = nullptr;
  if (mysql_service_mysql_thd_security_context->get(thd, &ctx) || ctx == nullptr) return "";
  MYSQL_LEX_CSTRING user{nullptr, 0}, host{nullptr, 0};
  mysql_service_mysql_security_context_options->get(ctx, "priv_user", &user);
  mysql_service_mysql_security_context_options->get(ctx, "priv_host", &host);
  std::string out(user.str ? user.str : "", user.str ? user.length : 0);
  out += "@";
  out.append(host.str ? host.str : "", host.str ? host.length : 0);
  return out;
}

// Emits a refusal as an audit message (MYSQL_AUDIT_MESSAGE_INTERNAL, component
// "mcp_guard", producer "component_mcp_guard"), for audit log consumers.
void audit_denial(const char *option, unsigned long long connection_id, const SessionState *state,
                  const std::string &what) {
  auto str = [](const char *k, const std::string &v) {
    mysql_event_message_key_value_t kv;
    kv.key = {k, std::strlen(k)};
    kv.value_type = MYSQL_AUDIT_MESSAGE_VALUE_TYPE_STR;
    kv.value.str = {v.c_str(), v.size()};
    return kv;
  };
  const std::string opt = option;
  const bool call = state != nullptr && state->in_call_statement;
  mysql_event_message_key_value_t kvs[5];
  size_t n = 0;
  kvs[n++] = str("option", opt);
  kvs[n].key = {"connection_id", sizeof("connection_id") - 1};
  kvs[n].value_type = MYSQL_AUDIT_MESSAGE_VALUE_TYPE_NUM;
  kvs[n++].value.num = static_cast<long long>(connection_id);
  kvs[n++] = str("statement", what);
  if (call) {
    kvs[n++] = str("tool", state->call_tool);
    kvs[n++] = str("request_id", state->call_req);
  }
  static const char kComponent[] = "mcp_guard";
  static const char kProducer[] = "component_mcp_guard";
  static const char kMessage[] = "refused";
  mysql_service_mysql_audit_api_message->emit(MYSQL_AUDIT_MESSAGE_INTERNAL, kComponent,
                                              sizeof(kComponent) - 1, kProducer,
                                              sizeof(kProducer) - 1, kMessage,
                                              sizeof(kMessage) - 1, kvs, n);
}

// Writes a refusal to the error log, at most kLogBurst lines per minute; the number left
// out is reported with the next line after the minute.
constexpr int kLogBurst = 10;
std::mutex log_mutex;
std::chrono::steady_clock::time_point log_window_start;
int log_window_count = 0;
long long log_suppressed = 0;

void log_denial(const char *option, unsigned long long connection_id, std::string_view what) {
  if (!opt_log_denials || log_bi == nullptr) return;
  long long suppressed = 0;
  {
    std::lock_guard<std::mutex> lock(log_mutex);
    const auto now = std::chrono::steady_clock::now();
    if (now - log_window_start >= std::chrono::minutes(1)) {
      suppressed = log_suppressed;
      log_suppressed = 0;
      log_window_start = now;
      log_window_count = 0;
    }
    if (log_window_count >= kLogBurst) {
      ++log_suppressed;
      return;
    }
    ++log_window_count;
  }
  if (suppressed > 0)
    LogEvent()
        .type(LOG_TYPE_ERROR)
        .prio(WARNING_LEVEL)
        .errcode(kErLogPrintfMsg)
        .subsys("mcp_guard")
        .component("component: mcp_guard")
        .message("mcp_guard: %lld more MCP refusals in the last minute were not logged", suppressed);
  const std::string shown = mcp_guard::redact_for_log(what.data(), what.size());
  LogEvent()
      .type(LOG_TYPE_ERROR)
      .prio(WARNING_LEVEL)
      .errcode(kErLogPrintfMsg)
      .subsys("mcp_guard")
      .component("component: mcp_guard")
      .message("mcp_guard: refused by %s: MCP connection %llu: %s", option, connection_id,
               shown.c_str());
}

bool deny(const char *option, std::atomic<long long> &counter, unsigned long long connection_id,
          std::string_view what, SessionState *state = nullptr) {
  ++counter;
  log_denial(option, connection_id, what);
  audit_denial(option, connection_id, state, mcp_guard::redact_for_log(what.data(), what.size()));
  if (state != nullptr && state->in_call_statement)
    call_log.update(state->call_seq, [](CallRecord &r) { ++r.denied; });
  mysql_error_service_emit_printf(mysql_service_mysql_runtime_error, kErOptionPreventsStatement,
                                  0, option);
  return true;
}

// Sets the session's max_execution_time to mcp.max_execution_time_ms when the setting
// differs from what was last applied to this session: at connect, and at the next
// statement after an administrator changes it. Between changes the session may lower
// its own value (and cannot raise it). When the setting goes back to 0 the session
// value is left as it is.
my_h_string max_execution_time_name = nullptr;

void apply_time_limit(MYSQL_THD thd, SessionState *state) {
  const unsigned long limit = opt_max_execution_time_ms;
  if (state == nullptr || limit == state->applied_time_limit) return;
  state->applied_time_limit = limit;
  if (limit == 0 || max_execution_time_name == nullptr) return;
  mysql_service_mysql_system_variable_update_integer->set_unsigned(
      thd, "SESSION", nullptr, max_execution_time_name, limit);
}

// The MCP session state of thd, classifying a session that has not been classified yet
// (no connection event: a command-service "local" session, possibly impersonating an
// account) from its grants. failed is set if an MCP session could not be marked; the
// caller then refuses the statement, so it is never run unguarded.
SessionState *classify(MYSQL_THD thd, bool *failed) {
  *failed = false;
  if (thd == nullptr || session_slot == nullptr) return nullptr;
  SessionState *state = raw_session_state(thd);
  if (state == kNotMcp) return nullptr;
  if (state != nullptr) return state;
  const bool mcp = thd_has_grant(thd, kMcpUser) && !thd_has_grant(thd, kMcpAdmin);
  if (mark_session(thd, mcp)) {
    *failed = mcp;
    return nullptr;
  }
  state = session_state(thd);
  if (state != nullptr) {
    state->account = session_account(thd);
    apply_time_limit(thd, state);
  }
  return state;
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
        return deny("mcp.enabled", statements_denied, data->connection_id, "connection refused");
      }
      if (mark_session(thd, mcp))  // could not mark an MCP session: fail closed
        return deny("mcp_guard", statements_denied, data->connection_id,
                    "connection refused: session state unavailable");
      if (mcp) {
        SessionState *state = session_state(thd);
        // COM_CHANGE_USER resets session variables: apply the limit again.
        if (state != nullptr && data->event_subclass == EVENT_TRACKING_CONNECTION_CHANGE_USER) {
          state->applied_time_limit = kTimeLimitNotApplied;
          state->call_seq = 0;  // a new account starts new tool calls
          state->call_req.clear();
        }
        if (state != nullptr) state->account = session_account(thd);
        apply_time_limit(thd, state);
      }
      return false;
    }
    case EVENT_TRACKING_CONNECTION_DISCONNECT:
      mark_session(thd, false);
      return false;
    default:
      return false;
  }
}

// Command events: COM_RESET_CONNECTION resets session variables (raising no connection
// event), so the time limit is applied again at the next statement.
mysql_event_tracking_command_subclass_t Event_tracking_command_implementation::filtered_sub_events =
    EVENT_TRACKING_COMMAND_START;

bool Event_tracking_command_implementation::callback(
    const mysql_event_tracking_command_data *data) {
  if (data->status != 0) return false;
  static constexpr std::string_view kReset = "Reset Connection";
  const std::string_view command(data->command.str ? data->command.str : "",
                                 data->command.str ? data->command.length : 0);
  if (command != kReset) return false;  // cheap check first: command events are frequent
  SessionState *state = session_state(current_thd_or_null());
  if (state != nullptr) state->applied_time_limit = kTimeLimitNotApplied;
  return false;
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
    EVENT_TRACKING_QUERY_NESTED_STATUS_END;

bool Event_tracking_query_implementation::callback(const mysql_event_tracking_query_data *data) {
  MYSQL_THD thd = current_thd_or_null();
  bool failed = false;
  SessionState *state = classify(thd, &failed);
  if (failed)
    return deny("mcp_guard", statements_denied, data->connection_id,
                "statement refused: session state unavailable");
  if (state == nullptr) return false;
  if (data->event_subclass == EVENT_TRACKING_QUERY_STATUS_END) {
    track_statement_end(state, data->status);  // performance_schema.mcp_tool_calls
    return false;
  }
  const char *cmd = data->sql_command ? data->sql_command : "";
  const char *query = data->query.str ? data->query.str : "";
  const size_t len = data->query.str ? data->query.length : 0;
  if (data->event_subclass == EVENT_TRACKING_QUERY_START) {
    ++statements_total;
    apply_time_limit(thd, state);  // picks up a changed mcp.max_execution_time_ms
    track_statement_start(state, data->connection_id, query, len);
  }
  const std::string_view text(query, len);
  // A statement refused at its start raises no STATUS_END event: end it here.
  const auto refuse = [&](const char *option) {
    const bool r = deny(option, statements_denied, data->connection_id, text, state);
    if (data->event_subclass == EVENT_TRACKING_QUERY_START) track_statement_end(state, 1);
    return r;
  };
  if (!opt_enabled) return refuse("mcp.enabled");
  if (opt_read_only) {
    const char *why = mcp_guard::read_only_violation(cmd, query, len);
    if (why != nullptr) return refuse(why);
  }
  // Table reads the schema rules cannot see (no table access event): refused while a rule
  // is in effect.
  if (opt_deny_system_schemas || current_allowed_schemas()->restrict) {
    if (mcp_guard::hides_table_reads(cmd, query, len))
      return refuse(current_allowed_schemas()->restrict ? "mcp.allowed_schemas"
                                                        : "mcp.deny_system_schemas");
  }
  const char *why = mcp_guard::time_limit_violation(cmd, query, len, opt_max_execution_time_ms);
  if (why != nullptr) return refuse(why);
  return false;
}

// Table access events: writes under read_only (for example from a stored function or a
// trigger), and the schema rules. They carry the exact database and table.
mysql_event_tracking_table_access_subclass_t
    Event_tracking_table_access_implementation::filtered_sub_events = 0;

bool Event_tracking_table_access_implementation::callback(
    const mysql_event_tracking_table_access_data *data) {
  MYSQL_THD thd = current_thd_or_null();
  bool failed = false;
  SessionState *state = classify(thd, &failed);
  if (failed)
    return deny("mcp_guard", table_access_denied, data->connection_id,
                "table access refused: session state unavailable");
  if (state == nullptr) return false;
  const std::string_view db(data->table_database.str ? data->table_database.str : "",
                            data->table_database.str ? data->table_database.length : 0);
  const std::string_view table(data->table_name.str ? data->table_name.str : "",
                               data->table_name.str ? data->table_name.length : 0);
  const bool write = data->event_subclass != EVENT_TRACKING_TABLE_ACCESS_READ;
  // Built only for a refusal: table events are frequent.
  const auto refuse = [&](const char *option) {
    const std::string what =
        std::string(write ? "write " : "read ") + std::string(db) + "." + std::string(table);
    return deny(option, table_access_denied, data->connection_id, what, state);
  };
  if (!opt_enabled) return refuse("mcp.enabled");
  const auto allowed = current_allowed_schemas();
  mcp_guard::TablePolicy policy;
  policy.read_only = opt_read_only;
  policy.deny_system_schemas = opt_deny_system_schemas;
  policy.restrict_schemas = allowed->restrict;
  policy.allowed_schemas = &allowed->names;
  policy.case_insensitive_names = case_insensitive_names;
  const char *why = mcp_guard::table_violation(policy, db, write);
  if (why != nullptr) return refuse(why);
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
  if (unregister_call_table()) return 1;  // its callbacks are in this library
  call_log.resize(0);
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
  if (max_execution_time_name != nullptr) {
    mysql_service_mysql_string_factory->destroy(max_execution_time_name);
    max_execution_time_name = nullptr;
  }
  return failed ? 1 : 0;
}

mysql_service_status_t mcp_guard_init() {
  log_bi = mysql_service_log_builtins;
  log_bs = mysql_service_log_builtins_string;
  static const char kName[] = "max_execution_time";
  if (mysql_service_mysql_string_converter->convert_from_buffer(
          &max_execution_time_name, kName, sizeof(kName) - 1, "latin1"))
    max_execution_time_name = nullptr;
  if (register_privileges() ||
      mysql_service_mysql_thd_store->register_slot("component_mcp_guard", free_session_slot,
                                                   &session_slot) ||
      register_sysvars() ||
      mysql_service_status_variable_registration->register_variable(status_vars)) {
    mcp_guard_deinit();
    return 1;
  }
  status_registered = true;
  call_log.resize(opt_tool_calls_history_size);
  if (register_call_table()) {
    mcp_guard_deinit();
    return 1;
  }
  read_lower_case_table_names();
  return 0;
}

}  // namespace

IMPLEMENTS_SERVICE_EVENT_TRACKING_COMMAND(mcp_guard);
IMPLEMENTS_SERVICE_EVENT_TRACKING_CONNECTION(mcp_guard);
IMPLEMENTS_SERVICE_EVENT_TRACKING_GLOBAL_VARIABLE(mcp_guard);
IMPLEMENTS_SERVICE_EVENT_TRACKING_QUERY(mcp_guard);
IMPLEMENTS_SERVICE_EVENT_TRACKING_TABLE_ACCESS(mcp_guard);

BEGIN_COMPONENT_PROVIDES(mcp_guard)
PROVIDES_SERVICE_EVENT_TRACKING_COMMAND(mcp_guard),
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
    REQUIRES_SERVICE(mysql_system_variable_update_integer),
    REQUIRES_SERVICE(mysql_string_factory), REQUIRES_SERVICE(mysql_string_converter),
    REQUIRES_SERVICE(log_builtins), REQUIRES_SERVICE(log_builtins_string),
    REQUIRES_SERVICE(mysql_security_context_options), REQUIRES_SERVICE(mysql_audit_api_message),
    REQUIRES_SERVICE(pfs_plugin_table_v1), REQUIRES_SERVICE(pfs_plugin_column_string_v2),
    REQUIRES_SERVICE(pfs_plugin_column_bigint_v1),
    REQUIRES_SERVICE(pfs_plugin_column_timestamp_v2),
    END_COMPONENT_REQUIRES();

BEGIN_COMPONENT_METADATA(mcp_guard)
METADATA("mysql.author", "askdba"), METADATA("mysql.license", "GPL"),
    METADATA("mcp_guard.phase", "2"), END_COMPONENT_METADATA();

DECLARE_COMPONENT(mcp_guard, "file://component_mcp_guard")
mcp_guard_init, mcp_guard_deinit END_DECLARE_COMPONENT();

DECLARE_LIBRARY_COMPONENTS &COMPONENT_REF(mcp_guard) END_DECLARE_LIBRARY_COMPONENTS
