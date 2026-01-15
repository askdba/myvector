INCLUDE_DIRECTORIES(SYSTEM ${BOOST_PATCHES_DIR} ${BOOST_INCLUDE_DIR} include)
MYSQL_ADD_PLUGIN(myvector
  src/myvector_plugin.cc
  src/myvector.cc
  src/myvector_binlog.cc
  LINK_LIBRARIES ${LIBMYSQLCLIENT}
  MODULE_ONLY
  MODULE_OUTPUT_NAME "myvector"
  )
