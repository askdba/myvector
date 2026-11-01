/*
   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License, version 2.0, for more details.
*/

// component_mcp: Phase 4 spike of RFC 0001 (askdba/mysql-mcp-server). An MCP Streamable
// HTTP endpoint (JSON-RPC 2.0 over POST /mcp) inside mysqld, on MySQL Router's HTTP server
// (the libraries component_mysql_rest_service links). Spike scope:
//
//   - loopback only, plain HTTP (TLS comes with Phase 5);
//   - Authorization: Bearer <token>, checked against mcp_http.token_sha256 (hex SHA-256)
//     and mapped to the one account in mcp_http.account (user@host);
//   - tools: list_databases (CALL mysql_mcp.list_databases()) and run_query (one statement);
//   - each tool call runs in a "local" command-service session that impersonates the
//     account, announced with connection audit events so that component_mcp_guard (and
//     audit log plugins) see it: such sessions start unaudited.
//
// Install (read-only settings, given at install time):
//   INSTALL COMPONENT 'file://component_mcp'
//     SET GLOBAL mcp_http.port = 33480, GLOBAL mcp_http.account = 'mcp@%',
//         GLOBAL mcp_http.token_sha256 = SHA2('the token', 256);

#include "my_rapidjson_size_t.h"  // before any rapidjson header

#include <rapidjson/document.h>
#include <rapidjson/stringbuffer.h>
#include <rapidjson/writer.h>

#include <mysql/components/component_implementation.h>
#include <mysql/components/service_implementation.h>
#include <mysql/components/services/audit_api_connection_service.h>
#include <mysql/components/services/component_sys_var_service.h>
#include <mysql/components/services/log_builtins.h>
#include <mysql/components/services/mysql_command_services.h>
#include <mysql/components/services/mysql_system_variable.h>
#include <mysqld_error.h>

#include <openssl/crypto.h>
#include <openssl/evp.h>

#include <atomic>
#include <cstring>
#include <memory>
#include <optional>
#include <string>
#include <thread>

#include "http/base/request.h"
#include "http/base/request_handler.h"
#include "http/base/uri_path_matcher.h"
#include "http/http_server_context.h"
#include "dim.h"
#include "mysql/harness/logging/logging.h"
#include "mysql/harness/logging/registry.h"
#include "mysqlrouter/io_component.h"
// In-tree, as component_mysql_rest_service: the session THD behind a command handle.
#include "sql/server_component/mysql_command_services_imp.h"

REQUIRES_SERVICE_PLACEHOLDER(log_builtins);
REQUIRES_SERVICE_PLACEHOLDER(log_builtins_string);
REQUIRES_SERVICE_PLACEHOLDER(component_sys_variable_register);
REQUIRES_SERVICE_PLACEHOLDER(component_sys_variable_unregister);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_factory);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_options);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_query);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_query_result);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_field_info);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_field_metadata);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_error_info);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_thread);
REQUIRES_SERVICE_PLACEHOLDER(mysql_audit_api_connection);
REQUIRES_SERVICE_PLACEHOLDER(mysql_system_variable_reader);

// Defined by Router's harness (its component log handler); set at init.
extern SERVICE_TYPE(log_builtins) *log_bi;
extern SERVICE_TYPE(log_builtins_string) *log_bs;

namespace {

// ----- settings -------------------------------------------------------------------------

unsigned long opt_port = 33480;
char *opt_account = nullptr;       // user@host the token maps to
char *opt_token_sha256 = nullptr;  // hex SHA-256 of the bearer token
bool port_registered = false, account_registered = false, token_registered = false;

bool register_sysvars() {
  INTEGRAL_CHECK_ARG(ulong) port_arg;
  port_arg.def_val = 33480;
  port_arg.min_val = 1;
  port_arg.max_val = 65535;
  port_arg.blk_sz = 0;
  if (mysql_service_component_sys_variable_register->register_variable(
          "mcp_http", "port", PLUGIN_VAR_LONG | PLUGIN_VAR_UNSIGNED | PLUGIN_VAR_READONLY,
          "TCP port of the MCP endpoint (loopback only)", nullptr, nullptr, &port_arg, &opt_port))
    return true;
  port_registered = true;
  {
    STR_CHECK_ARG(str) account_arg;
    account_arg.def_val = nullptr;
    if (mysql_service_component_sys_variable_register->register_variable(
            "mcp_http", "account", PLUGIN_VAR_STR | PLUGIN_VAR_MEMALLOC | PLUGIN_VAR_READONLY,
            "Account (user@host) the bearer token maps to", nullptr, nullptr, &account_arg,
            &opt_account))
      return true;
    account_registered = true;
  }
  {
    STR_CHECK_ARG(str) token_arg;
    token_arg.def_val = nullptr;
    if (mysql_service_component_sys_variable_register->register_variable(
            "mcp_http", "token_sha256",
            PLUGIN_VAR_STR | PLUGIN_VAR_MEMALLOC | PLUGIN_VAR_READONLY | PLUGIN_VAR_NOPERSIST,
            "Hex SHA-256 of the bearer token", nullptr, nullptr, &token_arg, &opt_token_sha256))
      return true;
    token_registered = true;
  }
  return false;
}

bool unregister_sysvars() {
  bool failed = false;
  auto drop = [&](bool &registered, const char *name) {
    if (!registered) return;
    if (mysql_service_component_sys_variable_unregister->unregister_variable("mcp_http", name))
      failed = true;
    else
      registered = false;
  };
  drop(token_registered, "token_sha256");
  drop(account_registered, "account");
  drop(port_registered, "port");
  return failed;
}

void log_info(const std::string &msg) {
  LogComponentErr(INFORMATION_LEVEL, ER_LOG_PRINTF_MSG, ("component_mcp: " + msg).c_str());
}

// component_mcp_guard enforces the policy (read-only, schema rules, time limit) on the
// sessions the endpoint opens; without it a tool call would run with the account's plain
// grants. Its presence is checked at install and before every tool call (it can be
// uninstalled later), by its mcp.enabled variable.
bool guard_installed() {
  char buf[16];
  void *value = buf;
  size_t length = sizeof(buf) - 1;
  return !mysql_service_mysql_system_variable_reader->get(nullptr, "GLOBAL", "mcp", "enabled",
                                                          &value, &length);
}

// ----- auth -------------------------------------------------------------------------------

std::string sha256_hex(const std::string &s) {
  unsigned char md[EVP_MAX_MD_SIZE];
  unsigned int n = 0;
  if (EVP_Digest(s.data(), s.size(), md, &n, EVP_sha256(), nullptr) != 1) return "";
  static const char hex[] = "0123456789abcdef";
  std::string out;
  for (unsigned int i = 0; i < n; ++i) {
    out.push_back(hex[md[i] >> 4]);
    out.push_back(hex[md[i] & 15]);
  }
  return out;
}

// The bearer token's hash matches mcp_http.token_sha256 (compared in constant time).
bool token_ok(const std::string *authorization) {
  if (authorization == nullptr || opt_token_sha256 == nullptr || opt_token_sha256[0] == '\0')
    return false;
  static const std::string kBearer = "Bearer ";
  if (authorization->compare(0, kBearer.size(), kBearer) != 0) return false;
  const std::string got = sha256_hex(authorization->substr(kBearer.size()));
  std::string want(opt_token_sha256);
  for (char &c : want) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return got.size() == want.size() && !got.empty() &&
         CRYPTO_memcmp(got.data(), want.data(), got.size()) == 0;
}

// ----- SQL in an impersonated local session ----------------------------------------------

// Result bounds: the reply keeps at most kMaxRows rows and kMaxBytes of values. The session's
// sql_select_limit stops the server past kMaxRows + 1 rows for statements without their own
// LIMIT. The command service's default consumer collects every row while the statement runs
// (store_result only hands that buffer over), so a statement with a large explicit LIMIT is
// still held in full by the server: a bounded custom consumer is Phase 5 work.
constexpr unsigned long long kMaxRows = 1000;
constexpr size_t kMaxBytes = 4u << 20;

struct SqlResult {
  bool ok = false;
  unsigned int sql_errno = 0;
  std::string error;
  rapidjson::Document rows{rapidjson::kArrayType};  // first result set, as objects
  bool truncated = false;
};

MYSQL_THD session_thd(MYSQL_H h) {
  auto *handle = reinterpret_cast<Mysql_handle *>(h);
  return handle == nullptr || handle->mysql == nullptr ? nullptr
                                                       : static_cast<MYSQL_THD>(handle->mysql->thd);
}

void read_error(MYSQL_H h, SqlResult *r) {
  char buf[512] = "";  // sql_error copies into the caller's buffer before 26.10
  char *msg = buf;
  mysql_service_mysql_command_error_info->sql_errno(h, &r->sql_errno);
  mysql_service_mysql_command_error_info->sql_error(h, &msg);
  r->error = msg != nullptr ? msg : "";
}

// Runs one statement as account (user@host) and returns its first result set.
SqlResult run_as(const std::string &account, const std::string &sql) {
  SqlResult r;
  const size_t at = account.rfind('@');
  if (at == std::string::npos) {
    r.error = "mcp_http.account is not user@host";
    return r;
  }
  const std::string user = account.substr(0, at), host = account.substr(at + 1);
  MYSQL_H h = nullptr;
  if (mysql_service_mysql_command_factory->init(&h) || h == nullptr) {
    r.error = "cannot create a session";
    return r;
  }
  // CALL needs multi-result support; multi-statements stay off, so "a; b" is a parse error.
  // Client protocol capabilities (mysql_com.h, which components cannot include).
  constexpr uint32_t kClientMultiResults = 1UL << 17, kClientPsMultiResults = 1UL << 18;
  const uint32_t client_flags = kClientMultiResults | kClientPsMultiResults;
  if (mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_PROTOCOL, "local") ||
      mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_CLIENT_FLAGS, &client_flags) ||
      mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_USER_NAME, user.c_str()) ||
      mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_HOST_NAME, host.c_str()) ||
      mysql_service_mysql_command_factory->connect(h)) {
    read_error(h, &r);
    if (r.error.empty()) r.error = "cannot open a session for " + account;
  } else {
    // The session starts unaudited: CONNECT turns auditing on, so the guard sees it.
    MYSQL_THD thd = session_thd(h);
    static const std::string kLimit =
        "SET SESSION sql_select_limit = " + std::to_string(kMaxRows + 1);
    if (thd == nullptr || mysql_service_mysql_audit_api_connection->emit(
                              thd, MYSQL_AUDIT_CONNECTION_CONNECT) != 0) {
      r.error = "session refused";
    } else if (mysql_service_mysql_command_query->query(h, kLimit.data(), kLimit.size())) {
      read_error(h, &r);
    } else if (mysql_service_mysql_command_query->query(h, sql.data(), sql.size())) {
      read_error(h, &r);
    } else {
      r.ok = true;
      MYSQL_RES_H res = nullptr;
      if (!mysql_service_mysql_command_query_result->store_result(h, &res) && res != nullptr) {
        unsigned int n = 0;
        mysql_service_mysql_command_field_info->num_fields(res, &n);
        // fetch_fields() returns an array of MYSQL_FIELD structs, not of handles: read the
        // names one field at a time.
        std::vector<std::string> names(n);
        for (unsigned int i = 0; i < n; ++i) {
          MYSQL_FIELD_H field = nullptr;
          const char *name = nullptr;
          if (!mysql_service_mysql_command_field_info->fetch_field(res, &field) && field)
            mysql_service_mysql_command_field_metadata->get(field, MYSQL_COMMAND_FIELD_METADATA_NAME,
                                                            &name);
          names[i] = name != nullptr ? name : "col" + std::to_string(i + 1);
        }
        auto &a = r.rows.GetAllocator();
        MYSQL_ROW_H row = nullptr;
        ulong *lengths = nullptr;
        size_t bytes = 0;
        while (!mysql_service_mysql_command_query_result->fetch_row(res, &row) && row) {
          mysql_service_mysql_command_query_result->fetch_lengths(res, &lengths);
          size_t row_bytes = 0;
          for (unsigned int i = 0; i < n; ++i) row_bytes += lengths ? lengths[i] : 0;
          // Stop before a row that does not fit, so the reply stays within the bounds.
          if (r.rows.Size() >= kMaxRows || bytes + row_bytes > kMaxBytes) {
            r.truncated = true;
            break;
          }
          bytes += row_bytes;
          rapidjson::Value obj(rapidjson::kObjectType);
          for (unsigned int i = 0; i < n; ++i) {
            rapidjson::Value k(names[i].c_str(), a), v;
            if (row[i] != nullptr) v.SetString(row[i], lengths ? lengths[i] : std::strlen(row[i]), a);
            obj.AddMember(k, v, a);
          }
          r.rows.PushBack(obj, a);
        }
        mysql_service_mysql_command_query_result->free_result(res);
      }
      // Drain any further results (a CALL ends with a status result).
      while (mysql_service_mysql_command_query_result->next_result(h) == 0) {
        MYSQL_RES_H more = nullptr;
        if (!mysql_service_mysql_command_query_result->store_result(h, &more) && more)
          mysql_service_mysql_command_query_result->free_result(more);
      }
    }
    MYSQL_THD thd2 = session_thd(h);
    if (thd2 != nullptr)
      mysql_service_mysql_audit_api_connection->emit(thd2, MYSQL_AUDIT_CONNECTION_DISCONNECT);
  }
  mysql_service_mysql_command_factory->close(h);
  return r;
}

// ----- MCP JSON-RPC -----------------------------------------------------------------------

using Json = rapidjson::Document;

std::string to_string(const rapidjson::Value &v) {
  rapidjson::StringBuffer b;
  rapidjson::Writer<rapidjson::StringBuffer> w(b);
  v.Accept(w);
  return {b.GetString(), b.GetSize()};
}

void tool_result(Json &out, const std::string &text, bool is_error) {
  auto &a = out.GetAllocator();
  rapidjson::Value result(rapidjson::kObjectType), content(rapidjson::kArrayType),
      item(rapidjson::kObjectType);
  item.AddMember("type", "text", a);
  item.AddMember("text", rapidjson::Value(text.c_str(), a), a);
  content.PushBack(item, a);
  result.AddMember("content", content, a);
  result.AddMember("isError", is_error, a);
  out.AddMember("result", result, a);
}

void call_tool(const rapidjson::Value &params, Json &out) {
  if (!guard_installed()) {
    tool_result(out, "component_mcp_guard is not installed: tool calls are refused", true);
    return;
  }
  const std::string account = opt_account != nullptr ? opt_account : "";
  const std::string name =
      params.HasMember("name") && params["name"].IsString() ? params["name"].GetString() : "";
  std::string sql;
  if (name == "list_databases") {
    sql = "CALL mysql_mcp.list_databases()";
  } else if (name == "run_query") {
    const rapidjson::Value *args = params.HasMember("arguments") ? &params["arguments"] : nullptr;
    if (args == nullptr || !args->IsObject() || !args->HasMember("sql") ||
        !(*args)["sql"].IsString()) {
      tool_result(out, "run_query needs a string argument sql", true);
      return;
    }
    sql = (*args)["sql"].GetString();
  } else {
    tool_result(out, "unknown tool: " + name, true);
    return;
  }
  SqlResult r = run_as(account, sql);
  if (!r.ok) {
    tool_result(out, "Error " + std::to_string(r.sql_errno) + ": " + r.error, true);
    return;
  }
  Json reply(rapidjson::kObjectType);
  reply.AddMember("rows", rapidjson::Value(r.rows, reply.GetAllocator()), reply.GetAllocator());
  reply.AddMember("truncated", r.truncated, reply.GetAllocator());
  tool_result(out, to_string(reply), false);
}

void list_tools(Json &out) {
  static const char kTools[] =
      R"({"tools":[)"
      R"({"name":"list_databases","description":"List databases visible to the mapped account",)"
      R"("inputSchema":{"type":"object","properties":{}}},)"
      R"({"name":"run_query","description":"Run one read-only SQL statement",)"
      R"("inputSchema":{"type":"object","properties":{"sql":{"type":"string"}},"required":["sql"]}}]})";
  Json tools;
  tools.Parse(kTools);
  out.AddMember("result", rapidjson::Value(tools, out.GetAllocator()), out.GetAllocator());
}

void initialize(const rapidjson::Value &params, Json &out) {
  auto &a = out.GetAllocator();
  rapidjson::Value result(rapidjson::kObjectType), caps(rapidjson::kObjectType),
      tools(rapidjson::kObjectType), info(rapidjson::kObjectType);
  const char *version = params.IsObject() && params.HasMember("protocolVersion") &&
                                params["protocolVersion"].IsString()
                            ? params["protocolVersion"].GetString()
                            : "2025-06-18";
  result.AddMember("protocolVersion", rapidjson::Value(version, a), a);
  caps.AddMember("tools", tools, a);
  result.AddMember("capabilities", caps, a);
  info.AddMember("name", "component_mcp", a);
  info.AddMember("version", "0.0.1-spike", a);
  result.AddMember("serverInfo", info, a);
  out.AddMember("result", result, a);
}

void rpc_error(Json &out, int code, const char *message) {
  auto &a = out.GetAllocator();
  rapidjson::Value err(rapidjson::kObjectType);
  err.AddMember("code", code, a);
  err.AddMember("message", rapidjson::Value(message, a), a);
  out.AddMember("error", err, a);
}

class McpHandler : public http::base::RequestHandler {
 public:
  void handle_request(http::base::Request &req) override {
    if (req.get_method() != HttpMethod::Post) {
      req.get_output_headers().add("Allow", "POST");
      req.send_reply(HttpStatusCode::MethodNotAllowed);
      return;
    }
    if (!token_ok(req.get_input_headers().find("Authorization"))) {
      req.get_output_headers().add("WWW-Authenticate", "Bearer");
      req.send_reply(HttpStatusCode::Unauthorized);
      return;
    }
    Json in;
    in.Parse(req.get_input_body().c_str());
    if (in.HasParseError() || !in.IsObject() || !in.HasMember("method") ||
        !in["method"].IsString()) {
      reply(req, nullptr, [](Json &out) { rpc_error(out, -32700, "parse error"); });
      return;
    }
    const std::string method = in["method"].GetString();
    if (!in.HasMember("id")) {  // a notification (e.g. notifications/initialized)
      req.send_reply(HttpStatusCode::Accepted);
      return;
    }
    static const rapidjson::Value kNoParams(rapidjson::kObjectType);
    const rapidjson::Value &params = in.HasMember("params") ? in["params"] : kNoParams;
    reply(req, &in["id"], [&](Json &out) {
      if (method == "initialize")
        initialize(params, out);
      else if (method == "tools/list")
        list_tools(out);
      else if (method == "tools/call" && params.IsObject())
        call_tool(params, out);
      else if (method == "ping")
        out.AddMember("result", rapidjson::Value(rapidjson::kObjectType), out.GetAllocator());
      else
        rpc_error(out, -32601, "method not found");
    });
  }

 private:
  template <typename F>
  static void reply(http::base::Request &req, const rapidjson::Value *id, F fill) {
    Json out(rapidjson::kObjectType);
    auto &a = out.GetAllocator();
    out.AddMember("jsonrpc", "2.0", a);
    rapidjson::Value idv;
    if (id != nullptr) idv.CopyFrom(*id, a);
    out.AddMember("id", idv, a);
    fill(out);
    req.get_output_headers().add("Content-Type", "application/json");
    req.send_reply(HttpStatusCode::Ok, "OK", to_string(out));
  }
};

// ----- HTTP server lifecycle -------------------------------------------------------------

std::shared_ptr<http::HttpServerContext> server;
std::unique_ptr<IoComponent::Workguard> workguard;
std::thread io_thread;
bool io_initialized = false;

void init_router_logging() {
  static mysql_harness::logging::Registry registry;
  auto &dim = mysql_harness::DIM::instance();
  dim.set_static_LoggingRegistry(&registry);
  auto &reg = dim.get_LoggingRegistry();
  mysql_harness::logging::clear_registry(reg);
  mysql_harness::logging::create_module_loggers(
      reg, mysql_harness::logging::LogLevel::kWarning,
      {mysql_harness::logging::kMainLogger, "http_server", "io", "http_common"},
      mysql_harness::logging::kMainLogger);
  mysql_harness::logging::create_main_log_handler(reg, "component_mcp", "", true);
  reg.set_ready();
}

bool start_server() {
  init_router_logging();
  auto &io = IoComponent::get_instance();
  // HTTP work runs on the io threads, which run SQL through the command services.
  io.set_thread_lifecycle({[](size_t) { mysql_service_mysql_command_thread->init(); },
                           [](size_t) { mysql_service_mysql_command_thread->end(); }});
  if (!io.init(1, IoBackend::preferred())) return true;
  io_initialized = true;
  try {
    server = std::make_shared<http::HttpServerContext>(
        &io.io_context(), &io.io_threads(), "127.0.0.1", static_cast<uint16_t>(opt_port),
        /*max_http_connections=*/64, /*max_request_body_size=*/1 << 20,
        /*max_response_body_size=*/16 << 20);
    server->add_direct_match_route("", http::base::UriPathMatcher{"/mcp", false, false},
                                   std::make_unique<McpHandler>());
    workguard = std::make_unique<IoComponent::Workguard>(io);
    io_thread = std::thread([] { IoComponent::get_instance().run(); });
    server->start();
  } catch (const std::exception &e) {
    log_info(std::string("cannot start the endpoint: ") + e.what());
    return true;
  }
  log_info("MCP endpoint on http://127.0.0.1:" + std::to_string(opt_port) + "/mcp");
  return false;
}

void stop_server() {
  if (server) server->stop();
  workguard.reset();
  if (io_initialized) IoComponent::get_instance().stop();
  if (io_thread.joinable()) io_thread.join();
  server.reset();
  if (io_initialized) {
    IoComponent::get_instance().reset();
    IoComponent::get_instance().reset_thread_lifecycle();
    io_initialized = false;
  }
}

mysql_service_status_t mcp_init() {
  log_bi = mysql_service_log_builtins;
  log_bs = mysql_service_log_builtins_string;
  if (!guard_installed()) {
    LogComponentErr(ERROR_LEVEL, ER_LOG_PRINTF_MSG,
                    "component_mcp: component_mcp_guard must be installed first");
    return 1;
  }
  if (register_sysvars()) {
    unregister_sysvars();
    return 1;
  }
  if (start_server()) {
    stop_server();
    unregister_sysvars();
    return 1;
  }
  return 0;
}

mysql_service_status_t mcp_deinit() {
  stop_server();
  return unregister_sysvars() ? 1 : 0;
}

}  // namespace

BEGIN_COMPONENT_PROVIDES(mcp)
END_COMPONENT_PROVIDES();

BEGIN_COMPONENT_REQUIRES(mcp)
REQUIRES_SERVICE(log_builtins), REQUIRES_SERVICE(log_builtins_string),
    REQUIRES_SERVICE(component_sys_variable_register),
    REQUIRES_SERVICE(component_sys_variable_unregister), REQUIRES_SERVICE(mysql_command_factory),
    REQUIRES_SERVICE(mysql_command_options), REQUIRES_SERVICE(mysql_command_query),
    REQUIRES_SERVICE(mysql_command_query_result), REQUIRES_SERVICE(mysql_command_field_info),
    REQUIRES_SERVICE(mysql_command_field_metadata), REQUIRES_SERVICE(mysql_command_error_info),
    REQUIRES_SERVICE(mysql_command_thread), REQUIRES_SERVICE(mysql_audit_api_connection),
    REQUIRES_SERVICE(mysql_system_variable_reader),
    END_COMPONENT_REQUIRES();

BEGIN_COMPONENT_METADATA(mcp)
METADATA("mysql.author", "askdba"), METADATA("mysql.license", "GPL"),
    END_COMPONENT_METADATA();

DECLARE_COMPONENT(mcp, "mysql:mcp") mcp_init, mcp_deinit END_DECLARE_COMPONENT();

DECLARE_LIBRARY_COMPONENTS &COMPONENT_REF(mcp) END_DECLARE_LIBRARY_COMPONENTS
