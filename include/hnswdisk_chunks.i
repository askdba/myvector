/* HierarchicalDiskNSW: the graph as rows for InnoDB storage (RFC-005,
 * docs/RFC-005-INNODB-INDEX-STORAGE.md sections 4.2-4.3). Included inside the
 * class, like hnswdisk.i.
 *
 * Each node's level-0 record is [level-0 links | vector | label]. It is split
 * in two so the parts that change at different rates are stored apart:
 *   - vector chunks: (vector | label) for a fixed range of node ids;
 *   - link chunks:   level-0 links for a fixed range of node ids;
 *   - upper rows:    linkLists_[i] (levels 1..L) for each node with L > 0.
 * The header is the same 96 bytes saveIndexHeader() writes to a file.
 *
 * Import is streamed, so a large index is never held twice: beginImport(), then
 * importVectorChunk()/importLinkChunk() for every chunk, endLevel0Import(),
 * importUpper() for every upper row, finishImport(). On any exception the
 * caller deletes the object; every step leaves it in a state clear() can free
 * (cur_element_count stays 0 until the per-node arrays exist). */

    size_t m_importCount{0};  ///< nodes declared by the header being imported
    size_t m_nextVector{0};   ///< next node a vector chunk must start at
    size_t m_nextLink{0};     ///< next node a link chunk must start at

    static constexpr size_t kHeaderBytes =
        6 * sizeof(size_t) + sizeof(int) + sizeof(tableint) + 3 * sizeof(size_t) +
        sizeof(double) + sizeof(size_t);

    template <typename T>
    static void putPOD(std::string &out, const T &v) {
        out.append(reinterpret_cast<const char *>(&v), sizeof(T));
    }
    template <typename T>
    static void getPOD(const std::string &in, size_t &pos, T &v) {
        memcpy(&v, in.data() + pos, sizeof(T));
        pos += sizeof(T);
    }

    /* headerBytes - the header in saveIndexHeader()'s layout. */
    std::string headerBytes() const {
        std::string h;
        size_t cur = cur_element_count;
        putPOD(h, offsetLevel0_);
        putPOD(h, max_elements_);
        putPOD(h, cur);
        putPOD(h, size_data_per_element_);
        putPOD(h, label_offset_);
        putPOD(h, offsetData_);
        putPOD(h, maxlevel_);
        putPOD(h, enterpoint_node_);
        putPOD(h, maxM_);
        putPOD(h, maxM0_);
        putPOD(h, M_);
        putPOD(h, mult_);
        putPOD(h, ef_construction_);
        return h;
    }

    size_t vectorRecordBytes() const { return size_data_per_element_ - size_links_level0_; }
    size_t linkRecordBytes() const { return size_links_level0_; }

    /* exportVectorChunk / exportLinkChunk - the (vector | label) or level-0
     * link records of nodes [first, first + count). */
    std::string exportVectorChunk(size_t first, size_t count) const {
        std::string out;
        out.reserve(count * vectorRecordBytes());
        for (size_t i = first; i < first + count; i++)
            out.append(data_level0_memory_ + i * size_data_per_element_ + offsetData_,
                       vectorRecordBytes());
        return out;
    }
    std::string exportLinkChunk(size_t first, size_t count) const {
        std::string out;
        out.reserve(count * linkRecordBytes());
        for (size_t i = first; i < first + count; i++)
            out.append(data_level0_memory_ + i * size_data_per_element_ + offsetLevel0_,
                       linkRecordBytes());
        return out;
    }

    /* upperLinksBytes - node i's links of levels 1..L, empty if L == 0. */
    std::string upperLinksBytes(size_t i) const {
        if (element_levels_[i] <= 0)
            return std::string();
        return std::string(linkLists_[i], size_links_per_element_ * element_levels_[i]);
    }

    /* beginImport - parse and validate the header (which must declare
     * expected_count nodes, the count stored next to it), allocate the level-0
     * block. */
    void beginImport(SpaceInterface<dist_t> *s, const std::string &header,
                     size_t expected_count, size_t max_elements_i) {
        clear();
        if (header.size() != kHeaderBytes)
            throw std::runtime_error(
                "Index seems to be corrupted or unsupported (header size)");
        size_t pos = 0, cur = 0;
        getPOD(header, pos, offsetLevel0_);
        getPOD(header, pos, max_elements_);
        getPOD(header, pos, cur);
        getPOD(header, pos, size_data_per_element_);
        getPOD(header, pos, label_offset_);
        getPOD(header, pos, offsetData_);
        getPOD(header, pos, maxlevel_);
        getPOD(header, pos, enterpoint_node_);
        getPOD(header, pos, maxM_);
        getPOD(header, pos, maxM0_);
        getPOD(header, pos, M_);
        getPOD(header, pos, mult_);
        getPOD(header, pos, ef_construction_);
        if (cur != expected_count)
            throw std::runtime_error(
                "Index seems to be corrupted (header node count differs from the stored count)");
        cur_element_count = cur;  // validateHeader() checks it against capacity
        try {
            validateHeader(s, max_elements_i);
        } catch (...) {
            cur_element_count = 0;  // nothing per-node allocated: keep clear() safe
            throw;
        }
        m_importCount = cur;
        m_nextVector = m_nextLink = 0;
        cur_element_count = 0;  // until endLevel0Import(): see the file comment
        data_level0_memory_ = (char *)malloc(max_elements_ * size_data_per_element_);
        if (data_level0_memory_ == nullptr)
            throw std::runtime_error("Not enough memory: import failed to allocate level0");
    }

    /* importVectorChunk / importLinkChunk - records for nodes [first, ...). */
    void importVectorChunk(size_t first, const std::string &data) {
        importRecords(first, data, vectorRecordBytes(), offsetData_, m_nextVector);
    }
    void importLinkChunk(size_t first, const std::string &data) {
        importRecords(first, data, linkRecordBytes(), offsetLevel0_, m_nextLink);
    }

    /* endLevel0Import - every vector and link chunk is in: register labels and
     * allocate the per-node arrays. */
    void endLevel0Import() {
        /* Every record written exactly once: no gap, no short chunk, so no
         * uninitialized byte of data_level0_memory_ is read as graph data. */
        if (m_nextVector != m_importCount || m_nextLink != m_importCount)
            throw std::runtime_error(
                "Index seems to be corrupted (chunks don't cover every node)");
        cur_element_count = m_importCount;
        try {
            allocateForLoad();
        } catch (...) {
            cur_element_count = 0;  // nothing per-node allocated yet
            throw;
        }
    }

    /* importUpper - node's links of levels 1..L. */
    void importUpper(size_t node, const std::string &links) {
        setUpperLinks(node, links.data(), links.size());
    }

    /* finishImport - checks that need the whole graph. */
    void finishImport() {
        finishLoad();
        validateAdjacency();
    }

    /* validateAdjacency - every neighbor list holds at most maxM0_ (level 0) or
     * maxM_ (upper levels) ids, each naming an existing node that has that
     * level. Searches read as many ids as a list's count says without checking
     * the allocation, so stored data must never get past this with a wrong
     * count (RFC-005 section 4.7). Throws std::runtime_error. */
    void validateAdjacency() const {
        for (tableint i = 0; i < cur_element_count; i++) {
            for (int level = 0; level <= element_levels_[i]; level++) {
                linklistsizeint *ll = get_linklist_at_level(i, level);
                size_t count = getListCount(ll);
                if (count > (level == 0 ? maxM0_ : maxM_))
                    throw std::runtime_error(
                        "Index seems to be corrupted (neighbor count out of range)");
                tableint *ids = (tableint *)(ll + 1);
                for (size_t j = 0; j < count; j++) {
                    if (ids[j] >= cur_element_count)
                        throw std::runtime_error(
                            "Index seems to be corrupted (neighbor id out of range)");
                    if (element_levels_[ids[j]] < level)
                        throw std::runtime_error(
                            "Index seems to be corrupted (neighbor missing that level)");
                }
            }
        }
    }

private:
    void importRecords(size_t first, const std::string &data, size_t record,
                       size_t offset, size_t &next) {
        if (record == 0 || data.empty() || data.size() % record != 0)
            throw std::runtime_error(
                "Index seems to be corrupted (chunk size not a whole number of records)");
        size_t count = data.size() / record;
        if (first != next)
            throw std::runtime_error(
                "Index seems to be corrupted (chunk doesn't start where the last ended)");
        if (first > m_importCount || count > m_importCount - first)
            throw std::runtime_error(
                "Index seems to be corrupted (chunk past the node count)");
        next = first + count;
        for (size_t k = 0; k < count; k++)
            memcpy(data_level0_memory_ + (first + k) * size_data_per_element_ + offset,
                   data.data() + k * record, record);
    }

public:
