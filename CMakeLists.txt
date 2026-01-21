INCLUDE_DIRECTORIES(SYSTEM ${BOOST_PATCHES_DIR} ${BOOST_INCLUDE_DIR})
INCLUDE_DIRECTORIES(SYSTEM "${MYSQL_SOURCE_DIR}/include")
INCLUDE_DIRECTORIES(SYSTEM "${MYSQL_BUILD_DIR}/include")

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
