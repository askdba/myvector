#ifndef MYVECTOR_UDF_SERVICE_H
#define MYVECTOR_UDF_SERVICE_H

#include "mysql_component_service_base.h"
#include <mysql/components/my_service.h>
#include <mysql/components/component_implementation.h>
#include <mysql/components/services/udf_registration.h>

namespace myvector_component {

class MyVectorUdfService : public mysql::mysql_service_base {
public:
    // Service interface methods
    const char* get_name() const override { return "MyVector UDF Service"; }
    const char* get_description() const override { return "Registers MyVector User-Defined Functions."; }
    uint32_t get_version() const override { return 1; }

    // Custom methods for registering/deregistering UDFs (uses udf_registration service)
    virtual int register_udfs(SERVICE_TYPE(udf_registration)* udf_registration_service) = 0;
    /* Unregisters every UDF; returns non-zero if any could not be removed (for example
     * because a running statement is using it). With rollback_on_failure the UDFs that
     * were removed are registered again, so a failed call leaves the set unchanged. */
    virtual int deregister_udfs(SERVICE_TYPE(udf_registration)* udf_registration_service,
                                bool rollback_on_failure = false) = 0;

protected:
    ~MyVectorUdfService() noexcept override = default;
};

SERVICE_INTERFACE_VERSION(MyVectorUdfService, 1);

/** Reference to the singleton implementation (defined in myvector_udf_service.cc). */
extern MyVectorUdfService& s_udf_service;

} // namespace myvector_component

#endif // MYVECTOR_UDF_SERVICE_H
