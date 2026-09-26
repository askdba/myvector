// Standalone unit test for MyVectorOptions (include/myvectorutils.h).
//
// Build and run from the repository root:
//   g++ -std=c++17 -I include tests/test_myvector_options.cc -o /tmp/test_myvector_options && /tmp/test_myvector_options
//
// Background: index options are read from the column COMMENT. The documented form has a
// '|' start marker ("MYVECTOR Column |type=HNSW,..."), but the smoke, benchmark and
// pre-release scripts write "MYVECTOR COLUMN type=hnsw,..." with no marker. Without
// handling for that prefix the first key parsed as "MYVECTOR COLUMN type", the index
// type came back empty and the index silently fell back to KNN.

#include <cstdio>
#include <string>

#include "myvectorutils.h"

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

int main() {
    {
        // Form used by the scripts: prefix, no '|' marker.
        MyVectorOptions o("MYVECTOR COLUMN type=hnsw,dim=3,size=1000,m=16,ef=50,idcol=id,dist=L2");
        CHECK_TRUE(o.isValid(), "no-pipe prefix: valid");
        CHECK_EQ(o.getOption("type"), "hnsw", "no-pipe prefix: type");
        CHECK_EQ(o.getOption("dim"), "3", "no-pipe prefix: dim");
        CHECK_EQ(o.getOption("idcol"), "id", "no-pipe prefix: idcol");
        CHECK_EQ(o.getOption("dist"), "L2", "no-pipe prefix: dist");
    }
    {
        // The prefix is matched case-insensitively (the stored procedures use LOCATE,
        // which is case-insensitive) and tolerates extra spaces.
        MyVectorOptions o("myvector column   type=HNSW,dim=50");
        CHECK_TRUE(o.isValid(), "lower-case prefix: valid");
        CHECK_EQ(o.getOption("type"), "HNSW", "lower-case prefix: type");
        CHECK_EQ(o.getOption("dim"), "50", "lower-case prefix: dim");
    }
    {
        // Documented form with the '|' start marker must keep working.
        MyVectorOptions o("MYVECTOR Column |type=HNSW,dim=50,size=400000,dist=L2,m=64,ef=100");
        CHECK_TRUE(o.isValid(), "pipe marker: valid");
        CHECK_EQ(o.getOption("type"), "HNSW", "pipe marker: type");
        CHECK_EQ(o.getOption("m"), "64", "pipe marker: m");
    }
    {
        // Bare option list (used for search options such as "nn=10,ef_search=64").
        MyVectorOptions o("nn=10,ef_search=64");
        CHECK_TRUE(o.isValid(), "bare list: valid");
        CHECK_EQ(o.getOption("nn"), "10", "bare list: nn");
    }
    {
        // A key that merely starts like the prefix must not be eaten.
        MyVectorOptions o("MYVECTORX=1,type=KNN");
        CHECK_EQ(o.getOption("type"), "KNN", "similar-looking key: type");
    }
    {
        // #158: a line break, tab or CRLF after the prefix (a multi-line COMMENT) must
        // parse the same as a space. It used to leave the prefix in the first key, so
        // the type read as empty and the index silently fell back to KNN.
        const char* forms[][2] = {
            {"MYVECTOR COLUMN\n    type=hnsw,dim=16,idcol=id,dist=L2", "newline after prefix"},
            {"MYVECTOR COLUMN\ttype=hnsw,dim=16,idcol=id,dist=L2", "tab after prefix"},
            {"MYVECTOR COLUMN\r\ntype=hnsw,dim=16,idcol=id,dist=L2", "CRLF after prefix"},
            {"\n  MYVECTOR COLUMN type=hnsw,dim=16,idcol=id,dist=L2", "leading newline"},
            {"\t MYVECTOR COLUMN\n type=hnsw,\n dim=16,\n idcol=id,\n dist=L2\n",
             "whitespace around every option"},
        };
        for (auto& f : forms) {
            MyVectorOptions o(f[0]);
            std::string what = f[1];
            CHECK_TRUE(o.isValid(), (what + ": valid").c_str());
            CHECK_EQ(o.getOption("type"), "hnsw", (what + ": type").c_str());
            CHECK_EQ(o.getOption("dim"), "16", (what + ": dim").c_str());
            CHECK_EQ(o.getOption("dist"), "L2", (what + ": dist").c_str());
        }
    }
    {
        // Pipe marker with a line break after it.
        MyVectorOptions o("MYVECTOR Column |\n  type=HNSW_BV,dim=64");
        CHECK_EQ(o.getOption("type"), "HNSW_BV", "pipe marker + newline: type");
    }
    {
        // The prefix must be followed by whitespace: a longer key is not truncated.
        MyVectorOptions o("MYVECTOR COLUMNX=1,type=KNN");
        CHECK_EQ(o.getOption("MYVECTOR COLUMNX"), "1", "MYVECTOR COLUMNX key kept whole");
        CHECK_EQ(o.getOption("type"), "KNN", "MYVECTOR COLUMNX: type");
    }
    {
        // lrtrim trims all whitespace at both ends and still collapses inner space runs.
        CHECK_EQ(lrtrim(" \t\r\n a  b \n\t"), "a b", "lrtrim: all whitespace");
        CHECK_EQ(lrtrim("abc"), "abc", "lrtrim: nothing to trim");
        CHECK_EQ(lrtrim(" \n\t "), "", "lrtrim: whitespace only");
        std::vector<std::string> parts;
        split("test.t1,\n 'id',\n\ttest.t1.v", parts);
        CHECK_TRUE(parts.size() == 3, "split: 3 parts");
        CHECK_EQ(parts[1], "'id'", "split: newline before element trimmed");
        CHECK_EQ(parts[2], "test.t1.v", "split: tab before element trimmed");
    }
    {
        // Malformed input is still rejected.
        MyVectorOptions o("MYVECTOR COLUMN type");
        CHECK_TRUE(!o.isValid(), "prefix then key without '=': invalid");
    }

    {
        // Index file base path. An empty index dir (the component default: nothing sets
        // myvector_index_dir) used to give "/<name>", the filesystem root, which mysqld
        // cannot write; it must be relative to the server's working directory (datadir).
        CHECK_EQ(MyVectorIndexBase("", "db.t.vec"), "./db.t.vec", "empty index dir -> relative");
        CHECK_EQ(MyVectorIndexBase("/var/lib/mysql", "db.t.vec"), "/var/lib/mysql/db.t.vec",
                 "index dir without trailing slash");
        CHECK_EQ(MyVectorIndexBase("/var/lib/mysql/", "db.t.vec"), "/var/lib/mysql/db.t.vec",
                 "index dir with trailing slash");
    }

    if (failures) {
        std::printf("\n%d check(s) failed\n", failures);
        return 1;
    }
    std::printf("\nall checks passed\n");
    return 0;
}
