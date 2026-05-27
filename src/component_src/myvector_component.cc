#include <mysql/components/component_implementation.h>
#include <mysql/components/services/udf_metadata.h>
#include <mysql/components/services/udf_registration.h>
#include <mysql/components/services/dynamic_loader_service_notification.h>
#include <chrono>
#include <cstring>
#include <thread>
#include "mysql/components/util/event_tracking/event_tracking_parse_consumer_helper.h"
#include "myvector.h"
#include "myvector_binlog_service.h"
#include "myvector_udf_service.h"

/* Required services: populated by framework when component loads */
REQUIRES_SERVICE_PLACEHOLDER(udf_registration);
REQUIRES_SERVICE_PLACEHOLDER(mysql_udf_metadata);

SERVICE_TYPE(mysql_udf_metadata)* myvector_component_udf_metadata = nullptr;

static int myvector_component_init() {
  int ret = 0;
  bool udfs_registered = false;

  if (!mysql_service_udf_registration || !mysql_service_mysql_udf_metadata) {
    return 1;
  }

  myvector_component_udf_metadata = mysql_service_mysql_udf_metadata;

  ret = myvector_component::s_udf_service.register_udfs(
      mysql_service_udf_registration);
  if (ret == 0) {
    udfs_registered = true;
  } else {
    myvector_component::s_udf_service.deregister_udfs(
        mysql_service_udf_registration);
    myvector_component_udf_metadata = nullptr;
  }

  if (ret == 0) {
    ret = myvector_component::get_binlog_service().start_binlog_monitoring();
    if (ret != 0 && udfs_registered) {
      myvector_component::s_udf_service.deregister_udfs(
          mysql_service_udf_registration);
      myvector_component_udf_metadata = nullptr;
    }
  }

  return ret;
}

static int myvector_component_deinit() {
  int ret = myvector_component::get_binlog_service().stop_binlog_monitoring();

  if (mysql_service_udf_registration) {
    ret |= myvector_component::s_udf_service.deregister_udfs(
        mysql_service_udf_registration);
  }

  myvector_component_udf_metadata = nullptr;
  return ret;
}

/* Define the service implementation structs (must be in same TU as PROVIDES) */
IMPLEMENTS_SERVICE_EVENT_TRACKING_PARSE(myvector);

/* Stop the binlog thread before MySQL checks the event_tracking_parse reference
 * count during UNINSTALL. The binlog thread's server-side THD holds a reference
 * that is never released while it is blocked in COM_BINLOG_DUMP. */
static mysql_service_status_t myvector_unload_notify(const char **services,
                                                      unsigned int count) {
  for (unsigned int i = 0; i < count; ++i) {
    if (strcmp(services[i], "event_tracking_parse.myvector") == 0) {
      myvector_component::get_binlog_service().stop_binlog_monitoring();
      // After mysql_close() the server-side binlog THD cleanup (which
      // releases the event_tracking_parse reference) is asynchronous.
      // Give the server ~300 ms to destroy the THD before dynamic_loader
      // checks the reference count for UNINSTALL COMPONENT.
      std::this_thread::sleep_for(std::chrono::milliseconds(300));
      break;
    }
  }
  return false;
}

BEGIN_SERVICE_IMPLEMENTATION(myvector,
                             dynamic_loader_services_unload_notification)
myvector_unload_notify END_SERVICE_IMPLEMENTATION();

BEGIN_COMPONENT_PROVIDES(myvector)
PROVIDES_SERVICE_EVENT_TRACKING_PARSE(myvector),
PROVIDES_SERVICE(myvector, dynamic_loader_services_unload_notification),
END_COMPONENT_PROVIDES();

/* Dependencies */
BEGIN_COMPONENT_REQUIRES(myvector)
REQUIRES_SERVICE(udf_registration),
REQUIRES_SERVICE(mysql_udf_metadata),
END_COMPONENT_REQUIRES();

/* Metadata */
BEGIN_COMPONENT_METADATA(myvector)
METADATA("mysql.author", "p3io"),
METADATA("mysql.license", "GPL"),
METADATA("mysql.component.version", "1.3.0"),
END_COMPONENT_METADATA();

/* Component declaration */
DECLARE_COMPONENT(myvector, "file://myvector")
myvector_component_init, myvector_component_deinit END_DECLARE_COMPONENT();

/* Library entry point */
DECLARE_LIBRARY_COMPONENTS &COMPONENT_REF(myvector)
END_DECLARE_LIBRARY_COMPONENTS
