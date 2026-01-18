/* hnswdisk.i - This file is included in hnswdisk.h inside the 
 * class HierarchicalDiskNSW. All the following code is thus part of
 * class HierarchicalDiskNSW. The code in this file implements HNSW
 * incremental disk persistence and crash recovery for MyVector.
 */

/* Flush list is partitioned/sharded to avoid mutex contention during
 * high volume, parallel insert.
 */
#define FLUSH_LIST_PARTS                             32
    
    void Write(int fd, void * buf, size_t nbytes,
               const std::string & file, int line)
    {
        ssize_t rc = write(fd, buf, nbytes);
        if (rc == -1 || rc != nbytes)
        {
            std::stringstream ss;
            ss << "Error writing " << nbytes << " bytes to " << file
               << " at line " << line << ",rc = " << rc << ",errno = " << errno;
            throw std::runtime_error(ss.str());
        }
    }

    void Fsync(int fd, const std::string & file)
    {
        int rc = fsync(fd);
        if (rc != 0)
        {
            std::stringstream ss;
            ss << "Error during fsync() on " <<  file 
               << ",fd = " << fd << ",rc = " << rc << ",errno = " << errno;
            throw std::runtime_error(ss.str());
        }
    }

    void Close(int fd, const std::string & file)
    {
        int rc = close(fd);
        if (rc != 0)
        {
            std::stringstream ss;
            ss << "Error during close() on " <<  file 
               << ",fd = " << fd << ",rc = " << rc << ",errno = " << errno;
            throw std::runtime_error(ss.str());
        }
    }

    off_t Lseek(int fd,  off_t offset, int whence, const std::string & file)
    { 
        off_t rc = lseek(fd, offset, whence);
        if (rc == (off_t)-1)
        {
            std::stringstream ss;
            ss << "Error during lseek() on " <<  file 
               << ",fd = " << fd << ",offset = " << offset << ",errno = " << errno;
            throw std::runtime_error(ss.str());
        }
        return rc;
    }

    int Open(const std::string & filepath, int flags, mode_t mode=0)
    {
        int fd = open(filepath.c_str(), flags, mode);
        if (fd == -1)
        {
            std::stringstream ss;
            ss << "Error during open() on " << filepath
               << ",fd = " << fd << ",flags= " << flags << ",mode=" << mode
               << ",errno="  << errno;
            throw std::runtime_error(ss.str());
        }
        return fd;
    }


    /* A node in the HNSW graph contains the vector and links.
     * We distinguish between :-
     * Full Node flush - New or Updated node, where vector also needs to be
     * flushed to disk -> m_nodeUpdates[]
     *
     * Node Links Level0 flush - Only the links (or edges list) has to tbe flushed
     * to disk (because the node's links got updated due to another vector insert).
     *
     * Node Links Level > 0 flush - A very low number of nodes will have level1,
     * level2, level3 ... links updated.
     */
    void addNodeToFlushList(tableint id) 
    {
        unsigned int index = rand() % FLUSH_LIST_PARTS;
        std::unique_lock <std::mutex> locklist(m_flushListMutex[index]);
        m_nodeUpdates[index].insert(id);
    }
    void addNodeLinksLevel0ToFlushList(tableint id) 
    {
        unsigned int index = rand() % FLUSH_LIST_PARTS;
        std::unique_lock <std::mutex> locklist(m_flushListMutex[index]);
        m_nodeLinksLevel0Updates[index].insert(id);
    }
    void addNodeLinksLevel0ToFlushList(std::set<tableint> & ids) 
    {
        unsigned int index = rand() % FLUSH_LIST_PARTS;
        std::unique_lock <std::mutex> locklist(m_flushListMutex[index]);
        for (auto id : ids)
          m_nodeLinksLevel0Updates[index].insert(id);
    }
    void addNodeLinksLevelGt0ToFlushList(tableint id, int level)
    {
        unsigned int index = rand() % FLUSH_LIST_PARTS;
        std::unique_lock <std::mutex> locklist(m_flushListMutex[index]);
        m_nodeLinksLevelGt0Updates[index].insert(id);
    }
    void addNodeLinksLevelGt0ToFlushList(std::set<tableint> & ids, int level)
    {
        unsigned int index = rand() % FLUSH_LIST_PARTS;
        std::unique_lock <std::mutex> locklist(m_flushListMutex[index]);
        for (auto id : ids)
            m_nodeLinksLevelGt0Updates[index].insert(id);
    }

    /* clearFlushList() - clear the "dirty" nodes lists after a checkpoint.*/
    void clearFlushList()
    {
        for (int i = 0; i < FLUSH_LIST_PARTS; i++)
        {
            std::unique_lock <std::mutex> locklist(m_flushListMutex[i]);
            m_nodeUpdates[i].clear();
            m_nodeLinksLevel0Updates[i].clear();
            m_nodeLinksLevelGt0Updates[i].clear();
        }
    }

    typedef enum
    {
        CKPT_BEGIN_INCR_PASS1  = 10001,
        CKPT_END_INCR_PASS1    = 10002,
        CKPT_BEGIN_INCR_PASS2  = 10003,
        CKPT_END_INCR_PASS2    = 10004,
        CKPT_BEGIN_FULL_WRITE  = 10005,
        CKPT_END_FULL_WRITE    = 10006,
        CKPT_CONSISTENT        = 11000
    } CheckPointState;

    void CreateStatusFile(const std::string & hnswFile)
    {
        std::string statusFileName = hnswFile + ".status";
        char buf[512];

        int ckptf = Open(statusFileName.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
        memset(buf, '.', sizeof(buf));
        Write(ckptf, buf, sizeof(buf), statusFileName, __LINE__);
        Fsync(ckptf, statusFileName);
        Close(ckptf, statusFileName);
    }

    /* MoveBackCheckPointStatus() - Replace an incompleted checkpoint record
     * with the earlier consistent checkpoint record. This routine is called
     * at end of successful recovery.
     */
    void MoveBackCheckPointStatus(const std::string & hnswFile)
    {
        std::string statusFileName = hnswFile + ".status";
        char buf1[256], buf2[256];

        int stfd = Open(statusFileName.c_str(), O_RDWR);
        read(stfd, buf1, sizeof(buf1));
        read(stfd, buf2, sizeof(buf2));
        Close(stfd, statusFileName);

        char *p = strchr(buf1, '|');
        *p = 0;
        string logr1(buf1);

        p = strchr(buf2, '|');
        *p = 0;
        string logr2(buf2);

        MyVectorOptions vo1(logr1);
        string status1 = vo1.getOption("status");
        string ckptid1 = vo1.getOption("ckptid");
        CheckPointState cs1 = static_cast<CheckPointState>(atoi(status1.c_str()));

        MyVectorOptions vo2(logr2);
        string status2 = vo2.getOption("status");
        string ckptid2 = vo2.getOption("ckptid");
        CheckPointState cs2 = static_cast<CheckPointState>(atoi(status2.c_str()));

        if (cs1 == CKPT_CONSISTENT || cs2 != CKPT_CONSISTENT)
        {
            /// TODO
            throw std::runtime_error("Internal error #1 in MoveBackCheckPointStatus.");
        }

        setCheckPointId(ckptid2);
        setCheckPointComplete(hnswFile);
    }

    void makeIndexConsistent(const std::string & hnswFile, bool & consistent,
                             size_t & outts)
    {
        /// ckptid=Checkpoint:binlog:binlog.000516:6761
        std::string statusFileName = hnswFile + ".status";
        char buf1[256], buf2[256];

        consistent = false;
        outts = 0;

        int stfd = Open(statusFileName.c_str(), O_RDWR);
        read(stfd, buf1, sizeof(buf1));
        read(stfd, buf2, sizeof(buf2));
        Close(stfd, statusFileName);

        char *p = strchr(buf1, '|');
        *p = 0;
        string logr1(buf1);
        // split the status line into kv
        MyVectorOptions vo(logr1);
        string status = vo.getOption("status");
        string ckptid = vo.getOption("ckptid");
        debug_print("makeIndexConsistent %s, current checkpoint status : %s %s",
                    hnswFile.c_str(), status.c_str(), ckptid.c_str());

        CheckPointState cs = static_cast<CheckPointState>(atoi(status.c_str()));

        if (cs == CKPT_CONSISTENT)
        {
            info_print("HNSW index %s is consistent.", hnswFile.c_str());
            setCheckPointId(ckptid);
            consistent = true;
        }

      // Check if it was a interrupted full-write, recovery is not possible.
      if (cs == CKPT_BEGIN_FULL_WRITE) {
        error_print("HNSW Index %s is not succesfully saved to disk,"
                    "this index needs to be rebuilt and saved.",
                    hnswFile.c_str());
        deleteIndexFiles(hnswFile);
        consistent = false;
        setCheckPointId(); // reset
      }

      if (cs == CKPT_BEGIN_INCR_PASS1 || cs == CKPT_END_INCR_PASS1) {
        // Recovery not needed, just delete the temp file
        warning_print("Flush of HNSW index %s was interrupted, rolling back "
                      "writes to open the index.", hnswFile.c_str());
        std::string filename = hnswFile + ".ckpt.state";
        unlink(filename.c_str());
        MoveBackCheckPointStatus(hnswFile);
        consistent = true;
      }

      // Needs recovery.
      if (cs == CKPT_BEGIN_INCR_PASS2) {
        warning_print("Flush of HNSW index %s was interrupted, crash "
                      "recovery is required.", hnswFile.c_str());
        setCheckPointId(ckptid);
        doRecovery(hnswFile);
        consistent = true;
      }

      if (cs == CKPT_END_INCR_PASS2 || cs == CKPT_END_FULL_WRITE) {
        warning_print("Flush of HNSW index %s had completed, recovery "
                      "not required.", hnswFile.c_str());
        setCheckPointId(ckptid);
        setCheckPointComplete(hnswFile);
        consistent = true;
      }

      return;
      
    } // makeIndexConsistent()

    void deleteIndexFiles(const std::string &hnswFile) {
      warning_print("Deleting all files of HNSW index %s.", hnswFile.c_str());
      std::string filename = hnswFile;
      unlink(filename.c_str());
      filename = hnswFile + ".status";
      unlink(filename.c_str());
      filename = hnswFile + ".links";
      unlink(filename.c_str());
      filename = hnswFile + ".links.data";
      unlink(filename.c_str());
      filename = hnswFile + ".ckpt.state";
      unlink(filename.c_str());
    }

    void WriteCheckPointStatus(const std::string &hnswFile,
                               CheckPointState status)
      {
        std::string statusFileName = hnswFile + ".status";
        char buf[512];
        int ckptf = -1;

        memset(buf, '.', sizeof(buf));
        try {
          ckptf = Open(statusFileName.c_str(), O_RDWR);
        }
        catch (...) {
          CreateStatusFile(hnswFile);
          ckptf = Open(statusFileName.c_str(), O_RDWR);
        }

        read(ckptf, buf, sizeof(buf));

        if (status == CKPT_BEGIN_INCR_PASS1 ||
            status == CKPT_BEGIN_FULL_WRITE) {
          memset(&buf[256], '.', 256);
          memcpy(&buf[256], &buf[0], 256); // shift and stash the current ckpt
        }

        struct timeval tv;
        gettimeofday(&tv, nullptr);
        char *ts = ctime((const time_t *)&tv.tv_sec);
        std::string tstr(ts, strlen(ts) - 1);

        std::stringstream ss;
        ss << "time=" << tstr  << ",ts=" << tv.tv_sec << ":" << tv.tv_usec
           << ",ckptid=" << getCheckPointId() << ",status=" << status << "|";

        memset(&buf[0], '.', 256);
        memcpy(buf, ss.str().c_str(), ss.str().length());

        warning_print("CheckPoint line = %s", ss.str().c_str());

        Lseek(ckptf, 0, SEEK_SET, statusFileName);

        Write(ckptf, buf, sizeof(buf), statusFileName, __LINE__);

        Fsync(ckptf, statusFileName);
        Close(ckptf, statusFileName);

      }

    const unsigned int FLUSH_OP_FULL_NODE         = 1;
    const unsigned int FLUSH_OP_LEVEL0_LINKS      = 2;
    const unsigned int FLUSH_OP_LEVEL_GT_0_LINKS  = 3;
    const unsigned int HNSW_FILE_METADATA_SIZE    = 96;


    std::mutex                                m_flushListMutex[FLUSH_LIST_PARTS];
    std::set<tableint>                        m_nodeUpdates[FLUSH_LIST_PARTS];
    std::set<tableint>                        m_nodeLinksLevel0Updates[FLUSH_LIST_PARTS];
    std::set<tableint>                        m_nodeLinksLevelGt0Updates[FLUSH_LIST_PARTS];
    std::unordered_map<tableint, size_t>      m_linksOffsetsInFile;
    std::string                               m_checkPointId;

    void doCheckPoint(const std::string &hnswFileName)
    {
   /* 
    * 0. CreateOrOpenFile(m_name.hnsw.ckpt);
    * 1. Write(ckptString_Step1) to file
    * 2. fsync(file);
    * 3. CreateFile(m_name,hnsw.ckpt.state);
    * 4. Write all updatedNodes and all updatedLinksNodes to file - 10MB
    * 5. fsync(file)
    * 6. Write(ckptString_Step1_Complete) to ckpt file
    * 7. fsync(ckpt_file);
    * 8. Write all updatedNodes/updatedLinksNodes to real hnsw file in-place
    * 9. fsync(hnsw file);
    *10. Write(ckptString_Complete) to ckpt file
    *11. Delete m_name.hnsw.ckpt.state file
  */

    WriteCheckPointStatus(hnswFileName, CKPT_BEGIN_INCR_PASS1);

    std::string ckptFileName = hnswFileName + ".ckpt.state";

    int ckptFile = Open(ckptFileName.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600); // new

    /* First merge all the partitioned lists into single set - automatic
     * duplicate removal due to set<>
     */
    std::set<tableint> mx_nodeUpdates;
    std::set<tableint> mx_nodeLinksLevel0Updates;
    std::set<tableint> mx_nodeLinksLevelGt0Updates;

    for (int i = 0; i < FLUSH_LIST_PARTS; i++) {
      mx_nodeUpdates.insert(m_nodeUpdates[i].begin(), m_nodeUpdates[i].end());
      mx_nodeLinksLevel0Updates.insert(m_nodeLinksLevel0Updates[i].begin(), m_nodeLinksLevel0Updates[i].end());
      mx_nodeLinksLevelGt0Updates.insert(m_nodeLinksLevelGt0Updates[i].begin(), m_nodeLinksLevelGt0Updates[i].end());
    }


    saveIndexHeader(ckptFile, ckptFileName);

    /* Next is the scope or size of this checkpoint. */
    size_t s = mx_nodeUpdates.size();
    Write(ckptFile, &s, sizeof(s), ckptFileName, __LINE__);
    s = mx_nodeLinksLevel0Updates.size();
    Write(ckptFile, &s, sizeof(s), ckptFileName, __LINE__);
    s = mx_nodeLinksLevelGt0Updates.size();
    Write(ckptFile, &s, sizeof(s), ckptFileName, __LINE__);

    debug_print("Flush List Sizes (%lu) (%lu) (%lu).", mx_nodeUpdates.size(),
                mx_nodeLinksLevel0Updates.size(), mx_nodeLinksLevelGt0Updates.size());

    // Sort the flush lists in NodeID order - already done in ordered_set<>

    for (auto nodeId : mx_nodeUpdates) {
        Write(ckptFile, &nodeId, sizeof(nodeId), ckptFileName, __LINE__);
        unsigned int sz = size_data_per_element_;
        Write(ckptFile, &sz, sizeof(sz), ckptFileName, __LINE__);

        char *nodeVectorAndLevel0Links = getDataByInternalId(nodeId);
        Write(ckptFile, nodeVectorAndLevel0Links, size_data_per_element_,
              ckptFileName, __LINE__);

        unsigned int linkListSizeLevelGt0 =
          element_levels_[nodeId] > 0 ? size_links_per_element_ * element_levels_[nodeId] : 0;
        Write(ckptFile, &linkListSizeLevelGt0, sizeof(linkListSizeLevelGt0),
              ckptFileName, __LINE__);
        if (linkListSizeLevelGt0) {
          Write(ckptFile, linkLists_[nodeId], linkListSizeLevelGt0,
                ckptFileName, __LINE__);
        }
    } // full node flush - vector data + level 0 links and higher level links


    for (auto nodeId : mx_nodeLinksLevel0Updates) {
        unsigned int sz = size_links_level0_;
        Write(ckptFile, &nodeId, sizeof(nodeId), ckptFileName, __LINE__);
        Write(ckptFile, &sz, sizeof(sz), ckptFileName, __LINE__);

        char *level0Links = (char *)get_linklist0(nodeId);
        Write(ckptFile, level0Links, size_links_level0_, ckptFileName, __LINE__);
    } // level = 0 links

    for (auto nodeId : mx_nodeLinksLevelGt0Updates) {
        unsigned int linkListSize =
          element_levels_[nodeId] > 0 ? size_links_per_element_ * element_levels_[nodeId] : 0;

        if (linkListSize) {
          Write(ckptFile, &nodeId, sizeof(nodeId), ckptFileName, __LINE__);
          Write(ckptFile, &linkListSize, sizeof(linkListSize), ckptFileName, __LINE__);
          Write(ckptFile, linkLists_[nodeId], linkListSize, ckptFileName, __LINE__);
        }
        else {
          throw std::runtime_error("checkpoint internal error #1");
        }
    } // level >= 1 links

    Fsync(ckptFile, ckptFileName);

    Close(ckptFile, ckptFileName);
    
    WriteCheckPointStatus(hnswFileName, CKPT_END_INCR_PASS1);

    /* All incremental state has been saved to ckpt file. Now we write to
     * the "real" HNSW index files.
     */
    
    WriteCheckPointStatus(hnswFileName, CKPT_BEGIN_INCR_PASS2);

    // Step 2 - Write to real hnsw index file now
    int hnswFile = Open(hnswFileName.c_str(), O_RDWR | O_CREAT, 0600);

    saveIndexHeader(hnswFile, hnswFileName);

    for (auto nodeId : mx_nodeUpdates) {
        //char *nodeVectorAndLevel0Links = getDataByInternalId(nodeId);
        char *nodeVectorAndLevel0Links = (char *)get_linklist0(nodeId);
        
        size_t ofs = (nodeVectorAndLevel0Links - data_level0_memory_) + HNSW_FILE_METADATA_SIZE;
        Lseek(hnswFile, ofs, SEEK_SET, hnswFileName);

        Write(hnswFile, nodeVectorAndLevel0Links, size_data_per_element_,
              hnswFileName, __LINE__);

        unsigned int linkListSizeLevelGt0 =
          element_levels_[nodeId] > 0 ? size_links_per_element_ * element_levels_[nodeId] : 0;
        if (linkListSizeLevelGt0)
           mx_nodeLinksLevelGt0Updates.insert(nodeId); // defer pattern!
    } // full node - vector data, level 0 links and higher level links


    for (auto nodeId : mx_nodeLinksLevel0Updates) {
        // check if this node level0 update is already processed above
        if (mx_nodeUpdates.find(nodeId) != mx_nodeUpdates.end()) continue;

        char *level0Links = (char *)get_linklist0(nodeId);
        size_t ofs = (level0Links - data_level0_memory_) + HNSW_FILE_METADATA_SIZE;
        Lseek(hnswFile, ofs, SEEK_SET, hnswFileName);
        Write(hnswFile, level0Links, size_links_level0_, hnswFileName, __LINE__);
        /// debug_print("Write level0 links of %lu at %lu.", nodeId, ofs);
    }

    Fsync(hnswFile, hnswFileName);

    Close(hnswFile, hnswFileName);

    std::string linksLocation = hnswFileName + ".links";
    std::string linksDataLocation = hnswFileName + ".links.data";
    
    int linksDirOutput  = Open(linksLocation.c_str(), O_RDWR | O_CREAT, 0600);
    int linksDataOutput = Open(linksDataLocation.c_str(), O_RDWR | O_CREAT, 0600);

    for (auto nodeId : mx_nodeLinksLevelGt0Updates) {
        unsigned int linkListSize =
          element_levels_[nodeId] > 0 ? size_links_per_element_ * element_levels_[nodeId] : 0;
        if (linkListSize) {
            size_t datafileOfs = 0;
            if (m_linksOffsetsInFile.find(nodeId) == m_linksOffsetsInFile.end()) {
              // first time addition of this node
              Lseek(linksDirOutput, 0, SEEK_END, linksLocation);
              Write(linksDirOutput, &nodeId, sizeof(nodeId),
                    linksLocation, __LINE__);
              Write(linksDirOutput, &linkListSize, sizeof(linkListSize), 
                    linksLocation, __LINE__);
              /// debug_print("Flushing new gt0 node %lu.", nodeId);
            }
            else {
              datafileOfs = m_linksOffsetsInFile[nodeId];
            }
            if (datafileOfs)
               Lseek(linksDataOutput, datafileOfs, SEEK_SET, linksDataLocation);
            else
            {
               Lseek(linksDataOutput, 0, SEEK_END, linksDataLocation);
               m_linksOffsetsInFile[nodeId] = Lseek(linksDataOutput, 0, SEEK_CUR, linksDataLocation);
               /// debug_print("Writing Gt0 links of node %lu, size %lu, file offset %lu.",
               ///            nodeId, linkListSize, m_linksOffsetsInFile[nodeId]);
            }
            Write(linksDataOutput, linkLists_[nodeId], linkListSize, linksDataLocation, __LINE__);
        }
    } // for Gt0 updates

    Fsync(linksDirOutput, linksLocation);
    Fsync(linksDataOutput, linksDataLocation);

    Close(linksDirOutput, linksLocation);
    Close(linksDataOutput, linksDataLocation);

    WriteCheckPointStatus(hnswFileName, CKPT_END_INCR_PASS2);

    setCheckPointComplete(hnswFileName);

    // ckpt file is not needed any more.

    } // doCheckPoint()

    void doRecovery(const std::string &hnswFileName) {

      /* Don't touch the checkpoint status file. We just "replay" the
       * previous checkpoint and mark the index consistent on completion.
       * If we crash during recovery, then the process is just repeated
       * again from start.
       */
    
      std::string ckptFileName = hnswFileName + ".ckpt.state";
      int ckptFile = Open(ckptFileName.c_str(), O_RDWR);
      int hnswFile = Open(hnswFileName.c_str(), O_RDWR);
      
      //read all the metadata fields first - directly into members
      readIndexHeader(ckptFile);

      saveIndexHeader(hnswFile, hnswFileName);

      cur_element_count = 0; // IMPORTANT

      size_t nodeCount = 0, nodeLevel0LinksCount = 0, nodeLevelGt0LinksCount = 0;

      read(ckptFile, &nodeCount, sizeof(nodeCount));
      read(ckptFile, &nodeLevel0LinksCount, sizeof(nodeLevel0LinksCount));
      read(ckptFile, &nodeLevelGt0LinksCount, sizeof(nodeLevelGt0LinksCount));

      debug_print("Recovery of %s, nodeCount=%lu,level0Links=%lu,levelGt0Links=%lu.",
                  hnswFileName.c_str(), nodeCount, nodeLevel0LinksCount,
                  nodeLevelGt0LinksCount);
 
      unsigned char *rdbuf = new unsigned char [65536];

      std::map<tableint, std::vector<unsigned char>> levelGt0NodesLists;

      for (int i = 0; i < nodeCount; i++) {
        tableint nodeId;
        unsigned int sz;
        
        read(ckptFile, &nodeId, sizeof(nodeId));
        read(ckptFile, &sz,     sizeof(sz)); 

        read(ckptFile, rdbuf,   sz);

        size_t ofs = (nodeId * size_data_per_element_) + offsetData_ +
                        HNSW_FILE_METADATA_SIZE;

        Lseek(hnswFile, ofs, SEEK_SET, hnswFileName);
        Write(hnswFile, rdbuf, sz, hnswFileName, __LINE__);
	
        unsigned int listsz = 0;
        read(ckptFile, &listsz, sizeof(listsz));
        if (listsz) {
          read(ckptFile, rdbuf, listsz);
          std::vector<unsigned char> saveList(rdbuf, rdbuf + listsz);
          levelGt0NodesLists[nodeId] = saveList; // stash for later
        }
      }

      for (int i = 0; i < nodeLevel0LinksCount; i++) {
        tableint nodeId;
        unsigned int listsz;

        read(ckptFile, &nodeId, sizeof(nodeId));
        read(ckptFile, &listsz, sizeof(listsz));

        read(ckptFile, rdbuf, listsz);

        size_t ofs = (nodeId * size_data_per_element_) + offsetLevel0_ + 
                        HNSW_FILE_METADATA_SIZE;

        Lseek(hnswFile, ofs, SEEK_SET, hnswFileName);
        Write(hnswFile, rdbuf, listsz, hnswFileName, __LINE__);
      }

      for (int i = 0; i < nodeLevelGt0LinksCount; i++) {
        tableint nodeId;
        unsigned int listsz;
        
        read(ckptFile, &nodeId, sizeof(nodeId));
        read(ckptFile, &listsz, sizeof(listsz));
        
        read(ckptFile, rdbuf, listsz);
        
        std::vector<unsigned char> saveList(rdbuf, rdbuf + listsz);
        levelGt0NodesLists[nodeId] = saveList;
      }

      std::string linksLocation = hnswFileName + ".links";
      std::string linksDataLocation = hnswFileName + ".links.data";

      // Restarting after crash - read & load the offsets map for links gt 0
      size_t current_data_pos = 0;
      std::map<tableint, size_t> gt0linksOffsetsInFile; // temporary
      std::ifstream inputLinksDir(linksLocation, std::ios::binary);
      while (inputLinksDir.good()) {
            unsigned int nodeID = 0,linkListSize = 0;
            readBinaryPOD(inputLinksDir, nodeID);
            readBinaryPOD(inputLinksDir, linkListSize);
            
            gt0linksOffsetsInFile[nodeID] = current_data_pos;
            current_data_pos = current_data_pos + linkListSize;
      }
      inputLinksDir.close();

      // Now write the levels > 0 links to specific files
    
      int linksDirOutput  = Open(linksLocation.c_str(), O_RDWR);
      int linksDataOutput = Open(linksDataLocation.c_str(), O_RDWR);

      /* ordered map is important for levelGt0NodesLists */

      Lseek(linksDirOutput, 0, SEEK_SET, linksLocation);
      for (auto iter : levelGt0NodesLists) { 
        tableint nodeId = iter.first;
        unsigned int linkListSize = iter.second.size();
        if (linkListSize) {
            size_t datafileOfs = 0;
            if (gt0linksOffsetsInFile.find(nodeId) == gt0linksOffsetsInFile.end()) {
              // First time addition of this node - ordered map is key
              Lseek(linksDirOutput, 0, SEEK_END, linksLocation);
              Write(linksDirOutput, &nodeId, sizeof(nodeId),
                    linksLocation, __LINE__);
              Write(linksDirOutput, &linkListSize, sizeof(linkListSize), 
                    linksLocation, __LINE__);
            }
            else {
              datafileOfs = gt0linksOffsetsInFile[nodeId];
            }
            if (datafileOfs)
               Lseek(linksDataOutput, datafileOfs, SEEK_SET, linksDataLocation);
            else
            {
               Lseek(linksDataOutput, 0, SEEK_END, linksDataLocation);
               gt0linksOffsetsInFile[nodeId] = Lseek(linksDataOutput, 0, SEEK_CUR, linksDataLocation);
            }
            std::vector<unsigned char> li = iter.second;
            Write(linksDataOutput, li.data(), linkListSize, linksDataLocation, __LINE__);
        }
      } // for Gt0 updates - ordered map

      Fsync(linksDirOutput, linksLocation);

      Fsync(linksDataOutput, linksDataLocation);

      Close(linksDirOutput, linksLocation);

      Close(linksDataOutput,linksDataLocation);

      Fsync(hnswFile, hnswFileName);

      Close(hnswFile, hnswFileName);

      Close(ckptFile, ckptFileName); // No writes, only read
      
      setCheckPointComplete(hnswFileName); // Recovery complete, mark CONSISTENT

      delete[] rdbuf;
      
    } // doRecovery()

    size_t indexFileMetadataSize() const {
        size_t size = 0;
        size += sizeof(offsetLevel0_);
        size += sizeof(max_elements_);
        size += sizeof(cur_element_count);
        size += sizeof(size_data_per_element_);
        size += sizeof(label_offset_);
        size += sizeof(offsetData_);
        size += sizeof(maxlevel_);
        size += sizeof(enterpoint_node_);
        size += sizeof(maxM_);

        size += sizeof(maxM0_);
        size += sizeof(M_);
        size += sizeof(mult_);
        size += sizeof(ef_construction_);

        return size;
    }
    
    size_t indexFileSize() const {
        size_t size = indexFileMetadataSize();

        size += cur_element_count * size_data_per_element_;

        for (size_t i = 0; i < cur_element_count; i++) {
            unsigned int linkListSize = element_levels_[i] > 0 ? size_links_per_element_ * element_levels_[i] : 0;
            size += sizeof(linkListSize);
            size += linkListSize;
        }
        return size;
    }

    void saveIndexHeader(int hnswFile, const std::string & filename) {

        Write(hnswFile, &offsetLevel0_, sizeof(offsetLevel0_), filename, __LINE__);
        Write(hnswFile, &max_elements_, sizeof(max_elements_), filename, __LINE__);
        Write(hnswFile, &cur_element_count, sizeof(cur_element_count), filename, __LINE__);
        Write(hnswFile, &size_data_per_element_, sizeof(size_data_per_element_), filename, __LINE__);
        Write(hnswFile, &label_offset_, sizeof(label_offset_), filename, __LINE__);
        Write(hnswFile, &offsetData_, sizeof(offsetData_), filename, __LINE__);
        Write(hnswFile, &maxlevel_, sizeof(maxlevel_), filename, __LINE__);
        Write(hnswFile, &enterpoint_node_, sizeof(enterpoint_node_), filename, __LINE__);
        Write(hnswFile, &maxM_, sizeof(maxM_), filename, __LINE__);
        Write(hnswFile, &maxM0_, sizeof(maxM0_), filename, __LINE__);
        Write(hnswFile, &M_, sizeof(M_), filename, __LINE__);
        Write(hnswFile, &mult_, sizeof(mult_), filename, __LINE__);
        Write(hnswFile, &ef_construction_, sizeof(ef_construction_), filename, __LINE__);
    }
    
    void readIndexHeader(int hnswFile) {
        read(hnswFile, &offsetLevel0_, sizeof(offsetLevel0_));
        read(hnswFile, &max_elements_, sizeof(max_elements_));
        read(hnswFile, &cur_element_count, sizeof(cur_element_count));
        read(hnswFile, &size_data_per_element_, sizeof(size_data_per_element_));
        read(hnswFile, &label_offset_, sizeof(label_offset_));
        read(hnswFile, &offsetData_, sizeof(offsetData_));
        read(hnswFile, &maxlevel_, sizeof(maxlevel_));
        read(hnswFile, &enterpoint_node_, sizeof(enterpoint_node_));
        read(hnswFile, &maxM_, sizeof(maxM_));
        read(hnswFile, &maxM0_, sizeof(maxM0_));
        read(hnswFile, &M_, sizeof(M_));
        read(hnswFile, &mult_, sizeof(mult_));
        read(hnswFile, &ef_construction_, sizeof(ef_construction_));
    }

    void debug() {
      size_t c = cur_element_count;
      debug_print("MaxElements = %lu, CurElements = %lu, SizePerElement = %lu"
                  "maxLevel = %lu, maxM = %lu, maxM0_ = %lu, M_  = %lu.",
                  max_elements_, c, size_data_per_element_,
                  maxlevel_, maxM_, maxM0_, M_);
      debug_print("CheckPointId = %s.", m_checkPointId.c_str());
    }

    std::string getCheckPointId() const {
      return m_checkPointId;
    }

    void setCheckPointId() {
      m_checkPointId = "[Invalid]";
    }

    void setCheckPointId(const std::string &ck) {
      m_checkPointId = ck;
    } 

    void setCheckPointComplete(const std::string &hnswFileName) {
      WriteCheckPointStatus(hnswFileName, CKPT_CONSISTENT);
      clearFlushList();
      m_checkPointId = "[Invalid]";
    }

    void saveIndex(const std::string &hnswFileName) {
        WriteCheckPointStatus(hnswFileName, CKPT_BEGIN_FULL_WRITE);
        int hnswFile = Open(hnswFileName.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);

        saveIndexHeader(hnswFile, hnswFileName);

        // This write() could do GBs of data write. All vectors & all level0
        // links are written to disk by this single Write() call.
        size_t xx = cur_element_count;
        size_t wrc = 0, wc = (cur_element_count * size_data_per_element_);
        while (1) {
          ssize_t ret = write(hnswFile, &data_level0_memory_[wrc], wc);
          if (ret < 0) {
            break;
          }
          wrc += ret;
          wc -= ret;
          if (!wc) break;
        }

        Fsync(hnswFile, hnswFileName);
        Close(hnswFile, hnswFileName);

        std::string linksLocation = hnswFileName + ".links";
        int gt0LinksF = Open(linksLocation.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
        std::string linksDataLocation = hnswFileName + ".links.data";
        int gt0LinksDataF = Open(linksDataLocation.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);

        for (size_t i = 0; i < cur_element_count; i++) {
            unsigned int linkListSize = element_levels_[i] > 0 ? 
              (size_links_per_element_ * element_levels_[i]) : 0;
            if (linkListSize)
              debug_print("Writing links of size %u for node %lu.", linkListSize, i);
            if (linkListSize) {
              unsigned int nodeID = i;
              Write(gt0LinksF, &nodeID, sizeof(nodeID), linksLocation, __LINE__);
              Write(gt0LinksF, &linkListSize, sizeof(linkListSize), linksLocation, __LINE__);

              Write(gt0LinksDataF, linkLists_[i], linkListSize, linksDataLocation, __LINE__);
            }
        }
        Fsync(gt0LinksF, linksLocation);
        Close(gt0LinksF, linksLocation);

        Fsync(gt0LinksDataF, linksDataLocation);
        Close(gt0LinksDataF, linksDataLocation);
        
        WriteCheckPointStatus(hnswFileName, CKPT_END_FULL_WRITE);

        setCheckPointComplete(hnswFileName);
    } // saveIndex()


    void loadIndex(const std::string &location, SpaceInterface<dist_t> *s, size_t max_elements_i = 0) {
        size_t ts = 0;
        bool bConsistent = true;
        makeIndexConsistent(location, bConsistent, ts);
        std::ifstream input(location, std::ios::binary);

        if (!input.is_open())
            throw std::runtime_error("Cannot open file");

        clear();
        // get file size:
        input.seekg(0, input.end);
        std::streampos total_filesize = input.tellg();
        input.seekg(0, input.beg);

        readBinaryPOD(input, offsetLevel0_);
        readBinaryPOD(input, max_elements_);
        readBinaryPOD(input, cur_element_count);
#if 0
        size_t max_elements = max_elements_i;
        if (max_elements < cur_element_count)
            max_elements = max_elements_;
        max_elements_ = max_elements;
#endif
        size_t max_elements = max_elements_;
        readBinaryPOD(input, size_data_per_element_);
        readBinaryPOD(input, label_offset_);
        readBinaryPOD(input, offsetData_);
        readBinaryPOD(input, maxlevel_);
        readBinaryPOD(input, enterpoint_node_);

        readBinaryPOD(input, maxM_);
        readBinaryPOD(input, maxM0_);
        readBinaryPOD(input, M_);
        readBinaryPOD(input, mult_);
        readBinaryPOD(input, ef_construction_);

        data_size_ = s->get_data_size();
        fstdistfunc_ = s->get_dist_func();
        dist_func_param_ = s->get_dist_func_param();

        auto pos = input.tellg();
#if 0
        /// Optional - check if index is ok:
        input.seekg(cur_element_count * size_data_per_element_, input.cur);
        for (size_t i = 0; i < cur_element_count; i++) {
            if (input.tellg() < 0 || input.tellg() >= total_filesize) {
                throw std::runtime_error("Index seems to be corrupted or unsupported");
            }

            unsigned int linkListSize;
            readBinaryPOD(input, linkListSize);
            if (linkListSize != 0) {
                input.seekg(linkListSize, input.cur);
            }
        }

        // throw exception if it either corrupted or old index
        if (input.tellg() != total_filesize)
            throw std::runtime_error("Index seems to be corrupted or unsupported");

        input.clear();
        /// Optional check end
#endif
        input.seekg(pos, input.beg);

        data_level0_memory_ = (char *) malloc(max_elements * size_data_per_element_);
        if (data_level0_memory_ == nullptr)
            throw std::runtime_error("Not enough memory: loadIndex failed to allocate level0");
        input.read(data_level0_memory_, cur_element_count * size_data_per_element_);

        size_links_per_element_ = maxM_ * sizeof(tableint) + sizeof(linklistsizeint);

        size_links_level0_ = maxM0_ * sizeof(tableint) + sizeof(linklistsizeint);
        std::vector<std::mutex>(max_elements).swap(link_list_locks_);
        std::vector<std::mutex>(MAX_LABEL_OPERATION_LOCKS).swap(label_op_locks_);

        visited_list_pool_.reset(new VisitedListPool(1, max_elements));

        linkLists_ = (char **) malloc(sizeof(void *) * max_elements);
        if (linkLists_ == nullptr)
            throw std::runtime_error("Not enough memory: loadIndex failed to allocate linklists");
        element_levels_ = std::vector<int>(max_elements);
        revSize_ = 1.0 / mult_;
        ef_ = 10;
        for (size_t i = 0; i < cur_element_count; i++) {
            label_lookup_[getExternalLabel(i)] = i;
            element_levels_[i] = 0;
            linkLists_[i] = nullptr;
        }

        std::string linksDirLocation = location + ".links";
        std::ifstream inputLinksDir(linksDirLocation, std::ios::binary);
        std::string linksDataLocation = location + ".links.data";
        std::ifstream inputLinksData(linksDataLocation, std::ios::binary);

        if (!inputLinksDir.is_open() || !inputLinksData.is_open()) {
            throw std::runtime_error("Cannot open file");
        }
        size_t current_data_pos = 0;
        while (inputLinksDir.good()) {
            unsigned int nodeID = 0,linkListSize = 0;
            readBinaryPOD(inputLinksDir, nodeID);
            readBinaryPOD(inputLinksDir, linkListSize);

            if (!inputLinksDir.good() || !linkListSize)
              break;

            element_levels_[nodeID] = linkListSize / size_links_per_element_;
            linkLists_[nodeID] = (char *) malloc(linkListSize);
            if (linkLists_[nodeID] == nullptr)
               throw std::runtime_error("Not enough memory: loadIndex failed to allocate linklist");
            inputLinksData.read(linkLists_[nodeID], linkListSize);
            m_linksOffsetsInFile[nodeID] = current_data_pos;
            current_data_pos = current_data_pos + linkListSize;
        } // while

        for (size_t i = 0; i < cur_element_count; i++) {
            if (isMarkedDeleted(i)) {
                num_deleted_ += 1;
                if (allow_replace_deleted_) deleted_elements.insert(i);
            }
        }

        input.close();
        inputLinksDir.close();
        inputLinksData.close();

        return;
    }


