# `mysql_mcp` SQL tool pack

Stored procedures and views that implement the tool logic of
[mysql-mcp-server](https://github.com/askdba/mysql-mcp-server) once, in SQL, for
MySQL 8.0 through 26.10. The Go server, `component_mcp_guard` and `component_mcp`
call it. See [RFC 0001](https://github.com/askdba/mysql-mcp-server/blob/main/docs/rfc/0001-mysql-component-architecture.md), §5.1.

**License:** everything in this directory is licensed under the Apache License 2.0
(see [`LICENSE`](LICENSE)), unlike the rest of this repository (GPLv2). The SQL files
are standalone scripts that are not linked into any component, and mysql-mcp-server
(Apache-2.0) vendors them.

**Reserved names:** `component_mcp_guard` registers the dynamic privileges `MCP_USER` and
`MCP_ADMIN` and unregisters them on `UNINSTALL COMPONENT`. MySQL's privilege registry has
no owner, so other components must not register these names.

Status: Phase 0 placeholder. The first procedures land in Phase 1.
