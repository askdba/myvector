/*
   Copyright (c) 2024 - p3io.in / shiyer22@gmail.com

   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.
*/

/* SQL access to the myvector.* tables that hold storage=innodb indexes
 * (RFC-005). All of it runs on one storage thread, in local command-service
 * sessions as the locked account 'mysql.myvector'@'localhost': a UDF call hands
 * the work to that thread and waits, instead of opening a second session from
 * inside a running user statement. */

#ifndef MYVECTOR_INNODB_STORE_H
#define MYVECTOR_INNODB_STORE_H

#include <functional>
#include <string>
#include <vector>

class IndexStore;  // include/myvector_index_store.h

namespace myvector_store {

/* One row of a result set: column values (binary safe) or nulls. */
struct Row {
    std::vector<std::string> cols;
    std::vector<bool> is_null;
};

/* A local session, valid only inside the function passed to run(). */
class Session {
public:
    virtual ~Session() = default;
    /* Run a statement that returns no rows. False and *err set on failure. */
    virtual bool exec(const std::string& sql, std::string* err) = 0;
    /* Run a query; on_row is called for each row and may return false to stop
     * reading. False and *err set on failure. */
    virtual bool query(const std::string& sql,
                       const std::function<bool(const Row&)>& on_row,
                       std::string* err) = 0;
    /* Rows changed by the last exec(). */
    virtual unsigned long long affected_rows() = 0;
};

/* Start and stop the storage thread (component init / deinit). */
bool start();
void stop();

/* Run fn on the storage thread with a freshly opened session, which is closed
 * afterwards. Returns fn's result, or false with *err set if the session could
 * not be opened. */
bool run(const std::function<bool(Session&, std::string*)>& fn, std::string* err);

/* Hex-encode bytes for an X'...' SQL literal. */
std::string hex(const char* data, size_t len);

/* Decode HEX(col) output. Binary columns are always read as HEX(col): on 8.4
 * and 9.7 the command service returns binary values that contain zero bytes
 * as empty strings. False if s is not valid hex. */
bool unhex(const std::string& s, std::string* out);

/* The store for storage=innodb indexes (registered with
 * myvector_set_index_store() at component init). */
::IndexStore* index_store();

}  // namespace myvector_store

#endif  // MYVECTOR_INNODB_STORE_H
