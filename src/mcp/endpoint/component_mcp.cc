/*
   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License, version 2.0, for more details.
*/

// component_mcp: RFC 0001 (askdba/mysql-mcp-server). An MCP Streamable HTTP endpoint
// (JSON-RPC 2.0 over POST /mcp) inside mysqld, on MySQL Router's HTTP server (the libraries
// component_mysql_rest_service links):
//
//   - 127.0.0.1 by default; TLS (mcp_http.ssl_cert / ssl_key) required off loopback;
//   - Authorization: Bearer <token>, mapped to an account (user@host): mcp_http.token_sha256 to
//     mcp_http.account, or the token table mysql.mcp_tokens (mcp_tokens.sql);
//   - the core tools (list_databases, list_tables, describe_table, run_query, ping,
//     server_info);
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
#include <mysql/components/services/component_status_var_service.h>
#include <mysql/components/services/component_sys_var_service.h>
#include <mysql/components/services/dynamic_privilege.h>
#include <mysql/components/services/log_builtins.h>
#include <mysql/components/services/mysql_command_consumer.h>
#include <mysql/components/services/mysql_command_services.h>
#include <mysql/components/services/security_context.h>
#include <mysql/components/services/event_tracking_query_service.h>
#include <mysqld_error.h>
#include "mysql/strings/m_ctype.h"
#include "mysql/strings/dtoa.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <openssl/crypto.h>
#include <openssl/evp.h>
#include <openssl/rand.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <system_error>
#include <thread>
#include <vector>

#include "http/base/request.h"
#include "http/base/request_handler.h"
#include "http/base/uri_path_matcher.h"
#include "http/http_server_context.h"
#include "dim.h"
#include "mysql/harness/logging/logging.h"
#include "mysql/harness/logging/registry.h"
#include "mysql/harness/tls_server_context.h"
#include "mysqlrouter/io_component.h"
// In-tree, as component_mysql_rest_service: the session THD behind a command handle.
#include "sql/server_component/mysql_command_services_imp.h"

REQUIRES_SERVICE_PLACEHOLDER(log_builtins);
REQUIRES_SERVICE_PLACEHOLDER(log_builtins_string);
REQUIRES_SERVICE_PLACEHOLDER(component_sys_variable_register);
REQUIRES_SERVICE_PLACEHOLDER(component_sys_variable_unregister);
REQUIRES_SERVICE_PLACEHOLDER(status_variable_registration);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_factory);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_options);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_query);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_query_result);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_field_info);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_field_metadata);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_error_info);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_thread);
REQUIRES_SERVICE_PLACEHOLDER(mysql_audit_api_connection);
REQUIRES_SERVICE_PLACEHOLDER(mysql_thd_security_context);
REQUIRES_SERVICE_PLACEHOLDER(mysql_security_context_options);
REQUIRES_SERVICE_PLACEHOLDER(global_grants_check);
// component_mcp_guard's query consumer: requiring it makes the server refuse to install the
// endpoint without the guard, and to uninstall the guard while the endpoint is installed.
REQUIRES_SERVICE_PLACEHOLDER_AS(event_tracking_query, mcp_guard_query);
// The server's default (DOM) text consumer: the bounded consumer below passes rows on to it.
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_text_consumer_row_factory_v1, dom_row_factory);
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_text_consumer_get_null_v1, dom_get_null);
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_text_consumer_get_integer_v1, dom_get_integer);
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_text_consumer_get_longlong_v1, dom_get_longlong);
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_text_consumer_get_decimal_v1, dom_get_decimal);
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_text_consumer_get_double_v1, dom_get_double);
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_text_consumer_get_date_time_v1, dom_get_date_time);
REQUIRES_SERVICE_PLACEHOLDER_AS(mysql_text_consumer_get_string_v1, dom_get_string);

// Defined by Router's harness (its component log handler); set at init.
extern SERVICE_TYPE(log_builtins) *log_bi;
extern SERVICE_TYPE(log_builtins_string) *log_bs;

namespace {

// ----- settings -------------------------------------------------------------------------

unsigned long opt_port = 33480;
unsigned long opt_threads = 4;                   // I/O workers: calls served at once
unsigned long opt_max_sessions_per_account = 4;  // concurrent calls per account
unsigned long opt_session_timeout_minutes = 30;  // idle Mcp-Session-Id lifetime
char *opt_account = nullptr;       // user@host the token maps to
char *opt_token_sha256 = nullptr;  // hex SHA-256 of the bearer token
char *opt_allowed_origins = nullptr;  // comma-separated Origin values browsers may send
char *opt_mask_columns = nullptr;     // comma-separated column name patterns to mask
char *opt_bind_address = nullptr;     // listen address; null means 127.0.0.1
char *opt_ssl_cert = nullptr;         // PEM certificate file; with opt_ssl_key, serve HTTPS
char *opt_ssl_key = nullptr;          // PEM private key file
bool session_timeout_registered = false;
bool port_registered = false, threads_registered = false, max_sessions_registered = false,
     account_registered = false, token_registered = false, origins_registered = false,
     mask_registered = false, bind_registered = false, cert_registered = false,
     key_registered = false;

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
    INTEGRAL_CHECK_ARG(ulong) timeout_arg;
    timeout_arg.def_val = 30;
    timeout_arg.min_val = 1;
    timeout_arg.max_val = 1440;
    timeout_arg.blk_sz = 0;
    if (mysql_service_component_sys_variable_register->register_variable(
            "mcp_http", "session_timeout_minutes",
            PLUGIN_VAR_LONG | PLUGIN_VAR_UNSIGNED | PLUGIN_VAR_READONLY,
            "Idle minutes after which an Mcp-Session-Id expires (404)", nullptr, nullptr,
            &timeout_arg, &opt_session_timeout_minutes))
      return true;
    session_timeout_registered = true;
  }
  {
    INTEGRAL_CHECK_ARG(ulong) threads_arg;
    threads_arg.def_val = 4;
    threads_arg.min_val = 1;
    threads_arg.max_val = 64;
    threads_arg.blk_sz = 0;
    if (mysql_service_component_sys_variable_register->register_variable(
            "mcp_http", "threads", PLUGIN_VAR_LONG | PLUGIN_VAR_UNSIGNED | PLUGIN_VAR_READONLY,
            "I/O worker threads; each serves one call at a time", nullptr, nullptr, &threads_arg,
            &opt_threads))
      return true;
    threads_registered = true;
  }
  {
    INTEGRAL_CHECK_ARG(ulong) max_sessions_arg;
    max_sessions_arg.def_val = 4;
    max_sessions_arg.min_val = 1;
    max_sessions_arg.max_val = 1024;
    max_sessions_arg.blk_sz = 0;
    if (mysql_service_component_sys_variable_register->register_variable(
            "mcp_http", "max_sessions_per_account",
            PLUGIN_VAR_LONG | PLUGIN_VAR_UNSIGNED | PLUGIN_VAR_READONLY,
            "Concurrent endpoint sessions per account (max_user_connections does not apply)",
            nullptr, nullptr, &max_sessions_arg, &opt_max_sessions_per_account))
      return true;
    max_sessions_registered = true;
  }
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
            // SENSITIVE: readable only with SENSITIVE_VARIABLES_OBSERVER, so accounts (the MCP
            // account included) cannot read the hash and guess a weak token offline.
            PLUGIN_VAR_STR | PLUGIN_VAR_MEMALLOC | PLUGIN_VAR_READONLY | PLUGIN_VAR_NOPERSIST |
                PLUGIN_VAR_SENSITIVE,
            "Hex SHA-256 of the bearer token", nullptr, nullptr, &token_arg, &opt_token_sha256))
      return true;
    token_registered = true;
  }
  {
    STR_CHECK_ARG(str) origins_arg;
    origins_arg.def_val = nullptr;
    if (mysql_service_component_sys_variable_register->register_variable(
            "mcp_http", "allowed_origins",
            PLUGIN_VAR_STR | PLUGIN_VAR_MEMALLOC | PLUGIN_VAR_READONLY,
            "Comma-separated Origin values accepted from browsers (exact match); a request "
            "with any other Origin gets 403",
            nullptr, nullptr, &origins_arg, &opt_allowed_origins))
      return true;
    origins_registered = true;
  }
  {
    STR_CHECK_ARG(str) mask_arg;
    mask_arg.def_val = nullptr;
    if (mysql_service_component_sys_variable_register->register_variable(
            "mcp_http", "mask_columns", PLUGIN_VAR_STR | PLUGIN_VAR_MEMALLOC | PLUGIN_VAR_READONLY,
            "Comma-separated patterns; result columns whose names contain one (case-insensitive) "
            "have their values replaced by ********",
            nullptr, nullptr, &mask_arg, &opt_mask_columns))
      return true;
    mask_registered = true;
  }
  {
    STR_CHECK_ARG(str) bind_arg;
    bind_arg.def_val = const_cast<char *>("127.0.0.1");
    if (mysql_service_component_sys_variable_register->register_variable(
            "mcp_http", "bind_address", PLUGIN_VAR_STR | PLUGIN_VAR_MEMALLOC | PLUGIN_VAR_READONLY,
            "Address the endpoint listens on; other than loopback, TLS is required", nullptr,
            nullptr, &bind_arg, &opt_bind_address))
      return true;
    bind_registered = true;
  }
  {
    STR_CHECK_ARG(str) cert_arg;
    cert_arg.def_val = nullptr;
    if (mysql_service_component_sys_variable_register->register_variable(
            "mcp_http", "ssl_cert", PLUGIN_VAR_STR | PLUGIN_VAR_MEMALLOC | PLUGIN_VAR_READONLY,
            "PEM certificate file; with mcp_http.ssl_key, the endpoint serves HTTPS", nullptr,
            nullptr, &cert_arg, &opt_ssl_cert))
      return true;
    cert_registered = true;
  }
  {
    STR_CHECK_ARG(str) key_arg;
    key_arg.def_val = nullptr;
    if (mysql_service_component_sys_variable_register->register_variable(
            "mcp_http", "ssl_key", PLUGIN_VAR_STR | PLUGIN_VAR_MEMALLOC | PLUGIN_VAR_READONLY,
            "PEM private key file of mcp_http.ssl_cert", nullptr, nullptr, &key_arg,
            &opt_ssl_key))
      return true;
    key_registered = true;
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
  drop(key_registered, "ssl_key");
  drop(cert_registered, "ssl_cert");
  drop(bind_registered, "bind_address");
  drop(mask_registered, "mask_columns");
  drop(origins_registered, "allowed_origins");
  drop(token_registered, "token_sha256");
  drop(account_registered, "account");
  drop(max_sessions_registered, "max_sessions_per_account");
  drop(threads_registered, "threads");
  drop(session_timeout_registered, "session_timeout_minutes");
  drop(port_registered, "port");
  return failed;
}

void log_info(const std::string &msg) {
  LogComponentErr(INFORMATION_LEVEL, ER_LOG_PRINTF_MSG, ("component_mcp: " + msg).c_str());
}

void log_error(const std::string &msg) {
  LogComponentErr(ERROR_LEVEL, ER_LOG_PRINTF_MSG, ("component_mcp: " + msg).c_str());
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
// The bearer token's hex SHA-256, or "" when the header is not "Bearer <token>".
std::string bearer_sha256(const std::string *authorization) {
  static const std::string kBearer = "Bearer ";
  if (authorization == nullptr || authorization->compare(0, kBearer.size(), kBearer) != 0 ||
      authorization->size() == kBearer.size())
    return "";
  return sha256_hex(authorization->substr(kBearer.size()));
}

// Whether the token hash matches mcp_http.token_sha256 (constant-time compare).
bool single_token_ok(const std::string &got) {
  if (got.empty() || opt_token_sha256 == nullptr || opt_token_sha256[0] == '\0') return false;
  std::string want(opt_token_sha256);
  for (char &c : want) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return got.size() == want.size() &&
         CRYPTO_memcmp(got.data(), want.data(), got.size()) == 0;
}

// DNS rebinding: a web page can make a browser send requests to 127.0.0.1, but the browser
// then sends the page's Origin. Requests without one (non-browser clients) pass; one with an
// Origin passes only if mcp_http.allowed_origins lists it (ASCII case-insensitive).
bool origin_ok(const std::string *origin) {
  if (origin == nullptr) return true;
  if (opt_allowed_origins == nullptr) return false;
  const auto same = [&](const char *b, const char *e) {
    while (b < e && *b == ' ') ++b;
    while (e > b && e[-1] == ' ') --e;
    return static_cast<size_t>(e - b) == origin->size() &&
           std::equal(b, e, origin->begin(), [](char x, char y) {
             return std::tolower(static_cast<unsigned char>(x)) ==
                    std::tolower(static_cast<unsigned char>(y));
           });
  };
  const char *p = opt_allowed_origins;
  for (const char *comma; (comma = std::strchr(p, ',')) != nullptr; p = comma + 1)
    if (same(p, comma)) return true;
  return same(p, p + std::strlen(p));
}

// ----- SQL in an impersonated local session ----------------------------------------------

// Result bounds: the reply keeps at most kMaxRows rows and kMaxBytes of serialized rows (the
// tool result then escapes them once more inside its text, so the HTTP reply stays well under
// its 16 MiB limit). The session's sql_select_limit stops the server past kMaxRows + 1 rows
// for statements without their own LIMIT. For the others, the bounded consumer below keeps
// the server from retaining more than kMaxRows + 1 rows and kMaxBytes of values while the
// statement runs (the default consumer collects every row; store_result only hands them over).
constexpr unsigned long long kMaxRows = 1000;
constexpr size_t kMaxBytes = 4u << 20;

// ----- Bounded text consumer ----------------------------------------------------------------
//
// A run_as() session selects these row and value services instead of the default ones. Each
// passes rows on to the default (DOM) consumer, so store_result() and fetch_row() work as
// before, until the statement has produced kMaxRows + 1 rows or kMaxBytes (values, plus the
// DOM consumer's per-cell and per-row overhead);
// later rows are dropped as they arrive. A row that crosses the byte budget is aborted in the
// DOM consumer, so it is never half stored. The statement runs on the calling thread, which
// holds its budget in tl_budget; without one, the services pass everything on.

struct RowBudget {
  unsigned long long max_rows = kMaxRows + 1;
  size_t max_bytes = kMaxBytes;
  unsigned long long kept = 0;
  size_t bytes = 0;       // charged so far, aborted rows included (enforces the budget)
  size_t row_start = 0;   // bytes when the current row started
  size_t kept_bytes = 0;  // charged for the rows kept
  bool in_row = false;    // the current row was passed on to the DOM consumer
  bool row_over = false;  // the current row crossed the byte budget
  bool full = false;      // later rows are dropped
  bool dropped = false;   // a row was dropped: the result is truncated
};

thread_local RowBudget *tl_budget = nullptr;

// Budget for one value of a passed-on row; false drops it (and the rest of the row).
bool take(size_t n) {
  RowBudget *b = tl_budget;
  if (b->row_over) return false;
  if (n > b->max_bytes - b->bytes) {
    b->row_over = b->full = b->dropped = true;
    return false;
  }
  b->bytes += n;
  return true;
}

// Upper bounds for the text form of non-string values (DOM ctx stages them as text). DOUBLE
// values are charged their exact length instead (double_text_bytes).
constexpr size_t kNumberBytes = 24, kDecimalBytes = 88, kTimeBytes = 32;

// The length of a DOUBLE as the DOM consumer formats it: in full with a fixed number of
// decimals (my_fcvt, up to FLOATING_POINT_BUFFER bytes), otherwise in %g style (my_gcvt).
size_t double_text_bytes(double v, unsigned int decimals) {
  char buf[FLOATING_POINT_BUFFER + 1];
  return decimals < DECIMAL_NOT_SPECIFIED
             ? my_fcvt(v, static_cast<int>(decimals), buf, nullptr)
             : my_gcvt(v, MY_GCVT_ARG_FLOAT, FLOATING_POINT_BUFFER, buf, nullptr);
}
// What the DOM consumer keeps besides the values: a pointer per cell (NULL cells included,
// so a wide result of NULLs is bounded too), and per row its list node and end pointer.
constexpr size_t kCellBytes = sizeof(char *), kRowBytes = 4 * sizeof(char *);

// True: the value belongs to a row the DOM consumer should not see. bytes() gives the value's
// size; it runs only for a value of a passed-on row that is still within the budget.
template <typename Bytes>
bool skip_value_sized(Bytes bytes) {
  RowBudget *b = tl_budget;
  return b != nullptr && (!b->in_row || b->row_over || !take(kCellBytes + bytes()));
}

bool skip_value(size_t n) {
  return skip_value_sized([n] { return n; });
}

DEFINE_BOOL_METHOD(bounded_start_row, (SRV_CTX_H ctx)) {
  RowBudget *b = tl_budget;
  if (b == nullptr) return dom_row_factory->start_row(ctx);
  b->row_over = false;
  if (b->full) {
    b->in_row = false;
    b->dropped = true;
    return false;
  }
  b->in_row = true;
  b->row_start = b->bytes;
  take(kRowBytes);  // over budget, the row is marked and end_row() aborts it
  return dom_row_factory->start_row(ctx);
}

DEFINE_BOOL_METHOD(bounded_abort_row, (SRV_CTX_H ctx)) {
  RowBudget *b = tl_budget;
  if (b == nullptr) return dom_row_factory->abort_row(ctx);
  if (!b->in_row) return false;
  b->in_row = false;
  return dom_row_factory->abort_row(ctx);
}

DEFINE_BOOL_METHOD(bounded_end_row, (SRV_CTX_H ctx)) {
  RowBudget *b = tl_budget;
  if (b == nullptr) return dom_row_factory->end_row(ctx);
  if (!b->in_row) return false;
  b->in_row = false;
  if (b->row_over) return dom_row_factory->abort_row(ctx);
  b->kept_bytes += b->bytes - b->row_start;
  if (++b->kept >= b->max_rows) b->full = true;
  return dom_row_factory->end_row(ctx);
}

DEFINE_BOOL_METHOD(bounded_get_null, (SRV_CTX_H ctx)) {
  return skip_value(0) ? false : dom_get_null->get(ctx);
}

DEFINE_BOOL_METHOD(bounded_get_integer, (SRV_CTX_H ctx, long long v)) {
  return skip_value(kNumberBytes) ? false : dom_get_integer->get(ctx, v);
}

DEFINE_BOOL_METHOD(bounded_get_longlong, (SRV_CTX_H ctx, long long v, unsigned int is_unsigned)) {
  return skip_value(kNumberBytes) ? false : dom_get_longlong->get(ctx, v, is_unsigned);
}

DEFINE_BOOL_METHOD(bounded_get_decimal, (SRV_CTX_H ctx, const DECIMAL_T_H v)) {
  return skip_value(kDecimalBytes) ? false : dom_get_decimal->get(ctx, v);
}

DEFINE_BOOL_METHOD(bounded_get_double, (SRV_CTX_H ctx, double v, unsigned int decimals)) {
  // Formatting costs time: dropped rows skip it.
  return skip_value_sized([&] { return double_text_bytes(v, decimals); })
             ? false
             : dom_get_double->get(ctx, v, decimals);
}

DEFINE_BOOL_METHOD(bounded_get_date, (SRV_CTX_H ctx, const MYSQL_TIME_H v)) {
  return skip_value(kTimeBytes) ? false : dom_get_date_time->get_date(ctx, v);
}

DEFINE_BOOL_METHOD(bounded_get_time, (SRV_CTX_H ctx, const MYSQL_TIME_H v, unsigned int p)) {
  return skip_value(kTimeBytes) ? false : dom_get_date_time->get_time(ctx, v, p);
}

DEFINE_BOOL_METHOD(bounded_get_datetime, (SRV_CTX_H ctx, const MYSQL_TIME_H v, unsigned int p)) {
  return skip_value(kTimeBytes) ? false : dom_get_date_time->get_datatime(ctx, v, p);
}

DEFINE_BOOL_METHOD(bounded_get_string,
                   (SRV_CTX_H ctx, const char *const v, size_t n, const char *const coll)) {
  return skip_value(n) ? false : dom_get_string->get_string(ctx, v, n, coll);
}

// The option and service name pairs a run_as() session sets.
constexpr std::pair<int, const char *> kBoundedConsumer[] = {
    {MYSQL_TEXT_CONSUMER_ROW_FACTORY, "mysql_text_consumer_row_factory_v1.mcp"},
    {MYSQL_TEXT_CONSUMER_GET_NULL, "mysql_text_consumer_get_null_v1.mcp"},
    {MYSQL_TEXT_CONSUMER_GET_INTEGER, "mysql_text_consumer_get_integer_v1.mcp"},
    {MYSQL_TEXT_CONSUMER_GET_LONGLONG, "mysql_text_consumer_get_longlong_v1.mcp"},
    {MYSQL_TEXT_CONSUMER_GET_DECIMAL, "mysql_text_consumer_get_decimal_v1.mcp"},
    {MYSQL_TEXT_CONSUMER_GET_DOUBLE, "mysql_text_consumer_get_double_v1.mcp"},
    {MYSQL_TEXT_CONSUMER_GET_DATE_TIME, "mysql_text_consumer_get_date_time_v1.mcp"},
    {MYSQL_TEXT_CONSUMER_GET_STRING, "mysql_text_consumer_get_string_v1.mcp"},
};

// Sets tl_budget for the calling thread while a statement runs.
class BudgetScope {
 public:
  explicit BudgetScope(RowBudget *b) { tl_budget = b; }
  ~BudgetScope() { tl_budget = nullptr; }
  BudgetScope(const BudgetScope &) = delete;
  BudgetScope &operator=(const BudgetScope &) = delete;
};

struct SqlResult {
  bool ok = false;
  unsigned int sql_errno = 0;
  std::string error;
  rapidjson::Document rows{rapidjson::kArrayType};  // first result set, as objects
  bool truncated = false;
};

bool valid_utf8(const char *s, size_t n) {
  const auto *p = reinterpret_cast<const unsigned char *>(s);
  size_t i = 0;
  while (i < n) {
    const unsigned char c = p[i];
    size_t len;
    unsigned int cp;
    if (c < 0x80) {
      ++i;
      continue;
    } else if ((c & 0xE0) == 0xC0) {
      len = 2, cp = c & 0x1F;
    } else if ((c & 0xF0) == 0xE0) {
      len = 3, cp = c & 0x0F;
    } else if ((c & 0xF8) == 0xF0) {
      len = 4, cp = c & 0x07;
    } else {
      return false;
    }
    if (i + len > n) return false;
    for (size_t k = 1; k < len; ++k) {
      if ((p[i + k] & 0xC0) != 0x80) return false;
      cp = (cp << 6) | (p[i + k] & 0x3F);
    }
    // No overlong forms, surrogates or code points past U+10FFFF.
    if ((len == 2 && cp < 0x80) || (len == 3 && cp < 0x800) || (len == 4 && cp < 0x10000) ||
        (cp >= 0xD800 && cp <= 0xDFFF) || cp > 0x10FFFF)
      return false;
    i += len;
  }
  return true;
}

// A result cell: a JSON string when its bytes are valid UTF-8, otherwise
// {"base64": "<the bytes>"}, so a binary value never produces invalid JSON.
void cell_value(const char *s, size_t n, rapidjson::Value *v,
                rapidjson::Document::AllocatorType &a) {
  if (valid_utf8(s, n)) {
    v->SetString(s, n, a);
    return;
  }
  std::string b64(4 * ((n + 2) / 3) + 1, '\0');
  const int len = EVP_EncodeBlock(reinterpret_cast<unsigned char *>(b64.data()),
                                  reinterpret_cast<const unsigned char *>(s), static_cast<int>(n));
  b64.resize(len > 0 ? static_cast<size_t>(len) : 0);
  v->SetObject();
  v->AddMember("base64", rapidjson::Value(b64.c_str(), a), a);
}

size_t serialized_size(const rapidjson::Value &v) {
  rapidjson::StringBuffer b;
  rapidjson::Writer<rapidjson::StringBuffer> w(b);
  v.Accept(w);
  return b.GetSize();
}

// Column masking, as mysql-mcp-server's MYSQL_MCP_MASK_COLUMNS: a result column whose name
// contains one of the mcp_http.mask_columns patterns (Unicode case-insensitive; patterns are
// trimmed of Unicode whitespace and empty ones ignored, as Go's strings.TrimSpace) has every
// non-NULL value replaced by "********".
constexpr char kMasked[] = "********";

// Unicode lowercase of UTF-8 text with the server's utf8mb4 case mapping (Go: strings.ToLower).
std::string unicode_lower(const std::string &s) {
  const CHARSET_INFO *cs = &my_charset_utf8mb4_0900_ai_ci;
  std::string src = s;  // casedn takes a mutable source
  // Lowercase can be longer than the source (U+023E is 2 bytes, its lowercase 3), although the
  // charset reports casedn_multiply = 1: no character becomes more than 4 bytes.
  std::string out(4 * s.size(), '\0');
  out.resize(cs->cset->casedn(cs, src.data(), src.size(), out.data(), out.size()));
  return out;
}

// Whether the UTF-8 sequence p[0, len) is whitespace as Go's unicode.IsSpace: ASCII \t-\r and
// space, U+0085, U+00A0, U+1680, U+2000-U+200A, U+2028, U+2029, U+202F, U+205F, U+3000.
bool unicode_space(const unsigned char *p, size_t len) {
  if (len == 1) return p[0] == ' ' || (p[0] >= '\t' && p[0] <= '\r');
  if (len == 2) return p[0] == 0xC2 && (p[1] == 0x85 || p[1] == 0xA0);
  if (len != 3) return false;
  const unsigned cp = ((p[0] & 0x0F) << 12) | ((p[1] & 0x3F) << 6) | (p[2] & 0x3F);
  return cp == 0x1680 || (cp >= 0x2000 && cp <= 0x200A) || cp == 0x2028 || cp == 0x2029 ||
         cp == 0x202F || cp == 0x205F || cp == 0x3000;
}

// Length of the UTF-8 sequence starting with lead byte c (1 for a stray byte).
size_t utf8_len(unsigned char c) { return c >= 0xF0 ? 4 : c >= 0xE0 ? 3 : c >= 0xC0 ? 2 : 1; }

// s[x, y) trimmed of Unicode whitespace at both ends.
std::string unicode_trim(const std::string &s, size_t x, size_t y) {
  const auto *p = reinterpret_cast<const unsigned char *>(s.data());
  while (x < y) {
    const size_t n = utf8_len(p[x]);
    if (x + n > y || !unicode_space(p + x, n)) break;
    x += n;
  }
  while (y > x) {
    size_t start = y - 1;  // back to the lead byte of the last character
    while (start > x && (p[start] & 0xC0) == 0x80) --start;
    if (start + utf8_len(p[start]) != y || !unicode_space(p + start, y - start)) break;
    y = start;
  }
  return s.substr(x, y - x);
}

std::vector<std::string> mask_patterns() {
  std::vector<std::string> out;
  if (opt_mask_columns == nullptr) return out;
  const std::string all = opt_mask_columns;
  size_t b = 0;
  while (b <= all.size()) {
    size_t e = all.find(',', b);
    if (e == std::string::npos) e = all.size();
    const std::string pattern = unicode_trim(all, b, e);
    if (!pattern.empty()) out.push_back(unicode_lower(pattern));
    b = e + 1;
  }
  return out;
}

bool masked_column(const std::string &name, const std::vector<std::string> &patterns) {
  const std::string lower = unicode_lower(name);
  for (const auto &p : patterns)
    if (lower.find(p) != std::string::npos) return true;
  return false;
}

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

// The token table, mysql.mcp_tokens (src/mcp/endpoint/mcp_tokens.sql): token hashes mapped to
// accounts, optionally expiring. The endpoint reads it in a session impersonating the locked
// account mysql.mcp@localhost, whose only grant is SELECT on that table; MCP sessions cannot read
// it (the guard denies the mysql schema to them).
constexpr char kTokenReaderUser[] = "mysql.mcp", kTokenReaderHost[] = "localhost";

// The account (user@host) a token hash maps to in mysql.mcp_tokens, if any and not expired.
// hash is the endpoint's own hex SHA-256, so it is safe in the statement text.
std::optional<std::string> table_token_account(const std::string &hash) {
  MYSQL_H h = nullptr;
  if (mysql_service_mysql_command_factory->init(&h) || h == nullptr) return std::nullopt;
  static constexpr bool kBypassMaxConnections = false;
  std::optional<std::string> account;
  if (!mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_PROTOCOL, "local") &&
      !mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_USER_NAME, kTokenReaderUser) &&
      !mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_HOST_NAME, kTokenReaderHost) &&
      !mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_BYPASS_MAX_CONNECTIONS,
                                                &kBypassMaxConnections) &&
      !mysql_service_mysql_command_factory->connect(h)) {
    const std::string sql =
        "SELECT account FROM mysql.mcp_tokens WHERE token_sha256 = '" + hash +
        "' AND (expires_at IS NULL OR expires_at > NOW())";
    MYSQL_RES_H res = nullptr;
    MYSQL_ROW_H row = nullptr;
    ulong *lengths = nullptr;
    if (!mysql_service_mysql_command_query->query(h, sql.data(), sql.size()) &&
        !mysql_service_mysql_command_query_result->store_result(h, &res) && res != nullptr) {
      if (!mysql_service_mysql_command_query_result->fetch_row(res, &row) && row != nullptr &&
          row[0] != nullptr &&
          !mysql_service_mysql_command_query_result->fetch_lengths(res, &lengths) && lengths)
        account.emplace(row[0], lengths[0]);
      mysql_service_mysql_command_query_result->free_result(res);
    }
  }
  mysql_service_mysql_command_factory->close(h);
  return account;
}

// The account the request's bearer token maps to: mcp_http.token_sha256 (to mcp_http.account)
// first, then the token table. nullopt: 401.
std::optional<std::string> token_account(const std::string *authorization) {
  const std::string hash = bearer_sha256(authorization);
  if (hash.empty()) return std::nullopt;
  if (single_token_ok(hash) && opt_account != nullptr && opt_account[0] != '\0')
    return std::string(opt_account);
  return table_token_account(hash);
}

// Per-account count of open endpoint sessions. The server does not apply max_user_connections
// to command-service sessions, so mcp_http.max_sessions_per_account caps them here.
class SessionSlot {
 public:
  explicit SessionSlot(const std::string &account) : account_(account) {
    std::lock_guard<std::mutex> lock(mutex_);
    unsigned long &n = open_[account_];
    if (n >= opt_max_sessions_per_account) return;
    ++n;
    held_ = true;
  }
  ~SessionSlot() {
    if (!held_) return;
    std::lock_guard<std::mutex> lock(mutex_);
    auto it = open_.find(account_);
    if (--it->second == 0) open_.erase(it);
  }
  SessionSlot(const SessionSlot &) = delete;
  SessionSlot &operator=(const SessionSlot &) = delete;
  bool held() const { return held_; }

 private:
  static inline std::mutex mutex_;
  static inline std::map<std::string, unsigned long> open_;
  std::string account_;
  bool held_ = false;
};

// Impersonation skips the login checks, ACCOUNT LOCK included, so the endpoint refuses a
// locked account itself. The session's security context records the lock (the account lookup
// at connect sets it). Fails closed: a context that cannot be read counts as locked.
bool account_locked(MYSQL_THD thd) {
  Security_context_handle sctx = nullptr;
  bool locked = true;
  if (mysql_service_mysql_thd_security_context->get(thd, &sctx) || sctx == nullptr ||
      mysql_service_mysql_security_context_options->get(sctx, "account_locked", &locked))
    return true;
  return locked;
}

// RFC 0001 §7.4: impersonation makes the endpoint fully trusted, so a token must never run as
// an account that could undo the guard or administer the server. The privilege the session's
// account holds, out of SUPER, SYSTEM_USER and MCP_ADMIN, or "" for none. Checked on every
// call, whichever token source mapped it. Fails closed: a context that cannot be read counts.
std::string forbidden_privilege(MYSQL_THD thd) {
  Security_context_handle sctx = nullptr;
  if (mysql_service_mysql_thd_security_context->get(thd, &sctx) || sctx == nullptr)
    return "(unreadable security context)";
  bool super = true;
  if (mysql_service_mysql_security_context_options->get(sctx, "privilege_super", &super) || super)
    return "SUPER";
  for (const char *priv : {"SYSTEM_USER", "MCP_ADMIN"})
    if (mysql_service_global_grants_check->has_global_grant(sctx, priv, std::strlen(priv)))
      return priv;
  return "";
}

bool set_bounded_consumer(MYSQL_H h) {
  for (const auto &[option, service] : kBoundedConsumer)
    if (mysql_service_mysql_command_options->set(h, option, service)) return false;
  return true;
}

// What the bounded consumer kept for the last statement an endpoint call ran (any tool, any
// session): rows passed on to the DOM consumer and the bytes charged for them, aborted rows
// excluded, and, observed independently, the rows the DOM consumer stored for the first result
// (its MYSQL_RES row count). Status variables, so tests and operators can check the bounds
// exactly rather than through process memory. One statement's values are published together.
struct Retained {
  long long rows = 0, bytes = 0, stored = 0;
};
std::mutex retained_mutex;
Retained last_retained;

void publish_retained(const Retained &r) {
  std::lock_guard<std::mutex> lock(retained_mutex);
  last_retained = r;
}

template <long long Retained::*field>
int show_retained(MYSQL_THD, SHOW_VAR *var, char *buf) {
  var->type = SHOW_LONGLONG;
  var->value = buf;
  std::lock_guard<std::mutex> lock(retained_mutex);
  *reinterpret_cast<long long *>(buf) = last_retained.*field;
  return 0;
}

SHOW_VAR status_vars[] = {
    {"mcp_http.last_rows_retained", reinterpret_cast<char *>(&show_retained<&Retained::rows>),
     SHOW_FUNC, SHOW_SCOPE_GLOBAL},
    {"mcp_http.last_bytes_retained", reinterpret_cast<char *>(&show_retained<&Retained::bytes>),
     SHOW_FUNC, SHOW_SCOPE_GLOBAL},
    {"mcp_http.last_rows_stored", reinterpret_cast<char *>(&show_retained<&Retained::stored>),
     SHOW_FUNC, SHOW_SCOPE_GLOBAL},
    {nullptr, nullptr, SHOW_LONGLONG, SHOW_SCOPE_GLOBAL}};
bool status_registered = false;

// Runs sql under a fresh row budget; *truncated reports whether rows were dropped, *retained
// what the budget kept.
bool bounded_query(MYSQL_H h, const std::string &sql, bool *truncated, Retained *retained) {
  RowBudget budget;
  BudgetScope scope(&budget);
  const bool failed = mysql_service_mysql_command_query->query(h, sql.data(), sql.size());
  *truncated = budget.dropped;
  retained->rows = static_cast<long long>(budget.kept);
  retained->bytes = static_cast<long long>(budget.kept_bytes);
  return failed;
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
  SessionSlot slot(account);
  if (!slot.held()) {
    r.sql_errno = ER_TOO_MANY_USER_CONNECTIONS;
    r.error = "User " + user + " already has " + std::to_string(opt_max_sessions_per_account) +
              " active endpoint sessions (mcp_http.max_sessions_per_account)";
    return r;
  }
  MYSQL_H h = nullptr;
  if (mysql_service_mysql_command_factory->init(&h) || h == nullptr) {
    r.error = "cannot create a session";
    return r;
  }
  // CALL needs multi-result support; multi-statements stay off, so "a; b" is a parse error.
  // Client protocol capabilities (mysql_com.h, which components cannot include).
  constexpr uint32_t kClientMultiResults = 1UL << 17, kClientPsMultiResults = 1UL << 18;
  const uint32_t client_flags = kClientMultiResults | kClientPsMultiResults;
  // Unlike the command service's default, the sessions are subject to max_connections.
  static constexpr bool kBypassMaxConnections = false;
  if (mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_PROTOCOL, "local") ||
      mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_CLIENT_FLAGS, &client_flags) ||
      mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_USER_NAME, user.c_str()) ||
      mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_HOST_NAME, host.c_str()) ||
      mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_BYPASS_MAX_CONNECTIONS,
                                                &kBypassMaxConnections) ||
      !set_bounded_consumer(h) || mysql_service_mysql_command_factory->connect(h)) {
    read_error(h, &r);
    if (r.error.empty()) r.error = "cannot open a session for " + account;
  } else {
    // The session starts unaudited: CONNECT turns auditing on, so the guard sees it.
    MYSQL_THD thd = session_thd(h);
    static const std::string kLimit =
        "SET SESSION sql_select_limit = " + std::to_string(kMaxRows + 1);
    // DISCONNECT follows CONNECT whatever CONNECT returned, so the guard releases its state.
    bool announced = false;
    const auto announce = [&] {
      announced = true;
      return mysql_service_mysql_audit_api_connection->emit(thd, MYSQL_AUDIT_CONNECTION_CONNECT) ==
             0;
    };
    if (thd == nullptr) {
      r.error = "session refused";
    } else if (account_locked(thd)) {
      r.sql_errno = ER_ACCOUNT_HAS_BEEN_LOCKED;
      r.error = "Access denied for user '" + user + "'@'" + host + "'. Account is locked.";
    } else if (const std::string priv = forbidden_privilege(thd); !priv.empty()) {
      r.sql_errno = ER_SPECIFIC_ACCESS_DENIED_ERROR;
      r.error = "Access denied for user '" + user + "'@'" + host +
                "': the MCP endpoint does not run as an account with SUPER, SYSTEM_USER or "
                "MCP_ADMIN (it has " + priv + ")";
    } else if (!announce()) {
      r.error = "session refused";
    } else if (mysql_service_mysql_command_query->query(h, kLimit.data(), kLimit.size())) {
      read_error(h, &r);
    } else if (Retained retained; bounded_query(h, sql, &r.truncated, &retained)) {
      publish_retained(retained);
      read_error(h, &r);
    } else {
      r.ok = true;
      MYSQL_RES_H res = nullptr;
      const bool stored =
          !mysql_service_mysql_command_query_result->store_result(h, &res) && res != nullptr;
      if (stored) retained.stored = static_cast<long long>(
                      reinterpret_cast<Mysql_res_handle *>(res)->mysql_res->row_count);
      publish_retained(retained);
      if (stored) {
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
        const std::vector<std::string> patterns = mask_patterns();
        std::vector<bool> masked(n);
        for (unsigned int i = 0; i < n; ++i) masked[i] = masked_column(names[i], patterns);
        auto &a = r.rows.GetAllocator();
        MYSQL_ROW_H row = nullptr;
        ulong *lengths = nullptr;
        size_t bytes = 0;
        while (!mysql_service_mysql_command_query_result->fetch_row(res, &row) && row) {
          mysql_service_mysql_command_query_result->fetch_lengths(res, &lengths);
          if (r.rows.Size() >= kMaxRows) {
            r.truncated = true;
            break;
          }
          rapidjson::Value obj(rapidjson::kObjectType);
          for (unsigned int i = 0; i < n; ++i) {
            rapidjson::Value k(names[i].c_str(), a), v;
            if (row[i] != nullptr && masked[i])
              v.SetString(kMasked, sizeof(kMasked) - 1);
            else if (row[i] != nullptr)
              cell_value(row[i], lengths ? lengths[i] : std::strlen(row[i]), &v, a);
            obj.AddMember(k, v, a);
          }
          // The budget counts the row as serialized (escapes included), and a row that does
          // not fit ends the result, so the reply stays within the bounds.
          const size_t row_bytes = serialized_size(obj);
          if (bytes + row_bytes > kMaxBytes) {
            r.truncated = true;
            break;
          }
          bytes += row_bytes;
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
    if (announced && thd2 != nullptr)
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

// A SQL expression for a text argument: a hex literal converted to utf8mb4, so the value needs
// no escaping and means the same under any sql_mode (NO_BACKSLASH_ESCAPES included).
std::string text_literal(const std::string &s) {
  static const char kHex[] = "0123456789ABCDEF";
  std::string out = "CONVERT(X'";
  for (const unsigned char c : s) {
    out += kHex[c >> 4];
    out += kHex[c & 15];
  }
  return out + "' USING utf8mb4)";
}

// The string argument key of a tools/call, or nullopt when it is missing or not a string.
std::optional<std::string> string_arg(const rapidjson::Value &params, const char *key) {
  if (!params.HasMember("arguments") || !params["arguments"].IsObject()) return std::nullopt;
  const rapidjson::Value &args = params["arguments"];
  if (!args.HasMember(key) || !args[key].IsString()) return std::nullopt;
  return std::string(args[key].GetString(), args[key].GetStringLength());
}

void call_tool(const rapidjson::Value &params, Json &out, const std::string &account) {
  const std::string name =
      params.HasMember("name") && params["name"].IsString() ? params["name"].GetString() : "";
  std::string sql;
  if (name == "list_databases") {
    sql = "CALL mysql_mcp.list_databases()";
  } else if (name == "list_tables") {
    const auto database = string_arg(params, "database");
    if (!database) {
      tool_result(out, "list_tables needs a string argument database", true);
      return;
    }
    sql = "CALL mysql_mcp.list_tables(" + text_literal(*database) + ")";
  } else if (name == "describe_table") {
    const auto database = string_arg(params, "database"), table = string_arg(params, "table");
    if (!database || !table) {
      tool_result(out, "describe_table needs string arguments database and table", true);
      return;
    }
    sql = "CALL mysql_mcp.describe_table(" + text_literal(*database) + ", " +
          text_literal(*table) + ")";
  } else if (name == "run_query") {
    const auto query = string_arg(params, "sql");
    if (!query) {
      tool_result(out, "run_query needs a string argument sql", true);
      return;
    }
    sql = *query;
  } else if (name == "ping") {
    // As mysql-mcp-server's ping: a failure is a result (success false), not a tool error.
    const auto start = std::chrono::steady_clock::now();
    const SqlResult r = run_as(account, "SELECT 1");
    const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                        std::chrono::steady_clock::now() - start)
                        .count();
    Json reply(rapidjson::kObjectType);
    auto &a = reply.GetAllocator();
    reply.AddMember("success", r.ok, a);
    reply.AddMember("latency_ms", static_cast<int64_t>(ms), a);
    const std::string message =
        r.ok ? "pong" : "ping failed: Error " + std::to_string(r.sql_errno) + ": " + r.error;
    reply.AddMember("message", rapidjson::Value(message.c_str(), a), a);
    tool_result(out, to_string(reply), false);
    return;
  } else if (name == "server_info") {
    sql =
        "SELECT VERSION() AS version, 'mysql' AS server_engine, "
        "@@version_comment AS version_comment, CURRENT_USER() AS `current_user`, "
        "IFNULL(DATABASE(), '') AS current_database, "  // "" as mysql-mcp-server: no default db
        "@@character_set_server AS character_set, "
        "@@collation_server AS collation, @@max_connections AS max_connections";
  } else {
    tool_result(out, "unknown tool: " + name, true);
    return;
  }
  SqlResult r = run_as(account, sql);
  if (!r.ok) {
    tool_result(out, "Error " + std::to_string(r.sql_errno) + ": " + r.error, true);
    return;
  }
  if (name == "server_info" && r.rows.Size() == 1) {  // one object, as mysql-mcp-server
    tool_result(out, to_string(r.rows[0]), false);
    return;
  }
  Json reply(rapidjson::kObjectType);
  reply.AddMember("rows", rapidjson::Value(r.rows, reply.GetAllocator()), reply.GetAllocator());
  reply.AddMember("truncated", r.truncated, reply.GetAllocator());
  tool_result(out, to_string(reply), false);
}

void list_tools(Json &out) {
  // The catalog and status tools only read (MCP tool annotations). run_query is not marked:
  // the guard keeps it read-only only while mcp.read_only is on.
#define MCP_READ_ONLY R"("annotations":{"readOnlyHint":true,"idempotentHint":true},)"
  static const char kTools[] =
      R"({"tools":[)"
      R"({"name":"list_databases","description":"List databases visible to the mapped account",)" MCP_READ_ONLY
      R"("inputSchema":{"type":"object","properties":{}}},)"
      R"({"name":"list_tables","description":"List the tables of a database",)" MCP_READ_ONLY
      R"("inputSchema":{"type":"object","properties":{"database":{"type":"string"}},)"
      R"("required":["database"]}},)"
      R"({"name":"describe_table","description":"Describe the columns of a table",)" MCP_READ_ONLY
      R"("inputSchema":{"type":"object","properties":{"database":{"type":"string"},)"
      R"("table":{"type":"string"}},"required":["database","table"]}},)"
      R"({"name":"run_query","description":"Run one read-only SQL statement",)"
      R"("inputSchema":{"type":"object","properties":{"sql":{"type":"string"}},"required":["sql"]}},)"
      R"({"name":"ping","description":"Check that the server answers, with the round-trip time",)" MCP_READ_ONLY
      R"("inputSchema":{"type":"object","properties":{}}},)"
      R"({"name":"server_info","description":"Server version, character set and limits",)" MCP_READ_ONLY
      R"("inputSchema":{"type":"object","properties":{}}}]})";
#undef MCP_READ_ONLY
  Json tools;
  tools.Parse(kTools);
  out.AddMember("result", rapidjson::Value(tools, out.GetAllocator()), out.GetAllocator());
}

// ----- Streamable HTTP: protocol versions and sessions --------------------------------------

// Two protocol eras share the endpoint (MCP 2026-07-28, "Versioning and Compatibility"):
//   - the session-based revisions (initialize, then Mcp-Session-Id), as mysql-mcp-server's default
//     stateful transport;
//   - the stateless revision 2026-07-28 (SEP-2575): no handshake; every request carries its
//     protocol version and client capabilities in _meta, mirrored in HTTP headers.
// A request is in the stateless era when it carries _meta io.modelcontextprotocol/protocolVersion
// or the MCP-Protocol-Version header 2026-07-28; a header naming a version not served gets
// -32022 before anything else. The component is stateless per call either way: each tool call runs in its own
// impersonated session.
constexpr const char *kProtocolVersions[] = {"2025-11-25", "2025-06-18", "2025-03-26",
                                             "2024-11-05"};
constexpr const char kStatelessVersion[] = "2026-07-28";
constexpr const char kServerName[] = "component_mcp";
constexpr const char kServerVersion[] = "0.1.0";
constexpr const char kMetaProtocolVersion[] = "io.modelcontextprotocol/protocolVersion";
constexpr const char kMetaClientCapabilities[] = "io.modelcontextprotocol/clientCapabilities";
constexpr const char kMetaClientInfo[] = "io.modelcontextprotocol/clientInfo";
constexpr const char kMetaServerInfo[] = "io.modelcontextprotocol/serverInfo";
// tools/list and server/discover results may be cached this long (ms). The tool list is the
// same for every account, so the cache scope is public.
constexpr int kListTtlMs = 3600000;

// JSON-RPC error codes of MCP 2026-07-28.
constexpr int kInvalidRequest = -32600;
constexpr int kMethodNotFound = -32601;
constexpr int kInvalidParams = -32602;
constexpr int kHeaderMismatch = -32020;
constexpr int kUnsupportedProtocolVersion = -32022;

bool supported_version(const std::string &v) {
  for (const char *s : kProtocolVersions)
    if (v == s) return true;
  return false;
}

// Every version served, newest first.
std::vector<std::string> all_versions() {
  std::vector<std::string> out{kStatelessVersion};
  for (const char *s : kProtocolVersions) out.emplace_back(s);
  return out;
}

std::string supported_versions() {
  std::string out;
  for (const std::string &s : all_versions()) out += (out.empty() ? "" : ", ") + s;
  return out;
}

rapidjson::Value versions_array(rapidjson::Document::AllocatorType &a) {
  rapidjson::Value out(rapidjson::kArrayType);
  for (const std::string &s : all_versions()) out.PushBack(rapidjson::Value(s.c_str(), a), a);
  return out;
}

rapidjson::Value server_info(rapidjson::Document::AllocatorType &a) {
  rapidjson::Value info(rapidjson::kObjectType);
  info.AddMember("name", rapidjson::StringRef(kServerName), a);
  info.AddMember("version", rapidjson::StringRef(kServerVersion), a);
  return info;
}

// Mcp-Session-Id sessions: issued by initialize, bound to the token's account, expired after
// mcp_http.session_timeout_minutes idle. A session id is not a credential: every request still
// needs its bearer token. At most kMaxSessions are kept (the least recently used is dropped).
class Sessions {
 public:
  enum class Lookup { kOk, kUnknown, kOtherAccount };

  std::string create(const std::string &account) {
    unsigned char raw[16];
    if (RAND_bytes(raw, sizeof(raw)) != 1) return "";
    static const char kHex[] = "0123456789abcdef";
    std::string id;
    for (const unsigned char c : raw) {
      id += kHex[c >> 4];
      id += kHex[c & 15];
    }
    std::lock_guard<std::mutex> lock(mutex_);
    expire_locked();
    if (sessions_.size() >= kMaxSessions) {
      auto oldest = sessions_.begin();
      for (auto it = sessions_.begin(); it != sessions_.end(); ++it)
        if (it->second.last_used < oldest->second.last_used) oldest = it;
      sessions_.erase(oldest);
    }
    sessions_[id] = {account, Clock::now()};
    return id;
  }

  // Marks the session used (POST) or ends it (DELETE).
  Lookup use(const std::string &id, const std::string &account, bool end) {
    std::lock_guard<std::mutex> lock(mutex_);
    expire_locked();
    auto it = sessions_.find(id);
    if (it == sessions_.end()) return Lookup::kUnknown;
    if (it->second.account != account) return Lookup::kOtherAccount;
    if (end)
      sessions_.erase(it);
    else
      it->second.last_used = Clock::now();
    return Lookup::kOk;
  }

 private:
  using Clock = std::chrono::steady_clock;
  struct Session {
    std::string account;
    Clock::time_point last_used;
  };
  static constexpr size_t kMaxSessions = 4096;

  void expire_locked() {
    const auto deadline = Clock::now() - std::chrono::minutes(opt_session_timeout_minutes);
    for (auto it = sessions_.begin(); it != sessions_.end();)
      it = it->second.last_used < deadline ? sessions_.erase(it) : std::next(it);
  }

  std::mutex mutex_;
  std::map<std::string, Session> sessions_;
};

Sessions sessions;

// Accept lists application/json and text/event-stream (or their wildcards), as the Go SDK.
bool accepts_json_and_stream(const std::string *accept) {
  if (accept == nullptr) return false;
  bool json = false, stream = false;
  size_t b = 0;
  while (b <= accept->size()) {
    size_t e = accept->find(',', b);
    if (e == std::string::npos) e = accept->size();
    std::string t = accept->substr(b, e - b);
    t = t.substr(0, t.find(';'));
    const size_t x = t.find_first_not_of(" \t"), y = t.find_last_not_of(" \t");
    t = x == std::string::npos ? "" : t.substr(x, y - x + 1);
    for (char &c : t) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    if (t == "application/json" || t == "application/*") json = true;
    if (t == "text/event-stream" || t == "text/*") stream = true;
    if (t == "*/*") json = stream = true;
    b = e + 1;
  }
  return json && stream;
}

bool json_content_type(const std::string *type) {
  if (type == nullptr) return false;
  std::string t = type->substr(0, type->find(';'));
  const size_t x = t.find_first_not_of(" \t"), y = t.find_last_not_of(" \t");
  t = x == std::string::npos ? "" : t.substr(x, y - x + 1);
  for (char &c : t) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return t == "application/json";
}

void initialize(const rapidjson::Value &params, Json &out) {
  auto &a = out.GetAllocator();
  rapidjson::Value result(rapidjson::kObjectType), caps(rapidjson::kObjectType),
      tools(rapidjson::kObjectType);
  // Version negotiation: the client's version when supported, else the newest supported.
  const std::string asked = params.IsObject() && params.HasMember("protocolVersion") &&
                                    params["protocolVersion"].IsString()
                                ? params["protocolVersion"].GetString()
                                : "";
  const std::string version = supported_version(asked) ? asked : kProtocolVersions[0];
  result.AddMember("protocolVersion", rapidjson::Value(version.c_str(), a), a);
  caps.AddMember("tools", tools, a);
  result.AddMember("capabilities", caps, a);
  result.AddMember("serverInfo", server_info(a), a);
  out.AddMember("result", result, a);
}

void rpc_error(Json &out, int code, const std::string &message,
               rapidjson::Value *data = nullptr) {
  auto &a = out.GetAllocator();
  rapidjson::Value err(rapidjson::kObjectType);
  err.AddMember("code", code, a);
  err.AddMember("message", rapidjson::Value(message.c_str(), a), a);
  if (data != nullptr) err.AddMember("data", *data, a);
  out.AddMember("error", err, a);
}

// server/discover (2026-07-28): the versions, capabilities and identity, without a handshake.
void discover(Json &out) {
  auto &a = out.GetAllocator();
  rapidjson::Value result(rapidjson::kObjectType), caps(rapidjson::kObjectType);
  result.AddMember("supportedVersions", versions_array(a), a);
  caps.AddMember("tools", rapidjson::Value(rapidjson::kObjectType), a);
  result.AddMember("capabilities", caps, a);
  out.AddMember("result", result, a);
}

// A stateless-era result: resultType "complete", the cache hints of list results, and the
// server's identity in _meta (servers SHOULD identify themselves on every result).
void stateless_result(Json &out, bool cacheable) {
  if (!out.HasMember("result") || !out["result"].IsObject()) return;
  auto &a = out.GetAllocator();
  rapidjson::Value &result = out["result"];
  result.AddMember("resultType", "complete", a);
  if (cacheable) {
    result.AddMember("ttlMs", kListTtlMs, a);
    result.AddMember("cacheScope", "public", a);
  }
  rapidjson::Value meta(rapidjson::kObjectType);
  meta.AddMember(rapidjson::StringRef(kMetaServerInfo), server_info(a), a);
  result.AddMember("_meta", meta, a);
}

bool known_tool(const std::string &name) {
  static const char *const kNames[] = {"list_databases", "list_tables", "describe_table",
                                       "run_query",      "ping",        "server_info"};
  for (const char *n : kNames)
    if (name == n) return true;
  return false;
}

class McpHandler : public http::base::RequestHandler {
 public:
  void handle_request(http::base::Request &req) override {
    const std::string *origin = req.get_input_headers().find("Origin");
    if (!origin_ok(origin)) {
      req.send_reply(HttpStatusCode::Forbidden);
      return;
    }
    // CORS for an allowed browser origin: its replies name it, so the page may read them, and
    // its preflight (OPTIONS, sent without credentials) is answered here.
    if (origin != nullptr) {
      req.get_output_headers().add("Access-Control-Allow-Origin", origin->c_str());
      req.get_output_headers().add("Access-Control-Expose-Headers", "Mcp-Session-Id");
      req.get_output_headers().add("Vary", "Origin");
    }
    if (req.get_method() == HttpMethod::Options && origin != nullptr) {
      req.get_output_headers().add("Access-Control-Allow-Methods", "POST, DELETE");
      req.get_output_headers().add("Access-Control-Allow-Headers",
                                   "Authorization, Content-Type, Mcp-Session-Id, "
                                   "MCP-Protocol-Version, Mcp-Method, Mcp-Name");
      req.get_output_headers().add("Access-Control-Max-Age", "600");
      req.send_reply(HttpStatusCode::NoContent);
      return;
    }
    // POST carries JSON-RPC, DELETE ends a session. GET (a server-to-client SSE stream) is not
    // offered: the endpoint never sends requests or notifications of its own.
    const bool is_delete = req.get_method() == HttpMethod::Delete;
    if (req.get_method() != HttpMethod::Post && !is_delete) {
      req.get_output_headers().add("Allow", "POST, DELETE");
      req.send_reply(HttpStatusCode::MethodNotAllowed);
      return;
    }
    const std::optional<std::string> account =
        token_account(req.get_input_headers().find("Authorization"));
    if (!account) {
      req.get_output_headers().add("WWW-Authenticate", "Bearer");
      req.send_reply(HttpStatusCode::Unauthorized);
      return;
    }
    // MCP-Protocol-Version must name a version this server serves. Otherwise 400 with the
    // JSON-RPC UnsupportedProtocolVersion error (-32022) listing the supported versions, before
    // the body is read: stateless clients recognize it and retry with a supported version, and
    // session-era clients only look at the status.
    const std::string *header_version = req.get_input_headers().find("MCP-Protocol-Version");
    if (header_version != nullptr && !supported_version(*header_version) &&
        *header_version != kStatelessVersion) {
      reply(req, nullptr, [&](Json &out) {
        auto &a = out.GetAllocator();
        rapidjson::Value data(rapidjson::kObjectType);
        data.AddMember("supported", versions_array(a), a);
        data.AddMember("requested", rapidjson::Value(header_version->c_str(), a), a);
        rpc_error(out, kUnsupportedProtocolVersion,
                  "Unsupported protocol version (supported versions: " + supported_versions() + ")",
                  &data);
      }, HttpStatusCode::BadRequest, /*omit_id=*/true);
      return;
    }
    // The stateless era has no sessions: DELETE does not exist in it (405, leaving any session
    // alone), and its requests' Mcp-Session-Id, if any, is ignored.
    const bool stateless_era = header_version != nullptr && *header_version == kStatelessVersion;
    if (is_delete && stateless_era) {
      req.get_output_headers().add("Allow", "POST");
      req.send_reply(HttpStatusCode::MethodNotAllowed);
      return;
    }
    const std::string *session_id =
        stateless_era ? nullptr : req.get_input_headers().find("Mcp-Session-Id");
    if (is_delete && session_id == nullptr) {
      plain(req, HttpStatusCode::BadRequest, "Bad Request: DELETE requires an Mcp-Session-Id header");
      return;
    }
    // A request without a session id is served on its own; one with an id must name a live
    // session of the same account.
    if (session_id != nullptr) {
      switch (sessions.use(*session_id, *account, is_delete)) {
        case Sessions::Lookup::kUnknown:
          plain(req, HttpStatusCode::NotFound, "session not found");
          return;
        case Sessions::Lookup::kOtherAccount:
          plain(req, HttpStatusCode::Forbidden, "session user mismatch");
          return;
        case Sessions::Lookup::kOk:
          break;
      }
    }
    if (is_delete) {
      req.send_reply(HttpStatusCode::NoContent);
      return;
    }
    if (!json_content_type(req.get_input_headers().find("Content-Type"))) {
      plain(req, HttpStatusCode::UnsupportedMediaType, "Content-Type must be 'application/json'");
      return;
    }
    if (!accepts_json_and_stream(req.get_input_headers().find("Accept"))) {
      plain(req, HttpStatusCode::BadRequest,
            "Accept must contain both 'application/json' and 'text/event-stream'");
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
    const rapidjson::Value *meta = params.IsObject() && params.HasMember("_meta") &&
                                           params["_meta"].IsObject()
                                       ? &params["_meta"]
                                       : nullptr;
    if (stateless_era || (meta != nullptr && meta->HasMember(kMetaProtocolVersion))) {
      if (serve_stateless(req, in, method, params, meta, header_version, *account)) return;
      // A session-era version in _meta, matching the header: served as that era (as the Go SDK).
    }
    if (method == "initialize" && session_id == nullptr) {
      const std::string id = sessions.create(*account);
      if (!id.empty()) req.get_output_headers().add("Mcp-Session-Id", id.c_str());
    }
    reply(req, &in["id"], [&](Json &out) {
      if (method == "initialize")
        initialize(params, out);
      else if (method == "tools/list")
        list_tools(out);
      else if (method == "tools/call" && params.IsObject())
        call_tool(params, out, *account);
      else if (method == "ping")
        out.AddMember("result", rapidjson::Value(rapidjson::kObjectType), out.GetAllocator());
      else
        rpc_error(out, kMethodNotFound, "method not found");
    });
  }

 private:
  // A request of the stateless era (MCP 2026-07-28): validates its _meta and the HTTP headers
  // that mirror the body, then serves server/discover, tools/list and tools/call. Returns false,
  // having replied nothing, when the request names a session-era version consistently in the
  // header and _meta; the caller serves it as that era.
  static bool serve_stateless(http::base::Request &req, const Json &in, const std::string &method,
                              const rapidjson::Value &params, const rapidjson::Value *meta,
                              const std::string *header_version, const std::string &account) {
    const rapidjson::Value &id = in["id"];
    const auto fail = [&](int status, int code, const std::string &message,
                          rapidjson::Value *data = nullptr) {
      reply(req, &id, [&](Json &out) { rpc_error(out, code, message, data); }, status);
      return true;
    };
    const rapidjson::Value *meta_version =
        meta != nullptr && meta->HasMember(kMetaProtocolVersion) ? &(*meta)[kMetaProtocolVersion]
                                                                 : nullptr;
    if (header_version == nullptr)
      return fail(HttpStatusCode::BadRequest, kHeaderMismatch,
                  std::string("MCP-Protocol-Version header is required for requests carrying _meta ") +
                      kMetaProtocolVersion);
    if (meta_version == nullptr || !meta_version->IsString())
      return fail(HttpStatusCode::BadRequest, kInvalidParams,
                  std::string("missing or invalid _meta field ") + kMetaProtocolVersion);
    const std::string version(meta_version->GetString(), meta_version->GetStringLength());
    if (version != *header_version)
      return fail(HttpStatusCode::BadRequest, kHeaderMismatch,
                  "MCP-Protocol-Version header '" + *header_version +
                      "' does not match _meta " + kMetaProtocolVersion + " '" + version + "'");
    // The header passed the version check, so a matching _meta version is served: a session-era
    // one is left to the caller, and anything else is 2026-07-28.
    if (supported_version(version)) return false;
    if (!in.HasMember("jsonrpc") || !in["jsonrpc"].IsString() ||
        std::strcmp(in["jsonrpc"].GetString(), "2.0") != 0)
      return fail(HttpStatusCode::BadRequest, kInvalidRequest, "jsonrpc must be \"2.0\"");
    if (!id.IsString() && !id.IsInt64())
      return fail(HttpStatusCode::BadRequest, kInvalidRequest,
                  "the request id must be a string or an integer");
    if (!meta->HasMember(kMetaClientCapabilities) || !(*meta)[kMetaClientCapabilities].IsObject())
      return fail(HttpStatusCode::BadRequest, kInvalidParams,
                  std::string("missing or invalid _meta field ") + kMetaClientCapabilities);
    if (meta->HasMember(kMetaClientInfo)) {
      const rapidjson::Value &info = (*meta)[kMetaClientInfo];
      if (!info.IsObject() || !info.HasMember("name") || !info["name"].IsString() ||
          !info.HasMember("version") || !info["version"].IsString())
        return fail(HttpStatusCode::BadRequest, kInvalidParams,
                    std::string("invalid _meta field ") + kMetaClientInfo);
    }
    // Mcp-Method mirrors the method on every request; Mcp-Name mirrors a tools/call's tool name.
    const std::string *method_header = req.get_input_headers().find("Mcp-Method");
    if (method_header == nullptr)
      return fail(HttpStatusCode::BadRequest, kHeaderMismatch, "Mcp-Method header is required");
    if (*method_header != method)
      return fail(HttpStatusCode::BadRequest, kHeaderMismatch,
                  "Mcp-Method header '" + *method_header + "' does not match the method '" +
                      method + "'");
    std::string tool;
    if (method == "tools/call") {
      if (!params.HasMember("name") || !params["name"].IsString())
        return fail(HttpStatusCode::BadRequest, kInvalidParams, "tools/call needs a string name");
      tool.assign(params["name"].GetString(), params["name"].GetStringLength());
      const std::string *name_header = req.get_input_headers().find("Mcp-Name");
      if (name_header == nullptr)
        return fail(HttpStatusCode::BadRequest, kHeaderMismatch,
                    "Mcp-Name header is required for tools/call");
      if (*name_header != tool)
        return fail(HttpStatusCode::BadRequest, kHeaderMismatch,
                    "Mcp-Name header '" + *name_header + "' does not match the tool name '" +
                        tool + "'");
    }
    // initialize and ping belong to the session era; every other method is unknown here.
    if (method != "server/discover" && method != "tools/list" && method != "tools/call")
      return fail(HttpStatusCode::NotFound, kMethodNotFound, "method not found: " + method);
    reply(req, &id, [&](Json &out) {
      if (method == "server/discover") {
        discover(out);
        stateless_result(out, true);
      } else if (method == "tools/list") {
        list_tools(out);
        stateless_result(out, true);
      } else if (!known_tool(tool)) {
        rpc_error(out, kInvalidParams, "Unknown tool: " + tool);
      } else {
        call_tool(params, out, account);
        stateless_result(out, false);
      }
    });
    return true;
  }

  static void plain(http::base::Request &req, int status, const std::string &text) {
    req.get_output_headers().add("Content-Type", "text/plain; charset=utf-8");
    req.send_reply(status, HttpStatusCode::get_default_status_text(status), text);
  }

  // A JSON-RPC response. A null id is written as null (as JSON-RPC 2.0 requires for a parse
  // error) unless omit_id is set: MCP 2026-07-28 errors allow an absent id but not null.
  template <typename F>
  static void reply(http::base::Request &req, const rapidjson::Value *id, F fill,
                    int status = HttpStatusCode::Ok, bool omit_id = false) {
    Json out(rapidjson::kObjectType);
    auto &a = out.GetAllocator();
    out.AddMember("jsonrpc", "2.0", a);
    rapidjson::Value idv;
    if (id != nullptr) idv.CopyFrom(*id, a);
    if (id != nullptr || !omit_id) out.AddMember("id", idv, a);
    fill(out);
    req.get_output_headers().add("Content-Type", "application/json");
    req.send_reply(status, HttpStatusCode::get_default_status_text(status), to_string(out));
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

bool nonempty(const char *s) { return s != nullptr && *s != '\0'; }

// Loopback: a literal IPv4 127.0.0.0/8 or IPv6 ::1 address, or "localhost".
bool loopback(const std::string &address) {
  if (address == "localhost") return true;
  in_addr v4{};
  if (inet_pton(AF_INET, address.c_str(), &v4) == 1) return (ntohl(v4.s_addr) >> 24) == 127;
  in6_addr v6{};
  return inet_pton(AF_INET6, address.c_str(), &v6) == 1 && IN6_IS_ADDR_LOOPBACK(&v6);
}

TlsServerContext make_tls_context() {
  TlsServerContext tls;
  if (const auto loaded = tls.load_key_and_cert(opt_ssl_key, opt_ssl_cert); !loaded)
    throw std::system_error(loaded.error(), std::string("loading mcp_http.ssl_key '") +
                                                opt_ssl_key + "' or mcp_http.ssl_cert '" +
                                                opt_ssl_cert + "'");
  if (const auto dh = tls.init_tmp_dh(""); !dh)
    throw std::system_error(dh.error(), "setting up TLS DH parameters");
  return tls;
}

bool start_server() {
  // "localhost" binds 127.0.0.1: Router would resolve the name again, and a resolver mapping it
  // to another interface would get plain HTTP off loopback.
  const std::string configured = nonempty(opt_bind_address) ? opt_bind_address : "127.0.0.1";
  const std::string address = configured == "localhost" ? "127.0.0.1" : configured;
  const bool tls = nonempty(opt_ssl_cert) || nonempty(opt_ssl_key);
  if (tls && !(nonempty(opt_ssl_cert) && nonempty(opt_ssl_key))) {
    log_error("mcp_http.ssl_cert and mcp_http.ssl_key must be set together");
    return true;
  }
  // RFC 0001 §5.3: plain HTTP only on loopback, so the bearer token never crosses a network
  // in the clear.
  if (!tls && !loopback(address)) {
    log_error("mcp_http.bind_address '" + address +
             "' is not loopback: set mcp_http.ssl_cert and mcp_http.ssl_key");
    return true;
  }
  init_router_logging();
  auto &io = IoComponent::get_instance();
  // HTTP work runs on the io threads, which run SQL through the command services.
  io.set_thread_lifecycle({[](size_t) { mysql_service_mysql_command_thread->init(); },
                           [](size_t) { mysql_service_mysql_command_thread->end(); }});
  if (!io.init(opt_threads, IoBackend::preferred())) return true;
  io_initialized = true;
  try {
    const auto port = static_cast<uint16_t>(opt_port);
    constexpr uint64_t kMaxConnections = 64, kMaxRequest = 1 << 20, kMaxResponse = 16 << 20;
    if (tls)
      server = std::make_shared<http::HttpServerContext>(
          &io.io_context(), &io.io_threads(), make_tls_context(), address, port, kMaxConnections,
          kMaxRequest, kMaxResponse);
    else
      server = std::make_shared<http::HttpServerContext>(&io.io_context(), &io.io_threads(),
                                                         address, port, kMaxConnections,
                                                         kMaxRequest, kMaxResponse);
    server->add_direct_match_route("", http::base::UriPathMatcher{"/mcp", false, false},
                                   std::make_unique<McpHandler>());
    workguard = std::make_unique<IoComponent::Workguard>(io);
    io_thread = std::thread([] { IoComponent::get_instance().run(); });
    server->start();
  } catch (const std::exception &e) {
    log_error(std::string("cannot start the endpoint: ") + e.what());
    return true;
  }
  const bool v6 = address.find(':') != std::string::npos;
  log_info(std::string("MCP endpoint on ") + (tls ? "https" : "http") + "://" +
           (v6 ? "[" + address + "]" : address) + ":" + std::to_string(opt_port) + "/mcp");
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

bool unregister_status_vars() {
  if (!status_registered) return false;
  if (mysql_service_status_variable_registration->unregister_variable(status_vars)) return true;
  status_registered = false;
  return false;
}

mysql_service_status_t mcp_init() {
  log_bi = mysql_service_log_builtins;
  log_bs = mysql_service_log_builtins_string;
  if (register_sysvars()) {
    unregister_sysvars();
    return 1;
  }
  if (mysql_service_status_variable_registration->register_variable(status_vars)) {
    unregister_sysvars();
    return 1;
  }
  status_registered = true;
  if (start_server()) {
    stop_server();
    unregister_status_vars();
    unregister_sysvars();
    return 1;
  }
  return 0;
}

mysql_service_status_t mcp_deinit() {
  stop_server();
  const bool status_failed = unregister_status_vars();
  return unregister_sysvars() || status_failed ? 1 : 0;
}

}  // namespace

BEGIN_SERVICE_IMPLEMENTATION(mcp, mysql_text_consumer_row_factory_v1)
bounded_start_row, bounded_abort_row, bounded_end_row END_SERVICE_IMPLEMENTATION();
BEGIN_SERVICE_IMPLEMENTATION(mcp, mysql_text_consumer_get_null_v1)
bounded_get_null END_SERVICE_IMPLEMENTATION();
BEGIN_SERVICE_IMPLEMENTATION(mcp, mysql_text_consumer_get_integer_v1)
bounded_get_integer END_SERVICE_IMPLEMENTATION();
BEGIN_SERVICE_IMPLEMENTATION(mcp, mysql_text_consumer_get_longlong_v1)
bounded_get_longlong END_SERVICE_IMPLEMENTATION();
BEGIN_SERVICE_IMPLEMENTATION(mcp, mysql_text_consumer_get_decimal_v1)
bounded_get_decimal END_SERVICE_IMPLEMENTATION();
BEGIN_SERVICE_IMPLEMENTATION(mcp, mysql_text_consumer_get_double_v1)
bounded_get_double END_SERVICE_IMPLEMENTATION();
BEGIN_SERVICE_IMPLEMENTATION(mcp, mysql_text_consumer_get_date_time_v1)
bounded_get_date, bounded_get_time, bounded_get_datetime END_SERVICE_IMPLEMENTATION();
BEGIN_SERVICE_IMPLEMENTATION(mcp, mysql_text_consumer_get_string_v1)
bounded_get_string END_SERVICE_IMPLEMENTATION();

BEGIN_COMPONENT_PROVIDES(mcp)
PROVIDES_SERVICE(mcp, mysql_text_consumer_row_factory_v1),
    PROVIDES_SERVICE(mcp, mysql_text_consumer_get_null_v1),
    PROVIDES_SERVICE(mcp, mysql_text_consumer_get_integer_v1),
    PROVIDES_SERVICE(mcp, mysql_text_consumer_get_longlong_v1),
    PROVIDES_SERVICE(mcp, mysql_text_consumer_get_decimal_v1),
    PROVIDES_SERVICE(mcp, mysql_text_consumer_get_double_v1),
    PROVIDES_SERVICE(mcp, mysql_text_consumer_get_date_time_v1),
    PROVIDES_SERVICE(mcp, mysql_text_consumer_get_string_v1), END_COMPONENT_PROVIDES();

BEGIN_COMPONENT_REQUIRES(mcp)
REQUIRES_SERVICE(log_builtins), REQUIRES_SERVICE(log_builtins_string),
    REQUIRES_SERVICE(component_sys_variable_register),
    REQUIRES_SERVICE(component_sys_variable_unregister),
    REQUIRES_SERVICE(status_variable_registration), REQUIRES_SERVICE(mysql_command_factory),
    REQUIRES_SERVICE(mysql_command_options), REQUIRES_SERVICE(mysql_command_query),
    REQUIRES_SERVICE(mysql_command_query_result), REQUIRES_SERVICE(mysql_command_field_info),
    REQUIRES_SERVICE(mysql_command_field_metadata), REQUIRES_SERVICE(mysql_command_error_info),
    REQUIRES_SERVICE(mysql_command_thread), REQUIRES_SERVICE(mysql_audit_api_connection),
    REQUIRES_SERVICE(mysql_thd_security_context), REQUIRES_SERVICE(mysql_security_context_options),
    REQUIRES_SERVICE(global_grants_check),
    REQUIRES_SERVICE_IMPLEMENTATION_AS(event_tracking_query, mcp_guard, mcp_guard_query),
    REQUIRES_SERVICE_IMPLEMENTATION_AS(mysql_text_consumer_row_factory_v1, mysql_server,
                                       dom_row_factory),
    REQUIRES_SERVICE_IMPLEMENTATION_AS(mysql_text_consumer_get_null_v1, mysql_server,
                                       dom_get_null),
    REQUIRES_SERVICE_IMPLEMENTATION_AS(mysql_text_consumer_get_integer_v1, mysql_server,
                                       dom_get_integer),
    REQUIRES_SERVICE_IMPLEMENTATION_AS(mysql_text_consumer_get_longlong_v1, mysql_server,
                                       dom_get_longlong),
    REQUIRES_SERVICE_IMPLEMENTATION_AS(mysql_text_consumer_get_decimal_v1, mysql_server,
                                       dom_get_decimal),
    REQUIRES_SERVICE_IMPLEMENTATION_AS(mysql_text_consumer_get_double_v1, mysql_server,
                                       dom_get_double),
    REQUIRES_SERVICE_IMPLEMENTATION_AS(mysql_text_consumer_get_date_time_v1, mysql_server,
                                       dom_get_date_time),
    REQUIRES_SERVICE_IMPLEMENTATION_AS(mysql_text_consumer_get_string_v1, mysql_server,
                                       dom_get_string),
    END_COMPONENT_REQUIRES();

BEGIN_COMPONENT_METADATA(mcp)
METADATA("mysql.author", "askdba"), METADATA("mysql.license", "GPL"),
    END_COMPONENT_METADATA();

DECLARE_COMPONENT(mcp, "mysql:mcp") mcp_init, mcp_deinit END_DECLARE_COMPONENT();

DECLARE_LIBRARY_COMPONENTS &COMPONENT_REF(mcp) END_DECLARE_LIBRARY_COMPONENTS
