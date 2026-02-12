#include "myvector_udf_service.h"
#include <mysql/udf_registration_types.h>
#include <mysql/service_my_plugin_log.h>
#include <mysql/service_mysql_alloc.h>
#include <mysql/service_plugin_registry.h>

#include <algorithm>
#include <iomanip>
#include <list>
#include <memory>
#include <mutex>
#include <queue>
#include <regex>
#include <set>
#include <shared_mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "hnswdisk.h"
#include "hnswlib.h"
#include "my_checksum.h"
#include "myvector.h"
#include "myvector_errors.h"
#include "myvectorutils.h"

// Forward declarations for functions/variables used by UDFs
extern char* myvector_index_dir;
extern long myvector_feature_level;

extern const std::set<std::string> MYVECTOR_INDEX_TYPES;
extern std::thread_local std::unordered_map<KeyTypeInteger, double> tls_distances_map;
extern std::thread_local std::unordered_map<KeyTypeInteger, double>* tls_distances;

extern int MyVectorStorageLength(int dim);
extern int MyVectorDimFromStorageLength(int length);
extern int MyVectorBVStorageLength(int dim);
extern int MyVectorBVDimFromStorageLength(int length);

extern double computeL2Distance(const FP32* __restrict v1, const FP32* __restrict v2, int dim);
extern double computeIPDistance(const FP32* __restrict v1, const FP32* __restrict v2, int dim);
extern double computeCosineDistance(const FP32* __restrict v1, const FP32* __restrict v2, int dim);
extern float computeCosineDistanceFn(const void* __restrict v1, const void* __restrict v2, const void* __restrict qty_ptr);
extern float HammingDistanceFn(const void* __restrict pVect1, const void* __restrict pVect2, const void* __restrict qty_ptr);

extern const unsigned int MYVECTOR_VERSION_V1;
extern const unsigned int MYVECTOR_VECTOR_FP32;
extern const unsigned int MYVECTOR_VECTOR_FP16;
extern const unsigned int MYVECTOR_VECTOR_BV;
#define MYVECTOR_V1_FP32_METADATA ((MYVECTOR_VECTOR_FP32 << 8) | MYVECTOR_VERSION_V1)
#define MYVECTOR_V1_BV_METADATA ((MYVECTOR_VECTOR_BV << 8) | MYVECTOR_VERSION_V1)

extern const unsigned int MYVECTOR_CONSTRUCT_MAX_LEN;
extern const unsigned int MYVECTOR_DISPLAY_MAX_LEN;
extern const unsigned int MYVECTOR_DISPLAY_DEF_PREC;
extern const unsigned int MYVECTOR_COLUMN_EXTRA_LEN;
extern const unsigned int MYVECTOR_DEFAULT_ANN_RETURN_COUNT;
extern const unsigned int MYVECTOR_MAX_ANN_RETURN_COUNT;

extern char* latin1;

// AbstractVectorIndex, VectorIndexCollection and related globals
extern VectorIndexCollection g_indexes;

// Placeholder for error logging - to be replaced by component-specific logging
#define UDF_ERROR_LOG(...) do { /* fprintf(stderr, __VA_ARGS__); */ } while(0)

#define SET_UDF_ERROR_AND_RETURN(...) \
    do { \
        UDF_ERROR_LOG(__VA_ARGS__); \
        *error = 1; \
        return (result); \
    } while (0)

/** On success returns false and sets initid->ptr. On allocation failure sets message and returns true. */
static bool myvector_alloc_init_ptr(UDF_INIT* initid, size_t size, char* message) {
    void* p = malloc(size);
    if (!p) {
        snprintf(message, MYSQL_ERRMSG_SIZE, "myvector UDF init: allocation failed");
        return true;  // failure
    }
    initid->ptr = (char*)p;
    return false;  // success
}

namespace myvector_component {

// UDF: myvector_ann_set
bool myvector_ann_set_init(UDF_INIT* initid, UDF_ARGS* args, char* message) {
    initid->ptr = nullptr;
    if (args->arg_count < 3 || args->arg_count > 4) {
        strcpy(message, ER_MYVECTOR_INCORRECT_ARGUMENTS);
        return true;  // error
    }

    char* col = (char*)args->args[0];
    AbstractVectorIndex* vi = g_indexes.get(col);
    if (!vi) {
        snprintf(message, MYSQL_ERRMSG_SIZE, ER_MYVECTOR_INDEX_NOT_FOUND, col);
        return true;  // error
    }
    SharedLockGuard l(vi);

    initid->max_length = MYVECTOR_DISPLAY_MAX_LEN;
    if (myvector_alloc_init_ptr(initid, initid->max_length, message))
        return true;
    // (*h_udf_metadata_service)->result_set(initid, "charset", latin1); // This requires h_udf_metadata_service

    tls_distances->clear();

    return false;
}

void myvector_ann_set_deinit(UDF_INIT* initid) {
    if (initid && initid->ptr)
        free(initid->ptr);
    tls_distances->clear();
}

char* myvector_ann_set(UDF_INIT* initid,
                                     UDF_ARGS* args,
                                     char* result,
                                     unsigned long* length,
                                     unsigned char* is_null,
                                     unsigned char* error) {
    char* col = (char*)args->args[0];
    char* idcol = (char*)args->args[1];
    FP32* searchvec = (FP32*)args->args[2];
    const char* searchoptions = nullptr;

    if (!col || !idcol || !searchvec) {
        *error = 1;
        *is_null = 1;
        return initid->ptr;
    }

    if (args->arg_count == 4)
        searchoptions = (char*)args->args[3];

    int nn = MYVECTOR_DEFAULT_ANN_RETURN_COUNT;
    int ef_search = 0;
    if (args->arg_count >= 4 && searchoptions && args->lengths[3]) {
        MyVectorOptions vo(searchoptions);

        nn = vo.getIntOption("nn", MYVECTOR_DEFAULT_ANN_RETURN_COUNT);
        if (nn <= 0)
            nn = MYVECTOR_DEFAULT_ANN_RETURN_COUNT;

        nn = std::min((const unsigned int)nn, MYVECTOR_MAX_ANN_RETURN_COUNT);

        ef_search = vo.getIntOption("ef_search", 0);
    }

    AbstractVectorIndex* vi = g_indexes.get(col);
    SharedLockGuard l(vi);

    std::stringstream ss;
    if (vi && searchvec) {
        std::vector<KeyTypeInteger> result_keys;
        if (ef_search)
            vi->setSearchEffort(ef_search);
        vi->searchVectorNN(searchvec, vi->getDimension(), result_keys, nn);

        ss << "[";
        for (size_t i = 0; i < result_keys.size(); i++) {
            if (i)
                ss << ",";
            ss << result_keys[i];
        }
        ss << "]";
    } else {
        *is_null = 1;
        *error = 1;
        ss << "[NULL]";
    }

    size_t out_len = ss.str().size();
    size_t max_copy = (initid->max_length > 0) ? (size_t)(initid->max_length - 1) : 0;
    if (out_len > max_copy)
        out_len = max_copy;
    result = (char*)initid->ptr;
    if (out_len > 0)
        memcpy(result, ss.str().c_str(), out_len);
    result[out_len] = '\0';
    *length = out_len;
    return result;
}

// UDF: myvector_construct_bv
int SQFloatVectorToBinaryVector(FP32* fvec, unsigned long* ivec, int dim) {
    memset(ivec, 0, (dim / BITS_PER_BYTE));  // 3rd param is bytes

    unsigned long elem = 0;
    unsigned long idx = 0;

    for (int i = 0; i < dim; i++) {
        elem = elem << 1;
        if (fvec[i] > 0) {
            elem = elem | 1;
        }

        if (((i + 1) % 64) == 0) {  // 8 bytes packed (i.e 64 dims in 1 ulong)
            ivec[idx] = elem;
            elem = 0;
            idx++;
        }
    }
    /// TODO : if dim is not a mulitple of 64
    return (idx * sizeof(unsigned long));  // number of bytes
}

char* myvector_construct_bv(const std::string& srctype,
                            char* src,
                            char* dst,
                            unsigned long srclen,
                            unsigned long* length,
                            unsigned char* is_null,
                            unsigned char* error) {
    int retlen = 0;

    if (srctype == "bv") {
        memcpy(
            dst, src, srclen);  // src is bytes representing the binary vector
        retlen = srclen;
    } else if (srctype == "float") {
        int dim = MyVectorDimFromStorageLength(srclen);
        retlen =
            SQFloatVectorToBinaryVector((FP32*)src, (unsigned long*)dst, dim);
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
            char* p1 = ptr;
            while (*ptr != ' ' && *ptr != ',' && *ptr != endch)
                ptr++;
            char buff[64];
            size_t len = (size_t)(ptr - p1);
            if (len >= sizeof(buff))
                len = sizeof(buff) - 1;
            memcpy(buff, p1, len);
            buff[len] = '\0';

            dst[retlen] = (unsigned char)(atoi(buff));

            retlen += sizeof(unsigned char);
        }  // while
    }  // else

#if MYSQL_VERSION_ID < 90000
    unsigned int metadata = MYVECTOR_V1_BV_METADATA;
    memcpy(&dst[retlen], &metadata, sizeof(metadata));
    retlen += sizeof(metadata);

    ha_checksum cksum = my_checksum(0, (const unsigned char*)dst, retlen);
    memcpy(&dst[retlen], &cksum, sizeof(cksum));
    retlen += sizeof(cksum);
#endif
    *length = retlen;
    return dst;
}

// UDF: myvector_construct
bool myvector_construct_init(UDF_INIT* initid, UDF_ARGS* args, char* message) {
    if (args->arg_count < 1 || args->arg_count > 2) {
        strcpy(message, ER_MYVECTOR_INCORRECT_ARGUMENTS);
        return true;  // error
    }
    initid->max_length = MYVECTOR_CONSTRUCT_MAX_LEN;
    return myvector_alloc_init_ptr(initid, MYVECTOR_CONSTRUCT_MAX_LEN, message);
}

char* myvector_construct(UDF_INIT* initid,
                                       UDF_ARGS* args,
                                       char* result,
                                       unsigned long* length,
                                       unsigned char* is_null,
                                       unsigned char* error) {
    char* ptr = (char*)args->args[0];
    const char* opt = nullptr;
    if (args->arg_count == 2)
        opt = (char*)args->args[1];

    char* start = nullptr;
    char endch;
    char* retvec = (char*)initid->ptr;
    int retlen = 0;
    bool skipConvert = false;

    if (!opt || !args->lengths[1])
        opt = "i=string,o=float";  // i=string,o=float
    else {
        MyVectorOptions vo(opt);

        if (vo.getOption("i") == "float" && vo.getOption("o") == "float")
            skipConvert = true;

        if (vo.getOption("o") == "bv")
            return myvector_construct_bv(vo.getOption("i"),
                                         ptr,
                                         (char*)initid->ptr,
                                         args->lengths[0],
                                         length,
                                         is_null,
                                         error);
    }  // else opt

    if (skipConvert) {
        if ((args->lengths[0] % sizeof(FP32)) != 0)
            SET_UDF_ERROR_AND_RETURN(
                "Input vector is malformed, length not a "
                "multiple of sizeof(float) %lu.",
                args->lengths[0]);
        memcpy(retvec, ptr, args->lengths[0]);
        retlen = args->lengths[0];
        goto addChecksum;
    }

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
        char* p1 = ptr;
        while (*ptr != ' ' && *ptr != ',' && *ptr != endch)
            ptr++;
        char buff[64];
        size_t len = (size_t)(ptr - p1);
        if (len >= sizeof(buff))
            len = sizeof(buff) - 1;
        memcpy(buff, p1, len);
        buff[len] = '\0';

        FP32 fval = atof(buff);
        memcpy(&retvec[retlen], &fval, sizeof(FP32));

        retlen += sizeof(FP32);
    }  // while

addChecksum:
#if MYSQL_VERSION_ID < 90000
    unsigned int metadata = MYVECTOR_V1_FP32_METADATA;
    memcpy(&retvec[retlen], &metadata, sizeof(metadata));
    retlen += sizeof(metadata);

    ha_checksum cksum = my_checksum(0, (const unsigned char*)retvec, retlen);
    memcpy(&retvec[retlen], &cksum, sizeof(cksum));
    retlen += sizeof(cksum);
#endif
    *length = retlen;

    return retvec;
}

void myvector_construct_deinit(UDF_INIT* initid) {
    if (initid && initid->ptr)
        free(initid->ptr);
}

// UDF: myvector_display
bool myvector_display_init(UDF_INIT* initid, UDF_ARGS* args, char* message) {
    if (args->arg_count == 0 || args->arg_count > 2) {
        strcpy(message, ER_MYVECTOR_INCORRECT_ARGUMENTS);
        return true;  // error
    }
    initid->max_length = MYVECTOR_DISPLAY_MAX_LEN;
    if (myvector_alloc_init_ptr(initid, MYVECTOR_DISPLAY_MAX_LEN, message))
        return true;
    return false;
}

char* myvector_display(UDF_INIT* initid,
                                     UDF_ARGS* args,
                                     char* result,
                                     unsigned long* length,
                                     char* is_null,
                                     char* error) {
    unsigned char* bvec = (unsigned char*)args->args[0];
    FP32* fvec = (FP32*)args->args[0];
    if (!bvec || !args->lengths[0]) {
        *is_null = 1;
        *error = 1;
        return result;
    }

    int precision = 0;
    if (args->arg_count > 1 && args->args[1] && args->lengths[1]) {
        precision = atoi((char*)args->args[1]);
    }
    if (!precision)
        precision = MYVECTOR_DISPLAY_DEF_PREC;

    std::stringstream ostr;

#if MYSQL_VERSION_ID < 90000
    ha_checksum cksum1;
    char* raw = (char*)args->args[0];
    memcpy((char*)&cksum1,
           &(raw[args->lengths[0] - sizeof(ha_checksum)]),
           sizeof(ha_checksum));
    ha_checksum cksum2 = my_checksum(
        0, (const unsigned char*)raw, args->lengths[0] - sizeof(ha_checksum));
    if (cksum1 != cksum2) {
        *error = 1;
        *is_null = 1;
        *length = 0;
        return nullptr;  /* Caller treats NULL with *is_null/ *error; no static string lifetime */
    }
#endif

    int dim = 0;
#if MYSQL_VERSION_ID < 90000
    unsigned int metadata = 0;
    memcpy((char*)&metadata,
           &bvec[args->lengths[0] - MYVECTOR_COLUMN_EXTRA_LEN],
           sizeof(metadata));

    if (metadata == MYVECTOR_V1_FP32_METADATA) {
        bvec = nullptr;
        dim = MyVectorDimFromStorageLength(args->lengths[0]);
    } else if (metadata == MYVECTOR_V1_BV_METADATA) {
        fvec = nullptr;
        dim = MyVectorBVDimFromStorageLength(args->lengths[0]);
        dim = dim / 8; /* bit-packet */
    } else {           /* 'old' v0 vectors */
        bvec = nullptr;
        dim = (args->lengths[0]) / sizeof(FP32);
    }
#else
    dim = MyVectorDimFromStorageLength(args->lengths[0]);
#endif

    ostr << "[";
    ostr << std::setprecision(precision);
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

    result = (char*)initid->ptr;
    size_t out_len = ostr.str().length();
    size_t max_copy = MYVECTOR_DISPLAY_MAX_LEN - 1;
    if (out_len > max_copy)
        out_len = max_copy;
    if (out_len > 0)
        memcpy(result, ostr.str().c_str(), out_len);
    result[out_len] = '\0';
    *length = out_len;

    return result;
}

void myvector_display_deinit(UDF_INIT* initid) {
    if (initid && initid->ptr)
        free(initid->ptr);
}

// UDF: myvector_distance
bool myvector_distance_init(UDF_INIT* initid,
                                          UDF_ARGS* args,
                                          char* message) {
    if (args->arg_count < 2 || args->arg_count > 3) {
        strcpy(message, ER_MYVECTOR_INCORRECT_ARGUMENTS);
        return true;  /// error
    }
    return false;
}

double myvector_distance(UDF_INIT* initid,
                                        UDF_ARGS* args,
                                        char* is_null,
                                        char* error) {
    unsigned char* v1_raw = (unsigned char*)args->args[0];
    unsigned long l1 = args->lengths[0];
    unsigned char* v2_raw = (unsigned char*)args->args[1];
    unsigned long l2 = args->lengths[1];

    if (!v1_raw || !l1 || !v2_raw || !l2) {
        *is_null = 1;
        return 0.0;
    }

    int dim1 = MyVectorDimFromStorageLength(l1);
    int dim2 = MyVectorDimFromStorageLength(l2);

    if (dim1 != dim2 || dim1 == 0) {
        *is_null = 1;
        return 0.0;
    }

    FP32* v1 = (FP32*)(v1_raw);
    FP32* v2 = (FP32*)(v2_raw);

#if MYSQL_VERSION_ID < 90000
    v1 = (FP32*)(v1_raw);
    v2 = (FP32*)(v2_raw);
#endif

    // Choose distance by optional third argument (default L2)
    double (*distfn)(const FP32*, const FP32*, int) = computeL2Distance;
    if (args->arg_count >= 3 && args->args[2] && args->lengths[2] > 0) {
        std::string metric(args->args[2], args->lengths[2]);
        std::transform(metric.begin(), metric.end(), metric.begin(), ::tolower);
        if (metric == "l2" || metric == "euclidean")
            distfn = computeL2Distance;
        else if (metric == "cosine")
            distfn = computeCosineDistance;
        else if (metric == "ip")
            distfn = computeIPDistance;
        else {
            *is_null = 1;
            return 0.0;
        }
    }

    double distance = distfn(v1, v2, dim1);
    return distance;
}

void myvector_distance_deinit(UDF_INIT* initid) {
    // No memory to free for myvector_distance
}

// UDF: myvector_construct_binaryvector (deprecated/renamed to myvector_construct with "o=bv")
bool myvector_construct_binaryvector_init(UDF_INIT* initid,
                                                        UDF_ARGS* args,
                                                        char* message) {
    if (args->arg_count != 1) {
        strcpy(message,
               "Incorrect arguments, usage : "
               "myvector_construct_binary_vector(vec_col_expr)");
        return true;  // error
    }
    initid->max_length = MYVECTOR_CONSTRUCT_MAX_LEN;  // Use general construct max len
    return myvector_alloc_init_ptr(initid, initid->max_length, message);
}

char* myvector_construct_binaryvector(UDF_INIT* initid,
                                                    UDF_ARGS* args,
                                                    char* result,
                                                    unsigned long* length,
                                                    unsigned char* is_null,
                                                    unsigned char* error) {
    if (!args->args[0]) {
        *is_null = 1;
        *error = 0;
        return nullptr;
    }
    char* ptr = (char*)args->args[0];
    std::string srctype = "string";  // Default input type for simplicity
    unsigned long arg_len = args->lengths[0];
    return myvector_construct_bv(srctype,
                                 ptr,
                                 (char*)initid->ptr,
                                 arg_len,
                                 length,
                                 is_null,
                                 error);
}

void myvector_construct_binaryvector_deinit(UDF_INIT* initid) {
    if (initid && initid->ptr)
        free(initid->ptr);
}

// UDF: myvector_hamming_distance
bool myvector_hamming_distance_init(UDF_INIT* initid,
                                                  UDF_ARGS* args,
                                                  char* message) {
    if (args->arg_count != 2) {
        strcpy(message, ER_MYVECTOR_INCORRECT_ARGUMENTS);
        return true; // error
    }
    return false;
}

double myvector_hamming_distance(UDF_INIT* initid,
                                               UDF_ARGS* args,
                                               char* is_null,
                                               char* error) {
    unsigned char* v1_raw = (unsigned char*)args->args[0];
    unsigned long l1 = args->lengths[0];
    unsigned char* v2_raw = (unsigned char*)args->args[1];
    unsigned long l2 = args->lengths[1];

    if (!v1_raw || !l1 || !v2_raw || !l2) {
        *is_null = 1;
        return 0.0;
    }

    int dim1_bits = MyVectorBVDimFromStorageLength(l1);
    int dim2_bits = MyVectorBVDimFromStorageLength(l2);

    if (dim1_bits != dim2_bits || dim1_bits == 0) {
        *is_null = 1;
        return 0.0;
    }

    // HammingDistanceFn expects qty in bits (it divides by sizeof(unsigned long)*8 for loop count)
    size_t qty_bits = static_cast<size_t>(dim1_bits);
    double distance = HammingDistanceFn(v1_raw, v2_raw, (void*)&qty_bits);
    return distance;
}

void myvector_hamming_distance_deinit(UDF_INIT* initid) {
    // No memory to free for myvector_hamming_distance
}


class MyVectorUdfServiceImpl : public MyVectorUdfService {
public:
    int register_udfs(SERVICE_TYPE(mysql_udf_metadata)* udf_metadata_service) override {
        int ret = 0;

        // Register myvector_ann_set
        ret |= udf_metadata_service->register_udf(
            "myvector_ann_set", (mysql_udf_init_fn)myvector_ann_set_init, (mysql_udf_fn)myvector_ann_set, (mysql_udf_deinit_fn)myvector_ann_set_deinit,
            (int)UDF_RETURNS_STRING, (int)UDF_ARGS_STRING, 3, 4, 0, 0);

        // Register myvector_construct
        ret |= udf_metadata_service->register_udf(
            "myvector_construct", (mysql_udf_init_fn)myvector_construct_init, (mysql_udf_fn)myvector_construct, (mysql_udf_deinit_fn)myvector_construct_deinit,
            (int)UDF_RETURNS_STRING, (int)UDF_ARGS_STRING, 1, 2, 0, 0);

        // Register myvector_display
        ret |= udf_metadata_service->register_udf(
            "myvector_display", (mysql_udf_init_fn)myvector_display_init, (mysql_udf_fn)myvector_display, (mysql_udf_deinit_fn)myvector_display_deinit,
            (int)UDF_RETURNS_STRING, (int)UDF_ARGS_STRING, 1, 2, 0, 0);

        // Register myvector_distance
        ret |= udf_metadata_service->register_udf(
            "myvector_distance", (mysql_udf_init_fn)myvector_distance_init, (mysql_udf_fn)myvector_distance, (mysql_udf_deinit_fn)myvector_distance_deinit,
            (int)UDF_RETURNS_REAL, (int)UDF_ARGS_STRING, 2, 3, 0, 0);

        // Register myvector_construct_binaryvector
        ret |= udf_metadata_service->register_udf(
            "myvector_construct_binaryvector", (mysql_udf_init_fn)myvector_construct_binaryvector_init, (mysql_udf_fn)myvector_construct_binaryvector, (mysql_udf_deinit_fn)myvector_construct_binaryvector_deinit,
            (int)UDF_RETURNS_STRING, (int)UDF_ARGS_STRING, 1, 1, 0, 0);

        // Register myvector_hamming_distance
        ret |= udf_metadata_service->register_udf(
            "myvector_hamming_distance", (mysql_udf_init_fn)myvector_hamming_distance_init, (mysql_udf_fn)myvector_hamming_distance, (mysql_udf_deinit_fn)myvector_hamming_distance_deinit,
            (int)UDF_RETURNS_REAL, (int)UDF_ARGS_STRING, 2, 2, 0, 0);

        return ret;
    }

    int deregister_udfs(SERVICE_TYPE(mysql_udf_metadata)* udf_metadata_service) override {
        int ret = 0;

        ret |= udf_metadata_service->deregister_udf("myvector_ann_set");
        ret |= udf_metadata_service->deregister_udf("myvector_construct");
        ret |= udf_metadata_service->deregister_udf("myvector_display");
        ret |= udf_metadata_service->deregister_udf("myvector_distance");
        ret |= udf_metadata_service->deregister_udf("myvector_construct_binaryvector");
        ret |= udf_metadata_service->deregister_udf("myvector_hamming_distance");

        return ret;
    }
};

MyVectorUdfServiceImpl s_udf_service;

SERVICE_REGISTRATION(myvector_udf_service, &s_udf_service);

} // namespace myvector_component
