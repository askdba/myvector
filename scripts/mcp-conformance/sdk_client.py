"""MCP conformance client for component_mcp: the official Python SDK (mcp, pinned by the caller).

Usage: python sdk_client.py <url> <bearer token>

Runs a full Streamable HTTP session through the SDK's own client (initialize, tools/list,
tools/call, ping, close) and prints one "PASS <name>" or "FAIL <name> -- <detail>" line per
check. Every HTTP exchange is recorded with an httpx2 response hook, so the transport itself is
checked too: later requests carry Mcp-Session-Id and MCP-Protocol-Version, and closing the
client ends the session with DELETE (204).
"""

import asyncio
import json
import sys

import httpx2
import mcp.client.streamable_http as streamable
from mcp import ClientSession

URL, TOKEN = sys.argv[1], sys.argv[2]
exchanges = []  # (method, session id sent, protocol version sent, status, session id received)
failed = 0


def check(name, ok, detail=""):
    global failed
    if ok:
        print(f"PASS  sdk: {name}")
    else:
        failed += 1
        print(f"FAIL  sdk: {name}  -- {detail}")


async def record(response):
    request = response.request
    exchanges.append((request.method, request.headers.get("Mcp-Session-Id"),
                      request.headers.get("MCP-Protocol-Version"), response.status_code,
                      response.headers.get("Mcp-Session-Id")))


def text(result):
    return result.content[0].text if result.content else ""


async def main():
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"},
                              event_hooks={"response": [record]}, timeout=30)
    async with http:
        async with streamable.streamable_http_client(URL, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                check("initialize negotiates 2025-11-25", init.protocol_version == "2025-11-25",
                      init.protocol_version)
                check("server name", init.server_info.name == "component_mcp", init.server_info.name)

                tools = {t.name: t for t in (await session.list_tools()).tools}
                check("tools/list: the six core tools",
                      sorted(tools) == sorted(["list_databases", "list_tables", "describe_table",
                                               "run_query", "ping", "server_info"]), sorted(tools))
                ro = tools.get("list_tables")
                check("tools carry readOnlyHint",
                      ro is not None and ro.annotations is not None and ro.annotations.read_only_hint,
                      ro and ro.annotations)

                r = await session.call_tool("run_query", {"sql": "SELECT i FROM d1.t"})
                check("tools/call run_query",
                      not r.is_error and json.loads(text(r))["rows"] == [{"i": "1"}], text(r))
                r = await session.call_tool("list_tables", {"database": "d1"})
                check("tools/call list_tables", not r.is_error and '"TABLE_NAME":"t"' in text(r), text(r))
                r = await session.call_tool("server_info", {})
                check("tools/call server_info",
                      not r.is_error and json.loads(text(r)).get("current_user") == "mcp@%", text(r))
                r = await session.call_tool("run_query", {"sql": "INSERT INTO d1.t VALUES (2)"})
                check("a refused statement is a tool error", r.is_error and "mcp.read_only" in text(r),
                      text(r))
                await session.send_ping()
                check("ping", True)

    posts = [e for e in exchanges if e[0] == "POST"]
    sid = posts[0][4] if posts else None
    check("initialize returned an Mcp-Session-Id", bool(sid), exchanges[:1])
    check("later requests carry the session id and version",
          len(posts) > 1 and all(e[1] == sid and e[2] == "2025-11-25" for e in posts[1:]),
          posts[1:])
    check("all POSTs answered 200 or 202", all(e[3] in (200, 202) for e in posts), posts)
    deletes = [e for e in exchanges if e[0] == "DELETE"]
    check("closing ends the session with DELETE (204), with the session id and version",
          len(deletes) == 1 and deletes[0][1] == sid and deletes[0][2] == "2025-11-25"
          and deletes[0][3] == 204, deletes)
    gets = [e for e in exchanges if e[0] == "GET"]
    # The pinned SDK opens the optional server-to-client stream after initialize; it must have
    # been tried, with the headers, and refused.
    check("no server-to-client stream: the SDK's GET (with the session id and version) gets 405",
          len(gets) >= 1 and all(e[1] == sid and e[2] == "2025-11-25" and e[3] == 405 for e in gets),
          gets)


asyncio.run(main())
sys.exit(1 if failed else 0)
