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
inline std::vector<Token> tokenize(const char *s, size_t n) {
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
        if (s[i] == '\\' && i + 1 < n) {
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
inline const char *read_only_violation(std::string_view cmd, const char *query, size_t len) {
  if (!read_only_command(cmd)) return "mcp.read_only";
  if (cmd == "select") {
    if (select_writes_or_locks(tokenize(query, len))) return "mcp.read_only";
  } else if (cmd == "set_option") {
    if (set_is_global(tokenize(query, len))) return "mcp.read_only";
  } else if (cmd == "call") {
    if (!call_into_tool_pack(tokenize(query, len))) return "mcp.read_only";
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
  const std::vector<std::string> *allowed_schemas = nullptr;  // empty or null: all
};

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
  if (p.allowed_schemas && !p.allowed_schemas->empty()) {
    for (const std::string &s : *p.allowed_schemas)
      if (db == s) return nullptr;
    return "mcp.allowed_schemas";
  }
  return nullptr;
}

}  // namespace mcp_guard

#endif  // MCP_GUARD_POLICY_H
