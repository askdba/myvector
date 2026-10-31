# mysql-mcp: the official MySQL image with the MCP components of RFC 0001
# (askdba/mysql-mcp-server): component_mcp_guard and the mysql_mcp SQL tool pack.
# Published as ghcr.io/askdba/mysql-mcp:<8.4|9.7|26.10> by docker-publish.yml.
# Build context: the repository root, with component_mcp_guard-<arch>.so staged there.
# MYSQL_VERSION (required, no default) must be the exact release the component was built
# against, for example 8.4.8: a component built for another patch release can crash the
# server.
ARG MYSQL_VERSION

FROM mysql:${MYSQL_VERSION}
ARG TARGETARCH

LABEL org.opencontainers.image.title="mysql-mcp" \
      org.opencontainers.image.description="MySQL with component_mcp_guard and the mysql_mcp tool pack for mysql-mcp-server" \
      org.opencontainers.image.source="https://github.com/askdba/myvector" \
      org.opencontainers.image.documentation="https://github.com/askdba/myvector/blob/main/docs/MCP_COMPONENTS.md" \
      org.opencontainers.image.licenses="GPL-2.0-only AND Apache-2.0"

# Statically linked against libstdc++/libgcc and built in oraclelinux:9, this image's base.
COPY component_mcp_guard-${TARGETARCH}.so /usr/lib64/mysql/plugin/component_mcp_guard.so
COPY sql/mcp/mysql_mcp.sql sql/mcp/uninstall.sql sql/mcp/LICENSE /usr/share/mysql-mcp/
COPY docker/mysql-mcp/mysql-mcp-init.sh /docker-entrypoint-initdb.d/50-mysql-mcp.sh
COPY docker/mysql-mcp/mysql-mcp-healthcheck.sh /usr/local/bin/mysql-mcp-healthcheck

# checkov:skip=CKV_DOCKER_3:MySQL entrypoint needs root for initialization
# Healthy only after first-start setup, on the server's port, without touching root's
# login (see the script).
HEALTHCHECK --interval=30s --timeout=5s --retries=5 CMD ["/usr/local/bin/mysql-mcp-healthcheck"]
