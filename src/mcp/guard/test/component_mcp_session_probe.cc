/*
   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License, version 2.0, for more details.
*/

// TEST ONLY, never installed or packaged. A component that runs SQL the way component_mcp
// (RFC 0001 Phase 4) does: on a thread of its own, in a "local" command-service session
// that impersonates an account, announced with connection audit events. The guard test
// (scripts/test-mcp-guard.sh) uses it to check that component_mcp_guard enforces its
// policy on such sessions.
//
//   SELECT mcp_test_local_session('mcp', '%', 'INSERT INTO d1.t VALUES (1)');
//   SELECT mcp_test_local_session('mcp', '%', '...', 'unaudited');  -- no connection events
//   -> "ok:<first value>" | "ok:no-result" | "err:<step>:<errno>:<message>"
//
// Sessions of the command services start unaudited (Srv_session sets m_audited = false):
// no connection or statement events reach audit consumers until the component emits
// MYSQL_AUDIT_CONNECTION_CONNECT for the session's THD, which turns auditing on. The THD
// is read from the handle, as component_mysql_rest_service does, because it is current
// only while a statement runs. A component that skips the event runs its SQL unseen by
// component_mcp_guard (and by audit log plugins).
#include <mysql/components/component_implementation.h>
#include <mysql/components/service_implementation.h>
#include <mysql/components/services/mysql_command_services.h>
#include <mysql/components/services/udf_registration.h>
#include <mysql/components/services/audit_api_connection_service.h>
#include <thread>
// In-tree only (as component_mysql_rest_service): the session THD behind a handle.
#include "sql/server_component/mysql_command_services_imp.h"
#include <cstring>
#include <string>

REQUIRES_SERVICE_PLACEHOLDER_AS(udf_registration, udf_srv);
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_command_factory, cmd_factory_srv);
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_command_options, cmd_options_srv);
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_command_query, cmd_query_srv);
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_command_query_result, cmd_query_result_srv);
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_command_field_info, cmd_field_info_srv);
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_command_error_info, cmd_error_info_srv);
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_command_thread, cmd_thread_srv);
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_audit_api_connection, audit_conn_srv);

static MYSQL_THD session_thd(MYSQL_H h) {
  auto *handle = reinterpret_cast<Mysql_handle *>(h);
  if (handle == nullptr || handle->mysql == nullptr) return nullptr;
  return static_cast<MYSQL_THD>(handle->mysql->thd);  // set by connect, as MRS reads it
}

static std::string run(const std::string &user, const std::string &host, const std::string &sql, bool audit = false) {
  MYSQL_H h = nullptr;
  std::string out;
  auto err = [&](const char *where) {
    unsigned int no = 0;
    // sql_error copies into the caller's buffer before 26.10 (strcpy into *errmsg), so it
    // is always given one; MYSQL_ERRMSG_SIZE is 512.
    char buf[512] = "";
    char *msg = buf;
    if (h != nullptr) {
      cmd_error_info_srv->sql_errno(h, &no);
      cmd_error_info_srv->sql_error(h, &msg);
    }
    return std::string("err:") + where + ":" + std::to_string(no) + ":" + (msg ? msg : "");
  };
  if (cmd_factory_srv->init(&h) || h == nullptr) return "err:init";
  if (cmd_options_srv->set(h, MYSQL_COMMAND_PROTOCOL, "local") ||
      cmd_options_srv->set(h, MYSQL_COMMAND_USER_NAME, user.c_str()) ||
      cmd_options_srv->set(h, MYSQL_COMMAND_HOST_NAME, host.c_str())) {
    out = err("options");
  } else if (cmd_factory_srv->connect(h)) {
    out = err("connect");
  } else if (audit && [&] {
               // As component_mcp will: on a thread of its own, emit CONNECT for the session
               // so audit consumers (component_mcp_guard) see it.
               MYSQL_THD thd = session_thd(h);
               if (thd == nullptr) {
                 out = "[no session THD] ";
                 return true;
               }
               return audit_conn_srv->emit(thd, MYSQL_AUDIT_CONNECTION_CONNECT) != 0;
             }()) {
    out += err("audit-connect");
  } else if (cmd_query_srv->query(h, sql.data(), sql.size())) {
    out += err("query");
  } else {
    MYSQL_RES_H res = nullptr;
    if (cmd_query_result_srv->store_result(h, &res) || res == nullptr) {
      out += "ok:no-result";
    } else {
      MYSQL_ROW_H row = nullptr;
      if (!cmd_query_result_srv->fetch_row(res, &row) && row != nullptr && row[0] != nullptr)
        out += std::string("ok:") + row[0];
      else
        out += "ok:empty";
      cmd_query_result_srv->free_result(res);
    }
  }
  if (audit) {
    MYSQL_THD thd = session_thd(h);
    if (thd != nullptr) audit_conn_srv->emit(thd, MYSQL_AUDIT_CONNECTION_DISCONNECT);
  }
  cmd_factory_srv->close(h);
  return out;
}

static char *probe_udf(UDF_INIT *, UDF_ARGS *args, char *result, unsigned long *length,
                       unsigned char *is_null, unsigned char *error) {
  *is_null = 0;
  *error = 0;
  for (unsigned i = 0; i < args->arg_count; ++i)
    if (args->args[i] == nullptr) {
      std::memcpy(result, "err:null-argument", 17);
      *length = 17;
      return result;
    }
  const std::string user(args->args[0], args->lengths[0]), host(args->args[1], args->lengths[1]),
      sql(args->args[2], args->lengths[2]);
  const bool audited = !(args->arg_count == 4 && args->args[3] != nullptr &&
                         std::string(args->args[3], args->lengths[3]) == "unaudited");
  std::string r;
  // A thread of its own, as component_mcp's HTTP workers.
  std::thread t([&] {
    if (cmd_thread_srv->init()) {
      r = "err:thread-init";
      return;
    }
    r = run(user, host, sql, audited);
    cmd_thread_srv->end();
  });
  t.join();
  const size_t n = r.size() < 250 ? r.size() : 250;
  std::memcpy(result, r.data(), n);
  *length = n;
  return result;
}

static bool probe_init(UDF_INIT *, UDF_ARGS *args, char *msg) {
  if (args->arg_count != 3 && args->arg_count != 4) {
    std::strcpy(msg, "mcp_test_local_session(user, host, sql[, 'unaudited'])");
    return true;
  }
  for (unsigned i = 0; i < args->arg_count; ++i) args->arg_type[i] = STRING_RESULT;
  return false;
}

static mysql_service_status_t init() {
  return udf_srv->udf_register("mcp_test_local_session", STRING_RESULT, reinterpret_cast<Udf_func_any>(probe_udf),
                               probe_init, nullptr);
}
static mysql_service_status_t deinit() {
  int was_present = 0;
  return udf_srv->udf_unregister("mcp_test_local_session", &was_present);
}

BEGIN_COMPONENT_PROVIDES(mcp_session_probe) END_COMPONENT_PROVIDES();
BEGIN_COMPONENT_REQUIRES(mcp_session_probe)
REQUIRES_SERVICE_AS(udf_registration, udf_srv), REQUIRES_SERVICE_AS(mysql_command_factory, cmd_factory_srv),
    REQUIRES_SERVICE_AS(mysql_command_options, cmd_options_srv),
    REQUIRES_SERVICE_AS(mysql_command_query, cmd_query_srv),
    REQUIRES_SERVICE_AS(mysql_command_query_result, cmd_query_result_srv),
    REQUIRES_SERVICE_AS(mysql_command_field_info, cmd_field_info_srv),
    REQUIRES_SERVICE_AS(mysql_command_error_info, cmd_error_info_srv),
    REQUIRES_SERVICE_AS(mysql_command_thread, cmd_thread_srv),
    REQUIRES_SERVICE_AS(mysql_audit_api_connection, audit_conn_srv), END_COMPONENT_REQUIRES();
BEGIN_COMPONENT_METADATA(mcp_session_probe) END_COMPONENT_METADATA();
DECLARE_COMPONENT(mcp_session_probe, "mysql:mcp_session_probe") init, deinit END_DECLARE_COMPONENT();
DECLARE_LIBRARY_COMPONENTS &COMPONENT_REF(mcp_session_probe) END_DECLARE_LIBRARY_COMPONENTS
