/*
   This program is free software; you can redistribute it and/or modify
   it under the terms of the GNU General Public License, version 2.0,
   as published by the Free Software Foundation.

   This program is distributed in the hope that it will be useful,
   but WITHOUT ANY WARRANTY; without even the implied warranty of
   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
   GNU General Public License, version 2.0, for more details.
*/

// component_mcp_guard: server-side guard for mysql-mcp-server sessions.
//
// Phase 0 (RFC 0001, askdba/mysql-mcp-server docs/rfc): build and lifecycle skeleton.
// It registers the MCP_USER and MCP_ADMIN dynamic privileges on install and removes
// them on uninstall. Policy enforcement (event tracking), system and status variables
// and performance_schema tables follow in Phase 2.

#include <mysql/components/component_implementation.h>
#include <mysql/components/services/dynamic_privilege.h>

#include <cstring>

REQUIRES_SERVICE_PLACEHOLDER(dynamic_privilege_register);

namespace {

// MCP_USER marks accounts used by mysql-mcp-server; MCP_ADMIN may change mcp.* policy.
// Both names are reserved for this component (see sql/mcp/README.md).
constexpr const char *kPrivileges[] = {"MCP_USER", "MCP_ADMIN"};
constexpr size_t kPrivilegeCount = sizeof(kPrivileges) / sizeof(kPrivileges[0]);

// Which privileges this load of the component registered. The server registry has no
// owner or reference count (registering an existing name reports success), so, like
// Oracle's component_mysql_rest_service, unregister only what this component's own
// calls registered, and never a name whose registration failed.
bool registered[kPrivilegeCount] = {};

bool unregister_privileges() {
  bool failed = false;
  for (size_t i = 0; i < kPrivilegeCount; ++i) {
    if (!registered[i]) continue;
    if (mysql_service_dynamic_privilege_register->unregister_privilege(
            kPrivileges[i], std::strlen(kPrivileges[i]))) {
      failed = true;
      continue;
    }
    registered[i] = false;
  }
  return failed;
}

mysql_service_status_t mcp_guard_init() {
  for (size_t i = 0; i < kPrivilegeCount; ++i) {
    if (mysql_service_dynamic_privilege_register->register_privilege(
            kPrivileges[i], std::strlen(kPrivileges[i]))) {
      unregister_privileges();  // roll back only the ones registered above
      return 1;
    }
    registered[i] = true;
  }
  return 0;
}

mysql_service_status_t mcp_guard_deinit() {
  return unregister_privileges() ? 1 : 0;
}

}  // namespace

BEGIN_COMPONENT_PROVIDES(mcp_guard)
END_COMPONENT_PROVIDES();

BEGIN_COMPONENT_REQUIRES(mcp_guard)
REQUIRES_SERVICE(dynamic_privilege_register),
END_COMPONENT_REQUIRES();

BEGIN_COMPONENT_METADATA(mcp_guard)
METADATA("mysql.author", "askdba"),
METADATA("mysql.license", "GPL"),
METADATA("mcp_guard.phase", "0"),
END_COMPONENT_METADATA();

DECLARE_COMPONENT(mcp_guard, "file://component_mcp_guard")
mcp_guard_init, mcp_guard_deinit END_DECLARE_COMPONENT();

DECLARE_LIBRARY_COMPONENTS &COMPONENT_REF(mcp_guard)
END_DECLARE_LIBRARY_COMPONENTS
