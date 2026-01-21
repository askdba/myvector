INCLUDE_DIRECTORIES(SYSTEM ${BOOST_PATCHES_DIR} ${BOOST_INCLUDE_DIR})
if(DEFINED MYSQL_SOURCE_DIR AND EXISTS "${MYSQL_SOURCE_DIR}/include")
  INCLUDE_DIRECTORIES(SYSTEM "${MYSQL_SOURCE_DIR}/include")
endif()
if(DEFINED MYSQL_BUILD_DIR AND EXISTS "${MYSQL_BUILD_DIR}/include")
  INCLUDE_DIRECTORIES(SYSTEM "${MYSQL_BUILD_DIR}/include")
endif()

# Source files - paths are relative to this CMakeLists.txt location
# When building within MySQL source tree, files should be in plugin/myvector/
MYSQL_ADD_PLUGIN(myvector
  myvector_plugin.cc
  myvector.cc
  myvector_binlog.cc
  myvectorutils.cc
  LINK_LIBRARIES ${LIBMYSQLCLIENT}
  MODULE_ONLY
  MODULE_OUTPUT_NAME "myvector"
  )
