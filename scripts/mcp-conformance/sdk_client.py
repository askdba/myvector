"""MCP conformance client for component_mcp: the official Python SDK (mcp, pinned by the caller).

Usage: python sdk_client.py <url> <bearer token>

Runs the SDK's own client in both protocol eras and prints one "PASS <name>" or
"FAIL <name> -- <detail>" line per check:
  1. a session-era Streamable HTTP session (initialize, tools/list, tools/call, ping, close);
  2. the stateless revision 2026-07-28 (server/discover, tools/list, tools/call; no session).
Every HTTP exchange is recorded with an httpx2 response hook, so the transport itself is checked
too: in the session era, later requests carry Mcp-Session-Id and MCP-Protocol-Version and
closing the client ends the session with DELETE (204); in 2026-07-28, every request carries the
version, Mcp-Method and (for tools/call) Mcp-Name headers, and no session id is sent or issued.
"""

import asyncio
import json
import sys

import httpx2
import mcp.client.streamable_http as streamable
from mcp import ClientSession

URL, TOKEN = sys.argv[1], sys.argv[2]
exchanges = []  # (method, session id sent, protocol version sent, status, session id received)
get_answered = None  # asyncio.Event: the SDK's background GET got its response
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
    if request.method == "GET" and get_answered is not None:
        get_answered.set()


def text(result):
    return result.content[0].text if result.content else ""


async def main():
    global get_answered
    get_answered = asyncio.Event()
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
                # The SDK sends its GET from a background task; wait (bounded) for the response, so
                # closing the session cannot cancel it before it is recorded.
                try:
                    await asyncio.wait_for(get_answered.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass  # the check below reports the missing GET

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


async def record_stateless(response):
    request = response.request
    body = json.loads(request.content or b"{}") if request.method == "POST" else {}
    stateless.append({
        "http": request.method, "status": response.status_code,
        "method": body.get("method"), "name": (body.get("params") or {}).get("name"),
        "meta_version": ((body.get("params") or {}).get("_meta") or {}).get(
            "io.modelcontextprotocol/protocolVersion"),
        "version_header": request.headers.get("MCP-Protocol-Version"),
        "method_header": request.headers.get("Mcp-Method"),
        "name_header": request.headers.get("Mcp-Name"),
        "session_sent": request.headers.get("Mcp-Session-Id"),
        "session_received": response.headers.get("Mcp-Session-Id"),
    })


stateless = []


async def main_stateless():
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"},
                              event_hooks={"response": [record_stateless]}, timeout=30)
    async with http:
        async with streamable.streamable_http_client(URL, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                disc = await session.discover()
                check("2026-07-28: server/discover negotiates 2026-07-28",
                      session.protocol_version == "2026-07-28", session.protocol_version)
                check("2026-07-28: discover lists every supported version",
                      disc.supported_versions == ["2026-07-28", "2025-11-25", "2025-06-18",
                                                  "2025-03-26", "2024-11-05"],
                      disc.supported_versions)
                check("2026-07-28: discover carries the cache hints",
                      disc.ttl_ms > 0 and disc.cache_scope == "public", (disc.ttl_ms, disc.cache_scope))
                check("2026-07-28: serverInfo from the result _meta",
                      session.server_info is not None and session.server_info.name == "component_mcp",
                      session.server_info)
                check("2026-07-28: tools capability", session.server_capabilities.tools is not None,
                      session.server_capabilities)

                tools = await session.list_tools()
                check("2026-07-28: tools/list: the six core tools",
                      sorted(t.name for t in tools.tools) == sorted(
                          ["list_databases", "list_tables", "describe_table", "run_query", "ping",
                           "server_info"]), [t.name for t in tools.tools])
                r = await session.call_tool("run_query", {"sql": "SELECT i FROM d1.t"})
                check("2026-07-28: tools/call run_query",
                      not r.is_error and json.loads(text(r))["rows"] == [{"i": "1"}], text(r))
                r = await session.call_tool("describe_table", {"database": "d1", "table": "t"})
                check("2026-07-28: tools/call describe_table",
                      not r.is_error and '"COLUMN_NAME":"i"' in text(r), text(r))
                r = await session.call_tool("run_query", {"sql": "INSERT INTO d1.t VALUES (3)"})
                check("2026-07-28: a refused statement is a tool error",
                      r.is_error and "mcp.read_only" in text(r), text(r))

    posts = [e for e in stateless if e["http"] == "POST"]
    check("2026-07-28: requests are POSTs only (no GET stream, no DELETE)",
          len(posts) == len(stateless) and len(posts) >= 5, [e["http"] for e in stateless])
    check("2026-07-28: every request answered 200", all(e["status"] == 200 for e in posts),
          [(e["method"], e["status"]) for e in posts])
    check("2026-07-28: every request carries the version in _meta and the header",
          all(e["meta_version"] == e["version_header"] == "2026-07-28" for e in posts),
          [(e["meta_version"], e["version_header"]) for e in posts])
    check("2026-07-28: Mcp-Method mirrors the method",
          all(e["method_header"] == e["method"] for e in posts),
          [(e["method"], e["method_header"]) for e in posts])
    calls = [e for e in posts if e["method"] == "tools/call"]
    check("2026-07-28: Mcp-Name mirrors the tool name",
          len(calls) == 3 and all(e["name_header"] == e["name"] for e in calls),
          [(e["name"], e["name_header"]) for e in calls])
    check("2026-07-28: no session id sent or issued",
          not any(e["session_sent"] or e["session_received"] for e in stateless), stateless)


asyncio.run(main())
asyncio.run(main_stateless())
sys.exit(1 if failed else 0)
