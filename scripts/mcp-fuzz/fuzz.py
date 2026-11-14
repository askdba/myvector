"""Fuzzing and soak driver for component_mcp (RFC 0001 §10 go/no-go).

Usage: python fuzz.py --seconds N --seed S [--out DIR]

Runs against the endpoint at 127.0.0.1:33480 (the caller shares the server's network namespace)
and MySQL at 127.0.0.1:3306, set up by scripts/fuzz-mcp-endpoint.sh. Workers run in parallel
for the whole duration:

  http    malformed and random HTTP and JSON-RPC: methods, headers, bodies, both protocol eras,
          raw socket abuse (bad framing, truncation, slow and dropped connections);
  sql     random and mutated SQL through run_query, including statements the guard must refuse;
  soak    well-formed sessions in both eras whose answers are checked (correctness under load);
  plain   non-MCP traffic from an ordinary account, which the guard must leave alone.

Invariants checked throughout (any violation fails the run):
  - mysqld answers SQL: checked every few seconds, at most HEALTH_GRACE seconds of silence (endpoint
    stalls under load are counted, not failed; the caller checks the endpoint recovers afterwards);
  - the guard never lets an MCP write through (d1.t keeps its rows);
  - no reply contains a token hash from mysql.mcp_tokens;
  - soak answers are right (known row counts, masked columns).

Prints a JSON summary at the end and writes it to OUT/summary.json; exit status 0 only if no
invariant was violated. Crashes and sanitizer reports are checked by the caller.
"""

import argparse
import hashlib
import json
import os
import random
import socket
import string
import sys
import threading
import time
from collections import Counter

HOST, PORT = "127.0.0.1", 33480
TOKENS = {"main": "fuzz-token-main", "table": "fuzz-token-table", "locked": "fuzz-token-locked",
          "expired": "fuzz-token-expired", "admin": "fuzz-token-admin"}
TOKEN_HASHES = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in TOKENS.items()}
VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
TOOLS = ["list_databases", "list_tables", "describe_table", "run_query", "ping", "server_info"]
METHODS = ["initialize", "tools/list", "tools/call", "ping", "server/discover",
           "notifications/initialized", "notifications/cancelled", "resources/list",
           "prompts/list", "subscriptions/listen", "logging/setLevel", "completion/complete"]
HEALTH_GRACE = 60
D1_T_ROWS = 3  # rows the setup puts in d1.t; MCP sessions must never change them

stop = threading.Event()
lock = threading.Lock()
stats = Counter()
violations = []


class Stalled(Exception):
    """The endpoint gave no HTTP response (stalled, askdba/myvector#269, #304): not a wrong answer."""


def violation(kind, detail):
    with lock:
        if len(violations) < 200:
            violations.append({"kind": kind, "detail": str(detail)[:2000], "t": time.time()})
        stats["violation:" + kind] += 1


def count(key, n=1):
    with lock:
        stats[key] += n


# ----- HTTP plumbing ------------------------------------------------------------------------

def send_raw(data, timeout=30, read=True, slow=None):
    """Send raw bytes on a fresh connection; return the reply bytes (b"" if none)."""
    s = socket.create_connection((HOST, PORT), timeout=timeout)
    try:
        if slow:
            for i in range(0, len(data), slow):
                s.sendall(data[i:i + slow])
                time.sleep(0.05)
        else:
            s.sendall(data)
        if not read:
            return b""
        chunks = []
        while True:
            try:
                c = s.recv(65536)
            except socket.timeout:
                break
            if not c:
                break
            chunks.append(c)
            if sum(map(len, chunks)) > 64 * 1024 * 1024:
                break
        return b"".join(chunks)
    finally:
        s.close()


def http_request(method, headers, body=b"", path="/mcp", timeout=60):
    """A well-framed HTTP/1.1 request; returns (status, headers dict, body bytes)."""
    if isinstance(body, str):
        body = body.encode("utf-8", "surrogatepass")
    lines = [f"{method} {path} HTTP/1.1", f"Host: {HOST}:{PORT}", "Connection: close"]
    has_len = any(k.lower() == "content-length" for k, _ in headers)
    for k, v in headers:
        lines.append(f"{k}: {v}")
    if not has_len:
        lines.append(f"Content-Length: {len(body)}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8", "surrogatepass") + body
    reply = send_raw(raw, timeout=timeout)
    head, _, rest = reply.partition(b"\r\n\r\n")
    status = 0
    hdrs = {}
    if head:
        first, *hl = head.split(b"\r\n")
        parts = first.split(b" ")
        if len(parts) > 1 and parts[1].isdigit():
            status = int(parts[1])
        for h in hl:
            k, _, v = h.partition(b":")
            hdrs[k.decode("latin-1").strip().lower()] = v.decode("latin-1").strip()
    if hdrs.get("transfer-encoding", "").lower() == "chunked":
        rest = dechunk(rest)
    return status, hdrs, rest


def dechunk(data):
    out, i = [], 0
    while i < len(data):
        j = data.find(b"\r\n", i)
        if j < 0:
            break
        try:
            n = int(data[i:j].split(b";")[0], 16)
        except ValueError:
            break
        if n == 0:
            break
        out.append(data[j + 2:j + 2 + n])
        i = j + 2 + n + 2
    return b"".join(out)


def check_reply(body, request=""):
    """Invariant: no reply carries a token hash. request: what was sent, for the report."""
    if not body:
        return
    text = body.decode("utf-8", "replace")
    for name, h in TOKEN_HASHES.items():
        if h in text:
            violation("token-hash-leak", f"{name} in the reply to {request[:600]!r}: {text[:300]}")
            return


def rpc(token, method, params=None, *, version=None, session=None, rid=1, extra=()):
    """A well-formed JSON-RPC call; returns (status, headers, parsed JSON or None, raw body)."""
    headers = [("Content-Type", "application/json"), ("Accept", "application/json, text/event-stream")]
    if token:
        headers.append(("Authorization", f"Bearer {token}"))
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if version == "2026-07-28":
        params = dict(params or {})
        params["_meta"] = {"io.modelcontextprotocol/protocolVersion": version,
                           "io.modelcontextprotocol/clientCapabilities": {},
                           "io.modelcontextprotocol/clientInfo": {"name": "fuzz", "version": "1"}}
        headers += [("MCP-Protocol-Version", version), ("Mcp-Method", method)]
        if method == "tools/call":
            headers.append(("Mcp-Name", params.get("name", "")))
    elif version:
        headers.append(("MCP-Protocol-Version", version))
    if session:
        headers.append(("Mcp-Session-Id", session))
    if params is not None:
        body["params"] = params
    headers += list(extra)
    status, hdrs, raw = http_request("POST", headers, json.dumps(body))
    check_reply(raw, f"token={token} version={version} {json.dumps(body)}")
    if status == 0 and stall_raises.get(threading.get_ident()):
        raise Stalled()
    try:
        doc = json.loads(raw) if raw else None
    except ValueError:
        doc = None
    return status, hdrs, doc, raw


def tool_text(doc):
    try:
        return doc["result"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None


# ----- random values -------------------------------------------------------------------------

def rand_str(r, n=None):
    n = r.choice([0, 1, 2, 8, 64, 1024, 65536]) if n is None else n
    alphabet = r.choice([string.ascii_letters, string.printable, "\x00\x01\x7f\xff",
                         "éøπ中文🙂‮﻿", "'\"\\;`%_"])
    return "".join(r.choice(alphabet) for _ in range(min(n, 200000)))


def rand_json(r, depth=0):
    kinds = ["null", "bool", "int", "float", "str", "list", "dict"] if depth < 6 else ["int", "str"]
    k = r.choice(kinds)
    if k == "null":
        return None
    if k == "bool":
        return r.random() < 0.5
    if k == "int":
        return r.choice([0, -1, 1, 2**31, 2**63 - 1, 2**63, 2**64, -2**63, 10**30, r.randint(-10**9, 10**9)])
    if k == "float":
        return r.choice([0.5, -0.0, 1e308, 1e-308, 3.14, float(r.randint(0, 9))])
    if k == "str":
        return rand_str(r)
    if k == "list":
        return [rand_json(r, depth + 1) for _ in range(r.randint(0, 5))]
    return {rand_str(r, r.randint(0, 12)): rand_json(r, depth + 1) for _ in range(r.randint(0, 5))}


def deep(r):
    n = r.choice([100, 1000, 10000, 100000, 1000000])
    o, c = r.choice([("[", "]"), ('{"a":', "}")])
    return o * n + "1" + c * n


def mutate(r, data):
    b = bytearray(data)
    for _ in range(r.randint(1, 8)):
        op = r.randint(0, 6)
        if not b:
            b += os.urandom(r.randint(1, 16))
            continue
        i = r.randrange(len(b))
        if op == 0:
            b[i] = r.randrange(256)
        elif op == 1:
            del b[i:i + r.randint(1, 32)]
        elif op == 2:
            b[i:i] = os.urandom(r.randint(1, 32))
        elif op == 3:
            b[i:i] = b[i:i + r.randint(1, 64)] * r.randint(2, 50)
        elif op == 4:
            b = b[:i]
        elif op == 5:
            b[i:i] = r.choice([b"\x00", b"\xff\xfe", b"\xc0\x80", b"\xed\xa0\x80", b'"', b"\\u0000"])
        else:
            b[i:i] = str(r.choice([2**64, -2**63 - 1, 1e400, "1" * 400])).encode()
    return bytes(b)


# ----- workers -------------------------------------------------------------------------------

def random_rpc_body(r):
    method = r.choice(METHODS + [rand_str(r, r.randint(0, 40))])
    body = {}
    if r.random() < 0.9:
        body["jsonrpc"] = r.choice(["2.0", "2.0", "1.0", 2, None, ""])
    if r.random() < 0.85:
        body["id"] = r.choice([1, 0, -1, 2**63, 2**64 - 1, 2**64, 1.5, "x", "", None, [], {}, rand_str(r, 40)])
    body["method"] = method if r.random() < 0.95 else rand_json(r)
    params = {}
    if method == "tools/call" or r.random() < 0.3:
        params["name"] = r.choice(TOOLS + [rand_str(r, 20), None, 1])
        args = {}
        for key in ["sql", "database", "table"]:
            if r.random() < 0.6:
                args[key] = r.choice([rand_str(r), "d1", "t", "mysql", "SELECT 1", rand_json(r)])
        params["arguments"] = args if r.random() < 0.9 else rand_json(r)
    if method == "initialize":
        params["protocolVersion"] = r.choice(VERSIONS + [rand_str(r, 12), None])
    if r.random() < 0.5:
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": r.choice(VERSIONS + ["2099-01-01", None, 1]),
            "io.modelcontextprotocol/clientCapabilities": r.choice([{}, None, "x", rand_json(r)]),
            "io.modelcontextprotocol/clientInfo": r.choice([{"name": "f", "version": "1"}, "x", rand_json(r)]),
        }
        if r.random() < 0.3:
            params["_meta"]["io.modelcontextprotocol/logLevel"] = rand_json(r)
    if r.random() < 0.2:
        params.update({rand_str(r, 8): rand_json(r) for _ in range(r.randint(1, 4))})
    body["params"] = params if r.random() < 0.9 else rand_json(r)
    text = json.dumps(body)
    if r.random() < 0.05:
        text = json.dumps([body] * r.randint(1, 5))  # batches
    return text


def random_headers(r, sessions):
    h = []
    tok = r.choice(list(TOKENS.values()) + ["", "x", rand_str(r, r.choice([8, 300, 70000]))])
    if r.random() < 0.9:
        h.append(("Authorization", r.choice([f"Bearer {tok}", f"bearer {tok}", tok, f"Basic {tok}", "Bearer"])))
    if r.random() < 0.9:
        h.append(("Content-Type", r.choice(["application/json", "application/json; charset=utf-8",
                                            "text/plain", "", "application/JSON", rand_str(r, 20)])))
    if r.random() < 0.9:
        h.append(("Accept", r.choice(["application/json, text/event-stream", "*/*", "application/json",
                                      "text/*, application/*", "", rand_str(r, 30)])))
    if r.random() < 0.6:
        h.append(("MCP-Protocol-Version", r.choice(VERSIONS + ["2099-01-01", "", rand_str(r, 12)])))
    if r.random() < 0.4:
        h.append(("Mcp-Method", r.choice(METHODS + [rand_str(r, 20)])))
    if r.random() < 0.3:
        h.append(("Mcp-Name", r.choice(TOOLS + [rand_str(r, 20)])))
    if r.random() < 0.4:
        with lock:
            known = list(sessions)
        h.append(("Mcp-Session-Id", r.choice(known + ["0" * 32, "", rand_str(r, 40)] if known else ["0" * 32, rand_str(r, 40)])))
    if r.random() < 0.2:
        h.append(("Origin", r.choice(["http://localhost:6274", "http://evil.example", "null", rand_str(r, 30)])))
    for _ in range(r.choice([0, 0, 0, 1, 5, 100])):
        h.append((r.choice(["X-" + rand_str(r, 8).replace(":", ""), "Authorization", "Content-Type"]),
                  rand_str(r, r.choice([1, 100, 8000])).replace("\r", "").replace("\n", "")))
    return [(k.replace("\r", "").replace("\n", "").replace(":", "") or "X", v.replace("\r", "").replace("\n", ""))
            for k, v in h]


def http_worker(seed, sessions):
    r = random.Random(seed)
    while not stop.is_set():
        try:
            mode = r.random()
            if mode < 0.70:
                body = random_rpc_body(r)
                if r.random() < 0.15:
                    body = deep(r)
                data = body.encode("utf-8", "surrogatepass")
                if r.random() < 0.3:
                    data = mutate(r, data)
                method = "POST" if r.random() < 0.9 else r.choice(["GET", "DELETE", "OPTIONS", "PUT", "PATCH", "HEAD"])
                headers = random_headers(r, sessions)
                status, hdrs, raw = http_request(method, headers, data,
                                                 path="/mcp" if r.random() < 0.95 else "/" + rand_str(r, 10))
                check_reply(raw, f"{method} {headers!r} {data[:400]!r}")
                sid = hdrs.get("mcp-session-id")
                if sid:
                    with lock:
                        sessions.add(sid)
                        if len(sessions) > 200:
                            sessions.pop()
                count(f"http:{status}")
            else:
                raw_abuse(r)
                count("http:raw")
        except (OSError, ValueError) as e:
            count("http:conn-error:" + type(e).__name__)
            time.sleep(0.01)


def raw_abuse(r):
    tok = TOKENS["main"]
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    base = (f"POST /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {tok}\r\n"
            "Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\n")
    choice = r.randint(0, 9)
    if choice == 0:  # Content-Length larger than the body, then close
        send_raw((base + f"Content-Length: {len(body) + r.randint(1, 10**6)}\r\n\r\n").encode() + body, timeout=5, read=False)
    elif choice == 1:  # Content-Length smaller than the body (pipelined garbage)
        send_raw((base + "Content-Length: 5\r\n\r\n").encode() + body + mutate(r, body), timeout=5)
    elif choice == 2:  # negative / huge / non-numeric Content-Length
        cl = r.choice(["-1", "99999999999999999999", "abc", "1e3", " 10", ""])
        send_raw((base + f"Content-Length: {cl}\r\n\r\n").encode() + body, timeout=5)
    elif choice == 3:  # chunked, malformed or valid
        chunks = r.choice([b"5\r\nhello\r\n0\r\n\r\n", b"zz\r\n", b"ffffffffffffffff\r\nx\r\n",
                           f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"])
        send_raw((base + "Transfer-Encoding: chunked\r\n\r\n").encode() + chunks, timeout=5)
    elif choice == 4:  # slow sender
        send_raw((base + f"Content-Length: {len(body)}\r\n\r\n").encode() + body, timeout=15, slow=r.choice([1, 7, 64]))
    elif choice == 5:  # header then drop
        send_raw(base.encode(), timeout=2, read=False)
    elif choice == 6:  # garbage request line
        send_raw(mutate(r, (base + "\r\n").encode()) + body, timeout=5)
    elif choice == 7:  # pipelined requests
        one = (base + f"Content-Length: {len(body)}\r\n\r\n").encode() + body
        send_raw(one * r.randint(2, 50), timeout=10)
    elif choice == 8:  # huge header block
        send_raw((base + "X-Big: " + "a" * r.choice([10**5, 10**6, 8 * 10**6]) + "\r\n\r\n").encode(), timeout=10)
    else:  # huge body
        big = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"x":"' + b"a" * r.choice([10**6, 3 * 10**7]) + b'"}}'
        send_raw((base + f"Content-Length: {len(big)}\r\n\r\n").encode() + big, timeout=30)


# Recent statements run_query answered without an error: when the monitor finds d1.t changed, the
# report lists those of the seconds before, so the statement that got through can be found.
recent_ok = []


SQL_SEEDS = [
    "SELECT 1", "SELECT * FROM d1.t", "SELECT COUNT(*) FROM d1.big", "SELECT n, SHA2(n, 256) FROM d1.big",
    "SELECT * FROM d1.masked", "SELECT REGEXP_REPLACE(REPEAT('ab', 1000), 'b+', 'c')",
    "SELECT JSON_EXTRACT('{\"a\":[1,2,{\"b\":3}]}', '$**.b')", "SELECT REPEAT('x', 1048576)",
    "WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM s WHERE n < 5000) SELECT * FROM s",
    "SELECT * FROM information_schema.tables LIMIT 50", "SELECT * FROM performance_schema.threads",
    "SELECT SLEEP(0.2)", "SELECT CAST(1e308 AS DOUBLE) * 10", "SELECT 0x00FF, _binary'\\0'",
    "SELECT d FROM d1.dbl", "SHOW TABLES FROM d1", "EXPLAIN SELECT * FROM d1.big", "DESCRIBE d1.t",
    "SELECT @@version, CURRENT_USER()", "SELECT * FROM d1.big a JOIN d1.big b LIMIT 3000",
    # must be refused (read-only, system schemas, guard settings)
    "INSERT INTO d1.t VALUES (99)", "UPDATE d1.t SET i = 0", "DELETE FROM d1.t", "DROP TABLE d1.t",
    "TRUNCATE d1.t", "CREATE TABLE d1.x (i INT)", "SELECT * FROM mysql.mcp_tokens", "SELECT * FROM mysql.user",
    "SET GLOBAL mcp.read_only = OFF", "SET SESSION mcp.read_only = OFF", "SELECT * FROM d1.t INTO OUTFILE '/tmp/x'",
    "LOAD DATA INFILE '/etc/passwd' INTO TABLE d1.t", "LOCK TABLES d1.t WRITE", "CALL mysql_mcp.list_databases()",
    "PREPARE s FROM 'DELETE FROM d1.t'", "SELECT 1; DELETE FROM d1.t", "KILL 1", "SHUTDOWN",
    "INSTALL COMPONENT 'file://component_mcp'", "GRANT ALL ON *.* TO mcp@'%'", "SET PASSWORD = 'x'",
    "SELECT * FROM sys.version", "HANDLER d1.t OPEN", "DO RELEASE_ALL_LOCKS()", "SELECT GET_LOCK('x', 1)",
    "XA START 'x'", "BEGIN", "COMMIT", "SAVEPOINT a", "SET autocommit = 0; INSERT INTO d1.t VALUES (5)",
    "/*!50000 DELETE FROM d1.t */", "DEL/**/ETE FROM d1.t", "SELECT * FROM d1.t FOR UPDATE",
]


def sql_worker(seed):
    r = random.Random(seed)
    while not stop.is_set():
        try:
            s = r.choice(SQL_SEEDS)
            if r.random() < 0.4:
                s = mutate(r, s.encode()).decode("utf-8", "replace")
            if r.random() < 0.1:
                s = s + " " + rand_str(r, r.choice([10, 1000, 100000]))
            version = r.choice(["2026-07-28", None, "2025-11-25"])
            tok = r.choice(["main", "main", "table"])
            status, _, doc, raw = rpc(TOKENS[tok], "tools/call",
                                      {"name": "run_query", "arguments": {"sql": s}}, version=version,
                                      rid=r.randint(1, 10**9))
            ok = bool(doc) and doc.get("result", {}).get("isError") is False
            count(f"sql:{status}:{'ok' if ok else 'err'}")
            if ok:
                with lock:
                    recent_ok.append((time.time(), tok, version, s[:400]))
                    del recent_ok[:-2000]
        except (OSError, ValueError) as e:
            count("sql:conn-error:" + type(e).__name__)
            time.sleep(0.01)


stall_raises = {}  # thread id -> True: rpc() raises Stalled on a missing response


def soak_worker(seed):
    r = random.Random(seed)
    stall_raises[threading.get_ident()] = True
    while not stop.is_set():
        try:
            version = r.choice(VERSIONS)
            session = None
            if version != "2026-07-28":
                st, hdrs, doc, raw = rpc(TOKENS["main"], "initialize", {"protocolVersion": version})
                session = hdrs.get("mcp-session-id")
                if st != 200 or not session or (doc or {}).get("result", {}).get("protocolVersion") != version:
                    violation("soak-initialize", f"{st} {hdrs} {raw[:300]!r}")
                    continue
            else:
                st, _, doc, raw = rpc(TOKENS["main"], "server/discover", {}, version=version)
                if st != 200 or "2026-07-28" not in json.dumps(doc):
                    violation("soak-discover", f"{st} {raw[:300]!r}")
                    continue
            v = version if version == "2026-07-28" else version
            for _ in range(r.randint(1, 8)):
                which = r.randint(0, 4)
                if which == 0:
                    st, _, doc, raw = rpc(TOKENS["main"], "tools/call",
                                          {"name": "run_query", "arguments": {"sql": "SELECT COUNT(*) AS c FROM d1.big"}},
                                          version=v, session=session)
                    if '\\"c\\":\\"1500\\"' not in raw.decode("utf-8", "replace") and '"c":"1500"' not in (tool_text(doc) or ""):
                        violation("soak-count", f"{st} {raw[:300]!r}")
                elif which == 1:
                    st, _, doc, raw = rpc(TOKENS["main"], "tools/call",
                                          {"name": "run_query", "arguments": {"sql": "SELECT secret_col, ok FROM d1.masked"}},
                                          version=v, session=session)
                    text = tool_text(doc) or ""
                    if "topsecret" in text or "********" not in text:
                        violation("soak-masking", f"{st} {raw[:300]!r}")
                elif which == 2:
                    st, _, doc, raw = rpc(TOKENS["main"], "tools/call",
                                          {"name": "run_query", "arguments": {"sql": "SELECT n FROM d1.big ORDER BY n"}},
                                          version=v, session=session)
                    text = tool_text(doc) or ""
                    if '"truncated":true' not in text:
                        violation("soak-bounded", f"{st} {raw[:300]!r}")
                elif which == 3:
                    st, _, doc, raw = rpc(TOKENS["main"], "tools/list", {}, version=v, session=session)
                    names = [t.get("name") for t in ((doc or {}).get("result") or {}).get("tools", [])]
                    if names != TOOLS:
                        violation("soak-tools", f"{st} {raw[:300]!r}")
                else:
                    st, _, doc, raw = rpc(TOKENS["main"], "tools/call",
                                          {"name": "describe_table", "arguments": {"database": "d1", "table": "t"}},
                                          version=v, session=session)
                    if '"isError":false' not in raw.decode("utf-8", "replace").replace(" ", ""):
                        violation("soak-describe", f"{st} {raw[:300]!r}")
                count(f"soak:{st}")
            for tok, want in (("locked", "3118"), ("admin", "1227"), ("expired", None)):
                if r.random() < 0.05:
                    st, _, doc, raw = rpc(TOKENS[tok], "tools/call",
                                          {"name": "run_query", "arguments": {"sql": "SELECT 1"}},
                                          version="2026-07-28")
                    text = raw.decode("utf-8", "replace")
                    ok = (st == 401) if want is None else (want in text and '"isError":true' in text.replace(" ", ""))
                    if not ok:
                        violation(f"soak-refusal-{tok}", f"{st} {text[:300]}")
            if session:
                st, _, _ = http_request("DELETE", [("Authorization", f"Bearer {TOKENS['main']}"),
                                                   ("Mcp-Session-Id", session)])
                if st == 0:
                    raise Stalled()
                if st != 204:
                    violation("soak-delete", st)
        except Stalled:
            count("soak:stalled")
            time.sleep(0.5)
        except (OSError, ValueError) as e:
            count("soak:conn-error:" + type(e).__name__)
            time.sleep(0.05)


def plain_worker(seed):
    import mysql.connector  # non-MCP traffic, from an ordinary account
    r = random.Random(seed)
    while not stop.is_set():
        try:
            c = mysql.connector.connect(host=HOST, port=3306, user="app", password="app-pw", database="d1")
            cur = c.cursor()
            for _ in range(r.randint(10, 200)):
                if r.random() < 0.7:
                    cur.execute("SELECT n FROM big WHERE n = %s", (r.randint(1, 1500),))
                    cur.fetchall()
                else:
                    cur.execute("INSERT INTO app_log (v) VALUES (%s)", (r.randint(0, 10**6),))
                    c.commit()
                count("plain:ok")
            c.close()
        except Exception as e:  # noqa: BLE001 - count every failure kind
            count("plain:error:" + type(e).__name__)
            time.sleep(0.2)


def monitor(start, seconds, out):
    """Health and invariants: mysqld answers SQL, d1.t keeps its rows, progress is logged.

    The endpoint itself can stall while the fuzzer holds its I/O threads with unfinished requests
    (Router's HTTP server has no handshake or idle timeout, askdba/myvector#269); stalls are
    counted (endpoint-stall-checks), and the caller checks that the endpoint recovers after the
    run. mysqld not answering SQL for HEALTH_GRACE seconds is a failure."""
    import mysql.connector
    last_sql_ok = time.time()
    while not stop.is_set():
        time.sleep(5)
        try:
            st, _, doc, _ = rpc(TOKENS["main"], "tools/list", {}, version="2026-07-28")
            count("monitor:endpoint-ok" if st == 200 else "monitor:endpoint-stall-checks")
        except OSError:
            count("monitor:endpoint-stall-checks")
        try:
            c = mysql.connector.connect(host=HOST, port=3306, user="fuzzmon", password="",
                                        database="d1", connection_timeout=10)
            cur = c.cursor()
            cur.execute("SELECT COUNT(*) FROM t")
            n = cur.fetchone()[0]
            c.close()
            last_sql_ok = time.time()
            if n != D1_T_ROWS:
                with lock:
                    suspects = [x for x in recent_ok if x[0] > time.time() - 15]
                violation("guard-write-through",
                          f"d1.t has {n} rows, expected {D1_T_ROWS}; statements answered OK in the last 15s: "
                          + json.dumps(suspects[-40:]))
                stop.set()  # the first occurrence is the one to read
                break
        except Exception as e:  # noqa: BLE001
            count("monitor:sql-error:" + type(e).__name__)
        if time.time() - last_sql_ok > HEALTH_GRACE:
            violation("server-unresponsive", f"mysqld did not answer SQL for {HEALTH_GRACE}s")
            stop.set()
            break
        elapsed = time.time() - start
        if int(elapsed) % 300 < 5:
            with lock:
                snap = dict(stats)
            print(f"[{elapsed / 3600:.2f}h] " + json.dumps({k: v for k, v in sorted(snap.items())}), flush=True)
        if elapsed >= seconds:
            stop.set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", default=".")
    ap.add_argument("--http", type=int, default=6)
    ap.add_argument("--sql", type=int, default=3)
    ap.add_argument("--soak", type=int, default=3)
    ap.add_argument("--plain", type=int, default=2)
    a = ap.parse_args()
    sessions = set()
    start = time.time()
    threads = [threading.Thread(target=monitor, args=(start, a.seconds, a.out), daemon=True)]
    threads += [threading.Thread(target=http_worker, args=(a.seed * 1000 + i, sessions), daemon=True) for i in range(a.http)]
    threads += [threading.Thread(target=sql_worker, args=(a.seed * 2000 + i,), daemon=True) for i in range(a.sql)]
    threads += [threading.Thread(target=soak_worker, args=(a.seed * 3000 + i,), daemon=True) for i in range(a.soak)]
    threads += [threading.Thread(target=plain_worker, args=(a.seed * 4000 + i,), daemon=True) for i in range(a.plain)]
    for t in threads:
        t.start()
    while not stop.is_set():
        time.sleep(1)
    time.sleep(5)  # let in-flight requests finish
    summary = {"seed": a.seed, "seconds": round(time.time() - start), "stats": dict(stats),
               "violations": violations}
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1, sort_keys=True)
    print(json.dumps({"seconds": summary["seconds"], "violations": len(violations),
                      "requests": sum(v for k, v in stats.items() if not k.startswith("violation"))}))
    for v in violations[:20]:
        print("VIOLATION", json.dumps(v))
    sys.exit(1 if violations else 0)


if __name__ == "__main__":
    main()
