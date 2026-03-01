#ifndef MYVECTOR_MYSQL_COMPONENT_SERVICE_BASE_H
#define MYVECTOR_MYSQL_COMPONENT_SERVICE_BASE_H

#include <cstdint>

/**
 * Minimal C++ base for MyVector component services.
 * MySQL's component API uses C structs (BEGIN_SERVICE_DEFINITION etc.);
 * we use this base for shared get_name/get_description/get_version.
 */
namespace mysql {

struct mysql_service_base {
    virtual const char* get_name() const = 0;
    virtual const char* get_description() const = 0;
    virtual uint32_t get_version() const = 0;
    virtual ~mysql_service_base() noexcept = default;
};

}  // namespace mysql

#ifndef SERVICE_INTERFACE_VERSION
#define SERVICE_INTERFACE_VERSION(name, version)  /* version marker for name */
#endif

/* No-op at namespace scope: internal services are used by component init/deinit, not exposed to registry */
#define MYVECTOR_SVC_REG_CONCAT_(a, b) a##b
#define MYVECTOR_SVC_REG_CONCAT(a, b) MYVECTOR_SVC_REG_CONCAT_(a, b)
#define SERVICE_REGISTRATION(service_name, impl_ptr) \
  static const bool MYVECTOR_SVC_REG_CONCAT(MYVECTOR_SVC_REG_, __COUNTER__) [[maybe_unused]] = true

#endif /* MYVECTOR_MYSQL_COMPONENT_SERVICE_BASE_H */
