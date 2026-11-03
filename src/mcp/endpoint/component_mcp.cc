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
#include <mysql/components/services/mysql_command_consumer.h>
#include <mysql/components/services/mysql_command_services.h>
#include <mysql/components/services/security_context.h>
#include <mysql/components/services/event_tracking_query_service.h>
#include <mysqld_error.h>
#include "mysql/strings/dtoa.h"

#include <openssl/crypto.h>
#include <openssl/evp.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

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
REQUIRES_SERVICE_PLACEHOLDER(mysql_thd_security_context);
REQUIRES_SERVICE_PLACEHOLDER(mysql_security_context_options);
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
char *opt_account = nullptr;       // user@host the token maps to
char *opt_token_sha256 = nullptr;  // hex SHA-256 of the bearer token
char *opt_allowed_origins = nullptr;  // comma-separated Origin values browsers may send
char *opt_mask_columns = nullptr;     // comma-separated column name patterns to mask
bool port_registered = false, threads_registered = false, max_sessions_registered = false,
     account_registered = false, token_registered = false, origins_registered = false,
     mask_registered = false;

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
  drop(mask_registered, "mask_columns");
  drop(origins_registered, "allowed_origins");
  drop(token_registered, "token_sha256");
  drop(account_registered, "account");
  drop(max_sessions_registered, "max_sessions_per_account");
  drop(threads_registered, "threads");
  drop(port_registered, "port");
  return failed;
}

void log_info(const std::string &msg) {
  LogComponentErr(INFORMATION_LEVEL, ER_LOG_PRINTF_MSG, ("component_mcp: " + msg).c_str());
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
  size_t bytes = 0;
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
// contains one of the mcp_http.mask_columns patterns (ASCII case-insensitive; empty patterns
// ignored) has every non-NULL value replaced by "********".
constexpr char kMasked[] = "********";

std::string ascii_lower(std::string s) {
  for (char &c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return s;
}

std::vector<std::string> mask_patterns() {
  std::vector<std::string> out;
  if (opt_mask_columns == nullptr) return out;
  const std::string all = opt_mask_columns;
  size_t b = 0;
  while (b <= all.size()) {
    size_t e = all.find(',', b);
    if (e == std::string::npos) e = all.size();
    size_t x = b, y = e;
    while (x < y && std::isspace(static_cast<unsigned char>(all[x]))) ++x;
    while (y > x && std::isspace(static_cast<unsigned char>(all[y - 1]))) --y;
    if (y > x) out.push_back(ascii_lower(all.substr(x, y - x)));
    b = e + 1;
  }
  return out;
}

bool masked_column(const std::string &name, const std::vector<std::string> &patterns) {
  const std::string lower = ascii_lower(name);
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

bool set_bounded_consumer(MYSQL_H h) {
  for (const auto &[option, service] : kBoundedConsumer)
    if (mysql_service_mysql_command_options->set(h, option, service)) return false;
  return true;
}

// Runs sql under a fresh row budget; *truncated reports whether rows were dropped.
bool bounded_query(MYSQL_H h, const std::string &sql, bool *truncated) {
  RowBudget budget;
  BudgetScope scope(&budget);
  const bool failed = mysql_service_mysql_command_query->query(h, sql.data(), sql.size());
  *truncated = budget.dropped;
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
    } else if (!announce()) {
      r.error = "session refused";
    } else if (mysql_service_mysql_command_query->query(h, kLimit.data(), kLimit.size())) {
      read_error(h, &r);
    } else if (bounded_query(h, sql, &r.truncated)) {
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

void call_tool(const rapidjson::Value &params, Json &out) {
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
    const std::string *origin = req.get_input_headers().find("Origin");
    if (!origin_ok(origin)) {
      req.send_reply(HttpStatusCode::Forbidden);
      return;
    }
    // CORS for an allowed browser origin: its replies name it, so the page may read them, and
    // its preflight (OPTIONS, sent without credentials) is answered here.
    if (origin != nullptr) {
      req.get_output_headers().add("Access-Control-Allow-Origin", origin->c_str());
      req.get_output_headers().add("Vary", "Origin");
    }
    if (req.get_method() == HttpMethod::Options && origin != nullptr) {
      req.get_output_headers().add("Access-Control-Allow-Methods", "POST");
      req.get_output_headers().add("Access-Control-Allow-Headers",
                                   "Authorization, Content-Type, Mcp-Session-Id, "
                                   "MCP-Protocol-Version");
      req.get_output_headers().add("Access-Control-Max-Age", "600");
      req.send_reply(HttpStatusCode::NoContent);
      return;
    }
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
  if (!io.init(opt_threads, IoBackend::preferred())) return true;
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
    REQUIRES_SERVICE(component_sys_variable_unregister), REQUIRES_SERVICE(mysql_command_factory),
    REQUIRES_SERVICE(mysql_command_options), REQUIRES_SERVICE(mysql_command_query),
    REQUIRES_SERVICE(mysql_command_query_result), REQUIRES_SERVICE(mysql_command_field_info),
    REQUIRES_SERVICE(mysql_command_field_metadata), REQUIRES_SERVICE(mysql_command_error_info),
    REQUIRES_SERVICE(mysql_command_thread), REQUIRES_SERVICE(mysql_audit_api_connection),
    REQUIRES_SERVICE(mysql_thd_security_context), REQUIRES_SERVICE(mysql_security_context_options),
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
