#include <mysql/components/component_implementation.h>
#include <mysql/components/my_service.h>
#include <mysql/components/services/mysql_string.h>
#include <mysql/components/services/query_rewrite.h>
#include "myvector.h"
#include "my_inttypes.h"
#include "my_thread.h"

// Placeholder for the actual query rewriting function, which currently resides in myvector.cc
// For component integration, this function should ideally be exposed by a separate library or be part of the component's own utility functions.
extern bool myvector_query_rewrite(const std::string& original_query, std::string* rewritten_query);

namespace myvector_component {

class QueryRewriterService : public mysql::Query_rewriter_service {
public:
    const char* get_name() const override { return "MyVector Query Rewriter"; }

    const char* get_description() const override {
        return "Rewrites MyVector related queries.";
    }

    uint32_t get_version() const override { return 1; }

    int rewrite_query(const Query_rewrite_request* request,
                      Query_rewrite_response* response) override {
        std::string rewritten_query_str;
        if (myvector_query_rewrite(request->original_query.str,
                                   &rewritten_query_str)) {
            response->rewritten_query = rewritten_query_str;
            return 0; // Success
        }
        return 1; // No rewrite or error
    }
};

static QueryRewriterService s_query_rewriter_service;

SERVICE_REGISTRATION(myvector_query_rewriter_service, &s_query_rewriter_service);

} // namespace myvector_component
