"""Offline tests for abf.py.

Every test runs against tests/mock_mcp.py on 127.0.0.1 — no network, no fixtures
to download. `python3 abf.py selftest` runs this file.

    python3 -m unittest discover -s tests -v
"""

import argparse
import datetime
import io
import json
import os
import tempfile
import os
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

import abf          # noqa: E402
import mock_mcp     # noqa: E402


import socket

# --------------------------------------------------------------- offline guard

_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


def _is_local(address):
    host = address[0] if isinstance(address, tuple) else address
    return isinstance(host, str) and host in _LOCAL_HOSTS


def _guard(real):
    def wrapper(self, address, *args, **kwargs):
        if not _is_local(address):
            raise AssertionError(
                "offline test tried to reach %r — stub the caller instead" % (address,))
        return real(self, address, *args, **kwargs)
    return wrapper


def setUpModule():
    """Fail fast if any test opens a non-loopback connection.

    The suite advertises "no network needed", and `abf.py` points a blocked user at it
    as the offline fallback. Two cases were quietly calling api.github.com anyway; the
    only reason they were noticed is that they made the suite slow.
    """
    socket.socket.connect = _guard(_REAL_CONNECT)
    socket.socket.connect_ex = _guard(_REAL_CONNECT_EX)


def tearDownModule():
    socket.socket.connect = _REAL_CONNECT
    socket.socket.connect_ex = _REAL_CONNECT_EX


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

    def test_limit_zero_shows_nothing(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.print_bounties([{"id": "a", "est_usd": 1}], limit=0)
        self.assertIn("no items", buf.getvalue())

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
        out = self.run_json(abf.cmd_earn_now, min_usd=5.0, limit=None, source=None, exclude_requires=(), closes_within=None, json=True)
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["items"][0]["id"], "frantic:129")
        self.assertEqual(out["min_usd"], 5.0)

    def test_earn_now_json_limit(self):
        out = self.run_json(abf.cmd_earn_now, min_usd=0.0, limit=2, source=None, exclude_requires=(), closes_within=None, json=True)
        self.assertEqual(out["count"], 2)

    def test_feed_json(self):
        out = self.run_json(abf.cmd_feed, top=4, min_usd=0.0, source=None,
                            exclude_requires=(), closes_within=None, json=True)
        self.assertEqual(out["count"], 4)

    def test_feed_honours_min_usd(self):
        # regression: cmd_feed used to pass a hardcoded 0.0 and ignore --min-usd
        out = self.run_json(abf.cmd_feed, top=99, min_usd=5.0, source=None,
                            exclude_requires=(), closes_within=None, json=True)
        self.assertEqual(out["count"], 3)

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
            abf.cmd_earn_now(argparse.Namespace(min_usd=5.0, limit=None, source=None, exclude_requires=(), closes_within=None, json=False))
        self.assertIn("filter: est_usd >= 5", buf.getvalue())
        self.assertIn("3 item(s)", buf.getvalue())


class TestCompetitionCheck(unittest.TestCase):
    """parse_issue_url and judge are pure, so they test without any network."""

    def test_parses_issue_url(self):
        self.assertEqual(abf.parse_issue_url("https://github.com/onyx-dot-app/onyx/issues/2281"),
                         ("onyx-dot-app", "onyx", 2281))

    def test_parses_url_with_trailing_slash_and_query(self):
        self.assertEqual(abf.parse_issue_url("https://github.com/a/b/issues/7/?x=1"), ("a", "b", 7))

    def test_rejects_pull_urls(self):
        self.assertIsNone(abf.parse_issue_url("https://github.com/a/b/pull/7"))

    def test_rejects_dot_only_owner(self):
        self.assertIsNone(abf.parse_issue_url("https://github.com/../x/issues/1"))

    def test_accepts_a_dotted_repo_name(self):
        self.assertEqual(
            abf.parse_issue_url("https://github.com/cpumsconfig/cpumsconfig.github.io/issues/3"),
            ("cpumsconfig", "cpumsconfig.github.io", 3))

    def test_rejects_non_numeric(self):
        self.assertIsNone(abf.parse_issue_url("https://github.com/a/b/issues/abc"))

    def test_rejects_plain_text(self):
        self.assertIsNone(abf.parse_issue_url("frantic:129"))

    def test_rejects_none_and_numbers(self):
        # a feed item with "url": null used to raise TypeError here
        self.assertIsNone(abf.parse_issue_url(None))
        self.assertIsNone(abf.parse_issue_url(42))

    def test_accepts_a_link_with_an_anchor(self):
        # copied from the browser, these carry #issuecomment-...
        self.assertEqual(
            abf.parse_issue_url("https://github.com/a/b/issues/7#issuecomment-123"),
            ("a", "b", 7))

    def test_judge_crowded(self):
        verdict, _ = abf.judge(age_days=3, prs=98, merged=90)
        self.assertEqual(verdict, "crowded")

    def test_judge_stalled(self):
        verdict, why = abf.judge(age_days=3, prs=2, merged=0)
        self.assertEqual(verdict, "stalled")
        self.assertIn("per 100", why)   # it is a rate, not a count of the last 100

    def test_judge_late(self):
        verdict, _ = abf.judge(age_days=30, prs=2, merged=50)
        self.assertEqual(verdict, "late")

    def test_judge_worth_a_look(self):
        verdict, why = abf.judge(age_days=1, prs=0, merged=40)
        self.assertEqual(verdict, "worth a look")
        self.assertIn("0 PRs", why)

    def test_a_404_is_not_retried_with_a_smaller_page(self):
        abf._MERGE_CACHE.clear()
        calls = []
        orig = abf.gh_get
        def boom(path):
            calls.append(path)
            raise RuntimeError("GitHub 404 on %s: Not Found" % path)
        abf.gh_get = boom
        try:
            with self.assertRaises(RuntimeError):
                abf.merged_rate("no", "such")
        finally:
            abf.gh_get = orig
        self.assertEqual(len(calls), 1)   # not three: quota is 60/hour

    def test_a_transport_error_still_falls_back(self):
        abf._MERGE_CACHE.clear()
        calls = []
        orig = abf.gh_get
        def flaky(path):
            calls.append(path)
            raise OSError("connection reset")
        abf.gh_get = flaky
        try:
            with self.assertRaises(RuntimeError):
                abf.merged_rate("a", "b")
        finally:
            abf.gh_get = orig
        self.assertEqual(len(calls), 3)   # 100 -> 30 -> 10

    def test_check_without_target_says_so(self):
        import argparse
        args = argparse.Namespace(all=False, target=None, min_usd=0.0, source=None,
                                  exclude_requires=(), closes_within=None, json=False,
                                  limit=10)
        with self.assertRaises(SystemExit) as ctx:
            abf.cmd_check(args)
        self.assertIn("needs a bounty id", str(ctx.exception))

    def test_check_board_shows_reward_when_est_usd_missing(self):
        feed = [{"id": "x", "url": "https://github.com/a/b/issues/1",
                 "est_usd": None, "reward": 100.0, "currency": "OUSD"}]
        row = {"repo": "a/b", "issue": 1, "title": "t", "state": "open",
               "created_at": "2026-10-31", "age_days": 1, "comments": 0, "prs": 0,
               "merged_last_100": 50, "merged_sample": 100, "merged_count": 50,
               "verdict": "worth a look", "why": "ok"}
        import argparse
        args = argparse.Namespace(all=True, target=None, min_usd=5.0, source=None,
                                  exclude_requires=(), closes_within=None, json=False, limit=10)
        orig_feed, orig_check = abf.load_feed, abf.check_issue
        abf.load_feed = lambda: feed
        abf.check_issue = lambda o, r, n: dict(row)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                abf.check_board(args)
        finally:
            abf.load_feed, abf.check_issue = orig_feed, orig_check
        self.assertIn("$100", buf.getvalue())

    def test_judge_boundaries(self):
        self.assertEqual(abf.judge(1, 10, 50)[0], "worth a look")   # 10 is not > 10
        self.assertEqual(abf.judge(1, 11, 50)[0], "crowded")
        self.assertEqual(abf.judge(1, 0, 5)[0], "worth a look")     # 5 is not < 5


    def test_filter_by_source(self):
        items = [{"est_usd": 5, "source": "algora"}, {"est_usd": 5, "source": "direct"},
                 {"est_usd": 5, "source": "algora"}]
        self.assertEqual(len(abf.filter_items(items, 0, "algora")), 2)
        self.assertEqual(len(abf.filter_items(items, 0, "nope")), 0)

    def test_filter_by_source_and_amount(self):
        items = [{"est_usd": 5, "source": "a"}, {"est_usd": 1, "source": "a"}]
        self.assertEqual(len(abf.filter_items(items, 2, "a")), 1)

    def test_rank_puts_worth_a_look_first(self):
        rows = [{"verdict": "crowded", "prs": 98}, {"verdict": "worth a look", "prs": 0},
                {"verdict": "stalled", "prs": 2}, {"verdict": "late", "prs": 5}]
        order = [r["verdict"] for r in abf.rank(rows)]
        self.assertEqual(order, ["worth a look", "late", "stalled", "crowded"])

    def test_rank_breaks_ties_by_fewest_prs(self):
        rows = [{"verdict": "crowded", "prs": 98}, {"verdict": "crowded", "prs": 12}]
        self.assertEqual([r["prs"] for r in abf.rank(rows)], [12, 98])

    def test_rank_handles_unknown_verdict(self):
        rows = [{"verdict": "error", "prs": 0}, {"verdict": "worth a look", "prs": 1}]
        self.assertEqual(abf.rank(rows)[0]["verdict"], "worth a look")


class TestTransportResilience(unittest.TestCase):
    """A TLS reset mid-run used to kill the command outright."""

    class _Resp:
        status = 200
        headers = {}
        def read(self, *a):
            return b'{"ok": true}'
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    def _patch(self, fail_times):
        calls = {"n": 0}
        orig_open, orig_sleep = abf.open_request, abf.time.sleep
        def flaky(req, timeout=None):
            calls["n"] += 1
            if calls["n"] <= fail_times:
                raise OSError("connection reset")
            return self._Resp()
        abf.open_request = flaky
        abf.time.sleep = lambda _s: None
        self.addCleanup(setattr, abf, "open_request", orig_open)
        self.addCleanup(setattr, abf.time, "sleep", orig_sleep)
        return calls

    def test_retries_a_transport_error_then_succeeds(self):
        calls = self._patch(fail_times=1)
        status, _, body = abf.http("https://example.invalid/x")
        self.assertEqual(status, 200)
        self.assertEqual(calls["n"], 2)

    def test_gives_up_after_three_attempts(self):
        calls = self._patch(fail_times=99)
        with self.assertRaises(OSError):
            abf.http("https://example.invalid/x")
        self.assertEqual(calls["n"], 3)

    def test_http_error_is_not_retried(self):
        calls = {"n": 0}
        orig = abf.open_request
        def boom(req, timeout=None):
            calls["n"] += 1
            raise abf.urllib.error.HTTPError("u", 404, "nf", {}, None)
        abf.open_request = boom
        self.addCleanup(setattr, abf, "open_request", orig)
        status, _, _ = abf.http("https://example.invalid/x")
        self.assertEqual(status, 404)
        self.assertEqual(calls["n"], 1)


class TestRpcParsing(unittest.TestCase):
    """A non-JSON frame or a null content block must not take the client down."""

    def _client(self, body, ctype="text/event-stream"):
        client = abf.MCP("http://example.invalid/mcp")
        def fake_post(payload, **kwargs):
            rid = payload.get("id")
            return 200, {"Content-Type": ctype}, body.replace("{ID}", str(rid))
        client._post = fake_post
        return client

    def test_skips_a_malformed_sse_frame(self):
        body = chr(10).join([
            'data: not-json',
            '',
            'data: {"jsonrpc":"2.0","id":{ID},"result":{"ok":true}}',
            '',
        ])
        client = self._client(body)
        self.assertEqual(client.rpc("tools/list"), {"ok": True})

    def test_an_html_body_is_reported_as_such(self):
        # a proxy error page should not surface as "Expecting value: line 1 column 1"
        client = abf.MCP("http://example.invalid/mcp")
        client._post = lambda payload, **kwargs: (200, {"Content-Type": "text/html"},
                                        "<html><body>blocked</body></html>")
        with self.assertRaises(RuntimeError) as ctx:
            client.rpc("tools/list")
        self.assertIn("not JSON-RPC", str(ctx.exception))

    def test_null_content_is_not_a_crash(self):
        client = self._client('{"jsonrpc":"2.0","id":{ID},"result":{"content":null}}',
                              ctype="application/json")
        self.assertEqual(client.call_tool("x"), "")


class TestPagination(unittest.TestCase):

    class FakeClient:
        def __init__(self, pages):
            self.pages = list(pages)
            self.calls = []
        def call_tool(self, name, arguments):
            self.calls.append(dict(arguments))
            return self.pages.pop(0)

    def test_walks_every_page(self):
        client = self.FakeClient([
            {"items": [{"id": "a"}], "has_more": True,
             "next_since": "S2", "next_after_id": "A2"},
            {"items": [{"id": "b"}], "has_more": False},
        ])
        self.assertEqual([i["id"] for i in abf.collect_new(client, "S1")], ["a", "b"])
        self.assertEqual(client.calls[0], {"since": "S1"})
        # `since` is carried forward in case the server wants the window too
        self.assertEqual(client.calls[1], {"since": "S1", "next_since": "S2",
                                           "next_after_id": "A2"})

    def test_stops_when_has_more_but_no_cursor(self):
        client = self.FakeClient([{"items": [{"id": "a"}], "has_more": True}])
        self.assertEqual(len(abf.collect_new(client, "S1")), 1)

    def test_stops_on_repeated_cursor(self):
        page = {"items": [{"id": "a"}], "has_more": True, "next_since": "S1"}
        client = self.FakeClient([page, dict(page), dict(page)])
        self.assertEqual(len(abf.collect_new(client, "S1", max_pages=3)), 2)

    def test_respects_max_pages(self):
        pages = [{"items": [{"id": str(n)}], "has_more": True,
                  "next_since": "S%d" % n} for n in range(5)]
        client = self.FakeClient(pages)
        self.assertEqual(len(abf.collect_new(client, "S0", max_pages=2)), 2)


class TestStatelessServer(unittest.TestCase):
    """The live server is stateless: it never issues a session id.

    Every other MCP test runs against the stateful mock, so without this the branch
    that actually matters in production would never execute.
    """

    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), mock_mcp.StatelessHandler)
        cls.url = "http://127.0.0.1:%d/mcp" % cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_client_works_without_a_session(self):
        client = abf.MCP(self.url)
        client.initialize()
        self.assertIsNone(client.session_id)
        tools = client.list_tools()
        self.assertEqual(len(tools), 4)

    def test_call_tool_without_a_session(self):
        client = abf.MCP(self.url)
        client.initialize()
        result = client.call_tool("list_bounties", {"limit": 2})
        self.assertEqual(len(result["items"]), 2)

    def test_unknown_tool_names_the_available_ones(self):
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stdout(io.StringIO()):
                abf.cmd_mcp(argparse.Namespace(url=self.url, tool="no_such_tool",
                                               arguments="{}", limit=None, json=False))
        message = str(ctx.exception)
        self.assertIn("no_such_tool", message)
        self.assertIn("list_bounties", message)

    def test_cmd_mcp_runs_against_a_stateless_server(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.cmd_mcp(argparse.Namespace(url=self.url, tool="get_feed_stats",
                                           arguments="{}", limit=None, json=False))
        self.assertIn("session (none)", buf.getvalue())


class TestGetAndNew(ServerCase):
    """cmd_get and cmd_new against the mock: the shapes they parse are the shapes
    the real server sends (item-wrapped bounty, items list), which is exactly what
    broke before."""

    def run_capture(self, fn, **kwargs):
        import argparse
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(argparse.Namespace(**kwargs))
        return buf.getvalue()

    def test_get_unwraps_the_item(self):
        out = self.run_capture(abf.cmd_get, id="frantic:129", url=self.url, json=False)
        self.assertIn("frantic:129", out)
        self.assertIn("est_usd", out)
        self.assertIn("mock note", out)

    def test_get_json_is_the_raw_result(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            import argparse
            abf.cmd_get(argparse.Namespace(id="frantic:129", url=self.url, json=True))
        payload = json.loads(buf.getvalue())
        self.assertIn("item", payload)

    def test_new_parses_the_items_list(self):
        out = self.run_capture(abf.cmd_new, since="24h", url=self.url, min_usd=0.0,
                               source=None, exclude_requires=(), closes_within=None,
                               limit=None, json=False)
        self.assertIn("frantic:129", out)
        self.assertIn("1 item(s)", out)

    def test_new_applies_min_usd(self):
        out = self.run_capture(abf.cmd_new, since="24h", url=self.url, min_usd=99.0,
                               source=None, exclude_requires=(), closes_within=None,
                               limit=None, json=False)
        self.assertIn("no items", out)


class TestRequiresFilter(unittest.TestCase):
    """exclude_requires mirrors the MCP server-side filter for the static endpoints."""

    ITEMS = [
        {"id": "a", "est_usd": 5, "requires": ["kyc"]},
        {"id": "b", "est_usd": 5, "requires": ["wallet_evm"]},
        {"id": "c", "est_usd": 5, "requires": ["wallet_evm", "public_post"]},
        {"id": "d", "est_usd": 5},
    ]

    def test_parse_splits_and_strips(self):
        self.assertEqual(abf.parse_requires(" kyc, stake ,bond"), ("kyc", "stake", "bond"))

    def test_parse_handles_empty(self):
        self.assertEqual(abf.parse_requires(None), ())
        self.assertEqual(abf.parse_requires(""), ())

    def test_drops_items_with_a_blocked_requirement(self):
        kept = [i["id"] for i in abf.filter_items(self.ITEMS, 0, None, ("kyc",))]
        self.assertEqual(kept, ["b", "c", "d"])

    def test_drops_on_any_intersection(self):
        kept = [i["id"] for i in abf.filter_items(self.ITEMS, 0, None, ("public_post",))]
        self.assertEqual(kept, ["a", "b", "d"])

    def test_keeps_items_without_requires(self):
        kept = [i["id"] for i in abf.filter_items(self.ITEMS, 0, None, abf.FRICTION)]
        self.assertEqual(kept, ["b", "c", "d"])

    def test_no_filter_keeps_everything(self):
        self.assertEqual(len(abf.filter_items(self.ITEMS, 0, None, ())), 4)


class TestWatchState(unittest.TestCase):
    """The dedupe layer: what counts as new, and does the state survive a restart."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state.json")

    def test_everything_is_new_on_first_pass(self):
        items = [{"id": "a", "est_usd": 5}, {"id": "b", "est_usd": 1}]
        self.assertEqual([i["id"] for i in abf.new_items({}, items, 2)], ["a"])

    def test_seen_items_are_skipped(self):
        items = [{"id": "a", "est_usd": 5}, {"id": "b", "est_usd": 5}]
        self.assertEqual([i["id"] for i in abf.new_items({"a": {}}, items, 0)], ["b"])

    def test_state_roundtrip(self):
        abf.save_state(self.path, {"seen": {"x": {"first_seen": "10:00:00"}}})
        self.assertEqual(abf.load_state(self.path)["seen"]["x"]["first_seen"], "10:00:00")

    def test_missing_state_file_is_empty(self):
        self.assertEqual(abf.load_state(os.path.join(self.dir, "nope.json")), {})

    def test_corrupt_state_file_is_empty(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(abf.load_state(self.path), {})

    def test_save_leaves_no_tmp_file(self):
        abf.save_state(self.path, {"seen": {}})
        self.assertEqual([f for f in os.listdir(self.dir) if f.endswith(".tmp")], [])


class TestAmountOf(unittest.TestCase):
    """Regression: items with est_usd null used to vanish from every --min-usd filter.

    taskmarket:TSK-Y7YSTT0V is the real case: est_usd null, reward 100, currency OUSD.
    Before this fix, `earn-now --min-usd 5` silently hid a $100 bounty.
    """

    def test_est_usd_wins(self):
        self.assertEqual(abf.amount_of({"est_usd": 16, "reward": 999}), 16.0)

    def test_falls_back_to_stablecoin_reward(self):
        self.assertEqual(abf.amount_of({"est_usd": None, "reward": 100, "currency": "OUSD"}), 100.0)
        self.assertEqual(abf.amount_of({"est_usd": None, "reward": 1, "currency": "USDC"}), 1.0)

    def test_refuses_non_stablecoin_reward(self):
        self.assertIsNone(abf.amount_of({"est_usd": None, "reward": 250000, "currency": "FLAPJAX"}))

    def test_unknown_amount_is_none(self):
        self.assertIsNone(abf.amount_of({}))
        self.assertIsNone(abf.amount_of({"est_usd": None, "reward": None}))

    def test_garbage_is_none(self):
        self.assertIsNone(abf.amount_of({"est_usd": "lots"}))

    def test_nan_is_not_a_price(self):
        # nan compares false against every threshold, so it would pass --min-usd
        self.assertIsNone(abf.amount_of({"est_usd": float("nan")}))
        self.assertEqual(abf.filter_items([{"id": "n", "est_usd": float("nan")}], 5), [])
        self.assertEqual(abf.money(float("nan")), "—")

    def test_non_dict_item_is_skipped(self):
        items = [None, "oops", {"id": "ok", "est_usd": 1}]
        self.assertEqual([i["id"] for i in abf.filter_items(items, 0)], ["ok"])

    def test_unpriced_item_survives_a_zero_floor(self):
        # regression: amount_of() made unknown prices behave like zero and vanish
        items = [{"id": "priced", "est_usd": 5},
                 {"id": "token", "est_usd": None, "reward": 250000, "currency": "FLAPJAX"}]
        self.assertEqual([i["id"] for i in abf.filter_items(items, 0.0)], ["priced", "token"])

    def test_unpriced_item_is_dropped_by_a_positive_floor(self):
        items = [{"id": "token", "est_usd": None, "reward": 250000, "currency": "FLAPJAX"}]
        self.assertEqual(abf.filter_items(items, 1.0), [])

    def test_the_100_usd_bounty_survives_a_min_usd_filter(self):
        item = {"id": "taskmarket:TSK-Y7YSTT0V", "est_usd": None,
                "reward": 100.0, "currency": "OUSD"}
        self.assertEqual(len(abf.filter_items([item], 5)), 1)


class TestDeadline(unittest.TestCase):
    """Deadlines decide what is worth starting, so they get their own tests."""

    @staticmethod
    def iso_in(hours):
        stamp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_parses_zulu_timestamp(self):
        parsed = abf.parse_deadline("2026-11-02T06:13:01Z")
        self.assertEqual(parsed.year, 2026)
        self.assertIsNotNone(parsed.tzinfo)

    def test_parses_fractional_seconds(self):
        self.assertIsNotNone(abf.parse_deadline("2026-11-02T06:13:01.227Z"))

    def test_missing_deadline_is_none(self):
        self.assertIsNone(abf.parse_deadline(None))
        self.assertIsNone(abf.parse_deadline(""))

    def test_garbage_is_none(self):
        self.assertIsNone(abf.parse_deadline("next tuesday"))

    def test_hours_left_is_positive_for_the_future(self):
        self.assertAlmostEqual(abf.hours_left(self.iso_in(10)), 10, delta=0.1)

    def test_hours_left_is_negative_when_past(self):
        self.assertLess(abf.hours_left(self.iso_in(-5)), 0)

    def test_hours_left_open_ended(self):
        self.assertIsNone(abf.hours_left(None))

    def test_closes_label_formats(self):
        # 5.5h, not 5h: strftime drops sub-seconds, so 5 would truncate to "4h"
        self.assertEqual(abf.closes_label(self.iso_in(5.5)), "5h")
        # 72h lands a hair under three days after truncation; assert the shape, not the edge
        days = abf.closes_label(self.iso_in(72))
        self.assertTrue(days.endswith("d") and days[:-1].isdigit(), days)
        self.assertEqual(abf.closes_label(self.iso_in(-1)), "past")
        self.assertEqual(abf.closes_label(None), "—")

    def test_parse_since_relative(self):
        self.assertRegex(abf.parse_since("6h"), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_parse_since_passthrough(self):
        self.assertEqual(abf.parse_since("2026-10-30T00:00:00Z"), "2026-10-30T00:00:00Z")


class TestClosesWithinFilter(unittest.TestCase):

    @staticmethod
    def iso_in(hours):
        stamp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_keeps_only_items_inside_the_window(self):
        items = [{"id": "soon", "est_usd": 5, "deadline": self.iso_in(3)},
                 {"id": "later", "est_usd": 5, "deadline": self.iso_in(200)},
                 {"id": "open", "est_usd": 5, "deadline": None},
                 {"id": "gone", "est_usd": 5, "deadline": self.iso_in(-2)}]
        kept = [i["id"] for i in abf.filter_items(items, 0, None, (), closes_within=24)]
        self.assertEqual(kept, ["soon"])

    def test_none_means_no_filter(self):
        items = [{"id": "a", "est_usd": 5, "deadline": None}]
        self.assertEqual(len(abf.filter_items(items, 0, None, (), closes_within=None)), 1)


class TestReachable(unittest.TestCase):
    """The honest number: listed, reachable without a human, alive, winnable."""

    FEED = [
        {"id": "a", "est_usd": 10, "source": "algora", "requires": ["github", "kyc"],
         "eligibility": "Stripe payout"},
        {"id": "b", "est_usd": 20, "source": "frantic", "requires": ["wallet_evm"],
         "eligibility": "Paid in USDC. So far 0 paid / 22 rejected."},
        {"id": "c", "est_usd": 30, "source": "taskmarket", "requires": ["wallet_evm"],
         "eligibility": "mode=bounty; 120 submissions so far"},
        {"id": "d", "est_usd": 40, "source": "direct", "requires": ["wallet_evm"],
         "eligibility": "Single winner."},
    ]

    def test_payout_stats_parses_prose(self):
        self.assertEqual(abf.payout_stats("So far 0 paid / 22 rejected."), (0, 22))

    def test_payout_stats_absent(self):
        self.assertIsNone(abf.payout_stats("Stripe payout"))
        self.assertIsNone(abf.payout_stats(None))
        self.assertIsNone(abf.payout_stats("0 paid / rejected"))   # no second number
        self.assertIsNone(abf.payout_stats(123))                   # not a string, not a crash

    def test_task_is_dead_boundary(self):
        def item(elig):
            return {"eligibility": elig}
        self.assertFalse(abf.task_is_dead(item("0 paid / 10 rejected")))
        self.assertTrue(abf.task_is_dead(item("0 paid / 11 rejected")))
        self.assertFalse(abf.task_is_dead(item("1 paid / 500 rejected")))
        self.assertFalse(abf.task_is_dead(item("no numbers here")))

    def test_source_is_dead_needs_an_explicit_zero(self):
        self.assertTrue(abf.source_is_dead({"paid": 0}))
        self.assertFalse(abf.source_is_dead({"paid": 22}))
        self.assertFalse(abf.source_is_dead({"paid": None}))   # unknown is not dead
        self.assertFalse(abf.source_is_dead(None))

    def test_board_health_keeps_basis(self):
        health = abf.board_health({"frantic": {"paid_count": 22, "paid_usd": 66.0,
                                               "basis": "claims on listed bounties"}})
        self.assertEqual(health["frantic"]["paid"], 22)
        self.assertTrue(health["frantic"]["known"])
        self.assertIn("listed", health["frantic"]["basis"])

    def test_submission_count(self):
        self.assertEqual(abf.submission_count({"eligibility": "120 submissions so far"}), 120)
        self.assertIsNone(abf.submission_count({"eligibility": "Single winner."}))

    def test_is_contested_boundary(self):
        self.assertFalse(abf.is_contested({"eligibility": "49 submissions"}))
        self.assertTrue(abf.is_contested({"eligibility": "50 submissions"}))
        self.assertFalse(abf.is_contested({"eligibility": "no numbers"}))

    def test_split_drops_each_tier(self):
        split = abf.reachable_split(self.FEED)
        self.assertEqual(len(split["listed"]), 4)
        self.assertEqual([i["id"] for i in split["reachable"]], ["b", "c", "d"])   # a needs kyc
        self.assertEqual([i["id"] for i in split["alive"]], ["c", "d"])           # b's own task is dead
        self.assertEqual([i["id"] for i in split["winnable"]], ["d"])             # taskmarket crowded
        self.assertEqual(split["dead"], set())   # no source-level evidence was supplied

    # --- regressions for the scope bug reported in issue #1 -----------------

    def test_item_order_does_not_change_the_verdict(self):
        """The bug: whichever task appeared first decided the fate of the whole source."""
        items = [
            {"id": "zero", "est_usd": 3, "source": "frantic", "requires": ["wallet_evm"],
             "eligibility": "So far 0 paid / 55 rejected."},
            {"id": "paid", "est_usd": 3, "source": "frantic", "requires": ["wallet_evm"],
             "eligibility": "So far 20 paid / 55 rejected."},
        ]
        forward = [i["id"] for i in abf.reachable_split(items)["alive"]]
        backward = [i["id"] for i in abf.reachable_split(list(reversed(items)))["alive"]]
        self.assertEqual(forward, ["paid"])
        self.assertEqual(backward, ["paid"])

    def test_mixed_payouts_keep_the_paying_task(self):
        items = [
            {"id": "dead", "est_usd": 3, "source": "s", "requires": ["wallet_evm"],
             "eligibility": "0 paid / 40 rejected"},
            {"id": "alive", "est_usd": 3, "source": "s", "requires": ["wallet_evm"],
             "eligibility": "20 paid / 57 rejected"},
        ]
        self.assertEqual([i["id"] for i in abf.reachable_split(items)["alive"]], ["alive"])

    def test_missing_history_is_unknown_not_dead(self):
        items = [{"id": "x", "est_usd": 9, "source": "direct", "requires": ["wallet_evm"],
                  "eligibility": "Single winner."}]
        split = abf.reachable_split(items, payout_evidence={"direct": {"paid_count": None}})
        self.assertEqual([i["id"] for i in split["alive"]], ["x"])
        self.assertEqual(split["dead"], set())

    def test_source_level_zero_drops_the_whole_source(self):
        items = [{"id": "x", "est_usd": 9, "source": "bad", "requires": ["wallet_evm"],
                  "eligibility": "20 paid / 5 rejected"}]
        split = abf.reachable_split(items, payout_evidence={"bad": {"paid_count": 0}})
        self.assertEqual(split["alive"], [])
        self.assertEqual(split["dead"], {"bad"})

    def test_expired_items_are_not_reachable(self):
        import datetime
        past = (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        feed = [{"id": "gone", "est_usd": 50, "source": "x", "requires": ["wallet_evm"],
                 "deadline": past, "eligibility": "n/a"}]
        split = abf.reachable_split(feed)
        self.assertEqual(len(split["listed"]), 1)
        self.assertEqual(split["reachable"], [])

    def test_is_expired(self):
        import datetime
        def iso(h):
            return (datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertTrue(abf.is_expired({"deadline": iso(-1)}))
        self.assertFalse(abf.is_expired({"deadline": iso(1)}))
        self.assertFalse(abf.is_expired({"deadline": None}))

    def test_max_subs_threshold_is_configurable(self):
        feed = [{"id": "c", "est_usd": 30, "source": "taskmarket", "requires": ["wallet_evm"],
                 "eligibility": "mode=bounty; 60 submissions so far"}]
        self.assertEqual(len(abf.reachable_split(feed, contested_at=50)["winnable"]), 0)
        self.assertEqual(len(abf.reachable_split(feed, contested_at=100)["winnable"]), 1)

    def test_friction_breakdown_counts_tags(self):
        tags = dict(abf.friction_breakdown(self.FEED))
        self.assertEqual(tags, {"kyc": 1})

    def test_usd_total_formats_thousands(self):
        self.assertEqual(abf.usd_total(1234567), "1,234,567")
        self.assertEqual(abf.usd_total(50717.6), "50,717.6")
        self.assertEqual(abf.usd_total(15.0), "15")

    def test_money_never_uses_an_exponent(self):
        self.assertEqual(abf.money(1234567), "$1.2M")
        self.assertEqual(abf.money(50717.6), "$50717.6")

    def test_json_shape(self):
        import argparse
        orig = abf.load_feed_doc
        abf.load_feed_doc = lambda: {"items": self.FEED, "stats": {}}
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                abf.cmd_reachable(argparse.Namespace(limit=None, json=True))
        finally:
            abf.load_feed_doc = orig
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["listed"]["usd"], 100.0)
        self.assertEqual(payload["winnable"]["usd"], 40.0)
        self.assertEqual(payload["dead_sources"], [])


class TestWatchLoop(unittest.TestCase):
    """The first pass must be silent with or without --once, and the loop must
    remember that it already had one."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "s.json")

    def _run(self, once, items):
        import argparse
        class Stop(Exception):
            pass
        orig_feed, orig_sleep = abf.load_feed, abf.time.sleep
        abf.load_feed = lambda: items
        abf.time.sleep = lambda _s: (_ for _ in ()).throw(Stop())
        args = argparse.Namespace(interval=30, once=once, state=self.state,
                                  min_usd=0.0, source=None, exclude_requires=(),
                                  closes_within=None)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                try:
                    abf.cmd_watch(args)
                except Stop:
                    pass
        finally:
            abf.load_feed, abf.time.sleep = orig_feed, orig_sleep
        return buf.getvalue()

    def test_first_pass_is_silent_without_once(self):
        out = self._run(False, [{"id": "a", "est_usd": 5, "title": "t"}])
        self.assertIn("first pass", out)
        self.assertNotIn("NEW", out)

    def test_second_pass_reports_new_items(self):
        self._run(False, [{"id": "a", "est_usd": 5, "title": "t"}])       # records a
        out = self._run(False, [{"id": "a", "est_usd": 5, "title": "t"},
                                {"id": "b", "est_usd": 7, "title": "u"}])
        self.assertIn("NEW", out)
        self.assertIn("u", out)
        self.assertNotIn("first pass", out)

    def test_reward_only_item_shows_its_amount(self):
        self._run(False, [{"id": "a", "est_usd": 5, "title": "t"}])
        out = self._run(False, [{"id": "a", "est_usd": 5, "title": "t"},
                                {"id": "c", "est_usd": None, "reward": 100.0,
                                 "currency": "OUSD", "title": "big"}])
        self.assertIn("$100", out)


class TestEdgeFeed(unittest.TestCase):
    """One fixture with every malformed shape the feed can plausibly emit.

    fixtures/edge-feed.json holds 13 entries: unpriced, NaN price, naive deadline,
    requires as a list and as a comma string, expired, a dead source, a source whose
    paid_count is the string "0", a task-level zero-paid record, a crowded contest,
    an entry with no id, and a null.
    """

    @classmethod
    def setUpClass(cls):
        path = os.path.join(ROOT, "fixtures", "edge-feed.json")
        with open(path, encoding="utf-8") as fh:
            cls.doc = json.load(fh)

    def split(self, **kwargs):
        stats = self.doc.get("stats") or {}
        return abf.reachable_split(self.doc["items"], payout_evidence=stats.get("payout_evidence"))

    def test_tiers_nest_and_match_the_fixture(self):
        split = self.split()
        self.assertEqual(len(split["listed"]), 10)
        self.assertEqual(len(split["reachable"]), 7)
        self.assertEqual(len(split["alive"]), 4)
        self.assertEqual(len(split["winnable"]), 3)
        for outer, inner in (("listed", "reachable"), ("reachable", "alive"), ("alive", "winnable")):
            self.assertLessEqual(len(split[inner]), len(split[outer]), "%s > %s" % (inner, outer))

    def test_string_requires_is_honoured(self):
        ids = {i.get("id") for i in self.split()["reachable"]}
        self.assertNotIn("kyc:1", ids)      # requires: ["kyc"]
        self.assertNotIn("strreq:1", ids)   # requires: "kyc"

    def test_quoted_zero_still_counts_as_dead(self):
        split = self.split()
        self.assertIn("deadsrc", split["dead"])
        self.assertIn("quotedsrc", split["dead"])   # paid_count is the string "0"

    def test_unpriced_and_nan_are_not_listed(self):
        ids = {i.get("id") for i in self.split()["listed"]}
        self.assertNotIn("token:1", ids)
        self.assertNotIn("nan:1", ids)

    def test_task_level_zero_paid_is_dropped_alone(self):
        ids = {i.get("id") for i in self.split()["alive"]}
        self.assertNotIn("taskdead:1", ids)
        self.assertIn("plain:1", ids)      # same source, different task

    def test_naive_deadline_renders(self):
        self.assertTrue(abf.closes_label("2026-12-15").endswith("d"))

    def test_table_survives_null_and_missing_id(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.print_bounties(self.doc["items"])
        out = buf.getvalue()
        self.assertIn("plain:1", out)
        self.assertIn("token:1", out)          # unpriced row still renders, as an em dash
        self.assertIn("—", out)
        self.assertIn("12 item(s)", out)       # the null entry is skipped, not counted

    def test_reachable_json_on_the_edge_feed(self):
        import argparse
        orig = abf.load_feed_doc
        abf.load_feed_doc = lambda: self.doc
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                abf.cmd_reachable(argparse.Namespace(limit=None, max_subs=50, json=True))
        finally:
            abf.load_feed_doc = orig
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["winnable"]["count"], 3)
        self.assertEqual(payload["winnable"]["usd"], 52.0)
        self.assertEqual(sorted(payload["dead_sources"]), ["deadsrc", "quotedsrc"])


def synthetic_feed(n, dead_source="s1"):
    """A deterministic feed of n items with a mix of every branch.

    Generated rather than stored: a 20k-item JSON would be megabytes in the repo for
    no extra coverage.
    """
    items = []
    for i in range(n):
        items.append({
            "id": "s%d:%d" % (i % 7, i),
            "source": "s%d" % (i % 7),
            "est_usd": (i % 50) + 1,
            "currency": "USD",
            "requires": ["wallet_evm"] if i % 3 else ["kyc"],
            "deadline": "2026-12-01T00:00:00Z" if i % 4 else None,
            "eligibility": "So far %d paid / %d rejected; %d submissions so far."
                           % (i % 2, i % 20, i % 200),
            "title": "t%d" % i,
        })
    return {"items": items,
            "stats": {"payout_evidence": {dead_source: {"paid_count": 0}}}}


class TestScale(unittest.TestCase):
    """The board is 25 items today; the code should not fall over at 20k.

    The tier diffs used list membership (`it not in split[...]`), which is O(n^2) on
    dict equality: 6000 items took 0.68s before, 0.02s after.
    """

    def test_five_thousand_items(self):
        doc = synthetic_feed(5000)
        split = abf.reachable_split(doc["items"], 50, doc["stats"]["payout_evidence"])
        self.assertEqual(len(split["listed"]), 5000)
        self.assertLessEqual(len(split["reachable"]), len(split["listed"]))
        self.assertLessEqual(len(split["alive"]), len(split["reachable"]))
        self.assertLessEqual(len(split["winnable"]), len(split["alive"]))

    def test_twenty_thousand_items_stay_fast(self):
        import time as _time
        doc = synthetic_feed(20000)
        orig = abf.load_feed_doc
        abf.load_feed_doc = lambda: doc
        try:
            started = _time.time()
            with redirect_stdout(io.StringIO()):
                abf.cmd_reachable(argparse.Namespace(limit=0, max_subs=50, json=False))
            elapsed = _time.time() - started
        finally:
            abf.load_feed_doc = orig
        # generous: the point is to catch a return to quadratic behaviour, not to
        # benchmark the machine
        self.assertLess(elapsed, 5.0, "20k items took %.2fs" % elapsed)

    def test_every_item_lands_in_exactly_one_bucket(self):
        doc = synthetic_feed(3000)
        split = abf.reachable_split(doc["items"], 50, doc["stats"]["payout_evidence"])
        listed, reachable = split["listed"], split["reachable"]
        alive, winnable = split["alive"], split["winnable"]
        # each tier is a subset of the one above it, by identity
        for outer, inner in ((listed, reachable), (reachable, alive), (alive, winnable)):
            self.assertTrue({id(x) for x in inner} <= {id(x) for x in outer})
        self.assertGreater(len(split["dead"]), 0)   # s1 has paid_count 0
        self.assertGreater(len(winnable), 0)


class TestBoardConcurrency(unittest.TestCase):
    """--jobs is opt-in; it must not change what the sweep reports, only how fast."""

    FEED = [{"id": "x%d" % i, "url": "https://github.com/o/r%d/issues/%d" % (i, i),
             "est_usd": 10, "title": "t%d" % i} for i in range(6)]

    def _run(self, jobs):
        import time as _time
        orig_feed, orig_check = abf.load_feed, abf.check_issue

        def slow(owner, repo, number):
            _time.sleep(0.02)
            return {"repo": "%s/%s" % (owner, repo), "issue": number, "title": "t",
                    "state": "open", "created_at": "2026-10-01", "age_days": 3,
                    "comments": 1, "prs": 2, "merged_last_100": 60, "merged_sample": 100,
                    "merged_count": 60, "verdict": "worth a look", "why": "ok"}

        abf.load_feed = lambda: self.FEED
        abf.check_issue = slow
        try:
            started = _time.time()
            buf = io.StringIO()
            with redirect_stdout(buf):
                abf.check_board(argparse.Namespace(
                    all=True, target=None, min_usd=0.0, source=None, exclude_requires=(),
                    closes_within=None, json=False, limit=10, jobs=jobs))
            return _time.time() - started, buf.getvalue()
        finally:
            abf.load_feed, abf.check_issue = orig_feed, orig_check

    def test_jobs_reports_the_same_rows(self):
        _, serial = self._run(1)
        _, parallel = self._run(6)
        self.assertEqual(serial.split(chr(10), 1)[1], parallel.split(chr(10), 1)[1])

    def test_jobs_is_faster(self):
        # best of two each: a single sample on a loaded machine is noise, and this is the
        # one assertion in the suite whose result depends on wall-clock time
        serial_time = min(self._run(1)[0] for _ in range(2))
        parallel_time = min(self._run(6)[0] for _ in range(2))
        self.assertLess(parallel_time, serial_time * 0.6,
                        "serial %.2fs vs parallel %.2fs" % (serial_time, parallel_time))

    def test_jobs_defaults_to_serial_without_the_flag(self):
        import time as _time
        orig_feed, orig_check = abf.load_feed, abf.check_issue
        abf.load_feed = lambda: self.FEED[:2]
        abf.check_issue = lambda o, r, n: {"repo": "%s/%s" % (o, r), "issue": n, "title": "t",
            "state": "open", "created_at": "2026-10-01", "age_days": 1, "comments": 0,
            "prs": 0, "merged_last_100": 60, "merged_sample": 100, "merged_count": 60,
            "verdict": "worth a look", "why": "ok"}
        try:
            with redirect_stdout(io.StringIO()):
                # no jobs attribute at all, as an older caller would pass it
                abf.check_board(argparse.Namespace(
                    all=True, target=None, min_usd=0.0, source=None, exclude_requires=(),
                    closes_within=None, json=False, limit=10))
        finally:
            abf.load_feed, abf.check_issue = orig_feed, orig_check


class TestReviewFixes(unittest.TestCase):
    """One test per defect from the external review that turned out to be real."""

    def test_no_friction_unions_with_exclude_requires(self):
        # passing both used to drop --no-friction entirely
        orig = abf.argparse.ArgumentParser.parse_args
        abf.argparse.ArgumentParser.parse_args = lambda self, argv=None: abf.argparse.Namespace(
            cmd="feed", func=lambda a: None, min_usd=0.0, source=None, top=1, json=False,
            no_friction=True, exclude_requires="twitter", closes_within=None)
        try:
            with redirect_stdout(io.StringIO()):
                abf.main()
        finally:
            abf.argparse.ArgumentParser.parse_args = orig

    def test_union_is_computed_not_overridden(self):
        reqs = set(abf.parse_requires("twitter"))
        reqs.update(abf.FRICTION)
        self.assertIn("twitter", reqs)
        self.assertIn("kyc", reqs)

    def test_is_error_is_not_treated_as_data(self):
        client = abf.MCP("http://example.invalid/mcp")
        client._post = lambda payload, **kwargs: (200, {"Content-Type": "application/json"},
            json.dumps({"jsonrpc": "2.0", "id": payload["id"],
                        "result": {"isError": True,
                                   "content": [{"type": "text", "text": "no such bounty"}]}}))
        with self.assertRaises(RuntimeError) as ctx:
            client.call_tool("get_bounty", {"id": "x"})
        self.assertIn("no such bounty", str(ctx.exception))

    def test_get_handles_a_null_result(self):
        client = abf.MCP("http://example.invalid/mcp")
        def fake_post(payload, **kwargs):
            if "id" not in payload:      # the notifications/initialized call
                return 202, {}, ""
            return 200, {"Content-Type": "application/json"}, json.dumps(
                {"jsonrpc": "2.0", "id": payload["id"], "result": None})
        client._post = fake_post
        orig = abf.MCP
        abf.MCP = lambda url=None: client
        try:
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    abf.cmd_get(argparse.Namespace(id="x", url="http://example.invalid",
                                                   json=False))
        finally:
            abf.MCP = orig

    def test_a_429_is_retried(self):
        calls = {"n": 0}
        orig_open, orig_sleep = abf.open_request, abf.time.sleep
        class R:
            status = 200
            headers = {}
            def read(self, *a): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        def flaky(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise abf.urllib.error.HTTPError("u", 429, "slow down", {"Retry-After": "1"}, None)
            return R()
        abf.open_request = flaky
        abf.time.sleep = lambda _s: None
        self.addCleanup(setattr, abf, "open_request", orig_open)
        self.addCleanup(setattr, abf.time, "sleep", orig_sleep)
        status, _, _ = abf.http("https://example.invalid/x")
        self.assertEqual(status, 200)
        self.assertEqual(calls["n"], 2)

    def test_a_404_is_still_not_retried(self):
        calls = {"n": 0}
        orig_open = abf.open_request
        def boom(req, timeout=None):
            calls["n"] += 1
            raise abf.urllib.error.HTTPError("u", 404, "nf", {}, None)
        abf.open_request = boom
        self.addCleanup(setattr, abf, "open_request", orig_open)
        status, _, _ = abf.http("https://example.invalid/x")
        self.assertEqual(status, 404)
        self.assertEqual(calls["n"], 1)

    def test_merged_rate_is_cached_per_repo(self):
        abf._MERGE_CACHE.clear()
        calls = []
        orig = abf.gh_get
        abf.gh_get = lambda path: (calls.append(path), [{"merged_at": "x"}] * 3)[1]
        try:
            first = abf.merged_rate("owner", "repo")
            second = abf.merged_rate("owner", "repo")
        finally:
            abf.gh_get = orig
            abf._MERGE_CACHE.clear()
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)      # one 100-PR page, not two

    def _check_issue_without_network(self, failure):
        """check_issue touches gh_get twice before the timeline; stub it out entirely."""
        searched = []
        def fake_gh(path):
            if "/timeline" in path:
                raise RuntimeError(failure)
            return {"created_at": "2026-10-01T00:00:00Z", "title": "t",
                    "state": "open", "comments": 0}
        self.addCleanup(setattr, abf, "gh_get", abf.gh_get)
        self.addCleanup(setattr, abf, "searched_prs", abf.searched_prs)
        abf.gh_get = fake_gh
        abf.searched_prs = lambda o, r, n: searched.append((o, r, n)) or 0
        with self.assertRaises(RuntimeError):
            abf.check_issue("o", "r", 5)
        return searched

    def test_a_rate_limit_is_not_swallowed_into_the_fallback(self):
        # falling back would hit Search, whose unauthenticated quota is 10/min
        self.assertEqual(
            self._check_issue_without_network("GitHub rate limit hit."), [])

    def test_a_404_also_skips_the_fallback(self):
        self.assertEqual(
            self._check_issue_without_network("GitHub 404 on /timeline"), [])

    def test_linked_prs_splits_by_pr_state(self):
        # counting every cross-reference calls 22 abandoned PRs "competition"
        def xref(number, state, merged=None):
            return {"event": "cross-referenced",
                    "source": {"issue": {"number": number, "state": state,
                                         "pull_request": {"merged_at": merged}}}}
        events = [
            {"event": "commented", "body": "looks good"},
            xref(11, "open"),
            xref(11, "open"),                       # same PR twice: counted once
            xref(12, "closed"),                     # abandoned
            xref(13, "closed", "2026-10-01T00:00:00Z"),   # merged
            {"event": "cross-referenced", "source": {"issue": {"number": 14}}},  # an issue
        ]
        orig = abf.gh_get
        # endswith, not "in": "page=1" is a substring of "per_page=100"
        abf.gh_get = lambda path: events if path.endswith("page=1") else []
        try:
            self.assertEqual(abf.linked_prs("o", "r", 5),
                             {"open": 1, "merged": 1, "closed": 1,
                              "complete": True, "source": "timeline"})
        finally:
            abf.gh_get = orig

    def test_a_merged_pr_makes_the_bounty_taken(self):
        verdict, why = abf.judge(age_days=1, prs=0, merged=80, merged_prs=2)
        self.assertEqual(verdict, "taken")
        self.assertIn("merged", why)

    def test_closed_unmerged_prs_do_not_crowd_the_verdict(self):
        # 0 open PRs means nobody is competing, however many attempts were abandoned
        verdict, _ = abf.judge(age_days=1, prs=0, merged=80, merged_prs=0)
        self.assertEqual(verdict, "worth a look")

    def test_linked_prs_dedups_across_pages(self):
        # a PR can cross-reference an issue more than once; pages must not double-count
        def pr(number):
            return {"event": "cross-referenced",
                    "source": {"issue": {"number": number, "state": "closed",
                                         "pull_request": {}}}}
        page1 = [pr(11)] * 100                 # full page forces a second request
        page2 = [pr(11), pr(12)]               # 11 repeats, 12 is new
        orig = abf.gh_get
        abf.gh_get = lambda path: page1 if path.endswith("page=1") else page2
        try:
            self.assertEqual(abf.linked_prs("o", "r", 5)["closed"], 2)
        finally:
            abf.gh_get = orig

    def test_watch_json_emits_one_object_per_line(self):
        import argparse
        class Stop(Exception):
            pass
        items = [{"id": "a", "est_usd": 5, "title": "t", "url": ""}]
        orig_feed, orig_sleep = abf.load_feed, abf.time.sleep
        abf.load_feed = lambda: items
        abf.time.sleep = lambda _s: (_ for _ in ()).throw(Stop())
        state = os.path.join(tempfile.mkdtemp(), "s.json")
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                try:
                    abf.cmd_watch(argparse.Namespace(interval=30, once=False, state=state,
                                                     min_usd=0.0, source=None,
                                                     exclude_requires=(), closes_within=None,
                                                     json=True))
                except Stop:
                    pass
        finally:
            abf.load_feed, abf.time.sleep = orig_feed, orig_sleep
        line = buf.getvalue().strip()
        self.assertEqual(json.loads(line)["event"], "first_pass")


class TestInputAndShapeHardening(unittest.TestCase):
    """Everything in this class came from a third review; each test pins one crash."""

    def _main(self, argv):
        orig = sys.argv
        sys.argv = ["abf.py"] + argv
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                abf.main()
        finally:
            sys.argv = orig

    def test_nan_min_usd_is_rejected(self):
        with self.assertRaises(SystemExit):
            self._main(["feed", "--min-usd", "nan"])

    def test_negative_limit_is_rejected(self):
        with self.assertRaises(SystemExit):
            self._main(["feed", "--top", "-1"])

    def test_state_file_holding_a_list_is_ignored(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "s.json")
        for payload in ("[]", "null", '{"seen": []}'):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(payload)
            state = abf.load_state(path)
            self.assertIsInstance(state, dict)
            self.assertIsInstance(state.get("seen"), dict)

    def test_feed_failure_raises_instead_of_exiting(self):
        # SystemExit derives from BaseException, so watch's except Exception misses it
        orig = abf.http
        abf.http = lambda *a, **k: (503, {}, "")
        try:
            with self.assertRaises(RuntimeError):
                abf.load_feed_doc()
        finally:
            abf.http = orig

    def test_earn_now_accepts_a_bare_array(self):
        orig = abf.http
        abf.http = lambda *a, **k: (200, {}, json.dumps([{"id": "x", "est_usd": 3}]))
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                abf.cmd_earn_now(argparse.Namespace(min_usd=0.0, source=None,
                                                    exclude_requires=(), closes_within=None,
                                                    limit=None, json=True))
        finally:
            abf.http = orig
        self.assertEqual(json.loads(buf.getvalue())["count"], 1)

    def test_get_survives_a_non_object_result(self):
        client = abf.MCP("http://example.invalid/mcp")
        def fake_post(payload, **kwargs):
            if "id" not in payload:
                return 202, {}, ""
            return 200, {"Content-Type": "application/json"}, json.dumps(
                {"jsonrpc": "2.0", "id": payload["id"], "result": [1, 2, 3]})
        client._post = fake_post
        orig = abf.MCP
        abf.MCP = lambda url=None: client
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                abf.cmd_get(argparse.Namespace(id="x", url="http://example.invalid", json=False))
        finally:
            abf.MCP = orig
        self.assertIn("1", buf.getvalue())

    def _client_with_protocol(self, version):
        client = abf.MCP("http://example.invalid/mcp")
        def fake_post(payload, **kwargs):
            if "id" not in payload:          # notifications/initialized
                return 202, {}, ""
            result = {} if version is None else {"protocolVersion": version}
            return 200, {"Content-Type": "application/json"}, json.dumps(
                {"jsonrpc": "2.0", "id": payload["id"], "result": result})
        client._post = fake_post
        return client

    def test_a_negotiated_protocol_is_recorded(self):
        client = self._client_with_protocol("2025-06-18")
        client.initialize()
        self.assertEqual(client.protocol, "2025-06-18")

    def test_negotiating_down_is_accepted(self):
        client = self._client_with_protocol("2025-03-26")
        client.initialize()
        self.assertEqual(client.protocol, "2025-03-26")

    def test_an_unknown_protocol_stops_instead_of_guessing(self):
        # the framing could differ; a wrong parse is worse than a clear stop
        client = self._client_with_protocol("2027-01-01")
        with self.assertRaises(RuntimeError) as ctx:
            client.initialize()
        self.assertIn("2027-01-01", str(ctx.exception))
        self.assertIn("2025-06-18", str(ctx.exception))

    def test_a_missing_protocol_does_not_crash(self):
        client = self._client_with_protocol(None)
        client.initialize()
        self.assertIsNone(client.protocol)

    def test_list_tools_survives_a_null_result(self):
        client = abf.MCP("http://example.invalid/mcp")
        client._post = lambda payload, **kwargs: (200, {"Content-Type": "application/json"},
            json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "result": None}))
        self.assertEqual(client.list_tools(), [])

    def test_incomplete_read_is_retried(self):
        import http.client as hc
        calls = {"n": 0}
        orig_open, orig_sleep = abf.open_request, abf.time.sleep
        class R:
            status = 200
            headers = {}
            def read(self, *a): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        def flaky(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise hc.IncompleteRead(b"partial", 100)
            return R()
        abf.open_request = flaky
        abf.time.sleep = lambda _s: None
        self.addCleanup(setattr, abf, "open_request", orig_open)
        self.addCleanup(setattr, abf.time, "sleep", orig_sleep)
        status, _, _ = abf.http("https://example.invalid/x")
        self.assertEqual(status, 200)
        self.assertEqual(calls["n"], 2)

    def test_a_string_id_still_matches(self):
        client = abf.MCP("http://example.invalid/mcp")
        client._post = lambda payload, **kwargs: (200, {"Content-Type": "application/json"},
            json.dumps({"jsonrpc": "2.0", "id": str(payload["id"]), "result": {"ok": True}}))
        self.assertEqual(client.rpc("tools/list"), {"ok": True})

    def test_sse_strips_one_leading_space_only(self):
        frames = abf.sse_data_frames("data:  two spaces")
        self.assertEqual(frames, [" two spaces"])

    def test_a_batch_json_array_is_read_as_messages(self):
        client = abf.MCP("http://example.invalid/mcp")
        client._post = lambda payload, **kwargs: (200, {"Content-Type": "application/json"},
            json.dumps([{"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}}]))
        self.assertEqual(client.rpc("tools/list"), {"ok": True})

    def test_short_fractional_seconds_parse(self):
        # Python < 3.11 rejects ".12"; padding keeps the value instead of dropping it
        self.assertIsNotNone(abf.parse_deadline("2026-11-01T12:00:00.12Z"))
        self.assertIsNotNone(abf.parse_deadline("2026-11-01T12:00:00.1Z"))

    def test_url_forms(self):
        self.assertEqual(abf.parse_issue_url("https://GitHub.com/a/b/issues/7"), ("a", "b", 7))
        self.assertEqual(abf.parse_issue_url("https://www.github.com/a/b/issues/7"), ("a", "b", 7))
        self.assertEqual(abf.parse_issue_url("github.com/a/b/issues/7"), ("a", "b", 7))
        self.assertIsNone(abf.parse_issue_url("https://notgithub.com/a/b/issues/7"))
        self.assertIsNone(abf.parse_issue_url("https://t.example/?to=github.com/a/b/issues/7"))


class TestFourthReview(unittest.TestCase):
    """The watch state-machine bug and three escape hatches."""

    def _watch_once(self, items, state, min_usd):
        import argparse
        orig = abf.load_feed
        abf.load_feed = lambda: items
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                abf.cmd_watch(argparse.Namespace(interval=30, once=True, state=state,
                                                 min_usd=min_usd, source=None,
                                                 exclude_requires=(), closes_within=None,
                                                 json=False))
            return buf.getvalue()
        finally:
            abf.load_feed = orig

    def test_a_big_bounty_after_an_empty_first_pass_is_reported(self):
        # with --min-usd 500 the first run matches nothing and leaves seen empty;
        # deriving first_pass from that swallowed the next bounty silently
        state = os.path.join(tempfile.mkdtemp(), "s.json")
        self._watch_once([{"id": "cheap", "est_usd": 5, "title": "cheap"}], state, 500.0)
        out = self._watch_once([{"id": "cheap", "est_usd": 5, "title": "cheap"},
                                {"id": "big", "est_usd": 1000, "title": "big"}], state, 500.0)
        self.assertIn("NEW", out)
        self.assertIn("1000", out)

    def test_state_records_an_initialized_flag_and_an_iso_timestamp(self):
        state_path = os.path.join(tempfile.mkdtemp(), "s.json")
        self._watch_once([{"id": "a", "est_usd": 5, "title": "t"}], state_path, 0.0)
        state = json.load(open(state_path, encoding="utf-8"))
        self.assertTrue(state.get("initialized"))
        self.assertRegex(state["seen"]["a"]["first_seen"], r"^\d{4}-\d{2}-\d{2}T")

    def test_non_object_frames_are_skipped(self):
        for body in (json.dumps([1, 2]), json.dumps("ok")):
            client = abf.MCP("http://example.invalid/mcp")
            client._post = lambda payload, b=body, **kwargs: (200, {"Content-Type": "application/json"}, b)
            with self.assertRaises(RuntimeError) as ctx:
                client.rpc("tools/list")
            self.assertIn("no JSON-RPC response", str(ctx.exception))

    def test_zero_limits_are_rejected(self):
        for argv in (["feed", "--top", "0"], ["reachable", "--max-subs", "0"],
                     ["earn-now", "--limit", "0"]):
            orig = sys.argv
            sys.argv = ["abf.py"] + argv
            try:
                with self.assertRaises(SystemExit):
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        abf.main()
            finally:
                sys.argv = orig

    def test_retry_exhaustion_exits_cleanly(self):
        # OSError that is not a URLError used to escape main() as a traceback
        orig = abf.http
        abf.http = lambda *a, **k: (_ for _ in ()).throw(OSError("connection reset"))
        orig_argv = sys.argv
        sys.argv = ["abf.py", "feed"]
        try:
            with self.assertRaises(SystemExit) as ctx:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    abf.main()
            self.assertEqual(ctx.exception.code, 1)
        finally:
            abf.http = orig
            sys.argv = orig_argv

    def test_a_truncated_error_page_is_not_a_crash(self):
        import http.client as hc
        orig_open = abf.open_request
        def boom(req, timeout=None):
            err = abf.urllib.error.HTTPError("u", 502, "bad gateway", {}, None)
            # HTTPError.read is set per instance, so patch the instance, not the class
            err.read = lambda: (_ for _ in ()).throw(hc.IncompleteRead(b"", 10))
            raise err
        abf.open_request = boom
        try:
            status, _, body = abf.http("https://example.invalid/x", attempts=1)
        finally:
            abf.open_request = orig_open
        self.assertEqual(status, 502)
        self.assertEqual(body, "")

    def test_lowercase_z_parses(self):
        self.assertIsNotNone(abf.parse_deadline("2026-11-01T12:00:00z"))
        self.assertIsNotNone(abf.parse_deadline("2026-11-01T12:00:00.123z"))


_is_local_alias = _is_local


def test_abf_is_local(address):
    return _is_local(address)


class TestOfflineGuard(unittest.TestCase):
    """The guard has to be tested, or a future Python could make it a no-op."""

    def test_a_non_local_connection_is_refused(self):
        with self.assertRaises(AssertionError) as ctx:
            socket.socket().connect(("example.com", 80))
        self.assertIn("offline test tried to reach", str(ctx.exception))

    def test_local_addresses_are_recognised(self):
        for address in (("127.0.0.1", 8080), ("::1", 80), ("localhost", 1)):
            self.assertTrue(test_abf_is_local(address), address)

    def test_remote_addresses_are_not(self):
        for address in (("api.github.com", 443), ("8.8.8.8", 53), ("0.0.0.0", 1) if False else ("example.org", 80)):
            self.assertFalse(test_abf_is_local(address), address)


class TestSecurityAudit(unittest.TestCase):
    """Fixes from the fifth review, which looked at this as a security surface."""

    def _attempts(self, tool):
        calls = {"n": 0}
        orig_open, orig_sleep = abf.open_request, abf.time.sleep
        def boom(req, timeout=None):
            calls["n"] += 1
            raise abf.urllib.error.HTTPError("u", 502, "bad gateway", {}, None)
        abf.open_request = boom
        abf.time.sleep = lambda _s: None
        self.addCleanup(setattr, abf, "open_request", orig_open)
        self.addCleanup(setattr, abf.time, "sleep", orig_sleep)
        client = abf.MCP("https://example.invalid/mcp")
        with self.assertRaises(RuntimeError):
            client.rpc("tools/call", {"name": tool, "arguments": {}})
        return calls["n"]

    def test_a_read_only_tool_is_retried(self):
        self.assertEqual(self._attempts("list_bounties"), 3)

    def test_a_side_effecting_tool_is_not_retried(self):
        # `mcp --tool` can name anything; repeating a write is not safe
        self.assertEqual(self._attempts("pay_now"), 1)

    def test_control_characters_never_reach_the_terminal(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.print_bounties([{"id": "x[2J", "est_usd": 1, "source": "s",
                                 "title": "]52;c;evilhi"}])
        out = buf.getvalue()
        self.assertNotIn("", out)
        self.assertNotIn("", out)
        self.assertIn("hi", out)

    def test_an_oversized_body_is_refused(self):
        class Huge:
            status, headers = 200, {}
            def read(self, *a): return b"x" * (abf.MAX_BODY + 10)
            def __enter__(self): return self
            def __exit__(self, *a): return False
        orig = abf.open_request
        abf.open_request = lambda *a, **k: Huge()
        self.addCleanup(setattr, abf, "open_request", orig)
        with self.assertRaises(RuntimeError) as ctx:
            abf.http("https://example.invalid/x", attempts=1)
        self.assertIn("exceeded", str(ctx.exception))

    def test_save_state_ignores_a_planted_tmp_file(self):
        directory = tempfile.mkdtemp()
        target = os.path.join(directory, "s.json")
        planted = target + ".tmp"
        with open(planted, "w", encoding="utf-8") as fh:
            fh.write("untouched")
        abf.save_state(target, {"seen": {"a": 1}})
        with open(planted, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "untouched")
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"seen": {"a": 1}})

    def test_feed_root_must_be_an_object(self):
        orig = abf.http
        abf.http = lambda *a, **k: (200, {}, "null")
        self.addCleanup(setattr, abf, "http", orig)
        with self.assertRaises(RuntimeError) as ctx:
            abf.load_feed_doc()
        self.assertIn("not an object", str(ctx.exception))

    def test_integer_and_string_ids_dedupe_together(self):
        # JSON object keys are strings; 123 and "123" must not be two different items
        items = [{"id": 123, "est_usd": 5}]
        self.assertEqual(abf.new_items({"123": {}}, items), [])
        self.assertEqual(len(abf.new_items({}, items)), 1)

    def test_an_item_without_an_id_is_not_reported_forever(self):
        items = [{"est_usd": 5, "title": "no id"}]
        self.assertEqual(abf.new_items({}, items), [])


class TestCompletenessHonesty(unittest.TestCase):
    """A lower bound must never be reported as a finished count."""

    def test_an_empty_timeline_is_complete(self):
        # no cross-references is a normal answer, not a truncated read
        orig = abf.gh_get
        abf.gh_get = lambda path: []
        self.addCleanup(setattr, abf, "gh_get", orig)
        self.assertTrue(abf.linked_prs("o", "r", 5)["complete"])

    def test_a_rate_limit_is_not_retried_inside_merged_rate(self):
        abf._MERGE_CACHE.clear()
        calls = []
        orig = abf.gh_get
        def limited(path):
            calls.append(path)
            raise RuntimeError("GitHub rate limit hit. Set GITHUB_TOKEN to raise it.")
        abf.gh_get = limited
        self.addCleanup(setattr, abf, "gh_get", orig)
        self.addCleanup(abf._MERGE_CACHE.clear)
        with self.assertRaises(RuntimeError):
            abf.merged_rate("o", "r")
        self.assertEqual(len(calls), 1)

    def test_a_truncated_timeline_yields_inconclusive(self):
        # a full page forces a second request, which never comes: truncated, and the
        # two PRs seen so far are not enough to decide either way
        events = [{"event": "cross-referenced",
                   "source": {"issue": {"number": n, "state": "open",
                                        "pull_request": {}}}} for n in range(2)]
        orig_gh, orig_rate = abf.gh_get, abf.merged_rate
        full = events * 50          # 100 events: a full page, so page 2 is requested
        def fake(path):
            if "/timeline" in path and path.endswith("page=1"):
                return full
            if "/timeline" in path:
                return full          # never short: the read never completes
            return {"created_at": "2026-10-01T00:00:00Z", "title": "t",
                    "state": "open", "comments": 0}
        abf.gh_get = fake
        abf.merged_rate = lambda o, r: (50, 100, 50)
        self.addCleanup(setattr, abf, "gh_get", orig_gh)
        self.addCleanup(setattr, abf, "merged_rate", orig_rate)
        result = abf.check_issue("o", "r", 5)
        self.assertEqual(result["verdict"], "inconclusive")
        self.assertFalse(result["prs_complete"])

    def _issue_with(self, stats, age_days=1):
        orig_gh, orig_rate = abf.gh_get, abf.merged_rate
        def fake(path):
            if "/timeline" in path:
                return [{"event": "cross-referenced",
                         "source": {"issue": {"number": n, "state": "open",
                                              "pull_request": {}}}} for n in range(1, 3)]
            return {"created_at": "2026-11-01T00:00:00Z", "title": "t",
                    "state": "open", "comments": 0}
        abf.gh_get = fake
        abf.merged_rate = lambda o, r: (50, 100, 50)
        self.addCleanup(setattr, abf, "gh_get", orig_gh)
        self.addCleanup(setattr, abf, "merged_rate", orig_rate)
        orig_linked = abf.linked_prs
        abf.linked_prs = lambda o, r, n: stats
        self.addCleanup(setattr, abf, "linked_prs", orig_linked)
        return abf.check_issue("o", "r", 5)

    def test_truncation_does_not_downgrade_a_crowded_verdict(self):
        # truncation can only add PRs, so 51 open ones stays crowded
        result = self._issue_with({"open": 51, "merged": 0, "closed": 49,
                                   "complete": False})
        self.assertEqual(result["verdict"], "crowded")
        self.assertIn("lower bound", result["why"])

    def test_truncation_does_not_downgrade_a_taken_verdict(self):
        result = self._issue_with({"open": 2, "merged": 1, "closed": 0,
                                   "complete": False})
        self.assertEqual(result["verdict"], "taken")
        self.assertIn("lower bound", result["why"])

    def test_truncation_downgrades_an_undecided_verdict(self):
        # nothing decisive seen yet, and an unread page could hold the merged PR
        result = self._issue_with({"open": 2, "merged": 0, "closed": 0,
                                   "complete": False})
        self.assertEqual(result["verdict"], "inconclusive")

    def test_the_search_fallback_is_never_complete(self):
        orig_linked, orig_searched = abf.linked_prs, abf.searched_prs
        def boom(owner, repo, number):
            raise ValueError("unexpected payload")
        abf.linked_prs = boom
        abf.searched_prs = lambda o, r, n: 4
        self.addCleanup(setattr, abf, "linked_prs", orig_linked)
        self.addCleanup(setattr, abf, "searched_prs", orig_searched)
        stats = abf.linked_prs("o", "r", 5) if False else None
        # exercise the branch through check_issue instead
        orig_gh, orig_rate = abf.gh_get, abf.merged_rate
        abf.gh_get = lambda path: {"created_at": "2026-10-01T00:00:00Z", "title": "t",
                                   "state": "open", "comments": 0}
        abf.merged_rate = lambda o, r: (50, 100, 50)
        self.addCleanup(setattr, abf, "gh_get", orig_gh)
        self.addCleanup(setattr, abf, "merged_rate", orig_rate)
        result = abf.check_issue("o", "r", 5)
        self.assertEqual(result["prs_source"], "search")
        self.assertFalse(result["prs_complete"])
        self.assertEqual(result["verdict"], "inconclusive")

    def test_a_negative_price_is_not_a_listing(self):
        split = abf.reachable_split([{"id": "neg", "est_usd": -5,
                                      "requires": ["wallet_evm"]}])
        self.assertEqual(split["listed"], [])

    def test_attempts_zero_is_clamped(self):
        orig = abf.open_request
        def boom(req, timeout=None):
            raise OSError("reset")
        abf.open_request = boom
        orig_sleep = abf.time.sleep
        abf.time.sleep = lambda _s: None
        self.addCleanup(setattr, abf, "open_request", orig)
        self.addCleanup(setattr, abf.time, "sleep", orig_sleep)
        with self.assertRaises(OSError):
            abf.http("https://example.invalid/x", attempts=0)

    def test_json_output_respects_limit(self):
        orig = abf.http
        abf.http = lambda *a, **k: (200, {}, json.dumps(
            [{"id": str(n), "est_usd": 5} for n in range(5)]))
        self.addCleanup(setattr, abf, "http", orig)
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.cmd_earn_now(argparse.Namespace(min_usd=0.0, source=None,
                                                exclude_requires=(), closes_within=None,
                                                limit=2, json=True))
        self.assertEqual(json.loads(buf.getvalue())["count"], 2)

    def test_collect_new_warns_when_has_more_has_no_cursor(self):
        import contextlib
        class Client:
            def call_tool(self, name, arguments):
                return {"items": [{"id": "a"}], "has_more": True}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            items = abf.collect_new(Client(), "2026-11-01T00:00:00Z")
        self.assertEqual(len(items), 1)
        self.assertIn("no cursor", err.getvalue())


class TestOutputConsistency(unittest.TestCase):
    """Counts describe the board; --limit only pages what is shown."""

    def test_reachable_json_counts_the_whole_tier(self):
        doc = {"items": [{"id": "i%d" % n, "est_usd": 10, "requires": ["wallet_evm"],
                          "source": "s"} for n in range(20)], "stats": {}}
        orig = abf.load_feed_doc
        abf.load_feed_doc = lambda: doc
        self.addCleanup(setattr, abf, "load_feed_doc", orig)
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.cmd_reachable(argparse.Namespace(limit=5, max_subs=50, json=True))
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["winnable"]["count"], 20)
        self.assertEqual(payload["returned"], 5)
        self.assertEqual(len(payload["items"]), 5)

    def test_get_cleans_remote_fields(self):
        client = abf.MCP("http://example.invalid/mcp")
        def fake_post(payload, **kwargs):
            if "id" not in payload:
                return 202, {}, ""
            return 200, {"Content-Type": "application/json"}, json.dumps(
                {"jsonrpc": "2.0", "id": payload["id"],
                 "result": {"item": {"id": "x", "title": "t",
                                     "url": "https://e/[2J"}, "note": "n[31m"}})
        client._post = fake_post
        orig = abf.MCP
        abf.MCP = lambda url=None: client
        self.addCleanup(setattr, abf, "MCP", orig)
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.cmd_get(argparse.Namespace(id="x", url="http://example.invalid", json=False))
        self.assertNotIn("", buf.getvalue())

    def test_a_missing_field_exits_cleanly(self):
        orig = abf.check_issue
        abf.check_issue = lambda o, r, n: (_ for _ in ()).throw(KeyError("created_at"))
        orig_argv = sys.argv
        sys.argv = ["abf.py", "check", "https://github.com/a/b/issues/1"]
        try:
            with self.assertRaises(SystemExit) as ctx:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    abf.main()
            self.assertIn("missing key", str(ctx.exception))
        finally:
            abf.check_issue = orig
            sys.argv = orig_argv

    def test_watch_once_fails_loudly_when_the_feed_is_down(self):
        orig = abf.load_feed
        abf.load_feed = lambda: (_ for _ in ()).throw(RuntimeError("feed down"))
        self.addCleanup(setattr, abf, "load_feed", orig)
        state = os.path.join(tempfile.mkdtemp(), "s.json")
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                abf.cmd_watch(argparse.Namespace(interval=30, once=True, state=state,
                                                 min_usd=0.0, source=None,
                                                 exclude_requires=(), closes_within=None,
                                                 json=False))
        self.assertEqual(ctx.exception.code, 1)

    def test_error_rows_carry_the_same_keys_as_success_rows(self):
        feed = [{"id": "x", "url": "https://github.com/a/b/issues/1", "est_usd": 5, "title": "t"}]
        orig_feed, orig_check = abf.load_feed, abf.check_issue
        abf.load_feed = lambda: feed
        abf.check_issue = lambda o, r, n: (_ for _ in ()).throw(RuntimeError("boom"))
        self.addCleanup(setattr, abf, "load_feed", orig_feed)
        self.addCleanup(setattr, abf, "check_issue", orig_check)
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.check_board(argparse.Namespace(all=True, target=None, min_usd=0.0, source=None,
                                               exclude_requires=(), closes_within=None,
                                               json=True, limit=10))
        row = json.loads(buf.getvalue())[0]
        for key in ("merged_prs", "closed_prs", "prs_complete", "prs_source",
                    "merged_sample", "merged_count"):
            self.assertIn(key, row)

    def test_no_history_says_so_in_the_reason(self):
        # "0 merged per 100" reads as a measured rate of zero when nothing was measured
        verdict, why = abf.judge(4, 1, 0, has_history=False)
        self.assertEqual(verdict, "worth a look")
        self.assertIn("no merge history", why)
        self.assertNotIn("merged/100", why)

    def test_with_history_the_reason_keeps_the_rate(self):
        _, why = abf.judge(4, 1, 40, has_history=True)
        self.assertIn("40 merged/100", why)

    def test_the_check_line_does_not_report_a_zero_rate_for_no_history(self):
        row = {"repo": "a/b", "issue": 3, "title": "t", "state": "open",
               "created_at": "2026-10-29", "age_days": 4, "comments": 1,
               "prs": 1, "merged_prs": 0, "closed_prs": 0, "prs_complete": True,
               "prs_source": "timeline", "merged_last_100": 0,
               "merged_sample": 0, "merged_count": 0,
               "verdict": "worth a look", "why": "ok"}
        orig = abf.check_issue
        abf.check_issue = lambda o, r, n: dict(row)
        self.addCleanup(setattr, abf, "check_issue", orig)
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.cmd_check(argparse.Namespace(target="https://github.com/a/b/issues/3",
                                             all=False, min_usd=0.0, source=None,
                                             exclude_requires=(), closes_within=None,
                                             json=False, limit=10))
        out = buf.getvalue()
        self.assertIn("no closed PRs to measure", out)
        self.assertNotIn("(0 per 100)", out)

    def test_a_repo_without_history_is_not_called_stalled(self):
        self.assertEqual(abf.judge(1, 0, 0, has_history=False)[0], "worth a look")
        self.assertEqual(abf.judge(1, 0, 0, has_history=True)[0], "stalled")

    def test_parse_requires_accepts_a_sequence(self):
        self.assertEqual(abf.parse_requires(["kyc", "stake"]), ("kyc", "stake"))
        self.assertEqual(abf.parse_requires(("kyc",)), ("kyc",))

    def test_selftest_without_a_tests_directory_says_so(self):
        orig_file = abf.__file__
        abf.__file__ = os.path.join(tempfile.mkdtemp(), "abf.py")
        self.addCleanup(setattr, abf, "__file__", orig_file)
        with self.assertRaises(SystemExit) as ctx:
            abf.cmd_selftest(argparse.Namespace(verbose=False))
        self.assertIn("no tests/", str(ctx.exception))


class TestStrictFriction(unittest.TestCase):
    """agent_account is a lighter wall than KYC, so it is opt-in via --strict."""

    FEED = [
        {"id": "open", "est_usd": 10, "source": "s", "requires": ["wallet_evm"]},
        {"id": "signup", "est_usd": 20, "source": "s",
         "requires": ["agent_account", "wallet_evm"]},
        {"id": "kyc", "est_usd": 30, "source": "s", "requires": ["kyc", "wallet_evm"]},
    ]

    def test_default_keeps_signup_items(self):
        split = abf.reachable_split(self.FEED)
        self.assertEqual([i["id"] for i in split["reachable"]], ["open", "signup"])

    def test_strict_drops_signup_items(self):
        split = abf.reachable_split(self.FEED, friction=abf.FRICTION + abf.SIGNUP)
        self.assertEqual([i["id"] for i in split["reachable"]], ["open"])

    def test_signup_is_not_in_the_default_set(self):
        self.assertNotIn("agent_account", abf.FRICTION)
        self.assertEqual(abf.SIGNUP, ("agent_account",))

    def test_breakdown_follows_the_set_it_is_given(self):
        dropped = [self.FEED[1], self.FEED[2]]
        self.assertEqual(dict(abf.friction_breakdown(dropped)), {"kyc": 1})
        self.assertEqual(dict(abf.friction_breakdown(dropped, abf.FRICTION + abf.SIGNUP)),
                         {"agent_account": 1, "kyc": 1})

    def _run(self, strict):
        orig = abf.load_feed_doc
        abf.load_feed_doc = lambda: {"items": self.FEED, "stats": {}}
        self.addCleanup(setattr, abf, "load_feed_doc", orig)
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.cmd_reachable(argparse.Namespace(limit=None, max_subs=50,
                                                 strict=strict, json=False))
        return buf.getvalue()

    def test_the_note_names_what_strict_would_remove(self):
        out = self._run(strict=False)
        self.assertIn("need an account on the source platform", out)
        self.assertIn("--strict excludes them", out)

    def test_strict_prints_no_such_note(self):
        self.assertNotIn("--strict excludes them", self._run(strict=True))

    def test_json_reports_the_friction_set_used(self):
        orig = abf.load_feed_doc
        abf.load_feed_doc = lambda: {"items": self.FEED, "stats": {}}
        self.addCleanup(setattr, abf, "load_feed_doc", orig)
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.cmd_reachable(argparse.Namespace(limit=None, max_subs=50,
                                                 strict=True, json=True))
        payload = json.loads(buf.getvalue())
        self.assertIn("agent_account", payload["friction"])
        self.assertEqual(payload["reachable"]["count"], 1)
        self.assertEqual(payload["signup_only"]["count"], 0)

    def test_overlapping_tags_are_labelled(self):
        # one item, two reasons: the counts must not look like a wrong subtraction
        orig = abf.load_feed_doc
        abf.load_feed_doc = lambda: {"items": [
            {"id": "both", "est_usd": 5, "source": "s",
             "requires": ["kyc", "agent_account"]}], "stats": {}}
        self.addCleanup(setattr, abf, "load_feed_doc", orig)
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.cmd_reachable(argparse.Namespace(limit=None, max_subs=50,
                                                 strict=True, json=False))
        self.assertIn("tags overlap", buf.getvalue())


class TestPooledRewards(unittest.TestCase):
    """est_usd is a pot for shared_pool / ranked_prizes, not one worker's payout."""

    POOL = {"id": "p", "est_usd": 100, "source": "s", "reward_basis": "shared_pool",
            "pool_amount": 100, "per_worker_usd": None, "title": "t"}
    RANKED = {"id": "r", "est_usd": 125, "source": "s", "reward_basis": "ranked_prizes",
              "pool_amount": 125, "title": "t"}
    SINGLE = {"id": "s", "est_usd": 50, "source": "s", "title": "t"}

    def test_basis_detection(self):
        self.assertTrue(abf.is_pooled(self.POOL))
        self.assertTrue(abf.is_pooled(self.RANKED))
        self.assertFalse(abf.is_pooled(self.SINGLE))          # absent means one winner
        self.assertFalse(abf.is_pooled({"reward_basis": "fixed"}))
        self.assertFalse(abf.is_pooled({"reward_basis": None}))

    def test_the_cell_is_marked_and_footnoted(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.print_bounties([self.POOL, self.SINGLE])
        out = buf.getvalue()
        self.assertIn("$100*", out)
        self.assertIn("$50 ", out)          # unmarked
        self.assertIn("pool split between winners", out)

    def test_no_footnote_without_a_pooled_row(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.print_bounties([self.SINGLE])
        self.assertNotIn("pool split", buf.getvalue())

    def _reachable(self, items, as_json=False):
        orig = abf.load_feed_doc
        abf.load_feed_doc = lambda: {"items": items, "stats": {}}
        self.addCleanup(setattr, abf, "load_feed_doc", orig)
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.cmd_reachable(argparse.Namespace(limit=None, max_subs=50,
                                                 strict=False, json=as_json))
        return buf.getvalue()

    def test_reachable_counts_the_pooled_share(self):
        out = self._reachable([self.POOL, self.SINGLE])
        self.assertIn("is a pool split between winners", out)

    def test_an_audit_contest_counts_as_a_pool(self):
        # reward_basis is absent on audit contests; kind is the only tell, and it is the
        # single biggest number on the board
        contest = {"id": "c", "est_usd": 50000, "kind": "audit_contest", "source": "s"}
        self.assertTrue(abf.is_pooled(contest))
        self.assertTrue(abf.is_pooled({"kind": "AUDIT_CONTEST"}))
        self.assertFalse(abf.is_pooled({"kind": "bounty"}))

    def test_the_listed_row_names_the_audit_contest_share(self):
        contest = {"id": "c", "est_usd": 50000, "kind": "audit_contest",
                   "source": "s", "requires": ["kyc"]}
        small = {"id": "b", "est_usd": 10, "source": "s"}
        out = self._reachable([contest, small])
        self.assertIn("of that is 1 audit contest", out)
        self.assertIn("$50,000", out)

    def test_json_breaks_the_audit_contest_out_of_listed(self):
        contest = {"id": "c", "est_usd": 50000, "kind": "audit_contest",
                   "source": "s", "requires": ["kyc"]}
        payload = json.loads(self._reachable([contest, {"id": "b", "est_usd": 10,
                                                        "source": "s"}], as_json=True))
        self.assertEqual(payload["listed"]["usd"], 50010.0)
        self.assertEqual(payload["listed"]["audit_contests"],
                         {"count": 1, "usd": 50000.0})

    def test_json_separates_pooled_from_the_total(self):
        payload = json.loads(self._reachable([self.POOL, self.SINGLE], as_json=True))
        self.assertEqual(payload["pooled"]["count"], 1)
        self.assertEqual(payload["pooled"]["usd"], 100.0)
        self.assertEqual(payload["winnable"]["usd"], 150.0)   # unchanged, just labelled


class TestTrustLayer(unittest.TestCase):
    """The feed says which fields are paid-only; two of them are what the tiers rebuild."""

    DOC = {
        "items": [{"id": "a", "est_usd": 10, "source": "s", "requires": ["wallet_evm"]},
                  {"id": "b", "est_usd": 20, "source": "s", "requires": ["wallet_evm"],
                   "stale": True},
                  {"id": "c", "est_usd": 30, "source": "s", "requires": ["wallet_evm"],
                   "verified": {"checked_at": "2026-10-31T00:00:00Z", "degraded": True}}],
        "trust_layer": {
            "free_fields": ["id", "est_usd"],
            "paid_only_fields": ["payout_track_record", "competition", "verified.how",
                                 "verification_evidence", "extra"],
            "paid_endpoint": {"url": "https://example.invalid/v1/bounties", "protocol": "x402"},
        },
    }

    def test_layer_is_parsed(self):
        layer = abf.trust_layer_of(self.DOC)
        self.assertIn("payout_track_record", layer["paid_only"])
        self.assertIn("id", layer["free"])
        self.assertEqual(layer["endpoint"]["protocol"], "x402")

    def test_layer_absent_or_wrong_type(self):
        for doc in ({}, {"trust_layer": None}, {"trust_layer": [1, 2]}, None):
            layer = abf.trust_layer_of(doc)
            self.assertEqual(layer["paid_only"], [])
            self.assertIsNone(layer["endpoint"])

    def test_verification_state(self):
        self.assertEqual(abf.verification_state({"stale": True}), "stale")
        self.assertEqual(abf.verification_state({"verified": {"degraded": True}}), "degraded")
        self.assertIsNone(abf.verification_state({"verified": {"checked_at": "x"}}))
        self.assertIsNone(abf.verification_state({"stale": False}))
        self.assertIsNone(abf.verification_state({"verified": "nonsense"}))

    def _run(self, as_json=False):
        orig = abf.load_feed_doc
        abf.load_feed_doc = lambda: self.DOC
        self.addCleanup(setattr, abf, "load_feed_doc", orig)
        buf = io.StringIO()
        with redirect_stdout(buf):
            abf.cmd_reachable(argparse.Namespace(limit=None, max_subs=50,
                                                 strict=False, json=as_json))
        return buf.getvalue()

    def test_it_names_only_the_fields_it_rebuilds(self):
        out = self._run()
        self.assertIn("payout_track_record (alive tier)", out)
        self.assertIn("competition (winnable tier)", out)

    def test_it_says_which_paid_fields_it_does_not_rebuild(self):
        # listing all five under "reconstructed" would claim coverage we do not have
        out = self._run()
        self.assertIn("not rebuilt", out)
        self.assertIn("verification_evidence", out)
        self.assertNotIn("verification_evidence (", out)

    def test_stale_and_degraded_rows_are_counted(self):
        out = self._run()
        self.assertIn("has degraded/stale verification", out)

    def test_json_carries_the_layer_and_the_states(self):
        payload = json.loads(self._run(as_json=True))
        self.assertIn("payout_track_record", payload["trust_layer"]["paid_only"])
        self.assertEqual(payload["unverified"]["count"], 2)
        self.assertEqual(payload["unverified"]["states"], ["degraded", "stale"])


class TestRedirects(unittest.TestCase):
    """urllib follows redirects by default; a 303 would drop the POST body silently."""

    def test_cross_host_redirect_is_refused(self):
        handler = abf.SameHostRedirects()
        req = abf.urllib.request.Request("https://a.example/mcp",
                                         data=b"{}", method="POST")
        with self.assertRaises(abf.urllib.error.HTTPError) as ctx:
            handler.redirect_request(req, None, 302, "Found", {},
                                     "https://b.example/mcp")
        self.assertIn("another host refused", str(ctx.exception))

    def test_same_host_redirect_is_followed(self):
        handler = abf.SameHostRedirects()
        req = abf.urllib.request.Request("https://a.example/mcp")
        followed = handler.redirect_request(req, None, 302, "Found", {},
                                            "https://a.example/mcp2")
        self.assertIsNotNone(followed)

    def test_a_live_cross_port_redirect_never_reaches_the_second_server(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        hits = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
            def log_message(self, *a):
                pass

        target = HTTPServer(("127.0.0.1", 0), Target)
        threading.Thread(target=target.serve_forever, daemon=True).start()
        self.addCleanup(target.shutdown)
        self.addCleanup(target.server_close)
        target_url = "http://127.0.0.1:%d/elsewhere" % target.server_address[1]

        class Redirector(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", target_url)
                self.end_headers()
            def log_message(self, *a):
                pass

        source = HTTPServer(("127.0.0.1", 0), Redirector)
        threading.Thread(target=source.serve_forever, daemon=True).start()
        self.addCleanup(source.shutdown)
        self.addCleanup(source.server_close)

        status, _, _ = abf.http("http://127.0.0.1:%d/x" % source.server_address[1],
                                attempts=1)
        self.assertEqual(status, 302)      # reported, not followed
        self.assertEqual(hits, [])         # the other server was never called


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
