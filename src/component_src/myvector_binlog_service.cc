#include "myvector_binlog_service.h"
#include <mysql/mysql.h>
#include <mysql/plugin.h>
#if defined(__linux__) || defined(__APPLE__)
#include <arpa/inet.h>
#endif
#if defined(_WIN32)
#include <io.h>
#include <windows.h>
#endif
#if defined(__linux__) || defined(__APPLE__)
#include <strings.h>
#endif
#if !defined(_WIN32)
#include <unistd.h>
#endif
#include <mysql/service_my_plugin_log.h>
#include <mysql/service_mysql_alloc.h>
#include <mysql/service_plugin_registry.h>
#include <atomic>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <fcntl.h>
#include <fstream>
#include <list>
#include <map>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include "my_inttypes.h"
#include "my_thread.h"
#include "myvector.h"
#include "myvectorutils.h"
#if MYSQL_VERSION_ID >= 80400
#include "mysql/binlog/event/binlog_event.h"
namespace binary_log = mysql::binlog::event;
#else
#include "binlog_event.h"
#endif

#if defined(__GNUC__) || defined(__clang__)
#define MYVECTOR_DIAGNOSTIC_PUSH _Pragma("GCC diagnostic push")
#define MYVECTOR_DIAGNOSTIC_POP _Pragma("GCC diagnostic pop")
#define MYVECTOR_IGNORE_DEPRECATED_DECLARATIONS                                \
    _Pragma("GCC diagnostic ignored \"-Wdeprecated-declarations\"")
#else
#define MYVECTOR_DIAGNOSTIC_PUSH
#define MYVECTOR_DIAGNOSTIC_POP
#define MYVECTOR_IGNORE_DEPRECATED_DECLARATIONS
#endif

// Configuration and paths: defined in myvector_plugin.cc (plugin build) or myvector_component_config.cc (component build).
extern char* myvector_index_dir;
extern char* myvector_config_file;
extern long myvector_feature_level;
extern long myvector_index_bg_threads;

void myvector_table_op(const std::string& dbname,
                       const std::string& tbname,
                       const std::string& cname,
                       unsigned int pkid,
                       std::vector<unsigned char>& vec,
                       const std::string& binlogfile,
                       const size_t& pos);
std::string myvector_find_earliest_binlog_file();

typedef struct {
    std::string dbName_;
    std::string tableName_;
    std::string columnName_;
    std::vector<unsigned char> vec_;
    unsigned int veclen_;  // bytes
    unsigned int pkid_;
    std::string binlogFile_;
    size_t binlogPos_;
} VectorIndexUpdateItem;

typedef struct {
    std::string vectorColumn;
    int idColumnPosition;
    int vecColumnPosition;
} VectorIndexColumnInfo;

/* Table ID in binlog is 6 bytes little-endian; use uint64_t for safe storage. */
static uint64_t read_table_id_6(const unsigned char* buf) {
    uint64_t id = 0;
    memcpy(&id, buf, 6);
    return id;
}

class EventsQ {
public:
    void enqueue(VectorIndexUpdateItem* item) {
        std::lock_guard lk(m_);
        items_.push_back(item);
        cv_.notify_one();
    }
    VectorIndexUpdateItem* dequeue() {
        std::unique_lock lk(m_);
        cv_.wait(lk, [this] { return !items_.empty() || shutting_down_; });
        if (shutting_down_ && items_.empty())
            return nullptr;  // shutdown signal for consumers
        VectorIndexUpdateItem* next = items_.front();
        items_.pop_front();
        if (items_.empty())
            cv_.notify_all();  // wake wait_until_empty()
        return next;  // consumer to call delete
    }
    /** Non-blocking dequeue: returns an item if available, nullptr if empty. */
    VectorIndexUpdateItem* try_dequeue() {
        std::lock_guard lk(m_);
        if (items_.empty())
            return nullptr;
        VectorIndexUpdateItem* next = items_.front();
        items_.pop_front();
        if (items_.empty())
            cv_.notify_all();  // wake wait_until_empty()
        return next;
    }
    /** Block until the queue is empty (e.g. all items consumed by workers). */
    void wait_until_empty() {
        std::unique_lock lk(m_);
        cv_.wait(lk, [this] { return items_.empty(); });
    }
    void request_shutdown() {
        std::lock_guard lk(m_);
        shutting_down_ = true;
        cv_.notify_all();
    }
    /** Reset shutdown flag for restart (e.g. component reinstall). */
    void clear_shutdown() {
        std::lock_guard lk(m_);
        shutting_down_ = false;
    }
    bool empty() {
        std::lock_guard lk(m_);
        return items_.empty();
    }

private:
    std::mutex m_;
    std::condition_variable cv_;
    std::list<VectorIndexUpdateItem*> items_;
    bool shutting_down_ = false;
};

static EventsQ gqueue_;

std::mutex binlog_stream_mutex_;
std::map<std::string, VectorIndexColumnInfo> g_OnlineVectorIndexes;
std::string currentBinlogFile = "";
size_t currentBinlogPos = 0;

/* Connection config: stored in restricted scope, used only at connection time.
 * Consider loading from a secrets manager or environment variables with minimal
 * lifetime for production deployments. */
namespace {
struct ConnConfig {
    std::string user_id;
    std::string password;
    std::string socket;
    std::string host;
    std::string port;
};
static ConnConfig g_conn_config;
/** Configurable binlog server_id for mysql_binlog_open; avoids collision with MySQL server/replica IDs. */
static uint32_t g_binlog_server_id = 0;

/** Compute a deterministic binlog server_id from hostname + PID when not configured. */
static uint32_t compute_binlog_server_id_fallback() {
    uint32_t seed;
#if defined(_WIN32)
    seed = static_cast<uint32_t>(GetCurrentProcessId()) * 1103515245u + 12345u;
#else
    char hostname[256] = {0};
    if (gethostname(hostname, sizeof(hostname) - 1) != 0)
        hostname[0] = '\0';
    std::string seed_str = std::string(hostname) + std::to_string(static_cast<long>(getpid()));
    seed = static_cast<uint32_t>(std::hash<std::string>{}(seed_str) & 0x7FFFFFFFu);
#endif
    return (seed == 0) ? 1u : seed;
}

/** Zero a string's contents to avoid leaving credentials in memory.
 * Uses platform-safe APIs so the compiler cannot optimize away the clear. */
static void secure_zero_string(std::string& s) {
    if (s.empty())
        return;
#if defined(_WIN32)
    SecureZeroMemory(&s[0], s.size());
#elif defined(__linux__) || defined(__APPLE__)
    explicit_bzero(&s[0], s.size());
#else
    volatile char* p = const_cast<char*>(s.data());
    for (size_t i = 0; i < s.size(); i++)
        p[i] = '\0';
#endif
}

struct BinlogState {
    std::string server_uuid;
    std::string binlog_file;
    size_t binlog_pos = 0;
};

const char* kBinlogStateFileName = "binlog_state.json";

std::string binlog_state_path() {
    if (!myvector_index_dir || !strlen(myvector_index_dir))
        return std::string(kBinlogStateFileName);
    std::string base(myvector_index_dir);
    if (base.back() != '/')
        base.push_back('/');
    return base + kBinlogStateFileName;
}

bool extract_json_string(const std::string& json,
                         const char* key,
                         std::string* value) {
    if (!value)
        return false;
    std::string needle = std::string("\"") + key + "\"";
    size_t key_pos = json.find(needle);
    if (key_pos == std::string::npos)
        return false;
    size_t colon = json.find(':', key_pos + needle.size());
    if (colon == std::string::npos)
        return false;
    size_t first_quote = json.find('"', colon + 1);
    if (first_quote == std::string::npos)
        return false;
    std::string out;
    out.reserve(64);
    bool found_close = false;
    for (size_t i = first_quote + 1; i < json.size(); i++) {
        char c = json[i];
        if (c == '"') {
            found_close = true;
            break;
        }
        if (c == '\\' && i + 1 < json.size()) {
            switch (json[i + 1]) {
                case '"': out += '"'; break;
                case '\\': out += '\\'; break;
                case '/': out += '/'; break;
                case 'b': out += '\b'; break;
                case 'f': out += '\f'; break;
                case 'n': out += '\n'; break;
                case 'r': out += '\r'; break;
                case 't': out += '\t'; break;
                default: out += json[i + 1]; break;
            }
            i++;
            continue;
        }
        out += c;
    }
    if (!found_close)
        return false;
    *value = out;
    return true;
}

/* Escape MySQL identifier for use inside backticks: wrap in backticks and double internal backticks. */
static size_t escape_identifier(char* out, size_t out_size, const char* id) {
    if (!out || out_size == 0 || !id)
        return 0;
    if (out_size < 3)
        return 0;  /* need space for `, `, and NUL */
    size_t j = 0;
    out[j++] = '`';
    for (; *id && j < out_size - 2; id++) {
        if (*id == '`') {
            if (j + 2 > out_size - 2)  /* need room for `` + closing ` + NUL */
                break;
            out[j++] = '`';
            out[j++] = '`';
        } else {
            out[j++] = *id;
        }
    }
    out[j++] = '`';
    out[j] = '\0';
    return j;
}

bool extract_json_number(const std::string& json,
                         const char* key,
                         size_t* value) {
    if (!value)
        return false;
    std::string needle = std::string("\"") + key + "\"";
    size_t key_pos = json.find(needle);
    if (key_pos == std::string::npos)
        return false;
    size_t colon = json.find(':', key_pos + needle.size());
    if (colon == std::string::npos)
        return false;
    size_t start = json.find_first_of("-0123456789", colon + 1);
    if (start == std::string::npos)
        return false;
    bool has_minus = (json[start] == '-');
    size_t digit_start = start + (has_minus ? 1 : 0);
    if (digit_start >= json.size())
        return false;
    size_t end = json.find_first_not_of("0123456789", digit_start);
    std::string number = json.substr(start, end - start);
    if (number.empty() || (has_minus && number.size() == 1))
        return false;
    try {
        long long parsed = std::stoll(number);
        if (parsed < 0)
            return false;
        *value = static_cast<size_t>(parsed);
        return true;
    } catch (const std::exception&) {
        return false;
    }
}

std::string EscapeJsonString(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 8);
    for (unsigned char c : s) {
        switch (c) {
            case '"':
                out += "\\\"";
                break;
            case '\\':
                out += "\\\\";
                break;
            case '\b':
                out += "\\b";
                break;
            case '\f':
                out += "\\f";
                break;
            case '\n':
                out += "\\n";
                break;
            case '\r':
                out += "\\r";
                break;
            case '\t':
                out += "\\t";
                break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += c;
                }
                break;
        }
    }
    return out;
}

bool load_binlog_state(BinlogState* state) {
    if (!state)
        return false;
    std::ifstream file(binlog_state_path());
    if (!file.is_open())
        return false;
    std::stringstream buffer;
    buffer << file.rdbuf();
    std::string json = buffer.str();
    if (json.empty())
        return false;
    if (!extract_json_string(json, "server_uuid", &state->server_uuid))
        return false;
    if (!extract_json_string(json, "binlog_file", &state->binlog_file))
        return false;
    if (!extract_json_number(json, "binlog_pos", &state->binlog_pos))
        return false;
    return true;
}

bool persist_binlog_state(const BinlogState& state) {
    std::string path = binlog_state_path();
    std::string tmp_path = path + ".tmp";
    std::ofstream out(tmp_path, std::ios::trunc);
    if (!out.is_open())
        return false;
    out << "{"
        << "\"server_uuid\":\"" << EscapeJsonString(state.server_uuid) << "\","
        << "\"binlog_file\":\"" << EscapeJsonString(state.binlog_file) << "\","
        << "\"binlog_pos\":" << state.binlog_pos << "}";
    out.flush();
    if (!out || out.fail()) {
        out.close();
        std::remove(tmp_path.c_str());
        return false;
    }
    out.close();
    if (out.fail()) {
        std::remove(tmp_path.c_str());
        return false;
    }
#if defined(_WIN32)
    {
        int fd = _open(tmp_path.c_str(), _O_RDONLY);
        if (fd >= 0) {
            _commit(fd);
            _close(fd);
        }
    }
#else
    {
        int fd = open(tmp_path.c_str(), O_RDONLY);
        if (fd >= 0) {
            fsync(fd);
            close(fd);
        }
    }
#endif
    if (std::rename(tmp_path.c_str(), path.c_str()) != 0)
        return false;
    return true;
}

bool fetch_server_uuid(MYSQL* mysql, std::string* server_uuid) {
    if (!mysql || !server_uuid)
        return false;
    const char* query = "SELECT @@server_uuid";
    if (mysql_real_query(mysql, query, strlen(query)))
        return false;
    MYSQL_RES* result = mysql_store_result(mysql);
    if (!result)
        return false;
    MYSQL_ROW row = mysql_fetch_row(result);
    bool ok = row && row[0];
    if (ok)
        *server_uuid = row[0];
    mysql_free_result(result);
    return ok;
}

} // namespace

// ... (rest of the helper functions like parseTableMapEvent, parseRowsEvent, parseRotateEvent, readConfigFile, GetBaseTableColumnPositions, OpenAllOnlineVectorIndexes, BuildMyVectorIndexSQL, myvector_checkpoint_index, FlushOnlineVectorIndexes)

#define EVENT_HEADER_LENGTH 19

typedef struct {
    uint64_t tableId;
    std::string dbName;
    std::string tableName;
    unsigned int nColumns;
    std::vector<unsigned char> columnTypes;
    std::vector<int> columnMetadata;
} TableMapEvent;

/* parseTableMapEvent - Parse the TableMap binlog event that appears before
 * any *ROWS* event.
 */
void parseTableMapEvent(const unsigned char* event_buf,
                        unsigned int event_len,
                        TableMapEvent& tev) {
    tev = TableMapEvent();

    unsigned int index = EVENT_HEADER_LENGTH;

    if (index + 6 > event_len)
        return;
    tev.tableId = read_table_id_6(&event_buf[index]);
    index += 6;

    index += 2;  // flags

    if (index + 1 > event_len)
        return;
    int dbNameLen = (int)event_buf[index];  // single byte
    index++;
    if (index + dbNameLen + 1 > event_len)
        return;
    tev.dbName = std::string((const char*)&event_buf[index], dbNameLen);
    index += (dbNameLen + 1);               // null

    if (index + 1 > event_len)
        return;
    int tbNameLen = (int)event_buf[index];  // single byte
    index++;
    if (index + tbNameLen + 1 > event_len)
        return;
    tev.tableName = std::string((const char*)&event_buf[index], tbNameLen);
    index += (tbNameLen + 1);

    std::string key = tev.dbName + "." + tev.tableName;
    if (g_OnlineVectorIndexes.find(key) == g_OnlineVectorIndexes.end())
        return;  /// we don't need to parse rest of the metadata

    if (index + 1 > event_len)
        return;
    tev.nColumns = (unsigned int)
        event_buf[index];  // TODO - we support only <= 255 columns
    index++;

    if (index + tev.nColumns > event_len)
        return;
    tev.columnTypes.insert(tev.columnTypes.end(),
                           &event_buf[index],
                           &event_buf[index + tev.nColumns]);
    index += tev.nColumns;
    if (index + 1 > event_len)
        return;
    unsigned int metadatalen = (unsigned int)event_buf[index];
    (void)metadatalen;
    index++;

    for (unsigned int i = 0; i < (unsigned int)tev.nColumns; i++) {
        unsigned int md = 0;
        switch (tev.columnTypes[i]) {
            case MYSQL_TYPE_FLOAT:
            case MYSQL_TYPE_DOUBLE:
            case MYSQL_TYPE_BLOB:
            case MYSQL_TYPE_JSON:
            case MYSQL_TYPE_GEOMETRY:
#if MYSQL_VERSION_ID >= 90000
            case MYSQL_TYPE_VECTOR:
#endif
                if (index + 1 > event_len)
                    return;
                md = (unsigned int)event_buf[index];
                index++;
                break;
            case MYSQL_TYPE_BIT:
            case MYSQL_TYPE_VARCHAR:
            case MYSQL_TYPE_NEWDECIMAL:
                if (index + 2 > event_len)
                    return;
                memcpy(&md, &event_buf[index], 2);
                index += 2;
                break;
            case MYSQL_TYPE_SET:
            case MYSQL_TYPE_ENUM:
            case MYSQL_TYPE_STRING:
                if (index + 2 > event_len)
                    return;
                memcpy(&md, &event_buf[index], 2);
                index += 2;
                break;
            case MYSQL_TYPE_TIME2:
            case MYSQL_TYPE_DATETIME2:
            case MYSQL_TYPE_TIMESTAMP2:
                if (index + 1 > event_len)
                    return;
                md = (unsigned int)event_buf[index];
                index++;
                break;
            default:
                // error_print("unrecognized column type %d",
                // (int)tev.columnTypes[i]);
                // TODO: Replace with component-specific logging
                break;
        }  /// switch
        tev.columnMetadata.push_back(md);
    }  /// for

    return;
}

void parseRowsEvent(const unsigned char* event_buf,
                    unsigned int event_len,
                    TableMapEvent& tev,
                    unsigned int pos1,
                    unsigned int pos2,
                    const std::string& binlog_file,
                    size_t binlog_pos,
                    std::vector<VectorIndexUpdateItem*>& updates) {
    updates.clear();

    if (event_len < 4)
        return;  // need at least 4-byte checksum
    event_len -= 4;  // checksum at the end

    const unsigned int min_payload = EVENT_HEADER_LENGTH + 6 + 2;
    if (event_len < min_payload)
        return;

    unsigned int index = EVENT_HEADER_LENGTH;

    (void)read_table_id_6(&event_buf[index]);  // tableId for row/table map matching
    index += 6;
    index += 2;

    unsigned int extrainfo = 0;
    memcpy(&extrainfo, &event_buf[index], 2);
    index += extrainfo;

    unsigned int ncols = (unsigned int)event_buf[index];
    index++;
    unsigned int inclen = (((unsigned int)(ncols) + 7) >> 3);
    (void)inclen;
    // TODO : Assuming included & null bitmaps are single byte
    unsigned int incbitmap = (unsigned int)event_buf[index];
    (void)incbitmap;
    index++;
    while (true) {
        if (index + 1 > event_len)
            break;
        unsigned int nullbitmap = (unsigned int)event_buf[index];
        (void)nullbitmap;
        index++;

        unsigned int lval = 0;
        unsigned long llval = 0;

        unsigned int idVal = 0, vecsz = 0;
        const unsigned char* vec = nullptr;
        bool row_overflow = false;
        for (unsigned int i = 0; i < ncols && !row_overflow; i++) {
            size_t remaining = event_len - index;
            switch (tev.columnTypes[i]) {
                case MYSQL_TYPE_LONG:
                    if (remaining < 4) {
                        row_overflow = true;
                        break;
                    }
                    memcpy(&lval, &event_buf[index], 4);
                    index += 4;
                    if (i == pos1)
                        idVal = lval;
                    break;
                case MYSQL_TYPE_LONGLONG:
                    if (remaining < 8) {
                        row_overflow = true;
                        break;
                    }
                    memcpy(&llval, &event_buf[index], 8);
                    index += 8;
                    break;
                case MYSQL_TYPE_VARCHAR: {
                    unsigned int clen = 0;
                    if (tev.columnMetadata[i] < 256) {
                        if (remaining < 1) {
                            row_overflow = true;
                            break;
                        }
                        clen = (unsigned int)event_buf[index];
                        index++;
                        remaining -= 1;
                    } else {
                        if (remaining < 2) {
                            row_overflow = true;
                            break;
                        }
                        memcpy(&clen, &event_buf[index], 2);
                        index += 2;
                        remaining -= 2;
                    }
                    if (remaining < clen) {
                        row_overflow = true;
                        break;
                    }
                    if (i == pos2) {  // found vector column
                        vec = &event_buf[index];
                        vecsz = clen;
                    }
                    index += clen;
                    break;
                }
#if MYSQL_VERSION_ID >= 90000
                case MYSQL_TYPE_VECTOR: {
                    unsigned int clen = 0;
                    unsigned int md_len = (i < tev.columnMetadata.size()) ? tev.columnMetadata[i] : 2;
                    if (md_len > 2)
                        md_len = 2;
                    if (remaining < (size_t)md_len) {
                        row_overflow = true;
                        break;
                    }
                    memcpy(&clen, &event_buf[index], md_len);
                    index += md_len;
                    if (remaining < (size_t)md_len + clen) {
                        row_overflow = true;
                        break;
                    }
                    if (i == pos2) {  // found vector column
                        vec = &event_buf[index];
                        vecsz = clen;
                    }
                    index += clen;
                    break;
                }
#endif
                case MYSQL_TYPE_TIMESTAMP2:
                    if (remaining < 4) {
                        row_overflow = true;
                        break;
                    }
                    index += 4;
                    break;
                default:
                    // error_print("unrecognized column type %d",
                    //             (int)tev.columnTypes[i]);
                    // TODO: Replace with component-specific logging
                    break;
            }  // switch
        }  // for columns

        if (row_overflow) {
            break;  /* abort row parse on buffer overflow */
        }
        if (!vec || vecsz == 0) {
            continue;  /* skip row: no vector data */
        }
        VectorIndexUpdateItem* item = new VectorIndexUpdateItem();
        std::string key = tev.dbName + "." + tev.tableName;
        std::string columnName = g_OnlineVectorIndexes[key].vectorColumn;
        item->dbName_ = tev.dbName;
        item->tableName_ = tev.tableName;
        item->columnName_ = columnName;
        item->vec_.assign(vec, vec + vecsz);
        item->pkid_ = idVal;
        item->binlogFile_ = binlog_file;
        item->binlogPos_ = binlog_pos;
        updates.push_back(item);
        // index += 4;
        if (index >= event_len)
            break;  // done - multi rows

    }  // while (true) - single row or multi-row event!
    return;
}

/* parseRotateEvent() : binlog ROTATE event indicates end of current binlog
 * file and start of new binlog file. The offs parameter is to handle quirks
 * in the first ROTATE event.
 */
void parseRotateEvent(const unsigned char* event_buf,
                      unsigned int event_len,
                      std::string& binlogfile,
                      size_t& binlogpos,
                      bool offs) {
    int index = EVENT_HEADER_LENGTH;
    size_t position = 0;

    memcpy(&position, &event_buf[index], sizeof(position));
    binlogpos = position;

    index += sizeof(position);

    std::string filename = std::string((const char*)&event_buf[index],
                                       (event_len - index) - (4 * offs));
    binlogfile = filename;
}

void readConfigFile(const char* config_file) {
    if (!config_file || !strlen(config_file))
        return;

    std::ifstream file(config_file);
    std::string line, info;

    while (std::getline(file, line)) {
        if (line.length() && line[0] == '#')
            continue;

        if (info.length())
            info += ",";
        info += line;
    }

    MyVectorOptions vo(info);

    g_conn_config.user_id = vo.getOption("myvector_user_id");
    g_conn_config.password = vo.getOption("myvector_user_password");
    g_conn_config.socket = vo.getOption("myvector_socket");
    g_conn_config.host = vo.getOption("myvector_host");
    g_conn_config.port = vo.getOption("myvector_port");

    bool valid = false;
    int configured = vo.getIntOption("myvector_binlog_server_id", 0, &valid);
    g_binlog_server_id = (valid && configured > 0) ? static_cast<uint32_t>(configured)
                                                      : compute_binlog_server_id_fallback();
}

/* GetBaseTableColumnPositions() - Get the column ordinal positions of the
 * "id" column and the "vector" column. e.g :-
 * CREATE TABLE books
 * (
 *   bookid   int   primary key,
 *   title    varchar(512),
 *   author   varchar(200),
 *   bvector  MYVECTOR(dim=1024,....)
 * );
 * This function will return "1" and "4" idcolpos & veccolpos resp.
 * The ordinal positions are needed to parse the binlog row events. The
 * positions are got from the information_schema.columns dictionary table.
 */
void GetBaseTableColumnPositions(MYSQL* hnd,
                                 const char* db,
                                 const char* table,
                                 const char* idcol,
                                 const char* veccol,
                                 int& idcolpos,
                                 int& veccolpos) {
    static const char* q =
        "select column_name, ordinal_position from "
        "information_schema.columns where table_schema='%s' "
        "and table_name='%s' and "
        "(column_name='%s' or column_name='%s');";
    char buff[MYVECTOR_BUFF_SIZE];
    /* Escaped identifiers: max expansion 2*len+1; 256 suffices for 64-char identifiers. */
    char esc_db[256], esc_table[256], esc_idcol[256], esc_veccol[256];
    size_t len_db = db ? strlen(db) : 0;
    size_t len_table = table ? strlen(table) : 0;
    size_t len_idcol = idcol ? strlen(idcol) : 0;
    size_t len_veccol = veccol ? strlen(veccol) : 0;

    idcolpos = veccolpos = 0;

    if (!hnd || !db || !table || !idcol || !veccol)
        return;
    if (len_db >= sizeof(esc_db) / 2 || len_table >= sizeof(esc_table) / 2 ||
        len_idcol >= sizeof(esc_idcol) / 2 || len_veccol >= sizeof(esc_veccol) / 2)
        return;

    unsigned long elen_db = mysql_real_escape_string(hnd, esc_db, db, (unsigned long)len_db);
    unsigned long elen_table = mysql_real_escape_string(hnd, esc_table, table, (unsigned long)len_table);
    unsigned long elen_idcol = mysql_real_escape_string(hnd, esc_idcol, idcol, (unsigned long)len_idcol);
    unsigned long elen_veccol = mysql_real_escape_string(hnd, esc_veccol, veccol, (unsigned long)len_veccol);
    esc_db[elen_db] = '\0';
    esc_table[elen_table] = '\0';
    esc_idcol[elen_idcol] = '\0';
    esc_veccol[elen_veccol] = '\0';

    int n = snprintf(buff, sizeof(buff), q, esc_db, esc_table, esc_idcol, esc_veccol);
    if (n < 0 || (size_t)n >= sizeof(buff))
        return;

    if (mysql_real_query(hnd, buff, strlen(buff))) {
        fprintf(stderr, "GetBaseTableColumnPositions query failed: %s\n", mysql_error(hnd));
        return;
    }

    MYSQL_RES* result = mysql_store_result(hnd);
    if (!result) {
        fprintf(stderr, "GetBaseTableColumnPositions store_result failed: %s\n", mysql_error(hnd));
        return;
    }

    if (mysql_num_fields(result) != 2) {
        fprintf(stderr, "GetBaseTableColumnPositions expected 2 columns, got %u\n", mysql_num_fields(result));
        mysql_free_result(result);
        return;
    }

    MYSQL_ROW row;
    while ((row = mysql_fetch_row(result))) {
        unsigned long* lengths = mysql_fetch_lengths(result);
        if (!lengths || !row || !row[0] || !row[1]) {
            continue;  /* skip malformed row */
        }
        if (lengths[0] == 0 || lengths[1] == 0) {
            continue;
        }
        /* Safe to compare: we have non-null row[0], row[1] and lengths. */
        const char* colname = row[0];
        const char* position = row[1];
        if (strlen(colname) != lengths[0] || strlen(position) != lengths[1]) {
            continue;  /* skip if not null-terminated / inconsistent */
        }
        if (strcmp(colname, idcol) == 0)
            idcolpos = atoi(position);
        else if (strcmp(colname, veccol) == 0)
            veccolpos = atoi(position);
    }  // while

    mysql_free_result(result);

    // debug_print(
    //     "GetBaseColumn %s %s = %d %d", idcol, veccol, idcolpos, veccolpos);
    // TODO: Replace with component-specific logging

    return;
}

void myvector_open_index_impl(char* vecid,
                              char* details,
                              char* pkidcol,
                              char* action,
                              char* extra,
                              char* result);

/* Schema where myvector_columns view is installed (see sql/myvectorplugin.sql).
 * Default is "mysql"; override if the view is created in another database.
 */
static const char* kMyVectorColumnsSchema = "mysql";

/* OpenAllOnlineVectorIndexes() - Query MYVECTOR_COLUMNS view and open/load
 * all vector indexes that have online=Y i.e updated online when DMLs are
 * done on the base table. This routine is called during plugin init.
 */
void OpenAllOnlineVectorIndexes(MYSQL* hnd) {
    char query_buf[256];
    snprintf(query_buf, sizeof(query_buf),
             "select db,tbl,col,info from %s.myvector_columns",
             kMyVectorColumnsSchema);

    if (mysql_real_query(hnd, query_buf, strlen(query_buf))) {
        // TODO
        return;
    }

    MYSQL_RES* result = mysql_store_result(hnd);
    if (!result) {
        // TODO
        return;
    }

    MYSQL_ROW row;
    while ((row = mysql_fetch_row(result))) {
        unsigned long* lengths;
        lengths = mysql_fetch_lengths(result);

        char* dbname = row[0];
        char* tbl = row[1];
        char* col = row[2];
        char* info = row[3];

        if (!lengths[0] || !lengths[1] || !lengths[2] || !lengths[3] ||
            !dbname || !tbl || !col || !info) {
            // TODO
            continue;
        }

        // info_print("Got index %s %s %s [%s]", dbname, tbl, col, info);
        // TODO: Replace with component-specific logging

        MyVectorOptions vo(info);

        if (!vo.isValid()) {
            // TODO
            continue;
        }

        std::string online = vo.getOption("online");
        std::string idcol = vo.getOption("idcol");

        int idcolpos = 0, veccolpos = 0;
        GetBaseTableColumnPositions(
            hnd, dbname, tbl, idcol.c_str(), col, idcolpos, veccolpos);
        if (idcolpos == 0 || veccolpos == 0)
            continue;

        if (online == "y" || online == "Y") {
            char empty[1024] = {0};
            char action[] = "load";
            char vecid[1024];
            snprintf(vecid, sizeof(vecid), "%s.%s.%s", dbname, tbl, col);
            myvector_open_index_impl(vecid, info, empty, action, empty, empty);

            snprintf(vecid, sizeof(vecid), "%s.%s", dbname, tbl);
            VectorIndexColumnInfo vc{col, idcolpos, veccolpos};
            g_OnlineVectorIndexes[vecid] = vc;
        }
    }  // while

    mysql_free_result(result);
}

/* BuildMyVectorIndexSQL - Build/Refresh the Vector Index! This function uses
 * SQL to fetch rows from the base table and put the ID & vector into the
 * vector index.
 */
void BuildMyVectorIndexSQL(const char* db,
                           const char* table,
                           const char* idcol,
                           const char* veccol,
                           const char* action,
                           const char* trackingColumn,
                           AbstractVectorIndex* vi,
                           char* errorbuf) {
    strcpy(errorbuf, "SUCCESS");
    readConfigFile(myvector_config_file);
    size_t nRows = 0;
    MYSQL mysql;
    MYSQL* conn = mysql_init(&mysql);
    if (conn == nullptr) {
        snprintf(errorbuf,
                 MYVECTOR_BUFF_SIZE,
                 "BuildMyVectorIndexSQL: mysql_init failed (out of memory).");
        fprintf(stderr, "BuildMyVectorIndexSQL: mysql_init failed (out of memory).\n");
        return;
    }
    MYSQL_RES* result = nullptr;
    char query[MYVECTOR_BUFF_SIZE];
    char esc_db[256], esc_table[256], esc_idcol[256], esc_veccol[256], esc_tracking[256];
    int n = 0;
    unsigned long current_ts = 0;
    std::string key;
    bool supportsIncr = false;
    VectorIndexColumnInfo vc{"", 0, 0};
    std::string savedBinlogFile;
    size_t savedBinlogPos = 0;

    fprintf(stderr,
            "BuildMyVectorIndexSQL %s %s %s %s %s %s.\n",
            db,
            table,
            idcol,
            veccol,
            action,
            trackingColumn);

    {
        std::string conn_host = g_conn_config.host;
        std::string conn_user = g_conn_config.user_id;
        std::string conn_password = g_conn_config.password;
        std::string conn_socket = g_conn_config.socket;
        std::string conn_port = g_conn_config.port;
        if (!mysql_real_connect(
                &mysql,
                conn_host.c_str(),
                conn_user.c_str(),
                conn_password.c_str(),
                NULL,
                (conn_port.length() ? atoi(conn_port.c_str()) : 0),
                conn_socket.c_str(),
                CLIENT_IGNORE_SIGPIPE)) {
            snprintf(errorbuf,
                     MYVECTOR_BUFF_SIZE,
                     "Error in new connection to build vector index : %s.",
                     mysql_error(&mysql));
            secure_zero_string(conn_password);
            goto cleanup;
        }
        secure_zero_string(conn_password);
    }

    (void)mysql_autocommit(&mysql, false);

    if (!db || !table || !idcol || !veccol) {
        snprintf(errorbuf, MYVECTOR_BUFF_SIZE, "BuildMyVectorIndexSQL: null identifier");
        goto cleanup;
    }
    escape_identifier(esc_db, sizeof(esc_db), db);
    escape_identifier(esc_table, sizeof(esc_table), table);
    escape_identifier(esc_idcol, sizeof(esc_idcol), idcol);
    escape_identifier(esc_veccol, sizeof(esc_veccol), veccol);

    snprintf(
        query, sizeof(query), "SET TRANSACTION ISOLATION LEVEL READ COMMITTED");
    if (mysql_real_query(&mysql, query, strlen(query))) {
        snprintf(errorbuf, MYVECTOR_BUFF_SIZE, "SET isolation failed: %s", mysql_error(&mysql));
        goto cleanup;
    }

    n = snprintf(query, sizeof(query), "LOCK TABLES %s.%s READ", esc_db, esc_table);
    if (n < 0 || (size_t)n >= sizeof(query)) {
        snprintf(errorbuf, MYVECTOR_BUFF_SIZE, "LOCK TABLES query too long");
        goto cleanup;
    }
    if (mysql_real_query(&mysql, query, strlen(query))) {
        snprintf(errorbuf, MYVECTOR_BUFF_SIZE, "LOCK TABLES failed: %s", mysql_error(&mysql));
        goto cleanup;
    }

    n = snprintf(query, sizeof(query), "SELECT %s, %s FROM %s.%s",
                 esc_idcol, esc_veccol, esc_db, esc_table);
    if (n < 0 || (size_t)n >= sizeof(query)) {
        snprintf(errorbuf, MYVECTOR_BUFF_SIZE, "SELECT query too long");
        goto cleanup;
    }

    current_ts = time(NULL);

    if (!strcmp(action, "refresh") || (trackingColumn && trackingColumn[0] != '\0')) {
        if (!trackingColumn) {
            snprintf(errorbuf, MYVECTOR_BUFF_SIZE, "BuildMyVectorIndexSQL: null trackingColumn");
            goto cleanup;
        }
        escape_identifier(esc_tracking, sizeof(esc_tracking), trackingColumn);
        unsigned long previous_ts = vi->getUpdateTs();
        char whereClause[1024];
        n = snprintf(whereClause, sizeof(whereClause),
                     " WHERE unix_timestamp(%s) > %lu AND unix_timestamp(%s) <= %lu",
                     esc_tracking, previous_ts, esc_tracking, current_ts);
        if (n < 0 || (size_t)n >= sizeof(whereClause)) {
            snprintf(errorbuf, MYVECTOR_BUFF_SIZE, "WHERE clause too long");
            goto cleanup;
        }
        size_t qlen = strlen(query);
        size_t rem = sizeof(query) - qlen - 1;
        if (strlen(whereClause) >= rem) {
            snprintf(errorbuf, MYVECTOR_BUFF_SIZE, "Query buffer overflow");
            goto cleanup;
        }
        strncat(query, whereClause, rem - 1);
        query[sizeof(query) - 1] = '\0';
    }

    vi->setUpdateTs(current_ts);

    if (mysql_real_query(&mysql, query, strlen(query))) {
        snprintf(errorbuf, MYVECTOR_BUFF_SIZE, "Build query failed: %s", mysql_error(&mysql));
        goto cleanup;
    }

    result = mysql_store_result(&mysql);
    if (!result) {
        snprintf(errorbuf, MYVECTOR_BUFF_SIZE, "store_result failed: %s", mysql_error(&mysql));
        goto cleanup;
    }

    MYSQL_ROW row;
    while ((row = mysql_fetch_row(result))) {
        unsigned long* lengths = mysql_fetch_lengths(result);
        if (!lengths || !row)
            continue;
        char* idval = row[0];
        char* vec = row[1];
        if (!lengths[0] || !lengths[1] || !idval || !vec)
            continue;
        vi->insertVector(vec, 0, atol(idval));
        nRows++;
    }

    mysql_free_result(result);
    result = nullptr;

    key = std::string(db) + "." + std::string(table);
    {
        std::lock_guard<std::mutex> binlogMutex(binlog_stream_mutex_);
        vi->setLastUpdateCoordinates(currentBinlogFile, currentBinlogPos);
        savedBinlogFile = currentBinlogFile;
        savedBinlogPos = currentBinlogPos;
        supportsIncr = vi->supportsIncrUpdates();
        if (supportsIncr) {
            int idcolpos = 0, veccolpos = 0;
            GetBaseTableColumnPositions(
                &mysql, db, table, idcol, veccol, idcolpos, veccolpos);
            vc = VectorIndexColumnInfo{veccol, idcolpos, veccolpos};
        }
    }
    vi->saveIndex(myvector_index_dir, "build");
    {
        std::lock_guard<std::mutex> binlogMutex(binlog_stream_mutex_);
        snprintf(errorbuf,
                 MYVECTOR_BUFF_SIZE,
                 "SUCCESS: Index created & saved at (%s %lu)"
                 ", rows : %lu.",
                 savedBinlogFile.c_str(),
                 (unsigned long)savedBinlogPos,
                 nRows);
        if (supportsIncr) {
            g_OnlineVectorIndexes[key] = vc;
        }
        snprintf(query, sizeof(query), "UNLOCK TABLES");
        int ret = 0;
        if ((ret = mysql_real_query(&mysql, query, strlen(query)))) {
            snprintf(errorbuf,
                     MYVECTOR_BUFF_SIZE,
                     "Error in unlocking table (%d) %s.",
                     ret,
                     mysql_error(&mysql));
        }
    }

cleanup:
    secure_zero_string(g_conn_config.password);
    if (result)
        mysql_free_result(result);
    mysql_close(&mysql);
}

void myvector_checkpoint_index(const std::string& dbtable,
                               const std::string& veccol,
                               const std::string& binlogFile,
                               size_t binlogPos);

/* FlushOnlineVectorIndexes - flush or checkpoint all vector indexes that
 * registered for online binlog based DML updates. This routine is currently
 * called on every binlog file rotation. Binlog global mutex should be held
 * by caller so that current binlog filename and position are locked.
 */
void FlushOnlineVectorIndexes() {
    std::string binlog_file;
    size_t binlog_pos = 0;
    std::map<std::string, VectorIndexColumnInfo> snapshot;
    {
        std::lock_guard<std::mutex> lock(binlog_stream_mutex_);
        binlog_file = currentBinlogFile;
        binlog_pos = currentBinlogPos;
        snapshot = g_OnlineVectorIndexes;
    }
    gqueue_.wait_until_empty();
    for (auto const& [key, val] : snapshot) {
        myvector_checkpoint_index(key,
                                  val.vectorColumn,
                                  binlog_file,
                                  binlog_pos);
    }
}

namespace myvector_component {

class MyVectorBinlogServiceImpl : public MyVectorBinlogService {
public:
    MyVectorBinlogServiceImpl() : binlog_thread_(nullptr) {
        shutdown_binlog_thread_.store(false);
    }

    ~MyVectorBinlogServiceImpl() override {
        stop_binlog_monitoring();
    }

    int start_binlog_monitoring() override {
        if (binlog_thread_) {
            // Already running
            return 0;
        }
        // Initialize global variables that will be used by the thread function.
        // In a proper component, these would be member variables or passed via context.
        // For now, we reuse the existing global approach to minimize changes during migration.
        // myvector_config_file and other configs should come from component services/configs.
        readConfigFile(myvector_config_file);

        if (myvector_feature_level & 1) {
            // info_print("Binlog event thread is disabled!");
            // TODO: Replace with component-specific logging
            return 0;
        }

        if (!preflight_binlog_state()) {
            // No credentials/config — treat as binlog disabled rather than
            // failing INSTALL COMPONENT. Component init succeeds; binlog
            // monitoring stays off until config is provided and component
            // is reinstalled.
            return 0;
        }

        shutdown_binlog_thread_.store(false);
        gqueue_.clear_shutdown();  // reset for restart (stop sets it, never clears)
        binlog_thread_ = new std::thread(&MyVectorBinlogServiceImpl::binlog_loop_fn, this, myvector_index_bg_threads);
        return 0;
    }

    int stop_binlog_monitoring() override {
        if (!binlog_thread_) {
            return 0;
        }
        shutdown_binlog_thread_.store(true);
        gqueue_.request_shutdown();  // unblock worker threads so they can exit
        binlog_thread_->join();
        delete binlog_thread_;
        binlog_thread_ = nullptr;
        // Join workers here so we never leave them running (safety net if
        // binlog_loop_fn exited early; otherwise they are already joined there).
        for (auto& t : worker_threads_) {
            if (t.joinable())
                t.join();
        }
        worker_threads_.clear();
        {
            std::lock_guard<std::mutex> lock(binlog_stream_mutex_);
            persist_state_snapshot(currentBinlogFile, currentBinlogPos);
        }
        // Drain any remaining items using non-blocking try_dequeue (avoids TOCTOU)
        VectorIndexUpdateItem* p;
        while ((p = gqueue_.try_dequeue()) != nullptr) {
            delete p;
        }
        return 0;
    }

private:
    std::atomic<bool> shutdown_binlog_thread_;
    std::thread* binlog_thread_;
    std::vector<std::thread> worker_threads_;
    MYSQL* binlog_mysql_conn_ = nullptr;
    std::string server_uuid_;
    std::string start_binlog_file_;
    size_t start_binlog_pos_ = 4;

    bool preflight_binlog_state() {
        MYSQL mysql;
        if (!mysql_init(&mysql)) {
            return false;
        }
        MYSQL* mysql_ptr = &mysql;
        std::string conn_host = g_conn_config.host;
        std::string conn_user = g_conn_config.user_id;
        std::string conn_password = g_conn_config.password;
        std::string conn_socket = g_conn_config.socket;
        std::string conn_port = g_conn_config.port;
        if (!mysql_real_connect(
                mysql_ptr,
                conn_host.c_str(),
                conn_user.c_str(),
                conn_password.c_str(),
                NULL,
                (conn_port.length() ? atoi(conn_port.c_str()) : 0),
                conn_socket.c_str(),
                CLIENT_IGNORE_SIGPIPE)) {
            secure_zero_string(conn_password);
            secure_zero_string(g_conn_config.password);
            mysql_close(mysql_ptr);
            return false;
        }
        secure_zero_string(conn_password);
        /* Do not clear g_conn_config.password here; binlog_loop_fn needs it next. */

        if (!fetch_server_uuid(mysql_ptr, &server_uuid_)) {
            mysql_close(mysql_ptr);
            return false;
        }

        BinlogState state;
        bool has_state = load_binlog_state(&state);
        if (has_state && state.server_uuid != server_uuid_) {
            // TODO: Replace with component-specific logging
            mysql_close(mysql_ptr);
            return false;
        }

        if (has_state) {
            start_binlog_file_ = state.binlog_file;
            start_binlog_pos_ = state.binlog_pos;
        } else {
            start_binlog_file_ = myvector_find_earliest_binlog_file();
            start_binlog_pos_ = 4;
        }

        mysql_close(mysql_ptr);
        return true;
    }

    void persist_state_snapshot(const std::string& binlog_file,
                                size_t binlog_pos) {
        if (server_uuid_.empty() || binlog_file.empty() || binlog_pos == 0)
            return;
        BinlogState state;
        state.server_uuid = server_uuid_;
        state.binlog_file = binlog_file;
        state.binlog_pos = binlog_pos;
        (void)persist_binlog_state(state);
    }

    void binlog_loop_fn(int num_q_threads) {
        MYSQL mysql;
        int connect_attempts = 0;

        auto close_binlog_mysql_conn = [&]() {
            if (binlog_mysql_conn_ == &mysql) {
                mysql_close(&mysql);
                binlog_mysql_conn_ = nullptr;
            }
        };

        // Connect to MySQL
        while (!shutdown_binlog_thread_.load()) {
            if (connect_attempts > 0) {
                mysql_close(&mysql);
                binlog_mysql_conn_ = nullptr;
            }
            if (!mysql_init(&mysql)) {
                close_binlog_mysql_conn();
                return;
            }
            binlog_mysql_conn_ = &mysql;
            unsigned int read_timeout_sec = 1;
            mysql_options(&mysql, MYSQL_OPT_READ_TIMEOUT, &read_timeout_sec);

            std::string conn_host = g_conn_config.host;
            std::string conn_user = g_conn_config.user_id;
            std::string conn_password = g_conn_config.password;
            std::string conn_socket = g_conn_config.socket;
            std::string conn_port = g_conn_config.port;
            if (!mysql_real_connect(
                    &mysql,
                    conn_host.c_str(),
                    conn_user.c_str(),
                    conn_password.c_str(),
                    NULL,
                    (conn_port.length() ? atoi(conn_port.c_str()) : 0),
                    conn_socket.c_str(),
                    CLIENT_IGNORE_SIGPIPE)) {
                secure_zero_string(conn_password);
                mysql_close(&mysql);
                binlog_mysql_conn_ = nullptr;
                std::this_thread::sleep_for(std::chrono::seconds(1));
                if (shutdown_binlog_thread_.load()) {
                    secure_zero_string(g_conn_config.password);
                    return;
                }
                connect_attempts++;
                if (connect_attempts > 600) {
                    secure_zero_string(g_conn_config.password);
                    return;
                }
                continue;
            }
            secure_zero_string(conn_password);
            secure_zero_string(g_conn_config.password);
            break;  /// connected
        }

        if (shutdown_binlog_thread_.load()) {
            close_binlog_mysql_conn();
            return;
        }

        std::string connected_uuid;
        if (!fetch_server_uuid(&mysql, &connected_uuid) ||
            (!server_uuid_.empty() && connected_uuid != server_uuid_)) {
            // TODO: Replace with component-specific logging
            close_binlog_mysql_conn();
            return;
        }

        std::string initQuery =
            "SET @master_binlog_checksum = 'NONE', @source_binlog_checksum = "
            "'NONE',@net_read_timeout = 3000, @replica_net_timeout = 3000;";
        if (mysql_real_query(&mysql, initQuery.c_str(), initQuery.length())) {
            close_binlog_mysql_conn();
            return;
        }

        OpenAllOnlineVectorIndexes(&mysql);

        std::string startbinlog = start_binlog_file_.empty()
                                      ? myvector_find_earliest_binlog_file()
                                      : start_binlog_file_;

        MYSQL_RPL rpl;
        memset(&rpl, 0, sizeof(rpl));
        rpl.file_name = NULL;
        if (startbinlog.length())
            rpl.file_name = startbinlog.c_str();
        rpl.start_position = start_binlog_pos_ ? start_binlog_pos_ : 4;
        rpl.server_id = g_binlog_server_id;
        if (mysql_binlog_open(&mysql, &rpl)) {
            close_binlog_mysql_conn();
            return;
        }

        {
            std::lock_guard<std::mutex> lock(binlog_stream_mutex_);
            currentBinlogFile = startbinlog;
            currentBinlogPos = rpl.start_position;
        }

        // Start queue processing threads (do not detach; join on shutdown)
        worker_threads_.clear();
        worker_threads_.reserve(static_cast<size_t>(num_q_threads));
        for (int i = 0; i < num_q_threads; i++) {
            worker_threads_.emplace_back([this]() {
                VectorIndexUpdateItem* item = nullptr;
                while (!this->shutdown_binlog_thread_.load()) {
                    item = gqueue_.dequeue();
                    if (!item) break;  // queue shutdown; exit worker
                    myvector_table_op(item->dbName_,
                                      item->tableName_,
                                      item->columnName_,
                                      item->pkid_,
                                      item->vec_,
                                      item->binlogFile_,
                                      item->binlogPos_);
                    delete item;
                }
            });
        }

        TableMapEvent tev;
        while (!shutdown_binlog_thread_.load()) {
            int fetch_rc = mysql_binlog_fetch(&mysql, &rpl);
            if (fetch_rc != 0) {
                if (shutdown_binlog_thread_.load()) {
                    break;
                }
                std::string err = mysql_error(&mysql);
                if (err.find("timed out") != std::string::npos) {
                    continue;
                }
                // Non-timeout error: signal workers to exit before join
                gqueue_.request_shutdown();
                break;
            }
#if MYSQL_VERSION_ID >= 80400
            MYVECTOR_DIAGNOSTIC_PUSH
            MYVECTOR_IGNORE_DEPRECATED_DECLARATIONS
            using MyvectorLogEventType = binary_log::Log_event_type;
            constexpr MyvectorLogEventType kRotateEvent = binary_log::ROTATE_EVENT;
            constexpr MyvectorLogEventType kTableMapEvent = binary_log::TABLE_MAP_EVENT;
            constexpr MyvectorLogEventType kWriteRowsEvent = binary_log::WRITE_ROWS_EVENT;
            MYVECTOR_DIAGNOSTIC_POP
#else
            using MyvectorLogEventType = binary_log::Log_event_type;
            constexpr MyvectorLogEventType kRotateEvent = binary_log::ROTATE_EVENT;
            constexpr MyvectorLogEventType kTableMapEvent =
                binary_log::TABLE_MAP_EVENT;
            constexpr MyvectorLogEventType kWriteRowsEvent =
                binary_log::WRITE_ROWS_EVENT;
#endif

            if (rpl.size == 0 || !rpl.buffer) {
                continue;
            }
            MyvectorLogEventType type = static_cast<MyvectorLogEventType>(
                rpl.buffer[1 + EVENT_TYPE_OFFSET]);
            unsigned long event_len = rpl.size - 1;
            const unsigned char* event_buf = rpl.buffer + 1;

            if (type == kRotateEvent) {
                if (currentBinlogFile.length()) {
                    FlushOnlineVectorIndexes();
                }
                {
                    std::lock_guard<std::mutex> lock(binlog_stream_mutex_);
                    parseRotateEvent(event_buf,
                                     event_len,
                                     currentBinlogFile,
                                     currentBinlogPos,
                                     (currentBinlogFile.length() > 0));
                    persist_state_snapshot(currentBinlogFile,
                                           currentBinlogPos);
                }
                continue;
            }
            {
                std::lock_guard<std::mutex> lock(binlog_stream_mutex_);
                currentBinlogPos += event_len;
            }
            {
                std::lock_guard<std::mutex> lock(binlog_stream_mutex_);
                if (g_OnlineVectorIndexes.empty())
                    continue;  // optimization!
                if (type == kTableMapEvent) {
                    parseTableMapEvent(event_buf, event_len, tev);
                } else if (type == kWriteRowsEvent) {
                    std::string key = tev.dbName + "." + tev.tableName;
                    if (g_OnlineVectorIndexes.find(key) ==
                        g_OnlineVectorIndexes.end()) {
                        continue;
                    }
                    int idcolpos = g_OnlineVectorIndexes[key].idColumnPosition;
                    int veccolpos = g_OnlineVectorIndexes[key].vecColumnPosition;
                    std::string binlog_file = currentBinlogFile;
                    size_t binlog_pos = currentBinlogPos;
                    std::vector<VectorIndexUpdateItem*> updates;
                    parseRowsEvent(event_buf,
                                   event_len,
                                   tev,
                                   idcolpos - 1,
                                   veccolpos - 1,
                                   binlog_file,
                                   binlog_pos,
                                   updates);
                    for (auto item : updates) {
                        gqueue_.enqueue(item);
                    }
                }
            }
        }
        gqueue_.request_shutdown();  /* ensure workers unblock if we exited loop by break */
        for (auto& t : worker_threads_) {
            if (t.joinable())
                t.join();
        }
        worker_threads_.clear();
        close_binlog_mysql_conn();
    }
};

MyVectorBinlogService& get_binlog_service() {
  static MyVectorBinlogServiceImpl impl;
  return impl;
}

SERVICE_REGISTRATION(myvector_binlog_service, &get_binlog_service());

} // namespace myvector_component
