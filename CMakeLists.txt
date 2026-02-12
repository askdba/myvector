cmake_minimum_required(VERSION 3.5)
project(MyVectorComponent LANGUAGES CXX)

# -----------------------------------------------------------------------------
# This CMakeLists supports two build modes:
# 1) In-tree plugin build: when included from mysql-server (add_subdirectory),
#    MYSQL_ADD_PLUGIN is available; we define the "myvector" plugin target
#    so that "make myvector" works (used by CI plugin job).
# 2) Standalone component build: when MYSQL_SOURCE_DIR is set, we build the
#    myvector_component shared library (see docs/COMPONENT_MIGRATION_PLAN.md).
# -----------------------------------------------------------------------------

if(COMMAND MYSQL_ADD_PLUGIN)
  # In-tree plugin build (e.g. CI copies this file to mysql-server/plugin/myvector)
  set(MYVECTOR_PLUGIN_SRCS
    myvector_plugin.cc
    myvector_binlog.cc
    myvector.cc
    myvectorutils.cc
  )
  MYSQL_ADD_PLUGIN(myvector ${MYVECTOR_PLUGIN_SRCS})
  return()
endif()

# -----------------------------------------------------------------------------
# Standalone component build.
# Required:
#   MYSQL_SOURCE_DIR  - Path to MySQL server source root (must contain
#                       include/mysql/components/component_implementation.h).
# Optional:
#   MYSQL_BUILD_DIR   - Path to MySQL server build dir (for generated headers).
#   MYSQL_DIR         - Path to MySQL install or client package; used to find
#                       libmysqlclient and client includes if not under source.
# -----------------------------------------------------------------------------

if(NOT DEFINED MYSQL_SOURCE_DIR OR NOT EXISTS "${MYSQL_SOURCE_DIR}/include/mysql/components/component_implementation.h")
  message(FATAL_ERROR
    "MYSQL_SOURCE_DIR must point to MySQL server source root (containing "
    "include/mysql/components/component_implementation.h). "
    "Example: cmake -DMYSQL_SOURCE_DIR=/path/to/mysql-server/ ..")
endif()

set(MYSQL_SOURCE_DIR "${MYSQL_SOURCE_DIR}" CACHE PATH "MySQL server source root")

# Optional server build dir (for generated headers)
if(DEFINED MYSQL_BUILD_DIR AND EXISTS "${MYSQL_BUILD_DIR}/include")
  set(MYSQL_BUILD_DIR "${MYSQL_BUILD_DIR}" CACHE PATH "MySQL server build dir")
endif()

# Optional MySQL install/client dir (for libmysqlclient and client includes)
if(DEFINED MYSQL_DIR)
  set(MYSQL_DIR "${MYSQL_DIR}" CACHE PATH "MySQL install or client package root")
endif()

# Include directories: project, server include/, server root (for sql/sql_plugin.h), and build (generated headers)
set(MYVECTOR_INCLUDE_DIRS
  "${CMAKE_CURRENT_SOURCE_DIR}/include"
  "${MYSQL_SOURCE_DIR}/include"
  "${MYSQL_SOURCE_DIR}"
)
if(DEFINED MYSQL_BUILD_DIR AND EXISTS "${MYSQL_BUILD_DIR}/include")
  list(APPEND MYVECTOR_INCLUDE_DIRS "${MYSQL_BUILD_DIR}/include")
endif()

# Find libmysqlclient (required for MYSQL C API used in binlog and UDF code)
find_library(MYSQLCLIENT_LIBRARY
  NAMES mysqlclient
  HINTS "${MYSQL_DIR}" "${MYSQL_BUILD_DIR}" "${MYSQL_SOURCE_DIR}"
  PATH_SUFFIXES lib lib64 lib/mysql
)
if(NOT MYSQLCLIENT_LIBRARY)
  message(FATAL_ERROR
    "libmysqlclient not found. Set MYSQL_DIR to MySQL install root, or "
    "ensure libmysqlclient is in CMAKE_LIBRARY_PATH.")
endif()

# Optional: extra client include path if different from server source
if(DEFINED MYSQL_DIR AND EXISTS "${MYSQL_DIR}/include")
  list(APPEND MYVECTOR_INCLUDE_DIRS "${MYSQL_DIR}/include")
endif()

# Boost (used by existing source)
if(DEFINED BOOST_PATCHES_DIR)
  list(APPEND MYVECTOR_INCLUDE_DIRS ${BOOST_PATCHES_DIR})
endif()
if(DEFINED BOOST_INCLUDE_DIR)
  list(APPEND MYVECTOR_INCLUDE_DIRS ${BOOST_INCLUDE_DIR})
endif()

# Source files for the MyVector component (exclude plugin entry and plugin binlog).
# The component has its own binlog implementation in myvector_binlog_service.cc;
# including src/myvector_binlog.cc would cause ODR/linker errors (duplicate
# parseTableMapEvent, parseRowsEvent, etc.).
set(MYVECTOR_COMPONENT_SRCS
  src/component_src/myvector_component.cc
  src/component_src/myvector_query_rewrite_service.cc
  src/component_src/myvector_binlog_service.cc
  src/component_src/myvector_udf_service.cc
  src/myvector.cc
  src/myvectorutils.cc
)

add_library(myvector_component SHARED ${MYVECTOR_COMPONENT_SRCS})
target_include_directories(myvector_component PRIVATE ${MYVECTOR_INCLUDE_DIRS})
target_link_libraries(myvector_component PRIVATE ${MYSQLCLIENT_LIBRARY})

if(CMAKE_CXX_COMPILER_ID STREQUAL "GNU")
  set_property(TARGET myvector_component APPEND_STRING PROPERTY LINK_FLAGS
    " -static-libstdc++ -static-libgcc")
endif()

# Install: shared library and component manifest
install(TARGETS myvector_component
  LIBRARY DESTINATION lib/plugin
  RUNTIME DESTINATION lib/plugin
  ARCHIVE DESTINATION lib
)
install(FILES src/component_src/myvector.json
  DESTINATION share/mysql/component
)
