#include <mysql/components/component_implementation.h>
#include <mysql/components/services/udf_metadata.h>
#include <mysql/components/services/udf_registration.h>
#include <cstring>
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
namespace Event_tracking_implementation {

mysql_event_tracking_parse_subclass_t
    Event_tracking_parse_implementation::filtered_sub_events = 0;

bool Event_tracking_parse_implementation::callback(
    mysql_event_tracking_parse_data *data [[maybe_unused]]) {
  // The parse service is primarily for event tracking/monitoring.
  // Query rewriting should be handled by the plugin hooks in myvector_plugin.cc
  // which use the AUDIT interface. The component's parse service provides
  // the interface but doesn't perform the actual rewriting here to avoid
  // conflicts and ensure stability.
  return false;  // Success - no error
}

}  // namespace Event_tracking_implementation

IMPLEMENTS_SERVICE_EVENT_TRACKING_PARSE(myvector);

BEGIN_COMPONENT_PROVIDES(myvector)
PROVIDES_SERVICE_EVENT_TRACKING_PARSE(myvector),
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
