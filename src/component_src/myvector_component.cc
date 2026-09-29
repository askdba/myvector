#include <mysql/components/component_implementation.h>
#include <mysql/components/services/udf_metadata.h>
#include <mysql/components/services/udf_registration.h>
#include "myvector.h"
#include "myvector_binlog_service.h"
#include "myvector_udf_service.h"
#ifdef MYVECTOR_HAS_EVENT_TRACKING_PARSE_SERVICE
#include <mysql/components/util/event_tracking/event_tracking_parse_consumer_helper.h>
#endif

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
  // Unregister the UDFs first: MySQL refuses to unregister a UDF that a running
  // statement is using. A failed deinit leaves the component loaded, so it must
  // stay fully functional: with rollback_on_failure the UDFs already removed are
  // registered again, and we return before touching the binlog thread.
  if (mysql_service_udf_registration) {
    if (myvector_component::s_udf_service.deregister_udfs(
            mysql_service_udf_registration, true) != 0) {
      return 1;
    }
  }

  int ret = myvector_component::get_binlog_service().stop_binlog_monitoring();
  if (ret != 0 && mysql_service_udf_registration) {
    // The UDFs are already gone; put them back so the loaded component keeps working.
    myvector_component::s_udf_service.register_udfs(mysql_service_udf_registration);
    return ret;
  }

  myvector_component_udf_metadata = nullptr;
  return ret;
}

/* Defines the service-implementation struct (a plain global, named by
 * SERVICE_IMPLEMENTATION(component, service)) that
 * PROVIDES_SERVICE_EVENT_TRACKING_PARSE below takes the address of -- must
 * be in the same translation unit as that reference. The actual rewrite
 * logic (Event_tracking_parse_implementation::callback/filtered_sub_events)
 * lives in myvector_query_rewrite_service.cc; this just wires it up as the
 * component's provided service. */
#ifdef MYVECTOR_HAS_EVENT_TRACKING_PARSE_SERVICE
IMPLEMENTS_SERVICE_EVENT_TRACKING_PARSE(myvector_event_tracking_parse);
#endif

/* Component provides the Event Tracking Parse service (pre-parse query
 * rewrite: inline MYVECTOR(...) DDL, MYVECTOR_IS_ANN) when the running
 * MySQL version has it -- see myvector_query_rewrite_service.cc and
 * myvector#144. UDF registration is internal, not a provided service. */
BEGIN_COMPONENT_PROVIDES(myvector)
#ifdef MYVECTOR_HAS_EVENT_TRACKING_PARSE_SERVICE
PROVIDES_SERVICE_EVENT_TRACKING_PARSE(myvector_event_tracking_parse),
#endif
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
