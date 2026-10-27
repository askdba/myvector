#include "myvectorutils.h"

#include <cstring>
#include <string>

std::string quote_identifier(const std::string& identifier) {
    std::string quoted_identifier = "`";
    for (char c : identifier) {
        quoted_identifier += c;
        if (c == '`')
            quoted_identifier += '`';  // double an embedded backtick
    }
    quoted_identifier += "`";
    return quoted_identifier;
}

std::string escape_sql_string_literal(const std::string& value) {
    std::string out;
    out.reserve(value.size());
    for (unsigned char c : value) {
        switch (c) {
            case '\0':
                out += "\\0";
                break;
            case '\n':
                out += "\\n";
                break;
            case '\r':
                out += "\\r";
                break;
            case '\\':
                out += "\\\\";
                break;
            case '\'':
                out += "\\'";
                break;
            case '"':
                out += "\\\"";
                break;
            case '\x1a':
                out += "\\Z";
                break;
            default:
                out += static_cast<char>(c);
        }
    }
    return out;
}
