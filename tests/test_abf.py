"""Offline tests for abf.py.

Every test runs against tests/mock_mcp.py on 127.0.0.1 — no network, no fixtures
to download. `python3 abf.py selftest` runs this file.

    python3 -m unittest discover -s tests -v
"""

import datetime
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
        def read(self):
            return b'{"ok": true}'
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    def _patch(self, fail_times):
        calls = {"n": 0}
        orig_open, orig_sleep = abf.urllib.request.urlopen, abf.time.sleep
        def flaky(req, timeout=None):
            calls["n"] += 1
            if calls["n"] <= fail_times:
                raise OSError("connection reset")
            return self._Resp()
        abf.urllib.request.urlopen = flaky
        abf.time.sleep = lambda _s: None
        self.addCleanup(setattr, abf.urllib.request, "urlopen", orig_open)
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
        orig = abf.urllib.request.urlopen
        def boom(req, timeout=None):
            calls["n"] += 1
            raise abf.urllib.error.HTTPError("u", 404, "nf", {}, None)
        abf.urllib.request.urlopen = boom
        self.addCleanup(setattr, abf.urllib.request, "urlopen", orig)
        status, _, _ = abf.http("https://example.invalid/x")
        self.assertEqual(status, 404)
        self.assertEqual(calls["n"], 1)


class TestRpcParsing(unittest.TestCase):
    """A non-JSON frame or a null content block must not take the client down."""

    def _client(self, body, ctype="text/event-stream"):
        client = abf.MCP("http://example.invalid/mcp")
        def fake_post(payload):
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
        self.assertEqual(client.calls[1], {"next_since": "S2", "next_after_id": "A2"})

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
