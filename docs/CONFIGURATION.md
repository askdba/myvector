# MyVector Configuration Reference

This document describes the MyVector configuration file used for the binlog connection (online index updates). For an overview of online updates, see [ONLINE_INDEX_UPDATES.md](ONLINE_INDEX_UPDATES.md).

## Configuration File Location

The config file path is set via the `myvector_config_file` system variable:

```sql
SET GLOBAL myvector_config_file = '/path/to/myvector.cnf';
```

Or in `my.cnf`:

```ini
[mysqld]
myvector_config_file=/path/to/myvector.cnf
```

**Default:** `myvector.cnf` (relative to the server's working directory, typically the data directory)

## Security: File Permissions

**The config file contains database credentials.** MyVector enforces the following at load time (Unix/Linux/macOS only):

| Requirement | Description |
|-------------|-------------|
| **Permissions** | File must be mode `0600` or `0400` (owner read/write or owner read-only). Group and world must have no access. |
| **Ownership** | File must be owned by the MySQL process user (e.g. `mysql`). When MySQL runs as root (e.g. in Docker), ownership is not enforced. |

If the file has insecure permissions or wrong ownership, the plugin **refuses to load** it and logs an error. The binlog thread will not start with credentials from that file.

**Missing file:** If the config file does not exist, the plugin proceeds with empty credentials (same as before the security checks). Deployments that do not use online updates do not need to create the file.

**Example:**

```bash
# Create config file
sudo touch /etc/myvector.cnf
sudo chown mysql:mysql /etc/myvector.cnf
sudo chmod 600 /etc/myvector.cnf
# Edit as mysql user
sudo -u mysql nano /etc/myvector.cnf
```

**Windows:** The plugin rejects config files that have no DACL (full access to everyone) or that grant the Everyone (world) SID read access. It also checks that the file can be opened and read; if the file is missing, it proceeds with empty credentials (same as Unix). Store the file in a secure location and restrict ACLs to the MySQL process account.

## Configuration Options

All options use `key=value` format, one per line. Lines starting with `#` are comments. Options are comma-separated or newline-separated.

| Option | Required | Default | Description |
|--------|----------|---------|-------------|
| `myvector_user_id` | Yes* | (empty) | MySQL user for the binlog connection. |
| `myvector_user_password` | Yes* | (empty) | Password for the binlog user. |
| `myvector_host` | No | (empty) | Host for TCP connection. Use empty for socket-only. |
| `myvector_port` | No | 0 | Port for TCP connection. Ignored if using socket. |
| `myvector_socket` | No | (empty) | Unix socket path for local connection. |

\*Required when using online updates. If empty, the binlog thread will fail to connect.

### Validation Rules

- **myvector_user_id**: Non-empty string. User must have `REPLICATION CLIENT` and `SELECT` on relevant tables.
- **myvector_user_password**: Any string. Stored in plain text in the config file—protect with file permissions.
- **myvector_host**: Hostname or IP. Use `127.0.0.1` for local TCP; empty for socket.
- **myvector_port**: Integer 1–65535. Default 0 when using socket.
- **myvector_socket**: Path to MySQL socket. Use for local connections without TCP.

### Connection Mode

- **Socket:** Set `myvector_socket` (e.g. `/tmp/mysql.sock`). `myvector_host` and `myvector_port` are ignored.
- **TCP:** Set `myvector_host` and optionally `myvector_port` (default 3306). Leave `myvector_socket` empty.

## Examples

### Local socket connection

```ini
myvector_user_id=myvector_binlog
myvector_user_password=your_password
myvector_socket=/tmp/mysql.sock
```

### Local TCP connection

```ini
myvector_user_id=myvector_binlog
myvector_user_password=your_password
myvector_host=127.0.0.1
myvector_port=3306
```

### Docker (typical)

```ini
myvector_user_id=root
myvector_user_password=myvector
myvector_host=127.0.0.1
myvector_port=3306
```

## TLS/SSL

TLS/SSL options for the binlog connection are not currently configurable via the config file. The connection uses MySQL's default behavior. For encrypted connections, use a socket or ensure MySQL is configured for TLS on the server-side.

## Related

- [ONLINE_INDEX_UPDATES.md](ONLINE_INDEX_UPDATES.md) — How to enable and use online index updates
- [DOCKER_IMAGES.md](DOCKER_IMAGES.md) — Docker-specific configuration
