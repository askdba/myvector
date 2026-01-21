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
-- myvectorplugin.sql - MyVector Plugin registration script.
--
-- This script should be run as 'root' since all objects are
-- registered in the "mysql" database by default. If the MyVector procedures
-- and views should be created in another database, please change the below line

USE mysql;

INSTALL PLUGIN myvector SONAME 'myvector.so';

DROP VIEW IF EXISTS myvector_columns;

CREATE VIEW myvector_columns
AS
SELECT TABLE_SCHEMA as db, TABLE_NAME as tbl, COLUMN_NAME as col,
       COLUMN_COMMENT as info
FROM INFORMATION_SCHEMA.COLUMNS
WHERE COLUMN_COMMENT LIKE 'MYVECTOR%'
ORDER BY db,tbl,col;

-- unregister all the UDFs
DROP FUNCTION IF EXISTS myvector_construct;
DROP FUNCTION IF EXISTS myvector_display;
DROP FUNCTION IF EXISTS myvector_distance;
DROP FUNCTION IF EXISTS myvector_row_distance;
DROP FUNCTION IF EXISTS myvector_ann_set;
DROP FUNCTION IF EXISTS myvector_is_valid;

DROP FUNCTION IF EXISTS myvector_search_open_udf;

-- myvector_construct(vector_string VARCHAR)
-- Return : Serialized sequence of native floats for storing in VARBINARY column
CREATE FUNCTION myvector_construct RETURNS STRING SONAME 'myvector.so';

-- myvector_display(vector_col_expr  VARBINARY)
-- Return : Vector as human readable string e.g [0.002487210 -0.019822444..]
CREATE FUNCTION myvector_display   RETURNS STRING SONAME 'myvector.so';

-- myvector_distance(vec1 VARBINARY, vec2 VARBINARY, disttype VARCHAR)
-- Return : Computed distance between 2 vectors. disttype is 1 of L2/EUCLIDEAN/IP
CREATE FUNCTION myvector_distance  RETURNS REAL   SONAME 'myvector.so';

-- myvector_ann_set(veccol VARCHAR, options VARCHAR, searchvec VARCHAR/VARBINARY)
-- Return : Comma separated list of IDs of nearest neighbours
CREATE FUNCTION myvector_ann_set  RETURNS STRING  SONAME 'myvector.so';

-- myvector_is_valid(vec1 VARBINARY, INT dim)
-- Return : 1 if vector is valid with respect to dimension & checksum, 0 otherwise
-- This function is critical to detect vector column tampering or malformed vectors.
CREATE FUNCTION myvector_is_valid RETURNS INTEGER  SONAME 'myvector.so';

-- myvector_row_distance(pkid INT)
-- Return : Distance of the select approximate near neighbour to the query vector
-- This function is used in the SELECT list when a MYVECTOR_IS_ANN query is run
CREATE FUNCTION myvector_row_distance  RETURNS REAL   SONAME 'myvector.so';

-- myvector_search_open_udf() - internal function, not for direct use
CREATE FUNCTION myvector_search_open_udf RETURNS STRING  SONAME 'myvector.so';

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
	  SIGNAL SQLSTATE '50001' SET CONSTRAINT_NAME=myvectorcolumn, MESSAGE_TEXT = 'Vector column not found', MYSQL_ERRNO = 50001;
        END IF;

	-- SELECT CONCAT('Column Comment is :',colinfo);

	IF LOCATE("MYVECTOR COLUMN", colinfo) <> 1 THEN
	  SIGNAL SQLSTATE '50002' SET MESSAGE_TEXT = 'Column is not a MYVECTOR column', MYSQL_ERRNO = 50002;
	END IF;

	IF action = "refresh" AND LOCATE("track=", colinfo) = 0 THEN
	  SIGNAL SQLSTATE '50003' SET MESSAGE_TEXT = 'MyVector Tracking timestamp column not found for incremental refresh', MYSQL_ERRNO = 50003;
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


