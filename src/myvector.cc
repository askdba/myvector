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
#include <iomanip>
#include <list>
#include <memory>
#include <mutex>
#include <regex>
#include <set>
#include <shared_mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#ifdef WIN32
#define PLUGIN_EXPORT extern "C" __declspec(dllexport)
#else
#define PLUGIN_EXPORT extern "C"
#endif

#ifdef WIN32
#include <fcntl.h>
#include <io.h>
#include <sys\stat.h>
#include <sys\types.h>
#include <time.h>
#include <windows.h>
#include <winsock2.h>

int fsync(int fd) { return _commit(fd); }

static void usleep(int usec) { ::Sleep(usec / 1000); }
int strcasecmp(const char *s1, const char *s2) { return _stricmp(s1, s2); }
void asctime_r(const struct tm *timeptr, char *dst) {
  const char *poi = asctime(timeptr);
  strcpy(dst, poi);
}
int gettimeofday(struct timeval *tv, struct timezone *tz) {
  tv->tv_sec = time(0);
  tv->tv_usec = 0;
  return 0;
}

#define O_WRONLY _O_WRONLY
#define O_CREAT _O_CREAT
#define O_TRUNC _O_TRUNC
#define O_RDWR _O_RDWR
#else
#include <unistd.h>
#endif

#include "mysql.h"
#include "mysql/plugin.h"
#include "mysql/service_my_plugin_log.h"
#include "mysql/udf_registration_types.h"
#include "mysql_version.h"
#include "myvector.h"

extern MYSQL_PLUGIN gplugin;

#define debug_print(...)                                                       \
  my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL, __VA_ARGS__)
#define info_print(...)                                                        \
  my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL, __VA_ARGS__)
#define error_print(...)                                                       \
  my_plugin_log_message(&gplugin, MY_ERROR_LEVEL, __VA_ARGS__)
#define warning_print(...)                                                     \
  my_plugin_log_message(&gplugin, MY_WARNING_LEVEL, __VA_ARGS__)

// using namespace std; - Removed for code quality
using std::atomic;
using std::endl;
using std::getline;
using std::ifstream;
using std::list;
using std::lock_guard;
using std::map;
using std::min;
using std::mutex;
using std::pair;
using std::priority_queue;
using std::regex;
using std::regex_search;
using std::reverse;
using std::set;
using std::shared_lock;
using std::shared_mutex;
using std::string;
using std::stringstream;
using std::thread;
using std::to_string;
using std::unique_lock;
using std::unordered_map;
using std::vector;

#include "hnswdisk.h"
#include "hnswlib.h"
#include "myvectorutils.h"

#include "my_checksum.h"

#include <mysql/components/component_implementation.h>
#include <mysql/components/my_service.h>
#include <mysql/components/services/mysql_string.h>
#include <mysql/components/services/udf_metadata.h>

extern REQUIRES_SERVICE_PLACEHOLDER(mysql_udf_metadata);
extern REQUIRES_SERVICE_PLACEHOLDER(mysql_string_converter);
extern REQUIRES_SERVICE_PLACEHOLDER(mysql_string_factory);

#include <mysql/service_plugin_registry.h>

#define SET_UDF_ERROR_AND_RETURN(...)                                          \
  {                                                                            \
    my_plugin_log_message(&gplugin, MY_ERROR_LEVEL, __VA_ARGS__);              \
    *error = 1;                                                                \
    return (result);                                                           \
  }

extern my_service<SERVICE_TYPE(mysql_udf_metadata)> *h_udf_metadata_service;

class AbstractVectorIndex;

#include "myvector.h"

/* Each serialized vector has 4 bytes of metadata.
 * Byte 1 - MyVector vector format version
 * Byte 2 - Native datatype of vector elements. e.g FP32, FP16 etc
 * Byte 3 - Unused
 * Byte 4 - Unused
 */

static const unsigned int MYVECTOR_VERSION_V1 = 0x01;

static const unsigned int MYVECTOR_VECTOR_FP32 = 0x01;

static const unsigned int MYVECTOR_VECTOR_FP16 = 0x02;

static const unsigned int MYVECTOR_VECTOR_BV = 0x04;

#define MYVECTOR_V1_FP32_METADATA                                              \
  ((MYVECTOR_VECTOR_FP32 << 8) | MYVECTOR_VERSION_V1)

#define MYVECTOR_V1_BV_METADATA                                                \
  ((MYVECTOR_VECTOR_BV << 8) | MYVECTOR_VERSION_V1)

/* Maximum length of string that can be passed to myvector_construct(). */

static const unsigned int MYVECTOR_CONSTRUCT_MAX_LEN = 128000;

/* Maximum buffer space for return value of myvector_display().
   e.g 3072 dimensions x 23 characters for each dimension = 70kb string
 */
static const unsigned int MYVECTOR_DISPLAY_MAX_LEN = 128000;

/* Default precision for float output in myvector_display() */
static const unsigned int MYVECTOR_DISPLAY_DEF_PREC = 7;

/* Overhead per MYVECTOR column value - 4 bytes for checksum and
   4 bytes for metadata. Overhead disabled if we are using MySQL's VECTOR
 */
#if MYSQL_VERSION_ID >= 90000
static const unsigned int MYVECTOR_COLUMN_EXTRA_LEN = 0;
#else
static const unsigned int MYVECTOR_COLUMN_EXTRA_LEN = 8;
#endif

/* Default number of neighbours to return by myvector_ann_set() */
static const unsigned int MYVECTOR_DEFAULT_ANN_RETURN_COUNT = 10;

/* Max number of neighbours that can be retrieved in single myvector_ann_set()
 */
static const unsigned int MYVECTOR_MAX_ANN_RETURN_COUNT = 10000;

/* Basic check for validity of index last update timestamp > '01-01-2024' */
static const unsigned long MYVECTOR_MIN_VALID_UPDATE_TS = 1704047400;

/* Parallel threads are started after 100K vectors + keys have been batch'ed up
 */
static const unsigned int HNSW_PARALLEL_BUILD_UNIT_SIZE = 100000;

/* Bit packing for Binary Vectors */
static const unsigned int BITS_PER_BYTE = 8;

extern char *myvector_index_dir;
extern long myvector_feature_level;

char *latin1 = const_cast<char *>("latin1");

const set<string> MYVECTOR_INDEX_TYPES{"KNN", "HNSW", "HNSW_BV"};

/* Thread-local storage for distances from last ANN search.
 * Using a static thread_local object avoids manual memory management
 * and prevents memory leaks. The map is automatically constructed
 * on first use per thread and destroyed when the thread exits.
 */
static thread_local unordered_map<KeyTypeInteger, double> tls_distances_map;
static thread_local unordered_map<KeyTypeInteger, double> *tls_distances =
    &tls_distances_map;

inline bool isValidIndexType(const string &indextype) {
  return (MYVECTOR_INDEX_TYPES.find(indextype) != MYVECTOR_INDEX_TYPES.end());
}

inline int MyVectorStorageLength(int dim) {
  return (dim * sizeof(FP32)) + MYVECTOR_COLUMN_EXTRA_LEN;
}

inline int MyVectorDimFromStorageLength(int length) {
  return (length - MYVECTOR_COLUMN_EXTRA_LEN) / sizeof(FP32);
}

inline int MyVectorBVStorageLength(int dim) {
  return (dim / BITS_PER_BYTE) + MYVECTOR_COLUMN_EXTRA_LEN;
}

inline int MyVectorBVDimFromStorageLength(int length) {
  return (length - MYVECTOR_COLUMN_EXTRA_LEN) * BITS_PER_BYTE;
}

/* Compute L2/Eucliean squared distance via optimized function from hnswlib */
double computeL2Distance(const FP32 *__restrict v1, const FP32 *__restrict v2,
                         int dim) {
  hnswlib::L2Space sp(dim);
  hnswlib::DISTFUNC<FP32> distfn;
  double dist = 0.0;
  size_t sdim = dim;

  if (v1 && v2 && dim) {
    distfn = sp.get_dist_func();
    dist = distfn(v1, v2, &sdim);
  }

  return dist;
}

/* Compute InnerProduct distance via optimized function from hnswlib */
double computeIPDistance(const FP32 *__restrict v1, const FP32 *__restrict v2,
                         int dim) {
  hnswlib::InnerProductSpace sp(dim);
  hnswlib::DISTFUNC<FP32> distfn;
  double dist = 0.0;
  size_t sdim = dim;

  if (v1 && v2 && dim) {
    distfn = sp.get_dist_func();
    dist = distfn(v1, v2, &sdim);
  }

  return dist;
}

/* Compute Cosine distance using standard formula */
double computeCosineDistance(const FP32 *__restrict v1,
                             const FP32 *__restrict v2, int dim) {
  double dist = 0.0, v1v2 = 0.0, normv1 = 0.0, normv2 = 0.0;

  for (int i = 0; i < dim; i++) {
    v1v2 += (v1[i] * v2[i]);
    normv1 += (v1[i] * v1[i]);
    normv2 += (v2[i] * v2[i]);
  }

  double t = (sqrt(normv1 * normv2));
  if (t)
    dist = (double)v1v2 / t;

  return (1 - dist);
}

float computeCosineDistanceFn(const void *__restrict v1,
                              const void *__restrict v2,
                              const void *__restrict qty_ptr) {
  size_t qty = *((size_t *)qty_ptr); /// dimension of the vector
  return (float)computeCosineDistance((const FP32 *)v1, (const FP32 *)v2,
                                      (int)qty);
}

class AngularDistanceSpace : public hnswlib::SpaceInterface<float> {
  hnswlib::DISTFUNC<float> fstdistfunc_;
  size_t data_size_;
  size_t dim_;

public:
  AngularDistanceSpace(size_t dim) {
    fstdistfunc_ = computeCosineDistanceFn;
    dim_ = dim;
    data_size_ = dim * sizeof(float);
  }

  size_t get_data_size() { return data_size_; }

  hnswlib::DISTFUNC<float> get_dist_func() { return fstdistfunc_; }

  void *get_dist_func_param() { return &dim_; }

  ~AngularDistanceSpace() {}
};

/* HammingDistanceFn - Calculate Hamming distance between 2 bit vectors.
 * Prototype is in HNSWLIB style */
float HammingDistanceFn(const void *__restrict pVect1,
                        const void *__restrict pVect2,
                        const void *__restrict qty_ptr) {
  size_t qty = *((size_t *)qty_ptr); /// dimension of the vector
  float dist = 0;
  unsigned long ldist = 0;
  unsigned long *a = (unsigned long *)pVect1;
  unsigned long *b = (unsigned long *)pVect2;

  /* Hamming Distance between 2 byte sequences - Number of bit positions
   * matching/different in both the sequences. In the plugin, we calculate
   * the diff'ing bit positions and that is the distance. Thus smaller
   * distance implies the vectors are 'nearer'/'similar' to each other.
   */
  size_t iter = (qty / (sizeof(unsigned long) * BITS_PER_BYTE));
  for (size_t i = 0; i < iter; i++) {
    unsigned long res = (*a ^ *b);
    a++;
    b++;

#ifdef WIN32
    while (res > 0) {
      ldist += (res & 1);
      res >>= 1;
    }
#else
    ldist += __builtin_popcountll(res);
#endif
  }
  dist = ldist;
  return dist;
}

/* HammingBinaryVectorSpace : Implement hnswlib's SpaceInterface for
 * Hamming Distance based HNSW index for Binary Vectors.
 */
class HammingBinaryVectorSpace : public hnswlib::SpaceInterface<float> {
  hnswlib::DISTFUNC<float> fstdistfunc_;
  size_t data_size_;
  size_t dim_;

public:
  HammingBinaryVectorSpace(size_t dim) {
    fstdistfunc_ = HammingDistanceFn;
    dim_ = dim;
    data_size_ =
        (dim /
         BITS_PER_BYTE); /// 1 bit per dimension - assumes dim is multiple of 8
  }

  size_t get_data_size() { return data_size_; }

  hnswlib::DISTFUNC<float> get_dist_func() { return fstdistfunc_; }

  void *get_dist_func_param() { return &dim_; }

  ~HammingBinaryVectorSpace() {}
};

/* KNNIndex - A vector index type that implements brute-force KNN search in
 * the MyVector plugin. This index type could possibly be faster than SQL
 * performing ORDER BY myvector_distance(...) [as long as all vectors fit
 * in memory].
 */
class KNNIndex : public AbstractVectorIndex {
public:
  KNNIndex(const string &name, const string &options);

  ~KNNIndex() {}

  /* Next 4 methods are no-op in the KNN in-memory index */
  bool saveIndex(const string &path, const string &option = "");

  bool saveIndexIncr(const string &path, const string &option = "");

  bool loadIndex(const string &path);

  bool dropIndex(const string &path);

  bool initIndex();

  bool closeIndex();

  string getName() { return m_name; }
  string getType() { return "KNN"; }

  string getStatus();

  bool searchVectorNN(VectorPtr qvec, int dim, vector<KeyTypeInteger> &keys,
                      int n);
  bool insertVector(VectorPtr vec, int dim, KeyTypeInteger id);

  bool supportsIncrUpdates() { return true; }

  bool supportsPersist() { return false; }

  bool supportsConcurrentUpdates() { return false; } /// no mutexing!

  bool supportsIncrRefresh() { return true; }

  bool isReady() { return true; }

  bool isDirty() { return false; } /// No persistence

  int getDimension() { return m_dim; }

  bool startParallelBuild(int nthreads) { return false; }

  void setUpdateTs(unsigned long ts) { m_updateTs = ts; }

  unsigned long getUpdateTs() { return m_updateTs; }

  unsigned long getRowCount() { return m_n_rows; }

private:
  string m_name;
  string m_options;
  int m_dim;
  unsigned long m_updateTs;
  MyVectorOptions m_optionsMap;

  mutable std::shared_mutex search_insert_mutex_;

  atomic<unsigned long> m_n_rows{0};
  atomic<unsigned long> m_n_searches{0};

  /* The in-memory data store for the vectors */
  vector<pair<vector<FP32>, KeyTypeInteger>> m_vectors;

  double (*m_distfn)(const FP32 *v1, const FP32 *v2, int dim);
};

KNNIndex::KNNIndex(const string &name, const string &options)
    : m_name(name), m_options(options), m_optionsMap(options), m_updateTs(0) {
  m_dim = m_optionsMap.getIntOption("dim", 0);

  m_distfn = computeL2Distance;
  if (m_optionsMap.getOption("dist").size()) {
    if (m_optionsMap.getOption("dist") == "Cosine")
      m_distfn = computeCosineDistance;
    else if (m_optionsMap.getOption("dist") == "IP")
      m_distfn = computeIPDistance;
  } else {
    m_optionsMap.setOption("dist", "L2");
  }
  setUpdateTs(0);
}

/* Brute-force, exact search KNN implemented using in-memory vector<> and
 * priority queue. Potentially faster than SELECT ... ORDER BY
 * myvector_distance()
 */
bool KNNIndex::searchVectorNN(VectorPtr qvec, int dim,
                              vector<KeyTypeInteger> &keys, int n) {
  std::shared_lock lock(search_insert_mutex_);

  priority_queue<pair<FP32, KeyTypeInteger>> pq;
  keys.clear();

  /* Use priority queue to find out 'n' neighbours with least distance */
  for (auto row : m_vectors) {
    vector<FP32> &a = row.first;
    double dist = m_distfn((FP32 *)qvec, a.data(), m_dim);

    if (pq.size() < n)
      pq.push({dist, row.second});
    else {
      auto top = pq.top();
      if (dist < top.first) {
        pq.pop();
        pq.push({dist, row.second});
      }
    }
  } /* for */

  while (pq.size()) {
    auto r = pq.top();
    pq.pop();
    keys.push_back(r.second);
    (*tls_distances)[r.second] = r.first; /// pkid -> distance
  }

  reverse(keys.begin(), keys.end()); /// nearest to farthest

  m_n_searches++;
  return true;
}

/* insertVector - just stash the vector into in-memory vector<> collection */
bool KNNIndex::insertVector(VectorPtr vec, int dim, KeyTypeInteger id) {
  std::unique_lock lock(search_insert_mutex_);

  FP32 *fvec = static_cast<FP32 *>(vec);
  vector<FP32> row(fvec, fvec + m_dim);
  m_vectors.push_back({row, id}); /// simple index - multithread safe

  m_n_rows++;

  return true;
}

bool KNNIndex::saveIndex(const string &, const string &) {
  my_plugin_log_message(&gplugin, MY_WARNING_LEVEL,
                        "KNN Memory Index (%s) - Save Index to disk is no-op",
                        m_name.c_str());

  return true;
}

bool KNNIndex::saveIndexIncr(const string &, const string &) {
  my_plugin_log_message(
      &gplugin, MY_WARNING_LEVEL,
      "KNN Memory Index (%s) - Save Index Incr to disk is no-op",
      m_name.c_str());

  return true;
}

bool KNNIndex::dropIndex(const string &) {
  my_plugin_log_message(&gplugin, MY_WARNING_LEVEL,
                        "KNN Memory Index (%s) - Drop Index is no-op",
                        m_name.c_str());

  return true;
}

bool KNNIndex::loadIndex(const string &) {
  my_plugin_log_message(&gplugin, MY_WARNING_LEVEL,
                        "KNN Memory Index (%s) - Load Index is no op",
                        m_name.c_str());
  return true;
}

bool KNNIndex::initIndex() {
  debug_print("KNN Memory Index (%s) - initIndex()", m_name.c_str());

  /// nothing much to do!!
  m_vectors.clear();
  m_n_rows = 0;
  m_n_searches = 0;

  return true;
}

bool KNNIndex::closeIndex() { return true; }

string KNNIndex::getStatus() {
  std::stringstream ss;

  ss << endl;
  ss << "Vector Index : " << m_name << endl;
  ss << "Type : KNN" << endl;
  ss << "Dimension : " << m_dim << endl;
  ss << "Distance : " << m_optionsMap.getOption("dist") << endl;
  ss << "Rows Inserted : " << m_n_rows << endl;
  ss << "Searches : " << m_n_searches << endl;

  return ss.str();
}

class HNSWMemoryIndex : public AbstractVectorIndex {
public:
  HNSWMemoryIndex(const string &name, const string &options);

  ~HNSWMemoryIndex();

  bool supportsIncrUpdates() { return m_incrUpdates; }
  bool supportsIncrRefresh() { return m_incrRefresh; }
  bool isDirty() { return m_isDirty; }

  bool saveIndex(const string &path, const string &option);

  bool saveIndexIncr(const string &path, const string &option);

  bool loadIndex(const string &path);

  bool dropIndex(const string &path);

  bool initIndex();

  bool closeIndex();

  string getName() { return m_name; }

  string getType() { return m_type; }

  string getStatus();

  bool searchVectorNN(VectorPtr qvec, int dim, vector<KeyTypeInteger> &keys,
                      int n);

  bool insertVector(VectorPtr vec, int dim, KeyTypeInteger id);

  int getDimension() { return m_dim; }

  void setUpdateTs(unsigned long ts) { m_updateTs = ts; }

  unsigned long getUpdateTs() { return m_updateTs; }

  unsigned long getRowCount() { return m_n_rows; }

  bool startParallelBuild(int nthreads);

  virtual hnswlib::SpaceInterface<float> *getSpace(size_t dim);

  int getEfConstruction() { return m_ef_construction; }
  int getM() { return m_M; }
  int getSize() { return m_size; }

  void getLastUpdateCoordinates(string &binlogFile, size_t &binlogPos);
  void setLastUpdateCoordinates(const string &binlogFile,
                                const size_t &binlogPos);
  void getCheckPointString(string &ckstr);

  void setSearchEffort(int ef_search);

private:
  string m_name;
  string m_type;
  string m_options;
  MyVectorOptions m_optionsMap;
  unsigned long m_updateTs;

  int m_dim;
  int m_ef_construction;
  int m_ef_search;
  int m_M;
  int m_size;

  string m_dist;

  hnswlib::AlgorithmInterface<FP32> *m_alg_hnsw = nullptr;
  hnswlib::SpaceInterface<float> *m_space = nullptr;

  atomic<unsigned long> m_n_rows{0};
  atomic<unsigned long> m_n_searches{0};

  bool m_isDirty;

  bool m_incrUpdates;
  bool m_incrRefresh;

  /* Temporary store for multi-threaded, parallel index build */
  vector<char> m_batch;
  vector<KeyTypeInteger> m_batchkeys;
  bool m_isParallelBuild{false};

  bool flushBatchParallel();
  bool flushBatchSerial();

  int m_threads;

  /// last update coordinates
  string m_binlogFile;
  size_t m_binlogPosition;
};

HNSWMemoryIndex::HNSWMemoryIndex(const string &name, const string &options)
    : m_name(name), m_options(options), m_optionsMap(options), m_updateTs(0) {
  bool valid = true;
  m_dim = m_optionsMap.getIntOption("dim", 0, &valid);
  m_size = m_optionsMap.getIntOption("size", 0, &valid);
  m_ef_construction = m_optionsMap.getIntOption("ef", 100, &valid);
  m_ef_search = m_ef_construction;
  m_M = m_optionsMap.getIntOption("M", 16, &valid);
  m_type = m_optionsMap.getOption("type"); // Supports HNSW and HNSW_BV
  m_incrUpdates = m_optionsMap.getOption("online") == "Y";
  m_incrRefresh = m_optionsMap.getOption("track").length() > 0;

  m_dist = "L2";

  if (m_optionsMap.getOption("dist") == "Cosine")
    m_dist = "Cosine";
  else if (m_optionsMap.getOption("dist") == "CosineNorm")
    m_dist = "CosineNorm";
  else if (m_optionsMap.getOption("dist") == "Angular")
    m_dist = "Angular";

  if (m_optionsMap.getOption("ef_search").length())
    m_ef_search = m_optionsMap.getIntOption("ef_search", m_ef_construction);

  debug_print("hnsw index params %s %s  %d %d %d %d %d", name.c_str(),
              m_type.c_str(), m_dim, m_size, m_ef_construction, m_ef_search,
              m_M);
}

HNSWMemoryIndex::~HNSWMemoryIndex() {
  if (m_alg_hnsw)
    delete m_alg_hnsw;
  if (m_space)
    delete m_space;
}

bool HNSWMemoryIndex::initIndex() {
  debug_print("hnsw initIndexO %p %s %d %d %d %d %d", this, m_name.c_str(),
              m_dim, m_size, m_ef_construction, m_ef_search, m_M);
  m_space = getSpace(m_dim);

  m_alg_hnsw = new hnswlib::HierarchicalDiskNSW<FP32>(m_space, m_size, m_M,
                                                      m_ef_construction);

  (dynamic_cast<hnswlib::HierarchicalDiskNSW<FP32> *>(m_alg_hnsw))
      ->setEf(m_ef_search);

  m_n_rows = 0;
  m_n_searches = 0;

  setLastUpdateCoordinates("zzzzzz.bin", 99999999999);
  setUpdateTs(0);

  return true;
}

void HNSWMemoryIndex::getCheckPointString(string &ckstr) {

  stringstream ss;

  if (supportsIncrUpdates()) {
    string binlogFile;
    size_t binlogPos = 0;

    getLastUpdateCoordinates(binlogFile, binlogPos);
    ss << "Checkpoint:binlog:" << binlogFile << ":" << binlogPos;
  } else {
    ss << "Checkpoint:timestamp:" << getUpdateTs();
  }
  ckstr = ss.str();
}

bool HNSWMemoryIndex::saveIndex(const string &path, const string &option) {
#ifdef TODO
  if (!m_isDirty) {
    my_plugin_log_message(&gplugin, MY_WARNING_LEVEL,
                          "HNSW index %s is not updated, save not required",
                          m_name.c_str());
    return true;
  }
#endif

  // lockExclusive();

  if (m_isParallelBuild) {
    flushBatchSerial(); // last batch, maybe small
  }

  debug_print("HNSWemoryIndex::saveIndex %s %s.", path.c_str(), option.c_str());

  string filename = path + "/" + m_name + ".hnsw.index";

  string checkPointStr;
  getCheckPointString(checkPointStr);

  if (!m_alg_hnsw) {
    error_print("HNSWMemoryIndex::saveIndex (%s) : null HNSW object.",
                m_name.c_str());
    return false;
  }

  hnswlib::HierarchicalDiskNSW<FP32> *alg_hnsw =
      dynamic_cast<hnswlib::HierarchicalDiskNSW<FP32> *>(m_alg_hnsw);
  alg_hnsw->setCheckPointId(checkPointStr);

  if (option == "build") {
    // hnswlib method for full write/rewrite. Expect 10GB to take 10 secs.
    alg_hnsw->saveIndex(filename);
  } else {
    // "refresh" or "checkpoint" - special MyVector incremental persistence.
    alg_hnsw->doCheckPoint(filename);
  }

  m_isDirty = false;
  m_isParallelBuild = false;

  return true;
}

bool HNSWMemoryIndex::saveIndexIncr(const string &path, const string &option) {
  return true;
}

bool HNSWMemoryIndex::loadIndex(const string &path) {
  if (m_alg_hnsw)
    delete m_alg_hnsw;
  if (m_space)
    delete m_space;

  m_alg_hnsw = nullptr;
  m_space = nullptr;

  m_space = getSpace(m_dim);

  string indexfile = path + "/" + m_name + ".hnsw.index";

  debug_print("Loading HNSW index %s from %s", m_name.c_str(),
              indexfile.c_str());

  /* hnswlib throws std::runtime_error for errors */
  m_alg_hnsw = nullptr;
  try {
    m_alg_hnsw = new hnswlib::HierarchicalDiskNSW<FP32>(m_space, indexfile);
  } catch (std::runtime_error &e) {
    warning_print("Error loading hnsw index (%s) from file : %s",
                  m_name.c_str(), e.what());
  }

  if (!m_alg_hnsw) { /* no disk files found */
    initIndex();
  } else {
    string binlogFile;
    size_t binlogPosition = 0;
    size_t ts = 0;

    string ckid = dynamic_cast<hnswlib::HierarchicalDiskNSW<FP32> *>(m_alg_hnsw)
                      ->getCheckPointId();

    if (ckid.find("Checkpoint:timestamp") != string::npos) {
      ts = atol(ckid.substr(ckid.rfind(":") + 1).c_str());
      debug_print("load index checkpoint ts = %lu.", ts);
      setUpdateTs(ts);
    } else if (ckid.find("Checkpoint:binlog") != string::npos) {
      // ckptid=Checkpoint:binlog:binlog.000516:6761
      size_t p1 = ckid.rfind(":");
      binlogPosition = atol(ckid.substr(p1 + 1).c_str());
      size_t p2 = ckid.rfind(":", p1 - 1);
      binlogFile = ckid.substr(p2 + 1, (p1 - (p2 + 1)));
      setLastUpdateCoordinates(binlogFile, binlogPosition);
    }
  }

  debug_print("debug HNSW index %s from %s", m_name.c_str(), indexfile.c_str());
  dynamic_cast<hnswlib::HierarchicalDiskNSW<FP32> *>(m_alg_hnsw)->debug();
  return true;
}

bool HNSWMemoryIndex::dropIndex(const string &path) {
  /* Force drop index - delete files and free memory */
  string indexfile = path + "/" + m_name + ".hnsw.index";
  unlink(indexfile.c_str());
  string linksfile = path + "/" + m_name + ".hnsw.index.links";
  unlink(linksfile.c_str());
  string linksdatafile = path + "/" + m_name + ".hnsw.index.links.data";
  unlink(linksdatafile.c_str());
  string statusfile = path + "/" + m_name + ".hnsw.index.status";
  unlink(statusfile.c_str());

  if (m_alg_hnsw)
    delete m_alg_hnsw;
  m_alg_hnsw = nullptr;

  if (m_space)
    delete m_space;
  m_space = nullptr;

  return true;
}

bool HNSWMemoryIndex::closeIndex() { return true; }

void HNSWMemoryIndex::setSearchEffort(int ef_search) {
  (dynamic_cast<hnswlib::HierarchicalDiskNSW<FP32> *>(m_alg_hnsw))
      ->setEf(ef_search);
}

hnswlib::SpaceInterface<float> *HNSWMemoryIndex::getSpace(size_t dim) {
  if (m_type == "HNSW" && m_dist == "L2")
    return new hnswlib::L2Space(m_dim);
  else if (m_type == "HNSW" && m_dist == "CosineNorm")
    return new hnswlib::InnerProductSpace(m_dim);
  else if (m_type == "HNSW" && m_dist == "Cosine")
    return new AngularDistanceSpace(m_dim);
  else if (m_type == "HNSW" && m_dist == "Angular")
    return new AngularDistanceSpace(m_dim);
  else if (m_type == "HNSW_BV")
    return new HammingBinaryVectorSpace(m_dim);

  return nullptr;
}

string HNSWMemoryIndex::getStatus() {
  std::stringstream ss;

  ss << endl;
  ss << "Vector Index : " << m_name << endl;
  ss << "Type : " << m_type << endl;
  ss << "Dimension : " << m_dim << endl;
  ss << "Distance : " << m_optionsMap.getOption("dist") << endl;
  ss << "Max. Capacity : " << m_size << endl;
  ss << "M = " << m_M << endl;

  if (m_alg_hnsw) {
    ss << "Element Data Size : "
       << (dynamic_cast<hnswlib::HierarchicalDiskNSW<FP32> *>(m_alg_hnsw))
              ->size_data_per_element_
       << endl;
    ss << "Current Rows : "
       << (dynamic_cast<hnswlib::HierarchicalDiskNSW<FP32> *>(m_alg_hnsw))
              ->cur_element_count
       << endl;
    ss << "Searches : " << m_n_searches << endl;
  }

  return ss.str();
}

bool HNSWMemoryIndex::searchVectorNN(VectorPtr qvec, int dim,
                                     vector<KeyTypeInteger> &keys, int n) {

  priority_queue<pair<FP32, hnswlib::labeltype>> result =
      m_alg_hnsw->searchKnn(qvec, n);

  keys.clear();
  tls_distances->clear();
  while (!result.empty()) {
    keys.push_back(result.top().second);
    (*tls_distances)[result.top().second] =
        result.top().first; /// pkid -> distance
    result.pop();
  }

  reverse(keys.begin(), keys.end()); // nearest to farthest
  m_n_searches++;
  return true;
}

void HNSWMemoryIndex::getLastUpdateCoordinates(string &binlogFile,
                                               size_t &binlogPosition) {
  binlogFile = m_binlogFile;
  binlogPosition = m_binlogPosition;
}

void HNSWMemoryIndex::setLastUpdateCoordinates(const string &binlogFile,
                                               const size_t &binlogPosition) {
  m_binlogFile = binlogFile;
  m_binlogPosition = binlogPosition;
  debug_print("setLastUpdateCoordinates %s %lu", binlogFile.c_str(),
              binlogPosition);
}

// Parallel HNSW index load (hnswlib:example_search_mt.cpp)
template <class Function>
inline void ParallelFor(size_t start, size_t end, size_t numThreads,
                        Function fn) {
  debug_print("Entered ParallelFor %lu %lu t=%lu", start, end, numThreads);
  if (numThreads <= 0) {
    numThreads = thread::hardware_concurrency();
  }

  if (numThreads == 1) {
    for (size_t id = start; id < end; id++) {
      fn(id, 0);
    }
  } else {
    vector<thread> threads;
    atomic<size_t> current(start);

    // keep track of exceptions in threads
    // https://stackoverflow.com/a/32428427/1713196
    exception_ptr lastException = nullptr;
    mutex lastExceptMutex;

    for (size_t threadId = 0; threadId < numThreads; ++threadId) {
      threads.push_back(thread([&, threadId] {
        while (true) {
          size_t id = current.fetch_add(1);

          if (id >= end) {
            break;
          }

          try {
            fn(id, threadId);
          } catch (...) {
            unique_lock<mutex> lastExcepLock(lastExceptMutex);
            lastException = current_exception();
            /*
             * This will work even when current is the largest value that
             * size_t can fit, because fetch_add returns the previous value
             * before the increment (what will result in overflow
             * and produce 0 instead of current + 1).
             */
            current = end;
            break;
          }
        }
      }));
    }
    for (auto &thread : threads) {
      thread.join();
    }
    if (lastException) {
      rethrow_exception(lastException);
    }
  }
}

bool HNSWMemoryIndex::startParallelBuild(int nthreads) {
  m_batch.clear();
  m_batchkeys.clear();
  m_isParallelBuild = true;
  m_threads = nthreads;
  return true;
}

bool HNSWMemoryIndex::flushBatchSerial() {
  debug_print("flushBatchSerial %lu", m_batchkeys.size());
  for (unsigned int i = 0; i < m_batchkeys.size(); i++) {
    m_alg_hnsw->addPoint((void *)&(m_batch[i * m_space->get_data_size()]),
                         m_batchkeys[i]);
  }
  return true;
}

bool HNSWMemoryIndex::flushBatchParallel() {
  debug_print("Entered flushBatchParallel for (%s), nthreads = %d, sz = %u",
              m_name.c_str(), m_threads, m_batchkeys.size());
  /* Add data to index - HNSW multi-threaded example from
   * hnswlib:example_search_mt.cpp.
   */
  ParallelFor(
      0, m_batchkeys.size(), m_threads, [&](size_t row, size_t threadId) {
        m_alg_hnsw->addPoint((void *)&(m_batch[row * m_space->get_data_size()]),
                             m_batchkeys[row]);
      });

  m_batch.clear();
  m_batchkeys.clear();

  return true;
}

bool HNSWMemoryIndex::insertVector(VectorPtr vec, int dim, KeyTypeInteger id) {
  FP32 *fvec = static_cast<FP32 *>(vec);
  if (m_isParallelBuild) {
    // m_batch.insert(m_batch.end(), fvec, fvec + m_dim);
    m_batch.insert(m_batch.end(), (char *)vec,
                   ((char *)vec + m_space->get_data_size()));
    m_batchkeys.push_back(id);

    if (m_batchkeys.size() == HNSW_PARALLEL_BUILD_UNIT_SIZE) {
      flushBatchParallel();
    }
  } else {
    m_alg_hnsw->addPoint(fvec, id);
  }

  m_n_rows++; // atomic
  m_isDirty = true;
  return true;
}

class SharedLockGuard {
public:
  SharedLockGuard(AbstractVectorIndex *h_index) : m_index(h_index) {}
  ~SharedLockGuard() {
    if (m_index)
      m_index->unlockShared();
  }
  void clear() { m_index = nullptr; }

private:
  AbstractVectorIndex *m_index{nullptr};
};

AbstractVectorIndex *VectorIndexCollection::open(const string &name,
                                                 const string &options,
                                                 const string &useraction) {

  lock_guard<mutex> l(m_mutex); // exclusive

  debug_print("Opening new index %s %s %s", name.c_str(), options.c_str(),
              useraction.c_str());

  AbstractVectorIndex *hnewindex = nullptr;

  /* First case handles both HNSW and HNSW_BV */
  if (options.rfind("type=HNSW") != string::npos) {
    hnewindex = new HNSWMemoryIndex(name, options);
  } else if (options.rfind("type=KNN") != string::npos) {
    hnewindex = new KNNIndex(name, options);
  } else {
    my_plugin_log_message(
        &gplugin, MY_ERROR_LEVEL,
        "MyVector unknown index type for %s options = %s, using KNN",
        name.c_str(), options.c_str());
    hnewindex = new KNNIndex(name, options);
  }

  m_indexes[name] = hnewindex;

  return hnewindex;
}

AbstractVectorIndex *VectorIndexCollection::get(const string &name) {
  lock_guard<mutex> l(m_mutex);
  AbstractVectorIndex *hindex = nullptr;
  if (m_indexes.find(name) != m_indexes.end()) {
    hindex = m_indexes[name];
    hindex->lockShared(); /* acquire lock before returning from here */
  } else {
    my_plugin_log_message(&gplugin, MY_ERROR_LEVEL,
                          "VectorIndexCollection::get() index not found %s",
                          name.c_str());
  }

  return hindex;
}

bool VectorIndexCollection::close(AbstractVectorIndex *hindex) {
  lock_guard<mutex> l(m_mutex);

  hindex->unlockShared(); /* taken when opening the index */

  hindex->lockExclusive(); /* wait for all readers to drain */
  hindex->closeIndex();
  m_indexes.erase(m_indexes.find(hindex->getName()));
  hindex->unlockExclusive();

  delete hindex; /* no other thread can hold shared lock */
  return true;
}

/* FindEarliestBinlogFile() - Find the oldest binlog file from all
 * the "online" vector indexes checkpoint info.
 */
string VectorIndexCollection::FindEarliestBinlogFile() {
  string ret = "";
  for (auto entry : m_indexes) {
    string binlogfile;
    size_t binlogpos;
    if (entry.second->supportsIncrUpdates()) {
      entry.second->getLastUpdateCoordinates(binlogfile, binlogpos);
      if (ret == "")
        ret = binlogfile;
      else if (binlogfile < ret)
        ret = binlogfile;
    }
  }
  if (ret == "zzzzzz.bin")
    ret = "";
  debug_print("FindEarliestBinlogFile : %s.", ret.c_str());
  return ret;
}

static VectorIndexCollection g_indexes;

/* The MYVECTOR* Annotations supported by this plugin */
const string MYVECTOR_COLUMN_A = "MYVECTOR(";
const string MYVECTOR_IS_ANN_A = "MYVECTOR_IS_ANN(";
const string MYVECTOR_SEARCH_A = "MYVECTOR_SEARCH";
const string MYVECTOR_DEFAULT_INDEX_TYPE = "type=KNN";

const string MYVECTOR_IS_ANN_USAGE =
    "MYVECTOR_IS_ANN('<vector col>','<id col>','<search_vec>'[,'<options>'])";
const string MYVECTOR_SEARCH_USAGE =
    "MYVECTOR_SEARCH(baseTable,idColumn,vectorColumn,queryTable[,options])";

/* MySQL Column COMMENT max. length is 1024.e.g comment with all fields set :
 * MYVECTOR Column
 * |type=HNSW,dim=1536,size=1000000,M=64,ef=100,track=updatets,threads=8,dist=L2
 */
const size_t MYVECTOR_MAX_COLUMN_INFO_LEN = 128;

/* For v1, let us restrict to 4096. OpenAI has 3072 dimension embeddings
 * now in model :  text-embedding-3-large. Technically, there is no limitation
 * in the VARBINARY datatype. MySQL's VECTOR datatype supports max of 16383.
 */
const size_t MYVECTOR_MAX_VECTOR_DIM = 4096;

/* rewriteMyVectorColumnDef() - rewrite the MYVECTOR(...) annotation in
 * CREATE TABLE & ALTER TABLE.
 */
bool rewriteMyVectorColumnDef(const string &query, string &newQuery) {
  // support multiple MYVECTOR(...) columns
  size_t pos;
  bool error = false;

  newQuery = query;
  while ((pos = newQuery.find(MYVECTOR_COLUMN_A)) != string::npos) {
    size_t spos = pos + MYVECTOR_COLUMN_A.length();
    size_t epos = newQuery.find_first_of(')', pos);

    if (epos == string::npos) {
      my_plugin_log_message(&gplugin, MY_ERROR_LEVEL,
                            "MYVECTOR column terminating ')' not found.");
      error = true;
      break;
    }

    string colinfo = newQuery.substr(spos, (epos - spos));
    if (colinfo.length() > MYVECTOR_MAX_COLUMN_INFO_LEN) {
      my_plugin_log_message(&gplugin, MY_ERROR_LEVEL,
                            "MYVECTOR column info too long, length = %d.",
                            colinfo.length());
      error = true;
      break;
    }

    MyVectorOptions vo(colinfo);

    if (!vo.isValid()) {
      my_plugin_log_message(&gplugin, MY_ERROR_LEVEL,
                            "MYVECTOR column options parse error, options=%s.",
                            colinfo.c_str());
      error = true;
      break;
    }

    string vtype = vo.getOption("type");
    if (vtype == "") {
      colinfo = MYVECTOR_DEFAULT_INDEX_TYPE + "," + colinfo;
      vtype = "KNN";
      vo.setOption("type", vtype);
    }

    if (vo.getOption("dim") == "") {
      my_plugin_log_message(&gplugin, MY_ERROR_LEVEL,
                            "MYVECTOR column dimension not defined.");
      error = true;
      break;
    }

    bool addTrackingColumn = false;
    string trackingColumn;
    if (vo.getOption("track").length()) {
      addTrackingColumn = true;
      trackingColumn = vo.getOption("track");
    }

    bool dimValid = true;
    int dim = vo.getIntOption("dim", 0, &dimValid);

    if (!dimValid || dim <= 1 || dim > MYVECTOR_MAX_VECTOR_DIM) {
      my_plugin_log_message(&gplugin, MY_ERROR_LEVEL,
                            "MYVECTOR column dimension incorrect %d.", dim);
      error = true;
      break;
    }

    size_t varblength = 0;
    if (vtype != "HNSW_BV")
      varblength = MyVectorStorageLength(dim);
    else
      varblength = MyVectorBVStorageLength(dim); // binary vector

    string newColumn = "";
#if MYSQL_VERSION_ID >= 90000
    newColumn = "VECTOR(" + to_string(dim) + ") COMMENT 'MYVECTOR Column |" +
                colinfo + "'";
#else
    newColumn = "VARBINARY(" + to_string(varblength) +
                ") COMMENT 'MYVECTOR Column |" + colinfo + "'";
#endif

    if (addTrackingColumn) {
      newColumn = newColumn + ", " + trackingColumn +
                  " TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE "
                  "CURRENT_TIMESTAMP";
    }
    newQuery = newQuery.substr(0, pos) + newColumn + newQuery.substr(epos + 1);
  } // while

  my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL,
                        "MYVECTOR column rewrite \n%s.", newQuery.c_str());
  return error;
}

/* rewriteMyVectorIsANN() - rewrite the "WHERE MYVECTOR_IS_ANN(...)" annotation
 */
bool rewriteMyVectorIsANN(const string &query, string &newQuery) {
  size_t pos;
  bool error = false;

  newQuery = query;
  while ((pos = newQuery.find(MYVECTOR_IS_ANN_A)) != string::npos) {
    size_t spos = pos + MYVECTOR_IS_ANN_A.length();
    size_t epos = spos + 1;
    int ob = 1;
    /* We can have nested () in MYVECTOR_IS_ANN
     * e.g MYVECTOR_IS_ANN(a,b,myvector_construct(...))
     */
    while (epos < newQuery.length()) {
      if (newQuery[epos] == '(')
        ob++;
      if (newQuery[epos] == ')') {
        ob--;
        if (!ob)
          break; /// done
      }
      epos++;
    }

    if (ob) {
      error = true;
      break;
    }

    string strparams = newQuery.substr(spos, (epos - spos));

    vector<string> annparams;
    split(strparams, annparams);

    if (annparams.size() < 3) {
      error = true;
      break;
    }

    string idcolexpr = annparams[1];
    idcolexpr =
        idcolexpr.substr(1, idcolexpr.length() - 2); // remove the single quote

    stringstream ss;
    ss << "( " << idcolexpr << " IN "
       << "(select `myvecid` from JSON_TABLE(myvector_ann_set(" << strparams
       << "), " << '"' << "$[*]" << '"'
       << " COLUMNS(`myvecid` BIGINT PATH \"$\")) `myvector_ann`) )";

    newQuery = newQuery.substr(0, pos) + ss.str() + newQuery.substr(epos + 1);
  } // while

  my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL,
                        "MYVECTOR_IS_ANN query rewrite \n%s.",
                        newQuery.c_str());
  return error;
}

/* rewriteMyVectorSearch - rewrite the MYVECTOR_SEARCH[...] annotation */
bool rewriteMyVectorSearch(const string &query, string &newQuery) {
  bool error = false;
  size_t pos;

  newQuery = query;
  string delim;
  /* No nested [] in MYVECTOR_SEARCH[...] */
  while ((pos = newQuery.find(MYVECTOR_SEARCH_A)) != string::npos) {
    size_t spos = pos + MYVECTOR_SEARCH_A.length();
    size_t epos;
    if (newQuery[spos] == '[')
      delim = "]";
    else if (newQuery[spos] == '{')
      delim = "}";
    else {
      error = true;
      break;
    }
    spos += delim.length();
    if ((epos = newQuery.find(delim, spos)) == string::npos) {
      error = true;
      break;
    }

    string strparams = newQuery.substr(spos, (epos - spos));

    vector<string> annparams;
    split(strparams, annparams);

    if (annparams.size() < 4 || annparams.size() > 5) {
      my_plugin_log_message(
          &gplugin, MY_ERROR_LEVEL,
          "Incorrect MYVECTOR_SEARCH syntax : %s\nExample usage : %s",
          strparams.c_str(), MYVECTOR_SEARCH_USAGE.c_str());
      error = true;
      break;
    }

    /*  select article5 from MYVECTOR_SEARCH[test.t1, id, test.t1.v1, query,
     * n=5]; */

    string basetable = annparams[0];
    string idcol = annparams[1];
    string vecindex = annparams[2];
    string queryt = annparams[3];

    string annopt = "";
    if (annparams.size() > 4)
      annopt = annparams[4];

    stringstream ss;
    /// The query table must have a vector column named 'searchvec'
    ss << basetable << " where " << idcol << " in (select myvecid from "
       << queryt << " b, json_table(myvector_ann_set('" << vecindex << "','"
       << idcol << "', searchvec, '" << annopt << "') , " << '"' << "$[*]"
       << '"' << " COLUMNS(`myvecid` BIGINT PATH " << '"' << "$" << '"'
       << ")) `myvector_ann`)";

    newQuery = newQuery.substr(0, pos) + ss.str() +
               newQuery.substr(epos + delim.length());
  }

  return error;
}

/* myvector_query_rewrite() - Entrypoint of pre-parse query rewrite by this
 * plugin. This routine looks for CREATE TABLE, ALTER TABLE, SELECT and
 * EXPLAIN and presence of MYVECTOR and proceeds to perform  the query
 * transformation.
 */
bool myvector_query_rewrite(const string &query, string *rewritten_query) {

  /* Check if SQL is CREATE/ALTER/SELECT/EXPLAIN */
  if (query.length() == 0 || !strchr("CcAaSsEe", query[0]))
    return false;

  /* quick top-level check and exit if no MYVECTOR* pattern found in query */
  if (!strstr(query.c_str(), "MYVECTOR"))
    return false;

  static const regex create_table("^CREATE\\s+TABLE",
                                  regex::icase | regex::nosubs);
  static const regex alter_table("^ALTER\\s+TABLE",
                                 regex::icase | regex::nosubs);
  static const regex select_stmt("^SELECT\\s+", regex::icase | regex::nosubs);
  static const regex explain_stmt("^EXPLAIN\\s+", regex::icase | regex::nosubs);

  string newQuery = "";
  *rewritten_query = query;

  if ((regex_search(query, select_stmt) || regex_search(query, explain_stmt))) {
    if (strstr(query.c_str(), MYVECTOR_IS_ANN_A.c_str())) {
      if (rewriteMyVectorIsANN(query, newQuery)) {
        newQuery = "";
      }
    } else if (strstr(query.c_str(), MYVECTOR_SEARCH_A.c_str())) {
      if (rewriteMyVectorSearch(query, newQuery)) {
        newQuery = "";
      }
    }
  } else if ((regex_search(query, create_table) ||
              regex_search(query, alter_table)) &&
             (strstr(query.c_str(), MYVECTOR_COLUMN_A.c_str()))) {
    if (rewriteMyVectorColumnDef(query, newQuery)) {
      newQuery = "";
    }
  }

  if (newQuery.length())
    *rewritten_query = newQuery;

  return (*rewritten_query != query);
}

PLUGIN_EXPORT bool myvector_ann_set_init(UDF_INIT *initid, UDF_ARGS *args,
                                         char *message) {
  initid->ptr = nullptr;
  if (args->arg_count < 3 || args->arg_count > 4) {
    strcpy(message,
           "Incorrect arguments, usage : "
           "myvector_ann_set('vec column', 'id column', searchvec [,nn=<n>]).");
    return true; // error
  }

  char *col = args->args[0];
  AbstractVectorIndex *vi = g_indexes.get(col);
  SharedLockGuard l(vi);
  if (!vi) {
    snprintf(message, MYSQL_ERRMSG_SIZE,
             "Vector index (%s) not defined or not open for access.", col);
    return true; // error
  }

  /* Users can possibly ask for 100s of neighbours. With buffer of 128000,
   * about 12800 PK ids can be filled in the return string
   */
  initid->max_length = MYVECTOR_DISPLAY_MAX_LEN;
  initid->ptr = (char *)malloc(initid->max_length);
  (*h_udf_metadata_service)->result_set(initid, "charset", latin1);

  /* tls_distances is now a static thread_local object, no allocation needed */
  tls_distances->clear();

  return false;
}

PLUGIN_EXPORT void myvector_ann_set_deinit(UDF_INIT *initid) {
  if (initid && initid->ptr)
    free(initid->ptr);
  /* tls_distances is a static thread_local object, no deallocation needed.
   * Clear it to free memory used by the map entries.
   */
  tls_distances->clear();
}

PLUGIN_EXPORT char *myvector_ann_set(UDF_INIT *initid, UDF_ARGS *args,
                                     char *result, unsigned long *length,
                                     unsigned char *is_null,
                                     unsigned char *error) {
  char *col = args->args[0];
  char *idcol = args->args[1];
  FP32 *searchvec = (FP32 *)args->args[2];
  const char *searchoptions = nullptr;

  if (!col || !idcol || !searchvec) {
    *error = 1;
    *is_null = 1;
    return initid->ptr;
  }

  if (args->arg_count == 4)
    searchoptions = args->args[3];

  int nn = MYVECTOR_DEFAULT_ANN_RETURN_COUNT;
  int ef_search = 0;
  if (searchoptions && args->lengths[3]) {
    MyVectorOptions vo(searchoptions);

    /* How many neighbours to return? */
    nn = vo.getIntOption("nn", MYVECTOR_DEFAULT_ANN_RETURN_COUNT);
    if (nn <= 0)
      nn = MYVECTOR_DEFAULT_ANN_RETURN_COUNT;

    nn = min((const unsigned int)nn, MYVECTOR_MAX_ANN_RETURN_COUNT);

    ef_search = vo.getIntOption("ef_search", 0);
  }

  AbstractVectorIndex *vi = g_indexes.get(col);
  SharedLockGuard l(vi);

  stringstream ss;
  if (vi && searchvec) {
    vector<KeyTypeInteger> result;
    if (ef_search)
      vi->setSearchEffort(ef_search);
    vi->searchVectorNN(searchvec, vi->getDimension(), result, nn);

    /* simple JSON list of neighbour rows Pkid */
    ss << "[";
    for (int i = 0; i < result.size(); i++) {
      if (i)
        ss << ",";
      ss << result[i];
    }
    ss << "]";
  } else {
    *is_null = 1;
    *error = 1;
    ss << "[NULL]";
  }

  *length = ss.str().length();
  result = initid->ptr;
  strcpy(result, ss.str().c_str());
  return result;
}

/* SQFloatVectorToBinaryVector - Simple scalar quantization to convert
 * a sequence of native floats to a binary vector. If the float value is
 * greater than 0, then corresponding bit is set to 1 in the binary vector.
 */
int SQFloatVectorToBinaryVector(FP32 *fvec, unsigned long *ivec, int dim) {
  memset(ivec, 0, (dim / BITS_PER_BYTE)); // 3rd param is bytes

  unsigned long elem = 0;
  unsigned long idx = 0;

  for (int i = 0; i < dim; i++) {
    elem = elem << 1;
    if (fvec[i] > 0) {
      elem = elem | 1;
    }

    if (((i + 1) % 64) == 0) { // 8 bytes packed (i.e 64 dims in 1 ulong)
      ivec[idx] = elem;
      elem = 0;
      idx++;
    }
  }
  /// TODO : if dim is not a mulitple of 64
  return (idx * sizeof(unsigned long)); // number of bytes
}

char *myvector_construct_bv(const std::string &srctype, char *src, char *dst,
                            unsigned long srclen, unsigned long *length,
                            unsigned char *is_null, unsigned char *error) {
  int retlen = 0;

  if (srctype == "bv") {
    memcpy(dst, src, srclen); // src is bytes representing the binary vector
    retlen = srclen;
  } else if (srctype == "float") {
    int dim = MyVectorDimFromStorageLength(srclen);
    retlen =
        SQFloatVectorToBinaryVector((FP32 *)src, (unsigned long *)dst, dim);
  } else if (srctype == "string") {
    char *start = nullptr, *ptr = nullptr;
    char endch;

    if ((start = strchr(src, '[')))
      endch = ']';
    else if ((start = strchr(src, '{')))
      endch = '}';
    else if ((start = strchr(src, '(')))
      endch = ')';
    else {
      start = src;
      endch = '\0';
    }
    if (endch)
      start++;

    ptr = start;

    while (*ptr && *ptr != endch) {
      while (*ptr && (*ptr == ' ' || *ptr == ','))
        ptr++;
      char *p1 = ptr;
      while (*ptr != ' ' && *ptr != ',' && *ptr != endch)
        ptr++;
      char buff[64];

      strncpy(buff, p1, (ptr - p1));
      buff[(int)(ptr - p1)] = 0;

      dst[retlen] = (unsigned char)(atoi(buff));

      retlen += sizeof(unsigned char);
    } // while
  }   // else

#if MYSQL_VERSION_ID < 90000
  unsigned int metadata = MYVECTOR_V1_BV_METADATA;
  memcpy(&dst[retlen], &metadata, sizeof(metadata));
  retlen += sizeof(metadata);

  ha_checksum cksum = my_checksum(0, (const unsigned char *)dst, retlen);
  memcpy(&dst[retlen], &cksum, sizeof(cksum));
  retlen += sizeof(cksum);
#endif
  *length = retlen;
  return dst;
}

PLUGIN_EXPORT bool myvector_construct_init(UDF_INIT *initid, UDF_ARGS *args,
                                           char *message) {
  if (args->arg_count < 1 || args->arg_count > 2) {
    strcpy(message, "Incorrect arguments, usage : "
                    "myvector_construct(vector_string [, input_format])");
    return true; // error
  }
  initid->max_length = MYVECTOR_CONSTRUCT_MAX_LEN;
  initid->ptr = (char *)malloc(MYVECTOR_CONSTRUCT_MAX_LEN);
  return false;
}

/* SQL function : MYVECTOR_CONSTRUCT(embedding_string)
 *   Input   : Embedding vector string e.g
 *             "[-0.006929283495992422 -0.005336422007530928 ...]"
 *   Returns : Serialized "myvector" to store in VARBINARY column.
 *             Each floating point value in the embedding vector string is
 *             converted to a 4-byte "FP32" (IEEE754). At the end, 2 4-byte
 *             integers are added :-
 *             1) 4-byte Metadata
 *             2) 4-byte Checksum (computed from all the FP32s plus Metadata)
 *
 *             If input string is NULL, then NULL is returned.
 */

PLUGIN_EXPORT char *myvector_construct(UDF_INIT *initid, UDF_ARGS *args,
                                       char *result, unsigned long *length,
                                       unsigned char *is_null,
                                       unsigned char *error) {
  char *ptr = args->args[0];
  const char *opt = nullptr;
  if (args->arg_count == 2)
    opt = args->args[1];

  char *start = nullptr;
  char endch;
  char *retvec = initid->ptr;
  int retlen = 0;
  bool skipConvert = false;

  if (!opt || !args->lengths[1])
    opt = "i=string,o=float"; // i=string,o=float
  else {
    MyVectorOptions vo(opt);

    /*
     * i = float, o = float : App already has the vector in floats, just
                              need to add MyVector metadata and checksum
     * i = bv,    o = bv    : App is sending bytes of a Binary Vector
                              (e.g Cohere model). Add metadata + checksum
     * i = string, o = bv   : Convert series of 1-byte int's to  Binary
                              Vector
     * i = column, o = bv   : App wants to implement SQ compression. Convert
                              MyVector float column to BV
     */
    if (vo.getOption("i") == "float" && vo.getOption("o") == "float")
      skipConvert = true;

    /* For Binary Vectors, we will branch out to a separate routine */
    if (vo.getOption("o") == "bv")
      return myvector_construct_bv(vo.getOption("i"), ptr, initid->ptr,
                                   args->lengths[0], length, is_null, error);
  } // else opt

  if (skipConvert) {
    // User is passing floats directly in bind variable or using "0x" literal
    if ((args->lengths[0] % sizeof(FP32)) != 0)
      SET_UDF_ERROR_AND_RETURN("Input vector is malformed, length not a "
                               "multiple of sizeof(float) %lu.",
                               args->lengths[0]);
    memcpy(retvec, ptr, args->lengths[0]);
    retlen = args->lengths[0];
    goto addChecksum;
  }

  /* Below code implements conversion from string "[0.134511 -0.082219 ...]" to
   * floats followed by metadata & checksum.
   */
  if ((start = strchr(ptr, '[')))
    endch = ']';
  else if ((start = strchr(ptr, '{')))
    endch = '}';
  else if ((start = strchr(ptr, '(')))
    endch = ')';
  else {
    start = ptr;
    endch = '\0';
  }
  if (endch)
    start++;

  ptr = start;

  while (*ptr && *ptr != endch) {
    while (*ptr && (*ptr == ' ' || *ptr == ','))
      ptr++;
    char *p1 = ptr;
    while (*ptr != ' ' && *ptr != ',' && *ptr != endch)
      ptr++;
    char buff[64];
    strncpy(buff, p1, (ptr - p1));

    // TODO - atof() returns 0 if not a valid float
    FP32 fval = atof(buff); // change these 2 lines for FP16, INT8 etc
    memcpy(&retvec[retlen], &fval, sizeof(FP32));

    retlen += sizeof(FP32);
  } // while

addChecksum:
#if MYSQL_VERSION_ID < 90000
  unsigned int metadata = MYVECTOR_V1_FP32_METADATA;
  memcpy(&retvec[retlen], &metadata, sizeof(metadata));
  retlen += sizeof(metadata);

  ha_checksum cksum = my_checksum(0, (const unsigned char *)retvec, retlen);
  memcpy(&retvec[retlen], &cksum, sizeof(cksum));
  retlen += sizeof(cksum);
#endif
  *length = retlen;

  return retvec;
}

PLUGIN_EXPORT void myvector_construct_deinit(UDF_INIT *initid) {
  if (initid && initid->ptr)
    free(initid->ptr);
}

PLUGIN_EXPORT bool myvector_display_init(UDF_INIT *initid, UDF_ARGS *args,
                                         char *message) {
  if (args->arg_count == 0 || args->arg_count > 2) {
    strcpy(message, "Incorrect arguments, usage : "
                    "myvector_display(vec_col_expr, ['prec']).");
    return true; // error
  }
  initid->max_length = MYVECTOR_DISPLAY_MAX_LEN;
  initid->ptr = (char *)malloc(MYVECTOR_DISPLAY_MAX_LEN);
  (*h_udf_metadata_service)->result_set(initid, "charset", latin1);
  return false;
}

PLUGIN_EXPORT char *myvector_display(UDF_INIT *initid, UDF_ARGS *args,
                                     char *result, unsigned long *length,
                                     char *is_null, char *error) {
  unsigned char *bvec = (unsigned char *)args->args[0];
  FP32 *fvec = (FP32 *)args->args[0];
  if (!bvec || !args->lengths[0]) {
    *is_null = 1;
    *error = 1;
    return result;
  }

  /* We assume MySQL passes the VARBINARY value aligned correctly i.e aligned
   * at highest 8 bytes. Need to verify that when supporting double, FP16 etc.
   */

  int precision = 0;
  if (args->arg_count > 1 && args->args[1] && args->lengths[1]) {
    precision = atoi(args->args[1]);
  }
  if (!precision)
    precision = MYVECTOR_DISPLAY_DEF_PREC;

  stringstream ostr;

#if MYSQL_VERSION_ID < 90000
  ha_checksum cksum1;
  char *raw = args->args[0];
  /* Checksum is at the end */
  memcpy((char *)&cksum1, &(raw[args->lengths[0] - sizeof(ha_checksum)]),
         sizeof(ha_checksum));
  ha_checksum cksum2 = my_checksum(0, (const unsigned char *)raw,
                                   args->lengths[0] - sizeof(ha_checksum));
  if (cksum1 != cksum2) {
    *error = 1;
    return "<invalid vector>";
  }
#endif

  int dim = 0;
#if MYSQL_VERSION_ID < 90000
  unsigned int metadata = 0;
  memcpy((char *)&metadata, &bvec[args->lengths[0] - MYVECTOR_COLUMN_EXTRA_LEN],
         sizeof(metadata));

  if (metadata == MYVECTOR_V1_FP32_METADATA) {
    bvec = nullptr;
    dim = MyVectorDimFromStorageLength(args->lengths[0]);
  } else if (metadata == MYVECTOR_V1_BV_METADATA) {
    fvec = nullptr;
    dim = MyVectorBVDimFromStorageLength(args->lengths[0]);
    dim = dim / 8; /* bit-packet */
  } else {         /* 'old' v0 vectors */
    bvec = nullptr;
    dim = (args->lengths[0]) / sizeof(FP32);
  }
#else
  dim = MyVectorDimFromStorageLength(args->lengths[0]);
#endif

  ostr << "[";
  ostr << setprecision(precision);
  for (int i = 0; i < dim; i++) {
    if (i)
      ostr << ", ";
    if (fvec) {
      ostr << *fvec;
      fvec++;
    } else {
      ostr << (unsigned int)*bvec;
      bvec++;
    }
  }
  ostr << "]";

  result = initid->ptr;
  strcpy(result, ostr.str().c_str());
  *length = ostr.str().length();

  return result;
}

PLUGIN_EXPORT void myvector_display_deinit(UDF_INIT *initid) {
  if (initid && initid->ptr)
    free(initid->ptr);
}

/* UDF MYVECTOR_DISTANCE() Implementation */
PLUGIN_EXPORT bool myvector_distance_init(UDF_INIT *, UDF_ARGS *args,
                                          char *message) {
  if (args->arg_count < 2) {
    strcpy(message, "myvector_distance() requires atleast 2 arguments.");
    return true; /// error
  }
  if (args->arg_count > 3) {
    strcpy(message,
           "Too many arguments, usage : myvector_distance(v1,v2 [,dist]).");
    return true; /// error
  }
  return false;
}

PLUGIN_EXPORT bool myvector_construct_binaryvector_init(UDF_INIT *initid,
                                                        UDF_ARGS *args,
                                                        char *message) {
  if (args->arg_count != 1) {
    strcpy(message, "Incorrect arguments, usage : "
                    "myvector_construct_binary_vector(vec_col_expr)");
    return true; // error
  }
  initid->max_length = 1024;
  initid->ptr = (char *)malloc(initid->max_length);
  return false;
}

PLUGIN_EXPORT bool myvector_hamming_distance_init(UDF_INIT *initid,
                                                  UDF_ARGS *args,
                                                  char *message) {
  return false;
}

PLUGIN_EXPORT double myvector_hamming_distance(UDF_INIT *, UDF_ARGS *args,
                                               char *is_null, char *) {
  double dist = 0.0;
  unsigned long *v1 = (unsigned long *)(args->args[0]);
  unsigned long *v2 = (unsigned long *)(args->args[1]);

  size_t dim = args->lengths[0] * 8;

  return HammingDistanceFn(v1, v2, &dim);
}

PLUGIN_EXPORT double myvector_distance(UDF_INIT *, UDF_ARGS *args,
                                       char *is_null, char *error) {
  double dist = 0.0;
  FP32 *v1 = (FP32 *)(args->args[0]);
  FP32 *v2 = (FP32 *)(args->args[1]);
  int dim1 = MyVectorDimFromStorageLength(args->lengths[0]);
  int dim2 = MyVectorDimFromStorageLength(args->lengths[1]);

  /* Unsafe hack - keep going if 2 vectors have different dimension? */
  if (dim1 != dim2) {
    if (dim2 > dim1)
      dim2 = dim1;
    else
      dim1 = dim2;
  }

  if (!v1 || !v2 || (dim1 != dim2) || (dim1 <= 0)) {
    *error = 1;
    *is_null = 1;
    return 0.0;
  }

  const char *disttype = "L2"; // default
  if (args->arg_count == 3)
    disttype = args->args[2];

  double (*distfn)(const FP32 *v1, const FP32 *v2, int dim);
  // TODO : Cache function pointer in _init() if the 3rd argument is a constant
  // literal
  if (!disttype) {
    *error = 1; // NULL distance measure
    return 0.0;
  }

  if (!strcasecmp(disttype, "L2") || !strcasecmp(disttype, "EUCLIDEAN"))
    distfn = computeL2Distance;
  else if (!strcasecmp(disttype, "Cosine"))
    distfn = computeCosineDistance;
  else if (!strcasecmp(disttype, "IP"))
    distfn = computeIPDistance;
  else {
    *error = 1; // Incorrect distance measure
    return 0.0;
  }

  dist = distfn(v1, v2, dim1);
  return dist;
}

PLUGIN_EXPORT void myvector_distance_deinit(UDF_INIT *initid) {}

PLUGIN_EXPORT bool
myvector_search_open_udf_init(UDF_INIT *initid, UDF_ARGS *args, char *message) {
  if (args->arg_count != 5) {
    strcpy(message, "Incorrect arguments to MyVector internal UDF.");
    return true;
  }
  return false;
}

void BuildMyVectorIndexSQL(const char *db, const char *table, const char *idcol,
                           const char *veccol, const char *action,
                           const char *trackingColumn, AbstractVectorIndex *vi,
                           char *errorbuf);

void myvector_open_index_impl(char *vecid, char *details, char *pkidcol,
                              char *action, char *extra, char *result) {
  bool existing = true;
  /* Admin operations usecases :-

   1. Load rows into base table -> call myvector("build"). If VectorIndex is
   already populated, it will be discarded and rebuilt. Persist index after
   build!
   2. Patch existing rows, delete a few rows, insert new rows -> call
   myvector("build")
   3. Any trouble with existing index  -> call myvector("drop")
   4. After reboot/restart VM -> call myvector("load") (or myvector("build"))
   5. If tracking column technique -> call myvector("refresh") - index should be
   loaded first. refresh will not persist. After reboot/restart, call
   myvector("load") followed by myvector("refresh")
   6. For explicit persist  -> call myvector("save"), needed after "refresh"
  */

  AbstractVectorIndex *vi = g_indexes.get(vecid);
  if (!vi) {
    vi = g_indexes.open(vecid, details, action);
    if (!vi) {
      strcpy(result, "Failed to open index");
      return;
    }
    existing = false;
  }
  SharedLockGuard l(vi);

  string trackingColumn = "";
  int nthreads = 0;

  MyVectorOptions vo(details);
  if (vo.getOption("track").length()) {
    trackingColumn = vo.getOption("track");
  }
  if (!strcmp(action, "refresh") && !trackingColumn.length()) {
    strcpy(result, "Tracking column not found for incremental refresh.");
    return;
  }
  if (vo.getOption("threads").length()) { // override
    nthreads = vo.getIntOption("threads", myvector_index_bg_threads);
  } else {
    nthreads = myvector_index_bg_threads;
  }

  if (!strcmp(action, "save")) {
    vi->saveIndex(myvector_index_dir);
  } else if (!strcmp(action, "status")) {
    string s = vi->getStatus();
    strcpy(result, s.c_str());
  } else if (!strcmp(action, "drop")) {
    vi->dropIndex(myvector_index_dir);
    l.clear();
    g_indexes.close(vi);
    vi = nullptr;
  }

  else if (!strcmp(action, "load")) {
    debug_print("Loading index %s.", vecid);
    vi->loadIndex(myvector_index_dir); // will handle 'reload' also
  } else if (!strcmp(action, "build")) {
    vi->dropIndex(myvector_index_dir);

    vi->initIndex(); // start new

    if (nthreads >= 2)
      vi->startParallelBuild(nthreads);
  } else if (!strcmp(action, "refresh")) {
    if (nthreads >= 2)
      vi->startParallelBuild(nthreads);
  }

  if (!strcmp(action, "build") || !strcmp(action, "refresh")) {
    char errorbuf[1024];
    char *db, *table, *veccol;
    db = vecid;
    table = strchr(db, '.');
    *table = 0;
    table++;
    veccol = strchr(table, '.');
    *veccol = 0;
    veccol++;
    BuildMyVectorIndexSQL(db, table, pkidcol, veccol, action,
                          trackingColumn.c_str(), vi, errorbuf);
    strcpy(result, errorbuf);
    if (!strcmp(action, "refresh")) {
      unsigned long lastts = vi->getUpdateTs();
      char timebuf[64];
      strcat(result, ", Vector Index refreshed at : ");
      asctime_r(gmtime((time_t *)&lastts), timebuf);
      strcat(result, timebuf);
    }

    vi->saveIndex(myvector_index_dir, action);
  }
  return;
}

PLUGIN_EXPORT char *myvector_search_open_udf(UDF_INIT *, UDF_ARGS *args,
                                             char *result,
                                             unsigned long *length,
                                             unsigned char *is_null,
                                             unsigned char *) {
  char *vecid = args->args[0];
  char *details = args->args[1];
  char *pkidcol = args->args[2];
  char *action = args->args[3];
  char *extra = args->args[4];

  my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL,
                        "myvector_search_open() params %s %s %s %s %s", vecid,
                        details, pkidcol, action, extra);

  strcpy(result, "SUCCESS");

  myvector_open_index_impl(vecid, details, pkidcol, action, extra, result);

  *length = strlen(result);
  return result;
}

PLUGIN_EXPORT void myvector_search_open_udf_deinit() {}

PLUGIN_EXPORT bool
myvector_search_save_udf_init(UDF_INIT *initid, UDF_ARGS *args, char *message) {
  if (args->arg_count != 5) {
    strcpy(message, "Incorrect arguments to MyVector internal UDF.");
    return true;
  }
  return false;
}

PLUGIN_EXPORT char *myvector_search_save_udf(UDF_INIT *, UDF_ARGS *args,
                                             char *result,
                                             unsigned long *length,
                                             unsigned char *is_null,
                                             unsigned char *) {
  char *vecid = args->args[0];
  char *details = args->args[1];
  char *pkidcol = args->args[2];
  char *action = args->args[3];
  char *extra = args->args[4];

  if (!g_indexes.get(vecid)) {
    my_plugin_log_message(&gplugin, MY_ERROR_LEVEL,
                          "Index %s is not opened for build/refresh.", vecid);
    strcpy(result, "FAILED");
    *length = 7;
    return result;
  }

  AbstractVectorIndex *vi = g_indexes.get(vecid);
  vi->saveIndex(myvector_index_dir, action);
  return result;
}

PLUGIN_EXPORT void myvector_search_save_udf_deinit() {}

PLUGIN_EXPORT bool myvector_search_add_row_udf_init(UDF_INIT *initid,
                                                    UDF_ARGS *args,
                                                    char *message) {
  char *vecid = args->args[0];
  char *details = args->args[1];

  /* Check if index is open and 'cache' it */
  if (!g_indexes.get(vecid)) {
    my_plugin_log_message(&gplugin, MY_ERROR_LEVEL,
                          "Index %s is not opened for update.", vecid);
    return true;
  }

  initid->ptr = (char *)(g_indexes.get(vecid));
  return false;
}

/* UDF : myvector_search_add_row_udf() */
PLUGIN_EXPORT long long myvector_search_add_row_udf(UDF_INIT *initid,
                                                    UDF_ARGS *args,
                                                    char *is_null,
                                                    char *error) {
  char *vecid = args->args[0];
  char *details = args->args[1];

  long long pkid = *((long long *)args->args[2]);
  char *vecval = args->args[3];
  int dims = MyVectorDimFromStorageLength(args->lengths[3]);

  AbstractVectorIndex *vi = (AbstractVectorIndex *)(initid->ptr);
  if (vi) {
    vi->insertVector((FP32 *)vecval, dims, pkid);
  } else {
    *error = 1;
    return 0;
  }

  return 1; /* 1 row added to index */
}

/* UDF : myvector_search_add_row_udf() */
PLUGIN_EXPORT void myvector_search_add_row_udf_deinit(UDF_INIT *initid) {
  /* build/rebuild/refresh complete - persist the index if it is capable */
  AbstractVectorIndex *vi = (AbstractVectorIndex *)(initid->ptr);

  if (vi) {
    my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL,
                          "Not Saving index %s to disk", vi->getName().c_str());

    // vi->saveIndex(myvector_index_dir);

    my_plugin_log_message(&gplugin, MY_INFORMATION_LEVEL,
                          "Not Saving index %s to disk completed",
                          vi->getName().c_str());
  }

  return;
}

PLUGIN_EXPORT bool myvector_is_valid_init(UDF_INIT *initid, UDF_ARGS *args,
                                          char *message) {
  if (!initid || args->arg_count != 2) {
    strcpy(message, "Incorrect arguments to myvector_is_valid(), "
                    "Usage : myvector_is_valid(<vector>,<dim>)");
    return true;
  }
  return false;
}

/* myvector_is_valid : Verify vector checksum and dimension */
PLUGIN_EXPORT long long myvector_is_valid(UDF_INIT *, UDF_ARGS *args,
                                          unsigned char *, unsigned char *) {
  long long dim = *((long long *)args->args[1]);

  /* A NULL vector OR a zero-dimension vector is not valid */
  if (!args->args[0] ||
      args->lengths[0] < (MYVECTOR_COLUMN_EXTRA_LEN + sizeof(FP32))) {
    return 0;
  }

  int vdim = MyVectorDimFromStorageLength(args->lengths[0]);

  if (vdim != dim) {
    return 0;
  }

  ha_checksum cksum1;
  char *raw = args->args[0];

  /* Checksum is at the end */
  memcpy((char *)&cksum1, &(raw[args->lengths[0] - sizeof(ha_checksum)]),
         sizeof(ha_checksum));
  ha_checksum cksum2 = my_checksum(0, (const unsigned char *)raw,
                                   args->lengths[0] - sizeof(ha_checksum));
  if (cksum1 != cksum2) {
    debug_print("myvector_is_valid checksum failure (%u != %u)", cksum1,
                cksum2);
    return 0;
  }

  return 1; // success
}

PLUGIN_EXPORT void myvector_is_valid_deinit(UDF_INIT *) {}

PLUGIN_EXPORT bool myvector_row_distance_init(UDF_INIT *initid, UDF_ARGS *args,
                                              char *message) {
  if (!initid || args->arg_count != 1) {
    strcpy(message, "Incorrect arguments to myvector_row_distance(), "
                    "Usage : myvector_row_distance(idval)");
    return true;
  }
  initid->const_item = 0;
  initid->decimals = 20; /* For some reason, this is explicitly needed here */
  return false;
}

PLUGIN_EXPORT double myvector_row_distance(UDF_INIT *, UDF_ARGS *args, char *,
                                           char *) {
  double dist = 99999999999.99;
  KeyTypeInteger idval = *((KeyTypeInteger *)args->args[0]);

  if (tls_distances->size()) {
    if (tls_distances->find(idval) != tls_distances->end())
      dist = (*tls_distances)[idval];
  }

  return dist;
}
PLUGIN_EXPORT void myvector_row_distance_deinit(UDF_INIT *) {}

bool isAfter(const string &binlogfile2, const size_t binlogpos2,
             const string &binlogfile1, const size_t binlogpos1) {
  return ((binlogfile2 == binlogfile1 && binlogpos2 > binlogpos1) ||
          (binlogfile2 > binlogfile1));
}

void myvector_table_op(const string &dbname, const string &tbname,
                       const string &cname, unsigned int pkid,
                       vector<unsigned char> &vec, const string &binlogfile,
                       const size_t &binlogpos) {
  string vecid = dbname + "." + tbname + "." + cname;
  AbstractVectorIndex *vi = g_indexes.get(vecid);

  if (vi) {
    SharedLockGuard l(vi);
    string binlogfileold;
    size_t binlogposold;

    vi->getLastUpdateCoordinates(binlogfileold, binlogposold);
    if (isAfter(binlogfile, binlogpos, binlogfileold, binlogposold)) {
      vi->insertVector(vec.data(), vi->getDimension(), pkid);
    } else {
      debug_print("Skipping index update (%s %lu) < (%s %lu).",
                  binlogfile.c_str(), binlogpos, binlogfileold.c_str(),
                  binlogposold);
    }
  }
}

/* myvector_checkpoint_index() - Incrementally persist a vector index. Check
 * hnswdisk.i for implementation details. This routine is called from the
 * binlog event listener thread at every binlog file rotation. The frequency
 * can be changed in future if needed.
 */
void myvector_checkpoint_index(const string &dbtable, const string &veccol,
                               const string &binlogFile, size_t binlogPos) {
  string vecid = dbtable + "." + veccol;
  AbstractVectorIndex *vi = g_indexes.get(vecid);

  if (vi) {
    SharedLockGuard l(vi);
    string binlogfileold;
    size_t binlogposold;

    vi->getLastUpdateCoordinates(binlogfileold, binlogposold);
    debug_print("Checkpoint index %s at (%s %lu)\n", vecid.c_str(),
                binlogFile.c_str(), binlogPos);
    if (isAfter(binlogFile, binlogPos, binlogfileold, binlogposold)) {
      vi->setLastUpdateCoordinates(binlogFile, binlogPos);
      vi->saveIndex(myvector_index_dir, "checkpoint");
    }
  }
}

string myvector_find_earliest_binlog_file() {
  return g_indexes.FindEarliestBinlogFile();
}
/* end of myvector.cc */
