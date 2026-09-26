/* Component implementation of MyVector's pre-parse query rewrite (the inline
 * `col MYVECTOR(...)` DDL annotation and `WHERE MYVECTOR_IS_ANN(...)`), via
 * the real MySQL component service framework.
 *
 * This file previously targeted a service (`mysql/components/services/
 * query_rewrite.h`, a `mysql::Query_rewriter_service` interface) that never
 * existed in MySQL server source -- CMakeLists.txt's EXISTS guard on that
 * header was always false on every real MySQL 8.4/9.7/26.7 checkout, so this
 * file was never compiled into any real build. It also used this project's
 * own internal `SERVICE_REGISTRATION` shim (mysql_component_service_base.h),
 * which is a no-op stub for MyVector's own internal C++ interfaces (see
 * myvector_binlog_service.h) -- not a call into MySQL's actual component
 * registry. Even with a real header, the old code would have compiled and
 * linked but never been invoked by the server: nothing here actually
 * registered with MySQL's component framework. See myvector#144.
 *
 * The real, current extension point for pre-parse query rewrite in a
 * component (the modern replacement for the plugin's Audit Plugin
 * MYSQL_AUDIT_PARSE_PREPARSE hook -- see myvector_sql_preparse() in
 * src/myvector_plugin.cc, which this mirrors) is the Event Tracking Parse
 * service: sql/sql_query_rewrite.cc's invoke_pre_parse_rewrite_plugins()
 * calls mysql_event_tracking_parse_notify() for every query, which fans out
 * to every component that PROVIDES/IMPLEMENTS the event_tracking_parse
 * service; if the returned flags carry
 * EVENT_TRACKING_PARSE_REWRITE_QUERY_REWRITTEN, the server swaps in the
 * rewritten query text before parsing. Documented, official consumer
 * pattern: include/mysql/components/util/event_tracking/
 * event_tracking_parse_consumer_helper.h (see the
 * EVENT_TRACKING_PARSE_CONSUMER_EXAMPLE in its header comment).
 */
#include <mysql/components/component_implementation.h>
#include <mysql/components/util/event_tracking/event_tracking_parse_consumer_helper.h>
#include <mysql/service_mysql_alloc.h>

#include <cstring>
#include <string>

#include "my_inttypes.h"
#include "my_sys.h"  /* MY_WME */
#include "myvector.h"

extern bool myvector_query_rewrite(const std::string& original_query,
                                   std::string* rewritten_query);

namespace Event_tracking_implementation {

/* Only interested in the pre-parse subevent; postparse is filtered out. */
mysql_event_tracking_parse_subclass_t
    Event_tracking_parse_implementation::filtered_sub_events =
        EVENT_TRACKING_PARSE_POSTPARSE;

bool Event_tracking_parse_implementation::callback(
    mysql_event_tracking_parse_data* data) {
    if (!data || data->event_subclass != EVENT_TRACKING_PARSE_PREPARSE)
        return false;  // not our subevent (shouldn't happen given the filter)

    std::string original(data->query.str ? data->query.str : "",
                         data->query.length);
    std::string rewritten_query;
    if (myvector_query_rewrite(original, &rewritten_query)) {
        char* rewritten_query_buf = static_cast<char*>(my_malloc(
            PSI_NOT_INSTRUMENTED, rewritten_query.length() + 1, MYF(MY_WME)));
        if (!rewritten_query_buf)
            return true;  // allocation failure -- signal error, don't rewrite
        memcpy(rewritten_query_buf, rewritten_query.c_str(),
              rewritten_query.length() + 1);
        data->rewritten_query->str = rewritten_query_buf;
        data->rewritten_query->length = rewritten_query.length();
        *(data->flags) |= EVENT_TRACKING_PARSE_REWRITE_QUERY_REWRITTEN;
    }

    return false;  // success (whether or not we rewrote)
}

}  // namespace Event_tracking_implementation

/* IMPLEMENTS_SERVICE_EVENT_TRACKING_PARSE(myvector_event_tracking_parse) --
 * i.e. the definition of the service-implementation struct this callback
 * backs -- lives in myvector_component.cc, in the same translation unit as
 * the BEGIN_COMPONENT_PROVIDES block that takes its address via
 * PROVIDES_SERVICE_EVENT_TRACKING_PARSE. A plain `extern` declaration here
 * would work too, but MySQL's own documented consumer example
 * (event_tracking_parse_consumer_helper.h) keeps callback logic and the
 * component's service-provides wiring in one file; splitting them still
 * needs the IMPLEMENTS_SERVICE_* macro co-located with its use, so it's
 * only the callback/filter definitions that live here. */
