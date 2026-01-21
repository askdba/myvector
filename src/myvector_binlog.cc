/*  Copyright (c) 2024 - p3io.in / shiyer22@gmail.com */
/*
   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.

   This program is also distributed with certain software (including
   but not limited to OpenSSL) that is licensed under separate terms,
   as designated in a particular file or component or in included license
   documentation.  The authors of MySQL hereby grant you an additional
   permission to link the program and your derivative works with the
   separately licensed software that they have included with MySQL.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License, version 2.0, for more details.

   You should have received a copy of the GNU General Public License
   along with this program; if not, write to the Free Software
   Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301  USA
*/
#include <algorithm>
#include <condition_variable>
#include <fcntl.h>
#include <fstream>
#include <inttypes.h>
#include <map>
#include <regex>
#include <signal.h>
#include <sstream>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <utility>
#include <vector>

// #include <boost/lockfree/queue.hpp>
#include "compression.h"
#if defined(__GNUC__) || defined(__clang__)
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
#endif
#include "libbinlogevents/include/codecs/factory.h"
#include "libbinlogevents/include/compression/factory.h"
#include "libbinlogevents/include/trx_boundary_parser.h"
#if defined(__GNUC__) || defined(__clang__)
#pragma GCC diagnostic pop
#endif
#include "my_byteorder.h"
#include "my_dbug.h"
#include "my_default.h"
#include "my_dir.h"
#include "my_io.h"
#include "my_macros.h"
#include "my_time.h"
#include "mysql_version.h" // MYSQL_VERSION_ID
#include "prealloced_array.h"
#include "print_version.h"
#include "scope_guard.h"
#include "sql/binlog_reader.h"
#include "sql/log_event.h"
#include "sql/rpl_constants.h"
#include "sql/rpl_gtid.h"
#include "sql_common.h"
#include "sql_string.h"
#include "typelib.h"

#ifdef WIN32
#include <io.h>

static void usleep(int usec) { ::Sleep(usec / 1000); }

#else
#include <unistd.h>
#endif

#include "mysql/plugin.h"
#include "mysql/service_my_plugin_log.h"
#include "myvector.h"

// using namespace std; - Removed for code quality
using std::condition_variable;
using std::getline;
using std::ifstream;
using std::list;
using std::lock_guard;
using std::map;
using std::mutex;
using std::string;
using std::stringstream;
using std::thread;
using std::unique_lock;
using std::vector;

extern MYSQL_PLUGIN gplugin;
extern std::atomic<bool> shutdown_binlog_thread;
extern MYSQL *binlog_mysql_conn;

#if defined(__GNUC__) || defined(__clang__)
#define MYVECTOR_DIAGNOSTIC_PUSH _Pragma("GCC diagnostic push")
#define MYVECTOR_DIAGNOSTIC_POP _Pragma("GCC diagnostic pop")
#define MYVECTOR_IGNORE_DEPRECATED_DECLARATIONS \
  _Pragma("GCC diagnostic ignored \"-Wdeprecated-declarations\"")
#else
#define MYVECTOR_DIAGNOSTIC_PUSH
#define MYVECTOR_DIAGNOSTIC_POP
#define MYVECTOR_IGNORE_DEPRECATED_DECLARATIONS
#endif

#define debug_print(...)                                                       \
  my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL, __VA_ARGS__)
#define info_print(...)                                                        \
  my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL, __VA_ARGS__)
#define error_print(...)                                                       \
  my_plugin_log_message(&gplugin, MY_ERROR_LEVEL, __VA_ARGS__)
#define warning_print(...)                                                     \
  my_plugin_log_message(&gplugin, MY_WARNING_LEVEL, __VA_ARGS__)
#include "myvectorutils.h"

extern char *myvector_index_dir;

/// Format_description_event glob_description_event(BINLOG_VERSION,
/// server_version);

void myvector_table_op(const string &dbname, const string &tbname,
                       const string &cname, unsigned int pkid,
                       vector<unsigned char> &vec, const string &binlogfile,
                       const size_t &pos);
string myvector_find_earliest_binlog_file();

typedef struct {
  string dbName_;
  string tableName_;
  string columnName_;
  vector<unsigned char> vec_;
  unsigned int veclen_; // bytes
  unsigned int pkid_;
  string binlogFile_;
  size_t binlogPos_;
} VectorIndexUpdateItem;

typedef struct {
  string vectorColumn;
  int idColumnPosition;
  int vecColumnPosition;
} VectorIndexColumnInfo;

// boost::lockfree::queue<VectorIndexUpdateItem*> gqueue(128); /* FUTURE */

/* Simple Queue class for single producer-multiple consumer pattern. This
 * implementation currently uses std::mutex & std::cv. Rework later to use
 * boost::lockfree::queue<> if scalability issues hit.
 */
class EventsQ {
public:
  void enqueue(VectorIndexUpdateItem *item) {
    lock_guard lk(m_);
    items_.push_back(item);
    cv_.notify_one();
  }
  VectorIndexUpdateItem *dequeue() {
    std::unique_lock lk(m_);
    cv_.wait(lk, [] { return items_.size(); });

    VectorIndexUpdateItem *next = items_.front();
    items_.pop_front();
    return next; // consumer to call delete
  }
  bool empty() const { return items_.size() == 0; }

private:
  mutex m_;
  condition_variable cv_;
  static list<VectorIndexUpdateItem *> items_;
};

list<VectorIndexUpdateItem *> EventsQ::items_;
EventsQ gqueue_;

// map from db.table to <col1,col2,...>. Usually table will have a single
// vector column. But MyVector supports multiple vector index in 1 table.
mutex binlog_stream_mutex_;
map<string, VectorIndexColumnInfo> g_OnlineVectorIndexes;
string currentBinlogFile = "";
size_t currentBinlogPos = 0;

string myvector_conn_user_id;
string myvector_conn_password;
string myvector_conn_socket;
string myvector_conn_host;
string myvector_conn_port;

#define EVENT_HEADER_LENGTH 19

typedef struct {
  unsigned long tableId;
  std::string dbName;
  std::string tableName;
  unsigned int nColumns;
  vector<unsigned char> columnTypes;
  vector<int> columnMetadata;
} TableMapEvent;

/* parseTableMapEvent - Parse the TableMap binlog event that appears before
 * any *ROWS* event.
 */
void parseTableMapEvent(const unsigned char *event_buf, unsigned int event_len,
                        TableMapEvent &tev) {
  (void)event_len;
  tev = TableMapEvent();

  unsigned int index = EVENT_HEADER_LENGTH;

  memcpy(&tev.tableId, &event_buf[index], 6);
  index += 6;

  index += 2; // flags

  int dbNameLen = (int)event_buf[index]; // single byte
  index++;
  tev.dbName = std::string((const char *)&event_buf[index], dbNameLen);

  index += (dbNameLen + 1);              // null
  int tbNameLen = (int)event_buf[index]; // single byte
  index++;
  tev.tableName = std::string((const char *)&event_buf[index], tbNameLen);
  index += (tbNameLen + 1);

  string key = tev.dbName + "." + tev.tableName;
  if (g_OnlineVectorIndexes.find(key) == g_OnlineVectorIndexes.end())
    return; /// we don't need to parse rest of the metadata

  tev.nColumns =
      (unsigned int)event_buf[index]; // TODO - we support only <= 255 columns
  index++;

  tev.columnTypes.insert(tev.columnTypes.end(), &event_buf[index],
                         &event_buf[index + tev.nColumns]);
  index += tev.nColumns;
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
      md = (unsigned int)event_buf[index];
      index++;
      break;
    case MYSQL_TYPE_BIT:
    case MYSQL_TYPE_VARCHAR:
    case MYSQL_TYPE_NEWDECIMAL:
      memcpy(&md, &event_buf[index], 2);
      index += 2;
      break;
    case MYSQL_TYPE_SET:
    case MYSQL_TYPE_ENUM:
    case MYSQL_TYPE_STRING:
      memcpy(&md, &event_buf[index], 2);
      index += 2;
      break;
    case MYSQL_TYPE_TIME2:
    case MYSQL_TYPE_DATETIME2:
    case MYSQL_TYPE_TIMESTAMP2:
      md = (unsigned int)event_buf[index];
      index++;
      break;
    default:
      md = 0;
    } /// switch
    tev.columnMetadata.push_back(md);
  } /// for

  return;
}

void parseRowsEvent(const unsigned char *event_buf, unsigned int event_len,
                    TableMapEvent &tev, unsigned int pos1, unsigned int pos2,
                    vector<VectorIndexUpdateItem *> &updates) {
  unsigned int index = EVENT_HEADER_LENGTH;

  unsigned long tableId = 0;

  event_len -= 4; // checksum at the end.

  updates.clear();

  memcpy(&tableId, &event_buf[index], 6);
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
    unsigned int nullbitmap = (unsigned int)event_buf[index];
    (void)nullbitmap;
    index++;

    unsigned int lval = 0;
    unsigned long llval = 0;

    unsigned int idVal = 0, vecsz = 0;
    const unsigned char *vec = nullptr;
    for (unsigned int i = 0; i < ncols; i++) {
      switch (tev.columnTypes[i]) {
      case MYSQL_TYPE_LONG:
        memcpy(&lval, &event_buf[index], 4);
        index += 4;
        if (i == pos1)
          idVal = lval;
        break;
      case MYSQL_TYPE_LONGLONG:
        memcpy(&llval, &event_buf[index], 8);
        index += 8;
        break;
      case MYSQL_TYPE_VARCHAR: {
        unsigned int clen = 0;
        if (tev.columnMetadata[i] < 256) {
          clen = (unsigned int)event_buf[index];
          index++;
        } else {
          memcpy(&clen, &event_buf[index], 2);
          index += 2;
        }
        if (i == pos2) { // found vector column
          vec = &event_buf[index];
          vecsz = clen;
        }
        index += clen;
        break;
      }
#if MYSQL_VERSION_ID >= 90000
      case MYSQL_TYPE_VECTOR: {
        unsigned int clen = 0;
        memcpy(&clen, &event_buf[index], tev.columnMetadata[i]);
        index += tev.columnMetadata[i];
        if (i == pos2) { // found vector column
          vec = &event_buf[index];
          vecsz = clen;
        }
        index += clen;
        break;
      }
#endif
      case MYSQL_TYPE_TIMESTAMP2:
        index += 4;
        break;
      default:
        error_print("unrecognized column type %d", (int)tev.columnTypes[i]);
      } // switch
    }   // for columns

    VectorIndexUpdateItem *item = new VectorIndexUpdateItem();
    string key = tev.dbName + "." + tev.tableName;
    string columnName = g_OnlineVectorIndexes[key].vectorColumn;
    item->dbName_ = tev.dbName;
    item->tableName_ = tev.tableName;
    item->columnName_ = columnName;
    item->vec_.assign(vec, vec + vecsz);
    item->pkid_ = idVal;
    item->binlogFile_ = currentBinlogFile;
    item->binlogPos_ = currentBinlogPos;
    updates.push_back(item);
    // index += 4;
    if (index >= event_len)
      break; // done - multi rows

  } // while (true) - single row or multi-row event!
  return;
}

/* parseRotateEvent() : binlog ROTATE event indicates end of current binlog
 * file and start of new binlog file. The offs parameter is to handle quirks
 * in the first ROTATE event.
 */
void parseRotateEvent(const unsigned char *event_buf, unsigned int event_len,
                      string &binlogfile, size_t &binlogpos, bool offs) {
  int index = EVENT_HEADER_LENGTH;
  size_t position = 0;

  memcpy(&position, &event_buf[index], sizeof(position));
  binlogpos = position;

  index += sizeof(position);

  string filename =
      string((const char *)&event_buf[index], (event_len - index) - (4 * offs));
  binlogfile = filename;
}

void readConfigFile(const char *config_file) {
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

  myvector_conn_user_id = vo.getOption("myvector_user_id");
  myvector_conn_password = vo.getOption("myvector_user_password");
  myvector_conn_socket = vo.getOption("myvector_socket");
  myvector_conn_host = vo.getOption("myvector_host");
  myvector_conn_port = vo.getOption("myvector_port");
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
void GetBaseTableColumnPositions(MYSQL *hnd, const char *db, const char *table,
                                 const char *idcol, const char *veccol,
                                 int &idcolpos, int &veccolpos) {
  static const char *q = "select column_name, ordinal_position from "
                         "information_schema.columns where table_schema='%s' "
                         "and table_name='%s' and "
                         "(column_name='%s' or column_name='%s');";
  char buff[MYVECTOR_BUFF_SIZE];

  idcolpos = veccolpos = 0;

  snprintf(buff, sizeof(buff), q, db, table, idcol, veccol);

  if (mysql_real_query(hnd, buff, strlen(buff))) {
    // TODO
  }

  MYSQL_RES *result = mysql_store_result(hnd);
  if (!result) {
    // TODO
  }

  if (mysql_num_fields(result) != 2) {
    // TODO
  }

  MYSQL_ROW row;
  while ((row = mysql_fetch_row(result))) {
    unsigned long *lengths = mysql_fetch_lengths(result);
    if (!lengths[0] || !lengths[1] || !row[0] || !row[1]) {
      // TODO
    }

    char *colname = row[0];
    char *position = row[1];

    if (!strcmp(colname, idcol))
      idcolpos = atoi(position);
    if (!strcmp(colname, veccol))
      veccolpos = atoi(position);
  } // while

  mysql_free_result(result);

  debug_print("GetBaseColumn %s %s = %d %d", idcol, veccol, idcolpos,
              veccolpos);

  return;
}

void myvector_open_index_impl(char *vecid, char *details, char *pkidcol,
                              char *action, char *extra, char *result);

/* OpenAllOnlineVectorIndexes() - Query MYVECTOR_COLUMNS view and open/load
 * all vector indexes that have online=Y i.e updated online when DMLs are
 * done on the base table. This routine is called during plugin init.
 */
void OpenAllOnlineVectorIndexes(MYSQL *hnd) {
  static const char *q = "select db,tbl,col,info from test.myvector_columns";

  if (mysql_real_query(hnd, q, strlen(q))) {
    // TODO
    return;
  }

  MYSQL_RES *result = mysql_store_result(hnd);
  if (!result) {
    // TODO
    return;
  }

  MYSQL_ROW row;
  while ((row = mysql_fetch_row(result))) {
    unsigned long *lengths;
    lengths = mysql_fetch_lengths(result);

    char *dbname = row[0];
    char *tbl = row[1];
    char *col = row[2];
    char *info = row[3];

    if (!lengths[0] || !lengths[1] || !lengths[2] || !lengths[3] || !dbname ||
        !tbl || !col || !info) {
      // TODO
      continue;
    }

    info_print("Got index %s %s %s [%s]", dbname, tbl, col, info);

    MyVectorOptions vo(info);

    if (!vo.isValid()) {
      // TODO
      continue;
    }

    string online = vo.getOption("online");
    string idcol = vo.getOption("idcol");

    int idcolpos = 0, veccolpos = 0;
    GetBaseTableColumnPositions(hnd, dbname, tbl, idcol.c_str(), col, idcolpos,
                                veccolpos);
    if (idcolpos == 0 || veccolpos == 0)
      continue;

    if (online == "y" || online == "Y") {
      char empty[1024];
      char action[] = "load";
      char vecid[1024];
      snprintf(vecid, sizeof(vecid), "%s.%s.%s", dbname, tbl, col);
      myvector_open_index_impl(vecid, info, empty, action, empty, empty);

      snprintf(vecid, sizeof(vecid), "%s.%s", dbname, tbl);
      VectorIndexColumnInfo vc{col, idcolpos, veccolpos};
      g_OnlineVectorIndexes[vecid] = vc;
    }
  } // while

  mysql_free_result(result);
}

/* BuildMyVectorIndexSQL - Build/Refresh the Vector Index! This function uses
 * SQL to fetch rows from the base table and put the ID & vector into the
 * vector index.
 */
void BuildMyVectorIndexSQL(const char *db, const char *table, const char *idcol,
                           const char *veccol, const char *action,
                           const char *trackingColumn, AbstractVectorIndex *vi,
                           char *errorbuf) {

  strcpy(errorbuf, "SUCCESS");
  size_t nRows = 0;

  fprintf(stderr, "BuildMyVectorIndexSQL %s %s %s %s %s %s.\n", db, table,
          idcol, veccol, action, trackingColumn);

  MYSQL mysql;

  /* Use a new connection for vector index */
  mysql_init(&mysql);

  if (!mysql_real_connect(
          &mysql, myvector_conn_host.c_str(), myvector_conn_user_id.c_str(),
          myvector_conn_password.c_str(), NULL,
          (myvector_conn_port.length() ? atoi(myvector_conn_port.c_str()) : 0),
          myvector_conn_socket.c_str(), CLIENT_IGNORE_SIGPIPE)) {
    snprintf(errorbuf, MYVECTOR_BUFF_SIZE,
             "Error in new connection to build vector index : %s.",
             mysql_error(&mysql));
    return;
  }

  (void)mysql_autocommit(&mysql, false);

  char query[MYVECTOR_BUFF_SIZE];

  snprintf(query, sizeof(query),
           "SET TRANSACTION ISOLATION LEVEL READ COMMITTED");
  if (mysql_real_query(&mysql, query, strlen(query))) {
    // TODO
    return;
  }

  snprintf(query, sizeof(query), "LOCK TABLES %s.%s READ", db,
           table); // No DMLs during build.

  if (mysql_real_query(&mysql, query, strlen(query))) {
    // TODO
    return;
  }

  snprintf(query, sizeof(query), "SELECT %s, %s FROM %s.%s", idcol, veccol, db,
           table);

  /// table has been locked, now we perform the timestamp related stuff
  unsigned long current_ts = time(NULL);

  if (!strcmp(action, "refresh") || strlen(trackingColumn)) {
    char whereClause[1024];

    unsigned long previous_ts = vi->getUpdateTs();
    snprintf(whereClause, sizeof(whereClause),
             " WHERE unix_timestamp(%s) > %lu AND unix_timestamp(%s) <= %lu",
             trackingColumn, previous_ts, trackingColumn, current_ts);
    strcat(query, whereClause);
  }

  vi->setUpdateTs(current_ts);

  debug_print("Final Build Query : %s", query);

  if (mysql_real_query(&mysql, query, strlen(query))) {
    // TODO
  }

  MYSQL_RES *result = mysql_store_result(&mysql);

  MYSQL_ROW row;

  while ((row = mysql_fetch_row(result))) {
    unsigned long *lengths;
    lengths = mysql_fetch_lengths(result);

    char *idval = row[0];
    char *vec = row[1];

    if (!lengths[0] || !lengths[1] || !idval) {
      // TODO
    }

    vi->insertVector(vec, 0, atol(idval)); /// dim is already known by vi
    nRows++;
  }

  mysql_free_result(result);

  // Get binlog coordinates, set checkpoint id and flush

  {

    lock_guard<mutex> binlogMutex(binlog_stream_mutex_);

    vi->setLastUpdateCoordinates(currentBinlogFile, currentBinlogPos);

    snprintf(errorbuf, MYVECTOR_BUFF_SIZE,
             "SUCCESS: Index created & saved at (%s %lu)"
             ", rows : %lu.",
             currentBinlogFile.c_str(), currentBinlogPos, nRows);

    vi->saveIndex(myvector_index_dir, "build");

    string key = string(db) + "." + string(table);

    if (vi->supportsIncrUpdates()) {
      int idcolpos = 0, veccolpos = 0;
      GetBaseTableColumnPositions(&mysql, db, table, idcol, veccol, idcolpos,
                                  veccolpos);
      VectorIndexColumnInfo vc{veccol, idcolpos, veccolpos};
      g_OnlineVectorIndexes[key] = vc;
    }

    snprintf(query, sizeof(query), "UNLOCK TABLES");

    int ret = 0;
    if ((ret = mysql_real_query(&mysql, query, strlen(query)))) {
      snprintf(errorbuf, MYVECTOR_BUFF_SIZE,
               "Error in unlocking table (%d) %s.", ret, mysql_error(&mysql));
    }

  } // scope guard for binlog mutex lock

  mysql_close(&mysql);
}

void myvector_checkpoint_index(const string &dbtable, const string &veccol,
                               const string &binlogFile, size_t binlogPos);

/* FlushOnlineVectorIndexes - flush or checkpoint all vector indexes that
 * registered for online binlog based DML updates. This routine is currently
 * called on every binlog file rotation. Binlog global mutex should be held
 * by caller so that current binlog filename and position are locked.
 */
void FlushOnlineVectorIndexes() {
  /* first wait for the binlog event Q to drain out */
  while (1) {
    if (gqueue_.empty()) {
      break;
    }
    usleep(500 * 1000); // 1/2 second
  }
  for (auto vi : g_OnlineVectorIndexes) {
    myvector_checkpoint_index(vi.first, vi.second.vectorColumn,
                              currentBinlogFile, currentBinlogPos);
  }
}

void myvector_binlog_loop(int id) {
  (void)id;
  MYSQL mysql;
  auto close_binlog_mysql = [&]() {
    if (binlog_mysql_conn == &mysql) {
      mysql_close(&mysql);
      binlog_mysql_conn = nullptr;
    }
  };

  int ret;

  int connect_attempts = 0;

  readConfigFile(myvector_config_file);

  if (myvector_feature_level & 1) {
    info_print("Binlog event thread is disabled!");
    binlog_mysql_conn = nullptr; // Ensure global pointer is cleared on early exit.
    return;
  }

  /* wait till mysql is open to access */
  while (1) {

    mysql_init(&mysql);
    binlog_mysql_conn = &mysql;
    unsigned int read_timeout_sec = 1;
    mysql_options(&mysql, MYSQL_OPT_READ_TIMEOUT, &read_timeout_sec);

    if (!mysql_real_connect(
            &mysql, myvector_conn_host.c_str(), myvector_conn_user_id.c_str(),
            myvector_conn_password.c_str(), NULL,
            (myvector_conn_port.length() ? atoi(myvector_conn_port.c_str())
                                         : 0),
            myvector_conn_socket.c_str(), CLIENT_IGNORE_SIGPIPE)) {
      /// fprintf(stderr, "real connect failed %s\n", mysql_error(&mysql));
      sleep(1);
      if (shutdown_binlog_thread.load()) { // Check shutdown flag after sleep
        error_print("Binlog thread shutting down during connect retry.");
        close_binlog_mysql();
        return;
      }
      connect_attempts++;
      if (connect_attempts > 600) {
        error_print("MyVector binlog thread failed to connect (%s)",
                    mysql_error(&mysql));
        close_binlog_mysql();
        return;
      }
      continue;
    }
    break; /// connected
  }

  std::string initQuery =
      "SET @master_binlog_checksum = 'NONE', @source_binlog_checksum = "
      "'NONE',@net_read_timeout = 3000, @replica_net_timeout = 3000;";
  ret = mysql_real_query(&mysql, initQuery.c_str(), initQuery.length());
  printf("mysql_query ret = %d\n", ret);

  // BinlogPos bp = getMinimumBinlogReadPosition();

  OpenAllOnlineVectorIndexes(&mysql);

  string startbinlog = myvector_find_earliest_binlog_file();

  MYSQL_RPL rpl;
  memset(&rpl, 0, sizeof(rpl));
  rpl.file_name = NULL;
  if (startbinlog.length())
    rpl.file_name = startbinlog.c_str();
  rpl.start_position = 4;
  rpl.server_id = 1;
  ret = mysql_binlog_open(&mysql, &rpl);

  int cnt = 0;

  void vector_q_thread_fn(int id);
  for (int i = 0; i < myvector_index_bg_threads; i++) {
    std::thread(vector_q_thread_fn, i).detach();
  }

  size_t nrows = 0;

  TableMapEvent tev;
  while (!shutdown_binlog_thread.load()) {
    int fetch_rc = mysql_binlog_fetch(&mysql, &rpl);
    if (fetch_rc != 0) {
      if (shutdown_binlog_thread.load()) {
        break;
      }
      std::string err = mysql_error(&mysql);
      if (err.find("timed out") != std::string::npos) {
        continue;
      }
      error_print("Binlog fetch failed: %s", err.c_str());
      break;
    }
#if MYSQL_VERSION_ID >= 80400
    MYVECTOR_DIAGNOSTIC_PUSH
    MYVECTOR_IGNORE_DEPRECATED_DECLARATIONS
    using MyvectorLogEventType = binary_log::Log_event_type;
    constexpr MyvectorLogEventType kRotateEvent = binary_log::ROTATE_EVENT;
    constexpr MyvectorLogEventType kTableMapEvent =
        binary_log::TABLE_MAP_EVENT;
    constexpr MyvectorLogEventType kWriteRowsEvent =
        binary_log::WRITE_ROWS_EVENT;
    MYVECTOR_DIAGNOSTIC_POP
#else
    using MyvectorLogEventType = binary_log::Log_event_type;
    constexpr MyvectorLogEventType kRotateEvent = binary_log::ROTATE_EVENT;
    constexpr MyvectorLogEventType kTableMapEvent =
        binary_log::TABLE_MAP_EVENT;
    constexpr MyvectorLogEventType kWriteRowsEvent =
        binary_log::WRITE_ROWS_EVENT;
#endif

    MyvectorLogEventType type =
        static_cast<MyvectorLogEventType>(rpl.buffer[1 + EVENT_TYPE_OFFSET]);
    unsigned long event_len = rpl.size - 1;
    const unsigned char *event_buf = rpl.buffer + 1;

    if (type == kRotateEvent) {
      if (currentBinlogFile.length()) {
        FlushOnlineVectorIndexes();
      }
      parseRotateEvent(event_buf, event_len, currentBinlogFile,
                       currentBinlogPos, (currentBinlogFile.length() > 0));
      continue;
    }
    /// fprintf(stderr, "binlog position : %s %lu (%lu)\n",
    ///        currentBinlogFile.c_str(), currentBinlogPos, currentBinlogPos +
    ///        event_len);
    currentBinlogPos += event_len;
    if (g_OnlineVectorIndexes.size() == 0)
      continue; // optimization!
    if (type == kTableMapEvent) {
      parseTableMapEvent(event_buf, event_len, tev);
    } else if (type == kWriteRowsEvent) {
      string key = tev.dbName + "." + tev.tableName;
      if (g_OnlineVectorIndexes.find(key) == g_OnlineVectorIndexes.end()) {
        continue;
      }
      int idcolpos = g_OnlineVectorIndexes[key].idColumnPosition;
      int veccolpos = g_OnlineVectorIndexes[key].vecColumnPosition;
      vector<VectorIndexUpdateItem *> updates;
      parseRowsEvent(event_buf, event_len, tev, idcolpos - 1, veccolpos - 1,
                     updates);
      nrows += updates.size();
      for (auto item : updates) {
        gqueue_.enqueue(item);
      }
    }
    cnt++;
  } // while (binlog_fetch)
  error_print("Exiting binlog func, error %s", mysql_error(&mysql));
  close_binlog_mysql();
} // myvector_binlog_loop()

void vector_q_thread_fn(int id) {
  VectorIndexUpdateItem *item = nullptr;

  info_print("vector_q thread started %d", id);

  while (1) {
    item = gqueue_.dequeue();
    myvector_table_op(item->dbName_, item->tableName_, item->columnName_,
                      item->pkid_, item->vec_, item->binlogFile_,
                      item->binlogPos_);
    delete item;
  }
}
