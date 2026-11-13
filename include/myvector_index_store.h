/*
   Copyright (c) 2024 - p3io.in / shiyer22@gmail.com

   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.
*/

/* Storage for indexes declared with storage=innodb (RFC-005,
 * docs/RFC-005-INNODB-INDEX-STORAGE.md). The index code hands the store its
 * graph through callbacks, so the store needs no HNSW internals. Only the
 * component build has a store; in the plugin build myvector_index_store()
 * returns nullptr and storage=innodb is refused. */

#ifndef MYVECTOR_INDEX_STORE_H
#define MYVECTOR_INDEX_STORE_H

#include <cstddef>
#include <functional>
#include <string>

class IndexStore {
public:
    virtual ~IndexStore() = default;

    /* What a save writes: the 96-byte graph header, the node count, the
     * size of one (vector | label) record and one level-0 link record, and
     * functions that return the records of nodes [first, first + count) and a
     * node's upper-level links (empty if it has none). */
    struct GraphSource {
        std::string header;
        size_t node_count = 0;
        size_t vector_record = 0;
        size_t link_record = 0;
        std::string metric;
        int dim = 0;
        std::function<std::string(size_t first, size_t count)> vectors;
        std::function<std::string(size_t first, size_t count)> links;
        std::function<std::string(size_t node)> upper;
    };

    /* What a load feeds, in this order: begin(header, node_count), then every
     * vector chunk and every link chunk (first = first node of the chunk),
     * end_level0(), every upper row, finish(). Any of them may throw
     * std::exception, which fails the load. */
    struct GraphSink {
        std::function<void(const std::string& header, size_t node_count)> begin;
        std::function<void(size_t first, const std::string& data)> vectors;
        std::function<void(size_t first, const std::string& data)> links;
        std::function<void()> end_level0;
        std::function<void(size_t node, const std::string& data)> upper;
        std::function<void()> finish;
    };

    enum class LoadStatus { Ok, NotSaved, Failed };

    /* "" if this server can hold storage=innodb indexes, otherwise why not
     * (GTID mode off, read_only or super_read_only on: RFC-005 D11, D13). */
    virtual std::string checkServer() = 0;

    /* Write the graph as a new generation and publish it (RFC-005 4.6). */
    virtual bool save(const std::string& name, const GraphSource& src,
                      std::string* err) = 0;

    /* Read the published generation in one consistent snapshot (4.7). The
     * stored metric and dimension must match. NotSaved: no active index. */
    virtual LoadStatus load(const std::string& name, const std::string& metric,
                            int dim, const GraphSink& sink, std::string* err) = 0;

    /* Tombstone the index, then delete its rows (4.11). */
    virtual bool drop(const std::string& name, std::string* err) = 0;

    /* True if an active saved index exists, or if that can't be told. */
    virtual bool exists(const std::string& name) = 0;
};

/* The store, or nullptr (plugin build, or the component isn't initialized). */
IndexStore* myvector_index_store();
void myvector_set_index_store(IndexStore* store);

#endif  // MYVECTOR_INDEX_STORE_H
