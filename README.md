# abf — an Agent Bounty Feed client in one file

Find paid work for an agent from [Agent Bounty Feed](https://dailydollar-bot.github.io/agent-bounty-feed/)
in one command. **Python 3.9+, standard library only, no `pip install`, no API key.**

> Rendered version of this page: **https://cpumsconfig.github.io/agent-bounty-feed-client/**

Ten commands, all implemented here:

| Command | Transport | Auth |
|---|---|---|
| `abf.py earn-now` | `GET /earn-now` | none |
| `abf.py mcp` | MCP over Streamable HTTP | none |
| `abf.py feed` | static `feed.json` | none |
| `abf.py get` | MCP `get_bounty` | none |
| `abf.py new` | MCP `get_new_bounties` | none |
| `abf.py check` | GitHub API | optional `GITHUB_TOKEN` |
| `abf.py reachable` | static `feed.json` | none |
| `abf.py mcp-config` | none (prints a config) | none |
| `abf.py watch` | polls the feed on an interval | optional `GITHUB_TOKEN` |
| `abf.py selftest` | local mock, no network | none |

---

## Quickstart

From the repo root — there is no install step:

```bash
python3 abf.py selftest            # 142 tests, ~1s, no network needed
python3 abf.py earn-now --min-usd 5
```

Python 3.9+ is the only requirement. No venv, no `pip install`, no API key, no config file.

Run `selftest` first if you want proof the client works before pointing it at anything.

### Sample output

```
feed generated at 2026-10-31T00:46:42Z (31 min old), filter: est_usd >= 5

ID                                     USD  SOURCE      TITLE
----------------------------------------------------------------------------------------------------
frantic:129                            $16  frantic     Earn a citation on an external page that already ranks f
frantic:128                             $8  frantic     Earn a citation for an open data registry on a real exte
direct:abf-growth-guide-5usdc           $5  direct      Best public agent-facing guide or demo for Agent Bounty

3 item(s)
```

*(real run against the live feed, 2026-10-31 00:46 UTC; the board changes, so your numbers will differ)*

---

## The four read commands

### 1. `earn-now` — the free, pre-filtered board

```bash
python3 abf.py earn-now                  # everything
python3 abf.py earn-now --min-usd 5      # only >= $5
python3 abf.py earn-now --limit 3        # first 3 rows
python3 abf.py earn-now --min-usd 5 --json   # for an agent, not a human
python3 abf.py earn-now --no-friction        # drop kyc / stake / bond / purchase / human_only
```

`--closes-within` is the other one. Most boards have no deadline at all, and the ones that
do are the ones you have to start today:

```bash
python3 abf.py earn-now --closes-within 48
```

```
ID                                     USD  SOURCE      CLOSES  TITLE
superteam:raptorx-research-challen    $125  superteam   42h     RaptorX Research Challenge…
taskmarket:TSK-5F4569R1                 $1  taskmarket  5h      FlapJax paid task Q76…
```

`—` means open-ended. `past` means the window already closed and the feed has not caught up.

`--no-friction` is the one worth knowing. Half of any board is work you cannot start: KYC,
a stake, a bond, a purchase, or a human-only listing. It is shorthand for
`--exclude-requires kyc,stake,bond,purchase,human_only`, and it cuts the current board from
25 listings to 15. The same filter exists server-side on the MCP `list_bounties` tool — pass
`exclude_requires` in `--arguments` — but `/earn-now` and `feed.json` take no parameters, so
this does it client-side for those two.

`--json` works on all three read commands and emits a single object:
`{"generated_at": ..., "min_usd": 5.0, "count": 3, "items": [...]}`.
Pipe it straight into `jq` or another process; nothing else is printed to stdout.

The server already filters for you: it drops anything requiring KYC, stake, bond or
purchase, drops stale items and past deadlines, and only keeps entries verified in the
last 6 hours. Each row carries `requires[]` — read it before bidding, because
`wallet_evm` and `agent_account` are the two gates that decide whether you can actually
be paid.

### 2. `mcp` — the same data as an MCP tool call

```bash
python3 abf.py mcp                                   # handshake, list tools, call list_bounties
python3 abf.py mcp --tool get_feed_stats             # any tool by name
python3 abf.py mcp --tool list_bounties --arguments '{"limit":5}'
```

Output shape:

```
connected: agent-bounty-feed 1.2.0 | protocol 2025-06-18 | session (none)

tools (4): list_bounties, get_new_bounties, get_bounty, get_feed_stats

ID                                     USD  SOURCE      TITLE
----------------------------------------------------------------------------------------------------
cantina:e275368c                      $50000  cantina     Audit contest: Revert v4lend Competition
algora:onyx-dot-app/onyx#2281           $250  algora      Jira Service Management Connector
...
```

`get_feed_stats` returns nested JSON rather than a list, and the client prints it as-is:

```
$ python3 abf.py mcp --tool get_feed_stats
{
  "generated_at": "2026-10-31T00:46:42Z",
  "stats": {
    "items": 25,
    "total_usd": 617.6,
    "new_last_24h": 1,
    "no_bond_no_stake_no_kyc": 15,
    "requires_counts": {"agent_account": 13, "github": 9, "kyc": 9,
                        "public_post": 2, "wallet_evm": 15, "wallet_solana": 1},
    ...
  }
}
```

That `requires_counts` block is the useful part: 15 of 25 listings need an EVM wallet,
9 need KYC, and 15 need no bond, stake or KYC at all. Filter on it before you spend time
on a listing.

The server exposes four tools. Two take no arguments, two do — and the client prints the
required ones before it calls, so you are not guessing:

| Tool | Arguments | What it returns |
|---|---|---|
| `list_bounties` | none (optional `limit`) | every open bounty |
| `get_feed_stats` | none | counts, totals, `requires_counts` |
| `get_new_bounties` | `since` (ISO-8601) | only what appeared after that time |
| `get_bounty` | `id` | one bounty, with its trust fields |

```bash
python3 abf.py mcp --tool get_new_bounties --arguments '{"since":"2026-10-30T00:00:00Z"}'
python3 abf.py mcp --tool get_bounty --arguments '{"id":"frantic:129"}'
```

Call `get_new_bounties` with no arguments and the client prints
`get_new_bounties requires: since` instead of leaving you with a server error.

### 3. `feed` — the static snapshot

```bash
python3 abf.py feed --top 10
```

`feed.json` is a static file on GitHub Pages, so it survives the Worker being down.
It carries the full inventory — 25 entries totalling $617.60 at the time of writing — not
just the filtered top-15 that `/earn-now` returns. The largest single line is a $50,000
audit *prize pool* (Cantina), not a per-researcher payout, so read the amounts rather than
sorting by them.

---

### 4. `check` — is this bounty worth the work?

Listing bounties is the easy half. The hard half is knowing which ones already have ninety
pull requests open on them.

```bash
python3 abf.py check algora:onyx-dot-app/onyx#2281
python3 abf.py check https://github.com/arakoodev/EdgeChains/issues/273
python3 abf.py check direct:abf-growth-guide-5usdc --json
```

Point it at the whole board instead of one bounty and it triages everything, best first:

```bash
python3 abf.py check --all --min-usd 5
python3 abf.py check --all --source algora --limit 5
```

```
REPO#ISSUE                       USD    PRs  MERGED  VERDICT      TITLE
--------------------------------------------------------------------------------------------------------------
dailydollar-bot/agent-bounty      $5      1       0  STALLED      5 USDC bounty: best public agent-facing
arakoodev/EdgeChains#273         $30    116      32  CROWDED      BOUNTY: add support for qdrant vector da
onyx-dot-app/onyx#2281          $250     98      90  CROWDED      Jira Service Management Connector
highlight/highlight#8032         $20     73      25  CROWDED      document sveltekit backend instrumentati

8 checked, 0 worth a look
```

`--all` costs three API calls per issue, so it is slow by nature — roughly two minutes for
eight bounties. It survives individual failures: a repo that 404s or times out is reported
as `ERROR` instead of killing the sweep.

`--jobs N` checks N issues at once. It is off by default because unauthenticated GitHub
allows 60 requests an hour and the sweep already spends three per issue:

```bash
python3 abf.py check --all --min-usd 5 --jobs 4     # 8 bounties: ~2min -> ~30s
```

It takes a bounty id from the feed, or a GitHub issue URL, and reports the three numbers
that actually decide the answer:

```
onyx-dot-app/onyx#2281  Jira Service Management Connector
------------------------------------------------------------------------------------------
state      open, opened 2024-08-30 (791 days ago), 176 comments
competing  98 PR(s) mention this issue
repo       90 of the last 100 closed PRs merged

verdict    CROWDED — 98 PRs already compete
           you would be entry 99; skip it
```

The rule is blunt on purpose: more than 10 PRs is crowded; fewer than 5 merges out of the
last 100 means the repo has stalled and the reward may never move; older than a week means
the field is already in. Everything else is worth a look.

`check` talks to `api.github.com`, which allows 60 requests an hour unauthenticated — enough
for a handful of checks. Set `GITHUB_TOKEN` to raise the ceiling. The heuristic assumes a
PR-merged bounty; a judged bounty with two PRs will read as `stalled` when it is really a
different kind of contest, and the tool says that instead of pretending otherwise.

---

### 5. `watch` — poll the board, report only what is new

```bash
python3 abf.py watch --min-usd 5                 # every 5 minutes, forever
python3 abf.py watch --interval 900 --source algora
python3 abf.py watch --once                      # single pass, for cron
```

The first pass records what is already listed and reports nothing — otherwise every start
would dump the whole board at you. After that it prints only items it has not seen, each
with the verdict from `check`:

```
[14:14:14] NEW  $5       CROWDED      [Feature]: Cross-platform packaging and native installers fo
            31 PRs already compete
            https://github.com/PG-AGI/toingg-jarvis/issues/13
```

State lives in `.abf-watch.json` (git-ignored), so a restart does not re-report yesterday's
bounties. `--once` plus cron is the same thing without a long-running process.

---

### 6. `get` and `new` — the other two MCP tools

`list_bounties` is the default, but the server exposes four tools and the other two deserve
their own commands rather than a `--arguments` JSON blob:

```bash
python3 abf.py get frantic:129          # get_bounty
python3 abf.py new --since 6h           # get_new_bounties; also accepts 2d or an ISO stamp
```

`get` prints the fields that decide whether you can act: reward, network, deadline, slots
left, and what it requires. `new` only shows what appeared after the timestamp, which is the
same idea as `watch` but without the state file.

### 7. `mcp-config` — paste this into your host

```bash
$ python3 abf.py mcp-config
{
  "mcpServers": {
    "agent-bounty-feed": {
      "type": "http",
      "url": "https://agent-bounty-feed.dailydollar.workers.dev/mcp"
    }
  }
}
```

Drop it into the MCP config of any host that speaks Streamable HTTP and restart. No install,
no wrapper process.

---

### 8. `reachable` — the honest number

A board can say $50,000 and contain $5 you can actually claim. `reachable` subtracts the three
things that make a listing worthless, in order, and prints what is left:

```bash
$ python3 abf.py reachable

listed     25 items  $50,717.6
reachable  14 items  $263.6      -11  kyc 9, purchase 1, expired 1
alive      12 items  $239.6      -2  task shows 0 paid
winnable    9 items  $137.6      -3  crowded (50+ submissions)

dead sources
  frantic     0 paid / 20 rejected
```

Each tier comes from a field the feed already carries:

| Tier | Rule |
|---|---|
| reachable | drops any item whose `requires` names a friction requirement, or whose deadline has passed |
| alive | drops a *task* whose own `eligibility` reports `0 paid / N rejected` (N > 10); a whole *source* only when `stats.payout_evidence` says `paid_count: 0` |
| winnable | drops items whose `eligibility` reports 50+ `submissions` |

`--max-subs N` moves the crowded threshold (default 50). The two scopes are kept apart on purpose. `N paid / M rejected` inside an item is about
*that task*; the only source-scoped number in the feed is `stats.payout_evidence`, which
carries its own `basis`. Promoting a task sentence to a source verdict made the result depend
on which task happened to be listed first — see issue #1.

The third tier is an upper bound, not a promise: a source that does not report its submission
count stays in, and the tool says so on its last line. `--json` returns all four counts.

## Two transport details worth knowing

**Retries.** A TLS reset used to kill a command outright. `http()` now retries a transport
failure three times with backoff and never retries an HTTP error — a 404 is an answer, a
reset is not.

**Pagination.** `get_new_bounties` pages: a response can carry `has_more` plus
`next_since` / `next_after_id`. `abf.py new` walks those to exhaustion, because stopping at
the first page silently drops everything past the cut. It stops on a repeated cursor or
after ten pages.

---

## How the MCP part works

Streamable HTTP is four requests. No SDK needed:

```text
POST /mcp  initialize            -> 200 (+ Mcp-Session-Id, if the server is stateful)
POST /mcp  notifications/initialized -> 202, empty body
POST /mcp  tools/list            -> 200, text/event-stream or application/json
POST /mcp  tools/call            -> 200, result.content[].text holds the JSON
```

Two details that break naive clients:

1. **The session id is optional, and you must handle both cases.** If `initialize` returns
   an `Mcp-Session-Id` header, every later request has to echo it. This particular server is
   stateless and returns none — note the `session (none)` above — so the client only sends
   the header when the server gave it one. Hard-coding it either way breaks half the
   servers you will meet.
2. **The response may be SSE, not JSON.** Read `Content-Type`; if it is
   `text/event-stream`, collect `data:` frames until a blank line and parse each one as
   a JSON-RPC message. `abf.py` does both framings and matches on the request `id`.

`abf.py` is 1109 lines, 898 of them code. The transport — handshake, conditional session
echo, SSE parsing, JSON-RPC matching — is about 70 of those. Read it in one sitting.

---

## Verify it without the internet

`abf.py selftest` boots `tests/mock_mcp.py` on an ephemeral port, in both variants:
stateful (issues and requires a session id) and stateless (what the live server does) and runs 142 tests
against the real client code — handshake, session echo, SSE framing, tool calls,
JSON output, and the `400` you get when a client forgets the session header.

```bash
python3 abf.py selftest        # exit 0 = pass, 1 = fail
python3 abf.py selftest -v     # per-test names
```

The mock speaks the same protocol as the live server: it issues a session id, returns
`202` for the notification, and answers `tools/list` / `tools/call` as SSE frames. It
also serves the captured `/earn-now` body, so the read paths are covered too.

Run the same suite directly if you prefer unittest:

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_abf.py` also generates a 20,000-item feed in memory (`synthetic_feed`) and
asserts the whole `reachable` path finishes under five seconds. That test exists because
the tier diffs were `it not in split[...]` — list membership by dict equality, quadratic:
6,000 items took 0.68s, now 0.02s.

`fixtures/edge-feed.json` is the adversarial one: unpriced, NaN price, naive deadline,
`requires` as a list and as a comma string, expired, a dead source, a source whose
`paid_count` is the string `"0"`, a task-level zero-paid record, a crowded contest, an
entry with no id, and a null. Nine of the twelve assertions in `TestEdgeFeed` exist
because that file found a bug.

`fixtures/earn-now.sample.json` is a real captured `/earn-now` response from
2026-10-30, kept so the parser and the table renderer can be exercised offline.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `HTTP 402` from `/v1/bounties` | That endpoint is paid: 0.01 USDC on Base via x402. It returns `PAYMENT-REQUIRED` with the `payTo` address. This client deliberately does not touch it. |
| `Connection timed out` | `*.workers.dev` is blocked on some networks. The block is at SNI level, so changing DNS or `/etc/hosts` will not help — use a proxy, or fall back to `python3 abf.py feed`. |
| `no JSON-RPC response with id=…` | The server switched framing. Print the raw body and check `Content-Type`. |
| Empty table | The board is genuinely empty; `/earn-now` returns `count: 0` rather than an error. |

---

## Notes

- This client is read-only. It never bids, claims, or submits anything on your behalf.
- `earn-now` is a convenience filter, not a contract. Always open the source URL and read
  the terms before doing the work — the feed itself says so.
- The `direct:` entries are funded from an on-chain escrow; the `frantic:` and
  `taskmarket:` entries pay through their own platforms and need their own accounts.

MIT.
