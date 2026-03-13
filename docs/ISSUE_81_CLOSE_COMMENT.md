# Suggested comment for closing issue #81

Copy the block below and paste as a comment on https://github.com/askdba/myvector/issues/81

---

**Documentation added.**

Created [docs/ONLINE_INDEX_UPDATES.md](https://github.com/askdba/myvector/blob/main/docs/ONLINE_INDEX_UPDATES.md) with:

- **Overview** of online updates (real-time index sync via MySQL binlogs)
- **Prerequisites:** row-based binlog format, `REPLICATION CLIENT` (or `REPLICATION SLAVE`) privileges, config file for binlog connection
- **Step-by-step guide** to create indexes with `online=Y` and `idcol=<pk_column>` in the MYVECTOR column options
- **Complete example** from table creation through DML and search
- **Troubleshooting** table for common issues

The doc is linked from the main [README](https://github.com/askdba/myvector/blob/main/README.md) Documentation section.
