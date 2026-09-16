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
-- myvector_install_component.sql - MyVector Component registration script.
--
-- Component analogue of sql/myvectorplugin.sql. Installing the component
-- (INSTALL COMPONENT) auto-registers the core UDFs (myvector_construct,
-- myvector_display, myvector_distance, myvector_ann_set,
-- myvector_construct_binaryvector, myvector_hamming_distance). This script
-- adds the pieces the component framework does NOT create:
--   * the supplemental SONAME UDFs (myvector_row_distance, myvector_is_valid,
--     myvector_search_open_udf) that index build / ANN queries rely on, and
--   * the MYVECTOR_INDEX_* stored procedures and myvector_columns view.
-- Without these, a component-only install can run basic UDFs but fails the
-- HNSW index-build and ANN workflows.
--
-- Run as 'root'; all objects are registered in the "mysql" database.
--
-- Clean teardown: sql/myvector_uninstall_component.sql (UNINSTALL COMPONENT
-- does NOT drop the supplemental SONAME UDFs, so they must be dropped
-- explicitly to avoid dangling mysql.func rows).
--
-- NOTE: index build/refresh also needs a "myvector.cnf" (myvector_host /
-- myvector_user_id / myvector_user_password / myvector_port) in mysqld's CWD
-- so the build thread can connect back over TCP, and myvector_index_dir set to
-- a writable path. Those are runtime/deployment settings, not SQL objects; see
-- docs/DOCKER_IMAGES.md and scripts/smoke-component.sh.

USE mysql;

-- Create the myvector_columns view BEFORE installing the component. The
-- component's binlog thread runs OpenAllOnlineVectorIndexes() on its first
-- connection (src/component_src/myvector_binlog_service.cc), which queries
-- mysql.myvector_columns. Installing the component first opens a race where
-- that thread can query the view before it exists and skip registering
-- existing online=Y indexes. The view depends only on INFORMATION_SCHEMA, not
-- on the component, so it is safe to create first.
DROP VIEW IF EXISTS myvector_columns;

CREATE VIEW myvector_columns
AS
SELECT TABLE_SCHEMA as db, TABLE_NAME as tbl, COLUMN_NAME as col,
       COLUMN_COMMENT as info
FROM INFORMATION_SCHEMA.COLUMNS
WHERE COLUMN_COMMENT LIKE 'MYVECTOR%'
ORDER BY db,tbl,col;

-- sqlfluff: disable=PRS
INSTALL COMPONENT 'file://myvector';
-- sqlfluff: enable=PRS

-- Supplemental UDFs. These are defined in myvector.so but are NOT registered
-- by the component's own register_udfs(), so create them explicitly by SONAME.
DROP FUNCTION IF EXISTS myvector_row_distance;
DROP FUNCTION IF EXISTS myvector_is_valid;
DROP FUNCTION IF EXISTS myvector_search_open_udf;

-- myvector_row_distance(pkid INT)
-- Return : Distance of the selected approximate near neighbour to the query vector.
-- Used in the SELECT list when a MYVECTOR_IS_ANN query is run.
CREATE FUNCTION myvector_row_distance  RETURNS REAL     SONAME 'myvector.so';

-- myvector_is_valid(vec1 VARBINARY, INT dim)
-- Return : 1 if vector is valid w.r.t. dimension & checksum, 0 otherwise.
-- Critical to detect vector column tampering or malformed vectors.
CREATE FUNCTION myvector_is_valid      RETURNS INTEGER  SONAME 'myvector.so';

-- myvector_search_open_udf() - internal function, not for direct use.
CREATE FUNCTION myvector_search_open_udf RETURNS STRING SONAME 'myvector.so';

DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_BUILD;

DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_REFRESH;

DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_DROP;

DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_LOAD;

DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_STATUS;

DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_INTERNAL;

DELIMITER //


CREATE PROCEDURE MYVECTOR_INDEX_STATUS(
	IN myvectorcolumn VARCHAR(256))
BEGIN
        DECLARE extra   VARCHAR(1024);
        DECLARE pkid    VARCHAR(1024);

        SET extra = '';
        SET pkid  = '';

        CALL MYVECTOR_INDEX_INTERNAL(myvectorcolumn, pkid, 'status', extra);

END
//

CREATE PROCEDURE MYVECTOR_INDEX_DROP(
	IN myvectorcolumn VARCHAR(256))
BEGIN
        DECLARE extra   VARCHAR(1024);
        DECLARE pkid    VARCHAR(1024);

        SET extra = '';
        SET pkid  = '';

        CALL MYVECTOR_INDEX_INTERNAL(myvectorcolumn, pkid, 'drop', extra);
END
//

CREATE PROCEDURE MYVECTOR_INDEX_LOAD(
	IN myvectorcolumn VARCHAR(256))
BEGIN
        DECLARE extra   VARCHAR(1024);
        DECLARE pkid    VARCHAR(1024);

        SET extra = '';
        SET pkid  = '';

        CALL MYVECTOR_INDEX_INTERNAL(myvectorcolumn, pkid, 'load', extra);
END
//

CREATE PROCEDURE MYVECTOR_INDEX_REFRESH(
	IN myvectorcolumn VARCHAR(256),
	IN pkidcolumn     VARCHAR(64))
BEGIN
        DECLARE extra   VARCHAR(1024);

        SET extra = '';

        CALL MYVECTOR_INDEX_INTERNAL(myvectorcolumn, pkidcolumn, 'refresh', extra);
END
//

CREATE PROCEDURE MYVECTOR_INDEX_INTERNAL(
	IN myvectorcolumn VARCHAR(256),
	IN pkidcolumn     VARCHAR(64),
	IN action         VARCHAR(64),
        IN extra          VARCHAR(1024))
BEGIN
	DECLARE pos INT;
	DECLARE status  VARCHAR(1024);
	DECLARE temp    VARCHAR(256);
	DECLARE dbname  VARCHAR(64);
	DECLARE tname   VARCHAR(64);
	DECLARE cname   VARCHAR(64);
	DECLARE colinfo VARCHAR(1024);
	DECLARE CONTINUE HANDLER FOR NOT FOUND SET colinfo = NULL;
	-- Verify column name is db.table.column

        -- Read column comment from I_S.COLUMNS
        SET pos    = LOCATE('.', myvectorcolumn);
	SET dbname = SUBSTR(myvectorcolumn, 1, pos-1);
	SET temp   = SUBSTR(myvectorcolumn, pos+1);
	SET pos    = LOCATE('.', temp);
	SET tname  = SUBSTR(temp, 1, pos-1);
	SET cname  = SUBSTR(temp, pos+1);


	SELECT column_comment INTO colinfo FROM INFORMATION_SCHEMA.COLUMNS
	WHERE table_schema = dbname AND table_name = tname AND
	column_name = cname;

	IF colinfo IS NULL THEN
	  SELECT myvectorcolumn AS InputVectorColumnName, CONCAT(dbname,'.',tname,'.',cname) AS ParsedVectorColumnName;
          SELECT 'Please use the fully qualified vector column name : <database>.<table>.<column>' AS Message;
	  SIGNAL SQLSTATE '50001' SET MESSAGE_TEXT = 'Vector column not found. Please use the fully qualified name: <database>.<table>.<column>.';
        END IF;

	-- SELECT CONCAT('Column Comment is :',colinfo);

	IF LOCATE("MYVECTOR COLUMN", colinfo) <> 1 THEN
	  SIGNAL SQLSTATE '50002' SET MESSAGE_TEXT = 'The specified column is not a MYVECTOR column.';
	END IF;

	IF action = "refresh" AND LOCATE("track=", colinfo) = 0 THEN
	  SIGNAL SQLSTATE '50003' SET MESSAGE_TEXT = 'MyVector tracking timestamp column not found for incremental refresh.';
	END IF;

        -- Call UDF to open/build/load index
        SET status  = MYVECTOR_SEARCH_OPEN_UDF(myvectorcolumn, colinfo, pkidcolumn, action, extra);

        -- below output goes to the terminal as a single status from this procedure
        SELECT status As Status;

END
//

-- action is 'build', 'refresh', 'load', 'drop'
CREATE PROCEDURE MYVECTOR_INDEX_BUILD(
	IN myvectorcolumn VARCHAR(256),
	IN pkidcolumn     VARCHAR(64))
BEGIN
        DECLARE extra   VARCHAR(1024);
        SET extra = '';

        CALL MYVECTOR_INDEX_INTERNAL(myvectorcolumn, pkidcolumn, 'build', extra);

END
//

DELIMITER ;
