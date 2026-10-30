/*
   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License, version 2.0, for more details.
*/

// Policy decisions of component_mcp_guard, kept free of server headers so they can be
// unit-tested on their own (src/mcp/guard/test_mcp_guard_policy.cc).
//
// Statements are classified by the server's sql_command name (event_tracking_query),
// which is exact. Three cases need the statement text as well: SELECT ... INTO
// OUTFILE/DUMPFILE and locking reads (still sql_command "select"), SET GLOBAL/PERSIST
// (still "set_option"), and CALL, which is allowed only into the mysql_mcp tool pack.
// The text is tokenized the way the server reads it: string literals and comments are
// skipped, but the body of an executable comment (/*! ... */) is code.

#ifndef MCP_GUARD_POLICY_H
#define MCP_GUARD_POLICY_H

#include <cctype>
#include <cstddef>
#include <cstring>
#include <string>
#include <string_view>
#include <vector>

namespace mcp_guard {

struct Token {
  enum Kind { kWord, kQuotedIdent, kPunct };
  Kind kind;
  std::string text;  // words upper-cased; quoted identifiers as written, unquoted
};

inline bool is_word_char(char c) {
  return std::isalnum(static_cast<unsigned char>(c)) || c == '_' || c == '$' ||
         c == '@' || (static_cast<unsigned char>(c) >= 0x80);
}

// Tokenizes SQL text: words, `quoted identifiers` and single punctuation characters.
// String literals ('...', "...") and comments (-- , #, /* */, optimizer hints /*+ */) are
// skipped. The body of an executable comment (/*!NNNNN ... */) is tokenized as code.
// backslash_escapes: whether \ escapes the next character in a quoted string, which
// depends on the session's sql_mode (NO_BACKSLASH_ESCAPES) and which the scanner cannot
// see; callers scan both ways (see read_only_violation).
inline std::vector<Token> tokenize(const char *s, size_t n, bool backslash_escapes = true) {
  std::vector<Token> out;
  size_t i = 0;
  bool in_exec_comment = false;
  while (i < n) {
    const char c = s[i];
    if (std::isspace(static_cast<unsigned char>(c))) {
      ++i;
      continue;
    }
    if (in_exec_comment && c == '*' && i + 1 < n && s[i + 1] == '/') {
      in_exec_comment = false;
      i += 2;
      continue;
    }
    if (c == '/' && i + 1 < n && s[i + 1] == '*') {
      // /*! ... */ (MySQL) and /*M! ... */ (MariaDB) are executable comments.
      const bool exec = i + 2 < n && (s[i + 2] == '!' || (s[i + 2] == 'M' && i + 3 < n && s[i + 3] == '!'));
      if (exec && !in_exec_comment) {
        in_exec_comment = true;
        i += s[i + 2] == '!' ? 3 : 4;
        while (i < n && std::isdigit(static_cast<unsigned char>(s[i]))) ++i;
        continue;
      }
      size_t end = i + 2;
      while (end + 1 < n && !(s[end] == '*' && s[end + 1] == '/')) ++end;
      i = end + 1 < n ? end + 2 : n;
      continue;
    }
    if (c == '#' || (c == '-' && i + 1 < n && s[i + 1] == '-' &&
                     (i + 2 == n || std::isspace(static_cast<unsigned char>(s[i + 2]))))) {
      while (i < n && s[i] != '\n') ++i;
      continue;
    }
    if (c == '\'' || c == '"') {
      const char q = c;
      ++i;
      while (i < n) {
        if (backslash_escapes && s[i] == '\\' && i + 1 < n) {
          i += 2;
        } else if (s[i] == q && i + 1 < n && s[i + 1] == q) {
          i += 2;
        } else if (s[i] == q) {
          ++i;
          break;
        } else {
          ++i;
        }
      }
      continue;
    }
    if (c == '`') {
      std::string ident;
      ++i;
      while (i < n) {
        if (s[i] == '`' && i + 1 < n && s[i + 1] == '`') {
          ident.push_back('`');
          i += 2;
        } else if (s[i] == '`') {
          ++i;
          break;
        } else {
          ident.push_back(s[i++]);
        }
      }
      out.push_back({Token::kQuotedIdent, ident});
      continue;
    }
    if (is_word_char(c)) {
      std::string w;
      while (i < n && is_word_char(s[i])) {
        w.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(s[i]))));
        ++i;
      }
      out.push_back({Token::kWord, w});
      continue;
    }
    out.push_back({Token::kPunct, std::string(1, c)});
    ++i;
  }
  return out;
}

inline bool word_is(const std::vector<Token> &t, size_t i, std::string_view w) {
  return i < t.size() && t[i].kind == Token::kWord && t[i].text == w;
}

inline bool ieq(std::string_view a, std::string_view b) {
  if (a.size() != b.size()) return false;
  for (size_t i = 0; i < a.size(); ++i)
    if (std::tolower(static_cast<unsigned char>(a[i])) !=
        std::tolower(static_cast<unsigned char>(b[i])))
      return false;
  return true;
}

// A SELECT that writes a file or takes row locks.
inline bool select_writes_or_locks(const std::vector<Token> &t) {
  for (size_t i = 0; i < t.size(); ++i) {
    if (word_is(t, i, "INTO") && (word_is(t, i + 1, "OUTFILE") || word_is(t, i + 1, "DUMPFILE")))
      return true;
    if (word_is(t, i, "FOR") && (word_is(t, i + 1, "UPDATE") || word_is(t, i + 1, "SHARE")))
      return true;
    if (word_is(t, i, "LOCK") && word_is(t, i + 1, "IN") && word_is(t, i + 2, "SHARE") &&
        word_is(t, i + 3, "MODE"))
      return true;
  }
  return false;
}

// A SET that changes global or persisted settings (session SET and user variables are fine).
inline bool set_is_global(const std::vector<Token> &t) {
  for (const Token &tok : t) {
    if (tok.kind != Token::kWord) continue;
    if (tok.text == "GLOBAL" || tok.text == "PERSIST" || tok.text == "PERSIST_ONLY" ||
        tok.text == "@@GLOBAL" || tok.text == "@@PERSIST" || tok.text == "@@PERSIST_ONLY")
      return true;
  }
  return false;
}

// CALL mysql_mcp.<routine>(...): a schema-qualified call into the tool pack.
inline bool call_into_tool_pack(const std::vector<Token> &t) {
  return word_is(t, 0, "CALL") && t.size() >= 4 &&
         (t[1].kind == Token::kWord || t[1].kind == Token::kQuotedIdent) &&
         ieq(t[1].text, "mysql_mcp") && t[2].kind == Token::kPunct && t[2].text == "." &&
         (t[3].kind == Token::kWord || t[3].kind == Token::kQuotedIdent);
}

// Statements an MCP session may run under mcp.read_only, by sql_command name. Anything
// not listed (including commands a future server adds) is denied. PREPARE and EXECUTE
// are allowed because the executed statement raises its own event with its own command.
inline bool read_only_command(std::string_view cmd) {
  if (cmd.substr(0, 5) == "show_") return true;
  static constexpr std::string_view kAllowed[] = {
      "select",       "set_option",         "do",
      "change_db",    "call",               "prepare",
      "execute",      "deallocate_prepare", "empty_query",
      "help",         "begin",              "commit",
      "rollback",     "savepoint",          "release_savepoint",
      "rollback_to_savepoint", "signal",    "resignal",
      "get_diagnostics", "check",           "checksum",
      "kill",         "unlock_tables",      "explain_other",
      "ha_open",      "ha_read",            "ha_close",
  };
  for (std::string_view a : kAllowed)
    if (cmd == a) return true;
  return false;
}

// Returns the option that denies the statement under mcp.read_only, or nullptr.
// Where a quoted string ends depends on sql_mode (NO_BACKSLASH_ESCAPES, ANSI_QUOTES), which
// a session may change. The text is therefore scanned both with and without backslash
// escapes: a statement is denied if either reading finds a violation, and a CALL is
// allowed only if both readings agree it targets mysql_mcp.
inline const char *read_only_violation(std::string_view cmd, const char *query, size_t len) {
  if (!read_only_command(cmd)) return "mcp.read_only";
  if (cmd != "select" && cmd != "set_option" && cmd != "call") return nullptr;
  for (bool escapes : {true, false}) {
    const std::vector<Token> t = tokenize(query, len, escapes);
    if (cmd == "select" && select_writes_or_locks(t)) return "mcp.read_only";
    if (cmd == "set_option" && set_is_global(t)) return "mcp.read_only";
    if (cmd == "call" && !call_into_tool_pack(t)) return "mcp.read_only";
  }
  return nullptr;
}

// ----- mcp.max_execution_time_ms ------------------------------------------------------

inline bool all_digits(std::string_view s) {
  if (s.empty() || s.size() > 18) return false;
  for (char c : s)
    if (!std::isdigit(static_cast<unsigned char>(c))) return false;
  return true;
}

// An explicit limit is acceptable when it is a positive literal no larger than the floor.
inline bool within_floor(std::string_view value, unsigned long long floor) {
  if (!all_digits(value)) return false;
  const unsigned long long v = std::stoull(std::string(value));
  return v > 0 && v <= floor;
}

// A SET of the session's max_execution_time that would lift the floor: to 0, above it, or
// to anything but a literal (DEFAULT, an expression, a variable).
inline bool set_raises_time_limit(const std::vector<Token> &t, unsigned long long floor) {
  for (size_t i = 0; i < t.size(); ++i) {
    if (t[i].kind != Token::kWord) continue;
    if (t[i].text != "MAX_EXECUTION_TIME" && t[i].text != "@@MAX_EXECUTION_TIME") continue;
    size_t j = i + 1;
    if (j < t.size() && t[j].kind == Token::kPunct && t[j].text == ":") ++j;
    if (!(j < t.size() && t[j].kind == Token::kPunct && t[j].text == "=")) continue;  // not assigned
    ++j;
    const bool literal = j < t.size() && t[j].kind == Token::kWord &&
                         (j + 1 == t.size() || (t[j + 1].kind == Token::kPunct &&
                                                (t[j + 1].text == "," || t[j + 1].text == ";")));
    if (!literal || !within_floor(t[j].text, floor)) return true;
  }
  return false;
}

// An optimizer hint (/*+ ... */) that sets the statement's time limit, either
// MAX_EXECUTION_TIME(n) or SET_VAR(max_execution_time = n), to 0, above the floor, or to
// something that is not a literal. Searched in the raw text, so a hint-like string literal
// is also refused; that only errs on the safe side.
inline bool hint_raises_time_limit(const char *s, size_t n, unsigned long long floor) {
  std::string upper(s, n);
  for (char &c : upper) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
  size_t pos = 0;
  while ((pos = upper.find("/*+", pos)) != std::string::npos) {
    const size_t end = upper.find("*/", pos + 3);
    const std::string body = upper.substr(pos + 3, end == std::string::npos ? std::string::npos
                                                                             : end - pos - 3);
    size_t k = 0;
    while ((k = body.find("MAX_EXECUTION_TIME", k)) != std::string::npos) {
      size_t v = k + std::strlen("MAX_EXECUTION_TIME");
      while (v < body.size() && std::isspace(static_cast<unsigned char>(body[v]))) ++v;
      if (v < body.size() && (body[v] == '(' || body[v] == '=')) ++v;
      while (v < body.size() && std::isspace(static_cast<unsigned char>(body[v]))) ++v;
      size_t e = v;
      while (e < body.size() && std::isdigit(static_cast<unsigned char>(body[e]))) ++e;
      if (!within_floor(std::string_view(body).substr(v, e - v), floor)) return true;
      k = e;
    }
    if (end == std::string::npos) break;
    pos = end + 2;
  }
  return false;
}

// Returns "mcp.max_execution_time_ms" when the statement would lift the floor, or nullptr.
// floor == 0 disables the check. Text is read in both quoting modes, as for read_only.
inline const char *time_limit_violation(std::string_view cmd, const char *query, size_t len,
                                        unsigned long long floor) {
  if (floor == 0) return nullptr;
  if (hint_raises_time_limit(query, len, floor)) return "mcp.max_execution_time_ms";
  if (cmd == "set_option") {
    for (bool escapes : {true, false})
      if (set_raises_time_limit(tokenize(query, len, escapes), floor))
        return "mcp.max_execution_time_ms";
  }
  return nullptr;
}

// Parses mcp.allowed_schemas: comma-separated schema names, spaces trimmed, empty
// entries dropped. An empty list means every schema.
inline std::vector<std::string> parse_schema_list(std::string_view v) {
  std::vector<std::string> out;
  size_t start = 0;
  while (start <= v.size()) {
    size_t comma = v.find(',', start);
    if (comma == std::string_view::npos) comma = v.size();
    std::string_view item = v.substr(start, comma - start);
    while (!item.empty() && std::isspace(static_cast<unsigned char>(item.front())))
      item.remove_prefix(1);
    while (!item.empty() && std::isspace(static_cast<unsigned char>(item.back())))
      item.remove_suffix(1);
    if (!item.empty()) out.emplace_back(item);
    start = comma + 1;
  }
  return out;
}

struct TablePolicy {
  bool read_only = true;
  bool deny_system_schemas = true;
  // restrict_schemas: mcp.allowed_schemas is set (not blank). Then only the listed
  // schemas are allowed; a value with no names in it (",") allows none, failing closed.
  bool restrict_schemas = false;
  const std::vector<std::string> *allowed_schemas = nullptr;
  // Compare schema names case-insensitively: lower_case_table_names is 1 or 2, where the
  // server lowercases (or compares in lowercase) the names it reports in table events.
  bool case_insensitive_names = false;
};

// Whether an mcp.allowed_schemas value restricts schemas at all: anything but blank.
inline bool restricts_schemas(std::string_view v) {
  for (char c : v)
    if (!std::isspace(static_cast<unsigned char>(c))) return true;
  return false;
}

inline bool is_system_schema(std::string_view db) {
  return db == "mysql" || db == "sys" || db == "information_schema" ||
         db == "performance_schema";
}

// Returns the option that denies a table access by an MCP session, or nullptr.
// write is true for insert, update and delete accesses.
inline const char *table_violation(const TablePolicy &p, std::string_view db, bool write) {
  if (write && p.read_only) return "mcp.read_only";
  if (is_system_schema(db)) {
    // information_schema and performance_schema stay readable: the tools use them, and
    // grants govern them. mysql (account and grant tables) and sys are denied.
    if (p.deny_system_schemas && (db == "mysql" || db == "sys")) return "mcp.deny_system_schemas";
    return nullptr;
  }
  if (db == "mysql_mcp") return nullptr;
  if (p.restrict_schemas) {
    if (p.allowed_schemas)
      for (const std::string &s : *p.allowed_schemas)
        if (p.case_insensitive_names ? ieq(db, s) : db == s) return nullptr;
    return "mcp.allowed_schemas";
  }
  return nullptr;
}

}  // namespace mcp_guard

#endif  // MCP_GUARD_POLICY_H
