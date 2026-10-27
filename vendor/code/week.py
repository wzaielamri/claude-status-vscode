#!/usr/bin/env python3
"""Your week with Claude, for the widget's Recap tab: how long you spent with
Claude and how long it worked, what the same work would cost at API prices,
where it went, and what would make it use less of your plan.

All of it comes from the chat transcripts Claude Code keeps on this computer
(and on the SSH hosts shown): nothing here starts Claude or calls a model,
and only --currency goes online (for the day's exchange rates). Each run
reads only what the transcripts added since the last one
(~/.cache/talpy-den/week-3.json), and the widget runs it only while its
Recap tab is open, never in its status checks.

The cache keeps what it read for good (the history: Overall goes back to the
first chat it read), also once Claude Code deletes a chat's transcript
(after its cleanupPeriodDays, 30 by default), unless the history is turned
off: then it keeps only what the transcripts still have.

    python3 -I week.py [--span day|week|all] [--back N] [--account ID] [--host HOST]... [--config-dir DIR]... [--currency C] [--pretty]

    --span S      day: one day (back N days); week: 7 days (back N weeks, the
                  default); all: everything kept, a row per 7 days
    --back N      the day or 7 days N before today's
    --history-status | --history on|off | --forget
                  whether the history is kept (and since when), keep it or
                  not, or delete it (the cache starts again from the
                  transcripts Claude still has)
    --account ID  one account's chats (an id of talpy_den.py's groups)
    --host HOST   an SSH host's chats too: remote.py runs this file there
                  (python3 -I - with talpy_den.py loaded first) with
    --data        that host's chats of those days, for the computer asking
    --remote      the JSON after a marker line (login scripts may print first)
    --currency C  amounts in C (EUR, …) at the European Central Bank's rate
                  of the day (fetched at most every few hours, only then)
    --allow KIND VALUE FOLDER   a rule its permission prompts offer, into
                  FOLDER/.claude/settings.local.json; --undo ID takes it back
    --permissions the widget's Permissions tab instead: the permission prompts
                  you answered in the last 7 days on this computer, the rules
                  that would have let them through, and the permissions you
                  gave (every rule and mode that lets chats go without asking)
    --revoke ID   takes one of those permissions out of its file
    --host HOST   (with --permissions) an SSH host's too; --on HOST: an
                  --allow, --undo or --revoke there

What it reads, per turn of a chat: your prompt, Claude's own count of its
work (the turn_duration record Claude Code writes when a turn ends, which
leaves out the time it waited for you), the tool calls you had to allow or
answer (permissionDecision), and each reply's token usage. A chat forked from
another starts with a copy of its entries: those count once.
"""

import difflib
import datetime
import json
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

sys.dont_write_bytecode = True  # no __pycache__ inside the installed widget...
if os.path.isfile(globals().get("__file__") or ""):  # (on an SSH host it comes on stdin, after talpy_den.py)
    if sys.version_info >= (3, 8):  # ...but talpy_den.py compiled in the cache, as poll.py does
        _cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "talpy-den"
        if _cache.is_dir():
            sys.pycache_prefix = str(_cache / "pycache")
            sys.dont_write_bytecode = False
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I leaves the script dir off sys.path
import talpy_den as cs  # noqa: E402

CACHE_FILE = "week-3.json"  # (a new layout gets a new name: an older copy of this file keeps its own)
OLD_CACHE_FILES = ("week-1.json", "week-2.json")
HISTORY_FILE = "history.json"  # {"keep": false} once you turn the history off
CACHE_LOCK = "week.lock"
LOCK_WAIT_S = 30  # another run reading the transcripts: wait for it this long, then use the cache as it is
SPANS = ("day", "week", "all")
BACK_LIMIT = 3650  # (days or weeks back: further than any history)
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
BUCKET_MS = 300_000  # 5 minutes: how Claude's /usage counts the chats running at once
IDLE_MS = 15 * 60_000  # a longer gap between your prompts and Claude's replies: you were away
PROMPT_LEAD_MS = 60_000  # a prompt takes at least this much of your time (writing it)
GAP_MS = 30_000  # a pause in a turn worth noting (waiting for you, a long tool, a limit)
SLEEP_MS = 30 * 60_000  # this long with nothing written while Claude answers: it wasn't working (a sleep, a limit)
JOIN_MS = 60_000  # the timeline joins stretches this close
TOOLS_KEPT = 50  # tool calls a transcript's reading remembers until their results come
GAPS_KEPT = 200
USER_TOOLS = ("AskUserQuestion", "ExitPlanMode")  # tools that wait for your answer
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
FILE_TOOLS = ("Read", "Grep", "Glob", "NotebookRead", "LSP")
SUBAGENT_TOOLS = ("Agent", "Task")
FILES_KEPT = 100  # edited files a chat's hour remembers (to count them)
EDIT_DIFF_LINES = 4000  # an edit bigger than this counts its lines without a diff
COLD_MS = 60 * 60_000  # a reply this long after the chat's last one finds its cache gone (an hour on a plan)
COLD_TOKENS = 20_000  # ...and writing this much of its context again is a cold resume
TOP_PROJECTS, TOP_CHATS, TOP_DAY_CHATS, TOP_TURNS = 6, 5, 8, 5
TREND_WEEKS = 5  # the trend lines: these 7 days and the four weeks before (what the cache keeps)

# What Claude Code's /usage lists as "contributing to your limits usage" (a
# scan of local transcripts like this one), with its thresholds: a request
# with this many input tokens sent without the cache, with this much
# context; a chat that starts with this much context before your prompt; a
# tool result this big (bytes / 2.5); chats with this many subagent
# requests or more than this share of their usage in subagents; this many
# chats in one 5-minute stretch; a chat active in this many clock hours. A
# behavior gets a tip from this share of the week's usage, the largest first.
USAGE_CACHE_MISS = 100_000
USAGE_LONG_CONTEXT = 150_000
USAGE_ALWAYS_ON = 40_000
USAGE_LARGE_RESULT = 10_000
USAGE_SUBAGENT_REQUESTS = 3
USAGE_SUBAGENT_SHARE = 0.5
USAGE_PARALLEL = 4
USAGE_LONG_HOURS = 8
USAGE_TIP_PCT = 10
TIPS_SHOWN = 5
WAIT_TIP_MS = 15 * 60_000  # Claude waited this long for your answers in a week: a tip
LIMIT_TIP = 2  # usage limits hit in a week: a tip
LIMIT_EPISODE_MS = 60 * 60_000  # hits of a usage limit this close (on one account) are one time you hit it

# API list prices (https://platform.claude.com/docs/en/about-claude/pricing),
# in $ per million tokens: input, 5-minute cache write, 1-hour cache write,
# cache read, output. Claude Code prices its own /usage cost the same way.
PRICES_AS_OF = "2026-10-24"
PRICES = {
    "fable-5-1": (10, 12.5, 20, 0.25, 50), "mythos-5-1": (10, 12.5, 20, 0.25, 50),
    "fable-5": (10, 12.5, 20, 1, 50), "mythos-5": (10, 12.5, 20, 1, 50),
    "opus-5-5": (4, 5, 8, 0.2, 20), "opus-5": (5, 6.25, 10, 0.5, 25), "opus-4-8": (5, 6.25, 10, 0.5, 25),
    "opus-4-7": (5, 6.25, 10, 0.5, 25), "opus-4-6": (5, 6.25, 10, 0.5, 25), "opus-4-5": (5, 6.25, 10, 0.5, 25),
    "opus-4-1": (15, 18.75, 30, 1.5, 75), "opus-4": (15, 18.75, 30, 1.5, 75),
    "sonnet-5-5": (2, 2.5, 4, 0.1, 10), "sonnet-5": (2, 2.5, 4, 0.2, 10), "sonnet-4-6": (3, 3.75, 6, 0.3, 15),
    "sonnet-4-5": (3, 3.75, 6, 0.3, 15), "sonnet-4": (3, 3.75, 6, 0.3, 15), "3-7-sonnet": (3, 3.75, 6, 0.3, 15),
    "haiku-5-5": (0.1, 0.125, 0.2, 0.01, 0.5), "haiku-4-5": (1, 1.25, 2, 0.1, 5), "3-5-haiku": (0.8, 1, 1.6, 0.08, 4),
}
LONG_PROMPT = 100_000  # a prompt over this many tokens costs more on these models:
LONG_PRICES = {"haiku-5-5": (0.5, 0.625, 1, 0.05, 2.5)}
FAST_PRICES = {"opus-5-5": (8, 40), "opus-5": (10, 50), "opus-4-8": (10, 50)}  # fast mode: input, output
US_ONLY = 1.1  # inference_geo "us"
WEB_SEARCH = 0.01  # $ per web search
# A model the list doesn't know yet: priced as the newest of its family (marked as a guess)
FAMILY_NOW = (("fable", "fable-5-1"), ("mythos", "mythos-5-1"), ("opus", "opus-5-5"), ("sonnet", "sonnet-5-5"),
              ("haiku", "haiku-5-5"))

# An hour's usage of a chat ("w"): its weight (how much it counts toward the
# plan's limits, the way /usage weighs it), its subagents' weight and
# requests, the weight of each behavior, the requests and the requests of
# two behaviors
W_ALL, W_SUB, N_SUB, W_MISS, W_LONG, W_MAX, W_FLOW, W_ON, W_BIG, N_ALL, N_MISS, N_LONG = range(12)
W_SIZE = 12
F_SUB, F_MISS, F_LONG, F_MAX, F_FLOW = 1, 2, 4, 8, 16
USAGE_KEYS = ("input", "output", "cacheRead", "cacheWrite5m", "cacheWrite1h", "webSearches")
TOKEN_KINDS = 5  # (the first of them are tokens: input, output, cache reads and writes)


# ---------------------------------------------------------------- prices


def price_model(model):
    """claude-opus-5-5 -> ("opus-5-5", False); a model the list doesn't know:
    the newest of its family and True (a guess); not a Claude model: (None, True)."""
    m = (model or "").lower().split("[")[0]
    m = re.sub(r"-\d{8}$", "", cs.strip_prefix(m, "claude-"))
    if m in PRICES:
        return m, False
    for family, key in FAMILY_NOW:
        if family in m:
            return key, True
    return None, True


def model_tier(model):
    m = (model or "").lower()
    return 10 if "fable" in m else 5 if "opus" in m else 1 if "haiku" in m else 3


def usage_weight(model, inp, out, read, write):
    """How much a request counts toward a plan's limits, as Claude Code's
    /usage weighs it: its tokens by kind (a cache read 1, input 10, a cache
    write 12.5, output 50) times its model's tier."""
    return (read + inp * 10 + write * 12.5 + out * 50) * model_tier(model)


def price_key(model, usage, prompt):
    """The model and what changes its price: fast mode (f), US-only inference
    (u), a long prompt (l)."""
    flags = ("f" if usage.get("speed") == "fast" else "") + ("u" if usage.get("inference_geo") == "us" else "")
    flags += "l" if prompt > LONG_PROMPT else ""
    return model + ("|" + flags if flags else "")


def prices_of(key):
    """(the prices of a price key, a guess) or (None, True)."""
    model, _, flags = key.partition("|")
    name, guessed = price_model(model)
    if name is None:
        return None, True
    p = LONG_PRICES.get(name, PRICES[name]) if "l" in flags else PRICES[name]
    if "f" in flags and name in FAST_PRICES:  # (the cache multipliers apply on top)
        fast_in, fast_out = FAST_PRICES[name]
        k = fast_in / p[0]
        p = (fast_in, p[1] * k, p[2] * k, p[3] * k, fast_out)
    return p, guessed


def request_cost(key, t):
    """$ of tokens t ([input, output, cache read, 5-minute write, 1-hour write,
    web searches, requests]) at the API price of key."""
    p, _ = prices_of(key)
    if p is None:
        return 0.0
    cost = (t[0] * p[0] + t[3] * p[1] + t[4] * p[2] + t[2] * p[3] + t[1] * p[4]) / 1e6
    if "u" in key.partition("|")[2]:
        cost *= US_ONLY
    return cost + t[5] * WEB_SEARCH


def cache_saving(key, t):
    """What the cache reads of t saved: the same tokens as plain input."""
    p, _ = prices_of(key)
    return t[2] * (p[0] - p[3]) / 1e6 if p else 0.0


# ---------------------------------------------------------------- reading the transcripts


def transcript_kind(parts):
    """(chat id, subagent, workflow agent) of the transcript at
    projects/<parts...>, or None: <project>/<chat>.jsonl, a subagent's
    <project>/<chat>/subagents/agent-*.jsonl, a workflow's agents in
    <project>/<chat>/subagents/workflows/<run>/agent-*.jsonl."""
    if len(parts) == 2 and parts[1].endswith(".jsonl"):
        return parts[1][:-6], False, False
    if len(parts) >= 4 and parts[2] == "subagents" and parts[-1].startswith("agent-") and parts[-1].endswith(".jsonl"):
        return parts[1], True, len(parts) > 4 and parts[3] == "workflows"
    return None


def found_transcripts(profiles):
    """{path: {"profiles", "st", "kind", "aliases"}}: every transcript of the
    profiles once (a chat shared by hard links is one file under each
    profile: the profiles holding it, its other paths)."""
    out, by_inode = {}, {}
    for p in profiles:
        root = p.real / "projects"
        for path, _linked in cs.transcript_files(p):
            try:
                kind = transcript_kind(path.relative_to(root).parts)
                st = path.stat()
            except (ValueError, OSError):
                continue
            if kind is None:
                continue
            key, ident = str(path), (st.st_dev, st.st_ino)
            first = by_inode.get(ident)
            if first is None:
                by_inode[ident] = key
                out[key] = {"profiles": [p.key], "st": st, "kind": kind, "aliases": []}
            else:
                f = out[first]
                if p.key not in f["profiles"]:
                    f["profiles"].append(p.key)
                f["aliases"].append(key)
    return out


def new_record(kind, st):
    chat, sub, flow = kind
    return {"ino": st.st_ino, "offset": 0, "chat": chat, "sub": sub, "flow": flow,
            "state": {"turn": None, "timed": False, "tools": {}, "msg": None,
                      "ctx": {"last": None, "ctx": 0, "carried": 0, "first": 0, "start": None, "pending": 0,
                              "large": 0}},
            "data": {"turns": [], "prompts": [], "limits": [], "hours": {}, "names": {}, "folder": "",
                     "prompt": "", "startTokens": None}}


def reply_id(mid):
    """A reply's id as the cache keeps it (its random end)."""
    return mid[-12:]


def scan(profiles, cache, now_ms, keep=True):
    """Read what the profiles' transcripts added since the last run into the
    cache (by path: {"ino", "offset", "chat", "sub", "state", "data"}).

    A reply can be in several transcripts: a forked chat starts with a copy
    of its original, a chat continued in another folder gets the same entries
    in both for a while, a forked subagent starts with a copy of the call that
    started it. Each reply counts once, in the first transcript read that has
    it (the ids of the counted ones are kept with their hour).

    keep: a transcript Claude has deleted keeps its record ("gone"), for
    the history; else it goes with it."""
    old = cache.get("files") if isinstance(cache.get("files"), dict) else {}
    found = found_transcripts(profiles)
    for key in list(found):  # a file reached through another of its paths now keeps its record
        f = found[key]
        if not (old.get(key) or {}).get("ino") == f["st"].st_ino:
            alias = next((a for a in f["aliases"] if (old.get(a) or {}).get("ino") == f["st"].st_ino), None)
            if alias:
                found[alias] = dict(f, aliases=[a for a in f["aliases"] if a != alias] + [key])
                del found[key]
    files = {}
    for key in sorted(found):
        f, st = found[key], found[key]["st"]
        rec = old.get(key)
        if (not isinstance(rec, dict) or rec.get("ino") != st.st_ino or rec.get("offset", 0) > st.st_size
                or "state" not in rec):
            rec = new_record(f["kind"], st)
        rec["profiles"] = f["profiles"]
        rec.pop("gone", None)
        files[key] = rec
    if keep:
        for key, rec in old.items():
            if key not in files and isinstance(rec, dict) and isinstance(rec.get("data"), dict):
                rec["gone"] = True  # (its transcript was deleted: what was read stays)
                if isinstance(rec.get("state"), dict):
                    rec["state"]["turn"] = None  # (no turn goes on in it any more)
                files[key] = rec
    counted = {i for rec in files.values() for agg in rec["data"]["hours"].values() for i in agg.get("m", [])}
    for key in sorted(found):
        rec = files[key]
        if rec["offset"] < found[key]["st"].st_size:
            read_transcript(Path(key), rec, counted)
    cache["files"] = files
    cache["scannedAt"] = now_ms
    return cache


def read_transcript(path, rec, counted):
    try:
        with open(path, "rb") as f:
            f.seek(rec["offset"])
            for line in f:
                if not line.endswith(b"\n"):
                    break  # never a half-written last line
                rec["offset"] += len(line)
                try:
                    take_line(line, rec, counted)
                except (KeyError, TypeError, ValueError, AttributeError, IndexError):
                    continue  # an entry of a shape this doesn't know
    except OSError:
        pass


TITLE_FIELDS = dict(cs.TITLE_KINDS)
TITLE_LINES = tuple(('"type":"%s"' % k).encode() for k in TITLE_FIELDS)


def take_line(line, rec, counted):
    state = rec["state"]
    if line.startswith(b'{"type":"api-request'):  # (Claude Code writes its JSON without spaces)
        return
    if b'"type":"assistant"' in line:
        kind = "assistant"
    elif b'"type":"user"' in line:
        kind = "user"
    elif b'"subtype":"turn_duration"' in line or b'"subtype":"compact_boundary"' in line:
        kind = "system"
    elif not rec["sub"] and any(t in line for t in TITLE_LINES):
        kind = "title"
    else:
        if state["ctx"]["last"] is None and b'"attachment":{"type":"file"' in line:
            state["ctx"]["first"] += len(line)  # (what /usage counts as your first prompt)
        return
    e = json.loads(line)
    if not isinstance(e, dict):
        return
    data = rec["data"]
    if not rec["sub"] and not data["folder"] and isinstance(e.get("cwd"), str) and e["cwd"].startswith("/"):
        data["folder"] = e["cwd"]  # (the folder it started in: Claude may cd elsewhere)
    if kind == "title":
        field = TITLE_FIELDS.get(e.get("type"))
        if field and isinstance(e.get(field), str) and e[field].strip():
            rec["data"]["names"][e["type"]] = cs.clean_text(e[field], 80)
        return
    ts = cs.to_ms(e.get("timestamp"))
    if ts is None or e.get("type") not in ("assistant", "user", "system"):
        return
    if e["type"] == "assistant":
        take_reply(e, ts, rec, counted)
    elif e["type"] == "user":
        take_user(e, ts, rec, line)
    elif e.get("subtype") == "compact_boundary":
        hour_agg(data, ts)[1]["k"] += 1
    elif e.get("subtype") == "turn_duration" and not rec["sub"]:
        worked = cs.number(e.get("durationMs"))
        if worked is not None and worked >= 0:
            state["timed"] = True
            if state["turn"] is None:  # (its prompt came before what was read: a fork's copy, a bg session's start)
                state["turn"] = {"start": ts - int(worked), "last": ts, "gaps": []}
            close_turn(state, data, ts, int(worked))


def note_gap(turn, start, end, kind):
    if end - start > GAP_MS:
        turn["gaps"].append([start, end, kind])
        if len(turn["gaps"]) > GAPS_KEPT:
            turn["gaps"].sort(key=lambda g: g[0] - g[1])
            del turn["gaps"][GAPS_KEPT:]
            turn["gaps"].sort()


def turn_activity(state, ts, kind="o"):
    """Something written during a turn: the stretch since the last entry is
    Claude answering (o), a tool running (t), a prompt you sent meanwhile (q)."""
    turn = state["turn"]
    if turn is None:
        return
    note_gap(turn, turn["last"], ts, kind)
    turn["last"] = max(turn["last"], ts)


def close_turn(state, data, end, worked=None):
    """Note the turn going on as ended at end. worked: Claude's own count of
    its work (turn_duration); without it, the whole turn but the waits for you.
    What a turn's wall time has beyond its work goes to where it can have
    been: waiting for your answers (y), a prompt you sent while it worked (q),
    then the longest pauses (o); a stretch of SLEEP_MS with nothing written
    is never work. waits: [[from, to, kind]], y = waiting for you, p = paused."""
    turn = state["turn"]
    state["turn"] = None
    state["tools"] = {}
    if turn is None:
        return
    start = turn["start"]
    end = max(end, start)
    wall = end - start
    gaps = [g for g in turn["gaps"] if start <= g[0] < g[1] <= end]
    asleep = [g for g in gaps if g[2] == "o" and g[1] - g[0] >= SLEEP_MS]
    waits = [[f, t, "p"] for f, t, _ in asleep]
    paused = sum(t - f for f, t, _ in asleep)
    if worked is None:
        waits += [[f, t, "y"] for f, t, k in gaps if k == "y"]
        worked = max(0, wall - covered(waits))
    else:
        worked = max(0, min(worked, wall - paused))
        left = wall - paused - worked
        for kind in ("y", "q", "o"):
            if left <= GAP_MS:
                break
            parts = sorted((g for g in gaps if g[2] == kind and (kind != "o" or g[1] - g[0] < SLEEP_MS)),
                           key=lambda g: g[0] - g[1])
            total = sum(t - f for f, t, _ in parts)
            if total <= 0:
                continue
            share = min(1.0, left / total) if kind == "y" else 1.0  # your answers share it, pauses the longest first
            for f, t, _ in parts:
                take = min(int((t - f) * share), left)
                if take <= 0:
                    break
                waits.append([f, f + take, "y" if kind == "y" else "p"])
                left -= take
    waits = sorted(w for w in waits if w[1] - w[0] >= 1000)
    if end - start > 0 or worked > 0:
        data["turns"].append([start, end, int(worked), waits, turn.get("t") or {}])


def tool_detail(name, inp):
    """What a tool call was about: the file, folder, page or command."""
    if not isinstance(inp, dict):
        return ""
    for key in ("file_path", "notebook_path", "path", "url", "command"):
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().split("\n")[0][:300] if key == "command" else value[:400]
    return ""


def edit_lines(name, inp):
    """(lines added, lines removed) by an edit tool call: what it replaced
    against what it wrote (a file written whole counts as added)."""
    if not isinstance(inp, dict):
        return 0, 0
    if name == "Edit":
        pairs = [(inp.get("old_string"), inp.get("new_string"))]
    elif name == "MultiEdit":
        pairs = [(e.get("old_string"), e.get("new_string")) for e in inp.get("edits") or [] if isinstance(e, dict)]
    else:
        pairs = [("", inp.get("content") if name == "Write" else inp.get("new_source"))]
    added = removed = 0
    for old, new in pairs:
        if not isinstance(old, str) or not isinstance(new, str):
            continue
        a, b = old.splitlines(), new.splitlines()
        if len(a) + len(b) > EDIT_DIFF_LINES:
            added, removed = added + len(b), removed + len(a)
            continue
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag in ("replace", "delete"):
                removed += i2 - i1
            if tag in ("replace", "insert"):
                added += j2 - j1
    return added, removed


def hour_agg(data, ts):
    h = str(ts // HOUR_MS)
    agg = data["hours"].get(h)
    if agg is None:
        agg = data["hours"][h] = new_hour()
    return h, agg


def new_hour():
    """A chat's hour: tokens by price key (t), weights (w), 5-minute buckets
    (b), attributions (a), large results (g), reply ids (m), tool calls by
    name (u), lines added and removed by edits (x), edited files (fe),
    compactions (k), cold resumes (c: [ms, price key, 5-minute writes,
    1-hour writes]) and the permission prompts you answered (p: [ms, tool,
    what, reason, waited ms, allowed])."""
    return {"t": {}, "w": [0] * W_SIZE, "b": {}, "a": {}, "g": {}, "m": [], "u": {}, "x": [0, 0], "fe": [], "k": 0,
            "c": [], "p": []}


def usage_values(u):
    """[input, output, cache read, 5-minute write, 1-hour write, web searches] of a reply's usage."""
    num = lambda v: int(cs.number(v) or 0)  # noqa: E731
    write = num(u.get("cache_creation_input_tokens"))
    split = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    hour = min(write, num(split.get("ephemeral_1h_input_tokens")))
    tools = u.get("server_tool_use") if isinstance(u.get("server_tool_use"), dict) else {}
    return [num(u.get("input_tokens")), num(u.get("output_tokens")), num(u.get("cache_read_input_tokens")),
            write - hour, hour, num(tools.get("web_search_requests"))]


def track_context(c, mid, inp, out, read, write, sub):
    """Claude Code's /usage reading of a transcript: how much of a request's
    context the chat had from its start (CLAUDE.md, tools, skills: on for
    every request) and how much is large tool results still in it ->
    (always-on tokens, large-result tokens)."""
    o = read + write + inp
    if mid == c["last"]:
        c["carried"] = max(c["carried"], o + out)
    else:
        if c["last"] is None:
            c["start"] = o - round(c["first"] / 2.5)
        elif o < c["ctx"]:  # (compacted)
            c["large"] = max(0, c["large"] - (c["ctx"] - o))
        else:
            added = min(o - c["carried"], c["pending"] / 2.5)
            if added > USAGE_LARGE_RESULT:
                c["large"] += added
        c["start"] = min(c["start"], o)
        c["last"], c["ctx"], c["carried"], c["pending"] = mid, o, o + out, 0
    on = c["start"] if not sub and c["start"] > USAGE_ALWAYS_ON else 0
    return on, int(c["large"])


def take_reply(e, ts, rec, counted):
    state, data = rec["state"], rec["data"]
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    blocks = [c for c in msg.get("content") or [] if isinstance(c, dict)] if isinstance(msg.get("content"), list) else []
    if not rec["sub"]:
        if e.get("isApiErrorMessage") is True:
            text = " ".join(cs.message_text(msg).split())
            if text.startswith(cs.LIMIT_STARTS) or text.startswith(cs.OUT_OF_USAGE):
                data["limits"].append(ts)
        turn_activity(state, ts)
    tools = state["tools"]
    uses = [c for c in blocks if c.get("type") == "tool_use" and isinstance(c.get("id"), str) and isinstance(c.get("name"), str)]
    for c in uses:
        tools[c["id"]] = [ts, c["name"], tool_detail(c["name"], c.get("input"))]
    if len(tools) > TOOLS_KEPT:
        for k in sorted(tools, key=lambda k: tools[k][0])[:len(tools) - TOOLS_KEPT]:
            del tools[k]
    usage, model = msg.get("usage"), msg.get("model")
    if not isinstance(usage, dict) or not isinstance(model, str) or not model or model.startswith("<"):
        return
    vals = usage_values(usage)
    inp, out, read, write = vals[0], vals[1], vals[2], vals[3] + vals[4]
    if not (inp or out or read or write):
        return
    mid = msg.get("id") or e.get("requestId") or e.get("uuid")
    sub = rec["sub"] or e.get("isSidechain") is True
    last = state["msg"]
    if last and last[0] == mid:  # another block of the same reply: its usage again, maybe grown
        track_context(state["ctx"], mid, inp, out, read, write, sub)
        if last[1] is None:
            return  # (a reply counted in another transcript)
        count_tools(data, last[1], uses)
        delta = [max(0, a - b) for a, b in zip(vals, last[3])]
        if any(delta) and not rec["sub"]:
            turn_tokens(state, last[2], delta)
        if any(delta):
            agg = rec["data"]["hours"].get(last[1])
            if agg is not None:
                t = agg["t"].setdefault(last[2], [0] * 7)
                for i, v in enumerate(delta):
                    t[i] += v
                w = usage_weight(model, delta[0], delta[1], delta[2], delta[3] + delta[4])
                flags = last[4]
                agg["w"][W_ALL] += w
                for flag, i in ((F_SUB, W_SUB), (F_MISS, W_MISS), (F_LONG, W_LONG), (F_MAX, W_MAX), (F_FLOW, W_FLOW)):
                    if flags & flag:
                        agg["w"][i] += w
                agg["b"][last[5]] = agg["b"].get(last[5], 0) + w
            last[3] = [max(a, b) for a, b in zip(vals, last[3])]
        return
    on, large = track_context(state["ctx"], mid, inp, out, read, write, sub)
    if not isinstance(mid, str) or reply_id(mid) in counted:  # counted in another transcript
        state["msg"] = [mid, None]
        return
    counted.add(reply_id(mid))
    if not sub:
        start = state["ctx"]["start"]
        data["startTokens"] = start if data["startTokens"] is None else min(data["startTokens"], start)
    prompt = inp + read + write
    key = price_key(model, usage, prompt)
    h, agg = hour_agg(data, ts)
    agg["m"].append(reply_id(mid))
    t = agg["t"].setdefault(key, [0] * 7)
    for i, v in enumerate(vals):
        t[i] += v
    t[6] += 1
    w = usage_weight(model, inp, out, read, write)
    input_w = w - out * 50 * model_tier(model)
    flags = ((F_SUB if sub else 0) | (F_MISS if inp > USAGE_CACHE_MISS else 0)
             | (F_LONG if prompt > USAGE_LONG_CONTEXT else 0) | (F_MAX if e.get("effort") == "max" else 0)
             | (F_FLOW if rec["flow"] else 0))
    a = agg["w"]
    a[W_ALL] += w
    a[N_ALL] += 1
    if flags & F_SUB:
        a[W_SUB] += w
        a[N_SUB] += 1
    if flags & F_MISS:
        a[W_MISS] += w
        a[N_MISS] += 1
    if flags & F_LONG:
        a[W_LONG] += w
        a[N_LONG] += 1
    if flags & F_MAX:
        a[W_MAX] += w
    if flags & F_FLOW:
        a[W_FLOW] += w
    if on and prompt:
        a[W_ON] += input_w * min(1.0, on / prompt)
    if large and prompt:
        a[W_BIG] += input_w * min(1.0, large / prompt)
    bucket = str((ts // BUCKET_MS) % (HOUR_MS // BUCKET_MS))
    agg["b"][bucket] = agg["b"].get(bucket, 0) + w
    count_tools(data, h, uses)
    if not rec["sub"]:
        turn_tokens(state, key, vals)
        # Back after an hour or more: the cache let the chat's context go, and
        # the reply writes it all again (a cold resume)
        if state.get("lastReq") and ts - state["lastReq"] > COLD_MS and vals[3] + vals[4] >= COLD_TOKENS:
            agg["c"].append([ts, key, vals[3], vals[4]])
        state["lastReq"] = ts
    for field, prefix in (("attributionPlugin", "plugin:"), ("attributionMcpServer", "mcp:")):
        if isinstance(e.get(field), str) and 0 < len(e[field]) <= 64:
            agg["a"][prefix + e[field]] = agg["a"].get(prefix + e[field], 0) + w
    agent, skill = e.get("attributionAgent"), e.get("attributionSkill")
    name = (skill or agent) if isinstance(agent, str) and agent else skill
    if isinstance(name, str) and 0 < len(name) <= 64:
        k = ("agent:" if isinstance(agent, str) and agent else "skill:") + name
        agg["a"][k] = agg["a"].get(k, 0) + w
    state["msg"] = [mid, h, key, vals, flags, bucket]


def count_tools(data, h, uses):
    """The tool calls of a reply's block, in its hour: by name, and what the
    edits changed (lines, files)."""
    agg = data["hours"].get(h)
    if agg is None:
        return
    for c in uses:
        name = c["name"]
        agg["u"][name] = agg["u"].get(name, 0) + 1
        if name in EDIT_TOOLS:
            added, removed = edit_lines(name, c.get("input"))
            agg["x"][0] += added
            agg["x"][1] += removed
            path = tool_detail(name, c.get("input"))
            if path and path not in agg["fe"] and len(agg["fe"]) < FILES_KEPT:
                agg["fe"].append(path)


def turn_tokens(state, key, vals):
    """A reply's tokens on the turn going on: its cost, once it ends."""
    turn = state["turn"]
    if turn is None:
        return
    t = turn.setdefault("t", {}).setdefault(key, [0] * 6)
    for i, v in enumerate(vals[:6]):
        t[i] += v


RESULT_FIELD = b'"type":"tool_result"'


def take_user(e, ts, rec, line):
    state, data = rec["state"], rec["data"]
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    content = msg.get("content")
    results = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_result"] if isinstance(content, list) else []
    main = not rec["sub"]
    if results:
        at = line.find(RESULT_FIELD)
        end = line.find(b'"toolUseResult":', at)
        size = (end if end > at else len(line)) - at  # (as /usage measures it)
        large = size / 2.5 > USAGE_LARGE_RESULT
        if large:
            state["ctx"]["pending"] += size
        pd = e.get("permissionDecision") if isinstance(e.get("permissionDecision"), dict) else {}
        yours = isinstance(pd.get("source"), str) and pd["source"].startswith("user")
        asked = None
        for r in results:
            tool = state["tools"].pop(r.get("tool_use_id"), None) if isinstance(r.get("tool_use_id"), str) else None
            if tool is None:
                continue
            if yours and tool[1] not in USER_TOOLS:  # a permission prompt you answered
                reason = pd.get("reasonCode") if isinstance(pd.get("reasonCode"), str) else ""
                hour_agg(data, ts)[1]["p"].append([ts, tool[1], tool[2], reason, max(0, ts - tool[0]),
                                                   1 if pd.get("decision") == "accept" else 0])
            if large:
                _, agg = hour_agg(data, ts)
                k = tool[1] + "|" + tool[2]
                g = agg["g"].setdefault(k, [0, 0])
                g[0] += 1
                g[1] = max(g[1], int(size / 2.5))
            if yours or tool[1] in USER_TOOLS:
                asked = tool[0] if asked is None else min(asked, tool[0])
        turn = state["turn"]
        if main and turn is not None:
            if asked is not None and ts - asked > 1000:  # Claude waited for you to allow it, or answer
                note_gap(turn, max(asked, turn["start"]), ts, "y")
                turn["last"] = max(turn["last"], ts)
            else:
                turn_activity(state, ts, "t")
        return
    if state["ctx"]["last"] is None:
        state["ctx"]["first"] += len(line)
    if not main:
        return
    text = cs.human_prompt(e)
    turn = state["turn"]
    if text is None:
        if cs.message_text(msg).lstrip().startswith("[Request interrupted"):
            if turn is not None:
                turn["stopped"] = ts  # (a turn_duration may still come)
                turn_activity(state, ts)
        else:
            turn_activity(state, ts)
        return
    data["prompts"].append(ts)
    if not data["prompt"]:
        data["prompt"] = cs.clean_text(text, 80)
    if turn is not None:
        # Sent while Claude works (it waits in Claude's queue), unless the
        # turn ended without a record of it (stopped, or a version that
        # doesn't write them): then it starts a new turn
        if state["timed"] and "stopped" not in turn and ts - turn["last"] <= IDLE_MS:
            turn_activity(state, ts, "q")
            return
        close_turn(state, data, turn.get("stopped", turn["last"]))
    state["turn"] = {"start": ts, "last": ts, "gaps": []}


# ---------------------------------------------------------------- the cache


def cache_path(cache_root):
    return cache_root / CACHE_FILE


def load_cache(cache_root):
    for name in OLD_CACHE_FILES:  # (read again into the new layout)
        try:
            (cache_root / name).unlink()
        except OSError:
            pass
    data = cs.read_json(cache_path(cache_root))
    return data if isinstance(data, dict) and isinstance(data.get("files"), dict) else {"files": {}}


class cache_lock:
    """Holds the cache's lock (another run may be reading the transcripts:
    another widget, the VS Code extension); .held is False when it didn't
    come within LOCK_WAIT_S."""

    def __init__(self, cache_root):
        self.cache_root = cache_root

    def __enter__(self):
        import fcntl
        cs.private_dir(self.cache_root)
        self.fd = os.open(str(self.cache_root / CACHE_LOCK), os.O_CREAT | os.O_RDWR, 0o600)
        self.held, waited = False, 0.0
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.held = True
                return self
            except OSError:
                if waited >= LOCK_WAIT_S:
                    return self
                time.sleep(0.2)
                waited += 0.2

    def __exit__(self, *exc):
        os.close(self.fd)


def updated_cache(cache_root, profiles, now_ms):
    """The cache with what the transcripts added since; while another run is
    reading them, the cache it writes."""
    with cache_lock(cache_root) as lock:
        cache = load_cache(cache_root)
        if lock.held:
            scan(profiles, cache, now_ms, keep_history(cache_root))
            try:
                cs.write_json_atomic(cache_path(cache_root), cache)
            except OSError:
                pass
        return cache


# ---------------------------------------------------------------- the history
# What the cache read stays after Claude Code deletes a chat's transcript,
# so Overall keeps growing; off, only what the transcripts still have is
# kept. One choice for every copy of this file (the widgets, the VS Code
# extension): it's a file next to the cache.


def keep_history(cache_root):
    data = cs.read_json(cache_root / HISTORY_FILE)
    return not (isinstance(data, dict) and data.get("keep") is False)


def first_activity(cache):
    """When the first chat the cache has began (ms), or None."""
    first = None
    for rec in cache.get("files", {}).values():
        data = rec.get("data") if isinstance(rec, dict) else None
        if not isinstance(data, dict):
            continue
        times = [t[0] for t in data.get("turns", [])[:1]] + data.get("prompts", [])[:1] + [
            int(h) * HOUR_MS for h in data.get("hours", {})]
        if times:
            first = min(times) if first is None else min(first, min(times))
    return first


def history_status(cache_root):
    cache = load_cache(cache_root)
    first = first_activity(cache)
    recs = [rec for rec in cache["files"].values() if isinstance(rec, dict)]
    # (a chat Claude moved to another folder isn't deleted: its transcript is there)
    gone = {r.get("chat") for r in recs if r.get("gone")} - {r.get("chat") for r in recs if not r.get("gone")}
    try:
        size = cache_path(cache_root).stat().st_size
    except OSError:
        size = 0
    return {"ok": True, "keep": keep_history(cache_root),
            "since": date.fromtimestamp(first / 1000).isoformat() if first else None,
            "deletedChats": len(gone), "sizeKB": size // 1024}


def set_history(cache_root, keep):
    """(ok, message): keep the history or not; off, what only it had goes now."""
    with cache_lock(cache_root) as lock:
        if not lock.held:
            return False, "The Recap is reading your chats: try again in a moment"
        cs.write_json_atomic(cache_root / HISTORY_FILE, {"keep": bool(keep)})
        if keep:
            return True, "The Recap keeps its history"
        cache = load_cache(cache_root)
        files = cache["files"]
        gone = [k for k, rec in files.items() if isinstance(rec, dict) and rec.get("gone")]
        for k in gone:
            del files[k]
        if gone:
            cs.write_json_atomic(cache_path(cache_root), cache)
        return True, "The Recap keeps only what Claude still has" + (
            " (it forgot %d deleted transcripts)" % len(gone) if gone else "")


def forget_history(cache_root):
    """(ok, message): the cache deleted; it starts again from the transcripts
    Claude still has."""
    with cache_lock(cache_root) as lock:
        if not lock.held:
            return False, "The Recap is reading your chats: try again in a moment"
        for name in (CACHE_FILE,) + OLD_CACHE_FILES:
            try:
                (cache_root / name).unlink()
            except OSError:
                pass
        return True, "The saved history is deleted: the Recap starts again from the chats Claude still has"


# ---------------------------------------------------------------- one machine's chats of some days


def merge_hour(into, agg):
    for key, t in agg.get("t", {}).items():
        mine = into["t"].setdefault(key, [0] * 7)
        for i, v in enumerate(t[:7]):
            mine[i] += v
    for name, n in agg.get("u", {}).items():
        into["u"][name] = into["u"].get(name, 0) + n
    for i, v in enumerate(agg.get("x", [])[:2]):
        into["x"][i] += v
    into["fe"] += [f for f in agg.get("fe", []) if f not in into["fe"]]
    into["k"] += agg.get("k", 0)
    into["c"] += agg.get("c", [])
    into["p"] += agg.get("p", [])
    for i, v in enumerate(agg.get("w", [])[:W_SIZE]):
        into["w"][i] += v
    for field in ("b", "a"):
        for k, v in agg.get(field, {}).items():
            into[field][k] = into[field].get(k, 0) + v
    for k, (n, size) in agg.get("g", {}).items():
        g = into["g"].setdefault(k, [0, 0])
        g[0] += n
        g[1] = max(g[1], size)


def live_turn(state):
    """The turn going on now, as far as it went: [start, last, work, waits]."""
    turn = state.get("turn") if isinstance(state, dict) else None
    if not isinstance(turn, dict):
        return None
    start, last = turn["start"], turn.get("stopped", turn["last"])
    waits = [[f, t, "y"] for f, t, k in turn.get("gaps", []) if k == "y" and t <= last]
    waits += [[f, t, "p"] for f, t, k in turn.get("gaps", []) if k == "o" and t - f >= SLEEP_MS and t <= last]
    return [start, last, max(0, last - start - covered(waits)), sorted(waits), turn.get("t") or {}]


def account_ids(profiles):
    """{profile key: account id}, [{"id", "name"}] (talpy_den.py's groups)."""
    by_profile, accounts = {}, []
    for gid, members in cs.group_profiles(profiles):
        named = next((p for p in members if not p.is_default), members[0])
        aid = cs.short_id(gid)
        accounts.append({"id": aid, "name": named.label, "plan": next((p.plan for p in members if p.plan), None)})
        for p in members:
            by_profile[p.key] = aid
    return by_profile, accounts


def project_of(folder, home, roots):
    """The project a chat's folder belongs to: its git repository's folder
    (a chat started in a subfolder counts for it), else the folder itself."""
    if folder not in roots:
        try:
            root = cs.git_root(Path(folder)) if folder.startswith("/") else None
        except OSError:
            root = None
        roots[folder] = str(root) if root is not None and root != home and root.parent != root else folder
    return roots[folder]


def chats_between(cache, profiles, since, until, home):
    """{chat id: {"profiles", "accounts", "folder", "project", "name",
    "turns", "prompts", "limits", "hours", "startTokens"}}: what the cache has
    of the profiles' chats between since and until (ms), with the turns going on."""
    by_profile, _ = account_ids(profiles)
    roots = {}
    first_hour, last_hour = since // HOUR_MS, until // HOUR_MS
    chats = {}
    for rec in cache.get("files", {}).values():
        data = rec.get("data") if isinstance(rec, dict) else None
        if not isinstance(data, dict):
            continue
        c = chats.get(rec["chat"])
        if c is None:
            c = chats[rec["chat"]] = {"profiles": [], "folder": "", "name": "", "turns": [], "prompts": [],
                                      "limits": [], "hours": {}, "startTokens": None}
        for p in rec.get("profiles", []):
            if p not in c["profiles"]:
                c["profiles"].append(p)
        for h, agg in data["hours"].items():
            if first_hour <= int(h) <= last_hour:
                merge_hour(c["hours"].setdefault(h, {k: v for k, v in new_hour().items() if k != "m"}), agg)
        if rec.get("sub"):
            continue
        turns = list(data["turns"])
        live = live_turn(rec.get("state"))
        if live:
            turns.append(live)
        c["turns"] += [t for t in turns if t[1] >= since and t[0] < until]
        c["prompts"] += [p for p in data["prompts"] if since <= p < until]
        c["limits"] += [p for p in data["limits"] if since <= p < until]
        names = data.get("names") or {}
        c["name"] = next((names[k] for k, _ in cs.TITLE_KINDS if names.get(k)), c["name"] or data.get("prompt") or "")
        c["folder"] = c["folder"] or data.get("folder") or ""
        if data.get("startTokens") is not None:
            c["startTokens"] = data["startTokens"] if c["startTokens"] is None else min(c["startTokens"], data["startTokens"])
    for c in chats.values():
        c["accounts"] = sorted({by_profile[p] for p in c["profiles"] if p in by_profile})
        c["project"] = project_of(c["folder"], home, roots) if c["folder"] else ""
        c["turns"] = one_of_each(c["turns"])
        c["prompts"] = sorted(set(c["prompts"]))
        c["limits"] = sorted(set(c["limits"]))
    return {k: c for k, c in chats.items() if c["turns"] or c["prompts"] or c["hours"]}


def one_of_each(turns):
    """A chat's turns in time order, each once: a chat Claude moved to another
    folder (its transcript went with it) has its old file's record kept as
    history, with the same turns as the new one; the copy that counted the
    tokens is kept (a reply counts once, in the first transcript read)."""
    best = {}
    for t in turns:
        k = (t[0], t[1], t[2])
        if k not in best or (len(t) > 4 and t[4] and not (len(best[k]) > 4 and best[k][4])):
            best[k] = t
    return sorted(best.values(), key=lambda t: (t[0], t[1], t[2]))


# ---------------------------------------------------------------- the recap


def midnight(day):
    """Local midnight starting day (a date), in ms."""
    return int(time.mktime(day.timetuple()) * 1000)


def week_days(back, now_ms):
    """The 7 days ending today, back weeks before: [(day, start ms, end ms)]."""
    return span_days("week", back, now_ms)


def span_days(span, back, now_ms, first_ms=None):
    """The days a span shows: day: the one back days before today; week:
    the 7 ending today, back weeks before; all: from the first one with a
    chat (first_ms) to today. [(day, start ms, end ms)]"""
    today = date.fromtimestamp(now_ms / 1000)
    if span == "day":
        days = [today - timedelta(days=back)]
    elif span == "all":
        first = date.fromtimestamp(first_ms / 1000) if first_ms else today
        days = [first + timedelta(days=i) for i in range(max(1, (today - first).days + 1))]
    else:
        end = today - timedelta(days=7 * back)
        days = [end - timedelta(days=i) for i in range(6, -1, -1)]
    return [(d.isoformat(), midnight(d), midnight(d + timedelta(days=1))) for d in days]


def span_since(span, back, now_ms, first_ms=None):
    """Where a span's chats start to matter: its days, the period before (the
    comparison) and the trend lines' periods."""
    first_day = date.fromisoformat(span_days(span, back, now_ms, first_ms)[0][0])
    if span == "all":
        return 0
    return midnight(first_day - timedelta(days=(1 if span == "day" else 7) * (TREND_WEEKS - 1)))


def back_max(span, now_ms, first_ms):
    """How far back a span can step: to the day or the 7 days with the first chat."""
    if span == "all" or not first_ms:
        return 0
    days = (date.fromtimestamp(now_ms / 1000) - date.fromtimestamp(first_ms / 1000)).days
    return max(0, days if span == "day" else days // 7)


def merged(intervals, join=0):
    """Sorted intervals [[start, end], ...] joined where they overlap (or come within join)."""
    out = []
    for s, e in sorted([i[0], i[1]] for i in intervals if i[1] > i[0]):
        if out and s <= out[-1][1] + join:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def covered(intervals):
    return sum(e - s for s, e in merged(intervals))


def clipped(intervals, start, end):
    return [[max(s, start), min(e, end)] + list(rest) for s, e, *rest in intervals if e > start and s < end]


LANES_MAX = 24


def day_lanes(chats, lo, hi, home):
    """The Day view's lanes: one for each chat that did something in [lo, hi)
    (oldest first), its stretches [from, to, kind]: w = Claude worked, y =
    waited for you (a permission, a question), p = paused (asleep, a usage
    limit); between its turns it was your turn. With how long it worked and
    waited."""
    lanes = []
    for c in chats:
        parts = []
        for turn in c["turns"]:
            s, e, _, gaps = turn[:4]
            if e < lo or s >= hi:
                continue
            for a, b in minus([s, e], gaps):
                parts.append([a, b, "w"])
            parts += [[f, t, kind] for f, t, kind in gaps]
        parts = [[max(a, lo), min(b, hi), k] for a, b, k in parts if b > lo and a < hi and min(b, hi) - max(a, lo) >= 1000]
        if not parts:
            continue
        parts.sort()
        lanes.append({"key": c["key"], "id": c["id"], "host": c["host"], "name": chat_label(c) or c["id"][:8],
                      "profileDir": c["profiles"][0] if c.get("profiles") else "",
                      "project": project_name(c["folder"], home) if c.get("folder") else "",
                      "parts": parts, "workedMs": sum(b - a for a, b, k in parts if k == "w"),
                      "waitedMs": sum(b - a for a, b, k in parts if k == "y")})
    lanes.sort(key=lambda l: l["parts"][0][0])
    if len(lanes) > LANES_MAX:   # (the busiest, in the order they began)
        keep = {id(l) for l in sorted(lanes, key=lambda l: -(l["workedMs"] + l["waitedMs"]))[:LANES_MAX]}
        lanes = [l for l in lanes if id(l) in keep]
    return lanes


def minus(span, holes):
    """span [start, end] without the holes: the stretches left."""
    out, at = [], span[0]
    for s, e in merged([h[:2] for h in holes]):
        if e <= at or s >= span[1]:
            continue
        if s > at:
            out.append([at, min(s, span[1])])
        at = max(at, e)
    if at < span[1]:
        out.append([at, span[1]])
    return out


def at_once(stretches):
    """Stretches [[start, end, chat], ...] of several chats -> [[start, end,
    how many at once]], where at least one goes on."""
    points = sorted([(s, 1, c) for s, e, c in stretches] + [(e, -1, c) for s, e, c in stretches])
    out, running, at = [], {}, None
    for t, step, chat in points:
        if running and at is not None and t > at:
            n = len(running)
            if out and out[-1][1] == at and out[-1][2] == n:
                out[-1][1] = t
            else:
                out.append([at, t, n])
        running[chat] = running.get(chat, 0) + step
        if running[chat] <= 0:
            del running[chat]
        at = t
    return out


def your_time(prompts, handovers):
    """When you were at it: from each prompt back to the last thing before it
    and from each thing Claude gave you (a finished turn, a question) on to
    your next prompt or answer, where the gap is under IDLE_MS, and the minute
    you spent writing each prompt. prompts and answers are yours; handovers
    (a turn's end, Claude asking) are Claude's."""
    events = sorted([(t, 1) for t in prompts] + [(t, 0) for t in handovers])
    spans = [[t - PROMPT_LEAD_MS, t] for t, yours in events if yours]
    for (a, a_yours), (b, b_yours) in zip(events, events[1:]):
        if (a_yours or b_yours) and 0 < b - a <= IDLE_MS:
            spans.append([a, b])
    return merged(spans)


def overlap(intervals, start, end):
    return sum(max(0, min(e, end) - max(s, start)) for s, e, *_ in intervals)


def first_time(seen, key):
    if key in seen:
        return False
    seen.add(key)
    return True


def chat_label(chat):
    return chat["name"] or (os.path.basename(chat["folder"].rstrip("/")) if chat["folder"] else "")


def day_of(starts, ms):
    """Index of the day (by their start ms) holding ms, or None."""
    for i in range(len(starts) - 1, -1, -1):
        if ms >= starts[i]:
            return i if i < len(starts) - 1 else None
    return None


def recap(sources, back, account, now_ms, home, usage=None, added=(), span="week", first_ms=None):
    """The Recap tab: sources = [{"host", "chats" (chats_between), "accounts"}];
    usage: this computer's plan windows now (plan_windows); added: the rules
    this tab added (to undo them); span: day, week or all (span_days), with
    first_ms when the first chat began (how far back it goes)."""
    days = span_days(span, back, now_ms, first_ms)
    starts = [d[1] for d in days] + [days[-1][2]]
    w0, w1 = starts[0], min(starts[-1], now_ms)
    first_day = date.fromisoformat(days[0][0])
    step = 1 if span == "day" else 7  # (days a period has: the one before and the trend's)
    p0 = midnight(first_day - timedelta(days=step))
    chats, accounts = [], {}
    for src in sources:
        host = src.get("host") or ""
        for a in src.get("accounts") or []:
            accounts.setdefault(a["id"], dict(a, hosts=[]))["hosts"].append(host)
        for cid, c in (src.get("chats") or {}).items():
            if account and account not in c.get("accounts", []):
                continue
            chats.append(dict(c, id=cid, host=host, key=host + "|" + cid))
    # A turn, prompt or limit two transcripts both have (a fork's copy, a
    # chat written to two folders for a while) counts once, in the first
    chats.sort(key=lambda c: c["key"])
    seen = set()
    for c in chats:
        c["turns"] = [t for t in c["turns"] if first_time(seen, (c["host"], "turn", t[1], t[2]))]
        c["prompts"] = [p for p in c["prompts"] if first_time(seen, (c["host"], "prompt", p))]
        c["limits"] = [p for p in c["limits"] if first_time(seen, (c["host"], "limit", p))]

    def period(lo, hi, full):
        """The numbers of [lo, hi): full = with the timeline and the lists."""
        stretches, waits, handovers, answers, turns, limits = [], [], [], [], 0, []
        worked = {}
        longest = None
        near = lo - IDLE_MS  # (what came just before still joins your time at the start)
        for c in chats:
            for turn in c["turns"]:
                s, e, work, gaps = turn[:4]
                if e < near or s >= hi:
                    continue
                parts = minus([s, e], gaps)
                length = sum(b - a for a, b in parts) or 1
                inside = sum(max(0, min(b, hi) - max(a, lo)) for a, b in parts)
                worked[c["key"]] = worked.get(c["key"], 0) + (work * inside / length if parts else 0)
                stretches += [[a, b, c["key"], work / length] for a, b in parts]
                for f, t, kind in gaps:
                    if kind == "y":
                        waits.append([f, t])
                        handovers.append(f)
                        answers.append(t)
                handovers.append(e)
                if lo <= e < hi:
                    turns += 1
                    if longest is None or work > longest["workedMs"]:
                        longest = {"workedMs": int(work), "startedAt": s, "name": chat_label(c), "host": c["host"]}
            limits += [(",".join(c["accounts"]) or c["key"], t) for t in c["limits"] if lo <= t < hi]
        prompts = [p for c in chats for p in c["prompts"] if near <= p < hi]
        yours = clipped(your_time(prompts + answers, handovers), lo, hi)
        own_prompts = sum(1 for p in prompts if p >= lo)
        cost, tokens = 0.0, dict.fromkeys(USAGE_KEYS, 0)
        for c in chats:
            for h, agg in c["hours"].items():
                if lo <= int(h) * HOUR_MS < hi:
                    for key, t in agg["t"].items():
                        cost += request_cost(key, t)
                        for i, name in enumerate(USAGE_KEYS):
                            tokens[name] += t[i]
        out = {"youMs": int(covered(yours)), "workedMs": int(sum(worked.values())),
               "busyMs": int(covered([s[:2] for s in clipped(stretches, lo, hi)])),
               "waitMs": int(covered(clipped(waits, lo, hi))), "prompts": own_prompts, "cost": round(cost, 4),
               "tokenTotal": sum(tokens[name] for name in USAGE_KEYS[:TOKEN_KINDS])}
        if not full:
            return out
        out.update(turns=turns, limits=limit_episodes(limits), tokens=tokens, longestTurn=longest,
                   chats=sum(1 for c in chats if worked.get(c["key"]) or any(lo <= p < hi for p in c["prompts"])
                             or any(lo <= int(h) * HOUR_MS < hi for h in c["hours"])))
        return out, {"stretches": stretches, "waits": waits, "yours": yours, "worked": worked}

    total, parts = period(w0, w1, True)
    previous = period(p0, w0, False) if span != "all" else None   # (everything has nothing before it)
    running = at_once([s[:3] for s in clipped(parts["stretches"], w0, w1)])
    total["peakAtOnce"] = max((n for _, _, n in running), default=0)

    # The days or weeks before, for the trend lines (oldest first, these
    # last); one before the first chat kept is unknown (null)
    trend = []
    if span != "all":
        for k in range(TREND_WEEKS - 1, 0, -1):
            lo, hi = midnight(first_day - timedelta(days=step * k)), midnight(first_day - timedelta(days=step * (k - 1)))
            trend.append(dict(period(lo, hi, False), start=lo) if first_ms is not None and hi > first_ms else None)
        trend.append({key: total[key] for key in ("youMs", "workedMs", "busyMs", "waitMs", "prompts", "cost", "tokenTotal")})
        trend[-1]["start"] = w0

    # The timeline, a day per row, with each day's chats (to unfold) and the
    # moments a usage limit stopped one; for all, a row per 7 days (newest
    # first, ending today) with their numbers
    hits = sorted({t for c in chats for t in c["limits"] if w0 <= t < w1})
    day_rows, row_days = [], days
    if span == "all":
        row_days = []
        end = date.fromisoformat(days[-1][0])
        while True:
            start = end - timedelta(days=6)
            row_days.append((start.isoformat(), midnight(start), midnight(end + timedelta(days=1))))
            if midnight(start) <= w0:
                break
            end = start - timedelta(days=1)
    for day, ds, row_end in row_days:
        if ds >= now_ms:
            break
        de = min(row_end, now_ms)
        work = [[s, e, n] for s, e, n in clipped(running, ds, de)]
        row = {"day": day, "start": ds, "end": row_end,
               "you": [s[:2] for s in merged(clipped(parts["yours"], ds, de), JOIN_MS)],
               "work": join_running(work),
               # (only while no chat worked: then Claude was idle, waiting for you)
               "wait": [w for span in merged(clipped(parts["waits"], ds, de), JOIN_MS) for w in minus(span, work)],
               "limits": [t for t in hits if ds <= t < de]}
        row["youMs"] = int(overlap(parts["yours"], ds, de))
        row["busyMs"] = int(overlap(work, ds, de))
        row["workedMs"] = int(sum(w * max(0, min(b, de) - max(a, ds)) for a, b, _, w in parts["stretches"]))
        row["waitMs"] = int(covered(clipped(parts["waits"], ds, de)))
        row["prompts"] = sum(1 for c in chats for p in c["prompts"] if ds <= p < de)
        used = [(k, t) for c in chats for h, a in c["hours"].items() if ds <= int(h) * HOUR_MS < de for k, t in a["t"].items()]
        row["cost"] = round(sum(request_cost(k, t) for k, t in used), 4)
        row["tokenTotal"] = sum(sum(t[:TOKEN_KINDS]) for _, t in used)
        row["chats"] = chats_of(chats, parts["stretches"], ds, de, home)
        if span == "all":  # (a row's numbers and chats: no 24 hours to draw)
            row.update(to=(date.fromisoformat(day) + timedelta(days=6)).isoformat(), you=[], work=[], wait=[])
        day_rows.append(row)
    total["busiestDay"] = (max(day_rows, key=lambda r: r["workedMs"])["day"]
                           if span == "week" and any(r["workedMs"] for r in day_rows) else None)

    # Where it went: folders (with their chats), chats, models
    folders, models, guessed, saved = {}, {}, set(), 0.0
    chat_rows = []
    for c in chats:
        cost = 0.0
        for h, agg in c["hours"].items():
            if w0 <= int(h) * HOUR_MS < w1:
                for key, t in agg["t"].items():
                    cost += request_cost(key, t)
                    saved += cache_saving(key, t)
                    model = key.partition("|")[0]
                    label = (cs.model_label(model) or model) + (" (fast)" if "f" in key.partition("|")[2] else "")
                    m = models.setdefault(label, {"model": label, "cost": 0.0, "tokens": 0, "requests": 0})
                    m["cost"] += request_cost(key, t)
                    m["tokens"] += sum(t[:5])
                    m["requests"] += t[6]
                    if prices_of(key)[1]:
                        guessed.add(label)
        work = parts["worked"].get(c["key"], 0)
        prompts = sum(1 for p in c["prompts"] if w0 <= p < w1)
        if work < 60_000 and cost < 0.01:
            continue  # (nothing worth a line)
        folder = c.get("project") or c["folder"] or ""
        fkey = c["host"] + "|" + folder
        f = folders.setdefault(fkey, {"name": project_name(folder, home), "path": cs.short_path(folder, home) if not c["host"] else folder,
                                      "host": c["host"], "workedMs": 0, "cost": 0.0, "prompts": 0, "chats": []})
        row = chat_row(c, home, work, cost, prompts)
        f["workedMs"] += work
        f["cost"] += cost
        f["prompts"] += prompts
        f["chats"].append(row)
        chat_rows.append(row)
    for f in folders.values():
        f["workedMs"] = int(f["workedMs"])
        f["cost"] = round(f["cost"], 4)
        f["chatCount"] = len(f["chats"])
        f["chats"] = sorted(f["chats"], key=lambda r: (-r["workedMs"], -r["cost"]))[:TOP_DAY_CHATS]
    for m in models.values():
        m["cost"] = round(m["cost"], 4)
        m["share"] = round(m["cost"] / total["cost"], 4) if total["cost"] else 0

    return {
        "ok": True,
        "generatedAt": now_ms,
        "span": span,
        "back": back,
        "backMax": back_max(span, now_ms, first_ms),
        "first": date.fromtimestamp(first_ms / 1000).isoformat() if first_ms else None,
        "account": account or "",
        "accounts": sorted(accounts.values(), key=lambda a: a["name"]),
        "from": days[0][0],
        "to": days[-1][0],
        "days": day_rows,
        "sum": total,
        "previous": previous,
        "trend": trend,
        "projects": sorted(folders.values(), key=lambda f: (-f["workedMs"], -f["cost"]))[:TOP_PROJECTS],
        "chats": sorted(chat_rows, key=lambda r: (-r["workedMs"], -r["cost"]))[:TOP_CHATS],
        "models": sorted(models.values(), key=lambda m: -m["cost"]),
        "cacheSaved": round(saved, 2),
        "spend": spending(chats, chat_rows, w0, w1, home),
        "work": claude_did(chats, w0, w1),
        "plan": plan_windows_of(usage or [], account) if back == 0 else [],
        "prompts": permission_prompts(chats, w0, w1, home, added),
        "tips": tips(chats, w0, w1, total, parts["waits"]),
        "prices": {"asOf": PRICES_AS_OF, "guessed": sorted(guessed)},
        "lanes": day_lanes(chats, w0, w1, home) if span == "day" else [],
        "sources": [{"host": s.get("host") or "", "ok": s.get("ok", True), "error": s.get("error") or ""}
                    for s in sources],
    }


def project_name(folder, home):
    return ("~" if cs.short_path(folder, home) == "~" else os.path.basename(folder.rstrip("/"))) or folder or "?"


def chat_row(c, home, work, cost, prompts):
    return {"name": chat_label(c) or c["id"][:8], "chatId": c["id"], "host": c["host"],
            "profileDir": c["profiles"][0] if c["profiles"] else "", "folder": cs.short_path(c["folder"], home) if not c["host"]
            else c["folder"], "cwd": c["folder"], "workedMs": int(work), "cost": round(cost, 4), "prompts": prompts}


def chats_of(chats, stretches, lo, hi, home):
    """The chats of [lo, hi) (a day), what Claude worked on most first:
    their work, cost and prompts in it."""
    work = {}
    for a, b, key, rate in stretches:
        work[key] = work.get(key, 0) + rate * max(0, min(b, hi) - max(a, lo))
    rows = []
    for c in chats:
        cost = sum(request_cost(k, t) for h, agg in c["hours"].items() if lo <= int(h) * HOUR_MS < hi for k, t in agg["t"].items())
        prompts = sum(1 for p in c["prompts"] if lo <= p < hi)
        if work.get(c["key"], 0) >= 60_000 or cost >= 0.01 or prompts:
            rows.append(chat_row(c, home, work.get(c["key"], 0), cost, prompts))
    return sorted(rows, key=lambda r: (-r["workedMs"], -r["cost"]))[:TOP_DAY_CHATS]


def turn_cost(tokens):
    return sum(request_cost(key, list(t[:6]) + [1]) for key, t in (tokens or {}).items())


def spending(chats, chat_rows, lo, hi, home):
    """Where the money went: the turns that cost most (their own replies, not
    their subagents'), the chats, and coming back to a chat after its cache
    let go (a cold resume: its context written again, at the price of cache
    writes rather than reads)."""
    turns = []
    for c in chats:
        for turn in c["turns"]:
            if lo <= turn[1] < hi and len(turn) > 4:
                cost = turn_cost(turn[4])
                if cost >= 0.01:
                    turns.append(dict(chat_row(c, home, turn[2], cost, 0), startedAt=turn[0], endedAt=turn[1]))
    cold, cold_cost, by_chat = 0, 0.0, {}
    for c in chats:
        for h, agg in c["hours"].items():
            if not lo <= int(h) * HOUR_MS < hi:
                continue
            for ts, key, w5, w1 in agg.get("c", []):
                p, _ = prices_of(key)
                if p is None:
                    continue
                extra = (w5 * (p[1] - p[3]) + w1 * (p[2] - p[3])) / 1e6 * (US_ONLY if "u" in key.partition("|")[2] else 1)
                cold += 1
                cold_cost += extra
                b = by_chat.setdefault(c["key"], dict(chat_row(c, home, 0, 0, 0), resumes=0, cost=0.0))
                b["resumes"] += 1
                b["cost"] += extra
    for b in by_chat.values():
        b["cost"] = round(b["cost"], 4)
    return {
        "turns": sorted(turns, key=lambda t: -t["cost"])[:TOP_TURNS],
        "chats": sorted(chat_rows, key=lambda r: -r["cost"])[:3],
        "cold": {"count": cold, "cost": round(cold_cost, 4),
                 "chats": sorted(by_chat.values(), key=lambda b: -b["cost"])[:3]},
    }


def claude_did(chats, lo, hi):
    """What Claude did: its tool calls (by tool; an MCP server's tools as one),
    the commands it ran, its edits and the files and lines they changed, the
    subagents it started, compactions, web searches."""
    tools, files, added, removed, compactions = {}, set(), 0, 0, 0
    searches = 0
    for c in chats:
        for h, agg in c["hours"].items():
            if not lo <= int(h) * HOUR_MS < hi:
                continue
            for name, n in agg.get("u", {}).items():
                label = "MCP: " + name.split("__")[1] if name.startswith("mcp__") and name.count("__") >= 2 else name
                tools[label] = tools.get(label, 0) + n
            files.update(agg.get("fe", []))
            added += agg.get("x", [0, 0])[0]
            removed += agg.get("x", [0, 0])[1]
            compactions += agg.get("k", 0)
            searches += sum(t[5] for t in agg["t"].values())
    return {
        "toolCalls": sum(tools.values()),
        "tools": sorted(([n, c] for n, c in tools.items()), key=lambda nc: (-nc[1], nc[0]))[:8],
        "commands": tools.get("Bash", 0),
        "edits": sum(tools.get(t, 0) for t in EDIT_TOOLS),
        "files": len(files),
        "linesAdded": added,
        "linesRemoved": removed,
        "subagents": sum(tools.get(t, 0) for t in SUBAGENT_TOOLS),
        "compactions": compactions,
        "webSearches": max(searches, tools.get("WebSearch", 0)),  # (Claude Code's searches run in requests of their own)
        "webFetches": tools.get("WebFetch", 0),
    }


# ---------------------------------------------------------------- permission prompts
# The prompts you answered, and the rules that would have let the same go
# without asking. A rule goes into the asking chats' own folder, its
# .claude/settings.local.json (Claude reads it for chats started there; it's
# not shared through git), never into your user settings. Only what's safe
# to allow is offered: folders of your projects to read, edits in the
# chat's own folder, read-only commands, pages of a site; never what Claude
# flagged (safetyCheck), what you refused, nor anything under /tmp or a
# Claude config folder.

RULES_FILE = "rules.json"  # in the widget's cache: the rules this tab added, to undo them
TOP_RULES = 6
SAFE_COMMANDS = ("ls", "pwd", "wc", "head", "tail", "grep", "rg", "which", "file", "stat", "du", "df", "tree", "date",
                 "uname", "journalctl")
GIT_READS = ("status", "log", "diff", "show", "branch", "rev-parse", "ls-files", "blame")
SHELL_SIGNS = set(";&|<>`$()\\\n{}*?!~")
BASH_RULE = re.compile(r"^Bash\((git (%s)|%s) \*\)$" % ("|".join(GIT_READS), "|".join(SAFE_COMMANDS)))
FETCH_RULE = re.compile(r"^WebFetch\(domain:[a-z0-9][a-z0-9.-]{0,252}\)$")
PRIVATE_HOME = (".claude", ".ssh", ".gnupg", ".config", ".local", ".cache", ".mozilla", ".pki", ".password-store")


def private_place(path, home):
    """Somewhere no rule may reach: outside your home, the home folder itself,
    a config or secrets folder, a temporary one."""
    p, h = str(path).rstrip("/"), str(home).rstrip("/")
    if not p.startswith(h + "/"):
        return True
    first = p[len(h) + 1:].split("/")[0]
    return any(first == d or first.startswith(d + "-") or first.startswith(d + "_") for d in PRIVATE_HOME)


def rule_for(tool, what, reason, folder, home):
    """(kind, value) of the rule that would have let a prompt you allowed in a
    chat started in folder go without asking, or None. kind: "dir" (a folder
    to read in: permissions.additionalDirectories), "edits" (accept edits
    in folder: permissions.defaultMode), "allow" (a permissions.allow rule)."""
    if reason == "safetyCheck" or not folder or private_place(folder, home):
        return None
    inside = lambda path: path == folder or path.startswith(folder.rstrip("/") + "/")  # noqa: E731
    if tool in FILE_TOOLS and what.startswith("/") and not inside(what):
        path = Path(what)
        try:
            root = cs.git_root(path if path.is_dir() else path.parent)
        except OSError:
            root = None
        if root is not None and not private_place(root, home) and not inside(str(root)):
            return "dir", str(root)
        return None
    if tool in EDIT_TOOLS and what.startswith("/") and inside(what):
        return "edits", folder
    if tool == "Bash" and what and not (SHELL_SIGNS & set(what)):
        words = what.split()
        if words[0] == "git" and len(words) > 1 and words[1] in GIT_READS:
            return "allow", "Bash(git %s *)" % words[1]
        if words[0] in SAFE_COMMANDS:
            return "allow", "Bash(%s *)" % words[0]
        return None
    if tool == "WebFetch" and what.startswith("https://"):
        host = (urlparse(what).hostname or "").lower()
        if host and FETCH_RULE.match("WebFetch(domain:%s)" % host):
            return "allow", "WebFetch(domain:%s)" % host
    return None


def tilde(path, home):
    """path from your home folder on, whole (where a rule goes must be clear)."""
    p, h = str(path), str(home).rstrip("/")
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


def rule_id(kind, value, project):
    return cs.short_id("%s\0%s\0%s" % (kind, value, project))


def settings_paths(project):
    base = Path(project) / ".claude"
    return [base / "settings.local.json", base / "settings.json"]


def in_settings(data, kind, value):
    perms = data.get("permissions") if isinstance(data, dict) and isinstance(data.get("permissions"), dict) else {}
    if kind == "dir":
        dirs = perms.get("additionalDirectories")
        return isinstance(dirs, list) and any(isinstance(d, str) and os.path.expanduser(d).rstrip("/") == value for d in dirs)
    if kind == "edits":
        return perms.get("defaultMode") in ("acceptEdits", "bypassPermissions", "dontAsk")
    rules = perms.get("allow")
    return isinstance(rules, list) and value in rules


def rule_state(kind, value, project, home, added):
    """"added" (by this tab: it can be undone), "allowed" (your settings
    already let it through) or "new"."""
    rid = rule_id(kind, value, project)
    if any(a.get("id") == rid for a in added):
        return "added"
    files = settings_paths(project) + [p.real / "settings.json" for p in cs.discover_profiles(home)]
    return "allowed" if any(in_settings(cs.read_json(f), kind, value) for f in files) else "new"


def rule_title(kind, value, project, home):
    where = project_name(project, home)
    if kind == "dir":
        return "Let chats in %s read %s" % (where, tilde(value, home))
    if kind == "edits":
        return "Let chats in %s edit their files without asking" % where
    if value.startswith("Bash("):
        return "Allow “%s” in %s" % (value[5:-3], where)
    return "Allow pages from %s in %s" % (value[len("WebFetch(domain:"):-1], where)


def rule_change(kind, value, project, home):
    """What allowing it changes, in words."""
    path = tilde(settings_paths(project)[0], home)
    if kind == "dir":
        return "adds %s to permissions.additionalDirectories in %s" % (tilde(value, home), path)
    if kind == "edits":
        return "sets permissions.defaultMode to acceptEdits in %s (chats started there accept edits)" % path
    return "adds %s to permissions.allow in %s" % (value, path)


def permission_prompts(chats, lo, hi, home, added=()):
    """The permission prompts you answered in [lo, hi): how many, how long
    Claude waited on them (prompts at once count their time once), by tool,
    and the rules that would have let the most of them through (this
    computer's chats)."""
    answered = refused = 0
    by_tool, rules, waits = {}, {}, []
    for c in chats:
        folder = c.get("folder") or ""
        for h, agg in c["hours"].items():
            if not lo <= int(h) * HOUR_MS < hi:
                continue
            for ts, tool, what, reason, wait, allowed in agg.get("p", []):
                answered += 1
                refused += 0 if allowed else 1
                waits.append([ts - wait, ts])
                by_tool[tool] = by_tool.get(tool, 0) + 1
                rule = rule_for(tool, what, reason, folder, home) if allowed and not c["host"] else None
                if rule is None:
                    continue
                r = rules.setdefault(rule + (folder,), {"kind": rule[0], "value": rule[1], "project": folder,
                                                        "count": 0, "waits": [], "example": tilde(what, home)[:120]})
                r["count"] += 1
                r["waits"].append([ts - wait, ts])
    for r in rules.values():
        r["waitMs"] = int(covered(r.pop("waits")))
    top = sorted(rules.values(), key=lambda r: (-r["count"], -r["waitMs"]))[:TOP_RULES]
    for r in top:
        r["id"] = rule_id(r["kind"], r["value"], r["project"])
        r["state"] = rule_state(r["kind"], r["value"], r["project"], home, added)
        r["title"] = rule_title(r["kind"], r["value"], r["project"], home)
        r["change"] = rule_change(r["kind"], r["value"], r["project"], home)
    return {"answered": answered, "refused": refused, "waitMs": int(covered(waits)),
            "byTool": sorted(([t, n] for t, n in by_tool.items()), key=lambda tn: (-tn[1], tn[0]))[:6], "rules": top}


def load_added(cache_root):
    data = cs.read_json(cache_root / RULES_FILE)
    return [a for a in data.get("added", [])] if isinstance(data, dict) and isinstance(data.get("added"), list) else []


def known_folder(cache, project):
    """Did a chat of yours start in project (the only places a rule goes)?"""
    return any(isinstance(r, dict) and isinstance(r.get("data"), dict) and r["data"].get("folder") == project
               for r in cache.get("files", {}).values())


def write_settings(path, data):
    path = Path(os.path.realpath(str(path)))  # (a link stays a link: its file changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    with open(str(tmp), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if path.exists():
        os.chmod(str(tmp), path.stat().st_mode & 0o7777)  # (who may read it stays as it was)
    os.replace(str(tmp), str(path))


def take_out(perms, key, value):
    """value out of the list perms[key]; the key goes once that's empty."""
    if isinstance(perms.get(key), list):
        perms[key] = [v for v in perms[key] if v != value]
        if not perms[key]:
            del perms[key]


def allow_rule(home, cache_root, kind, value, project):
    """(ok, message, id): the rule goes into project's settings.local.json,
    and is noted to undo it. Only a rule the Recap tab would offer: for a
    folder a chat of yours started in, of a kind and shape it offers."""
    project = os.path.normpath(project) if isinstance(project, str) and project.startswith("/") else ""
    if not project or not os.path.isdir(project) or private_place(project, home):
        return False, "Not a folder a rule can go to", None
    if not known_folder(load_cache(cache_root), project):
        return False, "No chat of yours started in %s" % tilde(project, home), None
    if kind == "dir":
        ok = (isinstance(value, str) and value.startswith("/") and os.path.isdir(value) and not private_place(value, home)
              and cs.git_root(Path(value)) == Path(value))
    elif kind == "edits":
        ok = value == project
    else:
        ok = kind == "allow" and isinstance(value, str) and bool(BASH_RULE.match(value) or FETCH_RULE.match(value))
    if not ok:
        return False, "Not a rule the Recap tab offers", None
    path = settings_paths(project)[0]
    data = cs.read_json(path) if path.exists() else {}
    if not isinstance(data, dict):
        return False, "%s isn't JSON the widget can change: leave it as it is" % tilde(path, home), None
    perms = data.get("permissions")
    if perms is None:
        perms = data["permissions"] = {}
    if not isinstance(perms, dict):
        return False, "permissions in %s isn't what Claude expects" % tilde(path, home), None
    note = {"id": rule_id(kind, value, project), "kind": kind, "value": value, "project": project, "file": str(path),
            "at": int(time.time() * 1000), "created": not path.exists()}
    if in_settings(data, kind, value):
        return True, "Already allowed in %s" % tilde(path, home), None
    if kind == "dir":
        perms.setdefault("additionalDirectories", []).append(value)
    elif kind == "edits":
        note["before"] = perms.get("defaultMode")
        perms["defaultMode"] = "acceptEdits"
    else:
        perms.setdefault("allow", []).append(value)
    write_settings(path, data)
    added = [a for a in load_added(cache_root) if a.get("id") != note["id"]] + [note]
    cs.write_json_atomic(cache_root / RULES_FILE, {"added": added})
    return True, "%s: done (%s)" % (rule_title(kind, value, project, home), rule_change(kind, value, project, home)), note["id"]


def undo_rule(home, cache_root, rid):
    """(ok, message): takes back a rule this tab added, as it added it."""
    added = load_added(cache_root)
    note = next((a for a in added if a.get("id") == rid), None)
    if note is None:
        return False, "That rule wasn't added here"
    path = Path(note["file"])
    data = cs.read_json(path)
    perms = data.get("permissions") if isinstance(data, dict) and isinstance(data.get("permissions"), dict) else None
    if perms is not None:
        if note["kind"] == "dir":
            take_out(perms, "additionalDirectories", note["value"])
        elif note["kind"] == "edits" and perms.get("defaultMode") == "acceptEdits":
            if note.get("before"):
                perms["defaultMode"] = note["before"]
            else:
                del perms["defaultMode"]
        elif note["kind"] == "allow":
            take_out(perms, "allow", note["value"])
        if not perms:
            del data["permissions"]
        if not data and note.get("created"):
            path.unlink()  # (the file this tab made, empty again)
        else:
            write_settings(path, data)
    cs.write_json_atomic(cache_root / RULES_FILE, {"added": [a for a in added if a.get("id") != rid]})
    return True, "Taken back: %s" % rule_title(note["kind"], note["value"], note["project"], home)


# ---------------------------------------------------------------- permissions you gave
# Everything that lets chats on this computer do more without asking you,
# wherever it is:
#   - settings files: each account's settings.json, the .claude/settings.json
#     and settings.local.json of every folder Claude ran in, and the
#     administrator's /etc/claude-code/managed-settings.json (+ .d/*.json):
#     permissions.allow rules, permissions.additionalDirectories, a
#     permissions.defaultMode that skips prompts, approved .mcp.json servers,
#     and the settings that loosen a check (FLAGS: those Claude Code itself
#     counts as not restrictive);
#   - each account's .claude.json: the folders you trusted, and per folder
#     the tools and .mcp.json servers approved there (what older Claude Code
#     saved on "don't ask again"), and bypass mode's warning accepted.
# Revoke takes one out of its file. Not changed here: a folder's
# .claude/settings.json (shared through git) and the administrator's.

GRANTING_MODES = {
    "acceptEdits": "Accept file edits without asking",
    "auto": "Auto mode: a classifier answers the prompts it finds safe",
    "bypassPermissions": "Skip every permission prompt (bypass permissions)",
}
FLAGS = {  # settings that let more go without asking (true): their key path and what they do
    "skipDangerousModePermissionPrompt": "Bypass permissions mode starts without its warning",
    "skipAutoPermissionPrompt": "Auto mode starts without its warning",
    "useAutoModeDuringPlan": "Auto mode acts while you plan",
    "enableAllProjectMcpServers": "Every MCP server of a folder's .mcp.json runs without asking",
    "skipWebFetchPreflight": "Web pages are fetched without Claude Code's safety check of their site",
    "channelsEnabled": "Channels can send messages into chats",
    "autoUploadSessions": "Chats are uploaded to claude.ai by themselves",
    "remoteControlAtStartup": "Remote control starts with every chat",
    "remoteTools.allowUnattendedServing": "Remote tools run while nobody watches",
    "sandbox.autoAllowBashIfSandboxed": "Commands run without asking inside the sandbox",
}
MANAGED_DIR = Path("/etc/claude-code")


def rule_folders(home, cache, profiles):
    """The folders Claude ran in here: each account's .claude.json lists
    them, and the Recap's chats started in them."""
    folders = set()
    for f in claude_jsons(home, profiles):
        data = cs.read_json(f)
        projects = data.get("projects") if isinstance(data, dict) else None
        if isinstance(projects, dict):
            folders.update(k for k in projects if isinstance(k, str))
    for r in cache.get("files", {}).values():
        if isinstance(r, dict) and isinstance(r.get("data"), dict) and isinstance(r["data"].get("folder"), str):
            folders.add(r["data"]["folder"])
    return sorted({os.path.normpath(f) for f in folders if f.startswith("/") and os.path.isdir(f)})


def claude_jsons(home, profiles):
    """Each account's .claude.json (what Claude Code keeps of its folders),
    once each: ~/.claude's is ~/.claude.json when it has none of its own."""
    files = [p.path / ".claude.json" for p in profiles] + [home / ".claude.json"]
    out, seen = [], set()
    for f in files:
        real = os.path.realpath(str(f))
        if real not in seen and os.path.isfile(real):
            seen.add(real)
            out.append(Path(real))
    return out


def flag_on(data, key):
    for part in key.split("."):
        data = data.get(part) if isinstance(data, dict) else None
    return data is True


def granted_title(kind, value, home):
    if kind == "mode":
        return GRANTING_MODES[value]
    if kind == "flag":
        return FLAGS.get(value) or "You accepted bypass permissions mode's warning"
    if kind == "dir":
        return "Read files in %s" % tilde(os.path.expanduser(value), home)
    if kind == "trust":
        return "Trusted folder: %s" % tilde(value, home)
    if kind == "mcp":
        return "MCP server “%s” of a folder's .mcp.json runs without asking" % value
    m = re.match(r"^(\w+)\((.+)\)$", value)
    if m and m.group(1) == "Bash":
        return "Run “%s”" % m.group(2)
    if m and m.group(1) == "WebFetch" and m.group(2).startswith("domain:"):
        return "Open pages from %s" % m.group(2)[len("domain:"):]
    return value


def granted_rules(home, folders, profiles, added=()):
    """Every rule, mode and setting that lets chats go without asking, your
    user settings first: [{"id", "kind" ("allow": a rule, "dir": a folder to
    read, "mode": a default mode, "flag": a setting, "mcp": an approved
    .mcp.json server, "trust": a trusted folder), "value", "folder" (for
    what .claude.json keeps per folder), "title", "place", "file", "where"
    (the file, from ~), "shared" (through git or the administrator's: not
    changed here), "managed" (the administrator's), "added" (by the Recap's Allow), "change" (what revoking
    it does)}]."""
    out, seen = [], set()

    def add(real, kind, value, place, shared, change, folder=""):
        where = tilde(real, home)
        out.append({"id": cs.short_id("%s\0%s\0%s\0%s" % (real, folder, kind, value)), "kind": kind, "value": value,
                    "folder": folder, "title": granted_title(kind, value, home), "place": place, "file": real,
                    "where": where, "shared": shared, "managed": real.startswith(str(MANAGED_DIR) + "/"),
                    "added": added_note(added, real, kind, value) is not None,
                    "change": change % where})

    def take(path, place, shared):
        real = os.path.realpath(str(path))
        if real in seen or not os.path.isfile(real):
            return
        seen.add(real)
        data = cs.read_json(real)
        if not isinstance(data, dict):
            return
        perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
        if perms.get("defaultMode") in GRANTING_MODES:
            add(real, "mode", perms["defaultMode"], place, shared,
                "takes permissions.defaultMode (%s) out of %%s" % perms["defaultMode"])
        for d in perms.get("additionalDirectories") or [] if isinstance(perms.get("additionalDirectories"), list) else []:
            if isinstance(d, str) and d.strip():
                add(real, "dir", d, place, shared, "takes %s out of permissions.additionalDirectories in %%s"
                    % tilde(os.path.expanduser(d), home).replace("%", "%%"))
        for r in perms.get("allow") or [] if isinstance(perms.get("allow"), list) else []:
            if isinstance(r, str) and r.strip():
                add(real, "allow", r, place, shared, "takes %s out of permissions.allow in %%s" % r.replace("%", "%%"))
        for key in FLAGS:
            if flag_on(data, key):
                add(real, "flag", key, place, shared, "sets %s to false in %%s" % key)
        for name in data.get("enabledMcpjsonServers") or [] if isinstance(data.get("enabledMcpjsonServers"), list) else []:
            if isinstance(name, str) and name.strip():
                add(real, "mcp", name, place, shared, "takes %s out of enabledMcpjsonServers in %%s" % name.replace("%", "%%"))

    accounts = {}
    for p in profiles:
        accounts.setdefault(os.path.realpath(str(p.real / "settings.json")), []).append(p.label)
    managed = sorted(MANAGED_DIR.glob("managed-settings.d/*.json")) if MANAGED_DIR.is_dir() else []
    for path in [MANAGED_DIR / "managed-settings.json"] + managed:
        take(path, "every chat (set by your administrator)", True)
    for p in profiles:
        take(p.real / "settings.json", "every chat of %s" % ", ".join(accounts[os.path.realpath(str(p.real / "settings.json"))]), False)
    for folder in folders:
        for name, shared in (("settings.local.json", False), ("settings.json", True)):
            take(Path(folder) / ".claude" / name,
                 "chats started in %s" % project_name(folder, home) + (" (shared through git)" if shared else ""), shared)
    # what each account's .claude.json keeps: trusted folders, and what was approved in them
    by_file = {}
    for p in profiles:
        f = os.path.realpath(str(p.path / ".claude.json"))
        if not os.path.isfile(f) and p.is_default:
            f = os.path.realpath(str(home / ".claude.json"))
        by_file.setdefault(f, []).append(p.label)
    for f in claude_jsons(home, profiles):
        real = str(f)
        names = ", ".join(by_file.get(real) or ["plain claude"])
        data = cs.read_json(real)
        if not isinstance(data, dict):
            continue
        if data.get("bypassPermissionsModeAccepted") is True:
            add(real, "flag", "bypassPermissionsModeAccepted", "every chat of %s" % names, False,
                "sets bypassPermissionsModeAccepted to false in %s (its warning shows again)")
        projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
        for folder in sorted(projects):
            proj = projects[folder]
            if not isinstance(proj, dict) or not isinstance(folder, str) or not os.path.isdir(folder):
                continue  # (a folder gone: nothing to trust any more)
            place = "chats started in %s, for %s" % (project_name(folder, home), names)
            if proj.get("hasTrustDialogAccepted") is True:
                add(real, "trust", folder, "for %s" % names, False,
                    "marks %s untrusted in %%s: Claude asks again before it works there" % tilde(folder, home).replace("%", "%%"),
                    folder)
            for tool in proj.get("allowedTools") or [] if isinstance(proj.get("allowedTools"), list) else []:
                if isinstance(tool, str) and tool.strip():
                    add(real, "allow", tool, place, False, "takes %s out of that folder's allowedTools in %%s" % tool.replace("%", "%%"),
                        folder)
            for name in proj.get("enabledMcpjsonServers") or [] if isinstance(proj.get("enabledMcpjsonServers"), list) else []:
                if isinstance(name, str) and name.strip():
                    add(real, "mcp", name, place, False,
                        "takes %s out of that folder's enabledMcpjsonServers in %%s" % name.replace("%", "%%"), folder)
            if proj.get("enableAllProjectMcpServers") is True:
                add(real, "flag", "enableAllProjectMcpServers", place, False,
                    "sets that folder's enableAllProjectMcpServers to false in %s", folder)
    return out


def added_note(added, real, kind, value):
    """The note of the Recap's Allow that put this rule there, if it did."""
    for a in added:
        if os.path.realpath(str(a.get("file") or "")) != real:
            continue
        if (a.get("kind"), a.get("value")) == (kind, value) or (a.get("kind") == "edits" and (kind, value) == ("mode", "acceptEdits")):
            return a
    return None


def host_permissions(host):
    """An SSH host's Permissions tab (this file run there), as a source."""
    import remote
    answer = remote.host_answer(host, ["--permissions"], name="week.py")
    if not answer.get("ok"):
        return {"host": host, "ok": False, "error": answer.get("message") or answer.get("error") or "no answer"}
    return {"host": host, "ok": True, "error": "", "prompts": answer.get("prompts"), "granted": answer.get("granted") or []}


def permissions(cache, profiles, now_ms, home, added=()):
    """The Permissions tab: the prompts you answered in the last 7 days on
    this computer, with the rules that would have let them through, and the
    permissions you gave."""
    days = week_days(0, now_ms)
    lo, hi = days[0][1], min(days[-1][2], now_ms)
    chats = [dict(c, id=cid, host="", key="|" + cid) for cid, c in chats_between(cache, profiles, lo, days[-1][2], home).items()]
    return {"ok": True, "generatedAt": now_ms, "from": days[0][0], "to": days[-1][0],
            "prompts": permission_prompts(chats, lo, hi, home, added),
            "granted": granted_rules(home, rule_folders(home, cache, profiles), profiles, added)}


def revoke_rule(home, cache_root, profiles, rid):
    """(ok, message): takes a rule or mode of granted_rules out of its file
    (one the Recap's Allow put there as Undo does: as it was before)."""
    added = load_added(cache_root)
    rules = granted_rules(home, rule_folders(home, load_cache(cache_root), profiles), profiles, added)
    g = next((g for g in rules if g["id"] == rid), None)
    if g is None:
        return False, "That rule isn't in your settings any more"
    if g["shared"]:
        return False, ("%s is your administrator's: it can't be changed here" if g["where"].startswith(str(MANAGED_DIR))
                       else "%s is shared through git: change it there") % g["where"]
    real = g["file"]
    note = added_note(added, real, g["kind"], g["value"])
    if note is not None and not g["folder"]:
        return undo_rule(home, cache_root, note["id"])
    data = cs.read_json(real)
    if not isinstance(data, dict):
        return False, "%s isn't JSON the widget can change: leave it as it is" % g["where"]
    kind, value = g["kind"], g["value"]
    if g["folder"] or value == "bypassPermissionsModeAccepted":  # (.claude.json: a folder's entry, or its warning)
        node = data if not g["folder"] else (data.get("projects") or {}).get(g["folder"])
        if not isinstance(node, dict):
            return False, "That isn't in %s any more" % g["where"]
        if kind == "trust":
            node["hasTrustDialogAccepted"] = False
        elif kind == "flag":
            node[value] = False
        else:
            take_out(node, "allowedTools" if kind == "allow" else "enabledMcpjsonServers", value)
            node.setdefault("allowedTools" if kind == "allow" else "enabledMcpjsonServers", [])  # (as Claude Code keeps it)
    elif kind == "flag":
        *parents, last = value.split(".")
        node = data
        for part in parents:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict) and node.get(last) is True:
            node[last] = False
    elif kind == "mcp":
        take_out(data, "enabledMcpjsonServers", value)
    else:
        perms = data.get("permissions")
        if not isinstance(perms, dict):
            return False, "%s isn't JSON the widget can change: leave it as it is" % g["where"]
        if kind == "mode":
            if perms.get("defaultMode") == value:
                del perms["defaultMode"]
        else:
            take_out(perms, "additionalDirectories" if kind == "dir" else "allow", value)
        if not perms:
            del data["permissions"]
    write_settings(real, data)
    return True, "Revoked: %s (%s)" % (g["title"], g["where"])


# ---------------------------------------------------------------- plan limits


def plan_windows(cache_root, profiles):
    """This computer's accounts' plan windows now, with how each filled up
    (the widget's usage history, usage.json): [{"account", "name", "windows":
    [{"key", "label", "resetsAt", "pct", "points": [[ms, pct], ...]}]}]."""
    data = cs.read_json(cache_root / "usage.json")
    groups = data.get("groups") if isinstance(data, dict) and isinstance(data.get("groups"), dict) else {}
    out = []
    for gid, members in cs.group_profiles(profiles):
        named = next((p for p in members if not p.is_default), members[0])
        windows = []
        for key, rec in (groups.get(cs.short_id(gid)) or {}).items():
            points = [p for p in rec.get("points") or [] if isinstance(p, list) and len(p) == 2] if isinstance(rec, dict) else []
            if not points or key not in cs.WINDOW_ORDER:
                continue
            step = max(1, len(points) // 60)  # (60 points draw it)
            windows.append({"key": key, "label": cs.WINDOW_LABELS.get(key, key), "resetsAt": rec.get("resetsAt"),
                            "pct": points[-1][1], "points": points[::step] + ([points[-1]] if (len(points) - 1) % step else [])})
        windows.sort(key=lambda w: cs.WINDOW_ORDER.index(w["key"]))
        if windows:
            out.append({"account": cs.short_id(gid), "name": named.label, "windows": windows})
    return out


def plan_windows_of(plans, account):
    return [p for p in plans if not account or p["account"] == account]


# ---------------------------------------------------------------- currency
# The amounts are API prices in US dollars. In another currency they're
# turned at the European Central Bank's euro reference rates of the day
# (fetched at most every 6 hours, and only then; the last rates serve when
# that fails).

RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
RATES_FILE = "rates.json"
RATES_EVERY_S = 6 * 3600  # (the ECB publishes once a working day, around 16:00 CET)
RATES_RETRY_S = 3600
RATE_LINE = re.compile(r"currency=['\"]([A-Z]{3})['\"]\s+rate=['\"]([0-9]+(?:\.[0-9]+)?)['\"]")
RATE_DAY = re.compile(r"time=['\"](\d{4}-\d{2}-\d{2})['\"]")


def parse_rates(text):
    """{"date", "rates": {code: units per euro}} of the ECB's daily XML, or None."""
    day = RATE_DAY.search(text)
    rates = {code: float(rate) for code, rate in RATE_LINE.findall(text) if float(rate) > 0}
    if not day or "USD" not in rates:
        return None
    rates["EUR"] = 1.0
    return {"date": day.group(1), "rates": rates}


def exchange_rates(cache_root, now_s, fetch=None):
    """The newest rates: the saved ones, fetched again when older than
    RATES_EVERY_S (after a failed try, not again for RATES_RETRY_S: the tab
    mustn't wait on a network that's down every time it reads)."""
    data = cs.read_json(cache_root / RATES_FILE)
    data = data if isinstance(data, dict) else {}
    saved = data if isinstance(data.get("rates"), dict) and "USD" in data["rates"] else None
    if (saved and now_s - (cs.number(data.get("fetchedAt")) or 0) < RATES_EVERY_S
            or "triedAt" in data and now_s - (cs.number(data["triedAt"]) or 0) < RATES_RETRY_S):
        return saved
    try:
        if fetch is None:
            import urllib.request
            with urllib.request.urlopen(RATES_URL, timeout=5) as r:  # noqa: S310 (a fixed https address)
                text = r.read(200_000).decode("utf-8", "replace")
        else:
            text = fetch()
        got = parse_rates(text)
    except Exception:
        got = None
    if got is None:
        data["triedAt"] = now_s
    else:
        data = saved = dict(got, fetchedAt=now_s)
    try:
        cs.write_json_atomic(cache_root / RATES_FILE, data)
    except OSError:
        pass
    return saved


def currency_of(code, cache_root, now_s, fetch=None):
    """{"code", "rate" (per US dollar), "date", "error"} for showing the amounts in code."""
    code = (code or "USD").upper()
    if code == "USD" or not re.match(r"^[A-Z]{3}$", code):
        return {"code": "USD", "rate": 1.0, "date": "", "error": ""}
    rates = exchange_rates(cache_root, now_s, fetch)
    if not rates or code not in rates["rates"]:
        return {"code": "USD", "rate": 1.0, "date": "", "error": "no %s rate from the European Central Bank yet" % code}
    return {"code": code, "rate": rates["rates"][code] / rates["rates"]["USD"], "date": rates["date"], "error": ""}


def limit_episodes(hits):
    """How many times hits [(account, ms)] of usage limits stopped you: the
    chats hitting one limit, and their tries again, are one time."""
    n, last = 0, {}
    for account, t in sorted(hits, key=lambda h: h[1]):
        if account not in last or t - last[account] > LIMIT_EPISODE_MS:
            n += 1
        last[account] = t
    return n


def join_running(work):
    """The timeline's working stretches, joined where the same number of
    chats goes on within JOIN_MS of each other."""
    out = []
    for s, e, n in work:
        if out and out[-1][2] == n and s - out[-1][1] <= JOIN_MS:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e, n])
    return out


# ---------------------------------------------------------------- tips


def tips(chats, w0, w1, total, waits):
    """What would use less of your plan, from the week's usage: Claude Code's
    /usage behaviors (each with its share of the usage, from USAGE_TIP_PCT),
    then how long Claude waited for you and the usage limits you hit."""
    weight, sums = 0.0, [0.0] * W_SIZE
    heavy = long_running = 0.0
    heavy_n = long_n = 0
    buckets, attrs, big = {}, {}, {}
    starts = []
    for c in chats:
        mine, hours = [0.0] * W_SIZE, set()
        for h, agg in c["hours"].items():
            hm = int(h) * HOUR_MS
            if not (w0 <= hm < w1):
                continue
            for i, v in enumerate(agg["w"][:W_SIZE]):
                mine[i] += v
            if agg["w"][W_ALL]:
                hours.add(h)
            for b, v in agg["b"].items():
                buckets.setdefault(int(h) * 12 + int(b), {})[c["key"]] = v
            for k, v in agg["a"].items():
                attrs[k] = attrs.get(k, 0) + v
            for k, (n, size) in agg["g"].items():
                g = big.setdefault(k, [0, 0])
                g[0] += n
                g[1] = max(g[1], size)
        if not mine[W_ALL]:
            continue
        weight += mine[W_ALL]
        sums = [a + b for a, b in zip(sums, mine)]
        if mine[N_SUB] >= USAGE_SUBAGENT_REQUESTS or mine[W_SUB] / mine[W_ALL] > USAGE_SUBAGENT_SHARE:
            heavy += mine[W_ALL]
            heavy_n += 1
        if len(hours) >= USAGE_LONG_HOURS:
            long_running += mine[W_ALL]
            long_n += 1
        if c.get("startTokens"):
            starts.append(c["startTokens"])
    out = []
    if weight > 0:
        parallel, peak = 0.0, 0
        for chats_then in buckets.values():
            peak = max(peak, len(chats_then))
            if len(chats_then) >= USAGE_PARALLEL:
                parallel += sum(chats_then.values())

        def tip(key, w, title, detail, command=""):
            pct = 100 * w / weight
            if pct >= USAGE_TIP_PCT:
                out.append({"key": key, "pct": round(pct), "title": title, "detail": detail, "command": command})

        tip("max_effort", sums[W_MAX], "Save max effort for hard tasks",
            "{}% of your usage ran at max effort".format(round(100 * sums[W_MAX] / weight)), "/effort xhigh")
        tip("workflow", sums[W_FLOW], "Save workflows for big tasks",
            "{}% of your usage was workflow agents".format(round(100 * sums[W_FLOW] / weight)))
        biggest_start = max(starts) if starts else 0
        tip("always_on_context", sums[W_ON], "See what fills your context",
            "chats start with up to {} tokens before your first prompt".format(short_count(biggest_start))
            if biggest_start else "", "/context")
        tip("large_tool_results", sums[W_BIG], *big_result_tip(big))
        tip("cache_miss", sums[W_MISS], "Compact before you step away",
            "{} requests sent over 100k tokens the cache had let go".format(int(sums[N_MISS])), "/compact")
        tip("long_context", sums[W_LONG], "Start fresh when you switch tasks",
            "{} requests carried over 150k tokens of context".format(int(sums[N_LONG])), "/clear")
        tip("subagent_heavy", heavy, "Give simple subagents a cheaper model",
            "subagents did most of the work in {} chats".format(heavy_n))
        tip("high_parallel", parallel, "Run fewer chats at once", "up to {} at once".format(peak))
        tip("long_running", long_running, "Stop chats you no longer need",
            "{} ran for 8 hours or more".format(long_n))
        if not any(t["key"] == "subagent_heavy" for t in out):
            agent = top_attribution(attrs, "agent:")
            if agent:
                tip("agent", agent[1], 'Give "{}" subagents a cheaper model'.format(agent[0]),
                    "{}% of your usage".format(round(100 * agent[1] / weight)))
        skill = top_attribution(attrs, "skill:")
        if skill:
            tip("skill", skill[1], "Run /{} on a cheaper model".format(skill[0]),
                "{}% of your usage".format(round(100 * skill[1] / weight)))
        for prefix, key, title in (("plugin:", "plugin", 'Review the "{}" plugin'),
                                   ("mcp:", "mcp_server", "Turn off {} when you don't need it")):
            found = top_attribution(attrs, prefix)
            if found:
                tip(key, found[1], title.format(found[0]), "{}% of your usage".format(round(100 * found[1] / weight)))
        out.sort(key=lambda t: -t["pct"])
        del out[TIPS_SHOWN:]
    waited = covered(clipped(waits, w0, w1))
    if waited >= WAIT_TIP_MS:
        n = sum(1 for f, t in waits if w0 <= f < w1)
        out.append({"key": "waits", "pct": None, "title": "Let Claude go on without asking",
                    "detail": "it waited {} for your answers, {} times · auto mode, or allow what you always allow"
                              .format(cs.spoken_duration(waited), n), "command": "/permissions"})
    if total.get("limits", 0) >= LIMIT_TIP:
        out.append({"key": "limits", "pct": None, "title": "Let chats go on after a usage limit",
                    "detail": "chats hit a usage limit {} times · Keep going after usage limits, or Go on at…"
                              .format(total["limits"]), "command": ""})
    return out


def top_attribution(attrs, prefix):
    found = [(k[len(prefix):], v) for k, v in attrs.items() if k.startswith(prefix)]
    return max(found, key=lambda kv: kv[1]) if found else None


def big_result_tip(big):
    """(title, detail, command) about the largest tool results, by what made them."""
    if not big:
        return "Ask for narrower reads and searches", "some results were over 10k tokens", ""
    (key, (n, size)) = max(big.items(), key=lambda kv: kv[1][0] * kv[1][1])
    tool, _, what = key.partition("|")
    detail = "up to {} tokens{}".format(short_count(size), ", {} times".format(n) if n > 1 else "")
    if tool == "Read" and what:
        return "Read {} in parts".format(os.path.basename(what)), detail, ""
    if tool == "Bash" and what:
        return 'Shorten the output of "{}"'.format(what), detail, ""
    if tool.startswith("mcp__"):
        return "Ask {} for smaller results".format(tool.split("__")[1] if tool.count("__") >= 2 else tool), detail, ""
    return "Ask {} for less".format(tool), detail, ""


def short_count(n):
    return "{}k".format(round(n / 1000)) if n >= 1000 else str(int(n))


# ---------------------------------------------------------------- SSH hosts


def host_chats(host, back, span="week"):
    """A host's chats of the days asked for (this file run there), as a source."""
    import remote
    answer = remote.host_answer(host, ["--data", "--span", span, "--back", str(back)], name="week.py")
    if not answer.get("ok"):
        return {"host": host, "ok": False, "error": answer.get("message") or answer.get("error") or "no answer",
                "chats": {}, "accounts": []}
    shift = int(time.time() * 1000) - int(cs.number(answer.get("now")) or 0)
    chats = answer.get("chats") if isinstance(answer.get("chats"), dict) else {}
    if abs(shift) > 5000:  # its clock differs from this one's
        for c in chats.values():
            c["turns"] = [[t[0] + shift, t[1] + shift, t[2], [[f + shift, to + shift, k] for f, to, k in t[3]]] + list(t[4:])
                          for t in c.get("turns", [])]
            c["prompts"] = [p + shift for p in c.get("prompts", [])]
            c["limits"] = [p + shift for p in c.get("limits", [])]
    return {"host": host, "ok": True, "chats": chats, "accounts": answer.get("accounts") or []}


# ---------------------------------------------------------------- projects
#
# The Projects tab: each project (a chat's git repository, else its folder)
# with how long Claude worked on it and what it cost, all of it and the last
# 7 days, when it was last at work there, and its chats (newest first). The
# front ends add what's going on now from the status (open chats, plans,
# memories, Claude's last recap).

PROJECT_CHATS = 40


def projects(chats, now_ms, home):
    week_ago = now_ms - 7 * 86400_000
    out = {}
    for c in chats:
        folder = c.get("project") or c.get("folder") or ""
        if not folder:
            continue
        cost = sum(request_cost(key, t) for agg in c["hours"].values() for key, t in agg["t"].items())
        worked = sum(t[2] for t in c["turns"])
        recent = sum(t[2] for t in c["turns"] if t[1] >= week_ago)
        last = max([t[1] for t in c["turns"]] + c["prompts"] + [0])
        if not last:
            continue
        p = out.setdefault(folder, {"name": project_name(folder, home), "path": folder, "short": cs.short_path(folder, home),
                                    "workedMs": 0, "weekMs": 0, "cost": 0.0, "lastAt": 0, "chats": []})
        p["workedMs"] += worked
        p["weekMs"] += recent
        p["cost"] += cost
        p["lastAt"] = max(p["lastAt"], last)
        p["chats"].append({"id": c["id"], "name": chat_label(c) or c["id"][:8], "profileDir": c["profiles"][0] if c["profiles"] else "",
                           "cwd": c.get("folder") or "", "lastAt": last, "workedMs": int(worked), "cost": round(cost, 4)})
    rows = sorted(out.values(), key=lambda p: -p["lastAt"])
    for p in rows:
        p["chats"].sort(key=lambda c: -c["lastAt"])
        p["chatCount"] = len(p["chats"])
        p["chats"] = p["chats"][:PROJECT_CHATS]
        p["workedMs"], p["weekMs"], p["cost"] = int(p["workedMs"]), int(p["weekMs"]), round(p["cost"], 2)
    return rows


# ---------------------------------------------------------------- the Monday recap
#
# Once a week (Monday from 9 o'clock, or the first check after it that week)
# a notification sums up the last 7 days and opens the Recap. Every copy of
# the widget and the VS Code extension asks; the first to ask in a week gets
# it (the week is marked under a lock), so it comes once.

MONDAY_FILE = "recap-monday.json"
MONDAY_HOUR = 9


def monday_due(cache_root, now_s):
    """Whether the Monday recap is due now, marking the week (once a week, for
    every copy that asks): the week's name, or None."""
    t = time.localtime(now_s)
    if t.tm_wday == 0 and t.tm_hour < MONDAY_HOUR:
        return None
    year, week, _ = datetime.date(t.tm_year, t.tm_mon, t.tm_mday).isocalendar()
    name = "%d-W%02d" % (year, week)
    cs.private_dir(cache_root)
    import fcntl
    with open(cache_root / (MONDAY_FILE + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        mark = cs.read_json(cache_root / MONDAY_FILE)
        if isinstance(mark, dict) and mark.get("week") == name:
            return None
        first = not isinstance(mark, dict)
        cs.write_json_atomic(cache_root / MONDAY_FILE, {"week": name, "at": int(now_s * 1000)})
    return None if first else name   # (the first time: from next week on, not the moment you install it)


def monday_recap(sources, now_ms, home):
    """The last 7 days, for the Monday recap's few words."""
    r = recap(sources, 0, "", now_ms, home, span="week")
    total = r.get("sum") or {}
    previous = r.get("previous") or {}
    top = (r.get("projects") or [{}])[0] if r.get("projects") else {}
    return {"workedMs": total.get("workedMs") or 0, "youMs": total.get("youMs") or 0, "prompts": total.get("prompts") or 0,
            "chats": total.get("chats") or 0, "tokenTotal": total.get("tokenTotal") or 0, "cost": total.get("cost") or 0,
            "busiestDay": total.get("busiestDay") or "", "project": top.get("name") or "",
            "promptsBefore": previous.get("prompts") or 0, "workedBefore": previous.get("workedMs") or 0}


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    back, account, hosts, config_dirs, currency, span = 0, "", [], [], "USD", "week"
    pretty = remote = data = perms = status = forget = monday = projects_asked = False
    allow = undo = revoke = history = on = None
    while args:
        arg = args.pop(0)
        if arg == "--allow" and len(args) >= 3:
            allow = [args.pop(0), args.pop(0), args.pop(0)]
        elif arg == "--undo" and args:
            undo = args.pop(0)
        elif arg == "--revoke" and args:
            revoke = args.pop(0)
        elif arg == "--on" and args:
            on = args.pop(0)
        elif arg == "--currency" and args:
            currency = args.pop(0)
        elif arg == "--back" and args and args[0].isdigit():
            back = min(BACK_LIMIT, int(args.pop(0)))
        elif arg == "--span" and args and args[0] in SPANS:
            span = args.pop(0)
        elif arg == "--history-status":
            status = True
        elif arg == "--history" and args and args[0] in ("on", "off"):
            history = args.pop(0) == "on"
        elif arg == "--forget":
            forget = True
        elif arg == "--account" and args:
            account = args.pop(0)
        elif arg == "--host" and args:
            hosts.append(args.pop(0))
        elif arg == "--config-dir" and args:
            config_dirs.append(args.pop(0))
        elif arg == "--pretty":
            pretty = True
        elif arg == "--remote":
            remote = True
        elif arg == "--data":
            data = True
        elif arg == "--permissions":
            perms = True
        elif arg == "--monday-recap":
            monday = True
        elif arg == "--projects":
            projects_asked = True
        else:
            sys.exit(__doc__)
    os.umask(0o077)  # cache files are private to the user
    home = Path.home()
    cs.adopt_legacy_dirs(home)
    now_ms = int(time.time() * 1000)
    cache_root = cs.cache_dir(home)

    def emit(result):
        text = json.dumps(result, indent=2 if pretty and not remote else None)
        sys.stdout.write(("\n" + cs.REMOTE_MARK + " " if remote else "") + text + "\n")

    if on and (allow or undo or revoke):  # (a button of an SSH host's permissions: this file runs there)
        import remote as remote_hosts
        args_there = (["--allow"] + allow if allow else ["--undo", undo] if undo else ["--revoke", revoke])
        answer = remote_hosts.host_answer(on, args_there, name="week.py")
        return emit({"ok": answer["ok"], "message": answer.get("message") or ""})
    if status or forget or history is not None or allow or undo or revoke:  # (a setting, or a button of the permissions)
        try:
            if status:
                result = history_status(cache_root)
            elif forget or history is not None:
                ok, message = forget_history(cache_root) if forget else set_history(cache_root, history)
                result = {"ok": ok, "message": message}
            elif allow:
                ok, message, rid = allow_rule(home, cache_root, *allow)
                result = {"ok": ok, "message": message, "id": rid}
            elif undo:
                ok, message = undo_rule(home, cache_root, undo)
                result = {"ok": ok, "message": message}
            else:
                ok, message = revoke_rule(home, cache_root, cs.discover_profiles(home, config_dirs), revoke)
                result = {"ok": ok, "message": message}
        except Exception as exc:
            result = {"ok": False, "message": "{}: {}".format(type(exc).__name__, exc)}
        return emit(result)
    if projects_asked:   # (the Projects tab: every chat the cache has, by project)
        try:
            profiles = cs.discover_profiles(home, config_dirs)
            cache = updated_cache(cache_root, profiles, now_ms)
            chats = chats_between(cache, profiles, 0, now_ms + 1, home)
            for cid, c in chats.items():
                c["id"] = cid
            result = {"ok": True, "generatedAt": now_ms, "projects": projects(list(chats.values()), now_ms, home),
                      "currency": currency_of(currency, cache_root, now_ms / 1000)}
        except Exception as exc:
            result = {"ok": False, "error": "{}: {}".format(type(exc).__name__, exc)}
        return emit(result)
    if monday:   # (cheap unless it's due: then the last 7 days)
        try:
            week = monday_due(cache_root, now_ms / 1000)
            if week is None:
                return emit({"ok": True, "due": False})
            profiles = cs.discover_profiles(home, config_dirs)
            cache = updated_cache(cache_root, profiles, now_ms)
            first_ms = first_activity(cache)
            days = span_days("week", 0, now_ms, first_ms)
            chats = chats_between(cache, profiles, span_since("week", 0, now_ms, first_ms), days[-1][2], home)
            _, accounts = account_ids(profiles)
            result = dict(monday_recap([{"host": "", "chats": chats, "accounts": accounts}], now_ms, home), ok=True, due=True, week=week)
        except Exception as exc:
            result = {"ok": False, "due": False, "error": "{}: {}".format(type(exc).__name__, exc)}
        return emit(result)
    try:
        profiles = cs.discover_profiles(home, config_dirs)
        cache = updated_cache(cache_root, profiles, now_ms)
        if perms:
            result = permissions(cache, profiles, now_ms, home, load_added(cache_root))
            if hosts:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as pool:
                    result["hosts"] = list(pool.map(host_permissions, hosts))
            return emit(result)
        first_ms = first_activity(cache)
        days = span_days(span, back, now_ms, first_ms)
        chats = chats_between(cache, profiles, span_since(span, back, now_ms, first_ms), days[-1][2], home)
        _, accounts = account_ids(profiles)
        if data:  # ("now" as it answers: the computer asking sets this one's clock against its own)
            result = {"ok": True, "now": int(time.time() * 1000), "chats": chats, "accounts": accounts,
                      "host": os.uname().nodename}
        else:
            sources = [{"host": "", "chats": chats, "accounts": accounts}]
            if hosts:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as pool:
                    sources += list(pool.map(lambda h: host_chats(h, back, span), hosts))
            if span == "all":  # (a host's chats may go back further)
                starts = [x for src in sources[1:] for c in (src.get("chats") or {}).values()
                          for x in [t[0] for t in c.get("turns", [])[:1]] + c.get("prompts", [])[:1]]
                first_ms = min([first_ms] * (first_ms is not None) + starts) if starts else first_ms
            result = recap(sources, back, account, now_ms, home, plan_windows(cache_root, profiles), load_added(cache_root),
                           span, first_ms)
            result["currency"] = currency_of(currency, cache_root, now_ms / 1000)
    except Exception as exc:  # the widget must always get parseable output
        result = {"ok": False, "error": "{}: {}".format(type(exc).__name__, exc), "generatedAt": now_ms}
    emit(result)


if __name__ == "__main__":
    main()
