#!/usr/bin/env python3
"""Local mock of the Agent Bounty Feed MCP server.

Exists so abf.py can be tested end-to-end without internet: the handshake,
the Mcp-Session-Id echo, the 202 notification, and both response framings
(plain JSON and text/event-stream) all go through the real client code.

    python3 tests/mock_mcp.py 8765 &
    python3 abf.py mcp --url http://127.0.0.1:8765/mcp
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

SESSION = "mock-session-0001"
FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "fixtures", "earn-now.sample.json")

TOOLS = [
    {"name": "list_bounties", "description": "List open bounties",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "get_new_bounties", "description": "Bounties new since a timestamp",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_bounty", "description": "Fetch one bounty by id",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}},
    {"name": "get_feed_stats", "description": "Feed counts",
     "inputSchema": {"type": "object", "properties": {}}},
]

BOUNTIES = {
    "items": [
        {"id": "frantic:129", "est_usd": 16, "source": "frantic",
         "title": "Earn a citation on an external page that already ranks"},
        {"id": "direct:abf-growth-guide-5usdc", "est_usd": 5, "source": "direct",
         "title": "Best public agent-facing guide or demo for Agent Bounty Feed"},
    ]
}


def sse(obj):
    return ("event: message\ndata: %s\n\n" % json.dumps(obj)).encode()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        """Serve the captured /earn-now response so the read paths test offline."""
        if self.path.split("?")[0] not in ("/earn-now", "/feed.json"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        with open(FIXTURE, encoding="utf-8") as fh:
            body = fh.read().encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        msg = json.loads(self.rfile.read(length) or b"{}")
        method = msg.get("method")
        rid = msg.get("id")

        # every request after initialize must echo the session id
        if method != "initialize":
            got = self.headers.get("Mcp-Session-Id")
            if got != SESSION:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":"missing session id"}')
                return

        if method == "initialize":
            body = json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-abf", "version": "0.0.1"},
            }}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", SESSION)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if method == "tools/list":
            body = sse({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = msg["params"]["name"]
            payload = {"counts": {"total": 25, "security": 2}} if name == "get_feed_stats" else BOUNTIES
            body = sse({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": False,
            }})
        else:
            body = sse({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32601, "message": "unknown method"}})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
