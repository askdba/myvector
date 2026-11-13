/*
   Copyright (c) 2024 - p3io.in / shiyer22@gmail.com

   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.
*/

#include "myvector_innodb_store.h"
#include "myvector_index_store.h"

#include <mysql/components/component_implementation.h>
#include <mysql/components/services/mysql_command_services.h>

#include <condition_variable>
#include <cstring>
#include <deque>
#include <future>
#include <mutex>
#include <system_error>
#include <thread>
#include <zlib.h>

#include "myvector_log.h"

REQUIRES_SERVICE_PLACEHOLDER(mysql_command_factory);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_options);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_query);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_query_result);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_field_info);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_error_info);
REQUIRES_SERVICE_PLACEHOLDER(mysql_command_thread);

namespace myvector_store {

namespace {

constexpr char kStoreUser[] = "mysql.myvector";
constexpr char kStoreHost[] = "localhost";

std::string session_error(MYSQL_H h) {
    unsigned int no = 0;
    char buf[512] = "";  // sql_error copies into the caller's buffer before 26.10
    char* msg = buf;
    mysql_service_mysql_command_error_info->sql_errno(h, &no);
    mysql_service_mysql_command_error_info->sql_error(h, &msg);
    return "error " + std::to_string(no) + ": " + (msg ? msg : "");
}

class LocalSession : public Session {
public:
    explicit LocalSession(MYSQL_H h) : m_h(h) {}

    bool exec(const std::string& sql, std::string* err) override {
        m_affected = 0;
        if (mysql_service_mysql_command_query->query(m_h, sql.data(), sql.size())) {
            if (err) *err = session_error(m_h);
            return false;
        }
        /* Read before drain(): on 26.10, draining resets the count. */
        uint64_t rows = 0;
        if (!mysql_service_mysql_command_query->affected_rows(m_h, &rows))
            m_affected = rows;
        drain();
        return true;
    }

    bool query(const std::string& sql, const std::function<bool(const Row&)>& on_row,
               std::string* err) override {
        if (mysql_service_mysql_command_query->query(m_h, sql.data(), sql.size())) {
            if (err) *err = session_error(m_h);
            return false;
        }
        MYSQL_RES_H res = nullptr;
        if (mysql_service_mysql_command_query_result->store_result(m_h, &res) ||
            res == nullptr) {
            if (err) *err = "query returned no result set";
            drain();
            return false;
        }
        unsigned int n = 0;
        mysql_service_mysql_command_field_info->num_fields(res, &n);
        MYSQL_ROW_H row = nullptr;
        ulong* lengths = nullptr;
        Row r;
        bool keep = true;
        while (keep && !mysql_service_mysql_command_query_result->fetch_row(res, &row) &&
               row != nullptr) {
            mysql_service_mysql_command_query_result->fetch_lengths(res, &lengths);
            r.cols.assign(n, std::string());
            r.is_null.assign(n, false);
            for (unsigned int i = 0; i < n; i++) {
                if (row[i] == nullptr)
                    r.is_null[i] = true;
                else
                    r.cols[i].assign(row[i], lengths ? lengths[i] : strlen(row[i]));
            }
            keep = on_row(r);
        }
        mysql_service_mysql_command_query_result->free_result(res);
        drain();
        return true;
    }

    unsigned long long affected_rows() override { return m_affected; }

private:
    /* Discard any further result sets so the session is ready for the next
     * statement. */
    void drain() {
        while (mysql_service_mysql_command_query_result->next_result(m_h) == 0) {
            MYSQL_RES_H more = nullptr;
            if (!mysql_service_mysql_command_query_result->store_result(m_h, &more) && more)
                mysql_service_mysql_command_query_result->free_result(more);
        }
    }

    MYSQL_H m_h;
    unsigned long long m_affected = 0;  ///< rows changed by the last exec()
};

/* The storage thread: runs queued jobs one at a time. */
class StoreThread {
public:
    bool start() {
        std::lock_guard<std::mutex> l(m_mutex);
        if (m_running)
            return true;
        m_stop = false;
        try {
            m_thread = std::thread([this] { loop(); });
        } catch (const std::system_error& e) {
            MYVEC_LOG_ERROR("MyVector storage thread could not start: %s", e.what());
            return false;  // the component init fails instead of mysqld
        }
        m_running = true;
        return true;
    }

    void stop() {
        {
            std::lock_guard<std::mutex> l(m_mutex);
            if (!m_running)
                return;
            m_stop = true;
        }
        m_cv.notify_all();
        m_thread.join();
        std::lock_guard<std::mutex> l(m_mutex);
        m_running = false;
    }

    /* Queue job and wait for it. False if the thread isn't running. */
    bool submit(std::function<void()> job) {
        std::packaged_task<void()> task(std::move(job));
        std::future<void> done = task.get_future();
        {
            std::lock_guard<std::mutex> l(m_mutex);
            if (!m_running || m_stop)
                return false;
            m_jobs.push_back(std::move(task));
        }
        m_cv.notify_one();
        done.wait();
        return true;
    }

private:
    void loop() {
        /* Sessions opened on this thread need it set up for the session
         * service, as component_mcp's workers do. */
        bool thread_ok = !mysql_service_mysql_command_thread->init();
        if (!thread_ok)
            MYVEC_LOG_ERROR("MyVector storage thread: session service init failed");
        for (;;) {
            std::packaged_task<void()> job;
            {
                std::unique_lock<std::mutex> l(m_mutex);
                m_cv.wait(l, [this] { return m_stop || !m_jobs.empty(); });
                if (m_jobs.empty())
                    break;  // stopping, nothing left to do
                job = std::move(m_jobs.front());
                m_jobs.pop_front();
            }
            job();
        }
        if (thread_ok)
            mysql_service_mysql_command_thread->end();
    }

    std::mutex m_mutex;
    std::condition_variable m_cv;
    std::deque<std::packaged_task<void()>> m_jobs;
    std::thread m_thread;
    bool m_running = false;
    bool m_stop = false;
};

StoreThread g_thread;

}  // namespace

bool start() { return g_thread.start(); }

void stop() { g_thread.stop(); }

bool run(const std::function<bool(Session&, std::string*)>& fn, std::string* err) {
    bool ok = false;
    std::string e;
    bool submitted = g_thread.submit([&] {
        MYSQL_H h = nullptr;
        if (mysql_service_mysql_command_factory->init(&h) || h == nullptr) {
            e = "could not create a storage session";
            return;
        }
        if (mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_PROTOCOL, "local") ||
            mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_USER_NAME, kStoreUser) ||
            mysql_service_mysql_command_options->set(h, MYSQL_COMMAND_HOST_NAME, kStoreHost) ||
            mysql_service_mysql_command_factory->connect(h)) {
            e = "could not open a storage session as 'mysql.myvector'@'localhost' (" +
                session_error(h) +
                "); was sql/myvector_install_component.sql run?";
            mysql_service_mysql_command_factory->close(h);
            return;
        }
        LocalSession s(h);
        ok = fn(s, &e);
        mysql_service_mysql_command_factory->close(h);
    });
    if (!submitted)
        e = "the MyVector storage thread is not running";
    if (!ok && err)
        *err = e;
    return ok;
}

std::string hex(const char* data, size_t len) {
    static const char digits[] = "0123456789ABCDEF";
    std::string out;
    out.resize(len * 2);
    for (size_t i = 0; i < len; i++) {
        unsigned char c = static_cast<unsigned char>(data[i]);
        out[2 * i] = digits[c >> 4];
        out[2 * i + 1] = digits[c & 0x0F];
    }
    return out;
}

bool unhex(const std::string& s, std::string* out) {
    auto val = [](char c) -> int {
        if (c >= '0' && c <= '9') return c - '0';
        if (c >= 'A' && c <= 'F') return c - 'A' + 10;
        if (c >= 'a' && c <= 'f') return c - 'a' + 10;
        return -1;
    };
    if (s.size() % 2 != 0)
        return false;
    out->resize(s.size() / 2);
    for (size_t i = 0; i < out->size(); i++) {
        int hi = val(s[2 * i]), lo = val(s[2 * i + 1]);
        if (hi < 0 || lo < 0)
            return false;
        (*out)[i] = static_cast<char>((hi << 4) | lo);
    }
    return true;
}

/* ---- storage=innodb indexes (RFC-005 Phase 1a) ---------------------------- */

namespace {

constexpr int kFormatVersion = 1;
constexpr size_t kVecChunkTarget = 4u << 20;    // RFC-005 D5
constexpr size_t kLinkChunkTarget = 256u << 10;
constexpr size_t kTxnBytes = 64u << 20;         // RFC-005 D9
constexpr size_t kStatementOverhead = 64u << 10;
constexpr size_t kLoadBatchBytes = 64u << 20;   // per SELECT while loading
constexpr int kDeleteBatch = 1000;

unsigned int crc(const std::string& d) {
    return (unsigned int)crc32(0L, reinterpret_cast<const Bytef*>(d.data()),
                               (uInt)d.size());
}

std::string lit(const std::string& bytes) {
    return "X'" + hex(bytes.data(), bytes.size()) + "'";
}

/* index_meta lookup by the name's hash; the stored name is compared byte for
 * byte afterwards (RFC-005 4.2). */
std::string by_name(const std::string& name) {
    return "name_sha256 = UNHEX(SHA2(" + lit(name) + ", 256))";
}

bool scalar(Session& s, const std::string& sql, std::string* out, std::string* err) {
    bool got = false;
    if (!s.query(sql,
                 [&](const Row& r) {
                     got = true;
                     *out = r.is_null[0] ? "" : r.cols[0];
                     return false;
                 },
                 err))
        return false;
    if (!got)
        *out = "";
    return true;
}

/* DELETE in small batches, each its own transaction. */
bool delete_batched(Session& s, const std::string& table, const std::string& where,
                    std::string* err) {
    for (;;) {
        if (!s.exec("DELETE FROM myvector." + table + " WHERE " + where + " LIMIT " +
                        std::to_string(kDeleteBatch),
                    err))
            return false;
        /* Another batch only when this one was full: a count that is not
         * exactly the batch size, however odd, ends the loop. */
        if (s.affected_rows() != (unsigned long long)kDeleteBatch)
            return true;
    }
}

bool session_setup(Session& s, std::string* err) {
    return s.exec("SET SESSION sql_log_bin = 0", err) &&
           s.exec("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ", err) &&
           s.exec("SET SESSION autocommit = 1", err);
}

std::string server_problem(Session& s) {
    std::string err;
    std::string gtid, ro, sro;
    if (!scalar(s, "SELECT @@GLOBAL.gtid_mode", &gtid, &err) ||
        !scalar(s, "SELECT @@GLOBAL.read_only", &ro, &err) ||
        !scalar(s, "SELECT @@GLOBAL.super_read_only", &sro, &err))
        return "could not read server settings: " + err;
    if (gtid != "ON")
        return "storage=innodb needs gtid_mode=ON (it is " + gtid + ")";
    if (ro == "1" || sro == "1")
        return std::string("storage=innodb can't be used while ") +
               (sro == "1" ? "super_read_only" : "read_only") +
               " is on (RFC-005 D13); use storage=file on this server";
    return "";
}

class InnoDbIndexStore : public IndexStore {
public:
    std::string checkServer() override {
        std::string problem, err;
        bool ok = run(
            [&](Session& s, std::string* e) {
                problem = server_problem(s);
                (void)e;
                return true;
            },
            &err);
        return ok ? problem : err;
    }

    bool save(const std::string& name, const GraphSource& src, std::string* err) override {
        return run(
            [&](Session& s, std::string* e) { return save_in(s, name, src, e); }, err);
    }

    LoadStatus load(const std::string& name, const std::string& metric, int dim,
                    const GraphSink& sink, std::string* err) override {
        LoadStatus status = LoadStatus::Failed;
        bool ran = run(
            [&](Session& s, std::string* e) {
                status = load_in(s, name, metric, dim, sink, e);
                s.exec("COMMIT", nullptr);
                return status != LoadStatus::Failed;
            },
            err);
        return ran || status != LoadStatus::Failed ? status : LoadStatus::Failed;
    }

    bool drop(const std::string& name, std::string* err) override {
        return run(
            [&](Session& s, std::string* e) {
                if (!session_setup(s, e))
                    return false;
                std::string id;
                if (!scalar(s, "SELECT index_id FROM myvector.index_meta WHERE " + by_name(name),
                            &id, e))
                    return false;
                if (id.empty())
                    return true;  // nothing stored
                // Tombstone first: the index is gone at once and keeps its
                // generation counter (RFC-005 4.11).
                if (!s.exec("UPDATE myvector.index_meta SET state = 'dropped' WHERE index_id = " +
                                id,
                            e))
                    return false;
                for (const char* t : {"index_vectors", "index_links", "index_upper"})
                    if (!delete_batched(s, t, "index_id = " + id, e))
                        return false;
                return true;
            },
            err);
    }

    bool exists(const std::string& name) override {
        std::string state, err;
        bool ok = run(
            [&](Session& s, std::string* e) {
                return scalar(s, "SELECT state FROM myvector.index_meta WHERE " + by_name(name),
                              &state, e);
            },
            &err);
        return !ok || state == "active";
    }

private:
    static bool save_in(Session& s, const std::string& name, const GraphSource& src,
                        std::string* e) {
        if (!session_setup(s, e))
            return false;
        std::string problem = server_problem(s);
        if (!problem.empty()) {
            *e = problem;
            return false;
        }

        // Chunk sizes: the RFC defaults, clamped so a dumped row (blobs doubled
        // by --hex-blob) and an INSERT with a hex literal still fit in one
        // statement (RFC-005 4.3).
        std::string map_s;
        if (!scalar(s, "SELECT @@GLOBAL.max_allowed_packet", &map_s, e))
            return false;
        size_t map = std::stoull(map_s);
        size_t row_limit = map > kStatementOverhead ? (map - kStatementOverhead) / 2 : 0;
        size_t vec_nodes =
            src.vector_record ? std::min(kVecChunkTarget, row_limit) / src.vector_record : 0;
        size_t lnk_nodes =
            src.link_record ? std::min(kLinkChunkTarget, row_limit) / src.link_record : 0;
        if (vec_nodes == 0 || lnk_nodes == 0) {
            *e = "max_allowed_packet (" + map_s +
                 ") is too small to store one node of this index; raise it to at least " +
                 std::to_string(2 * std::max(src.vector_record, src.link_record) +
                                kStatementOverhead);
            return false;
        }

        // 1. Find or create the index_meta row; a new one starts as a
        //    tombstone with generation 0 (RFC-005 4.6). Standalone mode: this
        //    server is the writer, so take writer_uuid (a copied datadir or a
        //    restore has another server's).
        std::string id, gen, stored_name;
        if (!s.exec("START TRANSACTION", e))
            return false;
        bool found = false;
        if (!s.query("SELECT index_id, generation, HEX(index_name) FROM myvector.index_meta "
                     "WHERE " + by_name(name) + " FOR UPDATE",
                     [&](const Row& r) {
                         found = true;
                         id = r.cols[0];
                         gen = r.cols[1];
                         stored_name = r.cols[2];
                         return false;
                     },
                     e))
            return false;
        if (found && stored_name != hex(name.data(), name.size())) {
            *e = "another index already uses this name's hash slot";
            s.exec("ROLLBACK", nullptr);
            return false;
        }
        if (!found) {
            if (!s.exec("INSERT INTO myvector.index_meta (index_name, name_sha256, state, "
                        "format_version, generation, ckpt_seq, ckpt_chunks, header, "
                        "header_crc32, metric, dim, vec_chunk_nodes, lnk_chunk_nodes, "
                        "ckpt_gtids, writer_uuid, ckpt_time, node_count) VALUES (" +
                            lit(name) + ", UNHEX(SHA2(" + lit(name) + ", 256)), 'dropped', " +
                            std::to_string(kFormatVersion) +
                            ", 0, 0, 0, '', 0, '', 0, 0, 0, '', @@server_uuid, NOW(6), 0)",
                        e) ||
                !scalar(s, "SELECT LAST_INSERT_ID()", &id, e))
                return false;
            gen = "0";
        } else if (!s.exec("UPDATE myvector.index_meta SET writer_uuid = @@server_uuid "
                           "WHERE index_id = " + id,
                           e)) {
            return false;
        }
        if (!s.exec("COMMIT", e))
            return false;
        unsigned long long old_gen = std::stoull(gen), g = old_gen + 1;
        const std::string G = std::to_string(g), I = id;

        // 2. Rows left by an earlier build that stopped before publishing.
        for (const char* t : {"index_vectors", "index_links", "index_upper"})
            if (!delete_batched(s, t, "index_id = " + I + " AND generation >= " + G, e))
                return false;

        // 3. Write every chunk and upper row as version 1 of generation g, in
        //    bounded transactions.
        size_t txn = 0, chunks = 0;
        auto put = [&](const std::string& sql, size_t bytes) {
            if (txn == 0 && !s.exec("START TRANSACTION", e))
                return false;
            if (!s.exec(sql, e))
                return false;
            txn += bytes;
            chunks++;
            if (txn >= kTxnBytes) {
                txn = 0;
                return s.exec("COMMIT", e);
            }
            return true;
        };
        const size_t n = src.node_count;
        for (size_t k = 0, first = 0; first < n; k++, first += vec_nodes) {
            std::string d = src.vectors(first, std::min(vec_nodes, n - first));
            if (!put("INSERT INTO myvector.index_vectors VALUES (" + I + ", " + G + ", " +
                         std::to_string(k) + ", 1, " + lit(d) + ", " +
                         std::to_string(crc(d)) + ")",
                     d.size()))
                return false;
        }
        for (size_t k = 0, first = 0; first < n; k++, first += lnk_nodes) {
            std::string d = src.links(first, std::min(lnk_nodes, n - first));
            if (!put("INSERT INTO myvector.index_links VALUES (" + I + ", " + G + ", " +
                         std::to_string(k) + ", 1, " + lit(d) + ", " +
                         std::to_string(crc(d)) + ")",
                     d.size()))
                return false;
        }
        std::string batch;
        size_t batch_bytes = 0;
        auto flush_upper = [&]() {
            if (batch.empty())
                return true;
            bool ok = put("INSERT INTO myvector.index_upper VALUES " + batch, batch_bytes);
            batch.clear();
            batch_bytes = 0;
            return ok;
        };
        for (size_t node = 0; node < n; node++) {
            std::string d = src.upper(node);
            if (d.empty())
                continue;
            if (d.size() > row_limit) {
                *e = "a node's upper-level links (" + std::to_string(d.size()) +
                     " bytes) don't fit in max_allowed_packet (" + map_s + ")";
                return false;
            }
            std::string row = std::string(batch.empty() ? "" : ", ") + "(" + I + ", " + G +
                              ", " + std::to_string(node) + ", 1, " + lit(d) + ", " +
                              std::to_string(crc(d)) + ")";
            if (batch_bytes + 2 * d.size() + 128 > row_limit && !flush_upper())
                return false;
            if (batch.empty() && row[0] == ',')
                row.erase(0, 2);
            batch += row;
            batch_bytes += d.size();
        }
        if (!flush_upper())
            return false;
        if (txn > 0 && !s.exec("COMMIT", e))
            return false;

        // 4. Publish with a compare-and-set: exactly one row must change.
        if (!s.exec("UPDATE myvector.index_meta SET generation = " + G +
                        ", ckpt_seq = 1, ckpt_chunks = " + std::to_string(chunks) +
                        ", state = 'active', format_version = " +
                        std::to_string(kFormatVersion) + ", header = " + lit(src.header) +
                        ", header_crc32 = " + std::to_string(crc(src.header)) +
                        ", metric = CONVERT(" + lit(src.metric) + " USING utf8mb4), dim = " +
                        std::to_string(src.dim) +
                        ", vec_chunk_nodes = " + std::to_string(vec_nodes) +
                        ", lnk_chunk_nodes = " + std::to_string(lnk_nodes) +
                        ", ckpt_gtids = '', ckpt_time = NOW(6), node_count = " +
                        std::to_string(n) + " WHERE index_id = " + I +
                        " AND generation = " + std::to_string(old_gen) +
                        " AND writer_uuid = @@server_uuid",
                    e))
            return false;
        if (s.affected_rows() != 1) {
            *e = "the index changed while it was being saved; try again";
            return false;
        }

        // 5. The previous generation.
        for (const char* t : {"index_vectors", "index_links", "index_upper"})
            if (!delete_batched(s, t, "index_id = " + I + " AND generation < " + G, e))
                return false;
        return true;
    }

    static LoadStatus load_in(Session& s, const std::string& name, const std::string& metric,
                              int dim, const GraphSink& sink, std::string* e) {
        if (!session_setup(s, e))
            return LoadStatus::Failed;
        std::string problem = server_problem(s);
        if (!problem.empty()) {
            *e = problem;
            return LoadStatus::Failed;
        }
        // One snapshot for every read below (REPEATABLE READ, set above).
        if (!s.exec("START TRANSACTION WITH CONSISTENT SNAPSHOT", e))
            return LoadStatus::Failed;
        Row meta;
        bool found = false;
        if (!s.query("SELECT index_id, state, format_version, generation, ckpt_seq, "
                     "HEX(header), header_crc32, metric, dim, vec_chunk_nodes, "
                     "lnk_chunk_nodes, node_count, HEX(index_name) "
                     "FROM myvector.index_meta WHERE " + by_name(name),
                     [&](const Row& r) {
                         found = true;
                         meta = r;
                         return false;
                     },
                     e))
            return LoadStatus::Failed;
        if (!found || meta.cols[1] != "active" ||
            meta.cols[12] != hex(name.data(), name.size()))
            return LoadStatus::NotSaved;
        const std::string I = meta.cols[0], G = meta.cols[3], SEQ = meta.cols[4];
        std::string header;
        if (std::stoi(meta.cols[2]) != kFormatVersion) {
            *e = "stored index has format version " + meta.cols[2] + ", expected " +
                 std::to_string(kFormatVersion);
            return LoadStatus::Failed;
        }
        if (!unhex(meta.cols[5], &header) || crc(header) != std::stoul(meta.cols[6])) {
            *e = "stored index header fails its CRC check";
            return LoadStatus::Failed;
        }
        if (meta.cols[7] != metric || std::stoi(meta.cols[8]) != dim) {
            *e = "stored index has metric " + meta.cols[7] + " and dim " + meta.cols[8] +
                 ", but the column says metric " + metric + " and dim " + std::to_string(dim);
            return LoadStatus::Failed;
        }
        const size_t vec_nodes = std::stoull(meta.cols[9]);
        const size_t lnk_nodes = std::stoull(meta.cols[10]);
        const size_t n = std::stoull(meta.cols[11]);
        if (n > 0 && (vec_nodes == 0 || lnk_nodes == 0)) {
            *e = "stored index has an invalid chunk size";
            return LoadStatus::Failed;
        }
        try {
            sink.begin(header, n);
            if (!read_chunks(s, "index_vectors", "data", I, G, SEQ, n, vec_nodes,
                             kVecChunkTarget, sink.vectors, e) ||
                !read_chunks(s, "index_links", "level0", I, G, SEQ, n, lnk_nodes,
                             kLinkChunkTarget, sink.links, e))
                return LoadStatus::Failed;
            sink.end_level0();
            if (!read_upper(s, I, G, SEQ, n, sink.upper, e))
                return LoadStatus::Failed;
            sink.finish();
        } catch (const std::exception& ex) {
            *e = ex.what();
            return LoadStatus::Failed;
        }
        return LoadStatus::Ok;
    }

    /* Read chunks 0 .. ceil(n / per)-1, newest version <= seq of each, in
     * batches; every chunk must be present exactly once and pass its CRC. */
    static bool read_chunks(Session& s, const char* table, const char* col,
                            const std::string& I, const std::string& G,
                            const std::string& SEQ, size_t n, size_t per,
                            size_t chunk_bytes,
                            const std::function<void(size_t, const std::string&)>& feed,
                            std::string* e) {
        const size_t total = n == 0 ? 0 : (n + per - 1) / per;
        size_t next = 0;  // the chunk id expected next
        // HEX doubles what the server returns; keep each result set near kLoadBatchBytes.
        const size_t per_query =
            std::max<size_t>(1, kLoadBatchBytes / (2 * std::max<size_t>(1, chunk_bytes)));
        std::string fail;
        for (size_t a = 0; a < total; a += per_query) {
            size_t b = std::min(total, a + per_query) - 1;
            bool ok = s.query(
                std::string("SELECT chunk_id, crc32, HEX(") + col + ") FROM myvector." + table +
                    " WHERE index_id = " + I + " AND generation = " + G + " AND version <= " +
                    SEQ + " AND chunk_id BETWEEN " + std::to_string(a) + " AND " +
                    std::to_string(b) + " ORDER BY chunk_id, version DESC",
                [&](const Row& r) {
                    size_t k = std::stoull(r.cols[0]);
                    if (k + 1 == next)
                        return true;  // an older version of the chunk just read
                    if (k != next) {
                        fail = std::string(table) + ": chunk " + std::to_string(next) + " is missing";
                        return false;
                    }
                    std::string d;
                    if (!unhex(r.cols[2], &d) || crc(d) != std::stoul(r.cols[1])) {
                        fail = std::string(table) + ": chunk " + std::to_string(k) +
                               " fails its CRC check";
                        return false;
                    }
                    feed(k * per, d);
                    next = k + 1;
                    return true;
                },
                e);
            if (!ok)
                return false;
            if (!fail.empty()) {
                *e = fail;
                return false;
            }
        }
        if (next != total) {
            *e = std::string(table) + ": chunk " + std::to_string(next) + " is missing";
            return false;
        }
        return true;
    }

    static bool read_upper(Session& s, const std::string& I, const std::string& G,
                           const std::string& SEQ, size_t n,
                           const std::function<void(size_t, const std::string&)>& feed,
                           std::string* e) {
        const size_t per_query = 20000;
        long long last = -1;
        std::string fail;
        for (size_t a = 0; a < n; a += per_query) {
            size_t b = std::min(n, a + per_query) - 1;
            bool ok = s.query(
                "SELECT node_id, crc32, HEX(links) FROM myvector.index_upper WHERE index_id = " +
                    I + " AND generation = " + G + " AND version <= " + SEQ +
                    " AND node_id BETWEEN " + std::to_string(a) + " AND " + std::to_string(b) +
                    " ORDER BY node_id, version DESC",
                [&](const Row& r) {
                    long long node = std::stoll(r.cols[0]);
                    if (node == last)
                        return true;  // older version
                    std::string d;
                    if (!unhex(r.cols[2], &d) || crc(d) != std::stoul(r.cols[1])) {
                        fail = "index_upper: node " + r.cols[0] + " fails its CRC check";
                        return false;
                    }
                    feed((size_t)node, d);
                    last = node;
                    return true;
                },
                e);
            if (!ok)
                return false;
            if (!fail.empty()) {
                *e = fail;
                return false;
            }
        }
        // Rows past the node count are rejected by the index (node out of range).
        bool extra = false;
        if (!s.query("SELECT 1 FROM myvector.index_upper WHERE index_id = " + I +
                         " AND generation = " + G + " AND version <= " + SEQ +
                         " AND node_id >= " + std::to_string(n) + " LIMIT 1",
                     [&](const Row&) {
                         extra = true;
                         return false;
                     },
                     e))
            return false;
        if (extra) {
            *e = "index_upper: a row names a node past the node count";
            return false;
        }
        return true;
    }
};

InnoDbIndexStore g_store;

}  // namespace

IndexStore* index_store() { return &g_store; }

}  // namespace myvector_store
