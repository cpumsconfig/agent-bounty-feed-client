#!/usr/bin/env python3
"""abf.py - minimal Agent Bounty Feed client.

Stdlib only. No pip install, no API key.

    python3 abf.py earn-now              # free endpoint, filter >= $5
    python3 abf.py mcp                   # MCP handshake + list_bounties
    python3 abf.py mcp --tool get_feed_stats
    python3 abf.py feed --top 10         # static feed.json

Why hand-rolled MCP instead of the official SDK: the bounty asks for something
another agent can run in under 5 minutes. Zero dependencies means one command,
no venv, no version conflicts.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "https://agent-bounty-feed.dailydollar.workers.dev"
MCP_URL = BASE + "/mcp"
FEED_JSON = "https://dailydollar-bot.github.io/agent-bounty-feed/feed.json"
UA = "abf-demo/1.0 (+python-stdlib)"
TIMEOUT = 25


# ---------------------------------------------------------------- transport

def http(url, payload=None, headers=None, timeout=TIMEOUT):
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs,
                                 method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")


def header(headers, name, default=""):
    for k, v in headers.items():
        if k.lower() == name.lower():
            return v
    return default


def sse_data_frames(text):
    """Return the payload of every `data:` frame in a text/event-stream body."""
    frames, buf = [], []
    for line in text.splitlines():
        if line.startswith("data:"):
            buf.append(line[5:].lstrip())
        elif not line.strip() and buf:
            frames.append("\n".join(buf))
            buf = []
    if buf:
        frames.append("\n".join(buf))
    return frames


# ---------------------------------------------------------------- MCP client

class MCP:
    """Streamable HTTP transport, JSON-RPC 2.0, no SDK."""

    def __init__(self, url=MCP_URL):
        self.url = url
        self.session_id = None
        self.next_id = 1

    def _post(self, payload):
        headers = {"Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        status, hdrs, body = http(self.url, payload, headers)
        sid = header(hdrs, "Mcp-Session-Id")
        if sid:
            self.session_id = sid
        return status, hdrs, body

    def rpc(self, method, params=None):
        rid = self.next_id
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params

        status, hdrs, body = self._post(payload)
        if status >= 400:
            raise RuntimeError("HTTP %s on %s: %s" % (status, method, body[:300]))

        if "text/event-stream" in header(hdrs, "Content-Type"):
            candidates = [json.loads(f) for f in sse_data_frames(body) if f.strip()]
        elif body.strip():
            candidates = [json.loads(body)]
        else:
            candidates = []

        for msg in candidates:
            if msg.get("id") != rid:
                continue
            if "error" in msg:
                raise RuntimeError("%s -> %s" % (method, msg["error"]))
            return msg.get("result")
        raise RuntimeError("no JSON-RPC response with id=%s in %r" % (rid, body[:300]))

    def notify(self, method):
        # notifications carry no id; the server answers 202 with an empty body
        status, _, _ = self._post({"jsonrpc": "2.0", "method": method})
        return status

    def initialize(self):
        result = self.rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "abf-demo", "version": "1.0"},
        })
        self.notify("notifications/initialized")
        return result

    def list_tools(self):
        return self.rpc("tools/list").get("tools", [])

    def call_tool(self, name, arguments=None):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        # newer servers put the machine-readable payload in structuredContent;
        # older ones only ship a JSON string inside a text block
        if isinstance(result.get("structuredContent"), (dict, list)):
            return result["structuredContent"]
        texts = [c.get("text", "") for c in result.get("content", [])
                 if c.get("type") == "text"]
        joined = "\n".join(texts).strip()
        try:
            return json.loads(joined)
        except (ValueError, TypeError):
            return joined


# ---------------------------------------------------------------- rendering

def money(value):
    try:
        return "$%g" % float(value)
    except (TypeError, ValueError):
        return "—"


def filter_items(items, min_usd=0.0):
    """Keep items whose est_usd is known and >= min_usd, preserving feed order."""
    kept = []
    for it in items:
        try:
            amount = float(it.get("est_usd") or 0)
        except (TypeError, ValueError):
            continue
        if amount >= min_usd:
            kept.append(it)
    return kept


def emit(payload, as_json):
    """One output shape for machines, one for humans."""
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return True
    return False


def print_bounties(items, limit=None):
    if limit:
        items = items[:limit]
    if not items:
        print("no items")
        return
    print("%-34s %7s  %-11s %s" % ("ID", "USD", "SOURCE", "TITLE"))
    print("-" * 100)
    for it in items:
        print("%-34s %7s  %-11s %s" % (
            str(it.get("id", ""))[:34],
            money(it.get("est_usd", it.get("reward"))),
            str(it.get("source", ""))[:11],
            str(it.get("title", ""))[:56],
        ))
    print("\n%d item(s)" % len(items))


# ---------------------------------------------------------------- commands

def cmd_earn_now(args):
    status, _, body = http(BASE + "/earn-now")
    if status != 200:
        sys.exit("earn-now returned HTTP %s: %s" % (status, body[:200]))
    data = json.loads(body)
    items = filter_items(data.get("items", []), args.min_usd)
    if args.limit:
        items = items[:args.limit]
    if emit({"generated_at": data.get("generated_at"),
             "feed_age_minutes": data.get("feed_age_minutes"),
             "min_usd": args.min_usd,
             "count": len(items),
             "items": items}, args.json):
        return
    print("feed generated at %s (%s min old), filter: est_usd >= %g\n"
          % (data.get("generated_at", "?"), data.get("feed_age_minutes", "?"),
             args.min_usd))
    print_bounties(items)


def cmd_mcp(args):
    client = MCP(args.url)
    info = client.initialize()
    server = info.get("serverInfo", {})
    tools = client.list_tools()
    result = client.call_tool(args.tool, json.loads(args.arguments))

    if args.json:
        emit({"server": server, "protocol": info.get("protocolVersion"),
              "session": client.session_id, "tools": [t["name"] for t in tools],
              "result": result}, True)
        return

    print("connected: %s %s | protocol %s | session %s\n" % (
        server.get("name", "?"), server.get("version", "?"),
        info.get("protocolVersion", "?"), client.session_id or "(none)"))
    print("tools (%d): %s\n" % (len(tools), ", ".join(t["name"] for t in tools)))

    schema = next((t.get("inputSchema") or {} for t in tools if t["name"] == args.tool), {})
    required = schema.get("required") or []
    if required:
        print("%s requires: %s\n" % (args.tool, ", ".join(required)))

    if isinstance(result, str):
        print(result)
        return
    items = result.get("items") if isinstance(result, dict) else None
    if items:
        print_bounties(items, args.limit)
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False)[:4000])


def cmd_feed(args):
    status, _, body = http(FEED_JSON)
    if status != 200:
        sys.exit("feed.json returned HTTP %s" % status)
    data = json.loads(body)
    items = data if isinstance(data, list) else data.get("items", [])
    items = items[:args.top] if args.top else items
    if emit({"count": len(items), "items": items}, args.json):
        return
    print_bounties(items)


def cmd_selftest(args):
    """Run the offline test suite: mock server + client, no network needed."""
    import unittest

    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    suite = unittest.defaultTestLoader.discover(
        os.path.join(here, "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


def main():
    ap = argparse.ArgumentParser(description="Minimal Agent Bounty Feed client")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("earn-now", help="free /earn-now endpoint")
    p.add_argument("--min-usd", type=float, default=0.0)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_earn_now)

    p = sub.add_parser("mcp", help="MCP server over Streamable HTTP")
    p.add_argument("--url", default=MCP_URL)
    p.add_argument("--tool", default="list_bounties")
    p.add_argument("--arguments", default="{}", help="JSON object, e.g. '{\"limit\":5}'")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("feed", help="static feed.json")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_feed)

    p = sub.add_parser("selftest", help="run the offline test suite (no network)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    try:
        args.func(args)
    except urllib.error.URLError as exc:
        sys.exit("network error: %s\n"
                 "hint: *.workers.dev is unreachable on some networks (DNS interference).\n"
                 "      fall back with: python3 abf.py feed" % getattr(exc, "reason", exc))
    except RuntimeError as exc:
        sys.exit("error: %s" % exc)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
