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
 *
 * KNOWN LIMITATION (myvector#155): once this service is registered,
 * `UNINSTALL COMPONENT` fails with ERROR 3540 ("Unregistration of service
 * implementation ... failed") whenever ANY OTHER session has dispatched at
 * least one query since this component was installed and is STILL
 * CONNECTED -- confirmed via isolated repro with no binlog listener and no
 * online index involved at all: a second, idle connection that has run one
 * query is sufficient; UNINSTALL succeeds the instant that connection
 * actually disconnects (deterministic, not a race). Root cause (traced into
 * MySQL 8.4.8 source): every THD lazily acquires and holds a registry
 * reference to matching event_tracking_parse providers on its first parse
 * dispatch (sql/reference_caching_setup.cc's Event_reference_caching_cache),
 * released only when that THD is destroyed; the unload-notification path
 * only refreshes the *current* THD (the one running UNINSTALL), not any
 * other live session's cache. This is a MySQL server-side limitation, not a
 * bug in this component or in the binlog listener's shutdown path (which is
 * correct: mysql_binlog_close()+mysql_close() on every exit path,
 * synchronously joined before deinit returns).
 *
 * In practice the binlog listener (myvector_binlog_service.cc) is almost
 * always the trigger, simply because it is the one connection in a typical
 * deployment that stays alive indefinitely (any `online=Y` index keeps it
 * running). Workaround: DROP all `online=Y` indexes (stopping the binlog
 * listener) -- and ensure no other client connection is idling with a
 * query already dispatched -- before UNINSTALL COMPONENT. See #155 for the
 * full investigation and suggested next steps (this looks like it may be
 * worth reporting upstream to MySQL, since it would affect any well-behaved
 * component providing this service type).
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
