"""Offline tests for abf.py.

Every test runs against tests/mock_mcp.py on 127.0.0.1 — no network, no fixtures
to download. `python3 abf.py selftest` runs this file.

    python3 -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import threading
import unittest
from contextlib import redirect_stdout
from http.server import HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import abf          # noqa: E402
import mock_mcp     # noqa: E402


class ServerCase(unittest.TestCase):
    """Boots the mock MCP server once per class on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), mock_mcp.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%d/mcp" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def client(self):
        return abf.MCP(self.url)


class TestSSEFraming(unittest.TestCase):
    def test_single_frame(self):
        self.assertEqual(abf.sse_data_frames('event: message\ndata: {"a":1}\n\n'),
                         ['{"a":1}'])

    def test_multiline_data_is_joined(self):
        self.assertEqual(abf.sse_data_frames("data: line1\ndata: line2\n\n"),
                         ["line1\nline2"])

    def test_two_frames(self):
        body = 'data: {"a":1}\n\ndata: {"b":2}\n\n'
        self.assertEqual(abf.sse_data_frames(body), ['{"a":1}', '{"b":2}'])

    def test_no_data_returns_empty(self):
        self.assertEqual(abf.sse_data_frames("event: ping\n\n"), [])

    def test_unterminated_frame_still_returned(self):
        self.assertEqual(abf.sse_data_frames('data: {"a":1}'), ['{"a":1}'])


class TestHelpers(unittest.TestCase):
    def test_money_formats(self):
        self.assertEqual(abf.money(16), "$16")
        self.assertEqual(abf.money(50000.0), "$50000")
        self.assertEqual(abf.money("3.5"), "$3.5")

    def test_money_handles_missing(self):
        self.assertEqual(abf.money(None), "—")
        self.assertEqual(abf.money("n/a"), "—")

    def test_header_is_case_insensitive(self):
        self.assertEqual(abf.header({"Mcp-Session-Id": "x"}, "mcp-session-id"), "x")
        self.assertEqual(abf.header({}, "mcp-session-id", "fallback"), "fallback")

    def test_filter_keeps_threshold(self):
        items = [{"est_usd": 5}, {"est_usd": 4.99}, {"est_usd": 16}]
        self.assertEqual([i["est_usd"] for i in abf.filter_items(items, 5)], [5, 16])

    def test_filter_skips_unknown_amounts(self):
        items = [{"est_usd": None}, {}, {"est_usd": "7"}]
        self.assertEqual(abf.filter_items(items, 5), [{"est_usd": "7"}])

    def test_filter_preserves_order(self):
        items = [{"est_usd": 9}, {"est_usd": 7}, {"est_usd": 8}]
        self.assertEqual([i["est_usd"] for i in abf.filter_items(items, 0)], [9, 7, 8])


class TestMCPClient(ServerCase):
    def test_initialize_reports_server(self):
        info = self.client().initialize()
        self.assertEqual(info["serverInfo"]["name"], "mock-abf")
        self.assertEqual(info["protocolVersion"], "2025-06-18")

    def test_session_id_is_captured(self):
        client = self.client()
        client.initialize()
        self.assertEqual(client.session_id, mock_mcp.SESSION)

    def test_list_tools_returns_four(self):
        client = self.client()
        client.initialize()
        names = [t["name"] for t in client.list_tools()]
        self.assertEqual(names, ["list_bounties", "get_new_bounties",
                                 "get_bounty", "get_feed_stats"])

    def test_call_tool_parses_json_content(self):
        client = self.client()
        client.initialize()
        result = client.call_tool("list_bounties")
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["id"], "frantic:129")

    def test_call_tool_returns_dict_for_stats(self):
        client = self.client()
        client.initialize()
        result = client.call_tool("get_feed_stats")
        self.assertIsInstance(result, dict)
        self.assertEqual(result["counts"]["total"], 25)

    def test_unknown_method_raises(self):
        client = self.client()
        client.initialize()
        with self.assertRaises(RuntimeError):
            client.rpc("tools/nope")

    def test_missing_session_is_rejected_by_server(self):
        """The mock 400s any non-initialize request without the session header."""
        client = self.client()
        client.initialize()
        client.session_id = None          # simulate a client that forgets to echo
        with self.assertRaises(RuntimeError) as ctx:
            client.list_tools()
        self.assertIn("400", str(ctx.exception))

    def test_session_header_absent_after_clear(self):
        client = self.client()
        self.assertIsNone(client.session_id)   # nothing sent before initialize


class TestRendering(unittest.TestCase):
    def test_empty_table(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.print_bounties([])
        self.assertIn("no items", buf.getvalue())

    def test_table_lists_every_row(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.print_bounties([{"id": "a:1", "est_usd": 16, "source": "a", "title": "t"}])
        out = buf.getvalue()
        self.assertIn("a:1", out)
        self.assertIn("$16", out)
        self.assertIn("1 item(s)", out)

    def test_limit_slices(self):
        buf = io.StringIO()
        rows = [{"id": "a:%d" % n, "est_usd": 1, "source": "a", "title": "t"} for n in range(5)]
        with redirect_stdout(buf):
            abf.print_bounties(rows, limit=2)
        self.assertIn("2 item(s)", buf.getvalue())


class TestCommandJSONOutput(ServerCase):
    """The --json paths, driven through the real command functions against the mock."""

    def setUp(self):
        self._base, self._feed = abf.BASE, abf.FEED_JSON
        root = "http://127.0.0.1:%d" % self.port
        abf.BASE, abf.FEED_JSON = root, root + "/feed.json"

    def tearDown(self):
        abf.BASE, abf.FEED_JSON = self._base, self._feed

    def run_json(self, fn, **kwargs):
        import argparse
        args = argparse.Namespace(**kwargs)
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(args)
        return json.loads(buf.getvalue())

    def test_earn_now_json_filters(self):
        out = self.run_json(abf.cmd_earn_now, min_usd=5.0, limit=None, json=True)
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["items"][0]["id"], "frantic:129")
        self.assertEqual(out["min_usd"], 5.0)

    def test_earn_now_json_limit(self):
        out = self.run_json(abf.cmd_earn_now, min_usd=0.0, limit=2, json=True)
        self.assertEqual(out["count"], 2)

    def test_feed_json(self):
        out = self.run_json(abf.cmd_feed, top=4, json=True)
        self.assertEqual(out["count"], 4)

    def test_mcp_json(self):
        out = self.run_json(abf.cmd_mcp, url=self.url, tool="list_bounties",
                            arguments="{}", limit=None, json=True)
        self.assertEqual(out["server"]["name"], "mock-abf")
        self.assertEqual(len(out["tools"]), 4)
        self.assertEqual(out["result"]["items"][0]["id"], "frantic:129")

    def test_earn_now_human_output(self):
        import argparse
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.cmd_earn_now(argparse.Namespace(min_usd=5.0, limit=None, json=False))
        self.assertIn("filter: est_usd >= 5", buf.getvalue())
        self.assertIn("3 item(s)", buf.getvalue())


class TestFixtureShape(unittest.TestCase):
    """fixtures/earn-now.sample.json is a real captured response."""

    def setUp(self):
        path = os.path.join(ROOT, "fixtures", "earn-now.sample.json")
        with open(path, encoding="utf-8") as fh:
            self.data = json.load(fh)

    def test_fixture_parses_and_filters(self):
        items = abf.filter_items(self.data["items"], 5)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[-1]["id"], "direct:abf-growth-guide-5usdc")

    def test_fixture_has_required_fields(self):
        for item in self.data["items"]:
            self.assertIn("id", item)
            self.assertIn("title", item)
            self.assertIn("requires", item)


if __name__ == "__main__":
    unittest.main(verbosity=2)
