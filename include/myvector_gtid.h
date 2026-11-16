/*
   Copyright (c) 2024 - p3io.in / shiyer22@gmail.com

   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.
*/

/* GTID sets, as far as storage=innodb indexes need them (RFC-005 4.4, 4.10):
 * which transactions an index's graph already includes. Text form is MySQL's:
 *   uuid:1-5:7,uuid2:1-3            untagged
 *   uuid:1-10:mytag:1-4:other:2     tags (MySQL 8.3+) follow the untagged part
 * Header-only and free of MySQL headers, so it has plain unit tests
 * (tests/test_myvector_gtid.cc). */

#ifndef MYVECTOR_GTID_H
#define MYVECTOR_GTID_H

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <map>
#include <string>
#include <utility>
#include <vector>

/* One transaction: source UUID (36 chars, lower case), optional tag, number. */
struct MyVectorGtid {
    std::string uuid;
    std::string tag;
    int64_t gno = 0;
    bool valid() const { return uuid.size() == 36 && gno > 0; }
};

class MyVectorGtidSet {
public:
    /* Parse MySQL's text form. Returns false on malformed input and leaves the
     * set empty. An empty string is the empty set. */
    bool parse(const std::string& text) {
        m_sets.clear();
        std::string s;
        for (char c : text)  // the server wraps long sets over several lines
            if (!std::isspace(static_cast<unsigned char>(c)))
                s.push_back(c);
        size_t pos = 0;
        while (pos < s.size()) {
            size_t comma = s.find(',', pos);
            std::string part = s.substr(pos, comma == std::string::npos ? comma : comma - pos);
            pos = comma == std::string::npos ? s.size() : comma + 1;
            if (part.empty())
                continue;
            if (!parseOne(part)) {
                m_sets.clear();
                return false;
            }
        }
        return true;
    }

    std::string toString() const {
        /* Group by UUID: untagged intervals first, then each tag (map order). */
        std::string out;
        std::string cur_uuid;
        for (const auto& e : m_sets) {
            if (e.second.empty())
                continue;
            const std::string& uuid = e.first.first;
            const std::string& tag = e.first.second;
            if (uuid != cur_uuid) {
                if (!out.empty())
                    out += ",";
                out += uuid;
                cur_uuid = uuid;
            }
            if (!tag.empty())
                out += ":" + tag;
            for (const auto& iv : e.second) {
                out += ":" + std::to_string(iv.first);
                if (iv.second != iv.first)
                    out += "-" + std::to_string(iv.second);
            }
        }
        return out;
    }

    bool empty() const {
        for (const auto& e : m_sets)
            if (!e.second.empty())
                return false;
        return true;
    }

    void clear() { m_sets.clear(); }

    void add(const MyVectorGtid& g) {
        if (g.valid())
            addInterval(key(g.uuid, g.tag), g.gno, g.gno);
    }

    bool contains(const MyVectorGtid& g) const {
        auto it = m_sets.find(key(g.uuid, g.tag));
        if (it == m_sets.end())
            return false;
        const Intervals& v = it->second;
        // first interval whose end is >= gno
        auto iv = std::lower_bound(v.begin(), v.end(), g.gno,
                                   [](const Interval& a, int64_t n) { return a.second < n; });
        return iv != v.end() && iv->first <= g.gno;
    }

    /* True if every transaction of other is in this set. */
    bool containsAll(const MyVectorGtidSet& other) const {
        for (const auto& e : other.m_sets) {
            if (e.second.empty())
                continue;
            auto it = m_sets.find(e.first);
            if (it == m_sets.end())
                return false;
            const Intervals& mine = it->second;
            for (const auto& iv : e.second) {
                auto m = std::lower_bound(
                    mine.begin(), mine.end(), iv.first,
                    [](const Interval& a, int64_t n) { return a.second < n; });
                if (m == mine.end() || m->first > iv.first || m->second < iv.second)
                    return false;
            }
        }
        return true;
    }

    void unionWith(const MyVectorGtidSet& other) {
        for (const auto& e : other.m_sets)
            for (const auto& iv : e.second)
                addInterval(e.first, iv.first, iv.second);
    }

    bool operator==(const MyVectorGtidSet& o) const { return toString() == o.toString(); }

private:
    typedef std::pair<int64_t, int64_t> Interval;  // inclusive
    typedef std::vector<Interval> Intervals;       // sorted, disjoint, non-adjacent
    typedef std::pair<std::string, std::string> Key;  // uuid, tag

    static Key key(const std::string& uuid, const std::string& tag) {
        std::string u = uuid, t = tag;
        for (char& c : u)
            c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        for (char& c : t)
            c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        return Key(u, t);
    }

    void addInterval(const Key& k, int64_t a, int64_t b) {
        Intervals& v = m_sets[k];
        // first interval that ends at or after a-1 (so it touches [a, b])
        auto it = std::lower_bound(v.begin(), v.end(), a - 1,
                                   [](const Interval& x, int64_t n) { return x.second < n; });
        auto first = it;
        while (it != v.end() && it->first <= b + 1) {
            a = std::min(a, it->first);
            b = std::max(b, it->second);
            ++it;
        }
        first = v.erase(first, it);
        v.insert(first, Interval(a, b));
    }

    static bool isUuid(const std::string& s) {
        if (s.size() != 36)
            return false;
        for (size_t i = 0; i < 36; i++) {
            bool dash = (i == 8 || i == 13 || i == 18 || i == 23);
            if (dash ? s[i] != '-' : !std::isxdigit(static_cast<unsigned char>(s[i])))
                return false;
        }
        return true;
    }

    /* A tag: a letter or underscore, then letters, digits or underscores, at
     * most 32 characters (MySQL's rule). */
    static bool isTag(const std::string& s) {
        if (s.empty() || s.size() > 32)
            return false;
        if (!(std::isalpha(static_cast<unsigned char>(s[0])) || s[0] == '_'))
            return false;
        for (char c : s)
            if (!(std::isalnum(static_cast<unsigned char>(c)) || c == '_'))
                return false;
        return true;
    }

    static bool parseNumber(const std::string& s, int64_t* out) {
        if (s.empty() || s.size() > 18)
            return false;
        int64_t v = 0;
        for (char c : s) {
            if (!std::isdigit(static_cast<unsigned char>(c)))
                return false;
            v = v * 10 + (c - '0');
        }
        *out = v;
        return true;
    }

    /* uuid(:interval | :tag)*, where an interval is N or N-M. */
    bool parseOne(const std::string& part) {
        std::vector<std::string> seg;
        size_t pos = 0;
        while (true) {
            size_t colon = part.find(':', pos);
            seg.push_back(part.substr(pos, colon == std::string::npos ? colon : colon - pos));
            if (colon == std::string::npos)
                break;
            pos = colon + 1;
        }
        if (!isUuid(seg[0]))
            return false;
        std::string tag;
        for (size_t i = 1; i < seg.size(); i++) {
            const std::string& t = seg[i];
            if (t.empty())
                return false;
            if (!std::isdigit(static_cast<unsigned char>(t[0]))) {
                if (!isTag(t))
                    return false;
                tag = t;
                continue;
            }
            size_t dash = t.find('-');
            int64_t a = 0, b = 0;
            if (!parseNumber(t.substr(0, dash), &a))
                return false;
            if (dash == std::string::npos)
                b = a;
            else if (!parseNumber(t.substr(dash + 1), &b))
                return false;
            if (a <= 0 || b < a)
                return false;
            addInterval(key(seg[0], tag), a, b);
        }
        return true;
    }

    std::map<Key, Intervals> m_sets;
};

/* Format 16 raw UUID bytes as MySQL prints them. */
inline std::string myvector_uuid_text(const unsigned char* b) {
    char buf[37];
    snprintf(buf, sizeof(buf),
             "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
             b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11],
             b[12], b[13], b[14], b[15]);
    return std::string(buf);
}

/* Decode the body of a GTID_LOG_EVENT (binlog event type 33), the bytes after
 * the 19-byte event header: flags(1) uuid(16) gno(8, little-endian). */
inline bool myvector_decode_gtid_event(const unsigned char* body, size_t len, MyVectorGtid* out) {
    if (len < 25)
        return false;
    out->uuid = myvector_uuid_text(body + 1);
    out->tag.clear();
    int64_t gno = 0;
    for (int i = 7; i >= 0; i--)
        gno = (gno << 8) | body[17 + i];
    out->gno = gno;
    return out->valid();
}

/* MySQL's variable-length unsigned integer (mysql::serialization): the number
 * of trailing 1 bits of the first byte is the number of bytes that follow; the
 * value is those bytes, little-endian, shifted right past the marker bits. */
inline bool myvector_read_varuint(const unsigned char* p, size_t len, size_t* pos,
                                  uint64_t* out) {
    if (*pos >= len)
        return false;
    unsigned char first = p[*pos];
    int extra = 0;
    while (extra < 8 && (first & (1u << extra)))
        extra++;
    if (*pos + 1 + static_cast<size_t>(extra) > len)
        return false;
    uint64_t v = 0;
    if (extra == 8) {
        for (int i = 7; i >= 0; i--)
            v = (v << 8) | p[*pos + 1 + i];
    } else {
        for (int i = extra; i >= 0; i--)
            v = (v << 8) | p[*pos + i];
        v >>= (extra + 1);
    }
    *pos += 1 + static_cast<size_t>(extra);
    *out = v;
    return true;
}

/* Decode the body of a GTID_TAGGED_LOG_EVENT (type 42, MySQL 8.3+). It is a
 * serialized message: format version, size, last non-ignorable field id, then
 * (field id, value) pairs in field order: 0 flags, 1 uuid (16 integers),
 * 2 gno (signed), 3 tag (length + bytes). Later fields are not needed. Returns
 * false if the layout is not exactly that. */
inline bool myvector_decode_tagged_gtid_event(const unsigned char* body, size_t len,
                                              MyVectorGtid* out) {
    size_t pos = 0;
    uint64_t v = 0;
    for (int i = 0; i < 3; i++)  // version, size, last non-ignorable field id
        if (!myvector_read_varuint(body, len, &pos, &v))
            return false;
    if (!myvector_read_varuint(body, len, &pos, &v) || v != 0)  // field 0: flags
        return false;
    if (!myvector_read_varuint(body, len, &pos, &v))
        return false;
    if (!myvector_read_varuint(body, len, &pos, &v) || v != 1)  // field 1: uuid
        return false;
    unsigned char uuid[16];
    for (int i = 0; i < 16; i++) {
        if (!myvector_read_varuint(body, len, &pos, &v) || v > 255)
            return false;
        uuid[i] = static_cast<unsigned char>(v);
    }
    if (!myvector_read_varuint(body, len, &pos, &v) || v != 2)  // field 2: gno
        return false;
    if (!myvector_read_varuint(body, len, &pos, &v))
        return false;
    int64_t gno = static_cast<int64_t>(v >> 1) ^ -static_cast<int64_t>(v & 1);
    if (!myvector_read_varuint(body, len, &pos, &v) || v != 3)  // field 3: tag
        return false;
    uint64_t tag_len = 0;
    if (!myvector_read_varuint(body, len, &pos, &tag_len) || tag_len > 32 ||
        pos + tag_len > len)
        return false;
    out->uuid = myvector_uuid_text(uuid);
    out->tag.assign(reinterpret_cast<const char*>(body + pos), tag_len);
    out->gno = gno;
    for (char c : out->tag)
        if (!(std::isalnum(static_cast<unsigned char>(c)) || c == '_'))
            return false;
    return out->valid();
}

#endif  // MYVECTOR_GTID_H
