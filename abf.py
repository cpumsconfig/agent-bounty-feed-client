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
import re
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
        if not isinstance(result, dict):
            return result
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
USD_LIKE = ("USD", "USDC", "OUSD", "USDT", "DAI")


def amount_of(item):
    """Normalised USD value, or None when it cannot be established.

    est_usd is the feed's own normalisation and always wins. Some sources leave it
    null and only fill reward/currency, which used to make those items vanish from
    every --min-usd filter; a stablecoin reward is a safe fallback, anything else
    (FLAPJAX, points, unknown) is not.
    """
    raw = item.get("est_usd")
    if raw is None and str(item.get("currency", "")).upper() in USD_LIKE:
        raw = item.get("reward")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_requires(value):
    """'kyc, stake' -> ('kyc', 'stake'). None or '' -> ()."""
    if not value:
        return ()
    return tuple(p.strip().lower() for p in value.split(",") if p.strip())


def parse_deadline(value):
    """ISO-8601 deadline string -> aware datetime, or None when absent/unparseable."""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def hours_left(value):
    """Hours until a deadline. Negative means it has passed. None means open-ended."""
    dl = parse_deadline(value)
    if dl is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    return (dl - now).total_seconds() / 3600.0


def parse_since(value):
    """'6h' / '2d' / an ISO timestamp -> an ISO timestamp the MCP tool accepts."""
    text = str(value).strip()
    unit = text[-1:].lower()
    if unit in ("h", "d") and text[:-1].isdigit():
        delta = datetime.timedelta(hours=int(text[:-1])) if unit == "h"             else datetime.timedelta(days=int(text[:-1]))
        stamp = datetime.datetime.now(datetime.timezone.utc) - delta
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    return text


def filter_items(items, min_usd=0.0, source=None, exclude_requires=(), closes_within=None):
    """Keep items whose est_usd is known and >= min_usd, preserving feed order.

    exclude_requires drops any item whose requires[] intersects the list — the same
    filter the MCP server applies server-side, done here for the endpoints that take
    no parameters (/earn-now and feed.json are fixed snapshots).
    """
    blocked = set(exclude_requires or ())
    kept = []
    for it in items:
        amount = amount_of(it)
        if amount is None or amount < min_usd:
            continue
        if source and it.get("source") != source:
            continue
        if blocked and blocked & set(it.get("requires") or ()):
            continue
        if closes_within is not None:
            left = hours_left(it.get("deadline"))
            if left is None or left < 0 or left > closes_within:
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


def closes_label(value):
    """Compact time-to-deadline: '3d', '18h', 'past', or an em dash when open-ended."""
    left = hours_left(value)
    if left is None:
        return "—"
    if left < 0:
        return "past"
    if left < 48:
        return "%dh" % int(left)
    return "%dd" % int(left / 24)


def print_bounties(items, limit=None):
    if limit:
        items = items[:limit]
    if not items:
        print("no items")
        return
    print("%-34s %7s  %-11s %-7s %s" % ("ID", "USD", "SOURCE", "CLOSES", "TITLE"))
    print("-" * 108)
    for it in items:
        print("%-34s %7s  %-11s %-7s %s" % (
            str(it.get("id", ""))[:34],
            money(amount_of(it)),
            str(it.get("source", ""))[:11],
            closes_label(it.get("deadline")),
            str(it.get("title", ""))[:50],
        ))
    print("")
    print("%d item(s)" % len(items))

def cmd_earn_now(args):
    status, _, body = http(BASE + "/earn-now")
    if status != 200:
        sys.exit("earn-now returned HTTP %s: %s" % (status, body[:200]))
    data = json.loads(body)
    items = filter_items(data.get("items", []), args.min_usd, args.source,
                         args.exclude_requires, args.closes_within)
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
    try:
        arguments = json.loads(args.arguments)
    except ValueError:
        sys.exit("--arguments must be a JSON object, got: %s" % args.arguments[:80])
    result = client.call_tool(args.tool, arguments)

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
    items = filter_items(items, args.min_usd, args.source, args.exclude_requires,
                         args.closes_within)
    items = items[:args.top] if args.top else items
    if emit({"count": len(items), "items": items}, args.json):
        return
    print_bounties(items)


def cmd_check(args):
    """Is this bounty worth the work? PRs already open on it, and is the repo alive?"""
    if args.all:
        return check_board(args)
    if not args.target:
        sys.exit("check needs a bounty id or an issue URL, or --all for the whole board")

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
    items = filter_items(load_feed(), args.min_usd, args.source,
                         args.exclude_requires, args.closes_within)
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
        row["usd"] = amount_of(it)
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


def new_items(seen, items, min_usd=0.0, source=None, exclude_requires=(), closes_within=None):
    """Items that pass the filters and whose id is not in the state map yet."""
    kept = filter_items(items, min_usd, source, exclude_requires, closes_within)
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
    if args.interval < 30 and not args.once:
        sys.exit("--interval must be at least 30 seconds, got %d" % args.interval)

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

        fresh = new_items(seen, items, args.min_usd, args.source,
                          args.exclude_requires, args.closes_within)
        if first_pass:
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
                    stamp(), money(amount_of(it)), verdict.upper(),
                    (it.get("title") or "")[:60]))
                if why:
                    print("            %s" % why)
                if it.get("url"):
                    print("            %s" % it["url"])

        for it in filter_items(items, args.min_usd, args.source,
                               args.exclude_requires, args.closes_within):
            seen.setdefault(it["id"], {"first_seen": stamp()})
        save_state(args.state, state)
        first_pass = False

        if args.once:
            return
        time.sleep(args.interval)


def stamp():
    return datetime.datetime.now().strftime("%H:%M:%S")


def cmd_get(args):
    """One bounty, straight from the get_bounty MCP tool."""
    client = MCP(args.url)
    client.initialize()
    result = client.call_tool("get_bounty", {"id": args.id})
    if args.json:
        emit(result, True)
        return
    if isinstance(result, str):
        print(result)
        return
    # the tool wraps the bounty: {"generated_at": ..., "item": {...}, "note": ...}
    item = result.get("item") if isinstance(result.get("item"), dict) else result
    print("%s  %s" % (item.get("id", "?"), item.get("title", "")))
    print("-" * 88)
    for key in ("est_usd", "currency", "source", "network", "deadline",
                "slots_left", "requires", "url", "first_seen"):
        if key in item:
            value = item[key]
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False)
            if value is None:
                value = "—"
            print("%-11s %s" % (key, value))
    note = result.get("note")
    if note:
        print("")
        print("note: %s" % str(note)[:220])


def cmd_new(args):
    """Only what appeared after a timestamp: --since 6h, 2d, or an ISO string."""
    since = parse_since(args.since)
    client = MCP(args.url)
    client.initialize()
    result = client.call_tool("get_new_bounties", {"since": since})
    items = result.get("items", []) if isinstance(result, dict) else []
    items = filter_items(items, args.min_usd, args.source, args.exclude_requires,
                         args.closes_within)
    if args.json:
        emit({"since": since, "count": len(items), "items": items}, True)
        return
    print("new since %s" % since)
    print("")
    print_bounties(items, args.limit)


def cmd_mcp_config(args):
    """A paste-ready MCP server block for any host that speaks Streamable HTTP."""
    config = {"mcpServers": {"agent-bounty-feed": {"type": "http", "url": MCP_URL}}}
    print(json.dumps(config, indent=2))
    if not args.quiet:
        print("Paste this into the MCP config of your host and restart it.")


PAID_RE = re.compile(r"([0-9]+) paid / ([0-9]+) rejected", re.I)
SUBS_RE = re.compile(r"([0-9]+) submissions", re.I)
DEAD_REJECTIONS = 10
CONTESTED_AT = 50


def payout_stats(eligibility):
    """'0 paid / 22 rejected' -> (0, 22). None when the prose carries no numbers.

    The feed only states a source's payout history as prose inside eligibility, so
    a client cannot sort on it without parsing. This is that parse.
    """
    match = PAID_RE.search(eligibility or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def source_health(items):
    """First paid/rejected claim seen per source. Sources with no claim are absent."""
    health = {}
    for it in items:
        source = it.get("source")
        if source in health:
            continue
        stats = payout_stats(it.get("eligibility"))
        if stats:
            health[source] = stats
    return health


def is_dead(stats):
    """A source that has never paid and rejects a lot is a filter, not a slow start."""
    if not stats:
        return False
    paid, rejected = stats
    return paid == 0 and rejected > DEAD_REJECTIONS


def submission_count(item):
    """'120 submissions so far' -> 120. None when the field says nothing."""
    match = SUBS_RE.search(item.get("eligibility") or "")
    return int(match.group(1)) if match else None


def is_contested(item):
    """A bounty with 200 entries already in is reachable in theory and lost in practice."""
    count = submission_count(item)
    return count is not None and count >= CONTESTED_AT


def reachable_split(items):
    """Three numbers: everything listed, what needs no human, what survives a dead source."""
    listed = [it for it in items if amount_of(it) is not None]
    reachable = filter_items(items, 0.0, None, FRICTION)
    health = source_health(items)
    dead = {s for s, st in health.items() if is_dead(st)}
    alive = [it for it in reachable if it.get("source") not in dead]
    winnable = [it for it in alive if not is_contested(it)]
    return {
        "listed": listed,
        "reachable": reachable,
        "alive": alive,
        "winnable": winnable,
        "health": health,
        "dead": dead,
    }


def total_usd(items):
    return sum(amount_of(it) or 0 for it in items)


def cmd_reachable(args):
    """What is actually reachable: no KYC, no stake, no dead sources."""
    items = load_feed()
    split = reachable_split(items)
    alive = sorted(split["winnable"], key=lambda it: -(amount_of(it) or 0))

    if args.json:
        emit({
            "listed": {"count": len(split["listed"]), "usd": total_usd(split["listed"])},
            "reachable": {"count": len(split["reachable"]), "usd": total_usd(split["reachable"])},
            "alive": {"count": len(split["alive"]), "usd": total_usd(split["alive"])},
            "winnable": {"count": len(alive), "usd": total_usd(alive)},
            "dead_sources": sorted(split["dead"]),
            "items": alive,
        }, True)
        return

    print("listed     %2d items  $%s" % (len(split["listed"]), money(total_usd(split["listed"]))[1:]))
    print("reachable  %2d items  $%s   no kyc/stake/bond/purchase/human_only"
          % (len(split["reachable"]), money(total_usd(split["reachable"]))[1:]))
    print("alive      %2d items  $%s   minus sources that never pay"
          % (len(split["alive"]), money(total_usd(split["alive"]))[1:]))
    print("winnable   %2d items  $%s   minus %d+ submission contests"
          % (len(alive), money(total_usd(alive))[1:], CONTESTED_AT))

    if split["dead"]:
        print("")
        print("dead sources")
        for source in sorted(split["dead"]):
            paid, rejected = split["health"][source]
            print("  %-11s %d paid / %d rejected" % (source, paid, rejected))

    print("")
    print_bounties(alive, args.limit)
    print("contest filtering uses the feed's own \"N submissions\" figure; a source")
    print("that does not report one stays in, so treat the last line as an upper bound")


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
    p.add_argument("--closes-within", type=float, default=None, metavar="HOURS",
                   help="only items whose deadline is within this many hours")


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

    p = sub.add_parser("get", help="one bounty by id (MCP get_bounty)")
    p.add_argument("id", help="e.g. frantic:129")
    p.add_argument("--url", default=MCP_URL)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("new", help="only what appeared after a time (MCP get_new_bounties)")
    p.add_argument("--since", default="24h", help="6h, 2d, or an ISO timestamp")
    p.add_argument("--url", default=MCP_URL)
    add_filter_args(p)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("reachable", help="what is actually reachable after friction and dead sources")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_reachable)

    p = sub.add_parser("mcp-config", help="print a paste-ready MCP server config")
    p.add_argument("--quiet", action="store_true", help="JSON only, no trailing hint")
    p.set_defaults(func=cmd_mcp_config)

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
    for name in ("min_usd", "source", "exclude_requires", "closes_within"):
        if not hasattr(args, name):
            setattr(args, name, None if name in ("source", "closes_within") else (() if name == "exclude_requires" else 0.0))
    try:
        args.func(args)
    except urllib.error.URLError as exc:
        print("network error: %s" % getattr(exc, "reason", exc))
        print("hint: blocked network? export HTTPS_PROXY=http://host:port; "
              "offline checks still work: python3 abf.py selftest")
        sys.exit(1)
    except (RuntimeError, ValueError) as exc:
        sys.exit("error: %s" % exc)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
