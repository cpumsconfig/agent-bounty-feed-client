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

    def test_rejects_non_numeric(self):
        self.assertIsNone(abf.parse_issue_url("https://github.com/a/b/issues/abc"))

    def test_rejects_plain_text(self):
        self.assertIsNone(abf.parse_issue_url("frantic:129"))

    def test_judge_crowded(self):
        verdict, _ = abf.judge(age_days=3, prs=98, merged=90)
        self.assertEqual(verdict, "crowded")

    def test_judge_stalled(self):
        verdict, _ = abf.judge(age_days=3, prs=2, merged=0)
        self.assertEqual(verdict, "stalled")

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
