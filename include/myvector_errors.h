/*
   Copyright (c) 2024 - p3io.in / shiyer22@gmail.com

   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.
*/

#ifndef MYVECTOR_ERRORS_H
#define MYVECTOR_ERRORS_H

// UDF Errors
#define ER_MYVECTOR_INCORRECT_ARGUMENTS                                        \
  "Incorrect arguments. Please check the function's documentation."
#define ER_MYVECTOR_INDEX_NOT_FOUND                                            \
  "Vector index not defined or not open for access."
#define ER_MYVECTOR_INVALID_VECTOR "Invalid vector format or checksum mismatch."

// Stored Procedure Errors
#define ER_MYVECTOR_COLUMN_NOT_FOUND                                           \
  "Vector column not found. Please use the fully qualified name: "             \
  "<database>.<table>.<column>."
#define ER_MYVECTOR_NOT_A_MYVECTOR_COLUMN                                      \
  "The specified column is not a MYVECTOR column."
#define ER_MYVECTOR_TRACKING_COLUMN_NOT_FOUND                                  \
  "MyVector tracking timestamp column not found for incremental refresh."

// SQLSTATES
#define SQLSTATE_MYVECTOR_COLUMN_NOT_FOUND "50001"
#define SQLSTATE_MYVECTOR_NOT_A_MYVECTOR_COLUMN "50002"
#define SQLSTATE_MYVECTOR_TRACKING_COLUMN_NOT_FOUND "50003"

#endif // MYVECTOR_ERRORS_H
