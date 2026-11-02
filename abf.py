#!/usr/bin/env python3
"""abf.py - minimal Agent Bounty Feed client.

Stdlib only. No pip install, no API key.

    python3 abf.py earn-now              # free endpoint, no filter by default
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
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://agent-bounty-feed.dailydollar.workers.dev"
MCP_URL = BASE + "/mcp"
FEED_JSON = "https://dailydollar-bot.github.io/agent-bounty-feed/feed.json"
UA = "abf-demo/1.0 (+python-stdlib)"
TIMEOUT = 25
RETRY_STATUS = (429, 500, 502, 503, 504)


# ---------------------------------------------------------------- transport

def http(url, payload=None, headers=None, timeout=TIMEOUT, attempts=3):
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs,
                                 method="POST" if body else "GET")
    # Retrying a POST is only safe because every call this client makes is read-only.
    last = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            # 429 and the gateway 5xx family are transient: retry those, but a 404 is
            # an answer and must be returned as one
            if exc.code in RETRY_STATUS and attempt + 1 < attempts:
                wait = exc.headers.get("Retry-After") if exc.headers else None
                time.sleep(min(float(wait), 30) if (wait or "").isdigit() else 1.5 * (attempt + 1))
                continue
            return exc.code, dict(exc.headers), body
        except OSError as exc:      # URLError, TLS resets, timeouts
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise last


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
        # Echo the session only if the server issued one. Stateless servers never do,
        # and sending a made-up header is what breaks clients against them.
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

        ctype = header(hdrs, "Content-Type").lower()
        if not body.strip():
            candidates = []
        elif "json" not in ctype and "event-stream" not in ctype and body.lstrip()[:1] not in "{[":
            # a proxy error page or a captive portal, not a JSON-RPC answer
            raise RuntimeError("%s returned %s, not JSON-RPC: %r"
                               % (method, ctype or "no content-type", body[:120]))

        if "text/event-stream" in ctype:
            candidates = []
            for frame in sse_data_frames(body):
                if not frame.strip():
                    continue
                try:
                    candidates.append(json.loads(frame))
                except ValueError:
                    continue      # a keep-alive or non-JSON frame is not an error
        elif body.strip():
            candidates = [json.loads(body)]

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
        if result.get("isError"):
            # MCP reports tool failures as HTTP 200 with isError, not as a JSON-RPC error
            texts = [c.get("text", "") for c in (result.get("content") or [])
                     if isinstance(c, dict) and c.get("type") == "text"]
            raise RuntimeError("%s failed: %s" % (name, " ".join(texts)[:200] or "no detail"))
        if isinstance(result.get("structuredContent"), (dict, list)):
            return result["structuredContent"]
        blocks = result.get("content") or []
        texts = [c.get("text", "") for c in blocks
                 if isinstance(c, dict) and c.get("type") == "text"]
        joined = "\n".join(texts).strip()
        try:
            return json.loads(joined)
        except (ValueError, TypeError):
            return joined


# ---------------------------------------------------------------- rendering

def money(value):
    """Compact cell format. Large values get an M suffix rather than %g's exponent."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(amount):
        return "—"
    if amount >= 1000000:
        return "$%.1fM" % (amount / 1000000.0)
    return "$%g" % amount


def usd_total(value):
    """Summary format: thousands separators, no exponent, no trailing zeros."""
    try:
        amount = round(float(value), 2)
    except (TypeError, ValueError):
        return "0"
    if not math.isfinite(amount):
        return "0"
    text = "{:,}".format(amount)
    return text.rstrip("0").rstrip(".") if "." in text else text


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
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # nan/inf compare false against every threshold, so they would slip through
    # --min-usd and poison every total; treat them as unknown
    return value if math.isfinite(value) else None


def requires_of(item):
    """The item's requirement tags, tolerating a comma string where a list is expected."""
    value = item.get("requires")
    if isinstance(value, str):
        return tuple(p.strip().lower() for p in value.split(",") if p.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip().lower() for v in value if v)
    return ()


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
        stamp = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    # "2026-11-12" parses as naive; subtracting it from an aware now() raises, so
    # treat a missing offset as UTC rather than crashing the whole table
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp


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
    """Apply the four filters in a fixed order, preserving feed order.

    An item whose price cannot be established is kept when no floor is set (unknown is
    not zero) and dropped once a floor is, because it cannot be shown to clear it.

    exclude_requires mirrors the filter the MCP server applies server-side; it is done
    here because /earn-now and feed.json are fixed snapshots that take no parameters.
    """
    blocked = set(exclude_requires or ())
    kept = []
    for it in items:
        if not isinstance(it, dict):
            continue
        amount = amount_of(it)
        if amount is None:
            # unpriced is not the same as worthless: keep it unless a floor is set,
            # in which case it cannot be shown to clear that floor
            if min_usd > 0:
                continue
        elif amount < min_usd:
            continue
        if source and it.get("source") != source:
            continue
        if blocked and blocked & set(requires_of(it)):
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
    if not isinstance(text, str):
        return None
    marker = "github.com/"
    if marker not in text:
        return None
    rest = text.split(marker, 1)[1].strip().rstrip("/")
    parts = rest.split("/")
    if len(parts) < 4 or parts[2] != "issues":
        return None
    owner, repo, number = parts[0], parts[1], parts[3].split("?")[0].split("#")[0]
    if not number.isdigit():
        return None
    # these two go straight into an API path; keep them to what GitHub allows
    for part in (owner, repo):
        # dots are legal in repo names (owner.github.io), so reject dot-only parts
        # explicitly rather than relying on the character class
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or not re.search(r"[A-Za-z0-9]", part):
            return None
    return owner, repo, int(number)


def judge(age_days, prs, merged):
    """Turn three numbers into a verdict. Pure, so it is cheap to test.

    Thresholds come from what the board actually looked like: the busiest issues carry
    90-120 PRs, repos that still merge sit well above 5 per 100, and a week is roughly
    when a bounty's first PRs land.
    """
    if prs > 10:
        return "crowded", "%d PRs already compete" % prs
    if merged < 5:
        return "stalled", "repo merges only %d per 100 closed PRs" % merged
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


def load_feed_doc():
    """The whole feed document, so callers can reach stats.payout_evidence too."""
    status, _, body = http(FEED_JSON)
    if status != 200:
        sys.exit("feed.json returned HTTP %s" % status)
    data = json.loads(body)
    if isinstance(data, list):
        return {"items": data, "stats": {}}
    return data


def load_feed():
    return load_feed_doc().get("items", [])


_MERGE_CACHE = {}
_MERGE_LOCK = threading.Lock()


def merged_rate(owner, repo):
    """Merged PRs per 100 closed ones. Falls back to a smaller page on huge repos.

    Cached per (owner, repo) for the life of the process: a board sweep often has four
    or five issues from the same repository, and each uncached call costs a 100-PR page.
    """
    key = (owner, repo)
    with _MERGE_LOCK:
        if key in _MERGE_CACHE:
            return _MERGE_CACHE[key]
    result = _merged_rate_uncached(owner, repo)
    with _MERGE_LOCK:
        _MERGE_CACHE[key] = result
    return result


def _merged_rate_uncached(owner, repo):
    last = None
    for size in (100, 30, 10):
        try:
            closed = gh_get("/repos/%s/%s/pulls?state=closed&per_page=%d" % (owner, repo, size))
        except Exception as exc:      # IncompleteRead on multi-MB responses, timeouts, 5xx
            if "GitHub 4" in str(exc):
                # a 404 or a rate-limit answer will not improve with a smaller page;
                # retrying it just burns quota
                raise
            last = exc
            continue
        if not closed:
            return 0, 0, 0
        merged = sum(1 for p in closed if p.get("merged_at"))
        return merged, len(closed), round(merged * 100.0 / len(closed))
    raise RuntimeError("could not read closed PRs: %s" % str(last)[:80])


def linked_prs(owner, repo, number, max_pages=5):
    """PRs that actually link to this issue, counted from the timeline.

    A text search on the issue number is unusable for small numbers: `type:pr 5 in:body`
    matches 577 PRs in an active repo, because every "5" in every body counts. Measured
    on Go-Hybrid#5: search says 577, the timeline says 22.
    """
    seen = set()
    for page in range(1, max_pages + 1):
        events = gh_get("/repos/%s/%s/issues/%d/timeline?per_page=100&page=%d"
                        % (owner, repo, number, page))
        if not isinstance(events, list) or not events:
            break
        for ev in events:
            if ev.get("event") != "cross-referenced":
                continue
            source = ((ev.get("source") or {}).get("issue") or {})
            if "pull_request" in source and source.get("number"):
                seen.add(source["number"])
        if len(events) < 100:
            break
    return len(seen)


def searched_prs(owner, repo, number):
    """Fallback when the timeline is unavailable. Noisy for small issue numbers."""
    query = urllib.parse.quote("repo:%s/%s type:pr %d in:body" % (owner, repo, number))
    return gh_get("/search/issues?q=%s&per_page=1" % query)["total_count"]


def check_issue(owner, repo, number):
    """How contested is this issue? PRs already open on it, and is the repo alive?"""
    issue = gh_get("/repos/%s/%s/issues/%d" % (owner, repo, number))
    created = datetime.datetime.strptime(
        issue["created_at"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    age_days = (datetime.datetime.now(datetime.timezone.utc) - created).days

    try:
        prs = linked_prs(owner, repo, number)
    except (RuntimeError, ValueError) as exc:
        text = str(exc)
        # a rate limit or a missing repo will fail the same way twice, and Search has
        # its own, much smaller quota (10/min unauthenticated): do not fall back
        if "rate limit" in text.lower() or "GitHub 404" in text or "GitHub 403" in text:
            raise
        prs = searched_prs(owner, repo, number)   # noisier, but better than nothing

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
    if limit is not None:
        items = items[:limit]
    if not items:
        print("no items")
        return
    print("%-34s %7s  %-11s %-7s %s" % ("ID", "USD", "SOURCE", "CLOSES", "TITLE"))
    print("-" * 108)
    shown = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        shown += 1
        print("%-34s %7s  %-11s %-7s %s" % (
            str(it.get("id", ""))[:34],
            money(amount_of(it)),
            str(it.get("source", ""))[:11],
            closes_label(it.get("deadline")),
            str(it.get("title", ""))[:50],
        ))
    print("")
    print("%d item(s)" % shown)

def cmd_earn_now(args):
    status, _, body = http(BASE + "/earn-now")
    if status != 200:
        sys.exit("earn-now returned HTTP %s: %s" % (status, body[:200]))
    data = json.loads(body)
    items = filter_items(data.get("items", []), args.min_usd, args.source,
                         args.exclude_requires, args.closes_within)
    if args.limit is not None:
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
    names = [t.get("name") for t in tools]
    if args.tool not in names:
        sys.exit("no tool named %r on this server; available: %s"
                 % (args.tool, ", ".join(n for n in names if n)))
    try:
        arguments = json.loads(args.arguments)
    except ValueError:
        sys.exit("--arguments must be a JSON object, got: %s" % args.arguments[:80])
    if not isinstance(arguments, dict):
        sys.exit("--arguments must be a JSON object, got a %s" % type(arguments).__name__)
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
    items = items[:args.top] if args.top is not None else items
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

    def one(pair):
        it, parsed = pair
        try:
            row = check_issue(*parsed)
        except Exception as exc:      # one dead repo must not kill the sweep
            row = {"repo": "%s/%s" % (parsed[0], parsed[1]), "issue": parsed[2],
                   "title": it.get("title", ""), "state": "?", "created_at": "?",
                   "age_days": 0, "comments": 0, "prs": 0, "merged_last_100": 0,
                   "verdict": "error", "why": str(exc)[:90]}
        row["usd"] = amount_of(it)
        return row

    jobs = max(1, getattr(args, "jobs", 1) or 1)
    if jobs > 1 and len(targets) > 1:
        # opt-in only: each repo costs three API calls and unauthenticated GitHub
        # allows 60 an hour, so the default stays serial
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(jobs, len(targets))) as pool:
            results = list(pool.map(one, targets))
    else:
        results = [one(p) for p in targets]

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

    as_json = getattr(args, "json", False)
    state = load_state(args.state)
    seen = state.setdefault("seen", {})
    # A first run records the board instead of reporting it, so a restart does not
    # replay every listing as "new".
    first_pass = not seen

    while True:
        try:
            items = load_feed()
        except Exception as exc:      # a bad body or a socket error must not end the loop
            if as_json:
                print(json.dumps({"event": "error", "error": str(exc)}), flush=True)
            else:
                print("[%s] feed error: %s" % (stamp(), exc), flush=True)
            if args.once:
                return
            time.sleep(args.interval)
            continue

        fresh = new_items(seen, items, args.min_usd, args.source,
                          args.exclude_requires, args.closes_within)
        if first_pass:
            if as_json:
                print(json.dumps({"event": "first_pass", "matched": len(fresh)}), flush=True)
            else:
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
                if as_json:
                    # one object per line, so another agent can pipe this
                    # flush: in a pipe stdout is block-buffered, so an unflushed line
                    # can sit for hours while the consumer waits
                    print(json.dumps({"event": "new", "verdict": verdict, "why": why,
                                      "usd": amount_of(it), "item": it},
                                     ensure_ascii=False), flush=True)
                    continue
                print("[%s] NEW  %-8s %-12s %s" % (
                    stamp(), money(amount_of(it)), verdict.upper(),
                    (it.get("title") or "")[:60]))
                if why:
                    print("            %s" % why)
                if it.get("url"):
                    print("            %s" % it["url"])

        for it in filter_items(items, args.min_usd, args.source,
                               args.exclude_requires, args.closes_within):
            if it.get("id"):
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
    if result is None:
        sys.exit("no bounty returned for %r" % args.id)
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


def collect_new(client, since, max_pages=10):
    """Walk get_new_bounties to exhaustion.

    The tool pages: a response carries has_more plus next_since / next_after_id, and
    stopping after the first page silently drops everything past the cut.
    """
    items = []
    cursor = {"since": since}
    for _ in range(max_pages):
        result = client.call_tool("get_new_bounties", dict(cursor))
        if not isinstance(result, dict):
            break
        items.extend(result.get("items") or [])
        if not result.get("has_more"):
            break
        nxt = {k: result[k] for k in ("next_since", "next_after_id") if result.get(k)}
        if not nxt or nxt == cursor:
            break
        cursor = nxt
    return items


def cmd_new(args):
    """Only what appeared after a timestamp: --since 6h, 2d, or an ISO string."""
    since = parse_since(args.since)
    client = MCP(args.url)
    client.initialize()
    items = collect_new(client, since)
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
    """'0 paid / 22 rejected' -> (0, 22). None when that sentence is absent.

    The producer attaches this sentence to *one task* ("this bounty's claims so far").
    It is not a source lifetime, and absence is unknown rather than zero — see issue #1.
    """
    if not isinstance(eligibility, str):
        return None
    match = PAID_RE.search(eligibility)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def board_health(payout_evidence):
    """Source-level payout counts, as the feed states them, with their stated basis.

    This is the only source-scoped number in the feed. The `N paid / M rejected` sentence
    inside an item's eligibility is about *that task*, not about the source, so it must
    never be promoted to a source-wide verdict: doing that made the result depend on which
    task happened to appear first in feed order.
    """
    health = {}
    for source, entry in (payout_evidence or {}).items():
        if not isinstance(entry, dict):
            continue
        paid = entry.get("paid_count")
        if isinstance(paid, str) and paid.strip().isdigit():
            paid = int(paid)      # the feed has been seen to quote numbers
        health[source] = {
            "paid": paid,
            "usd": entry.get("paid_usd"),
            "basis": entry.get("basis"),
            "known": paid is not None,
        }
    return health


def source_is_dead(entry):
    """Only an explicit zero at source scope counts. Unknown is not dead."""
    return bool(entry) and entry.get("paid") == 0


def task_is_dead(item):
    """This task's own claim history: zero paid with a meaningful number of rejections."""
    stats = payout_stats(item.get("eligibility"))
    if not stats:
        return False
    paid, rejected = stats
    return paid == 0 and rejected > DEAD_REJECTIONS


def submission_count(item):
    """'120 submissions so far' -> 120. None when the field says nothing."""
    text = item.get("eligibility")
    if not isinstance(text, str):
        return None
    match = SUBS_RE.search(text)
    return int(match.group(1)) if match else None


def is_contested(item, threshold=CONTESTED_AT):
    """A bounty with 200 entries already in is reachable in theory and lost in practice."""
    count = submission_count(item)
    return count is not None and count >= threshold


def is_expired(item):
    """A deadline in the past is as unreachable as a KYC wall."""
    left = hours_left(item.get("deadline"))
    return left is not None and left < 0


def reachable_split(items, contested_at=CONTESTED_AT, payout_evidence=None):
    """Four tiers, each with its own evidence scope.

    listed -> reachable (friction, deadline) -> alive (payout evidence) -> winnable (crowd).
    A source is dropped only on source-level evidence (stats.payout_evidence); an item only
    on its own task-level claim history.
    """
    # the tiers must nest: reachable is a subset of listed, not a parallel set,
    # otherwise the "-N" arithmetic in the output goes negative
    listed = [it for it in items if isinstance(it, dict) and amount_of(it) is not None]
    reachable = [it for it in filter_items(listed, 0.0, None, FRICTION)
                 if not is_expired(it)]
    health = board_health(payout_evidence)
    dead = {s for s, entry in health.items() if source_is_dead(entry)}
    alive = [it for it in reachable
             if it.get("source") not in dead and not task_is_dead(it)]
    winnable = [it for it in alive if not is_contested(it, contested_at)]
    return {
        "listed": listed,
        "reachable": reachable,
        "alive": alive,
        "winnable": winnable,
        "health": health,
        "dead": dead,
    }


def friction_breakdown(items):
    """How many items each friction tag is responsible for, most common first."""
    counts = {}
    for it in items:
        for tag in sorted(set(requires_of(it)) & set(FRICTION)):
            counts[tag] = counts.get(tag, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def total_usd(items):
    return sum(amount_of(it) or 0 for it in items if isinstance(it, dict))


def cmd_reachable(args):
    """What is actually reachable: no KYC, no stake, no dead sources."""
    doc = load_feed_doc()
    items = doc.get("items", [])
    max_subs = getattr(args, "max_subs", CONTESTED_AT)
    stats = doc.get("stats")
    evidence = stats.get("payout_evidence") if isinstance(stats, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    split = reachable_split(items, max_subs, evidence)
    alive = sorted(split["winnable"], key=lambda it: -(amount_of(it) or 0))

    if args.json:
        emit({
            "listed": {"count": len(split["listed"]), "usd": total_usd(split["listed"])},
            "reachable": {"count": len(split["reachable"]), "usd": total_usd(split["reachable"])},
            "alive": {"count": len(split["alive"]), "usd": total_usd(split["alive"])},
            "winnable": {"count": len(alive), "usd": total_usd(alive)},
            "dead_sources": sorted(split["dead"]),
            "payout_evidence": split["health"],
            "items": alive,
        }, True)
        return

    # identity sets, not `in` on a list: membership by equality made this O(n^2)
    reachable_ids = {id(it) for it in split["reachable"]}
    alive_ids = {id(it) for it in split["alive"]}
    winnable_ids = {id(it) for it in split["winnable"]}
    dropped_friction = [it for it in split["listed"] if id(it) not in reachable_ids]
    dropped_dead = [it for it in split["reachable"] if id(it) not in alive_ids]
    dropped_crowd = [it for it in split["alive"] if id(it) not in winnable_ids]

    def row(label, items, note=""):
        print(("%-10s %2d items  $%-10s %s" % (label, len(items), usd_total(total_usd(items)), note)).rstrip())

    tags = friction_breakdown(dropped_friction)
    expired = [it for it in dropped_friction if is_expired(it)]
    reasons = ["%s %d" % (t, n) for t, n in tags]
    if expired:
        reasons.append("expired %d" % len(expired))
    row("listed", split["listed"], "")
    row("reachable", split["reachable"],
        "-%d  %s" % (len(dropped_friction), ", ".join(reasons) or "friction"))
    dead_sources = ", ".join(sorted(split["dead"])) or "none"
    row("alive", split["alive"],
        "-%d  task shows 0 paid%s" % (
            len(dropped_dead),
            "; dead source: " + dead_sources if split["dead"] else ""))
    row("winnable", split["winnable"],
        "-%d  crowded (%d+ submissions)" % (len(dropped_crowd), max_subs))

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
    p.add_argument("--jobs", type=int, default=1, metavar="N",
                   help="check N issues in parallel (default 1; each costs 3 API calls)")
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
    p.add_argument("--max-subs", type=int, default=CONTESTED_AT, metavar="N",
                   help="treat N or more submissions as crowded (default %d)" % CONTESTED_AT)
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
    p.add_argument("--json", action="store_true", help="one JSON object per line (JSONL)")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("selftest", help="run the offline test suite (no network)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    if hasattr(args, "exclude_requires"):
        wanted = set(parse_requires(args.exclude_requires))
        if getattr(args, "no_friction", False):
            wanted.update(FRICTION)      # union, not override: both flags must apply
        args.exclude_requires = tuple(sorted(wanted))
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
