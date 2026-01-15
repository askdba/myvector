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
#ifndef PLUGIN_MYVECTOR_H
#define PLUGIN_MYVECTOR_H

#include <string>
#include <mutex>
#include <shared_mutex>
#include <map>
#include <unordered_map>

bool myvector_query_rewrite(const std::string &query, std::string *rewritten_query);

/* MyVector only supports INT/BIGINT key type. 2 options for users :-

   1. Create the table with a INT primary key.
   OR
   2. If INT variant primary key is not possible, user can add a
      AUTO INCREMENT with UNIQUE index column.
*/

typedef size_t KeyTypeInteger;


/* v1 uses 4-byte floats for representing vector dimensions.
   Just by templatizing all references to FP32, MyVector can support
   FP16, 8-bit SQ etc in v2 :-

      Replace all references to FP32 with T.
      Add template<class T> before the functions/classes.
 */
typedef float       FP32;

typedef void*       VectorPtr;

using namespace std;

/* Interface for various types of vector indexes. Initial design is based
 * on 2 index types - 1) KNN in-memory using vector<> and priority_queue<>
 * 2) HNSW in-memory with persistence from hnswlib.
 */
class AbstractVectorIndex
{
public:
    virtual ~AbstractVectorIndex() {}

    virtual string getName() = 0;

    virtual string getType() = 0;

    virtual int         getDimension() { return 0; }

    virtual bool        supportsIncrUpdates() { return false; }

    virtual bool        supportsPersist() { return false; }

    virtual bool        supportsConcurrentUpdates() { return false; }

    virtual bool        supportsIncrRefresh() { return false; }

    virtual bool        isReady() { return false; }

    virtual bool        isDirty() { return false; }

    virtual string      getStatus() { return getName() + "<Status>"; }

    virtual bool loadIndex(const string & path)  = 0;

    virtual bool saveIndex(const string & path, const string & option = "")  = 0;
          
    virtual bool saveIndexIncr(const string & path, const string & option = "")  = 0;
          
    virtual bool dropIndex(const string & path)  = 0;

    virtual bool initIndex()                     = 0;

    virtual bool closeIndex()                    = 0;

    /* searchVectorNN - search and return 'n' Nearest Neighbours */
    virtual bool searchVectorNN(VectorPtr qvec, int dim,
                                vector<KeyTypeInteger> & nnkeys,
                                int n) = 0;

    /* insertVectortor - insert a vector into the index */
    virtual bool insertVector(VectorPtr vec, int dim, KeyTypeInteger id) = 0;

    /* startParallelBuild - User has initiated parallel index build/rebuild */
    virtual bool startParallelBuild(int nthreads) = 0;

    /* setUpdateTs - timestamp when the last index build/refresh was started */
    virtual void setUpdateTs(unsigned long ts) = 0;

    /* getUpdateTs - get timestamp when the last index build/refresh was started */
    virtual unsigned long getUpdateTs() = 0;

    /* getRowCount - get number of vectors present in the index */
    virtual unsigned long getRowCount() = 0;
          
    virtual void getLastUpdateCoordinates(string & /* file */, size_t & /* pos */) {}

    virtual void setLastUpdateCoordinates(const string & /* file */, const size_t & /* pos */) {}

    virtual void setSearchEffort(int ef_search) {} /* how much deep/wide to go? e.g ef_search in HNSW */

    void lockShared()      { m_mutex.lock_shared(); }
    void lockExclusive()   { m_mutex.lock(); }

    void unlockShared()    { m_mutex.unlock_shared(); }
    void unlockExclusive() { m_mutex.unlock(); }

    shared_mutex & mutex()  { return m_mutex; }
private:
    mutable shared_mutex m_mutex;
};

class VectorIndexCollection
{
public:
    AbstractVectorIndex* open(const string & name,
                              const string & options,
                              const string & useraction);

    AbstractVectorIndex* get(const string & name);

    bool                 close(AbstractVectorIndex * hindex);

    string               FindEarliestBinlogFile();

private:
    unordered_map<string, AbstractVectorIndex*> m_indexes;
    mutex m_mutex;
};

#define MYVECTOR_BUFF_SIZE       1024

extern long myvector_index_bg_threads;
extern long myvector_feature_level;
extern char* myvector_config_file;

#endif  // PLUGIN_MYVECTOR_H
