/*
 * Pre-parse query rewrite for the myvector component.
 *
 * Provides the event_tracking_parse service so MySQL calls this component
 * before each query is parsed.  On PREPARSE events we call
 * myvector_query_rewrite() — the same function used by the plugin audit hook
 * in myvector_plugin.cc — to rewrite MYVECTOR_IS_ANN / MYVECTOR_SEARCH
 * syntax into HNSW index lookups.
 *
 * When the query is not rewritten the function returns false immediately
 * and MySQL continues to the next parse-event consumer unchanged.
 */

#include "mysql/components/util/event_tracking/event_tracking_parse_consumer_helper.h"
#include "myvector.h"

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <string>

extern bool myvector_query_rewrite(const std::string &original,
                                   std::string *rewritten);

namespace Event_tracking_implementation {

/* Filter out POSTPARSE — we only need to act before parsing. */
mysql_event_tracking_parse_subclass_t
    Event_tracking_parse_implementation::filtered_sub_events =
        EVENT_TRACKING_PARSE_POSTPARSE;

bool Event_tracking_parse_implementation::callback(
    mysql_event_tracking_parse_data *data) {
  if (data->event_subclass != EVENT_TRACKING_PARSE_PREPARSE) return false;

  std::string original(data->query.str, data->query.length);
  std::string rewritten;
  if (!myvector_query_rewrite(original, &rewritten)) return false;

  // MySQL's sql_query_rewrite.cc frees this buffer via my_free(), which expects a
  // 32-byte PSI header at ptr[-32] with m_magic==1234 at bytes [4..7].
  // my_malloc() is not exported from mysqld to external components, so we
  // prepend the PSI header manually using plain malloc().
  // Layout from mysql/psi/mysql_memory.h (stable since MySQL 8.0):
  //   [0..3]  PSI_memory_key  m_key  = 0 (PSI_NOT_INSTRUMENTED)
  //   [4..7]  uint            m_magic = 1234 (PSI_MEMORY_MAGIC)
  //   [8..15] size_t          m_size  = user allocation size
  //   [16..31] padding / PSI_thread* m_owner = nullptr
  static const size_t kPsiHdrSize = 32;
  static const uint32_t kPsiMagic = 1234;
  const size_t alloc_size = rewritten.length() + 1;
  char *raw = static_cast<char *>(malloc(kPsiHdrSize + alloc_size));
  if (!raw) return true;
  memset(raw, 0, kPsiHdrSize);
  *reinterpret_cast<uint32_t *>(raw + 4) = kPsiMagic;
  *reinterpret_cast<size_t *>(raw + 8) = alloc_size;
  char *buf = raw + kPsiHdrSize;
  memcpy(buf, rewritten.c_str(), alloc_size);
  data->rewritten_query->str = buf;
  data->rewritten_query->length = rewritten.length();
  *data->flags |= EVENT_TRACKING_PARSE_REWRITE_QUERY_REWRITTEN;
  return false;
}

}  // namespace Event_tracking_implementation
