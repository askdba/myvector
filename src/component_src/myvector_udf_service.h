#ifndef MYVECTOR_UDF_SERVICE_H
#define MYVECTOR_UDF_SERVICE_H

#include <mysql/components/my_service.h>
#include <mysql/components/component_implementation.h>
#include <mysql/components/services/udf_metadata.h>

namespace myvector_component {

class MyVectorUdfService : public mysql::mysql_service_base {
public:
    // Service interface methods
    const char* get_name() const override { return "MyVector UDF Service"; }
    const char* get_description() const override { return "Registers MyVector User-Defined Functions."; }
    uint32_t get_version() const override { return 1; }

    // Custom methods for registering/deregistering UDFs
    virtual int register_udfs(SERVICE_TYPE(mysql_udf_metadata)* udf_metadata_service) = 0;
    virtual int deregister_udfs(SERVICE_TYPE(mysql_udf_metadata)* udf_metadata_service) = 0;

protected:
    ~MyVectorUdfService() = default;
};

SERVICE_INTERFACE_VERSION(MyVectorUdfService, 1);

} // namespace myvector_component

#endif // MYVECTOR_UDF_SERVICE_H
