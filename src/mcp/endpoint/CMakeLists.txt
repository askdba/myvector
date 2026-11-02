# component_mcp: Phase 4 spike of RFC 0001 (askdba/mysql-mcp-server).
# An MCP endpoint inside mysqld on MySQL Router's HTTP server.
# Built in-tree like component_mysql_rest_service; needs the Router HTTP targets, which
# MRS adds (it is configured after this directory; targets resolve at generate time).

DISABLE_MISSING_PROFILE_WARNING()

IF(NOT WITH_ROUTER AND NOT (WITH_COMPONENT_MRS OR (NOT DEFINED WITH_COMPONENT_MRS AND NOT DEFINED WITHOUT_COMPONENT_MRS)))
  MESSAGE(STATUS "component_mcp: needs the Router HTTP libraries (WITH_ROUTER or component_mysql_rest_service)")
  RETURN()
ENDIF()

SET(MCP_ROUTER_LIBRARIES
  http_server_lib_object
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
