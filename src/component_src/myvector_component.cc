#include <mysql/components/component_implementation.h>
#include <mysql/plugin.h>
#include <mysql/components/my_service.h>
#include <mysql/components/services/udf_metadata.h>
#include "myvector_binlog_service.h" // Include the new binlog service header
#include "myvector_udf_service.h"    // Include the new UDF service header

extern REQUIRES_SERVICE_PLACEHOLDER(mysql_udf_metadata);
my_service<SERVICE_TYPE(mysql_udf_metadata)>* h_udf_metadata_service;
extern class myvector_component::MyVectorBinlogService s_binlog_service;
extern class myvector_component::MyVectorUdfService s_udf_service;

// Component initialization
static int myvector_component_init() {
    int ret = 0;
    bool udfs_registered = false;
    h_udf_metadata_service = new my_service<SERVICE_TYPE(mysql_udf_metadata)>(
        "mysql_udf_metadata", nullptr); // No registry needed for component services

    if (h_udf_metadata_service->is_valid()) {
        ret = myvector_component::s_udf_service.register_udfs(
            h_udf_metadata_service->get_service());
        if (ret == 0)
            udfs_registered = true;
    } else {
        // Handle error: UDF metadata service not available
        ret = 1;
    }

    if (ret == 0) {
        // Start the binlog monitoring service
        ret = myvector_component::s_binlog_service.start_binlog_monitoring();
        if (ret != 0 && udfs_registered) {
            myvector_component::s_udf_service.deregister_udfs(
                h_udf_metadata_service->get_service());
            udfs_registered = false;
        }
    }

    if (ret != 0) {
        delete h_udf_metadata_service;
        h_udf_metadata_service = nullptr;
    }

    return ret;
}

// Component deinitialization
static int myvector_component_deinit() {
    int ret = 0;
    // Stop the binlog monitoring service
    ret |= myvector_component::s_binlog_service.stop_binlog_monitoring();

    if (h_udf_metadata_service && h_udf_metadata_service->is_valid()) {
        ret |= myvector_component::s_udf_service.deregister_udfs(
            h_udf_metadata_service->get_service());
    }
    delete h_udf_metadata_service;
    h_udf_metadata_service = nullptr;
    return ret;
}

// Component descriptor
extern class myvector_component::QueryRewriterService s_query_rewriter_service;

mysql_declare_component(myvector)
{
    MYSQL_COMPONENT_INTERFACE_VERSION,
    "MyVector Component",
    "myvector/p3io",
    "Vector Storage & Search Component for MySQL",
    myvector_component_init,
    myvector_component_deinit,
    nullptr, // No global component services yet
    {
        &s_query_rewriter_service,
        &s_binlog_service, // Register the binlog service
        &s_udf_service,    // Register the UDF service
        nullptr
    }
}
mysql_declare_component_end;
