-- This program is free software; you can redistribute it and/or modify it under the terms of
-- the GNU General Public License, version 2.0, as published by the Free Software Foundation.
-- This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
-- without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
-- See the GNU General Public License, version 2.0, for more details.
--
-- component_mcp token table (RFC 0001 Phase 5): bearer tokens mapped to accounts.
-- Run once as an administrator, before or after INSTALL COMPONENT 'file://component_mcp'.
--
--   mysql.mcp_tokens        token SHA-256 (hex) -> account (user@host), optional expiry.
--   'mysql.mcp'@'localhost' locked account the endpoint impersonates to read the table; its
--                           only grant is SELECT on it. Nobody can log in as it.
--
-- The table is in the mysql schema: component_mcp_guard denies it to MCP sessions
-- (mcp.deny_system_schemas), and ordinary accounts have no grants there. Store only the
-- SHA-256 of a token, never the token, e.g.
--
--   INSERT INTO mysql.mcp_tokens (token_sha256, account, expires_at, comment)
--   VALUES (SHA2('<token>', 256), 'mcp@%', NOW() + INTERVAL 90 DAY, 'claude desktop');
--
-- (SHA2() puts the token in the statement text, and so in logs that record statements;
-- hashing it outside MySQL avoids that.) Revoke with DELETE; changes apply to the next request.

CREATE TABLE IF NOT EXISTS mysql.mcp_tokens (
  token_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY
    COMMENT 'lowercase hex SHA-256 of the bearer token',
  account VARCHAR(288) NOT NULL COMMENT 'user@host the token maps to',
  expires_at DATETIME NULL COMMENT 'server time; NULL: no expiry',
  comment VARCHAR(255) NOT NULL DEFAULT '',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT mcp_tokens_hex CHECK (token_sha256 REGEXP '^[0-9a-f]{64}$')
) ENGINE=InnoDB COMMENT='component_mcp: bearer token hashes mapped to accounts';

CREATE USER IF NOT EXISTS 'mysql.mcp'@'localhost' ACCOUNT LOCK;
GRANT SELECT ON mysql.mcp_tokens TO 'mysql.mcp'@'localhost';
