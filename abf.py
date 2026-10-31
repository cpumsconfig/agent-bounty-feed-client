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
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
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


FRICTION = ("kyc", "stake", "bond", "purchase", "human_only")


def parse_requires(value):
    """'kyc, stake' -> ('kyc', 'stake'). None or '' -> ()."""
    if not value:
        return ()
    return tuple(p.strip().lower() for p in value.split(",") if p.strip())


def filter_items(items, min_usd=0.0, source=None, exclude_requires=()):
    """Keep items whose est_usd is known and >= min_usd, preserving feed order.

    exclude_requires drops any item whose requires[] intersects the list — the same
    filter the MCP server applies server-side, done here for the endpoints that take
    no parameters (/earn-now and feed.json are fixed snapshots).
    """
    blocked = set(exclude_requires or ())
    kept = []
    for it in items:
        try:
            amount = float(it.get("est_usd") or 0)
        except (TypeError, ValueError):
            continue
        if amount < min_usd:
            continue
        if source and it.get("source") != source:
            continue
        if blocked and blocked & set(it.get("requires") or ()):
            continue
        kept.append(it)
    return kept


def emit(payload, as_json):
    """One output shape for machines, one for humans."""
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return True
    return False


# ---------------------------------------------------------------- competition

GH_API = "https://api.github.com"


def parse_issue_url(text):
    """Pull (owner, repo, number) out of a GitHub issue URL, or return None."""
    marker = "github.com/"
    if marker not in text:
        return None
    rest = text.split(marker, 1)[1].strip().rstrip("/")
    parts = rest.split("/")
    if len(parts) < 4 or parts[2] != "issues":
        return None
    owner, repo, number = parts[0], parts[1], parts[3].split("?")[0]
    if not number.isdigit():
        return None
    return owner, repo, int(number)


def judge(age_days, prs, merged):
    """Turn three numbers into a verdict. Pure, so it is cheap to test."""
    if prs > 10:
        return "crowded", "%d PRs already compete" % prs
    if merged < 5:
        return "stalled", "repo merged only %d of its last 100 closed PRs" % merged
    if age_days > 7:
        return "late", "opened %d days ago; the field is already in" % age_days
    return "worth a look", "%d PRs, %d merged/100, %d days old" % (prs, merged, age_days)


def gh_get(path):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    status, _, body = http(GH_API + path, headers=headers)
    if status == 403 and "rate limit" in body.lower():
        raise RuntimeError("GitHub rate limit hit. Set GITHUB_TOKEN to raise it.")
    if status != 200:
        raise RuntimeError("GitHub %s on %s: %s" % (status, path, body[:160]))
    return json.loads(body)


VERDICT_RANK = {"worth a look": 0, "late": 1, "stalled": 2, "crowded": 3, "error": 4}


def rank(results):
    """Best first: an unclaimed bounty beats a stalled repo beats a crowded one."""
    return sorted(results, key=lambda r: (VERDICT_RANK.get(r["verdict"], 9), r.get("prs", 0)))


def load_feed():
    status, _, body = http(FEED_JSON)
    if status != 200:
        sys.exit("feed.json returned HTTP %s" % status)
    data = json.loads(body)
    return data if isinstance(data, list) else data.get("items", [])


def merged_rate(owner, repo):
    """Merged PRs per 100 closed ones. Falls back to a smaller page on huge repos."""
    last = None
    for size in (100, 30, 10):
        try:
            closed = gh_get("/repos/%s/%s/pulls?state=closed&per_page=%d" % (owner, repo, size))
        except Exception as exc:      # IncompleteRead on multi-MB responses, timeouts, 5xx
            last = exc
            continue
        if not closed:
            return 0, 0, 0
        merged = sum(1 for p in closed if p.get("merged_at"))
        return merged, len(closed), round(merged * 100.0 / len(closed))
    raise RuntimeError("could not read closed PRs: %s" % str(last)[:80])


def check_issue(owner, repo, number):
    """How contested is this issue? PRs already open on it, and is the repo alive?"""
    issue = gh_get("/repos/%s/%s/issues/%d" % (owner, repo, number))
    created = datetime.datetime.strptime(
        issue["created_at"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    age_days = (datetime.datetime.now(datetime.timezone.utc) - created).days

    query = urllib.parse.quote("repo:%s/%s type:pr %d in:body" % (owner, repo, number))
    prs = gh_get("/search/issues?q=%s&per_page=1" % query)["total_count"]

    merged, sample, per_hundred = merged_rate(owner, repo)

    verdict, why = judge(age_days, prs, per_hundred)
    return {
        "repo": "%s/%s" % (owner, repo),
        "issue": number,
        "title": issue["title"],
        "state": issue["state"],
        "created_at": issue["created_at"][:10],
        "age_days": age_days,
        "comments": issue["comments"],
        "prs": prs,
        "merged_last_100": per_hundred,
        "merged_sample": sample,
        "merged_count": merged,
        "verdict": verdict,
        "why": why,
    }


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
    items = filter_items(data.get("items", []), args.min_usd, args.source,
                         args.exclude_requires)
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
    items = filter_items(items, 0.0, args.source, args.exclude_requires)
    items = items[:args.top] if args.top else items
    if emit({"count": len(items), "items": items}, args.json):
        return
    print_bounties(items)


def cmd_check(args):
    """Is this bounty worth the work? PRs already open on it, and is the repo alive?"""
    if args.all:
        return check_board(args)

    target = args.target.strip()
    parsed = parse_issue_url(target)
    if parsed is None:
        # an id like "frantic:129" — resolve it through the feed first
        match = [i for i in load_feed() if i.get("id") == target]
        if not match:
            sys.exit("no bounty with id %r in the feed" % target)
        parsed = parse_issue_url(match[0].get("url", ""))
        if parsed is None:
            sys.exit("%r is not a GitHub issue bounty: %s" % (target, match[0].get("url")))

    result = check_issue(*parsed)
    if args.json:
        emit(result, True)
        return

    print("%s#%d  %s" % (result["repo"], result["issue"], result["title"][:70]))
    print("-" * 90)
    print("state      %s, opened %s (%d days ago), %d comments"
          % (result["state"], result["created_at"], result["age_days"], result["comments"]))
    print("competing  %d PR(s) mention this issue" % result["prs"])
    print("repo       %d of the last %d closed PRs merged (%d per 100)"
          % (result["merged_count"], result["merged_sample"], result["merged_last_100"]))
    print()
    print("verdict    %s — %s" % (result["verdict"].upper(), result["why"]))
    if result["verdict"] == "crowded":
        print("           you would be entry %d; skip it" % (result["prs"] + 1))
    elif result["verdict"] == "stalled":
        if result["prs"] <= 3:
            print("           few PRs but no merges: this may be a judged bounty rather than a")
            print("           PR race, in which case the merge rate is the wrong signal")
        else:
            print("           PRs here tend to sit unmerged; the reward may never move")


def check_board(args):
    """Triage the whole board: run the check on every GitHub-issue bounty, best first."""
    items = filter_items(load_feed(), args.min_usd, args.source, args.exclude_requires)
    targets = [(it, parse_issue_url(it.get("url", ""))) for it in items]
    targets = [(it, p) for it, p in targets if p]
    if args.limit:
        targets = targets[:args.limit]
    if not targets:
        sys.exit("no GitHub-issue bounties matched (--min-usd %g, --source %s)"
                 % (args.min_usd, args.source or "any"))

    results = []
    for it, parsed in targets:
        try:
            row = check_issue(*parsed)
        except Exception as exc:      # one dead repo must not kill the sweep
            row = {"repo": "%s/%s" % (parsed[0], parsed[1]), "issue": parsed[2],
                   "title": it.get("title", ""), "state": "?", "created_at": "?",
                   "age_days": 0, "comments": 0, "prs": 0, "merged_last_100": 0,
                   "verdict": "error", "why": str(exc)[:90]}
        row["usd"] = it.get("est_usd")
        results.append(row)

    results = rank(results)
    if args.json:
        emit(results, True)
        return

    print("%-34s %7s %6s %7s  %-12s %s" % ("REPO#ISSUE", "USD", "PRs", "MERGED", "VERDICT", "TITLE"))
    print("-" * 112)
    for r in results:
        print("%-34s %7s %6d %7d  %-12s %s" % (
            ("%s#%d" % (r["repo"], r["issue"]))[:34], money(r.get("usd")), r["prs"],
            r["merged_last_100"], r["verdict"].upper(), r["title"][:34]))

    open_now = [r for r in results if r["verdict"] == "worth a look"]
    print("\n%d checked, %d worth a look" % (len(results), len(open_now)))
    if open_now:
        for r in open_now:
            print("  -> %s#%d  %s" % (r["repo"], r["issue"], r["why"]))


def new_items(seen, items, min_usd=0.0, source=None, exclude_requires=()):
    """Items that pass the filters and whose id is not in the state map yet."""
    kept = filter_items(items, min_usd, source, exclude_requires)
    return [it for it in kept if it.get("id") not in seen]


def load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def cmd_watch(args):
    """Poll the board and report only what is new, with a verdict attached."""
    state = load_state(args.state)
    seen = state.setdefault("seen", {})
    first_pass = not seen

    while True:
        try:
            items = load_feed()
        except (SystemExit, RuntimeError) as exc:
            print("[%s] feed error: %s" % (stamp(), exc))
            if args.once:
                return
            time.sleep(args.interval)
            continue

        fresh = new_items(seen, items, args.min_usd, args.source, args.exclude_requires)
        if args.once and first_pass:
            print("[%s] first pass: %d item(s) match, recording state (nothing reported)"
                  % (stamp(), len(fresh)))
        else:
            for it in fresh:
                verdict, why = "unchecked", ""
                parsed = parse_issue_url(it.get("url", ""))
                if parsed:
                    try:
                        row = check_issue(*parsed)
                        verdict, why = row["verdict"], row["why"]
                    except Exception as exc:
                        verdict, why = "error", str(exc)[:70]
                print("[%s] NEW  %-8s %-12s %s" % (
                    stamp(), money(it.get("est_usd")), verdict.upper(),
                    (it.get("title") or "")[:60]))
                if why:
                    print("            %s" % why)
                if it.get("url"):
                    print("            %s" % it["url"])

        for it in filter_items(items, args.min_usd, args.source, args.exclude_requires):
            seen.setdefault(it["id"], {"first_seen": stamp()})
        save_state(args.state, state)

        if args.once:
            return
        time.sleep(args.interval)


def stamp():
    return datetime.datetime.now().strftime("%H:%M:%S")


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


def add_filter_args(p):
    p.add_argument("--min-usd", type=float, default=0.0)
    p.add_argument("--source", default=None, help="algora, taskmarket, direct, frantic, ...")
    p.add_argument("--exclude-requires", default=None,
                   help="drop items needing any of these, e.g. kyc,stake,bond")
    p.add_argument("--no-friction", action="store_true",
                   help="shorthand for --exclude-requires %s" % ",".join(FRICTION))


def main():
    ap = argparse.ArgumentParser(description="Minimal Agent Bounty Feed client")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("earn-now", help="free /earn-now endpoint")
    add_filter_args(p)
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
    add_filter_args(p)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_feed)

    p = sub.add_parser("check", help="how contested is a GitHub bounty? (needs api.github.com)")
    p.add_argument("target", nargs="?", help="bounty id (frantic:129) or an issue URL")
    p.add_argument("--all", action="store_true", help="triage the whole board, best first")
    add_filter_args(p)
    p.add_argument("--limit", type=int, default=10, help="max issues to check with --all")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("watch", help="poll the board, report only new items with a verdict")
    p.add_argument("--interval", type=int, default=300, help="seconds between polls")
    add_filter_args(p)
    p.add_argument("--state", default=".abf-watch.json", help="state file (dedupe across runs)")
    p.add_argument("--once", action="store_true", help="single pass, for cron")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("selftest", help="run the offline test suite (no network)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    if getattr(args, "no_friction", False) and not getattr(args, "exclude_requires", None):
        args.exclude_requires = ",".join(FRICTION)
    if hasattr(args, "exclude_requires"):
        args.exclude_requires = parse_requires(args.exclude_requires)
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
