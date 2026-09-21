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
