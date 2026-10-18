# Related Projects

Other open-source MySQL projects from the same author.

## MySQL MCP Server

[MySQL MCP Server](https://github.com/askdba/mysql-mcp-server) is a fast,
read-only [Model Context Protocol](https://modelcontextprotocol.io) (MCP)
server for MySQL and MariaDB, written in Go. It lets AI assistants such as
Claude Desktop explore your databases, describe schemas and run safe,
row-limited queries.

- **Read-only by design:** only `SELECT`, `SHOW`, `DESCRIBE` and `EXPLAIN`
  are allowed. Write statements and the `mysql` system schema are blocked.
- **Tested on:** MySQL 8.0, 8.4, 9.7 and 26.7, and MariaDB 11.4 and 12.3.
- **Vector tools:** optional `vector_search` and `vector_info` tools for
  MySQL 9.0+ vector columns.
- **Two modes:** MCP over stdio for AI clients, or a REST API.

Install with Homebrew or Docker:

```bash
brew install askdba/tap/mysql-mcp-server
```

```bash
docker pull ghcr.io/askdba/mysql-mcp-server:latest
```

See the [README](https://github.com/askdba/mysql-mcp-server#readme) for
configuration, and the
[releases page](https://github.com/askdba/mysql-mcp-server/releases) for
binaries for macOS, Linux and Windows.
