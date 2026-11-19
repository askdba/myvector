// Standalone unit test for MyVectorGtidSet and the GTID event decoders
// (include/myvector_gtid.h).
//
// Build and run from the repository root:
//   g++ -std=c++17 -I include tests/test_myvector_gtid.cc -o /tmp/test_myvector_gtid && /tmp/test_myvector_gtid
//
// Background: an online index stored in InnoDB (RFC-005) records the set of
// transactions its graph includes. After a restart the binlog listener skips
// exactly those and applies the rest, so a wrong answer here either loses a row
// change or applies one twice.

#include <cstdio>
#include <string>
#include <vector>

#include "myvector_gtid.h"

static int failures = 0;

#define CHECK_EQ(actual, expected, what)                                            \
    do {                                                                            \
        std::string a_ = (actual);                                                  \
        std::string e_ = (expected);                                                \
        if (a_ != e_) {                                                             \
            std::printf("FAIL: %s: expected '%s', got '%s'\n", what, e_.c_str(),    \
                        a_.c_str());                                                \
            failures++;                                                             \
        } else {                                                                    \
            std::printf("PASS: %s\n", what);                                        \
        }                                                                           \
    } while (0)

#define CHECK_TRUE(cond, what)                                                      \
    do {                                                                            \
        if (!(cond)) {                                                              \
            std::printf("FAIL: %s\n", what);                                        \
            failures++;                                                             \
        } else {                                                                    \
            std::printf("PASS: %s\n", what);                                        \
        }                                                                           \
    } while (0)

static const char* U1 = "140f207e-e1f5-11f1-9a84-5e0d48933362";
static const char* U2 = "2744675a-e1f5-11f1-b402-86104b0255c5";

static MyVectorGtid gtid(const char* uuid, int64_t gno, const char* tag = "") {
    MyVectorGtid g;
    g.uuid = uuid;
    g.tag = tag;
    g.gno = gno;
    return g;
}

static std::string roundTrip(const std::string& text) {
    MyVectorGtidSet s;
    if (!s.parse(text))
        return "<parse error>";
    return s.toString();
}

static std::vector<unsigned char> fromHex(const std::string& hex) {
    std::vector<unsigned char> out;
    for (size_t i = 0; i + 1 < hex.size(); i += 2)
        out.push_back(static_cast<unsigned char>(std::stoi(hex.substr(i, 2), nullptr, 16)));
    return out;
}

int main() {
    std::string u1(U1), u2(U2);

    // ---- parse and print
    CHECK_EQ(roundTrip(""), "", "empty set");
    CHECK_EQ(roundTrip(u1 + ":1-10"), u1 + ":1-10", "one interval");
    CHECK_EQ(roundTrip(u1 + ":7"), u1 + ":7", "single transaction");
    CHECK_EQ(roundTrip(u1 + ":1-5:7:9-12"), u1 + ":1-5:7:9-12", "several intervals");
    CHECK_EQ(roundTrip(u1 + ":1-5:6-9"), u1 + ":1-9", "adjacent intervals merge");
    CHECK_EQ(roundTrip(u1 + ":5-9:1-6"), u1 + ":1-9", "overlapping, out of order");
    CHECK_EQ(roundTrip(u2 + ":1-3," + u1 + ":1-2"), u1 + ":1-2," + u2 + ":1-3",
             "two servers, sorted by uuid");
    CHECK_EQ(roundTrip(u1 + ":1-3,\n" + u2 + ":4"), u1 + ":1-3," + u2 + ":4",
             "line break after the comma, as the server prints long sets");
    CHECK_EQ(roundTrip(u1 + ":1-10:mytag:1"), u1 + ":1-10:mytag:1",
             "tagged, as @@gtid_executed prints it");
    CHECK_EQ(roundTrip(u1 + ":mytag:1-4:other:2"), u1 + ":mytag:1-4:other:2",
             "tags only");
    CHECK_EQ(roundTrip(u1 + ":zz:3:aa:1-2"), u1 + ":aa:1-2:zz:3", "tags sorted");
    CHECK_EQ(roundTrip("140F207E-E1F5-11F1-9A84-5E0D48933362:1"), u1 + ":1",
             "upper-case uuid");
    CHECK_EQ(roundTrip(u1 + ":1-10," + u1 + ":mytag:1"), u1 + ":1-10:mytag:1",
             "same uuid in two parts");
    CHECK_EQ(roundTrip(" " + u1 + ":1-3 ,\n " + u2 + ":4 \n"), u1 + ":1-3," + u2 + ":4",
             "whitespace around commas and at the ends");
    CHECK_EQ(roundTrip(u1 + ":1000000000000000000-9223372036854775806"),
             u1 + ":1000000000000000000-9223372036854775806", "19-digit numbers, up to the largest");

    // ---- malformed input is refused, never half-read
    const char* bad[] = {
        "abc",
        "140f207e-e1f5-11f1-9a84-5e0d48933362:",
        "140f207e-e1f5-11f1-9a84-5e0d48933362:0",
        "140f207e-e1f5-11f1-9a84-5e0d48933362:5-3",
        "140f207e-e1f5-11f1-9a84-5e0d48933362:1-",
        "140f207e-e1f5-11f1-9a84-5e0d48933362:1-2-3",
        "140f207e-e1f5-11f1-9a84-5e0d48933362:1x",
        "140f207e-e1f5-11f1-9a84-5e0d4893336:1",
        "140f207e_e1f5-11f1-9a84-5e0d48933362:1",
        "140f207e-e1f5-11f1-9a84-5e0d48933362:bad-tag:1",
        "140f207e-e1f5-11f1-9a84-5e0d48933362:1,zzz",
        "140f207e-e1f5-11f1-9a84-5e0d48933362:9999999999999999999",
        "140f207e-e1f5-11f1-9a84-5e0d48933362:9223372036854775807",
        "140f207e-e1f5-11f1-9a84-5e0d48933362:1 2",
        "140f207e-e1f5-11f1-9a84-5e0d48933362:1- 2",
        "140f207e-e1f5-11f1-9a84-5e0d48933362:1tag:1",
    };
    for (const char* b : bad) {
        MyVectorGtidSet s;
        bool ok = s.parse(b);
        CHECK_TRUE(!ok && s.empty(), b);
    }

    // ---- add and contains
    {
        MyVectorGtidSet s;
        CHECK_TRUE(s.empty(), "new set is empty");
        CHECK_TRUE(!s.contains(gtid(U1, 1)), "empty set contains nothing");
        s.add(gtid(U1, 3));
        s.add(gtid(U1, 1));
        CHECK_EQ(s.toString(), u1 + ":1:3", "add leaves a hole");
        s.add(gtid(U1, 2));
        CHECK_EQ(s.toString(), u1 + ":1-3", "add fills the hole");
        s.add(gtid(U1, 2));
        CHECK_EQ(s.toString(), u1 + ":1-3", "add twice");
        s.add(gtid(U1, 4));
        s.add(gtid(U1, 0));
        CHECK_EQ(s.toString(), u1 + ":1-4", "extend at the end; gno 0 is ignored");
        CHECK_TRUE(s.contains(gtid(U1, 1)) && s.contains(gtid(U1, 4)), "contains both ends");
        CHECK_TRUE(!s.contains(gtid(U1, 5)), "does not contain past the end");
        CHECK_TRUE(!s.contains(gtid(U2, 1)), "does not contain another server's");
        CHECK_TRUE(!s.contains(gtid(U1, 1, "mytag")), "a tagged gtid is a different one");
        s.add(gtid(U1, 1, "mytag"));
        CHECK_TRUE(s.contains(gtid(U1, 1, "mytag")), "contains the tagged one once added");
        CHECK_TRUE(s.contains(gtid(U1, 1, "MyTag")), "tags compare without case");
        CHECK_EQ(s.toString(), u1 + ":1-4:mytag:1", "tagged printed after untagged");
    }
    {
        MyVectorGtidSet s;
        s.parse(u1 + ":1-5:10-20:30");
        CHECK_TRUE(s.contains(gtid(U1, 5)) && !s.contains(gtid(U1, 6)) &&
                       !s.contains(gtid(U1, 9)) && s.contains(gtid(U1, 10)) &&
                       s.contains(gtid(U1, 20)) && !s.contains(gtid(U1, 21)) &&
                       s.contains(gtid(U1, 30)) && !s.contains(gtid(U1, 31)),
                   "contains around the interval edges");
    }

    // ---- union
    {
        MyVectorGtidSet a, b;
        a.parse(u1 + ":1-5:9");
        b.parse(u1 + ":4-8," + u2 + ":1-2:t:7");
        a.unionWith(b);
        CHECK_EQ(a.toString(), u1 + ":1-9," + u2 + ":1-2:t:7", "union");
        MyVectorGtidSet e;
        a.unionWith(e);
        CHECK_EQ(a.toString(), u1 + ":1-9," + u2 + ":1-2:t:7", "union with the empty set");
        e.unionWith(a);
        CHECK_TRUE(e == a, "union into the empty set");
        a.unionWith(a);
        CHECK_EQ(a.toString(), u1 + ":1-9," + u2 + ":1-2:t:7", "union with itself");
    }

    // ---- subset: which binlog file can an index resume from
    {
        MyVectorGtidSet base, prev;
        base.parse(u1 + ":1-100");
        prev.parse(u1 + ":1-80");
        CHECK_TRUE(base.containsAll(prev), "file starting before the checkpoint qualifies");
        prev.parse(u1 + ":1-100");
        CHECK_TRUE(base.containsAll(prev), "file starting exactly at the checkpoint qualifies");
        prev.parse(u1 + ":1-101");
        CHECK_TRUE(!base.containsAll(prev), "file starting after the checkpoint does not");
        prev.parse("");
        CHECK_TRUE(base.containsAll(prev), "the first binlog file always qualifies");
        prev.parse(u1 + ":1-50," + u2 + ":1");
        CHECK_TRUE(!base.containsAll(prev), "another server's transaction is not covered");
        prev.parse(u1 + ":1-50:t:1");
        CHECK_TRUE(!base.containsAll(prev), "a tagged transaction is not covered");
        base.parse(u1 + ":1-10:20-30");
        prev.parse(u1 + ":5-25");
        CHECK_TRUE(!base.containsAll(prev), "an interval spanning a hole is not covered");
        prev.parse(u1 + ":2-9:21-22");
        CHECK_TRUE(base.containsAll(prev), "intervals inside two of mine are covered");
        MyVectorGtidSet none;
        CHECK_TRUE(!none.containsAll(prev) && none.containsAll(none),
                   "the empty set covers only the empty set");
    }

    // ---- GTID_LOG_EVENT (type 33) body, bytes from a MySQL 9.7 binlog
    {
        std::vector<unsigned char> body =
            fromHex("00140f207ee1f511f19a845e0d48933362" "0800000000000000" "02");
        MyVectorGtid g;
        CHECK_TRUE(myvector_decode_gtid_event(body.data(), body.size(), &g), "decode type 33");
        CHECK_EQ(g.uuid, u1, "type 33 uuid");
        CHECK_TRUE(g.gno == 8 && g.tag.empty(), "type 33 gno, no tag");
        CHECK_TRUE(!myvector_decode_gtid_event(body.data(), 24, &g), "type 33 too short");
    }

    // ---- GTID_TAGGED_LOG_EVENT (type 42) bodies, captured from real binlogs
    //      after SET gtid_next='AUTOMATIC:mytag' (4-byte checksum included)
    {
        struct {
            const char* what;
            const char* hex;
            const char* uuid;
            size_t need;  // bytes up to the end of the tag
        } cases[] = {
            {"9.7",
             "027200000002281e40fc8503d50322c50369021102bc1a904d0266c40404060a6d79746167"
             "080c0a100c7f109266e7696006103d041283120b212cd9bb",
             "140f207e-e1f5-11f1-9a84-5e0d48933362", 37},
            {"8.4",
             "0274000000023c786c968503d50322c5034502bd033c6903b85503e2d60404060a6d7974"
             "6167080c0a100c7fc2d64ce869600610410412ebd00936efb240",
             "1e3c364b-e1f5-11f1-91ef-1eda5cd5716b", 38},
            {"26.10",
             "0272000000024e88ceb48503d50322c503d102041902209604aa15030404060a6d79746167"
             "080c0a100c7f46ad33e9696006103d041243dc1f5733c8cc",
             "2744675a-e1f5-11f1-b402-86104b0255c5", 37},
        };
        for (const auto& c : cases) {
            std::vector<unsigned char> body = fromHex(c.hex);
            MyVectorGtid g;
            bool ok = myvector_decode_tagged_gtid_event(body.data(), body.size(), &g);
            std::string what = std::string("decode type 42 from ") + c.what;
            CHECK_TRUE(ok, what.c_str());
            CHECK_EQ(g.uuid, c.uuid, (what + ": uuid").c_str());
            CHECK_EQ(g.tag, "mytag", (what + ": tag").c_str());
            CHECK_TRUE(g.gno == 1, (what + ": gno").c_str());
            // every truncation is refused, none reads past the end
            bool any = false;
            for (size_t n = 0; n < c.need; n++) {
                MyVectorGtid t;
                if (myvector_decode_tagged_gtid_event(body.data(), n, &t))
                    any = true;
            }
            MyVectorGtid whole;
            CHECK_TRUE(!any && myvector_decode_tagged_gtid_event(body.data(), c.need, &whole),
                       (what + ": every shorter body refused, the shortest whole one read").c_str());
        }
        {
            // a tag starting with a digit could be printed but not parsed back
            std::vector<unsigned char> body = fromHex(cases[0].hex);
            body[32] = '1';
            MyVectorGtid t;
            CHECK_TRUE(!myvector_decode_tagged_gtid_event(body.data(), body.size(), &t),
                       "type 42 with a tag starting with a digit refused");
        }
        std::vector<unsigned char> junk(64, 0xff);
        MyVectorGtid g;
        CHECK_TRUE(!myvector_decode_tagged_gtid_event(junk.data(), junk.size(), &g),
                   "type 42 junk refused");
    }

    // ---- variable-length integers
    {
        struct {
            const char* hex;
            uint64_t value;
        } cases[] = {{"00", 0}, {"02", 1}, {"fe", 127}, {"0102", 128}, {"8503", 0xe1},
                     {"ff0100000000000000", 1}};
        for (const auto& c : cases) {
            std::vector<unsigned char> b = fromHex(c.hex);
            size_t pos = 0;
            uint64_t v = 99;
            bool ok = myvector_read_varuint(b.data(), b.size(), &pos, &v);
            CHECK_TRUE(ok && v == c.value && pos == b.size(), c.hex);
        }
    }

    if (failures) {
        std::printf("\n%d check(s) FAILED\n", failures);
        return 1;
    }
    std::printf("\nAll checks passed\n");
    return 0;
}
