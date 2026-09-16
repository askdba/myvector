/*  Copyright (c) 2024 - p3io.in / shiyer22@gmail.com */
/*
   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License, version 2.0, for more details.
*/
-- myvector_uninstall_component.sql - Clean teardown for the MyVector component.
--
-- Counterpart to sql/myvector_install_component.sql. UNINSTALL COMPONENT only
-- deregisters the UDFs the component registered itself; it does NOT remove the
-- supplemental SONAME UDFs, the stored procedures, or the view created by the
-- install script. Dropping them here first keeps mysql.func / mysql.proc clean
-- and avoids dangling entries that reference myvector.so after the component
-- (and its .so) are gone. Run as 'root'.

USE mysql;

DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_BUILD;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_REFRESH;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_DROP;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_LOAD;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_STATUS;
DROP PROCEDURE IF EXISTS MYVECTOR_INDEX_INTERNAL;

DROP VIEW IF EXISTS myvector_columns;

-- Supplemental SONAME UDFs — NOT removed by UNINSTALL COMPONENT.
DROP FUNCTION IF EXISTS myvector_row_distance;
DROP FUNCTION IF EXISTS myvector_is_valid;
DROP FUNCTION IF EXISTS myvector_search_open_udf;

-- sqlfluff: disable=PRS
UNINSTALL COMPONENT 'file://myvector';
-- sqlfluff: enable=PRS
