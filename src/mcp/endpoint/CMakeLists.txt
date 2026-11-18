# component_mcp: Phase 4 spike of RFC 0001 (askdba/mysql-mcp-server).
# An MCP endpoint inside mysqld on MySQL Router's HTTP server.
# Built in-tree like component_mysql_rest_service; needs the Router HTTP targets, which
# MRS adds (it is configured after this directory; targets resolve at generate time).

DISABLE_MISSING_PROFILE_WARNING()

IF(NOT WITH_ROUTER AND NOT (WITH_COMPONENT_MRS OR (NOT DEFINED WITH_COMPONENT_MRS AND NOT DEFINED WITHOUT_COMPONENT_MRS)))
  MESSAGE(STATUS "component_mcp: needs the Router HTTP libraries (WITH_ROUTER or component_mysql_rest_service)")
  RETURN()
ENDIF()

# Router's HTTP server, built here against a copy of http/server/connection.h with the edits in
# router_connection_edits.cmake (replies from worker threads, #304; an idle bound per connection,
# #269), instead of linking http_server_lib_object. The server tree is not modified.
SET(MCP_ROUTER_HTTP_SRC ${CMAKE_SOURCE_DIR}/router/src/http/src)
SET(MCP_ROUTER_PATCHED ${CMAKE_CURRENT_BINARY_DIR}/router_patched)
INCLUDE(${CMAKE_CURRENT_SOURCE_DIR}/router_connection_edits.cmake)
FILE(READ ${MCP_ROUTER_HTTP_SRC}/http/server/connection.h MCP_CONNECTION_H)
FOREACH(i RANGE 1 ${MCP_ROUTER_EDITS})
  STRING(FIND "${MCP_CONNECTION_H}" "${MCP_ROUTER_EDIT_${i}_ANCHOR}" first)
  STRING(FIND "${MCP_CONNECTION_H}" "${MCP_ROUTER_EDIT_${i}_ANCHOR}" last REVERSE)
  IF(first EQUAL -1 OR NOT first EQUAL last)
    MESSAGE(FATAL_ERROR "component_mcp: edit ${i} of router_connection_edits.cmake does not match "
      "${MCP_ROUTER_HTTP_SRC}/http/server/connection.h exactly once; update the edits for this "
      "MySQL release")
  ENDIF()
  STRING(REPLACE "${MCP_ROUTER_EDIT_${i}_ANCHOR}" "${MCP_ROUTER_EDIT_${i}_TEXT}"
    MCP_CONNECTION_H "${MCP_CONNECTION_H}")
ENDFOREACH()
# Written through CONFIGURE_FILE(COPYONLY), which leaves the file alone when it is unchanged.
FILE(WRITE ${MCP_ROUTER_PATCHED}/connection.h.new "${MCP_CONNECTION_H}")
CONFIGURE_FILE(${MCP_ROUTER_PATCHED}/connection.h.new
  ${MCP_ROUTER_PATCHED}/http/server/connection.h COPYONLY)
SET_PROPERTY(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
  ${MCP_ROUTER_HTTP_SRC}/http/server/connection.h
  ${CMAKE_CURRENT_SOURCE_DIR}/router_connection_edits.cmake)

# The sources and settings of http_server_lib_object (router/src/http/src/CMakeLists.txt).
ADD_LIBRARY(mcp_http_server_object OBJECT
  ${MCP_ROUTER_HTTP_SRC}/http/server/bind.cc
  ${MCP_ROUTER_HTTP_SRC}/http/server/request.cc
  ${MCP_ROUTER_HTTP_SRC}/http/server/server.cc
  ${MCP_ROUTER_HTTP_SRC}/http/http_server_context.cc
  ${MCP_ROUTER_HTTP_SRC}/http_auth.cc
  ${MCP_ROUTER_HTTP_SRC}/http_server_component.cc
  ${MCP_ROUTER_HTTP_SRC}/http_request_router.cc
)
TARGET_INCLUDE_DIRECTORIES(mcp_http_server_object BEFORE PRIVATE ${MCP_ROUTER_PATCHED})
TARGET_COMPILE_DEFINITIONS(mcp_http_server_object PRIVATE
  $<TARGET_PROPERTY:http_server_lib_object,COMPILE_DEFINITIONS>)
TARGET_LINK_LIBRARIES(mcp_http_server_object PUBLIC http_server_lib_interface)
TARGET_COMPILE_FEATURES(mcp_http_server_object PUBLIC cxx_std_20)
SET_TARGET_PROPERTIES(mcp_http_server_object PROPERTIES POSITION_INDEPENDENT_CODE ON)
ADD_DEPENDENCIES(mcp_http_server_object http_server_lib_object)

SET(MCP_ROUTER_LIBRARIES
  mcp_http_server_object
  http_common_object
  http_auth_realm_lib_object
  http_auth_backend_lib_object
  io_component_object
  harness-library_object
  harness_stdx_object
  harness_tls_object
  harness_net_ts
  router_utils_object
  clientlib mytime strings vio mysys
)

ADD_DEFINITIONS(-DLOG_COMPONENT_TAG="component_mcp")
ADD_DEFINITIONS(-DROUTER_UTILS_STATIC_DEFINE)

MYSQL_ADD_COMPONENT(mcp
  component_mcp.cc
  LINK_LIBRARIES PRIVATE
    ${MCP_ROUTER_LIBRARIES}
    extra::rapidjson
    OpenSSL::SSL
    OpenSSL::Crypto
  MODULE_ONLY
)

IF(UNIX AND NOT APPLE)
  TARGET_LINK_OPTIONS(component_mcp PRIVATE "-Wl,--exclude-libs,ALL")
ENDIF()

TARGET_INCLUDE_DIRECTORIES(component_mcp BEFORE PRIVATE ${MCP_ROUTER_PATCHED})
TARGET_INCLUDE_DIRECTORIES(component_mcp PRIVATE
  ${CMAKE_SOURCE_DIR}/router/src/router/include
  ${CMAKE_SOURCE_DIR}/router/src/harness/include
  ${CMAKE_SOURCE_DIR}/router/src/http/src
  ${CMAKE_SOURCE_DIR}/router/src/http/include
  ${CMAKE_BINARY_DIR}/router
  ${CMAKE_BINARY_DIR}/router/include
  ${CMAKE_BINARY_DIR}/router/src/harness/include
  ${CMAKE_BINARY_DIR}/router/src/router/include
)
ADD_DEPENDENCIES(component_mcp GenError)
