#!/usr/bin/env python3
"""Status backend for the Talpy Den Plasma widget.

Prints one JSON object describing every local Claude Code session (working,
waiting for input, idle), the plan-limit usage captured by the status line,
and token totals parsed from transcripts. Standard library only; the widget
runs it as `python3 -I talpy_den.py` every couple of seconds.

Data sources (per Claude config dir, e.g. ~/.claude, ~/.claude-work):
  <cfg>/sessions/<pid>.json       live session registry kept by Claude Code
  <cfg>/jobs/<id>/state.json      background jobs (also ones with no process)
  <cfg>/projects/**/*.jsonl       transcripts (token usage per message, and
                                  which chat a session shows: shared chats are
                                  hard-linked into several profiles)
  <cfg>/history.jsonl             prompt history (recent folders, last use)
  <account file>                  oauthAccount.accountUuid / organizationType /
                                  displayName, cachedUsageUtilization (Claude
                                  Code's own plan usage) and each folder's
                                  trust flag only
  ~/.cache/talpy-den/statusline/<cfg dirname>/<session_id>.json
                                  status-line input snapshots (rate limits,
                                  context window), written by statusline-snippet.sh
  ~/.cache/talpy-den/accounts.json
                                  profiles accounts.py logged out of
"""

import fcntl
import hashlib
import json
import math
import mmap
import os
import re
import shlex
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

SNAPSHOT_MAX_AGE = 8 * 86400  # seconds a status-line snapshot is kept
TOKEN_SCAN_INTERVAL = 60  # seconds between transcript scans
TOKEN_DAYS = 8  # days of per-message token records kept in the cache
STATS_DAYS = 30  # days of per-day sums kept for the stats (token_stats)
STATS_SHOWN_DAYS = 14  # days the stats show
CACHE_VERSION = 3  # token cache; 3 = keyed by the real profile folder, linked folders followed
# (the stats' history, prompts and chatFolders were added without a new version: older
# copies of this script, e.g. a VS Code extension not updated yet, keep them as they are)

PLAN_NAMES = {
    "claude_free": "Free",
    "claude_pro": "Pro",
    "claude_max": "Max",
    "claude_team": "Team",
    "claude_enterprise": "Enterprise",
}
WINDOW_LABELS = {
    "five_hour": "5h",
    "seven_day": "7d",
    "seven_day_opus": "7d Opus",
    "seven_day_sonnet": "7d Sonnet",
    "spend_limit": "Spend",
}
WINDOW_ORDER = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"]
STATE_ORDER = {"waiting": 0, "working": 1, "idle": 2, "done": 3}
# Why a session reports status "waiting" (Claude Code's waitingFor values) ->
# what it waits for. "dialog open" is left out on purpose: that is a dialog you
# opened yourself (/model, /effort, ...), and Claude Code's own terminal status
# doesn't count it as blocked either.
WAITING_REASONS = {
    "permission prompt": "permission",
    "goal proposal": "goal approval",
    "sandbox request": "network access",
    "worker request": "worker request",
    "input needed": "question",
}
FINISHED_JOB_STATES = ("done", "failed", "stopped")
SESSION_KINDS = ("interactive", "bg")  # not the daemon's own processes
# Everything Claude Code writes into these fields of its session and job files
# (read from the 2.1.304 binary). A value outside them is new: an unknown
# status counts as idle, an unknown kind isn't listed, so the check says so
# (unknown_values_text) instead of quietly showing chats wrong. (A new
# waitingFor shows as it is, with the chat waiting: you'd see that.)
KNOWN_VALUES = {
    "session kind": ("interactive", "bg", "daemon", "daemon-worker"),
    "session status": ("busy", "shell", "idle", "waiting"),
    "job state": ("working", "running", "blocked") + FINISHED_JOB_STATES,
    "job tempo": ("active", "blocked", "idle"),
}
NOT_STARTED = "send a prompt to start"  # needs of a background session given no prompt yet
RECENT_JOB_MS = 5000
KEEP_DONE_HOURS = 24  # finished background jobs stay listed this long


# ---------------------------------------------------------------- helpers


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, ValueError):  # ValueError: not UTF-8
        return None


def short_id(value):
    """Stable one-way id, so account UUIDs and paths never appear in the output."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def private_dir(path):
    """Create the cache dir readable only by the user: it holds project paths."""
    path.mkdir(parents=True, exist_ok=True)
    if path.stat().st_mode & 0o077:
        os.chmod(path, 0o700)


def write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        # (on a full disk every check, a new process, would leave one)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def to_ms(value):
    """Epoch seconds, epoch milliseconds or an ISO-8601 string -> epoch ms."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value if value > 1e12 else value * 1000) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def local_day(timestamp):
    """ISO timestamp from a transcript -> local YYYY-MM-DD, or None."""
    ms = to_ms(timestamp)
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000).date().isoformat()
    except (OverflowError, OSError, ValueError):  # a date out of range
        return None


def short_path(path, home):
    if not path:
        return ""
    p, h = str(path), str(home)
    if p == h:
        return "~"
    if p.startswith(h + "/"):
        p = "~/" + p[len(h) + 1 :]
    parts = p.split("/")
    if len(parts) > 4:
        p = "…/" + "/".join(parts[-2:])
    return p


def strip_prefix(text, prefix):  # str.removeprefix needs Python 3.9; remote hosts may be older
    return text[len(prefix):] if text.startswith(prefix) else text


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


# ---------------------------------------------------------------- profiles


class Profile:
    """One Claude Code config dir (what CLAUDE_CONFIG_DIR points at)."""

    def __init__(self, path, home, aliases=()):
        self.path = path
        self.dirname = path.name
        try:
            self.real = path.resolve()
        except (OSError, RuntimeError):
            self.real = path
        # Rows and caches are keyed by the real folder: a link like ~/.claude
        # can be switched to another profile at any time.
        self.key = self.real.name
        # Other names of the same dir (e.g. ~/.claude linked to ~/.claude-personal):
        # status-line snapshots are filed under whichever name a session used.
        # A snapshot filed under a link's name only counts if it's newer than
        # the link itself: before the link was last switched, that name meant
        # another profile.
        names = [path, *(a if isinstance(a, Path) else home / a for a in aliases), self.real]
        self.snapshot_sources = {}
        for p in names:
            try:
                since = p.lstat().st_mtime if p.is_symlink() else 0.0
            except OSError:
                since = 0.0
            self.snapshot_sources[p.name] = max(since, self.snapshot_sources.get(p.name, 0.0))
        self.snapshot_dirs = list(self.snapshot_sources)
        is_default = path == home / ".claude"
        name = (read_text(path / ".profile-name") or "").strip()
        self.custom_name = bool(name)
        if not name:
            # (~/.claude: "main", not "default", which marks the default account)
            name = "main" if is_default else strip_prefix(path.name, ".claude-").lstrip(".") or path.name
        self.label = name
        # Claude Code keeps account info in $CLAUDE_CONFIG_DIR/.claude.json, or
        # ~/.claude.json when running with the default config dir.
        data = read_json(path / ".claude.json")
        if is_default and not (isinstance(data, dict) and data.get("oauthAccount")):
            data = read_json(home / ".claude.json")
        data = data if isinstance(data, dict) else {}
        account = data.get("oauthAccount") if isinstance(data.get("oauthAccount"), dict) else {}
        self.account_id = account.get("accountUuid")
        # and its own cache of the plan usage (what /usage and the VS Code
        # panel fetch): for an account whose status line hasn't run lately
        cache = data.get("cachedUsageUtilization")
        self.usage_cache = cache if isinstance(cache, dict) and cache.get("accountUuid") in (None, self.account_id) else None
        self.display_name = clean_text(account.get("displayName"), 40) or None
        org_type = account.get("organizationType") or ""
        self.plan = PLAN_NAMES.get(org_type) or (
            strip_prefix(org_type, "claude_").replace("_", " ").title() if org_type else None
        )
        self.is_default = is_default


def is_profile_dir(path):
    """A Claude config folder: one Claude has run in, or a new one (made by
    accounts.py or another profile tool: .profile-name; Claude's .claude.json once
    you've started to log in)."""
    return is_dir(path) and (is_dir(path / "sessions") or is_dir(path / "projects")
                             or os.path.isfile(str(path / ".profile-name")) or os.path.isfile(str(path / ".claude.json")))


def discover_profiles(home, extra_dirs=()):
    candidates = [home / ".claude", *sorted(home.glob(".claude-*"))]
    found = []
    for d in candidates:
        if is_profile_dir(d):
            found.append(d)
    for e in extra_dirs:
        d = Path(e).expanduser()
        if is_dir(d):
            found.append(d)
    by_real = {}
    for d in found:
        by_real.setdefault(d.resolve(), []).append(d)
    # A folder found as itself and through a link (~/.claude) is named after
    # itself: the link can be switched to another profile at any time
    profiles = [Profile(dirs[0], home, dirs[1:])
                for dirs in (sorted(ds, key=lambda d: d.is_symlink()) for ds in by_real.values())]
    keys = set()
    for p in profiles:  # two folders of the same name (e.g. --config-dir /data/.claude): told apart
        if p.key in keys:
            p.key = f"{p.key}-{short_id(str(p.real))[:6]}"
        keys.add(p.key)
    # A lone ~/.claude isn't the "main" one of anything: name it the way Claude
    # knows you (the account's display name), unless .profile-name says otherwise.
    if len(profiles) == 1 and profiles[0].is_default and not profiles[0].custom_name:
        profiles[0].label = profiles[0].display_name or "Claude"
    return profiles


def matches_profile(profile, name):
    """Is name (a folder name like .claude-work, or a path) this profile?"""
    if name in (profile.dirname, profile.key, *profile.snapshot_dirs):
        return True
    try:
        return Path(name).expanduser().resolve() == profile.real
    except (OSError, RuntimeError):
        return False


def is_dir(path):
    """Path.is_dir, also on Python 3.7-3.11 (an SSH host's), where a folder
    you may not read raises instead of saying no."""
    try:
        return path.is_dir()
    except OSError:
        return False


def logged_out_notes(cache_root):
    """Real folders of the profiles accounts.py logged out of: {path: ms}."""
    data = read_json(cache_root / "accounts.json")
    notes = data.get("loggedOut") if isinstance(data, dict) else None
    return notes if isinstance(notes, dict) else {}


def needs_login(profile, logged_out=()):
    """Isn't this profile logged in to a Claude account? Claude's logout
    takes oauthAccount out of its .claude.json. Without it, a profile may
    still use an API key (which the widget can't see), so only a new one
    (.profile-name, no chats yet) or one logged out here counts."""
    if profile.account_id is not None:
        return False
    return profile.custom_name or not is_dir(profile.path / "projects") or str(profile.real) in logged_out


def group_profiles(profiles):
    """Profiles logged into the same account share plan limits -> one group."""
    groups = {}
    for p in profiles:
        groups.setdefault(p.account_id or f"dir:{p.path}", []).append(p)
    return list(groups.items())


# ---------------------------------------------------------------- sessions


def proc_start_time(pid):
    """Field 22 (starttime) of /proc/<pid>/stat, as Claude stores in procStart."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            stat = f.read()
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2 :].split()  # comm may contain spaces
    return fields[19] if len(fields) > 19 else None


def is_alive(entry):
    pid = entry.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    start = proc_start_time(pid)
    if start is None:
        return False
    expected = entry.get("procStart")
    return expected is None or str(expected) == start  # guards against PID reuse


def is_recurring(job):
    """Routines, self-waking jobs and /loop jobs finish a run without being done
    (Claude Code 2.1.300: GY + Vat)."""
    in_flight = job.get("inFlight") if isinstance(job.get("inFlight"), dict) else {}
    looped = any(isinstance(job.get(k), str) and job[k].strip().lower().startswith("/loop")
                 for k in ("intent", "initialPrompt"))
    return (job.get("routine") is not None or job.get("selfWake") is True
            or "session_cron" in (in_flight.get("kinds") or []) or looped)


def job_finished(job):
    return job.get("state") in FINISHED_JOB_STATES and job.get("tempo") != "active"


def note_unknown(ctx, record, job=False):
    """Note what a session entry (or a job's state.json) holds that Claude Code
    didn't write before (KNOWN_VALUES), for unknown_values_text."""
    unknown = getattr(ctx, "unknown", None)
    if unknown is None or not isinstance(record, dict):
        return
    if job:
        fields, version = [("job state", "state"), ("job tempo", "tempo")], record.get("cliVersion")
    else:
        fields, version = [("session kind", "kind"), ("session status", "status")], record.get("version")
    for what, key in fields:
        value = record.get(key)
        if value is not None and value not in KNOWN_VALUES[what]:
            unknown.setdefault((what, clean_text(str(value), 40)), version if isinstance(version, str) else "")


def unknown_values_text(unknown):
    """One line for the popup about the new values, or None."""
    if not unknown:
        return None
    versions = sorted({v for v in unknown.values() if v}, key=version_key)
    found = ", ".join(f'{what} "{value}"' for what, value in sorted(unknown))
    return (f"{'Claude Code ' + versions[-1] if versions else 'Claude Code'} writes something this widget "
            f"doesn't know yet ({found}): some chats may show the wrong state until the widget is updated.")


def session_state(entry, job=None):
    """Map a session (and its background-job record) to waiting/working/idle/done.

    Mirrors Claude Code 2.1: the `claude agents` rules for background jobs and
    the terminal status (OSC 7501) rules for interactive sessions.
    """
    status = entry.get("status")
    waiting = status == "waiting" and entry.get("waitingFor") != "dialog open"
    if status == "busy":
        return "working"
    if job is None:
        if status == "shell":  # turn over, but a background shell still runs
            return "working"
        return "waiting" if waiting else "idle"
    # Background job. The live status above wins over the job record, whose
    # state can lag behind (e.g. still "blocked" after you answered); a job
    # needs you only when its tempo says so or the session itself waits.
    if job_finished(job) and not (job.get("state") == "done" and is_recurring(job)):
        return "done"
    if job.get("needs") == NOT_STARTED and job.get("tempo") == "blocked":
        return "idle"  # dispatched without a prompt: nothing for Claude to do yet
    if job.get("tempo") == "blocked" or waiting:
        return "waiting"
    return "working"


def parked_job_state(job, present, now_ms, keep_done_ms=0):
    """(state, reason) of a job record whose session process is gone, or None to hide it.

    Mirrors `claude agents --all`: the daemon retires idle workers, including
    jobs blocked on you, so a job can need input with no process running.
    Finished jobs (and jobs whose process died) stay listed for keep_done_ms.
    """
    if job.get("needs") == NOT_STARTED:
        return None
    updated = to_ms(job.get("updatedAt"))
    recent_end = bool(keep_done_ms) and updated is not None and now_ms - updated <= keep_done_ms
    if job_finished(job):
        if not (job.get("state") == "done" and is_recurring(job)):
            return ("done", "" if job.get("state") == "done" else job["state"]) if recent_end else None
        # A recurring job between two runs isn't done (`claude agents` too)
        return ("waiting" if job.get("tempo") == "blocked" else "working"), ""
    created = to_ms(job.get("createdAt"))
    if not present and not (created is not None and now_ms - created < RECENT_JOB_MS):
        exec_job = job.get("template") == "exec" and not job.get("respawnFlags")
        # (state "blocked" stays after you answer: with tempo "active" again,
        # the job went on working and its process died mid-task)
        if job.get("state") == "blocked" and job.get("tempo") != "active" and not exec_job:
            return "waiting", ""
        return ("done", "failed") if recent_end else None  # its process died mid-task
    return ("waiting" if job.get("tempo") == "blocked" else "working"), ""


def clean_text(value, limit=200):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def valid_id(value):
    return isinstance(value, str) and value != "" and "/" not in value and value not in (".", "..")


def snapshot_paths(snapshot_root, profile, ids):
    """(mtime, path) of this profile's status-line snapshots of these session
    ids, newest first, leaving out ones filed under a link's name before the
    link was last switched."""
    found = []
    for name, since in profile.snapshot_sources.items():
        for sid in ids:
            if not valid_id(sid):
                continue
            path = snapshot_root / name / f"{sid}.json"
            t = mtime(path)
            if t is not None and t >= since:
                found.append((t, path))
    found.sort(reverse=True)
    return found


def attach_snapshot(session, snapshot_root, profile, ids=None):
    """Add model, context fill and cost from the session's newest status-line snapshot."""
    ids = dict.fromkeys(ids or [session["sessionId"]])
    for _, path in snapshot_paths(snapshot_root, profile, ids):
        snap = read_json(path)
        if isinstance(snap, dict):
            part = lambda key: snap.get(key) if isinstance(snap.get(key), dict) else {}
            session["context"] = number(part("context_window").get("used_percentage"))
            model = part("model").get("display_name")
            session["model"] = model if isinstance(model, str) and model else None
            effort = part("effort").get("level")
            session["effort"] = effort if isinstance(effort, str) and effort else None
            session["costUsd"] = number(part("cost").get("total_cost_usd"))
            return


def make_session(profile, home, *, sid, pid, name, cwd, kind, entrypoint, state, reason,
                 waiting_for, detail, since, started_at):
    return {
        # A shared chat can be listed under two profiles with the same id, so
        # rows are told apart by profile + id + live/background-record.
        "key": f"{profile.key}/{sid}/{'live' if pid else 'job'}",
        "profileDir": profile.key,
        "sessionId": sid,
        "chatId": sid,
        "pid": pid,
        "name": name or os.path.basename(cwd) or sid[:8],
        "cwd": cwd,
        "cwdShort": short_path(cwd, home),
        "profile": profile.label,
        "kind": kind,
        "entrypoint": entrypoint,
        "state": state,
        "reason": reason,
        "waitingFor": waiting_for,
        "detail": detail,
        "since": since,
        "startedAt": started_at,
        "context": None,
        "model": None,
        "effort": None,  # the reasoning effort, from the status line (low … max)
        "costUsd": None,
        "agents": None,
        "shared": None,
        "tmux": False,  # runs in a tmux pane: it can be interrupted (Esc)
        "peek": None,  # what it's doing, your last prompt, Claude's last reply (chat_peek)
        "changes": None,  # the files it changed: {"count", "files"} (ChatCache.changes)
        "tasks": None,  # its task list: {"done", "total", "current", "items"} (task_progress)
        "stats": None,  # its recap: prompts, tool calls, tokens, time worked (stats_summary)
        "plan": None,  # the plan it made: {"slug", "title", "path", "updatedAt"} (plan_of)
        # other chats with a process that changed some of the same files (note_overlaps)
        "overlaps": None,
        "stopped": None,  # what stopped it: a usage limit or an expired login (stopped_by, limit_stop)
        "reply": "background",  # how it takes a message from the chat pane (reply_route)
        "review": None,  # a chat that changed files: {"editedAt", "tests"} (TestsCache.review)
        "permission": None,  # what it asks while it waits for a permission (note_permissions)
        "live": None,  # what it does now, step by step, from the live hook (note_live)
    }


def process_config_dir(pid):
    """The Claude config dir a running process uses right now (its
    CLAUDE_CONFIG_DIR, else ~/.claude of its HOME), with links resolved."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            env = f.read().split(b"\0")
    except OSError:
        return None
    values = {}
    for item in env:
        key, sep, value = item.partition(b"=")
        if sep and key in (b"CLAUDE_CONFIG_DIR", b"HOME"):
            values[key] = value.decode(errors="replace")
    folder = values.get(b"CLAUDE_CONFIG_DIR") or (values[b"HOME"] + "/.claude" if values.get(b"HOME") else None)
    if not folder:
        return None
    try:
        return Path(folder).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def live_entries_all(profiles):
    """[(entry, profile it runs on, profile whose sessions/ holds the entry)]
    of every live session process.

    A process started with a link name (CLAUDE_CONFIG_DIR=~/.claude) follows
    the link when it's switched to another profile: its next status write
    lands in the other profile's sessions/, and the old file stays behind
    until the process exits. So the same process (same pid, start time and
    file name) can show up in two profiles: keep its newest entry, under the
    profile its config dir points to now.
    """
    by_real = {p.real: p for p in profiles}
    best, order = {}, []
    for p in profiles:
        try:
            files = sorted((p.path / "sessions").glob("*.json"))
        except OSError:
            continue
        for f in files:
            entry = read_json(f)
            if not isinstance(entry, dict) or not is_alive(entry):
                continue
            key = (entry.get("pid"), str(entry.get("procStart")), f.stem)
            stamp = max(number(entry.get("statusUpdatedAt")) or 0, number(entry.get("updatedAt")) or 0,
                        int((mtime(f) or 0) * 1000))
            if key not in best:
                order.append(key)
            if key not in best or stamp > best[key][2]:
                best[key] = (entry, p, stamp)
    result = []
    for key in order:
        entry, holder, _ = best[key]
        runs_on = by_real.get(process_config_dir(entry["pid"]))
        result.append((entry, runs_on or holder, holder))
    return result


def read_job(profiles, job_id):
    """A job's state.json, from the first of profiles that has it (newest wins
    when several do: a job written through a switched link can leave a copy)."""
    if not valid_id(job_id):
        return None
    best = None
    for p in profiles:
        job = read_json(p.path / "jobs" / job_id / "state.json")
        if isinstance(job, dict) and (best is None or (to_ms(job.get("updatedAt")) or 0) > (to_ms(best.get("updatedAt")) or 0)):
            best = job
    return best


def live_row(entry, profile, holder, home, ctx):
    """The row of a live session process, or None for processes that aren't sessions."""
    # Pre-warmed spares, terminals parked on a background job and the daemon's
    # own processes are not sessions (`claude agents` hides them).
    if entry.get("spare") or entry.get("parkedJobId") or entry.get("kind") not in SESSION_KINDS:
        return None
    job_id = entry.get("jobId") if valid_id(entry.get("jobId")) else None
    # Background jobs keep their lifecycle (state, tempo, what they need, a
    # one-line detail) in jobs/<jobId>/state.json; some sessions write the same
    # fields into their own entry.
    job = read_job(dict.fromkeys([profile, holder]), job_id) if job_id else None
    own = {k: entry[k] for k in ("state", "tempo", "needs", "detail") if k in entry}
    if own:
        job = {**(job or {}), **own}
    note_unknown(ctx, job, job=True)
    state = session_state(entry, job)
    waiting_for = entry.get("waitingFor") if entry.get("status") == "waiting" else None
    if state == "waiting":
        reason = WAITING_REASONS.get(waiting_for, waiting_for or "")
        detail = clean_text((job or {}).get("needs"))
    elif state == "done":
        outcome = (job or {}).get("state")
        reason = outcome if outcome in ("failed", "stopped") else ""
        detail = clean_text((job or {}).get("detail"))
    else:
        reason = "background shell" if entry.get("status") == "shell" else ""
        detail = ""
    cwd = entry.get("cwd") if isinstance(entry.get("cwd"), str) else ""
    sid = entry.get("sessionId") if valid_id(entry.get("sessionId")) else ""
    row = make_session(
        profile, home, sid=sid, pid=entry.get("pid"), name=entry.get("name"),
        cwd=cwd, kind=entry.get("kind"), entrypoint=entry.get("entrypoint") or "", state=state,
        reason=reason, waiting_for=waiting_for or "", detail=detail,
        since=number(entry.get("statusUpdatedAt")) or number(entry.get("startedAt")),
        started_at=number(entry.get("startedAt")))
    row["tmux"] = isinstance(entry.get("tmux"), str) and bool(entry["tmux"])
    return finish_row(row, job, job_id, profile, ctx, alive=True)


def job_dirs(profile):
    try:
        return sorted(d for d in (profile.path / "jobs").iterdir() if d.is_dir())
    except OSError:
        return []


def newest_job_records(profiles):
    """{job id: real folder of the profile with the job's newest record}. A
    job written through a switched link leaves a copy of its record in the
    other profile; only the newest one counts, even when it's too old to be
    listed (else the stale copy, e.g. still "blocked", would be)."""
    newest = {}
    for p in profiles:
        for d in job_dirs(p):
            job = read_json(d / "state.json")
            if isinstance(job, dict):
                t = to_ms(job.get("updatedAt")) or 0
                if d.name not in newest or t > newest[d.name][0]:
                    newest[d.name] = (t, p.real)
    return {job_id: real for job_id, (_, real) in newest.items()}


def parked_rows(profile, home, live_jobs, ctx, newest=None):
    """Background jobs with no session process: blocked on you, or recently finished."""
    status = read_json(profile.path / "daemon.status.json")
    roster = status.get("workers") if isinstance(status, dict) else None
    roster = roster if isinstance(roster, dict) else {}
    rows = []
    for d in job_dirs(profile):
        if d.name in live_jobs or (newest is not None and newest.get(d.name, profile.real) != profile.real):
            continue
        job = read_json(d / "state.json")
        if not isinstance(job, dict):
            continue
        note_unknown(ctx, job, job=True)
        try:
            row = parked_row(profile, home, ctx, d, job, roster)
        except Exception as exc:  # one odd record mustn't hide the others
            getattr(ctx, "errors", []).append(f"{profile.label}: job {d.name}: {type(exc).__name__}: {exc}")
            continue
        if row is not None:
            rows.append(row)
    return rows


def parked_row(profile, home, ctx, d, job, roster):
    """The row of one background job with no session process, or None."""
    worker = roster.get(d.name)
    parked = parked_job_state(job, isinstance(worker, dict) and is_alive(worker), ctx.now_ms, ctx.keep_done_ms)
    if parked is None:
        return None
    state, reason = parked
    name = next((job[k] for k in ("name", "displayIntent", "intent")
                 if isinstance(job.get(k), str) and job[k].strip()), "")
    row = make_session(
        profile, home, sid=job["sessionId"] if valid_id(job.get("sessionId")) else d.name,
        pid=None, name=clean_text(name, 80), cwd=job.get("cwd") if isinstance(job.get("cwd"), str) else "",
        kind="bg", entrypoint="", state=state, reason=reason, waiting_for="",
        detail=clean_text(job.get("needs") if state == "waiting" else job.get("detail") if state == "done" else ""),
        since=to_ms(job.get("updatedAt")), started_at=to_ms(job.get("createdAt")))
    row = finish_row(row, job, d.name, profile, ctx, alive=False)
    return None if handed_over(profile, row["chatId"], ctx.profiles) else row


def finish_row(row, job, job_id, profile, ctx, alive):
    """Which conversation the row shows, whether it's shared, its status-line
    snapshot and its subagents."""
    row["jobId"] = job_id
    row["reply"] = reply_route(row)
    row["jobSessionId"] = job["sessionId"] if isinstance(job, dict) and valid_id(job.get("sessionId")) else None
    row["chatId"], row["shared"] = chat_link(row, job, profile, ctx)
    attach_snapshot(row, ctx.snapshot_root, profile, [row["sessionId"], row["chatId"], row["jobSessionId"]])
    if ctx.agents is not None:  # a session continuing a chat shows that chat's agents
        row["agents"] = ctx.agents.session_agents(profile, row["chatId"], alive=alive,
                                                  since=row["startedAt"] if alive else None)
    chats = getattr(ctx, "chats", None)
    if chats is not None:
        path = transcript_path(profile, row["chatId"])
        row["peek"] = chats.peek(path)
        peek = row["peek"] or {}
        row["plan"] = plan_of(profile, peek.get("slug"))
        if row["name"].startswith(CONTINUE_PROMPT[:30]) and path is not None and getattr(ctx, "links", None) is not None:
            # a chat that went on by itself is named after that message: its own name instead
            try:
                own = ctx.links.chat_info(path, path.stat())[0]
            except OSError:
                own = ""
            if own and not own.startswith(CONTINUE_PROMPT[:30]):
                row["name"] = clean_text(own, 80)
        if row["plan"] is not None:
            row["plan"]["edited"] = plan_edited(row["plan"]["updatedAt"], mtime(path) if path is not None else None)
        if peek.get("stopped") and row["state"] == "waiting" and not row["waitingFor"] \
                and isinstance(job, dict) and job.get("tempo") == "blocked":
            # a background session a usage limit stopped is "blocked" ("rate
            # limited — wait and retry"): it waits for the limit, not for you
            row["state"], row["reason"], row["detail"] = "idle", "", ""
        # (one that works again, or waits for you, got past it)
        row["stopped"] = dict(peek["stopped"]) if peek.get("stopped") and row["state"] in ("idle", "done") else None
        files = chats.changed_files(path, row["cwd"], ctx.own_folders)
        row["changes"] = chats.changes(path, row["cwd"], ctx.home, ctx.own_folders, files)
        tests = getattr(ctx, "tests", None)
        row["review"] = tests.review(path) if files and tests is not None else None
        if files and isinstance(getattr(ctx, "changed", None), dict):
            ctx.changed[row["key"]] = set(files)
        row["tasks"] = task_progress(ctx.profiles, [row["sessionId"], row["chatId"], row.get("jobSessionId")])
        row["stats"] = chats.stats(path)
        if row["agents"] and path is not None:
            subs = {sub.name[len("agent-"):-len(".jsonl")]: sub for sub in subagent_transcripts(path)}
            for agent in row["agents"]["tree"]:
                st = chats.edits(subs[agent["id"]])["stats"] if agent["id"] in subs else None
                summary = stats_summary(st) if st else None
                if summary:
                    agent["toolCalls"] = sum(n for name, n in st["tools"].items() if name != "SubagentHandback")
                    agent["tokens"] = summary["tokens"]["total"]
    return row


# ---------------------------------------------------------------- subagents

AGENT_TAIL_BYTES = 65536
AGENT_TAIL_ENTRIES = 40  # conversation entries read from the end (its status needs the last few, its steps more)
AGENT_TAIL_MIN = 8  # read further back (up to PEEK_TAIL_MAX) until this many are found
AGENT_CACHE_VERSION = 3  # bump when agent_progress() reads transcripts differently (3: the model)
AGENT_TREE_LIMIT = 30
AGENT_STEPS = 6  # its last tool calls a row shows
AGENT_TASK_CHARS = 600
AGENT_REPORT_CHARS = 1500
FORK_BOILERPLATE_END = "</fork-boilerplate>"  # a fork's first prompt: Claude's rules for forks, then its directive


def short_text(value, limit=70):
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_tool(block):
    """What a tool call is doing, in a few words: "Bash: npm test", "Edit: parser.py"."""
    name = block.get("name") or "tool"
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    if name.startswith("mcp__"):
        parts = name.split("__")
        name = f"{parts[1]}: {parts[2]}" if len(parts) >= 3 else name
    if name == "Bash" and inp.get("command"):
        return f"Bash: {short_text(inp['command'], 60)}"
    for key in ("file_path", "notebook_path", "path"):
        if inp.get(key):
            return f"{name}: {os.path.basename(str(inp[key]).rstrip('/')) or inp[key]}"
    for key in ("description", "pattern", "query", "url", "skill", "prompt"):
        if inp.get(key):
            return f"{name}: {short_text(inp[key], 60)}"
    return name


def report_line(text, limit=90):
    """First line of an agent's report, without markdown decoration."""
    for line in (text or "").splitlines():
        line = line.replace("**", "").strip().lstrip("#*>-| ").strip()
        if line:
            return short_text(line, limit)
    return "finished"


def model_label(model):
    """claude-opus-5-5 -> "Opus 5.5", claude-haiku-4-5-20251001 -> "Haiku 4.5"
    (a model id as transcripts record it); None for none or a stand-in."""
    if not isinstance(model, str) or not model.startswith("claude"):
        return None
    words = [w for w in re.split(r"[-_]", model.split("[")[0])[1:] if not (w.isdigit() and len(w) >= 6)]
    family = next((w for w in words if w.isalpha()), None)
    version = ".".join(w for w in words if w.isdigit())
    if family is None:
        return model
    return f"{family.capitalize()} {version}".strip()


def transcript_model(entries):
    """The model of the newest assistant entry (not a stand-in like <synthetic>)."""
    for e in entries:
        msg = e.get("message") if isinstance(e.get("message"), dict) else {}
        label = model_label(msg.get("model")) if e.get("type") == "assistant" else None
        if label:
            return label
    return None


def agent_progress(path, finished=False):
    """(status, activity, last timestamp ms, model, {"steps", "report"}) from
    the end of a subagent transcript.

    Done = it handed its report back (a SubagentHandback call) or its last entry
    is a final answer (end_turn); failed = it ended on an API error (e.g. a
    usage limit); stopped = interrupted. Otherwise it's still working: on a
    tool call (shown) or thinking about tool results, unless Claude says it
    finished (finished: its meta.json). Steps are its last tool calls
    ("Bash: npm test"), oldest first; the report is what it handed back, as
    plain text ("" until it's done).
    """
    status, activity, ts, recent, report = agent_status(path, finished)
    detail = {"steps": agent_steps(recent), "report": tidy_text(plain_text(report), AGENT_REPORT_CHARS)}
    return status, activity, ts, transcript_model(recent), detail


def agent_status(path, finished=False):
    """agent_progress without the model and steps, plus the entries it
    looked at (newest first) and the report in full."""
    status, activity, ts, recent, report = agent_end(path)
    if status == "running" and finished:  # an end it doesn't know (e.g. out of output tokens)
        return "done", report_line(report) if report else "finished", ts, recent, report
    return status, activity, ts, recent, report


def agent_end(path):
    """agent_status, without what Claude says about it."""
    try:
        size = path.stat().st_size
    except OSError:
        return "running", "", None, [], ""
    nbytes = AGENT_TAIL_BYTES
    while True:  # more of it while big tool results fill the end
        entries = [e for e in tail_entries(path, size, nbytes) if e["type"] != "last-prompt"]
        recent = entries[:AGENT_TAIL_ENTRIES]
        if len(recent) >= AGENT_TAIL_MIN or nbytes >= size or nbytes >= PEEK_TAIL_MAX:
            break
        nbytes *= 4
    if nbytes >= size and len(entries) <= AGENT_TAIL_ENTRIES:
        # the whole run: what came before its first user entry is a fork's
        # copy of the call that started it, not something it did
        users = [i for i, e in enumerate(recent) if e["type"] == "user"]
        recent = recent[:users[-1] + 1] if users else []
    if not recent:
        return "running", "starting…", None, recent, ""
    ts = to_ms(recent[0].get("timestamp"))
    newest = recent[0]
    if newest["type"] == "user":
        text = message_text(newest.get("message")).strip()
        if text.startswith("[Request interrupted"):
            return "stopped", "interrupted", ts, recent, ""
        origin = newest.get("origin") if isinstance(newest.get("origin"), dict) else {}
        if origin.get("kind") == "coordinator" and text:  # the main chat sent it more to do
            return "running", "reading a message from its chat…", ts, recent, ""
    for e in recent:
        msg, content = blocks_of(e)
        if e["type"] == "user":
            if e is recent[0]:
                continue  # tool results just came back; look at what it ran
            return "running", "thinking…", ts, recent, ""
        if e.get("isApiErrorMessage") is True:
            return "failed", short_text(message_text(msg) or "API error", 90), ts, recent, ""
        uses = [c for c in content if c.get("type") == "tool_use"]
        if uses and uses[-1].get("name") == "SubagentHandback":
            report = (uses[-1].get("input") or {}).get("message")
            report = report if isinstance(report, str) else ""
            return "done", report_line(report), ts, recent, report
        if uses:
            label = describe_tool(uses[-1])
            return "running", (label if e is recent[0] else f"{label} (thinking about the result)"), ts, recent, ""
        if msg.get("stop_reason") == "end_turn":
            report = final_answer(recent, e)
            return "done", report_line(report), ts, recent, report
        return "running", "writing…", ts, recent, ""
    return "running", "thinking…", ts, recent, ""


def final_answer(recent, last):
    """The text of an agent's final answer: every text block of the reply
    that ends with entry last (each block is an entry of its own)."""
    msg_id = (last.get("message") or {}).get("id")
    parts = []
    for e in recent[next(i for i, e in enumerate(recent) if e is last):]:
        msg, blocks = blocks_of(e)
        if e["type"] != "assistant" or (e is not last and (msg_id is None or msg.get("id") != msg_id)):
            break
        parts[:0] = [c["text"] for c in blocks if c.get("type") == "text" and isinstance(c.get("text"), str)]
    return "\n\n".join(p for p in parts if p.strip())


def agent_steps(recent):
    """The last AGENT_STEPS tool calls among recent (newest first), oldest first."""
    steps = []
    for e in recent:
        if e["type"] == "assistant":
            steps += [describe_tool(c) for c in reversed(blocks_of(e)[1])
                      if c.get("type") == "tool_use" and c.get("name") != "SubagentHandback"]
            if len(steps) >= AGENT_STEPS:
                break
    return steps[:AGENT_STEPS][::-1]


def agent_task(path):
    """The task an agent was given: its transcript's first prompt (for a
    fork, the directive after Claude's rules for forks), or ""."""
    try:
        with open(path, "rb") as f:
            for n, line in enumerate(f):
                if n >= 50:
                    break
                if not USER_LINE.search(line):
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict) or e.get("type") != "user":
                    continue
                text = message_text(e.get("message"))
                if FORK_BOILERPLATE_END in text:
                    text = re.sub(r"^\s*Your directive:\s*", "", text.split(FORK_BOILERPLATE_END, 1)[1])
                return text.strip()
    except OSError:
        pass
    return ""


def agent_started(path):
    """Timestamp (ms) of the first entry of a subagent transcript."""
    try:
        with open(path, "rb") as f:
            for line in f:
                try:
                    ts = to_ms(json.loads(line).get("timestamp"))
                except (ValueError, AttributeError):
                    continue
                if ts is not None:
                    return ts
    except OSError:
        pass
    return None


class AgentCache:
    """Remembers each subagent transcript's progress by (size, mtime) between polls,
    so only transcripts that changed are read again."""

    PARENT_RETRY_S = 60  # how soon to look again for a nested agent's parent that wasn't found

    def __init__(self, path):
        self.path = path
        data = read_json(path)
        ok = isinstance(data, dict) and data.get("version") == AGENT_CACHE_VERSION
        self.old = data if ok else {}
        self.old_files = data.get("files", {}) if ok else {}
        self.old_parents = data.get("parents", {}) if ok else {}
        self.files, self.parents, self.changed = {}, {}, not ok
        # Parents looked for in vain (agent id: when), so their siblings'
        # transcripts aren't all read again on every check
        misses = data.get("misses") if ok else None
        self.misses = {k: t for k, t in misses.items() if isinstance(t, (int, float))} if isinstance(misses, dict) else {}
        # What an unfolded agent shows (its task, last steps and report) is
        # kept in a file of its own: older copies of this script (a VS Code
        # extension not updated yet) rewrite agents.json without it.
        self.details_path = path.with_name("agent-details.json")
        details = read_json(self.details_path)
        ok = isinstance(details, dict) and details.get("version") == 1
        self.old_details_data = details if ok else {}
        self.old_details = details.get("files", {}) if ok else {}
        self.details, self.details_changed = {}, not ok

    def progress(self, transcript, finished=False):
        """(status, activity, last ts, started ts, model, {"task", "steps",
        "report"}) of a subagent transcript (finished: Claude says it is)."""
        try:
            st = transcript.stat()
        except OSError:
            return "running", "starting…", None, None, None, {"task": "", "steps": [], "report": ""}
        key = str(transcript)
        old, old_detail = self.old_files.get(key), self.old_details.get(key)
        fresh = lambda rec, n: isinstance(rec, list) and len(rec) == n and rec[0] == st.st_size and rec[1] == st.st_mtime_ns
        # "running" from before it finished (or from an older copy of this
        # script, which took an agent that died on an API error for one at
        # work) is read again once
        if fresh(old, 7) and fresh(old_detail, 3) and not (finished and old[2] == "running"):
            self.files[key], self.details[key] = old, old_detail
            return (*old[2:], old_detail[2])
        status, activity, ts, model, detail = agent_progress(transcript, finished)
        if model is None and old and len(old) == 7:
            model = old[6]  # its newest entries are tool results; the model came earlier
        started = old[5] if old and len(old) == 7 and old[5] is not None else agent_started(transcript)
        known = old_detail[2] if isinstance(old_detail, list) and len(old_detail) == 3 and isinstance(old_detail[2], dict) else {}
        # read again while empty: the first look may come before its first prompt is written
        task = known["task"] if isinstance(known.get("task"), str) and known["task"] else \
            tidy_text(plain_text(agent_task(transcript)), AGENT_TASK_CHARS)
        detail = {"task": task, **detail}
        entry = [st.st_size, st.st_mtime_ns, status, activity, ts, started, model]
        self.changed = self.changed or entry != old
        self.files[key], self.details[key] = entry, [st.st_size, st.st_mtime_ns, detail]
        self.details_changed = True
        return status, activity, ts, started, model, detail

    def parent(self, agent_dir, agent_id, tool_use_id):
        """Which other agent of the session started this one (nested agents), if any."""
        if agent_id in self.old_parents:
            self.parents[agent_id] = self.old_parents[agent_id]
            return self.parents[agent_id]
        now = time.time()
        if 0 <= now - self.misses.get(agent_id, 0) < self.PARENT_RETRY_S:
            return None
        needle = f'"{tool_use_id}"'.encode()
        for other in agent_dir.glob("agent-*.jsonl"):
            other_id = other.name[len("agent-"):-len(".jsonl")]
            if other_id == agent_id:
                continue
            try:
                if needle in other.read_bytes():
                    self.parents[agent_id] = other_id
                    self.misses.pop(agent_id, None)
                    self.changed = True
                    return other_id
            except OSError:
                continue
        self.misses[agent_id] = int(now)
        self.changed = True
        return None

    def session_agents(self, profile, session_id, alive, since=None):
        """Subagents of one session as counts plus a display-ordered tree, or
        None. since: when the session's process started (ms), if it runs."""
        if not session_id or "/" in session_id:
            return None
        agents = []
        for agent_dir in (profile.path / "projects").glob(f"*/{session_id}/subagents"):
            for meta_path in agent_dir.glob("agent-*.meta.json"):
                meta = read_json(meta_path)
                if not isinstance(meta, dict):
                    continue
                agent_id = meta_path.name[len("agent-"):-len(".meta.json")]
                status, activity, ts, started, model, detail = self.progress(agent_dir / f"agent-{agent_id}.jsonl",
                                                                              meta.get("finished") is True)
                # its session ended before it finished, or the process that ran
                # it did (a resumed chat keeps its id, and its agents' folder)
                if status == "running" and (not alive or (since and ts is not None and ts < since)):
                    status = "stopped"
                depth = meta.get("spawnDepth") if isinstance(meta.get("spawnDepth"), int) else 1
                parent = None
                if depth > 1 and isinstance(meta.get("toolUseId"), str):
                    parent = self.parent(agent_dir, agent_id, meta["toolUseId"])
                if started is None:
                    try:
                        started = int(meta_path.stat().st_mtime * 1000)
                    except OSError:
                        pass
                agents.append({
                    "id": agent_id,
                    "parent": parent,
                    "type": str(meta.get("agentType") or "agent"),
                    "description": short_text(meta.get("description") or "", 80),
                    "background": meta.get("requestShape") == "background",
                    "status": status,
                    "activity": activity,
                    "started": started,
                    "updated": ts,
                    "model": model,  # e.g. "Haiku 4.5", from its replies
                    "task": detail["task"],  # what it was asked to do
                    "steps": detail["steps"],  # its last tool calls, oldest first
                    "report": detail["report"] if status == "done" else "",  # what it handed back
                    "toolCalls": None,  # how many tool calls and tokens so far (finish_row)
                    "tokens": None,
                })
        if not agents:
            return None
        counts = {k: sum(a["status"] == k for a in agents) for k in ("running", "done", "stopped", "failed")}
        # Display order: depth-first from the session's own agents, oldest first.
        children = {}
        ids = {a["id"] for a in agents}
        for a in sorted(agents, key=lambda a: (a["started"] or 0, a["id"])):
            children.setdefault(a["parent"] if a["parent"] in ids else None, []).append(a)
        tree = []

        def walk(parent, depth):
            for a in children.get(parent, []):
                tree.append({**a, "depth": depth})
                walk(a["id"], depth + 1)
        walk(None, 0)
        if len(tree) > AGENT_TREE_LIMIT:  # keep every running one, then the newest
            keep = {a["id"] for a in tree if a["status"] == "running"}
            for a in sorted(tree, key=lambda a: -(a["started"] or 0)):
                if len(keep) >= AGENT_TREE_LIMIT:
                    break
                keep.add(a["id"])
            tree = [a for a in tree if a["id"] in keep]
        return {**counts, "total": len(agents), "tree": tree}

    def save(self):
        now = time.time()
        data = {"version": AGENT_CACHE_VERSION, "files": self.files, "parents": self.parents,
                "misses": {k: t for k, t in self.misses.items() if 0 <= now - t < CACHE_KEEP_S}}
        kept = keep_unused(self.old, data, ("files", "parents"), now)
        if self.changed or kept or set(self.files) != set(self.old_files):
            try:
                write_json_atomic(self.path, data)
            except OSError:
                pass
        details = {"version": 1, "files": self.details}
        kept = keep_unused(self.old_details_data, details, ("files",), now)
        if self.details_changed or kept or set(self.details) != set(self.old_details):
            try:
                write_json_atomic(self.details_path, details)
            except OSError:
                pass


# ---------------------------------------------------------------- a subagent's whole run, to read
#
# Claude Code has no way to open a subagent from outside (its terminal shows
# an agent's run when you pick it with the keys, VS Code's link only opens
# chats), and a subagent isn't a chat you can resume. So the widget renders
# its transcript as Markdown: the task, every step (its text, each tool call
# with its input and the start of its result, messages the main chat sent it)
# and its report.

AGENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
RUN_RESULT_LINES = 25
RUN_RESULT_CHARS = 2500
RUN_INPUT_LINES = 40
RUN_INPUT_CHARS = 4000
RUN_THINKING_CHARS = 2000


def fenced(text, lang=""):
    """text as a fenced code block, with a fence longer than any run of backticks in it."""
    longest = max([len(m) for m in re.findall(r"`{3,}", text)] + [2])
    mark = "`" * (longest + 1)
    return f"{mark}{lang}\n{text.rstrip()}\n{mark}"


def code_span(text):
    """text as inline code, with more backticks around it than in it."""
    text = str(text)
    ticks = "`" * (max([len(m) for m in re.findall(r"`+", text)] + [0]) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def closed_fences(markdown):
    """Claude's Markdown with a code block it left open closed at its end (a
    reply cut off mid-block), so the rest of the page stays Markdown."""
    open_fence = None
    for line in markdown.splitlines():
        m = FENCE_LINE.match(line)
        if not m:
            continue
        mark = m.group(1)
        if open_fence is None:
            open_fence = mark
        elif mark[0] == open_fence[0] and len(mark) >= len(open_fence) and not line.strip()[len(mark):].strip():
            open_fence = None
    return markdown if open_fence is None else f"{markdown}\n{open_fence}"


def clipped(text, lines, chars):
    """(the start of text: at most lines lines and chars characters, a note
    on what was left out or "")."""
    text = text.rstrip()
    all_lines = text.splitlines()
    kept = "\n".join(all_lines[:lines])[:chars].rstrip()
    if len(kept) >= len(text):
        return text, ""
    rest = len(all_lines) - len(kept.splitlines())
    return kept, (f"… {rest} more line{'s' if rest != 1 else ''}" if rest > 0 else "… cut short")


def clipped_block(text, lines, chars, lang=""):
    kept, note = clipped(text, lines, chars)
    return fenced(kept, lang) + (f"\n*{note}*" if note else "")


def quoted(text):
    return "\n".join("> " + line if line.strip() else ">" for line in text.strip().splitlines())


def clock(ms, with_day=False):
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M" if with_day else "%H:%M:%S")


def spoken_duration(ms):
    """2h 5m, 3m 12s, 40s"""
    s = max(0, int(ms // 1000))
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60}m"
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def token_count(n):
    """45.2k, 1.3M (by what it rounds to: 999,960 is 1.0M, not 1000.0k)"""
    return f"{n / 1e6:.1f}M" if n >= 999_950 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(n)


def tool_call_md(block):
    """A tool call: its name and what it was asked, in Markdown."""
    name = block.get("name") or "tool"
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    shown = name.split("__", 2)[-1] if name.startswith("mcp__") else name
    head = f"**▸ {shown}**"
    if name == "Bash":
        desc = f" · {inp['description']}" if isinstance(inp.get("description"), str) else ""
        return f"{head}{desc}\n\n" + clipped_block(str(inp.get("command", "")), RUN_INPUT_LINES, RUN_INPUT_CHARS, "bash")
    path = next((inp[k] for k in ("file_path", "notebook_path") if isinstance(inp.get(k), str)), None)
    if name in ("Edit", "MultiEdit") and path:
        edits = inp.get("edits") if isinstance(inp.get("edits"), list) else [inp]
        diff = "\n\n".join(
            "\n".join(["- " + l for l in str(e.get("old_string", "")).splitlines()]
                      + ["+ " + l for l in str(e.get("new_string", "")).splitlines()])
            for e in edits if isinstance(e, dict))
        return f"{head} {code_span(path)}\n\n" + clipped_block(diff, RUN_INPUT_LINES, RUN_INPUT_CHARS, "diff")
    if name == "Write" and path:
        return f"{head} {code_span(path)}\n\n" + clipped_block(str(inp.get("content", "")), RUN_INPUT_LINES, RUN_INPUT_CHARS)
    if name == "Agent":
        what = " · ".join(str(inp[k]) for k in ("subagent_type", "description") if isinstance(inp.get(k), str))
        prompt = clipped(str(inp.get("prompt", "")), RUN_INPUT_LINES, RUN_INPUT_CHARS)
        return f"{head}{' · ' + what if what else ''}\n\n" + quoted(prompt[0] + (f"\n\n*{prompt[1]}*" if prompt[1] else ""))
    if path:
        return f"{head} {code_span(path)}"
    simple = {k: v for k, v in inp.items() if isinstance(v, (str, int, float, bool))}
    if len(simple) == len(inp) and len(inp) <= 3 and all(len(str(v)) <= 120 and "\n" not in str(v) for v in inp.values()):
        return head + "".join(f" · {k}: {code_span(v)}" for k, v in inp.items())
    return f"{head}\n\n" + clipped_block(json.dumps(inp, indent=2, ensure_ascii=False), RUN_INPUT_LINES, RUN_INPUT_CHARS, "json")


def tool_result_md(block):
    """A tool's result (the start of it), in Markdown."""
    content = block.get("content")
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
                parts.append(c["text"])
            elif isinstance(c, dict) and c.get("type") == "image":
                parts.append("(an image)")
        content = "\n".join(parts)
    text = content if isinstance(content, str) else ""
    label = "Error" if block.get("is_error") else "Result"
    if not text.strip():
        return f"*{label}: nothing*"
    return f"*{label}:*\n\n" + clipped_block(text, RUN_RESULT_LINES, RUN_RESULT_CHARS)


def agent_run_markdown(path, meta, parent_name="", cwd="", home=None):
    """(title, Markdown) of a subagent's whole run, from its transcript."""
    status, activity, last_ts, recent, report = agent_status(path, meta.get("finished") is True)
    title = short_text(meta.get("description") or meta.get("agentType") or "Subagent", 80)
    steps, task, tools, started, model = [], None, 0, None, transcript_model(recent)
    tokens = new_stats()
    try:
        f = open(path, "rb")
    except OSError:
        return title, f"# {title}\n\nIts transcript can't be read."
    with f:
        for line in f:
            if not (USER_LINE.search(line) or ASSISTANT_LINE.search(line)):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get("type") not in ("user", "assistant"):
                continue
            if task is None and e["type"] == "assistant":
                continue  # a fork's copy of the call that started it
            fold_stats(tokens, e)
            ts = to_ms(e.get("timestamp"))
            started = started if started is not None else ts
            msg, blocks = blocks_of(e)
            if e["type"] == "user":
                if task is None:  # its first prompt: the task
                    task = message_text(msg)
                    if FORK_BOILERPLATE_END in task:
                        task = re.sub(r"^\s*Your directive:\s*", "", task.split(FORK_BOILERPLATE_END, 1)[1])
                    continue
                for c in blocks:
                    if c.get("type") == "tool_result":
                        steps.append(tool_result_md(c))
                text = message_text(msg).strip()
                origin = e.get("origin") if isinstance(e.get("origin"), dict) else {}
                if text and (origin.get("kind") == "coordinator" or not (e.get("isMeta") or text.startswith("<"))):
                    steps.append(f"**{clock(ts)} · Message from the main chat**\n\n" + quoted(text))
                continue
            for c in blocks:
                kind = c.get("type")
                if kind == "text" and isinstance(c.get("text"), str) and c["text"].strip():
                    steps.append(f"**{clock(ts)} · Claude**\n\n{closed_fences(c['text'].strip())}")
                elif kind == "thinking" and isinstance(c.get("thinking"), str) and c["thinking"].strip():
                    thought, note = clipped(c["thinking"], 10 ** 6, RUN_THINKING_CHARS)
                    steps.append(quoted(f"*Thinking:* {thought}" + (f" *{note}*" if note else "")))
                elif kind == "tool_use" and c.get("name") != "SubagentHandback":
                    tools += 1
                    steps.append(f"{clock(ts)} " + tool_call_md(c))
    summary = stats_summary(tokens)
    facts = [f"{meta.get('agentType') or 'agent'} agent"]
    if model:
        facts.append(model)
    if meta.get("requestShape") == "background":
        facts.append("in the background")
    if status == "done":
        facts.append(f"done after {spoken_duration((last_ts or 0) - (started or 0))}" if started and last_ts else "done")
    elif status == "failed":
        facts.append(f"failed: {activity}")
    elif status == "stopped":
        facts.append("stopped")
    else:
        facts.append(f"not finished · last step: {activity}" if activity else "not finished")
    facts.append(f"{tools} tool call{'s' if tools != 1 else ''}")
    if summary and summary["tokens"]["total"]:
        facts.append(f"{token_count(summary['tokens']['total'])} tokens")
    where = [f"A subagent of “{parent_name}”" if parent_name else "A subagent"]
    if cwd:
        where.append(f"in {short_path(cwd, home) if home else cwd}")
    if started:
        where.append(f"started {clock(started, with_day=True)}")
    out = [f"# {title}", " · ".join(facts), " · ".join(where),
           "## Task", (task or "").strip() or "*(none recorded)*", "## Steps"]
    out += steps or ["*None yet.*"]
    out.append("## Report")
    if status == "done":
        out.append(closed_fences(report.strip()) or "*It finished without a report.*")
    elif status in ("failed", "stopped"):
        out.append("*None: it " + ("ended on an error" if status == "failed" else "was interrupted") + " before it had one.*")
    else:
        out.append(f"*None yet: it hadn't finished at {clock(int(time.time() * 1000))} (or its chat stopped it). "
                   "Open it again for what it has done since.*")
    out.append(f"---\n*Transcript: {code_span(path)}*")
    return title, "\n\n".join(part for part in out if part) + "\n"


def find_agent_transcript(profiles, chat_id, agent_id, profile_key=None):
    """(profile, transcript) of a chat's subagent, or (None, None)."""
    if not (isinstance(chat_id, str) and AGENT_ID.match(chat_id) and isinstance(agent_id, str) and AGENT_ID.match(agent_id)):
        return None, None
    for p in sorted(profiles, key=lambda p: p.key != profile_key):
        for path in p.path.glob(f"projects/*/{chat_id}/subagents/agent-{agent_id}.jsonl"):
            return p, path
    return None, None


def agent_run(home, extra_dirs, chat_id, agent_id, profile_key=None):
    """(ok, message, title, Markdown) of a chat's subagent (agent_run_markdown)."""
    profiles = discover_profiles(home, extra_dirs)
    profile, path = find_agent_transcript(profiles, chat_id, agent_id, profile_key)
    if path is None:
        return False, "Can't find that subagent's transcript", None, None
    meta = read_json(path.with_name(f"agent-{agent_id}.meta.json"))
    parent = path.parent.parent.parent / f"{chat_id}.jsonl"
    try:
        name, cwd, _ = read_chat_info(parent, parent.stat().st_size)
    except OSError:
        name, cwd = "", ""
    title, markdown = agent_run_markdown(path, meta if isinstance(meta, dict) else {}, name, cwd, home)
    return True, f"The run of “{title}”", title, markdown


# ---------------------------------------------------------------- what a chat is doing, and what it changed
#
# A peek at a chat without switching to it, from the end of its transcript:
# its newest step (a tool call, thinking, writing), your last prompt and
# Claude's last reply. Each content block of a reply is an entry of its own,
# all with the reply's message id. Also from there (as Claude Code 2.1.304
# writes them):
#   - Claude's recap of the chat: a `system` record of subtype `away_summary`,
#     written when you come back to a chat after a while ("You asked …; Next
#     step: …"; /config turns them off). It counts while nothing was said
#     after it.
#   - the permission mode (default, plan, auto, acceptEdits, …): the newest
#     `permission-mode` record, or the one your last prompt carries.
#   - the chat's slug: its plan, if it made one, is <cfg>/plans/<slug>.md.
#   - what stopped it: a reply that is an API error saying a usage limit was
#     hit ("You've hit your session limit · resets 7:10pm (Europe/Berlin)",
#     the limit named as Claude names each plan window) or the login expired.
#
# The files it changed: before a chat first edits a file, Claude backs it up
# as <cfg>/file-history/<session id>/<name>@v<n> and records that in the
# transcript (a `file-history-delta`, or in older versions a
# `file-history-snapshot` update); a backup name of null means the file
# didn't exist yet. Each of your prompts gets a `file-history-snapshot` with
# every tracked file's backup as of that prompt, which is where the last turn
# started. Changes made with Bash (sed, scripts) aren't tracked, and a file a
# chat edited with no backup on record (e.g. in a subagent) is compared with
# git's HEAD (jump.py --changes).

PEEK_TAIL_BYTES = 262144
PEEK_TAIL_MAX = 4 << 20  # the last lines can be big tool results
PEEK_REPLY_CHARS = 1500
PEEK_PROMPT_CHARS = 300
PEEK_LINE = re.compile(rb'"type":\s*"(?:user|assistant|last-prompt)"')
PEEK_MORE_LINE = re.compile(rb'"type":\s*"(?:user|assistant|last-prompt|system|permission-mode)"')
PEEK_ADDED = ("recap", "mode", "slug", "stopped")  # a cached peek without them (an older copy's) is read again
RECAP_NOTE = re.compile(r"\s*\(disable recaps in /config\)\s*$")
# Claude's names for the plan windows in "You've hit your … limit" (its
# rate-limit types: five_hour is the session limit)
LIMIT_NAMES = (("session limit", "five_hour"), ("weekly limit", "seven_day"), ("Opus limit", "seven_day_opus"),
               ("Sonnet limit", "seven_day_sonnet"), ("Fable limit", "seven_day_overage_included"))
LIMIT_STARTS = ("You've hit your ", "You've reached your ")
OUT_OF_USAGE = ("You're out of usage credits", "Your org is out of usage")
LOGIN_ENDED = ("Login expired", "OAuth token revoked", "Anthropic profile login expired")
PLAN_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")  # a plan's file name (<slug>.md)
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
EDIT_LINE = re.compile(rb'"name":\s*"(?:Edit|Write|MultiEdit|NotebookEdit)"')
SESSION_ID_FIELD = re.compile(rb'"sessionId":\s*"([0-9A-Za-z_-]{1,80})"')
CHANGES_LISTED = 12  # file names a row carries (its tooltip lists them)
CHAT_CACHE_VERSION = 1  # its records got "stats" without a new version (older copies keep them); one without is read again
USER_LINE = re.compile(rb'"type":\s*"user"')
ASSISTANT_LINE = re.compile(rb'"type":\s*"assistant"')


def message_text(msg):
    """The text of a transcript message: a string, or its text blocks."""
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c["text"] for c in content
                         if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str))
    return ""


def human_prompt(e):
    """What you typed, if this transcript entry is a prompt of yours (not a
    tool result, a command's output or a note of Claude Code's own)."""
    if e.get("type") != "user" or e.get("isMeta") or e.get("isCompactSummary") or e.get("isSidechain"):
        return None
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    if isinstance(msg.get("content"), list) and any(
            isinstance(c, dict) and c.get("type") == "tool_result" for c in msg["content"]):
        return None
    text = message_text(msg).strip()
    if not text or text.startswith("<") or text.startswith("[Request interrupted"):
        return None
    return text


def blocks_of(e):
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    content = msg.get("content")
    return msg, [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []


def tail_entries(path, size, nbytes, more=False):
    """Conversation entries (and last-prompt records) from the last nbytes
    of a transcript, newest first; more: system and permission-mode records too."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - nbytes))
            lines = f.read(min(size, nbytes)).splitlines()
    except OSError:
        return []
    if size > nbytes:
        lines = lines[1:]  # probably cut in the middle
    pattern = PEEK_MORE_LINE if more else PEEK_LINE
    kinds = ("user", "assistant", "last-prompt") + (("system", "permission-mode") if more else ())
    entries = []
    for line in reversed(lines):
        if not pattern.search(line):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") in kinds:
            entries.append(e)
    return entries


def step_text(convo):
    """What the chat is doing, from its newest entry: "Bash: npm test",
    "thinking…", "writing…"; "" once its reply is finished."""
    newest = convo[0]
    msg, blocks = blocks_of(newest)
    if newest["type"] == "user":
        if human_prompt(newest) is None:  # tool results came back: what it ran
            prev = next((e for e in convo[1:] if e["type"] == "assistant"), None)
            uses = [c for c in blocks_of(prev)[1] if c.get("type") == "tool_use"] if prev else []
            if uses:
                return f"{describe_tool(uses[-1])} (thinking about the result)"
        return "thinking…"
    uses = [c for c in blocks if c.get("type") == "tool_use"]
    if uses:
        return describe_tool(uses[-1])
    if isinstance(msg.get("stop_reason"), str):
        return ""
    return "writing…" if any(c.get("type") == "text" for c in blocks) else "thinking…"


MARKDOWN_EMPHASIS = re.compile(r"(\*\*|__|`)(.+?)\1")


def plain_text(markdown):
    """Claude's markdown, readable as plain text: no heading marks, code
    fences or emphasis markers; bullets as •."""
    out = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            continue
        line = re.sub(r"^(\s*)#{1,6}\s+", r"\1", line)
        line = re.sub(r"^(\s*)[-*]\s+", r"\1• ", line)
        out.append(MARKDOWN_EMPHASIS.sub(r"\2", line))
    return "\n".join(out)


def tidy_text(text, limit):
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.strip().splitlines()))
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def peek_from(entries):
    """(peek, whether it's complete) from transcript entries, newest first:
    complete once your last prompt is among them, or Claude's reply since it
    plus the last-prompt record."""
    convo = [e for e in entries if e["type"] in ("user", "assistant") and not e.get("isSidechain")]
    last_prompt = next((e["lastPrompt"] for e in entries
                        if e["type"] == "last-prompt" and isinstance(e.get("lastPrompt"), str)), "")
    reply_id, parts, prompt = None, [], None
    for e in convo:
        prompt = human_prompt(e)
        if prompt is not None:
            break
        if e["type"] != "assistant":
            continue
        msg, blocks = blocks_of(e)
        texts = [c["text"] for c in blocks
                 if c.get("type") == "text" and isinstance(c.get("text"), str) and c["text"].strip()]
        if not texts:
            continue
        mid = msg.get("id") if isinstance(msg.get("id"), str) else id(e)
        if reply_id is None:
            reply_id = mid
        if mid == reply_id:
            parts[:0] = texts  # older blocks of the same reply come first
    reply = tidy_text(plain_text("\n\n".join(parts)), PEEK_REPLY_CHARS)
    asked = prompt if prompt is not None else last_prompt
    # The newest turn: a prompt of yours or a reply (not tool results or notes)
    turn = next((e for e in convo if e["type"] == "assistant" or human_prompt(e) is not None), None)
    peek = {
        "activity": step_text(convo) if convo else "",
        "prompt": short_text(asked, PEEK_PROMPT_CHARS) if asked.strip() else "",
        "reply": reply,
        "replyLine": report_line(reply, 160) if reply else "",
        "at": to_ms(convo[0].get("timestamp")) if convo else None,
        "recap": recap_of(entries),
        "mode": mode_of(entries),
        "slug": next((e["slug"] for e in entries if isinstance(e.get("slug"), str) and PLAN_SLUG.match(e["slug"])), None),
        "stopped": stopped_by(turn) if turn is not None else None,
    }
    return peek, prompt is not None or (bool(parts) and bool(last_prompt))


def mode_of(entries):
    """The chat's permission mode (newest first entries), e.g. "plan", or None."""
    for e in entries:
        if e["type"] == "permission-mode" or human_prompt(e) is not None:
            if isinstance(e.get("permissionMode"), str) and e["permissionMode"]:
                return e["permissionMode"]
    return None


def recap_of(entries):
    """{"text", "at"} of Claude's recap of the chat (newest first entries), if
    nothing was said after it: a prompt of yours or a reply makes it old."""
    for e in entries:
        if e["type"] == "system" and e.get("subtype") == "away_summary":
            text = RECAP_NOTE.sub("", e["content"]).strip() if isinstance(e.get("content"), str) else ""
            return {"text": tidy_text(text, PEEK_REPLY_CHARS), "at": to_ms(e.get("timestamp"))} if text else None
        if (e["type"] == "assistant" and not e.get("isSidechain")) or human_prompt(e) is not None:
            return None
    return None


def stopped_by(e):
    """What stopped a chat whose newest turn is the entry e: {"kind": "limit"
    (a usage limit; window: the plan window, e.g. "five_hour", if Claude
    named it) or "login" (it expired), "text": Claude's message, "at"}, or None."""
    if e.get("type") != "assistant" or e.get("isApiErrorMessage") is not True:
        return None
    text = " ".join(message_text(e.get("message")).split())
    kind, window = None, None
    start = next((s for s in LIMIT_STARTS if text.startswith(s)), None)
    if start is not None:
        rest = text[len(start):]
        kind, window = "limit", next((w for name, w in LIMIT_NAMES if rest.startswith(name)), None)
    elif text.startswith(OUT_OF_USAGE):
        kind = "limit"
    elif text.startswith(LOGIN_ENDED):
        kind = "login"
    if kind is None:
        return None
    return {"kind": kind, "window": window, "text": short_text(text, 160), "at": to_ms(e.get("timestamp"))}


def chat_peek(path):
    """{"activity", "prompt", "reply", "replyLine", "at", "recap", "mode",
    "slug", "stopped"} of a chat, read from the end of its transcript (more
    of it while that isn't enough)."""
    try:
        size = path.stat().st_size
    except (OSError, AttributeError):
        return None
    nbytes = PEEK_TAIL_BYTES
    while True:
        peek, complete = peek_from(tail_entries(path, size, nbytes, more=True))
        if complete or nbytes >= size or nbytes >= PEEK_TAIL_MAX:
            return peek
        nbytes *= 4


def new_edits():
    """What a chat changed so far (fold_edits):
    files: {path: [backup name, version]} with each file's first backup; the
        name is None for a file the chat created, False when no backup is on
        record (an edit tool call is all there is);
    snap: id of your newest prompt's snapshot; snapFiles: {path: backup name}
        as of that prompt; turn: {path: backup name} of files first backed up
        since then;
    sessions: session ids whose file-history folders hold its backups;
    stats: what the chat did (fold_stats)."""
    return {"files": {}, "snap": None, "snapFiles": {}, "turn": {}, "sessions": [], "stats": new_stats()}


def new_stats():
    """A chat's numbers so far: your prompts, its tool calls by name, tokens
    [input, output, cacheRead, cacheWrite] (cur: the reply being read, whose
    blocks repeat its usage), first and last entry (ms), and how long Claude
    worked: each turn from your prompt to its last step (busyMs, plus the turn
    open now: turnStart … turnLast)."""
    return {"prompts": 0, "tools": {}, "tokens": [0, 0, 0, 0], "cur": None, "first": None, "last": None,
            "busyMs": 0, "turnStart": None, "turnLast": None, "started": False}


def fold_stats(st, e):
    """Fold one transcript entry into a chat's numbers (new_stats). What
    comes before its first user entry isn't its own: a fork starts with a
    copy of the call that started it (the parent's reply, usage and all)."""
    if not st.get("started", True):  # (numbers kept from before this rule: started)
        if e.get("type") != "user":
            return
        st["started"] = True
    ts = to_ms(e.get("timestamp"))
    if ts is not None:
        st["first"] = ts if st["first"] is None else min(st["first"], ts)
        st["last"] = ts if st["last"] is None else max(st["last"], ts)
    if human_prompt(e) is not None:
        st["prompts"] += 1
        if st["turnStart"] is not None and st["turnLast"] is not None and st["turnLast"] > st["turnStart"]:
            st["busyMs"] += st["turnLast"] - st["turnStart"]
        st["turnStart"], st["turnLast"] = ts, None
        return
    if e.get("type") != "assistant":
        return
    msg, blocks = blocks_of(e)
    if ts is not None and st["turnStart"] is not None:
        st["turnLast"] = ts
    for c in blocks:
        if c.get("type") == "tool_use" and isinstance(c.get("name"), str):
            name = c["name"].split("__")[-1] if c["name"].startswith("mcp__") else c["name"]
            st["tools"][name] = st["tools"].get(name, 0) + 1
    u = msg.get("usage")
    if not isinstance(u, dict):
        return
    vals = [int(number(u.get(k)) or 0) for k in (
        "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    mid = msg.get("id") or e.get("requestId") or e.get("uuid")
    if st["cur"] and st["cur"][0] == mid:
        st["cur"] = [mid] + [max(a, b) for a, b in zip(st["cur"][1:], vals)]
    else:
        if st["cur"]:
            st["tokens"] = [a + b for a, b in zip(st["tokens"], st["cur"][1:])]
        st["cur"] = [mid] + vals


def stats_summary(main, others=()):
    """A chat's recap, for its row: {"prompts", "toolCalls", "tools": [[name,
    count]] most used first, "tokens": {"input", "output", "cacheRead",
    "cacheWrite", "total"}, "workedMs", "startedAt", "lastAt"}; its subagents'
    (others) tool calls and tokens included."""
    tokens, tools = [0, 0, 0, 0], {}
    for st in (main,) + tuple(others):
        cur = st["cur"][1:] if st.get("cur") else [0, 0, 0, 0]
        tokens = [a + b + c for a, b, c in zip(tokens, st["tokens"], cur)]
        for name, n in st["tools"].items():
            tools[name] = tools.get(name, 0) + n
    worked = main["busyMs"]
    if main["turnStart"] is not None and main["turnLast"] is not None and main["turnLast"] > main["turnStart"]:
        worked += main["turnLast"] - main["turnStart"]
    if not (main["prompts"] or sum(tokens) or tools):
        return None
    keys = ("input", "output", "cacheRead", "cacheWrite")
    return {
        "prompts": main["prompts"],
        "toolCalls": sum(tools.values()),
        "tools": sorted(([n, c] for n, c in tools.items()), key=lambda nc: (-nc[1], nc[0]))[:5],
        "tokens": dict(zip(keys, tokens), total=sum(tokens)),
        "workedMs": worked,
        "startedAt": main["first"],
        "lastAt": main["last"],
    }


def backup_target(key, backup):
    """The file a backup record is about (absolute, or relative to the chat's folder)."""
    if not isinstance(key, str) or not key:
        return None
    parent = backup.get("realParentDir") if isinstance(backup, dict) else None
    if isinstance(parent, str) and parent.startswith("/"):
        return os.path.join(parent, os.path.basename(key))
    return key


def backup_name(backup):
    name = backup.get("backupFileName") if isinstance(backup, dict) else None
    return name if isinstance(name, str) and valid_id(name) else None


def note_backup(rec, path, backup):
    version = backup.get("version") if isinstance(backup, dict) and isinstance(backup.get("version"), int) else 0
    first = rec["files"].get(path)
    if first is None or first[0] is False or version < first[1]:
        rec["files"][path] = [backup_name(backup), version]


def fold_edits(rec, e):
    """Fold one transcript entry into rec (new_edits)."""
    kind = e.get("type")
    if kind == "file-history-snapshot":
        snap = e.get("snapshot") if isinstance(e.get("snapshot"), dict) else {}
        tracked = snap.get("trackedFileBackups") if isinstance(snap.get("trackedFileBackups"), dict) else {}
        found = {}
        for key, backup in tracked.items():
            path = backup_target(key, backup)
            if path:
                found[path] = backup
        mid = e.get("messageId") or snap.get("messageId")
        if not e.get("isSnapshotUpdate"):
            rec["snap"], rec["snapFiles"], rec["turn"] = mid, {p: backup_name(b) for p, b in found.items()}, {}
        elif mid == rec["snap"]:
            for path, backup in found.items():
                if path not in rec["snapFiles"] and path not in rec["turn"]:
                    rec["turn"][path] = backup_name(backup)
        for path, backup in found.items():
            note_backup(rec, path, backup)
    elif kind == "file-history-delta":
        path = backup_target(e.get("trackingPath"), e.get("backup"))
        if path:
            if rec["snap"] is not None and e.get("snapshotMessageId") == rec["snap"] and path not in rec["turn"]:
                rec["turn"][path] = backup_name(e.get("backup"))
            note_backup(rec, path, e.get("backup"))
    elif kind == "assistant":
        for c in blocks_of(e)[1]:
            if c.get("type") == "tool_use" and c.get("name") in EDIT_TOOLS and isinstance(c.get("input"), dict):
                path = c["input"].get("file_path") or c["input"].get("notebook_path")
                if isinstance(path, str) and path.startswith("/"):
                    rec["files"].setdefault(os.path.normpath(path), [False, 0])


def scan_edits(path, rec, start=0, sessions=None):
    """Fold the transcript's lines from byte offset start into rec; returns
    the offset read up to (a half-written last line is left for next time).
    With a sessions set, the session ids its entries name are added to it."""
    offset = start
    try:
        with open(path, "rb") as f:
            f.seek(start)
            for line in f:
                if not line.endswith(b"\n"):
                    break
                if sessions is not None:
                    m = SESSION_ID_FIELD.search(line)
                    if m:
                        sessions.add(m.group(1).decode())
                edits = b'"file-history-' in line or EDIT_LINE.search(line)
                talk = ASSISTANT_LINE.search(line) or (USER_LINE.search(line) and b'"tool_result"' not in line)
                if not talk and not rec["stats"].get("started", True) and USER_LINE.search(line):
                    rec["stats"]["started"] = True  # a fork's first entry: tool results, then its directive
                if edits or talk:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        e = None
                    if isinstance(e, dict):
                        if edits:
                            fold_edits(rec, e)
                        fold_stats(rec["stats"], e)
                offset += len(line)
    except OSError:
        pass
    return offset


def merge_edits(rec, other):
    """Add the files another transcript (a subagent's) changed to rec's."""
    for path, first in other["files"].items():
        mine = rec["files"].get(path)
        if mine is None or (mine[0] is False and first[0] is not False):
            rec["files"][path] = list(first)


def subagent_transcripts(path):
    """A chat's subagent transcripts: projects/<proj>/<id>/subagents/agent-*.jsonl."""
    try:
        return sorted((path.parent / path.stem / "subagents").glob("agent-*.jsonl"))
    except OSError:
        return []


def chat_edits(path):
    """Everything a chat changed (new_edits), subagents included, read in full."""
    rec, sessions = new_edits(), {path.stem}
    scan_edits(path, rec, 0, sessions)
    for sub in subagent_transcripts(path):
        other = new_edits()
        scan_edits(sub, other)
        merge_edits(rec, other)
    rec["sessions"] = sorted(s for s in sessions if valid_id(s))
    return rec


def file_label(path, cwd, home):
    """A changed file's name as a row shows it: relative to the chat's folder, else ~/…"""
    if cwd and (path + "/").startswith(cwd.rstrip("/") + "/") and path != cwd:
        return path[len(cwd.rstrip("/")) + 1:]
    return short_path(path, home)


def absolute_path(path, cwd):
    """A path from a backup record, made absolute (they can be relative to the chat's folder)."""
    if path.startswith("/"):
        return path
    return os.path.normpath(os.path.join(cwd, path)) if cwd else None


def project_file(path, skip):
    """Is this changed file part of the chat's work, not in one of Claude's own folders (skip)?"""
    return path is not None and not any(path == s or path.startswith(s + "/") for s in skip)


def changed_paths(rec, cwd, skip):
    """The absolute paths of the files in rec, minus those in Claude's own folders."""
    return sorted({a for a in (absolute_path(p, cwd) for p in rec["files"]) if project_file(a, skip)})


TASKS_LISTED = 20


def task_number(task):
    try:
        return (0, int(task.get("id")))
    except (TypeError, ValueError):
        return (1, str(task.get("id")))


def task_progress(profiles, ids):
    """A chat's task list (Claude's TaskCreate/TaskUpdate keep it as
    <cfg>/tasks/<session id>/<n>.json), or None: {"done", "total", "current"
    (what it's on, e.g. "Running the tests"), "items": [{"subject", "status"}]},
    status being pending, in_progress or completed."""
    for sid in dict.fromkeys(i for i in ids if valid_id(i)):
        for p in profiles:
            try:
                files = sorted((p.path / "tasks" / sid).glob("*.json"))
            except OSError:
                continue
            tasks = [t for t in map(read_json, files)
                     if isinstance(t, dict) and isinstance(t.get("subject"), str) and t.get("status") != "deleted"]
            if not tasks:
                continue
            tasks.sort(key=task_number)
            current = next((t for t in tasks if t.get("status") == "in_progress"), None)
            return {
                "done": sum(t.get("status") == "completed" for t in tasks),
                "total": len(tasks),
                "current": clean_text((current.get("activeForm") or current["subject"]) if current else "", 120),
                "items": [{"subject": clean_text(t["subject"], 120), "status": str(t.get("status") or "pending")}
                          for t in tasks[:TASKS_LISTED]],
            }
    return None


def own_folders(profiles):
    """Folders a chat writes to that aren't its project: Claude's config
    folders (plans, memory) and its scratchpads (/tmp/claude-<uid>)."""
    skip = {f"/tmp/claude-{os.getuid()}"}
    for p in profiles:
        skip.update((str(p.path).rstrip("/"), str(p.real).rstrip("/")))
    return sorted(skip)


CACHE_KEEP_S = 2 * 86400  # a cache entry no copy of this script used for this long is dropped
SEEN_EVERY_S = 3600  # how often an entry's last use is written down


def keep_unused(old, new, sections, now):
    """Put the entries of the cache old that this copy didn't use into new.

    Other copies of this script share the cache file but may list other
    chats (a widget with other settings, the VS Code extension, a poll from
    another computer): when each saved only the entries it used, they kept
    reading each other's transcripts from the start. new["seen"] notes when
    each entry was last used (in whole hours or so); one that no copy used
    for CACHE_KEEP_S is dropped. Whether that changed anything to save."""
    seen_old = old.get("seen") if isinstance(old.get("seen"), dict) else {}
    seen, changed = {}, False
    for section in sections:
        old_entries = old.get(section) if isinstance(old.get(section), dict) else {}
        was = seen_old.get(section) if isinstance(seen_old.get(section), dict) else {}
        noted = seen[section] = {}
        for key in new[section]:
            t = was.get(key)
            if isinstance(t, (int, float)) and 0 <= now - t < SEEN_EVERY_S:
                noted[key] = t
            else:
                noted[key] = int(now)
                changed = True
        for key, value in old_entries.items():
            if key in new[section]:
                continue
            t = was.get(key)
            t = t if isinstance(t, (int, float)) else now  # from a copy that doesn't note it
            if now - t < CACHE_KEEP_S:
                new[section][key] = value
                noted[key] = int(t)
            else:
                changed = True
    new["seen"] = seen
    return changed


class ChatCache:
    """What the widget read from the listed chats' transcripts, kept between
    polls in chats.json: each one's peek (read again when the file changed)
    and the files it changed (only what was appended is read)."""

    def __init__(self, path):
        self.path = path
        data = read_json(path)
        ok = isinstance(data, dict) and data.get("version") == CHAT_CACHE_VERSION
        self.old = data if ok else {}
        self.data = {"version": CHAT_CACHE_VERSION, "peeks": {}, "edits": {}}
        self.changed = not ok

    def _old(self, section, key):
        return self.data[section].get(key) or self.old.get(section, {}).get(key)

    def peek(self, path):
        try:
            st = path.stat()
        except (OSError, AttributeError):
            return None
        key = file_key(st)
        old = self._old("peeks", key)
        # (an older copy of this script reads no recap, mode, slug or stop: read it again)
        if isinstance(old, list) and len(old) == 3 and old[0] == st.st_size and old[1] == st.st_mtime_ns \
                and (not isinstance(old[2], dict) or all(k in old[2] for k in PEEK_ADDED)):
            self.data["peeks"][key] = old
            return old[2]
        peek = chat_peek(path)
        self.data["peeks"][key] = [st.st_size, st.st_mtime_ns, peek]
        self.changed = True
        return peek

    def edits(self, path):
        """new_edits of one transcript, reading only what was appended since last time."""
        try:
            st = path.stat()
        except OSError:
            return new_edits()
        key = file_key(st)
        old = self._old("edits", key)
        if isinstance(old, dict) and isinstance(old.get("size"), int) and old["size"] <= st.st_size \
                and isinstance(old.get("rec"), dict) and isinstance(old["rec"].get("stats"), dict):
            size, rec = old["size"], old["rec"]
            # An older copy of this script (1.8 and before) reads on without
            # counting what "stats" counts, but keeps the key: count from
            # where it was counted last (reading the edits again is harmless)
            counted = rec.get("statsAt")
            if isinstance(counted, int) and 0 <= counted < size:
                size = counted
        else:
            size, rec = 0, new_edits()
        if size < st.st_size:
            rec = json.loads(json.dumps(rec))  # a copy: the old one may be kept as is
            size = scan_edits(path, rec, size)
            rec["statsAt"] = size
            self.changed = True
        self.data["edits"][key] = {"size": size, "rec": rec}
        return rec

    def stats(self, path):
        """The chat's recap (stats_summary), subagents included, or None."""
        if path is None:
            return None
        main = self.edits(path).get("stats") or new_stats()
        return stats_summary(main, [self.edits(sub).get("stats") or new_stats() for sub in subagent_transcripts(path)])

    def changed_files(self, path, cwd, skip):
        """The absolute paths of the files a chat changed, subagents included."""
        if path is None:
            return []
        rec = new_edits()
        merge_edits(rec, self.edits(path))
        for sub in subagent_transcripts(path):
            merge_edits(rec, self.edits(sub))
        return changed_paths(rec, cwd, skip)

    def changes(self, path, cwd, home, skip, paths=None):
        """{"count", "files"} of the files a chat changed (paths: changed_files,
        if known), or None."""
        paths = self.changed_files(path, cwd, skip) if paths is None else paths
        if not paths:
            return None
        labels = sorted(file_label(p, cwd, home) for p in paths)
        return {"count": len(paths), "files": labels[:CHANGES_LISTED]}

    def save(self):
        sections = ("peeks", "edits")
        kept = keep_unused(self.old, self.data, sections, time.time())
        if self.changed or kept or any(set(self.data[s]) != set(self.old.get(s, {})) for s in sections):
            try:
                write_json_atomic(self.path, self.data)
            except OSError:
                pass


# ---------------------------------------------------------------- permission prompts
#
# A chat waiting for a permission shows what it asks (the command, the file
# and the change) and, where the widget can answer, Allow once, Always allow
# and Deny. Without the hook, what it asks comes from the transcript: its
# newest tool call without a result (pending_tool), and a chat in tmux can be
# answered with keys (pane.py --keys). With the hook (permission_hook.py,
# pane.py --relay on), each request is in the cache (permission_requests)
# and the widget's answer goes back through it (pane.py --answer).
#
# Claude's multiple-choice questions (its AskUserQuestion tool) ask for a
# permission the same way, and the hook gets them too: the chat waits for
# "input needed" then, and its "questions" can be answered through the hook
# only (an allow that carries the answers, as Claude's own dialog sends them).

DIFF_LINES = 400  # of one file's diff, in the review (and of an edit a prompt asks for)
DIFF_CHARS = 60000
PERMISSIONS = "permissions"
REQUEST_MAX_AGE_MS = 86400_000
ASK_TOOL = "AskUserQuestion"
QUESTION_CHARS = 2000  # of a question, an option's label or its description
# A chat leaving plan mode (its ExitPlanMode call) asks the same way, with
# its plan: approve it (and maybe accept edits from then on) or keep
# planning, with what to change (pane.py --decision approve|approve-edits|keep;
# verified on Claude Code 2.1.311: an allow must carry the call's input, a
# setMode in updatedPermissions sets the mode, a deny's message is the feedback)
PLAN_TOOL = "ExitPlanMode"
PLAN_CHARS = 60000  # of a plan, in the row


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def read_file(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


HUNK_LINE = re.compile(r"^@@ -(\d+)(,\d+)? \+(\d+)(,\d+)? @@")


def edit_diff(name, inp, current, limit=DIFF_LINES):
    """The lines of a unified diff of the change an Edit, MultiEdit, Write or
    NotebookEdit call asks for (current: the file's text now, for Write and
    for an Edit's line numbers); the first limit of them (None: all)."""
    import difflib
    if name == "Edit":
        old, new = inp.get("old_string"), inp.get("new_string")
        pairs = [(old, new)] if isinstance(old, str) and isinstance(new, str) else []
    elif name == "MultiEdit":
        pairs = [(e.get("old_string"), e.get("new_string")) for e in inp.get("edits") or []
                 if isinstance(e, dict) and isinstance(e.get("old_string"), str) and isinstance(e.get("new_string"), str)]
    elif name == "Write":
        pairs = [(current or "", inp.get("content"))] if isinstance(inp.get("content"), str) else []
    elif name == "NotebookEdit":
        pairs = [("", inp.get("new_source"))] if isinstance(inp.get("new_source"), str) else []
    else:
        pairs = []
    out = []
    for old, new in pairs:
        diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2))
        # The @@ lines at the file's line numbers: a Write's are; an Edit's
        # are the change's own, moved to where its text is in the file
        # (current), else left out ("@@ @@")
        shift = 0 if name == "Write" else None
        if name == "Edit" and current and old and old in current:
            shift = current[:current.index(old)].count("\n")
        for line in diff[2:]:
            m = HUNK_LINE.match(line)
            if m:
                line = "@@ @@" if shift is None else "@@ -%d%s +%d%s @@" % (
                    int(m.group(1)) + shift, m.group(2) or "", int(m.group(3)) + shift, m.group(4) or "")
            out.append(line)
    return out[:limit]


def question_view(inp):
    """The questions of an AskUserQuestion call: [{"question", "header",
    "multiSelect", "options": [{"label", "description"}]}]."""
    def text(value):
        return value[:QUESTION_CHARS] if isinstance(value, str) else ""
    out = []
    for q in inp.get("questions") or [] if isinstance(inp, dict) else []:
        if not isinstance(q, dict) or not text(q.get("question")):
            continue
        options = [{"label": text(o.get("label")), "description": text(o.get("description"))}
                   for o in q.get("options") or [] if isinstance(o, dict) and text(o.get("label"))]
        out.append({"question": text(q["question"]), "header": text(q.get("header")),
                    "multiSelect": q.get("multiSelect") is True, "options": options})
    return out


def permission_view(tool, inp, cwd, suggestions=()):
    """What a permission prompt asks, as the pane and the row show it:
    {"tool", "line" (one line), "command" (a Bash command), "file", "diff"
    (lines), "always" (what Always allow lets through, or ""), "questions"
    (Claude's multiple-choice questions: question_view)}."""
    inp = inp if isinstance(inp, dict) else {}
    view = {"tool": tool or "", "line": describe_tool({"name": tool, "input": inp}), "command": "", "file": "",
            "diff": [], "always": "", "questions": [], "plan": None}
    if tool == PLAN_TOOL:
        view["plan"] = plan_view(inp)
        view["line"] = "Plan: " + view["plan"]["title"] if view["plan"]["title"] else "A plan, ready to start"
        return view
    if tool == ASK_TOOL:
        view["questions"] = question_view(inp)
        if view["questions"]:
            view["line"] = short_text(view["questions"][0]["question"], 120)
        return view
    if tool == "Bash" and isinstance(inp.get("command"), str):
        view["command"] = inp["command"]
    path = inp.get("file_path") or inp.get("notebook_path")
    if isinstance(path, str) and path:
        view["file"] = file_label(path, cwd or "", Path.home())
        current = None
        if tool in ("Write", "Edit"):
            data = read_file(path) if tool == "Write" or file_size(path) < (2 << 20) else None
            current = data.decode("utf-8", "replace") if data is not None else ""
        view["diff"] = edit_diff(tool, inp, current)
    suggestions = [s for s in suggestions or () if isinstance(s, dict)]
    rules = [r for s in suggestions if s.get("type") == "addRules" for r in s.get("rules") or () if isinstance(r, dict)]
    dirs = [d for s in suggestions if s.get("type") == "addDirectories"
            for d in s.get("directories") or () if isinstance(d, str)]
    modes = [s.get("mode") for s in suggestions if s.get("type") == "setMode" and isinstance(s.get("mode"), str)]
    if rules:
        view["always"] = ", ".join(f"{r.get('toolName')}({r['ruleContent']})" if r.get("ruleContent")
                                   else str(r.get("toolName")) for r in rules)
    elif dirs:
        view["always"] = "the folder " + ", ".join(short_path(d, Path.home()) for d in dirs)
    elif modes:
        view["always"] = f"everything in {modes[0]} mode"
    return view


def plan_view(inp):
    """The plan of an ExitPlanMode call: {"text" (Markdown), "cut" (longer
    than PLAN_CHARS), "file" (its plan file), "title" (its first heading or
    line)}; the text from the plan file when the call doesn't carry it."""
    text = inp.get("plan") if isinstance(inp.get("plan"), str) else ""
    path = inp.get("planFilePath") if isinstance(inp.get("planFilePath"), str) else ""
    if not text.strip() and path:
        data = read_file(path)
        text = data.decode("utf-8", "replace") if data is not None else ""
    title = ""
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            title = short_text(line, 100)
            break
    return {"text": text[:PLAN_CHARS], "cut": len(text) > PLAN_CHARS, "file": path, "title": title}


def pending_tool(path):
    """The newest tool call of a chat's last turn that has no result yet
    (the one a permission prompt is about while the chat waits), or None."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - PEEK_TAIL_BYTES))
            lines = f.read(min(size, PEEK_TAIL_BYTES)).split(b"\n")
    except (OSError, AttributeError):
        return None
    if size > PEEK_TAIL_BYTES:
        lines = lines[1:]
    answered = set()
    for line in reversed(lines):
        if not PEEK_LINE.search(line):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("isSidechain") or e.get("type") not in ("user", "assistant"):
            continue
        _, blocks = blocks_of(e)
        if e["type"] == "user":
            if human_prompt(e) is not None:
                return None
            answered.update(c.get("tool_use_id") for c in blocks if c.get("type") == "tool_result")
            continue
        for c in reversed([c for c in blocks if c.get("type") == "tool_use"]):
            if c.get("id") not in answered:
                return c
    return None


def pending_permission(path, cwd):
    """permission_view of what a waiting chat asks, from its transcript, or None."""
    use = pending_tool(path) if path is not None else None
    return permission_view(use.get("name"), use.get("input"), cwd) if use else None


def requests_dir(cache_root):
    return cache_root / PERMISSIONS


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def permission_requests(cache_root, now_ms=None):
    """{session id: its oldest open request} of the requests the hook wrote
    (relayed), whose hook still waits; the files of the others are removed."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    folder = requests_dir(cache_root)
    try:
        names = os.listdir(folder)
    except OSError:
        return {}
    out = {}
    for name in sorted(names):
        if not name.endswith(".json") or name.endswith(".answer.json"):
            continue
        path = folder / name
        req = read_json(path)
        stale = not isinstance(req, dict) or not pid_alive(req.get("hookPid")) \
            or now_ms - (req.get("at") or 0) > REQUEST_MAX_AGE_MS
        if stale:
            for p in (path, folder / (name[:-5] + ".answer.json")):
                try:
                    p.unlink()
                except OSError:
                    pass
            continue
        sid = req.get("sessionId")
        if valid_id(sid or "") and (sid not in out or req.get("at", 0) < out[sid].get("at", 0)):
            out[sid] = req
    return out


def request_view(req):
    """A relayed request as a row carries it: permission_view plus its id."""
    view = permission_view(req.get("tool"), req.get("input"), req.get("cwd") or "", req.get("suggestions"))
    view["request"] = req.get("id")
    return view


def note_permissions(groups, ctx, requests):
    """A chat waiting for a permission (or with a question for you) gets
    "permission": what it asks (request_view, or pending_permission without
    the hook) and "answer": how the widget can answer it ("hook", "keys" in
    tmux, or ""; a question: "hook" or "")."""
    for g in groups:
        for s in g["sessions"]:
            s["permission"] = None
            wait = s.get("waitingFor")
            if s["state"] != "waiting" or wait not in PERMISSION_WAITS:
                continue
            req = requests.get(s["sessionId"])
            if req is not None:
                if wait != QUESTION_WAIT or req.get("tool") == ASK_TOOL:
                    s["permission"] = dict(request_view(req), answer="hook")
                continue
            profile = next((p for p in ctx.profiles if p.key == s.get("profileDir")), None)
            path = transcript_path(profile, s.get("chatId") or s["sessionId"]) if profile is not None else None
            view = pending_permission(path, s.get("cwd") or "")
            if view is not None and (wait != QUESTION_WAIT or view["tool"] == ASK_TOOL):
                question = view["tool"] == ASK_TOOL
                keys = s.get("tmux") and not question and view["tool"] != PLAN_TOOL   # (a plan's dialog differs by account)
                s["permission"] = dict(view, request=None, answer="keys" if keys else "")


QUESTION_WAIT = "input needed"  # (also an MCP server's question: only AskUserQuestion's count)
PERMISSION_WAITS = ("permission prompt", QUESTION_WAIT)


# ---------------------------------------------------------------- live mode
#
# The live hook (live_hook.py, which pane.py --live on puts in Claude's
# settings) folds each chat's events into its live file,
# ~/.cache/talpy-den/live/<session id>.json, as they happen: what the
# chat does now, its last steps, its running subagents, what it said last.
# A running chat's row carries it ("live"), and the widget follows the files
# between two checks (live.py --wait; the VS Code extension: fs.watch).

LIVE = "live"
LIVE_HOOK_FILE = "live-hook.py"
LIVE_FIELDS = ("state", "doing", "asks", "at", "turnAt", "steps", "agents", "said", "saidAt", "failed")


def live_hook_on(home):
    """Whether the live hook is on (pane.py --live on keeps its copy here)."""
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return (data / "talpy-den" / LIVE_HOOK_FILE).is_file()


def live_view(cache_root, sid, pid):
    """A running chat's live file as its row carries it, or None (none, or
    another process's: the chat went on in another one)."""
    if not valid_id(sid or "") or not pid:
        return None
    view = read_json(cache_root / LIVE / (sid + ".json"))
    if not isinstance(view, dict) or view.get("state") == "ended" or view.get("pid") not in (None, pid):
        return None
    return {k: view.get(k) for k in LIVE_FIELDS}


def note_live(groups, cache_root):
    for g in groups:
        for s in g["sessions"]:
            s["live"] = live_view(cache_root, s["sessionId"], s["pid"])


def reply_route(row):
    """How a chat takes a message: "tmux" (typed into its pane), "vscode"
    (its VS Code tab's input), "background" (claude --bg --resume: a chat
    with no process, or a background session) or "terminal" (a terminal
    that takes no text from outside: going on in the background or the
    clipboard)."""
    if not row.get("pid") or row.get("kind") == "bg":
        return "background"
    if row.get("entrypoint") == "claude-vscode":
        return "vscode"
    return "tmux" if row.get("tmux") else "terminal"


# ---------------------------------------------------------------- tests after the last edit, reviewed chats
#
# A finished chat that changed files is "to review" until you mark it
# reviewed (reviewed.json: {"chats": {"[host|]chat id": when}}), or again
# once it changes files after that. Its row says whether tests ran after its
# last edit and how they went: a Bash command that looks like a test run
# (TEST_COMMAND), and its result (an exit code other than 0 is an error).

REVIEWED = "reviewed.json"
REVIEWED_KEEP_MS = 60 * 86400_000
TESTS_CACHE_VERSION = 1
TEST_COMMAND = re.compile(
    r"(?:^|[\s;&|(/])(?:py\.?test|pytest|unittest|nose2|tox|nox|jest|vitest|mocha|ava|karma|rspec|phpunit|ctest"
    r"|bats|go\s+test|cargo\s+(?:test|nextest)|deno\s+test|bun\s+test|node\s+--test|dotnet\s+test|mix\s+test"
    r"|swift\s+test|zig\s+build\s+test|meson\s+test|ninja\s+test|playwright\s+test|cypress\s+run"
    r"|(?:npm|pnpm|yarn)\s+(?:run\s+)?test\S*|make\s+(?:\S+\s+)*(?:test|check)\b|mvnw?\s+(?:\S+\s+)*test"
    r"|(?:\./)?gradlew?\s+(?:\S+\s+)*test|(?:run_?)?tests?\.(?:sh|py))(?=$|[\s;&|)'\"])")
BASH_LINE = re.compile(rb'"name":\s*"Bash"')


def review_key(chat_id, host=""):
    return f"{host}|{chat_id}" if host else chat_id


def load_reviewed(cache_root):
    """{review_key: when} of the chats marked reviewed (0: marked not
    reviewed, also if their last edit came before reviewed_since)."""
    data = read_json(cache_root / REVIEWED)
    chats = data.get("chats") if isinstance(data, dict) else None
    return {k: v for k, v in chats.items() if isinstance(v, (int, float))} if isinstance(chats, dict) else {}


def reviewed_since(cache_root, now_ms):
    """Since when chats are to review: what they changed before the widget
    first looked isn't (else every chat that ever changed a file would be)."""
    data = read_json(cache_root / REVIEWED)
    if isinstance(data, dict) and isinstance(data.get("since"), (int, float)):
        return data["since"]
    try:
        private_dir(cache_root)
        write_json_atomic(cache_root / REVIEWED, {"since": now_ms, "chats": load_reviewed(cache_root)})
    except OSError:
        pass
    return now_ms


def new_tests():
    """A transcript's last edit (editAt, ms) and its newest test run
    ({"command", "ok": True/False, "at"}), with the test runs that have no
    result yet (pending: {tool id: [command, at]})."""
    return {"editAt": None, "run": None, "pending": {}}


QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\\\]|\\\\.)*\"")


def is_test_run(command):
    """Does a Bash command run tests? Its first line, quotes left out (not
    a script it pipes in, nor a commit message that names a test tool)."""
    if not isinstance(command, str):
        return False
    return bool(TEST_COMMAND.search(QUOTED.sub("''", command.split("\n", 1)[0])))


def fold_tests(rec, e):
    kind = e.get("type")
    at = to_ms(e.get("timestamp"))
    if kind == "assistant":
        for c in blocks_of(e)[1]:
            if c.get("type") != "tool_use" or not isinstance(c.get("input"), dict):
                continue
            if c.get("name") in EDIT_TOOLS:
                rec["editAt"] = at if rec["editAt"] is None or (at or 0) >= rec["editAt"] else rec["editAt"]
            elif c.get("name") == "Bash" and is_test_run(c["input"].get("command")) and isinstance(c.get("id"), str):
                rec["pending"][c["id"]] = [short_text(c["input"]["command"].split("\n", 1)[0], 120), at]
    elif kind == "user":
        for c in blocks_of(e)[1]:
            if c.get("type") == "tool_result" and c.get("tool_use_id") in rec["pending"]:
                command, started = rec["pending"].pop(c["tool_use_id"])
                rec["run"] = {"command": command, "ok": not c.get("is_error"), "at": at or started}


def scan_tests(path, rec, start=0):
    """Fold a transcript's lines from byte offset start into rec (new_tests);
    returns the offset read up to."""
    offset = start
    try:
        with open(path, "rb") as f:
            f.seek(start)
            for line in f:
                if not line.endswith(b"\n"):
                    break
                offset += len(line)
                wanted = (ASSISTANT_LINE.search(line) and (EDIT_LINE.search(line) or BASH_LINE.search(line))) or (
                    rec["pending"] and b'"tool_result"' in line
                    and any(i.encode() in line for i in rec["pending"]))
                if not wanted:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and not e.get("isSidechain"):
                    fold_tests(rec, e)
    except OSError:
        pass
    return offset


def merge_tests(recs):
    """{"editAt", "run"} of a chat from its transcript's and its subagents'
    (newest edit; newest run), run None if it came before the last edit."""
    edit = max((r["editAt"] for r in recs if r["editAt"] is not None), default=None)
    runs = [r["run"] for r in recs if r["run"]]
    run = max(runs, key=lambda r: r.get("at") or 0) if runs else None
    if run is not None and edit is not None and (run.get("at") or 0) < edit:
        run = None
    return {"editAt": edit, "run": run}


def chat_tests(path):
    """merge_tests of a chat, read in full (pane.py --review)."""
    recs = []
    for p in [path] + subagent_transcripts(path):
        rec = new_tests()
        scan_tests(p, rec)
        recs.append(rec)
    return merge_tests(recs)


class TestsCache:
    """new_tests of the transcripts of listed and recent chats that changed
    files, kept between polls in tests.json (only what was appended is read)."""

    def __init__(self, path):
        self.path = path
        data = read_json(path)
        ok = isinstance(data, dict) and data.get("version") == TESTS_CACHE_VERSION
        self.old = data.get("files", {}) if ok and isinstance(data.get("files"), dict) else {}
        self.data = {}
        self.changed = not ok

    def rec(self, path):
        try:
            st = path.stat()
        except OSError:
            return new_tests()
        key = file_key(st)
        old = self.data.get(key) or self.old.get(key)
        if isinstance(old, list) and len(old) == 2 and isinstance(old[0], int) and old[0] <= st.st_size \
                and isinstance(old[1], dict) and all(k in old[1] for k in ("editAt", "run", "pending")):
            size, rec = old
        else:
            size, rec = 0, new_tests()
        if size < st.st_size:
            rec = json.loads(json.dumps(rec))
            size = scan_tests(path, rec, size)
            self.changed = True
        self.data[key] = [size, rec]
        return rec

    def review(self, path):
        """{"editedAt", "tests"} of a chat (merge_tests), or None."""
        if path is None:
            return None
        merged = merge_tests([self.rec(p) for p in [path] + subagent_transcripts(path)])
        return {"editedAt": merged["editAt"], "tests": merged["run"]} if merged["editAt"] is not None else None

    def save(self):
        if self.changed or set(self.data) != set(self.old):
            try:
                write_json_atomic(self.path, {"version": TESTS_CACHE_VERSION, "files": self.data})
            except OSError:
                pass


# ---------------------------------------------------------------- shared chats
#
# A chat shared across profiles is one transcript file hard-linked into each
# profile's projects/ folder (as sharing.py does). Every row knows which
# conversation it shows (chatId: the transcript it writes) and how that
# relates to a shared chat:
#   - the shared chat itself, maybe through a background session of its own
#     whose own file holds no messages (`continues`);
#   - a copy: Claude forked the shared chat into a transcript of its own, which
#     starts with the shared chat's history (`forkOf`);
#   - nothing to do with it (a new conversation).


def transcript_path(profile, session_id):
    if not valid_id(session_id):
        return None
    return next(iter(profile.path.glob(f"projects/*/{session_id}.jsonl")), None)


def chat_folder(path):
    """The folder a chat last ran in (the last "cwd" in its transcript),
    read from the end (further back while the last lines are huge ones)."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    nbytes = PEEK_TAIL_BYTES
    while True:
        try:
            with open(path, "rb") as f:
                f.seek(max(0, size - nbytes))
                lines = f.read(min(size, nbytes)).splitlines()
        except OSError:
            return None
        for line in reversed(lines[1:] if size > nbytes else lines):  # the first may be cut
            if b'"cwd"' not in line:
                continue
            try:
                cwd = json.loads(line).get("cwd")
            except (ValueError, AttributeError):
                continue
            if isinstance(cwd, str) and cwd.startswith("/"):
                return cwd
        if nbytes >= size or nbytes >= PEEK_TAIL_MAX:
            return None
        nbytes *= 4


def start_folder(path):
    """The folder a chat started in (its first "cwd"), when Claude Code files
    it under that folder (projects/<the folder, mangled>); later ones follow
    Claude's `cd`s."""
    try:
        with open(path, "rb") as f:
            head = f.read(PEEK_TAIL_BYTES)
    except OSError:
        return None
    for line in head.splitlines():  # (a cut last line doesn't parse)
        if b'"cwd"' not in line:
            continue
        try:
            cwd = json.loads(line).get("cwd")
        except (ValueError, AttributeError):
            continue
        if isinstance(cwd, str) and cwd.startswith("/"):
            return cwd if mangle(cwd) == path.parent.name else None
    return None


def shared_info(session_id, profile, all_profiles):
    """Is this chat shared across profiles? Returns {"profiles": [labels],
    "openIn": label holding it open or None}, or None."""
    own = transcript_path(profile, session_id)
    try:
        st = own.stat() if own else None
    except OSError:
        return None
    if st is None or st.st_nlink < 2:
        return None
    labels = [profile.label]
    for other in all_profiles:
        if other is profile:
            continue
        for t in other.path.glob(f"projects/*/{session_id}.jsonl"):
            try:
                o = t.stat()
            except OSError:
                continue
            if (o.st_dev, o.st_ino) == (st.st_dev, st.st_ino) and other.label not in labels:
                labels.append(other.label)
    if len(labels) < 2:
        return None
    return {"profiles": labels, "openIn": shared_holder(session_id, all_profiles)}


MESSAGE_LINE = re.compile(rb'"type":\s*"(?:user|assistant)"')


def has_own_messages(profile, session_id):
    """Does this session's own transcript hold any conversation?"""
    path = transcript_path(profile, session_id)
    if path is None:
        return False
    try:
        with open(path, "rb") as f:
            return any(MESSAGE_LINE.search(line) for line in f)
    except OSError:
        return False


def file_key(st):
    return f"{st.st_dev}:{st.st_ino}"


class LinkCache:
    """What the widget learned about transcripts, kept between polls in
    links.json, so big shared transcripts are only read where they grew:

    - for each shared transcript: its `continued-in` records (Claude writes one
      when the chat moves on to another session) and where its last finished
      reply is, which tells whether the chat moved on (Claude's own rule);
    - for each other transcript: the uuid of its first entry, and whether a
      shared transcript holds that entry (then it's a copy of that chat).
    """

    VERSION = 2

    def __init__(self, path):
        self.path = path
        data = read_json(path)
        ok = isinstance(data, dict) and data.get("version") == self.VERSION
        self.old = data if ok else {}
        self.data = {"version": self.VERSION, "transcripts": {}, "firstUuid": {}, "holds": {}, "chats": {}}
        self.changed = not ok

    def _keep(self, section, key, value):
        if self.old.get(section, {}).get(key) != value:
            self.changed = True
        self.data[section][key] = value

    def transcript(self, path):
        """{"size", "conts": [[session id, offset]], "final": offset} of a
        shared transcript, scanning only what was appended since last time."""
        try:
            st = path.stat()
        except OSError:
            return None
        key = file_key(st)
        rec = self.data["transcripts"].get(key) or self.old.get("transcripts", {}).get(key)
        if not (isinstance(rec, dict) and isinstance(rec.get("size"), int) and rec["size"] <= st.st_size):
            rec = {"size": 0, "conts": [], "final": -1}
        if rec["size"] < st.st_size:
            rec = dict(rec, conts=list(rec["conts"]))
            try:
                with open(path, "rb") as f:
                    f.seek(rec["size"])
                    chunk = f.read(st.st_size - rec["size"])
            except OSError:
                chunk = b""
            end = chunk.rfind(b"\n")  # never consume a half-written last line
            if end >= 0:
                offset = rec["size"]
                for line in chunk[:end + 1].splitlines(keepends=True):
                    if b'"continued-in"' in line or b'"stop_reason":"' in line:
                        try:
                            e = json.loads(line)
                        except ValueError:
                            e = None
                        if isinstance(e, dict):
                            if e.get("type") == "continued-in" and valid_id(e.get("continuedInSessionId")):
                                rec["conts"].append([e["continuedInSessionId"], offset])
                            elif finished_reply(e):
                                rec["final"] = offset
                    offset += len(line)
                rec["size"] = offset
        self._keep("transcripts", key, rec)
        return rec

    def first_uuid(self, path):
        """uuid of the first entry that has one ('' if none in the first 256 KB)."""
        try:
            st = path.stat()
        except OSError:
            return ""
        key = file_key(st)
        cached = self.data["firstUuid"].get(key, self.old.get("firstUuid", {}).get(key))
        if isinstance(cached, str) and cached:
            self._keep("firstUuid", key, cached)
            return cached
        found = ""
        try:
            with open(path, "rb") as f:
                head = f.read(262144)
        except OSError:
            head = b""
        for line in head.splitlines()[:-1] if not head.endswith(b"\n") else head.splitlines():
            if b'"uuid"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and isinstance(e.get("uuid"), str) and e["uuid"]:
                found = e["uuid"]
                break
        self._keep("firstUuid", key, found)
        return found

    def holds(self, shared_path, uuid):
        """Does this shared transcript contain the entry with this uuid? Once
        known it stays true; a copy's first entry is in the chat before the
        copy exists, so a no stays a no too (per file)."""
        try:
            st = shared_path.stat()
        except OSError:
            return False
        key = f"{uuid}|{file_key(st)}"
        cached = self.data["holds"].get(key, self.old.get("holds", {}).get(key))
        if isinstance(cached, bool):
            self._keep("holds", key, cached)
            return cached
        found = False
        try:
            with open(shared_path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
                found = m.find(f'"uuid":"{uuid}"'.encode()) >= 0
        except (OSError, ValueError):
            pass
        self._keep("holds", key, found)
        return found

    def chat_info(self, path, st):
        """(name, cwd, has messages) of a transcript, read again only when it grew."""
        key = file_key(st)
        cached = self.data["chats"].get(key, self.old.get("chats", {}).get(key))
        if isinstance(cached, list) and len(cached) == 4 and cached[0] == st.st_size:
            self._keep("chats", key, cached)
            return cached[1], cached[2], cached[3]
        name, cwd, messages = read_chat_info(path, st.st_size)
        self._keep("chats", key, [st.st_size, name, cwd, messages])
        return name, cwd, messages

    def save(self):
        sections = ("transcripts", "firstUuid", "holds", "chats")
        kept = keep_unused(self.old, self.data, sections, time.time())
        if self.changed or kept or any(set(self.data[s]) != set(self.old.get(s, {})) for s in sections):
            try:
                write_json_atomic(self.path, self.data)
            except OSError:
                pass


def finished_reply(e):
    """Claude's test for a finished assistant reply (its transcript `I()`)."""
    msg = e.get("message")
    return (e.get("type") == "assistant" and e.get("isApiErrorMessage") is not True
            and isinstance(msg, dict) and isinstance(msg.get("stop_reason"), str))


def shared_transcripts(folder):
    """Shared (hard-linked) transcripts in a project folder."""
    try:
        files = sorted(folder.glob("*.jsonl"))
    except OSError:
        return []
    out = []
    for t in files:
        try:
            if t.stat().st_nlink > 1:
                out.append(t)
        except OSError:
            continue
    return out


def project_folders(profile, ids, cwd):
    """Project folders (projects/<key>) a session's transcripts can be in."""
    folders = []
    for sid in ids:
        path = transcript_path(profile, sid)
        if path is not None:
            folders.append(path.parent)
    if cwd:
        folders.append(profile.path / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd))
    return list(dict.fromkeys(f for f in folders if f.is_dir()))


def continued_from(profile, ids, cwd, ctx):
    """The shared chat whose `continued-in` record names one of these ids (a
    background session that took the chat over gets an id of its own)."""
    wanted = {i for i in ids if valid_id(i)}
    for folder in project_folders(profile, ids, cwd):
        for t in shared_transcripts(folder):
            rec = ctx.links.transcript(t)
            if rec and any(c[0] in wanted for c in rec["conts"]) and t.stem not in wanted:
                return t.stem
    return None


def copied_from(profile, session_id, ctx):
    """The shared chat this session's transcript is a copy of: Claude forked it
    and the copy starts with the chat's history (its first entry's uuid is in
    the shared transcript)."""
    path = transcript_path(profile, session_id)
    if path is None:
        return None
    uuid = ctx.links.first_uuid(path)
    if not uuid:
        return None
    for t in shared_transcripts(path.parent):
        if t != path and ctx.links.holds(t, uuid):
            return t.stem
    return None


def chat_link(row, job, profile, ctx):
    """(chatId, shared info or None) of a row.

    chatId is the conversation the row shows: its own transcript, or, when its
    own file holds no messages, the chat it took over (the job's
    resumeSessionId, else the chat whose `continued-in` record names it).
    """
    own = row["sessionId"]
    job = job if isinstance(job, dict) else {}
    chat, continues = own, None
    if not has_own_messages(profile, own):
        target = job.get("resumeSessionId") if valid_id(job.get("resumeSessionId")) else None
        if target == own:
            target = None
        if target is None:
            target = continued_from(profile, [own, job.get("sessionId")], row["cwd"], ctx)
        if target is not None:
            chat, continues = target, target
    info = shared_info(chat, profile, ctx.profiles)
    if info:
        if continues:
            info["continues"] = continues
        return chat, info
    # Not shared itself: a copy of a shared chat? The job may name it; else
    # its history gives it away.
    origin = None
    for k in ("forkParentSessionId", "resumeSessionId"):
        candidate = job.get(k)
        if valid_id(candidate) and candidate != chat and transcript_path(profile, candidate) is not None:
            origin = candidate
            break
    if origin is None or not shared_info(origin, profile, ctx.profiles):
        origin = copied_from(profile, chat, ctx)
    if origin is not None:
        info = next((i for i in (shared_info(origin, p, ctx.profiles) for p in ctx.profiles) if i), None)
        if info:
            return chat, {**info, "forkOf": origin, "forkOn": profile.label}
    return chat, None


def handed_over(profile, chat, profiles):
    """The chat isn't this profile's any more: it has no transcript of it, and
    another profile has (a shared chat kept only there, sharing.py's
    --unshare). A background record of it left here is stale."""
    if transcript_path(profile, chat) is not None:
        return False
    return any(p.real != profile.real and transcript_path(p, chat) is not None for p in profiles)


def chat_id(session):
    """The conversation a row shows (a background session that took a chat
    over has an id of its own but is that chat)."""
    return session.get("chatId") or session["sessionId"]


def moved_to(chat, ctx):
    """Session id a chat moved on to, by Claude's own rule: its transcript's
    last `continued-in` record comes after its last finished reply."""
    for p in ctx.profiles:
        path = transcript_path(p, chat)
        if path is None:
            continue
        rec = ctx.links.transcript(path)
        if rec:
            after = [c[0] for c in rec["conts"] if c[1] > rec["final"]]
            return after[-1] if after else None
    return None


def chat_activity(chat, ids, ctx):
    """{profile: last time (ms) this chat was used there}, from the status-line
    snapshots, the prompt history and a sharing hook's lock."""
    seen = {}
    ids = [i for i in dict.fromkeys([chat, *ids]) if valid_id(i)]
    for p in ctx.profiles:
        snaps = snapshot_paths(ctx.snapshot_root, p, ids)
        times = [int(snaps[0][0] * 1000)] if snaps else []
        last_prompt = max((ctx.prompts.get(p.real, {}).get(i, 0) for i in ids), default=0)
        if last_prompt:
            times.append(last_prompt)
        if times:
            seen[p] = max(times)
    holder = lock_holder(chat, ctx.profiles)
    if holder is not None:
        p, when = holder
        seen[p] = max(seen.get(p, 0), when)
    return seen


REHOME_SLACK_MS = 60_000


def place_chats(groups, ctx):
    """List each conversation once.

    - A chat that moved on to a copy of itself (Claude's `continued-in` rule)
      isn't listed: the copy is, and accounts sharing the chat get a link to it.
    - A chat that runs somewhere is listed there; its finished background
      records elsewhere are left out (the row notes them as stopped copies).
    - A finished shared chat is listed under the account it was used on last.
      If that account has no record of it (e.g. you used it in a terminal
      there), the newest record moves there as a finished chat ("chat").
    """
    by_profile = {p.key: p for p in ctx.profiles}
    group_of = {key: g for g in groups for key in g["profileKeys"]}
    ctx.moved_on = set()
    rows = {}
    for g in groups:
        for s in g["sessions"]:
            rows.setdefault(chat_id(s) or s["key"], []).append((g, s))

    def is_record(s):  # a finished background record with no process
        return s["state"] == "done" and s["pid"] is None

    def note_stopped(keep, gone):
        for _, k in keep:
            for _, s in gone:
                if s["kind"] == "bg" and s["profile"] != k["profile"]:
                    if k["shared"] is not None:
                        stopped = k["shared"].setdefault("stoppedCopies", [])
                        if s["profile"] not in stopped:
                            stopped.append(s["profile"])

    def drop(entries):
        for g, s in entries:
            if g is not None:
                g["sessions"] = [x for x in g["sessions"] if x is not s]

    # A shared chat that moved on to a listed copy of itself
    forks = {}
    for g in groups:
        for s in g["sessions"]:
            if s["shared"] and s["shared"].get("forkOf"):
                forks.setdefault(s["shared"]["forkOf"], []).append(s)
    for chat, entries in list(rows.items()):
        if not any(s["shared"] and not s["shared"].get("forkOf") for _, s in entries) or chat not in forks:
            continue
        target = moved_to(chat, ctx)
        if target is None:  # it didn't move on (a fork's jobSessionId is often None too)
            continue
        copy = next((f for f in forks[chat] if target in (f["sessionId"], f.get("jobSessionId"))), None)
        if copy is None:
            continue
        if all(is_record(s) for _, s in entries):
            copy["shared"]["movedHere"] = True
            ctx.moved_on.add(chat)
            drop(entries)
            note_stopped([(None, copy)], entries)
            del rows[chat]

    # A finished shared chat that moved on to a chat listed anyway: its copy
    # was shared too, so it's a shared chat of its own rather than a copy
    listed = {i: s for g in groups for s in g["sessions"]
              for i in (s["sessionId"], s.get("jobSessionId"), chat_id(s)) if i}
    for chat, entries in list(rows.items()):
        if not any(s["shared"] for _, s in entries) or not all(is_record(s) for _, s in entries):
            continue
        target = moved_to(chat, ctx)
        if target is None or target == chat or target not in listed or chat_id(listed[target]) == chat:
            continue
        ctx.moved_on.add(chat)
        drop(entries)
        del rows[chat]

    for chat, entries in rows.items():
        shared = next((s["shared"] for _, s in entries if s["shared"] and not s["shared"].get("forkOf")), None)
        active = [(g, s) for g, s in entries if not is_record(s)]
        if active:
            stale = [(g, s) for g, s in entries if is_record(s)]
            drop(stale)
            note_stopped(active, stale)
            continue
        entries = sorted(entries, key=lambda e: -(e[1]["since"] or 0))
        newest_g, newest = entries[0]
        if shared is None:
            drop(entries[1:])
            continue
        # Finished everywhere: which account used it last?
        seen = chat_activity(chat, [i for _, s in entries for i in (s["sessionId"], s.get("jobSessionId"))], ctx)
        for g, s in entries:
            p = by_profile.get(s["profileDir"])
            if p is not None:
                seen[p] = max(seen.get(p, 0), s["since"] or 0)
        listed = {by_profile.get(s["profileDir"]) for _, s in entries}
        home = max(seen, key=lambda p: seen[p]) if seen else None
        if home is None or home in listed or home.key not in group_of \
                or seen[home] - max((seen.get(p, 0) for p in listed if p is not None), default=0) < REHOME_SLACK_MS:
            keep_p = home if home in listed else by_profile.get(newest["profileDir"])
            keep = next(((g, s) for g, s in entries if s["profileDir"] == getattr(keep_p, "key", None)), entries[0])
            gone = [e for e in entries if e[1] is not keep[1]]
            drop(gone)
            note_stopped([keep], gone)
            continue
        # Last used on an account with no record of it: list it there
        moved = dict(newest, shared=dict(newest["shared"]) if newest["shared"] else None)
        moved.update({
            "key": f"{home.key}/{chat}/chat", "profileDir": home.key, "profile": home.label,
            "sessionId": chat, "chatId": chat, "pid": None, "kind": "chat", "entrypoint": "",
            "state": "done", "reason": "", "waitingFor": "", "detail": "", "since": seen[home],
            "startedAt": None, "jobId": None, "jobSessionId": None,
            "context": None, "model": None, "costUsd": None,
            "formerIds": [i for _, s in entries for i in (s["sessionId"], s.get("jobSessionId")) if i],
        })
        attach_snapshot(moved, ctx.snapshot_root, home, [chat, *(i for _, s in entries for i in (s["sessionId"], s.get("jobSessionId")))])
        drop(entries)
        if moved["shared"] is not None:
            moved["shared"].pop("stoppedCopies", None)
        note_stopped([(None, moved)], entries)
        group_of[home.key]["sessions"].append(moved)


def link_shared_elsewhere(groups):
    """A shared chat is listed once. Give every other account it's shared with
    a pointer to it (`sharedHere`), unless that account lists the chat itself
    or a copy of it. A chat that moved on to a copy points to the copy."""
    for g in groups:
        g["sharedHere"] = []
    listed = {chat_id(s) for g in groups for s in g["sessions"]}
    pointed = set()  # (account, chat): a chat listed twice (open on two accounts) gets one pointer
    for g in groups:
        for s in g["sessions"]:
            info = s["shared"]
            if not info:
                continue
            copy = bool(info.get("forkOf"))
            if copy and not (info.get("movedHere") and info["forkOf"] not in listed):
                continue
            chat = info["forkOf"] if copy else chat_id(s)
            for h in groups:
                if h is g or not set(h["profiles"]) & set(info["profiles"]):
                    continue
                if any(chat_id(t) == chat or (t["shared"] or {}).get("forkOf") == chat for t in h["sessions"]):
                    continue
                if (id(h), chat) in pointed:
                    continue
                pointed.add((id(h), chat))
                h["sharedHere"].append({
                    "key": f"{s['key']}/shared",
                    "sessionId": s["sessionId"],
                    "name": s["name"],
                    "cwdShort": s["cwdShort"],
                    "state": s["state"],
                    "reason": s["reason"],
                    "since": s["since"],
                    "profile": s["profile"],
                    "profileDir": s["profileDir"],
                    "copy": copy,
                    "stoppedHere": any(p in h["profiles"] for p in info.get("stoppedCopies", [])),
                })


# Hooks can hold a shared chat to one session at a time: the session using it
# writes ~/.local/state/claude-shared-chats/lock-<id> on each prompt and tool
# call. The widget doesn't install such hooks; where they are set up, their
# lock tells it who has the chat open and where it was used last.


def lock_path(session_id):
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "claude-shared-chats"
    return state / f"lock-{session_id}"


def read_lock(session_id):
    """(pid, start, config dir, mtime) from a shared chat's lock
    (`<pid>-<start> <config dir>`), or None."""
    if not valid_id(session_id):
        return None
    path = lock_path(session_id)
    text = read_text(path)
    if not text:
        return None
    holder, _, folder = text.strip().partition(" ")
    pid, _, start = holder.partition("-")
    if not pid.isdigit() or not folder:
        return None
    return int(pid), start or None, folder, mtime(path)


def profile_of(folder, all_profiles):
    try:
        real = Path(folder).resolve()
    except (OSError, RuntimeError):
        return None
    return next((p for p in all_profiles if p.real == real), None)


def shared_holder(session_id, all_profiles):
    """Profile whose session holds a shared chat open, from its lock, if that
    process is still alive."""
    lock = read_lock(session_id)
    if lock is None or not is_alive({"pid": lock[0], "procStart": lock[1]}):
        return None
    p = profile_of(lock[2], all_profiles)
    return p.label if p else None


def lock_holder(session_id, all_profiles):
    """(profile, ms) of the last session that used a shared chat: the lock is
    rewritten on each prompt and tool call, and stays after the session ends."""
    lock = read_lock(session_id)
    if lock is None or lock[3] is None:
        return None
    p = profile_of(lock[2], all_profiles)
    return (p, int(lock[3] * 1000)) if p else None


# ---------------------------------------------------------------- recent chats

RECENT_CHATS = 5
TITLE_READ_BYTES = 65536
TITLE_KINDS = (("custom-title", "customTitle"), ("agent-name", "agentName"), ("ai-title", "aiTitle"),
               ("summary", "summary"))


def read_chat_info(path, size):
    """(name, cwd, has messages) of a transcript, from its first and last 64 KB.
    The name is what you renamed the chat to, else its session name, else
    Claude's title for it, else its first prompt."""
    try:
        with open(path, "rb") as f:
            head = f.read(TITLE_READ_BYTES)
            if size > 2 * TITLE_READ_BYTES:
                f.seek(size - TITLE_READ_BYTES)
            tail = f.read() if size > TITLE_READ_BYTES else b""
    except OSError:
        return "", "", False
    lines = head.splitlines()[:-1 if len(head) == TITLE_READ_BYTES else None]
    if tail:
        lines += tail.splitlines()[1:]
    found, cwd, first_prompt, messages = {}, "", "", False
    for line in lines:
        if not line.startswith(b"{"):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        kind = e.get("type")
        for name, field in TITLE_KINDS:
            if kind == name and isinstance(e.get(field), str) and e[field].strip():
                found[name] = e[field]
        if kind in ("user", "assistant"):
            messages = True
            if isinstance(e.get("cwd"), str) and e["cwd"].startswith("/"):
                cwd = e["cwd"]
            if kind == "user" and not first_prompt and not e.get("isMeta"):
                content = e["message"].get("content") if isinstance(e.get("message"), dict) else None
                if isinstance(content, list):
                    content = next((c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"), "")
                if isinstance(content, str) and content.strip() and not content.lstrip().startswith("<"):
                    first_prompt = content
    name = next((found[k] for k, _ in TITLE_KINDS if k in found), first_prompt)
    return clean_text(name, 80), cwd, messages


def recent_chats(groups, ctx, limit=RECENT_CHATS, starred=frozenset()):
    """Each account's latest chats that aren't listed as a session already
    (group["recent"]), and of those its starred ones (ids in starred),
    however old (group["starred"]). A chat shared across accounts shows up
    under the one it was used on last; of a chat and its copies (same first
    entry), only the newest; a shared chat that moved on to a listed copy,
    not at all."""
    listed = {i for g in groups for s in g["sessions"] for i in (s["sessionId"], s.get("chatId"), s.get("jobSessionId")) if i}
    listed |= getattr(ctx, "moved_on", set())
    by_key = {p.key: p for p in ctx.profiles}
    group_of = {key: g for g in groups for key in g["profileKeys"]}
    candidates = [c for c in all_transcripts(ctx) if c[1].key in group_of and c[2].stem not in listed]
    for g in groups:
        g["recent"], g["starred"] = [], []
    wanted = set(starred)  # (starred chats not found yet)
    if limit <= 0 and not wanted:
        return
    seen, firsts = set(), set()
    # A listed chat's older copies (they start with the same entry) are that
    # chat, e.g. one that moved on to a background session listed now
    for g in groups:
        for s in g["sessions"]:
            p = by_key.get(s.get("profileDir"))
            path = transcript_path(p, chat_id(s)) if p is not None else None
            first = ctx.links.first_uuid(path) if path is not None else ""
            if first:
                firsts.add(first)
    for t, p, f, st in candidates:
        full = all(len(g["recent"]) >= limit for g in groups)
        if full and not wanted:
            break
        star = f.stem in wanted
        if full and not star:
            continue
        inode = (st.st_dev, st.st_ino)
        if inode in seen:
            continue
        seen.add(inode)
        name, cwd, messages = ctx.links.chat_info(f, st)
        if not messages:
            continue
        first = ctx.links.first_uuid(f)
        if first and first in firsts:  # an older copy of a chat already listed
            continue
        firsts.add(first)
        shared = st.st_nlink > 1 and shared_info(f.stem, p, ctx.profiles)
        if shared and moved_to(f.stem, ctx) in listed:
            continue  # it moved on to a session listed already
        home = p
        if shared:  # under the account that used it last
            seen_at = chat_activity(f.stem, [], ctx)
            if seen_at:
                home = max(seen_at, key=lambda q: seen_at[q])
                if home.key not in group_of:
                    home = p
        g = group_of[home.key]
        room = len(g["recent"]) < limit
        if not room and not star:
            continue
        chat = {
            "key": f"{home.key}/{f.stem}/recent",
            "sessionId": f.stem,
            "name": name or f.stem[:8],
            "cwd": cwd,
            "cwdShort": short_path(cwd, ctx.home),
            "lastActive": int(t * 1000),
            "profile": home.label,
            "profileDir": home.key,
            "shared": bool(shared),
            "starred": star,
            **recent_extras(ctx, f, g, cwd),
        }
        if room:
            g["recent"].append(chat)
        if star:
            wanted.discard(f.stem)
            g["starred"].append(dict(chat, key=f"{home.key}/{f.stem}/starred"))


def recent_extras(ctx, path, group, cwd=""):
    """A recent chat's recap and what stopped it, from the end of its
    transcript; the files it changed and its review (TestsCache.review)."""
    chats = getattr(ctx, "chats", None)
    peek = (chats.peek(path) if chats is not None else None) or {}
    stopped = peek.get("stopped")
    files = chats.changed_files(path, cwd, ctx.own_folders) if chats is not None else []
    tests = getattr(ctx, "tests", None)
    return {"recap": peek.get("recap"),
            "stopped": limit_stop(stopped, group.get("limits"), ctx.now_ms) if stopped else None,
            "changes": chats.changes(path, cwd, ctx.home, ctx.own_folders, files) if files else None,
            "review": tests.review(path) if files and tests is not None else None}


# ---------------------------------------------------------------- usage limits that stopped chats


RESET_IN_TEXT = re.compile(r"\bresets (\d{1,2})(?::(\d{2}))?\s*([ap]m)\b", re.IGNORECASE)


def reset_from_text(text, at_ms):
    """When "… · resets 7:10pm (Europe/Berlin)" in Claude's message is (taken
    as this machine's local time), after at_ms; None if it names no time of day."""
    m = RESET_IN_TEXT.search(text or "")
    if not m or not at_ms:
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
    minute = int(m.group(2) or 0)
    if minute > 59:
        return None
    then = datetime.fromtimestamp(at_ms / 1000)
    reset = then.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= then:
        reset += timedelta(days=1)
    return int(reset.timestamp() * 1000)


def limit_stop(stopped, limits, now_ms):
    """A copy of what stopped a chat (stopped_by), with when the usage limit
    it hit resets ("resetsAt") or whether it has since ("over"): from the
    account's plan window of that limit (a window that started after the
    chat stopped is a new one), else from the time in Claude's message."""
    s = dict(stopped, resetsAt=None, over=False)
    if s.get("kind") != "limit":
        return s
    at = s.get("at") or 0
    w = next((w for w in (limits or {}).get("windows", []) if w.get("key") == s.get("window")), None) \
        if s.get("window") else None
    known = False
    if w is not None and w.get("reset"):
        ended = w.get("endedAt")
        if ended is None or at < ended:  # (else that window was over before the chat stopped: says nothing)
            s["over"], known = True, True
    elif w is not None and w.get("resetsAt"):
        hours = window_hours(w["key"])
        if hours and at < w["resetsAt"] - hours * 3600_000:
            s["over"] = True
        else:
            s["resetsAt"] = w["resetsAt"]
        known = True
    if not known:
        guess = reset_from_text(s.get("text"), at)
        if guess is not None:
            s["over"] = guess <= now_ms
            s["resetsAt"] = None if s["over"] else guess
    return s


def note_limit_stops(group, now_ms):
    """The account's chats that a usage limit stopped: when it resets."""
    for s in group["sessions"]:
        if s.get("stopped"):
            s["stopped"] = limit_stop(s["stopped"], group.get("limits"), now_ms)


def note_limit_hits(group):
    """A plan window one of the account's chats hit the limit of after its
    usage was last read is full, whatever that read said (VS Code's panel
    runs no status line: its chats use up a window with no new numbers)."""
    limits = group.get("limits")
    if not limits:
        return
    for s in group["sessions"] + group.get("recent", []):
        stopped = s.get("stopped") or {}
        at = stopped.get("at")
        if stopped.get("kind") != "limit" or not at or at <= limits["updatedAt"]:
            continue
        if s.get("shared") and s.get("startedAt") and at < s["startedAt"]:
            continue  # (a shared chat, or a copy, may have hit another account's limit before it came here)
        for w in limits["windows"]:
            hours = window_hours(w["key"])
            if w["key"] != stopped.get("window") or w["reset"] or not w["resetsAt"] or not hours \
                    or not w["resetsAt"] - hours * 3600_000 <= at < w["resetsAt"]:
                continue  # (a limit of a window that's over since)
            w["pct"] = max(w["pct"], 100.0)
            if w.get("forecast"):
                w["forecast"] = dict(w["forecast"], fullAt=None, atReset=max(w["forecast"]["atReset"], w["pct"]))


# ---------------------------------------------------------------- chats changing the same files
#
# Two chats that can still change files (each has a process) and changed
# some of the same ones may overwrite each other's work: each row names the
# other chat and those files (from what the widget reads of what a chat
# changed: Claude's backups and its edit tool calls, subagents included).

OVERLAP_FILES = 8  # file names a row carries per other chat


def note_overlaps(groups, changed, home):
    """Set "overlaps" on the rows of chats with a process that changed the
    same files as another such chat: [{"key", "name", "sessionId",
    "profileDir", "count", "files"}]. changed: row key -> absolute paths."""
    rows = [s for g in groups for s in g["sessions"] if s.get("pid") is not None and changed.get(s["key"])]
    named = {}  # row key -> the chats it names (one listed twice, once)
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if (a.get("chatId") or a["sessionId"]) == (b.get("chatId") or b["sessionId"]):
                continue  # one chat listed twice
            both = changed[a["key"]] & changed[b["key"]]
            if not both:
                continue
            for row, other in ((a, b), (b, a)):
                chat = other.get("chatId") or other["sessionId"]
                if chat in named.setdefault(row["key"], set()):
                    continue
                named[row["key"]].add(chat)
                row["overlaps"] = (row.get("overlaps") or []) + [{
                    "key": other["key"], "name": other["name"], "sessionId": other["sessionId"],
                    "profileDir": other["profileDir"], "count": len(both),
                    "files": sorted(file_label(p, row["cwd"], home) for p in both)[:OVERLAP_FILES],
                }]


# ---------------------------------------------------------------- tasks waiting for a usage limit
#
# A task for a background session can wait until the usage limit of its
# account resets: it's kept in ~/.cache/talpy-den/queue.json (an SSH
# host's too: they start from here, over ssh), and every status lists it
# ("due" once its time has come). A widget (or the VS Code extension) then
# starts it with jump.py --start-queued, which takes it out of the queue
# first, so of several copies only one starts it.

QUEUE_PROMPT_MAX = 100_000


def load_queue(cache_root):
    data = read_json(cache_root / "queue.json")
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return [t for t in tasks if isinstance(t, dict) and valid_id(t.get("id"))] if isinstance(tasks, list) else []


def change_queue(cache_root, add=None, take=None, put_back=None, once=False):
    """Change queue.json under a lock: add a task (once: unless a go-on of
    the same chat is queued already), take one out by its id (returned), or
    put one back (e.g. with the error it got). The task taken out, or None."""
    private_dir(cache_root)
    with open(cache_root / "queue.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        tasks = load_queue(cache_root)
        taken = None
        if take is not None:
            taken = next((t for t in tasks if t["id"] == take), None)
            tasks = [t for t in tasks if t["id"] != take]
        if put_back is not None:
            tasks = [t for t in tasks if t["id"] != put_back["id"]] + [put_back]
        if add is not None and once and add.get("chat") and any(
                t.get("chat") == add["chat"] and (t.get("host") or "") == (add.get("host") or "") for t in tasks):
            add = None
        if add is not None:
            tasks.append(add)
        if add is not None or taken is not None or put_back is not None:
            write_json_atomic(cache_root / "queue.json", {"tasks": tasks})
        return taken


def queue_entry(profile, folder, prompt, start_at, host="", account="", plan=None, plan_profile=None,
                plan_title="", now_ms=None, chat=None, name="", scheduled=False, why="", model=None, effort=None,
                mode=None):
    """A task to queue (checked), or ValueError. With chat (its id), it goes
    on with that chat (one a usage limit stopped: continue_chat), named name;
    why: "retry" (its turn broke off) or "cap" (stopped at the usage cap) goes
    on whatever its newest turn is (keep_going). scheduled: it starts at a
    time you picked, not when a limit resets. model, effort, mode: a new
    chat's (chat_options)."""
    chat_options(model, effort, mode)  # (checked)
    if not isinstance(profile, str) or not profile or not isinstance(folder, str):
        raise ValueError("A queued task needs an account and a folder")
    if chat is not None and not valid_id(chat):
        raise ValueError(f"Not a chat id: {chat}")
    if not (prompt or "").strip() and not plan and chat is None:
        raise ValueError("Write what Claude should do first")
    if len(prompt or "") > QUEUE_PROMPT_MAX:
        raise ValueError("That task is too long")
    if plan is not None and not PLAN_SLUG.match(plan):
        raise ValueError(f"Not a plan name: {plan}")
    if not isinstance(start_at, int) or start_at <= 0:
        raise ValueError("A queued task needs the time it starts at")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return {
        "id": os.urandom(4).hex(), "host": host or "", "profile": profile, "account": account or profile,
        "folder": folder, "prompt": (prompt or "").strip(), "plan": plan, "planProfile": plan_profile,
        "planTitle": plan_title or "", "startAt": start_at, "createdAt": now_ms, "error": "",
        "chat": chat, "name": clean_text(name, 80) if chat else "", "scheduled": bool(scheduled) and chat is None,
        "why": why if chat and why in ("retry", "cap") else "",
        "model": (model or None) if chat is None else None, "effort": (effort or None) if chat is None else None,
        "mode": (mode or None) if chat is None else None,
    }


def queued_tasks(cache_root, now_ms):
    """The queued tasks as the status lists them: each with "due" (its time
    has come, and it didn't fail to start) and "folderShort"."""
    out = []
    for t in load_queue(cache_root):
        t = {"chat": None, "name": "", "scheduled": False, **t}
        t["due"] = not t.get("error") and isinstance(t.get("startAt"), (int, float)) and t["startAt"] <= now_ms
        if t.get("afterTurn"):  # (a message for when its turn ends: the widget sees that, and sends it)
            t["due"] = False
        t["folderShort"] = short_path(t["folder"], Path.home()) if not t.get("host") else t.get("folder") or "~"
        out.append(t)
    return out


# ---------------------------------------------------------------- chats that keep going
#
# A chat of this computer can keep going until it's done: whenever a usage
# limit stops it, it goes on a minute after the limit resets, as often as that
# takes. Such chats are kept in ~/.cache/talpy-den/keep.json, and the
# status run of the copy that notifies looks after them (look_after_kept): it
# queues each go-on (started like one you queued), tries again after a pause
# when one didn't take (nothing new in the chat a while after it went on) or
# when the turn broke off on an API error that isn't a limit (the connection
# lost), a few times with longer pauses, and lets the chat go once a turn of
# it has ended by itself. One you interrupted has ended too: you took over.
#
# With a usage cap (--usage-cap N), it also stops what an account has at work
# once one of its plan windows reaches N%, once in that window: the chats
# that keep going go on after the window resets, and queued tasks wait for it.

KEEP_TAKE_MS = 5 * 60_000  # a go-on that leaves nothing new in its chat after this long didn't take
KEEP_RETRY_MINUTES = (2, 5, 15, 30, 60)  # the pauses before each try again; then it gives up
KEEP_IDLE_MS = 7 * 86400_000  # a chat kept this long with nothing new is let go
TURN_ENDS = ("end_turn", "stop_sequence", "max_tokens")  # a reply's stop_reason that ends its turn
RETRY_PROMPT = "Your last turn broke off (an API error): go on where you stopped."


def load_keep(cache_root):
    """{"chats": the chats that keep going, "capped": {account|window: when
    that window resets}, for the windows the usage cap stopped chats in}."""
    data = read_json(cache_root / "keep.json")
    data = data if isinstance(data, dict) else {}
    chats = data.get("chats") if isinstance(data.get("chats"), list) else []
    capped = data.get("capped") if isinstance(data.get("capped"), dict) else {}
    return {"chats": [k for k in chats if isinstance(k, dict) and valid_id(k.get("chat"))
                      and isinstance(k.get("profile"), str) and k["profile"]],
            "capped": {w: at for w, at in capped.items() if isinstance(w, str) and number(at) is not None}}


def change_keep(cache_root, change, wait=True):
    """Change keep.json under a lock: change(data) changes it in place and
    says whether it did. False (nothing done) when another copy has the lock
    and wait is False: it looks after the chats right now."""
    private_dir(cache_root)
    with open(cache_root / "keep.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            return False
        data = load_keep(cache_root)
        if change(data):
            write_json_atomic(cache_root / "keep.json", data)
        return True


def keep_chat(cache_root, profile_key, chat_id, name="", folder="", on=True, now_ms=None):
    """Have a chat of this computer keep going, or (not on) not any more."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms

    def change(data):
        had = any(k["chat"] == chat_id for k in data["chats"])
        if on and had:
            return False
        data["chats"] = [k for k in data["chats"] if k["chat"] != chat_id]
        if on:
            data["chats"].append({"chat": chat_id, "profile": profile_key, "name": clean_text(name, 80),
                                  "folder": folder or "", "since": now_ms, "wentOnAt": None, "tries": 0,
                                  "note": "", "error": "", "errorAt": None, "cappedUntil": None})
        return on or had
    change_keep(cache_root, change)


def note_went_on(cache_root, chat_id, now_ms=None):
    """A go-on of a chat that keeps going has started: something new should
    show in it soon (look_after_kept checks)."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms

    def change(data):
        k = next((k for k in data["chats"] if k["chat"] == chat_id), None)
        if k is None:
            return False
        k.update(wentOnAt=now_ms, note="", cappedUntil=None)
        return True
    change_keep(cache_root, change)


def kept_chats(cache_root):
    """The chats that keep going, as the status lists them."""
    return [{f: k.get(f) for f in ("chat", "profile", "name", "since", "wentOnAt", "tries", "note", "error")}
            for k in load_keep(cache_root)["chats"]]


def last_turn(path):
    """How a chat's newest turn (a prompt of yours or a reply; not tool
    results or notes) stands, from the end of its transcript: {"kind":
    "prompt" (no reply yet), "reply", "limit", "login" (stopped_by) or "error"
    (an API error that isn't a limit: the connection lost, say), "at",
    "ended": whether the turn is over}, or None."""
    try:
        size = path.stat().st_size
    except (OSError, AttributeError):
        return None
    nbytes = PEEK_TAIL_BYTES
    while True:
        turn = next((e for e in tail_entries(path, size, nbytes) if not e.get("isSidechain")
                     and (e["type"] == "assistant" or human_prompt(e) is not None)), None)
        if turn is not None or nbytes >= size or nbytes >= PEEK_TAIL_MAX:
            break
        nbytes *= 4
    if turn is None:
        return None
    at = to_ms(turn.get("timestamp")) or 0
    if turn["type"] != "assistant":
        return {"kind": "prompt", "at": at, "ended": False}
    stop = stopped_by(turn)
    if stop is not None:
        return dict(stop, at=stop.get("at") or at, ended=True)
    msg = turn.get("message") if isinstance(turn.get("message"), dict) else {}
    if turn.get("isApiErrorMessage") is True:
        return {"kind": "error", "at": at, "text": short_text(" ".join(message_text(msg).split()), 160), "ended": True}
    return {"kind": "reply", "at": at, "ended": msg.get("stop_reason") in TURN_ENDS}


def kept_step(k, turn, row, queued, limits, now_ms):
    """What to do now about the chat that keeps going k (keep.json's entry,
    changed in place: tries, note, error), whose newest turn is turn
    (last_turn), whose row in the status (its process) is row, and whose
    account's plan limits are limits: ("go on", when, why, prompt) to queue
    its go-on, "done" to let it go, or None (nothing for now)."""
    if queued:
        return None  # its go-on waits (or couldn't start, and says why)
    k["note"] = ""
    if row is not None and row.get("state") in ("working", "waiting"):
        return None  # at work, or waiting for you
    live = row is not None and row.get("pid") is not None
    # (open in a Claude Code tab, or in a session it was resumed in: ending that isn't up to the widget)
    in_tab = live and (row.get("entrypoint") == "claude-vscode" or row.get("sessionId") != row.get("chatId"))

    def go(start, why="", prompt=""):
        if in_tab:
            k["note"] = "It's open in a Claude Code tab: it can't go on by itself while that's open"
            return None
        return "go on", int(start), why, prompt

    def again(why, prompt=""):
        tries = k.get("tries") or 0
        if in_tab:
            return go(now_ms)  # (says why it can't)
        if tries >= len(KEEP_RETRY_MINUTES):
            k.update(error=f"Gave up after {tries} tries: {why}", errorAt=now_ms)
            return None
        k["tries"] = tries + 1
        return go(now_ms + KEEP_RETRY_MINUTES[tries] * 60_000, "retry", prompt)

    if k.get("cappedUntil"):  # the usage cap stopped it: on once that window resets
        return go(max(k["cappedUntil"] + 60_000, now_ms), "cap")
    if turn is None:
        return None
    at, went = turn["at"], k.get("wentOnAt") or 0
    if k.get("error"):
        if at <= (k.get("errorAt") or 0):
            return None  # it gave up; something new in the chat starts it over
        k.update(error="", errorAt=None, tries=0)
    if turn["kind"] == "limit" and at > went:
        k["tries"] = 0
        s = limit_stop(turn, limits, now_ms)
        return go(s["resetsAt"] + 60_000 if s.get("resetsAt") and not s.get("over") else now_ms)
    if turn["kind"] == "login":
        k["note"] = "Its account's login has expired: log in again, then it can go on"
        return None
    if turn["kind"] == "error" and at > went:
        return again(turn.get("text") or "an API error", RETRY_PROMPT)
    if went and at <= went:
        # nothing new since it went on: that didn't take
        return again("it didn't go on") if now_ms - went > KEEP_TAKE_MS else None
    if not turn["ended"] and not live:
        # a turn with no process to finish it: it broke off (its process ended in it)
        return again("its turn broke off") if now_ms - at > KEEP_TAKE_MS else None
    if turn["ended"] and at > max(went, k.get("since") or 0):
        return "done"
    if now_ms - max(at, went, k.get("since") or 0) > KEEP_IDLE_MS:
        return "done"  # (nothing new for days: let it go)
    return None


def cap_window(group, cap, now_ms):
    """The account's plan window that's at or past cap percent (of several,
    the one that resets last), or None."""
    best = None
    for w in ((group.get("limits") or group.get("accountLimits") or {}).get("windows") or []):
        pct, at = number(w.get("pct")), number(w.get("resetsAt"))
        if pct is not None and pct >= cap and at and at > now_ms and not w.get("reset") \
                and (best is None or at > best["resetsAt"]):
            best = w
    return best


def note_caps(groups, cap, now_ms):
    """Mark each account whose plan window is at the usage cap: group["cap"]
    = {"pct", "label", "key", "resetsAt"} (queued tasks wait for it), else None."""
    for g in groups:
        w = cap_window(g, cap, now_ms) if cap else None
        g["cap"] = {"pct": cap, "label": w.get("label") or "", "key": w.get("key") or "", "resetsAt": int(w["resetsAt"])} \
            if w else None


def look_after_kept(home, cache_root, extra_dirs, groups, now_ms, cap=0):
    """Look after the chats that keep going, and stop the chats at work at
    the usage cap (see above). groups: this computer's accounts, as the
    status lists them. Nothing when another copy is at it right now."""
    profiles = discover_profiles(home, extra_dirs)
    queued = {t.get("chat") for t in load_queue(cache_root) if t.get("chat") and not t.get("host")}
    rows = {}
    for g in groups:
        for s in g.get("sessions", []):
            for i in (s.get("sessionId"), s.get("chatId")):
                if i and (i not in rows or s.get("pid") is not None):
                    rows[i] = s
    adds, stops = [], []

    def change(data):
        changed = False
        for mark, at in list(data["capped"].items()):
            if at <= now_ms:  # (that window is over)
                del data["capped"][mark]
                changed = True
        for g in groups:
            w = g.get("cap")
            mark = "|".join(g.get("profileKeys") or []) + "|" + (w["key"] if w else "")
            if not w or mark in data["capped"]:
                continue
            data["capped"][mark] = w["resetsAt"]
            changed = True
            for s in g.get("sessions", []):
                if s.get("pid") is not None and s.get("state") == "working":
                    stops.append(s)
                    for k in data["chats"]:
                        if k["chat"] in (s.get("chatId"), s.get("sessionId")):
                            k["cappedUntil"] = w["resetsAt"]
        for k in list(data["chats"]):
            before = dict(k)
            profile = next((p for p in profiles if matches_profile(p, k["profile"])), None)
            path = transcript_path(profile, k["chat"]) if profile is not None else None
            g = next((g for g in groups if k["profile"] in (g.get("profileKeys") or [])), None)
            step = None if path is None else kept_step(
                k, last_turn(path), rows.get(k["chat"]), k["chat"] in queued,
                (g.get("limits") or g.get("accountLimits")) if g else None, now_ms)
            if path is None or step == "done":
                data["chats"].remove(k)
                changed = True
            elif step is not None:
                _, start, why, prompt = step
                adds.append(queue_entry(k["profile"], k.get("folder") or "", prompt, int(start), chat=k["chat"],
                                        name=k.get("name") or "", why=why, now_ms=now_ms))
                k["cappedUntil"] = None
            changed = changed or k != before
        return changed

    if not change_keep(cache_root, change, wait=False):
        return
    for entry in adds:
        change_queue(cache_root, add=entry, once=True)
    if stops:
        in_background(lambda: stop_all(home, extra_dirs, stops))


def stop_all(home, extra_dirs, sessions):
    """Stop these chats at work, as the stop button does where they run (an
    interrupt in tmux: the chat stays open there)."""
    profiles = discover_profiles(home, extra_dirs)
    for s in sessions:
        try:
            if s.get("kind") == "bg" and valid_id(s.get("jobId")):
                profile = next((p for p in profiles if matches_profile(p, s.get("profileDir") or "")), None)
                if profile is not None:
                    stop_background(profile, s["jobId"], read_job([profile], s["jobId"]), home)
            elif s.get("tmux"):
                interrupt_chat_by_id(home, extra_dirs, s["sessionId"], s.get("profileDir"))
            else:
                stop_chat_by_id(home, extra_dirs, s["sessionId"], s.get("profileDir"), busy_ok=True)
        except Exception:  # (on with the others)
            pass


def in_background(task):
    """Run task in a process of its own once this one has answered (the
    widget waits until the status's output ends: it mustn't inherit it)."""
    try:
        pid = os.fork()
    except OSError:
        return
    if pid:
        return
    try:
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        task()
    except Exception:
        pass
    finally:
        os._exit(0)


# ---------------------------------------------------------------- plans
#
# A chat in plan mode writes its plan to <cfg>/plans/<slug>.md, the slug
# being the chat's (its transcript's entries carry it). Claude writes the
# file in place, so a plan hard-linked into several profiles (shared, like a
# chat: sharing.py) stays one file. Each account lists its plans, newest
# first, with the chat that made each one where it's still there.

PLANS_LISTED = 8
PLAN_TITLE_BYTES = 4096


def plan_path(profile, slug):
    return profile.path / "plans" / f"{slug}.md" if isinstance(slug, str) and PLAN_SLUG.match(slug) else None


def plan_title(path):
    """A plan's first heading (in its first 4 KB), else ''."""
    try:
        with open(path, "rb") as f:
            head = f.read(PLAN_TITLE_BYTES)
    except OSError:
        return ""
    for line in head.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("#"):
            return clean_text(line.lstrip("#").strip(), 120)
    return ""


def plan_of(profile, slug):
    """{"slug", "title", "path", "updatedAt"} of the plan a chat made on this
    profile, or None."""
    path = plan_path(profile, slug)
    try:
        st = path.stat() if path is not None else None
    except OSError:
        st = None
    if st is None or not path.is_file():
        return None
    return {"slug": slug, "title": plan_title(path) or slug, "path": str(path), "updatedAt": int(st.st_mtime * 1000)}


def plan_edited(plan_ms, transcript_mtime):
    """Was the plan changed after its chat last did anything (so not by
    Claude: by you, e.g. with the widget's edit button)?"""
    return isinstance(transcript_mtime, (int, float)) and plan_ms / 1000 > transcript_mtime + 2


class SlugCache:
    """Each transcript's slug (the name of the plan it would write), read from
    its end and kept in plans.json until the transcript grows."""

    VERSION = 1

    def __init__(self, path):
        self.path = path
        data = read_json(path)
        ok = isinstance(data, dict) and data.get("version") == self.VERSION
        self.old = data if ok else {}
        self.data = {"version": self.VERSION, "slugs": {}}
        self.changed = not ok

    def slug(self, path, st):
        """The transcript's newest slug, reading only what was appended since last time."""
        key = file_key(st)
        cached = self.data["slugs"].get(key, self.old.get("slugs", {}).get(key))
        size, slug = 0, ""
        if isinstance(cached, list) and len(cached) == 2 and isinstance(cached[0], int) \
                and 0 <= cached[0] <= st.st_size and isinstance(cached[1], str):
            size, slug = cached
        if size < st.st_size:
            slug = transcript_slug(path, st.st_size, size) or slug
            self.changed = True
        self.data["slugs"][key] = [st.st_size, slug]
        return slug or None

    def cached(self, st):
        """The slug noted for this transcript as it is, without reading it; None if there's none."""
        key = file_key(st)
        cached = self.data["slugs"].get(key, self.old.get("slugs", {}).get(key))
        if not (isinstance(cached, list) and len(cached) == 2 and cached[0] == st.st_size and isinstance(cached[1], str)):
            return None
        self.data["slugs"][key] = cached
        return cached[1] or None

    def save(self):
        kept = keep_unused(self.old, self.data, ("slugs",), time.time())
        if self.changed or kept or set(self.data["slugs"]) != set(self.old.get("slugs", {})):
            try:
                write_json_atomic(self.path, self.data)
            except OSError:
                pass


SLUG_LINE = re.compile(rb'"slug":\s*"([A-Za-z0-9][A-Za-z0-9_-]{0,127})"')


def transcript_slug(path, size, start=0):
    """The newest slug in a transcript's lines after byte start, looking at
    its last 64 KB first, then more (up to 1 MB) while there's none; None if
    there's none."""
    nbytes = 65536
    while True:
        begin = max(start, size - nbytes)
        try:
            with open(path, "rb") as f:
                f.seek(begin)
                chunk = f.read(size - begin)
        except OSError:
            return None
        found = SLUG_LINE.findall(chunk)
        if found:
            return found[-1].decode()
        if begin <= start or nbytes >= 1 << 20:
            return None
        nbytes *= 4


def all_transcripts(ctx):
    """[(mtime, profile, path, stat)] of every chat transcript, newest first
    (once per check)."""
    found = getattr(ctx, "transcripts", None)
    if found is not None:
        return found
    found = []
    for p in ctx.profiles:
        try:
            files = list(p.path.glob("projects/*/*.jsonl"))
        except OSError:
            continue
        for f in files:
            if not valid_id(f.stem) or "." in f.stem:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            found.append((st.st_mtime, p, f, st))
    found.sort(key=lambda c: -c[0])
    ctx.transcripts = found
    return found


def account_plans(group, ctx, limit=PLANS_LISTED, starred=frozenset()):
    """The plans of an account's profiles, newest first: [{"key", "slug",
    "title", "path", "profileDir", "profile", "updatedAt", "shared", "chatId",
    "cwd", "cwdShort", "starred"}], the chat being the one that made it
    (where it's still there; None else). The first limit of them, then the
    starred ones (slugs in starred) that are older."""
    by_key = {p.key: p for p in ctx.profiles}
    found, seen = [], set()
    for key in group["profileKeys"]:
        p = by_key.get(key)
        try:
            files = list(p.path.glob("plans/*.md")) if p is not None else []
        except OSError:
            continue
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            if not PLAN_SLUG.match(f.stem) or not f.is_file() or (st.st_dev, st.st_ino) in seen:
                continue
            seen.add((st.st_dev, st.st_ino))
            found.append((st.st_mtime, p, f, st))
    found.sort(key=lambda c: -c[0])
    found = [c for i, c in enumerate(found) if i < limit or c[2].stem in starred]
    plans = []
    for t, p, f, st in found:
        plans.append({
            "key": f"{p.key}/{f.stem}/plan",
            "slug": f.stem,
            "title": plan_title(f) or f.stem,
            "path": str(f),
            "profileDir": p.key,
            "profile": p.label,
            "updatedAt": int(t * 1000),
            # linked into another profile's plans too (sharing.py)
            "shared": st.st_nlink > 1 and any(q.real != p.real and same_file(plan_path(q, f.stem), st)
                                              for q in ctx.profiles),
            "chatId": None, "chatProfileDir": None, "cwd": "", "cwdShort": "", "edited": False,
            "starred": f.stem in starred,
        })
    if plans:
        find_plan_chats(plans, ctx)
    return plans


def same_file(path, st):
    try:
        other = path.stat() if path is not None else None
    except OSError:
        return False
    return other is not None and (other.st_dev, other.st_ino) == (st.st_dev, st.st_ino)


def find_plan_chats(plans, ctx):
    """The chat that made each plan: the newest transcript with its slug. It
    was active when the plan was written, so only transcripts changed since
    the oldest plan's time are read; older ones count with the slug noted
    for them then (a plan you changed since is newer than its chat)."""
    slugs = getattr(ctx, "slugs", None)
    if slugs is None:
        return
    wanted = {pl["slug"]: pl for pl in plans}
    oldest = min(pl["updatedAt"] for pl in plans) / 1000 - 60
    for t, p, f, st in all_transcripts(ctx):
        if not wanted:
            break
        slug = slugs.slug(f, st) if t >= oldest else slugs.cached(st)
        pl = wanted.pop(slug, None) if slug else None
        if pl is None:
            continue
        _, cwd, _ = ctx.links.chat_info(f, st)
        pl["chatId"], pl["chatProfileDir"] = f.stem, p.key
        pl["cwd"], pl["cwdShort"] = cwd, short_path(cwd, ctx.home) if cwd else ""
        pl["edited"] = plan_edited(pl["updatedAt"], t)


# ---------------------------------------------------------------- memories
#
# Claude keeps what it learns about you and your projects as memories: one
# Markdown file each, in <cfg>/projects/<folder>/memory/ (MEMORY.md is their
# index), with a name, a description and a type (user, feedback, project,
# reference) at the top. Each account lists its memories, newest first,
# after its own CLAUDE.md: your instructions for every chat on it (type
# "account"; often one file linked into each account's folder).

MEMORIES_LISTED = 30
MEMORY_HEAD_BYTES = 2048
MEMORY_FIELD = re.compile(r"^\s*(name|description|type):\s*(.*?)\s*$")


def memory_head(path):
    """{"name", "description", "type"} from the top of a memory file."""
    try:
        with open(path, "rb") as f:
            head = f.read(MEMORY_HEAD_BYTES).decode("utf-8", "replace")
    except OSError:
        return {}
    lines = head.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = MEMORY_FIELD.match(line)
        if m and m.group(1) not in out:
            value = m.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            out[m.group(1)] = value
    return out


def project_folder_of(project):
    """The folder a projects/<key> folder is for: Claude names it after the
    folder, every character but letters and digits made a "-", so it's
    found step by step on disk (the longest name that fits first); "" when
    it isn't there any more."""
    rest, path = project.name[1:] if project.name.startswith("-") else project.name, "/"
    while rest:
        try:
            names = sorted(os.listdir(path), key=len, reverse=True)
        except OSError:
            return ""
        step = next((n for n in names if (rest == mangle(n) or rest.startswith(mangle(n) + "-"))
                     and os.path.isdir(os.path.join(path, n))), None)
        if step is None:
            return ""
        path, rest = os.path.join(path, step), rest[len(mangle(step)) + 1:]
    return path


def mangle(name):
    return re.sub(r"[^A-Za-z0-9]", "-", name)


def account_memories(group, ctx, limit=MEMORIES_LISTED):
    """The memories of an account's profiles, newest first: [{"key", "name",
    "description", "type", "path", "folder", "folderShort", "profileDir",
    "profile", "updatedAt"}]."""
    by_key = {p.key: p for p in ctx.profiles}
    found, seen = [], set()
    for key in group["profileKeys"]:
        p = by_key.get(key)
        try:
            files = list(p.path.glob("projects/*/memory/*.md")) if p is not None else []
        except OSError:
            continue
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            if f.name == "MEMORY.md" or not f.is_file() or (st.st_dev, st.st_ino) in seen:
                continue
            seen.add((st.st_dev, st.st_ino))
            found.append((st.st_mtime, p, f))
    found.sort(key=lambda c: -c[0])
    folders, out = {}, account_instructions(group, ctx)
    for t, p, f in found[:limit]:
        project = f.parent.parent
        if project not in folders:
            folders[project] = project_folder_of(project)
        head = memory_head(f)
        folder = folders[project]
        out.append({
            "key": f"{p.key}/{project.name}/{f.stem}/memory",
            "name": clean_text(head.get("name") or f.stem, 120),
            "description": clean_text(head.get("description"), 300),
            "type": head.get("type") if head.get("type") in ("user", "feedback", "project", "reference") else "",
            "path": str(f),
            "folder": folder,
            "folderShort": short_path(folder, ctx.home) if folder else "",
            "profileDir": p.key,
            "profile": p.label,
            "updatedAt": int(t * 1000),
        })
    return out


def account_instructions(group, ctx):
    """The account's CLAUDE.md (each of its profiles', once), as memories."""
    by_key = {p.key: p for p in ctx.profiles}
    out, seen = [], set()
    for key in group["profileKeys"]:
        p = by_key.get(key)
        f = p.path / "CLAUDE.md" if p is not None else None
        try:
            st = f.stat() if f is not None else None
        except OSError:
            st = None
        if st is None or not f.is_file() or (st.st_dev, st.st_ino) in seen:
            continue
        seen.add((st.st_dev, st.st_ino))
        # (linked to a file outside the account's folder, or into another's: shared)
        shared = f.is_symlink() or st.st_nlink > 1
        out.append({
            "key": f"{p.key}/CLAUDE.md/memory", "name": "CLAUDE.md",
            "description": "Your instructions for every chat on this account" + (" (shared with other accounts)" if shared else ""),
            "type": "account", "path": str(f), "folder": "", "folderShort": "", "profileDir": p.key,
            "profile": p.label, "updatedAt": int(st.st_mtime * 1000),
        })
    return out


def memory_file(profiles, path):
    """path, when it's a memory file of one of the profiles (projects/*/memory/*.md,
    or its CLAUDE.md), else None."""
    try:
        real = Path(path).resolve()
    except (OSError, RuntimeError):
        return None
    if real.suffix != ".md" or not real.is_file():
        return None
    for p in profiles:
        projects = p.real / "projects"
        try:
            projects = projects.resolve()  # (it may be a link to a folder elsewhere, e.g. on another disk)
        except (OSError, RuntimeError):
            pass
        if real.parent.name == "memory" and real.parent.parent.parent == projects:
            return real
        if Path(path) in (p.path / "CLAUDE.md", p.real / "CLAUDE.md"):
            return Path(path)  # (by its own name: it may be a link to a shared file)
    return None


# ---------------------------------------------------------------- feedback on a plan
#
# You change a plan (its file) or comment on its lines (VS Code), then send
# that to the chat that made it, which revises the plan in plan mode. What
# you changed is told apart from what Claude wrote by Claude's own version
# of the plan, rebuilt from the chat's transcript (claude_plan): what it
# last wrote to the file (Write), with its edits since (Edit, MultiEdit),
# or the plan it presented (ExitPlanMode's "plan"), counting only the tool
# calls that went through.

PLAN_FEEDBACK_MAX = 90_000  # characters of a message
# ...and bytes of it once shell-quoted: the command that reopens its chat has
# it in one argument, of 128 KB at most (a character can take 3 bytes or 4)
PLAN_FEEDBACK_BYTES = 120_000
PLAN_TOOLS = ("Write", "Edit", "MultiEdit", "ExitPlanMode")


def claude_plan(transcript, slug):
    """The plan <slug>.md as Claude last wrote it, from its transcript; None
    when that can't be told (no full version, or an edit that doesn't fit)."""
    text, pending = None, {}
    name_end = f"/plans/{slug}.md"
    try:
        with open(transcript, encoding="utf-8", errors="replace") as f:
            for line in f:
                if name_end not in line and not any(i in line for i in pending):
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                content = (e.get("message") or {}).get("content") if isinstance(e.get("message"), dict) else None
                for c in content if isinstance(content, list) else []:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "tool_use" and c.get("name") in PLAN_TOOLS and isinstance(c.get("input"), dict):
                        inp = c["input"]
                        where = inp.get("planFilePath") if c["name"] == "ExitPlanMode" else inp.get("file_path")
                        if isinstance(where, str) and where.endswith(name_end) and isinstance(c.get("id"), str):
                            pending[c["id"]] = (c["name"], inp)
                    elif c.get("type") == "tool_result" and c.get("tool_use_id") in pending:
                        name, inp = pending.pop(c["tool_use_id"])
                        if c.get("is_error") and name != "ExitPlanMode":
                            continue  # (refused: the file didn't change; a plan you didn't approve still is the file)
                        text = apply_plan_tool(text, name, inp)
    except OSError:
        return None
    return text


def plan_chat(profiles, slug, limit=2000):
    """(profile, transcript) of the chat that made a plan: the newest chat
    with its slug at its end (entries carry it), else (None, None)."""
    found = []
    for p in profiles:
        try:
            files = list(p.path.glob("projects/*/*.jsonl"))
        except OSError:
            continue
        for f in files:
            if not valid_id(f.stem):
                continue
            try:
                found.append((f.stat().st_mtime, p, f))
            except OSError:
                pass
    found.sort(key=lambda c: -c[0])
    needle = re.compile(rb'"slug":\s*"' + re.escape(slug.encode()) + rb'"')
    for _, p, f in found[:limit]:
        try:
            with open(f, "rb") as fh:
                fh.seek(max(0, fh.seek(0, 2) - 65536))
                if needle.search(fh.read()):
                    return p, f
        except OSError:
            continue
    return None, None


def apply_plan_tool(text, name, inp):
    """The plan after one of Claude's tool calls on it (None: lost track)."""
    if name == "ExitPlanMode":
        return inp["plan"] if isinstance(inp.get("plan"), str) and inp["plan"].strip() else text
    if name == "Write":
        return inp["content"] if isinstance(inp.get("content"), str) else None
    edits = inp.get("edits") if name == "MultiEdit" else [inp]
    for ed in edits if isinstance(edits, list) else [None]:
        old, new = (ed or {}).get("old_string"), (ed or {}).get("new_string")
        if text is None or not isinstance(old, str) or not isinstance(new, str) or old not in text:
            return None
        if new == "" and not old.endswith("\n") and old + "\n" in text:
            old += "\n"  # (as Claude's Edit does: what it takes out goes with the end of its line)
        text = text.replace(old, new) if ed.get("replace_all") else text.replace(old, new, 1)
    return text


def plan_feedback(plan, original, comments=(), home=None):
    """The message that gives Claude your feedback on a plan (a path): what
    you changed in it (a diff from original, Claude's version; None when that
    isn't known) and your comments [{"from", "to", "quote", "text"}] on its
    lines; "" when there's nothing to say."""
    import difflib

    def size(text):
        return len(shlex.quote(text).encode("utf-8", "replace"))

    with open(plan, encoding="utf-8", errors="replace") as f:
        current = f.read()
    where = short_folder(str(plan), home)
    parts = []
    if original is not None and original != current:
        diff = "".join(difflib.unified_diff(original.splitlines(True), current.splitlines(True),
                                            "the plan as you wrote it", "the plan as I changed it", n=2))
        if len(diff) > PLAN_FEEDBACK_MAX // 2 or size(diff) > PLAN_FEEDBACK_BYTES // 2:
            diff = diff[:PLAN_FEEDBACK_MAX // 2]
            while size(diff) > PLAN_FEEDBACK_BYTES // 2:
                diff = diff[:len(diff) * 9 // 10]
            diff += "\n… (more changes: read the file)\n"
        parts.append(f"I changed the plan file. My changes:\n\n```diff\n{diff.rstrip()}\n```")
    elif original is None:
        parts.append(f"I changed the plan file: read {where} again for my changes.")
    for i, c in enumerate(c for c in comments if isinstance(c, dict) and str(c.get("text") or "").strip()):
        first, last = c.get("from"), c.get("to")
        lines = (f"line {first}" if first == last else f"lines {first}–{last}") if isinstance(first, int) else "the plan"
        quote = "\n".join("> " + l for l in str(c.get("quote") or "").strip("\n").splitlines()[:12])
        parts.append(f"Comment {i + 1}, on {lines}:\n" + (quote + "\n" if quote else "") + str(c["text"]).strip())
    if not parts:
        return ""
    head = (f"Here is my feedback on your plan ({where}). Revise the plan with it, and stay in plan mode: "
            "show me the revised plan before you change anything else.")
    message = (head + "\n\n" + "\n\n".join(parts))[:PLAN_FEEDBACK_MAX]
    while size(message) > PLAN_FEEDBACK_BYTES:
        message = message[:len(message) * 9 // 10]
    return message


# ---------------------------------------------------------------- finding a chat
#
# Every chat whose name, folder or one of your prompts (history.jsonl) has
# all the words you look for, newest first. Transcripts are only read for
# their names (kept in links.json), so this is quick after the first time.

FIND_LIMIT = 30
HISTORY_ALL_BYTES = 16 << 20
SNIPPET_CHARS = 140


def history_all(profile):
    """The entries of a profile's prompt history (its last 16 MB)."""
    path = profile.path / "history.jsonl"
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - HISTORY_ALL_BYTES))
            lines = f.read().splitlines()
    except OSError:
        return []
    if size > HISTORY_ALL_BYTES:
        lines = lines[1:]
    entries = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            entries.append(e)
    return entries


def snippet(text, words):
    """The part of text around the first of words it has, on one line."""
    flat = " ".join(text.split())
    low = flat.casefold()
    at = min((i for i in (low.find(w) for w in words) if i >= 0), default=0)
    start = max(0, at - SNIPPET_CHARS // 3)
    part = flat[start:start + SNIPPET_CHARS]
    return ("…" if start > 0 else "") + part + ("…" if start + SNIPPET_CHARS < len(flat) else "")


def find_chats(home, cache_root, extra_dirs, query, limit=FIND_LIMIT):
    """{"ok", "results": [...], "more", "projects", "plans", "memories"}: the
    chats with every word of query in their name, folder or one of your
    prompts, newest first; and the project folders, plans and memories with
    them (find_more)."""
    words = [w.casefold() for w in query.split()]
    if not words:
        return {"ok": True, "results": [], "more": False}
    profiles = discover_profiles(home, extra_dirs)
    ctx = Context(home=home, profiles=profiles, links=LinkCache(cache_root / "links.json"))
    prompts, used = {}, {}
    for p in profiles:
        for e in history_all(p):
            sid, text = e.get("sessionId"), e.get("display")
            if valid_id(sid) and isinstance(text, str) and text.strip():
                prompts.setdefault(sid, []).append(text)
                used[(p.real, sid)] = max(used.get((p.real, sid), 0), number(e.get("timestamp")) or 0)
    accounts = {}
    for gid, members in group_profiles(profiles):
        named = next((p for p in members if not p.is_default), members[0])
        plan = next((p.plan for p in members if p.plan), None)
        for p in members:
            accounts[p.key] = (short_id(gid), f"{named.label} · {plan}" if plan else named.label)
    results, seen, firsts, more = [], set(), set(), False
    for t, p, f, st in all_transcripts(ctx):
        inode = (st.st_dev, st.st_ino)
        if inode in seen:
            continue
        seen.add(inode)
        sid = f.stem
        said = prompts.get(sid, [])
        name, cwd, messages = ctx.links.chat_info(f, st)
        if not messages or not all(w in "\n".join([name, cwd, *said]).casefold() for w in words):
            continue
        # of a chat and its copies (Claude's forks start with its entries), the newest
        first = ctx.links.first_uuid(f)
        if first and first in firsts:
            continue
        firsts.add(first)
        if len(results) >= limit:
            more = True
            break
        home_p = p
        if st.st_nlink > 1:  # a shared chat: under the account it was used on last
            holders = [q for q in profiles if same_file(transcript_path(q, sid), st)]
            home_p = max(holders or [p], key=lambda q: used.get((q.real, sid), 0))
        hit = next((s for s in reversed(said) if all(w in s.casefold() for w in words)), None) \
            or next((s for s in reversed(said) if any(w in s.casefold() for w in words)), None)
        gid, account = accounts.get(home_p.key, ("", home_p.label))
        results.append({
            "key": f"{home_p.key}/{sid}/found",
            "sessionId": sid,
            "name": name or sid[:8],
            "cwd": cwd,
            "cwdShort": short_path(cwd, home),
            "lastActive": int(t * 1000),
            "profile": home_p.label,
            "profileDir": home_p.key,
            "groupId": gid,
            "account": account,
            "shared": st.st_nlink > 1,
            "match": snippet(hit, words) if hit and hit.strip() != name.strip() else "",
        })
    found = {"ok": True, "results": results, "more": more, **find_more(ctx, words, accounts)}
    ctx.links.save()
    return found


FIND_MORE_LIMIT = 10
FIND_TEXT_BYTES = 200_000


def find_more(ctx, words, accounts):
    """{"projects", "plans", "memories"} with every word: the folders chats
    ran in (by their path; the newest chat's account), the plans (title or
    text) and Claude's memories (name, description or text), newest first."""
    def has(*texts):
        text = "\n".join(t for t in texts if t).casefold()
        return all(w in text for w in words)

    def body(path):
        try:
            with open(path, "rb") as f:
                return f.read(FIND_TEXT_BYTES).decode("utf-8", "replace")
        except OSError:
            return ""

    folders, seen = {}, set()
    for t, p, f, st in all_transcripts(ctx):
        if (st.st_dev, st.st_ino) in seen:  # (a chat shared between accounts: once)
            continue
        seen.add((st.st_dev, st.st_ino))
        _, cwd, messages = ctx.links.chat_info(f, st)
        if not cwd or not messages:
            continue
        entry = folders.get(cwd)
        if entry is None:
            if not has(short_path(cwd, ctx.home), cwd):
                folders[cwd] = None
                continue
            gid, account = accounts.get(p.key, ("", p.label))
            folders[cwd] = {"folder": cwd, "folderShort": short_path(cwd, ctx.home), "lastActive": int(t * 1000),
                            "chats": 0, "profileDir": p.key, "profile": p.label, "groupId": gid, "account": account}
        if folders[cwd] is not None:
            folders[cwd]["chats"] += 1
    projects = [e for e in folders.values() if e is not None][:FIND_MORE_LIMIT]

    plans, seen = [], set()
    for p in ctx.profiles:
        try:
            files = list(p.path.glob("plans/*.md"))
        except OSError:
            continue
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            if not PLAN_SLUG.match(f.stem) or (st.st_dev, st.st_ino) in seen:
                continue
            seen.add((st.st_dev, st.st_ino))
            title = plan_title(f) or f.stem
            if has(title, body(f)):
                gid, account = accounts.get(p.key, ("", p.label))
                plans.append({"key": f"{p.key}/{f.stem}/plan", "slug": f.stem, "title": title, "path": str(f),
                              "profileDir": p.key, "profile": p.label, "groupId": gid, "account": account,
                              "updatedAt": int(st.st_mtime * 1000)})
    plans.sort(key=lambda x: -x["updatedAt"])

    memories = []
    for gid, members in group_profiles(ctx.profiles):
        for m in account_memories({"profileKeys": [p.key for p in members]}, ctx, limit=1000):
            if has(m["name"], m["description"], body(m["path"])):
                memories.append({**m, "groupId": short_id(gid), "account": accounts.get(m["profileDir"], ("", m["profile"]))[1]})
    memories.sort(key=lambda x: -x["updatedAt"])
    return {"projects": projects, "plans": plans[:FIND_MORE_LIMIT], "memories": memories[:FIND_MORE_LIMIT]}


# ---------------------------------------------------------------- opening sessions
#
# Shell commands that reopen, stop or start a chat with the right
# CLAUDE_CONFIG_DIR (used by jump.py here and by the widget for remote hosts).


def service_config_dir(profile):
    """CLAUDE_CONFIG_DIR value of the background service running for this profile, if any.

    The service is keyed by the config-dir *path* (its socket lives in
    /tmp/cc-daemon-<uid>/<sha256(path)[:8]>) but locks the real folder. When a
    folder has two names (~/.claude linked to ~/.claude-personal), a client
    using the other name waits forever at "Starting background service...".
    """
    status = read_json(profile.path / "daemon.status.json")
    if not isinstance(status, dict):
        return None
    pid = status.get("supervisorPid")
    if not is_alive({"pid": pid, "procStart": status.get("supervisorProcStart")}):
        return None
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return None
    value = next((e[len(b"CLAUDE_CONFIG_DIR="):].decode(errors="replace") for e in env
                  if e.startswith(b"CLAUDE_CONFIG_DIR=")), None)
    return value or str(Path.home() / ".claude")  # unset means the default path


def job_config_dir(profile, job):
    """The CLAUDE_CONFIG_DIR a background job was started with (its state.json
    providerEnv), if that is a name of this profile's folder."""
    env = (job or {}).get("providerEnv")
    value = env.get("CLAUDE_CONFIG_DIR") if isinstance(env, dict) else None
    if not isinstance(value, str) or not value:
        return None
    try:
        return value if Path(value).resolve() == profile.path.resolve() else None
    except OSError:
        return None


def names_profile(value, profile):
    """Does the config dir name value lead to this profile's folder now?"""
    try:
        return Path(value).expanduser().resolve() == profile.path.resolve()
    except (OSError, RuntimeError):
        return False


def config_dir_value(profile, home=None, job=None, for_job=False):
    """CLAUDE_CONFIG_DIR to run claude with for this profile, or None to unset it.

    Always named explicitly (a terminal's shell may export another one): the
    running service's own name if one runs, else the name the job was started
    with (a service started under another name of the folder leaves the job's
    worker and the service apart), else ~/.claude when it links to this
    profile (what terminals and VS Code use with linked profiles), else the
    profile's folder. Only a plain (unlinked) ~/.claude runs without the
    variable: its account file is ~/.claude.json, which Claude reads only
    when CLAUDE_CONFIG_DIR is unset.

    A service started as ~/.claude keeps that name after you switch the link
    to another profile. Attaching to or stopping one of its jobs (for_job)
    still goes through it; a new or resumed chat is this profile's, so it
    gets a name that still leads here (else it would open on the account the
    link points to now).
    """
    default = (home or Path.home()) / ".claude"
    if profile.is_default and not profile.path.is_symlink():
        return None
    service = service_config_dir(profile)
    if service is not None and not for_job and not names_profile(service, profile):
        service = None
    path = service or job_config_dir(profile, job)
    if path is None:
        linked_here = default.is_symlink() and default.resolve() == profile.path.resolve()
        path = str(default if linked_here else profile.path.resolve())
    return path


def trusted_folders(profile, home=None):
    """The folders Claude's "do you trust this folder?" question was answered
    yes for (in this profile's account file)."""
    home = home or Path.home()
    data = read_json(profile.path / ".claude.json")
    if profile.is_default and not profile.path.is_symlink() and not (data or {}).get("projects"):
        data = read_json(home / ".claude.json")
    projects = (data or {}).get("projects")
    if not isinstance(projects, dict):
        return set()
    return {k for k, v in projects.items() if isinstance(v, dict) and v.get("hasTrustDialogAccepted") is True}


def git_root(folder):
    for q in (folder, *folder.parents):
        if (q / ".git").exists():
            return q
    return None


def folder_trusted(profile, folder, home=None, trusted=None):
    """Has Claude's trust question been answered yes for folder, or (as
    Claude looks) for a folder above it, within its git repository if it's in
    one? Claude never saves it for the home folder, so it asks there every
    time. trusted: trusted_folders(profile), if read already."""
    trusted = trusted_folders(profile, home) if trusted is None else trusted
    if not trusted or not isinstance(folder, str) or not folder.startswith("/"):
        return False
    path = Path(folder)
    if path == (home or Path.home()):
        return False
    root = git_root(path)
    for q in (path, *path.parents):
        if str(q) in trusted:
            return True
        if q == root:
            return False
    return False


# Claude refuses attach, stop, logs, rm and --bg ("'attach' is disabled by
# CLAUDE_CODE_DISABLE_AGENT_VIEW") while this is set: a desktop may have it
# for every program, and new chats in terminals get it (new_chat_command)
NO_AGENT_VIEW = "CLAUDE_CODE_DISABLE_AGENT_VIEW"


def config_env(profile, home=None, job=None, for_job=False):
    """Shell words that set (or unset) CLAUDE_CONFIG_DIR for this profile;
    for a background session's command (for_job), without NO_AGENT_VIEW."""
    value = config_dir_value(profile, home, job, for_job)
    if for_job:
        return f"env -u {NO_AGENT_VIEW} " + ("-u CLAUDE_CONFIG_DIR" if value is None else f"CLAUDE_CONFIG_DIR={shlex.quote(value)}")
    return "env -u CLAUDE_CONFIG_DIR" if value is None else f"CLAUDE_CONFIG_DIR={shlex.quote(value)}"


def claude_words(claude):
    """Shell words that run claude: its name or path, with its folder first on
    PATH when Node lives there too (an npm install under nvm runs
    `#!/usr/bin/env node`, and a terminal's shell may not have that folder)."""
    folder = os.path.dirname(claude)
    if folder and os.path.isfile(os.path.join(folder, "node")):
        return f'PATH={shlex.quote(folder)}:"$PATH" {shlex.quote(claude)}'
    return shlex.quote(claude)


def claude_command(profile, args, home=None, job=None, folder=None, claude="claude", for_job=False):
    """Shell command running claude with args on this profile, in folder."""
    command = (f"{config_env(profile, home, job, for_job)} {claude_words(claude)} "
               f"{' '.join(shlex.quote(a) for a in args)}")
    if isinstance(folder, str) and folder and not folder_trusted(profile, folder, home):
        # The question that comes up has "No, exit" preselected: say what it is
        note = (f"Claude will ask whether you trust {short_folder(folder, home)} before it reopens this session. "
                "Choose \"Yes, I trust this folder\" to continue; \"No, exit\" (preselected) closes it.")
        command = f"printf '%s\\n\\n' {shlex.quote(note)}; {command}"
    return command


def attach_command(profile, job_id, home=None, job=None, claude="claude"):
    """Shell command that attaches to a background job of this profile."""
    return claude_command(profile, ["attach", job_id], home, job, (job or {}).get("cwd"), claude, for_job=True)


def resume_command(profile, session_id, home=None, folder=None, claude="claude"):
    """Shell command that reopens a finished chat on this profile."""
    return claude_command(profile, ["--resume", session_id], home, None, folder, claude)


MODEL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._\[\]-]{0,79}$")  # an alias (opus, sonnet, fable) or a model's full name
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# claude --permission-mode's ("manual" is newer Claude's name for "default",
# which older ones on SSH hosts know only by that)
PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")


def chat_options(model=None, effort=None, mode=None):
    """claude's options for a new chat's model, reasoning effort and
    permission mode (none for Claude's own defaults); ValueError for one
    that isn't."""
    words = []
    if model:
        if not MODEL_NAME.match(model):
            raise ValueError(f"Not a model name: {model}")
        words += ["--model", model]
    if effort:
        if effort not in EFFORT_LEVELS:
            raise ValueError(f"Not an effort level: {effort} (it's low, medium, high, xhigh or max)")
        words += ["--effort", effort]
    if mode:
        mode = "default" if mode == "manual" else mode
        if mode not in PERMISSION_MODES:
            raise ValueError(f"Not a permission mode: {mode} (it's {', '.join(PERMISSION_MODES)})")
        words += ["--permission-mode", mode]
    return words


# A picture pasted into a new chat's task is saved in the cache's images/
# (jump.py --paste-image; an SSH host's chat gets a copy in the host's,
# remote.py --put-image), and the task names it: [Image: /…/images/NAME.png].
# Outside the chat's folder, Claude would ask before it opens it: a chat whose
# task names one starts with that folder added. Pictures go after
# IMAGES_KEEP_DAYS, by when their chats have long read them (a task waits a
# week at most).
IMAGES_KEEP_DAYS = 30


def images_dir(home):
    return cache_dir(home) / "images"


def picture_options(home, text):
    """claude's options for a chat whose task names a pasted picture: its
    folder (one word: --add-dir FOLDER would take the prompt as a second folder)."""
    folder = str(images_dir(home))
    return [f"--add-dir={folder}"] if folder + os.sep in (text or "") else []


def new_chat_command(profile, home=None, claude="claude", options=()):
    """Shell command starting a new chat on this profile (options: claude's,
    e.g. chat_options'). With background sessions around, plain `claude` opens
    the agent view (where a task becomes a new background session): ask for a
    normal chat."""
    return (f"CLAUDE_CODE_DISABLE_AGENT_VIEW=1 {config_env(profile, home)} {claude_words(claude)}"
            + "".join(" " + shlex.quote(w) for w in options))


def login_command(profile, home=None, claude="claude"):
    """Shell command logging this profile in to a Claude account: Claude's
    own `claude auth login`, which keeps the login in the profile's folder."""
    note = (f"Log in to Claude for {profile.label} ({short_folder(str(profile.path), home)}): sign in with the "
            "account this folder is for. Claude opens your browser; where it can't (over ssh), open the address "
            "it shows and paste the code you get back here.")
    return f"printf '%s\\n\\n' {shlex.quote(note)}; {config_env(profile, home)} {claude_words(claude)} auth login"


def stop_command(profile, job_id, home=None, job=None, claude="claude"):
    """Shell command stopping a background job (its conversation is kept)."""
    return f"{config_env(profile, home, job, for_job=True)} {claude_words(claude)} stop {shlex.quote(job_id)}"


def short_folder(folder, home=None):
    home = str(home or Path.home())
    return "~" + folder[len(home):] if folder == home or folder.startswith(home + "/") else folder


# ---------------------------------------------------------------- starting a task
#
# A new chat can start with a task, its first prompt (maybe about one of the
# account's plans): in a terminal (`claude "<task>"`, which sends it at once),
# or in the background (`claude --bg "<task>"`: Claude's background service
# runs it and it's listed right away). The background needs the folder
# trusted already: Claude refuses otherwise ("Workspace not trusted"), and
# always in the home folder.

TASK_PLAN_MAX = 100_000  # bytes of a plan that go into a prompt
# ...as long as the prompt fits in one argument (128 KB) once quoted for an
# SSH host's terminal: in the host's command, its sh -c and the ssh line
TASK_ARG_MAX = 120_000
TASK_ARG_QUOTES = 3
TASK_PLAN_CUT = "\n\n(The plan goes on in that file.)"
BACKGROUND_WAIT = 60  # seconds `claude --bg` gets (it may start Claude's background service first)
# What a Claude session puts into the environment of what it runs: a claude
# started with them would count as part of that session
SESSION_ENV = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
               "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
               "CLAUDE_CODE_SESSION_ID", "CLAUDE_EFFORT", "CLAUDE_PID", "CLAUDE_JOB_DIR")
SHORT_ID = re.compile(r"\b([0-9a-f]{8})\b")
ONE_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


def find_plan(profiles, slug, profile_key=None):
    """The plan file <cfg>/plans/<slug>.md (on the profile profile_key, else
    any), or None."""
    for p in profiles:
        if profile_key and not matches_profile(p, profile_key):
            continue
        path = plan_path(p, slug)
        if path is not None and path.is_file():
            return path
    return None


def task_prompt(text, plan=None, home=None):
    """A task's first prompt: what you wrote, and the plan (a path) it's about."""
    text = (text or "").strip()
    if plan is None:
        return text
    with open(plan, "rb") as f:
        data = f.read(TASK_PLAN_MAX + 1)
    body, where = data[:TASK_PLAN_MAX].decode("utf-8", "replace"), short_folder(str(plan), home)
    cut = len(data) > TASK_PLAN_MAX
    while True:
        prompt = plan_prompt(text, body.rstrip() + TASK_PLAN_CUT if cut else body, where)
        quoted = prompt
        for _ in range(TASK_ARG_QUOTES):  # each quote turns a ' into 4 more characters
            quoted = shlex.quote(quoted)
        if len(quoted.encode("utf-8", "replace")) <= TASK_ARG_MAX or not body:
            return prompt
        shorter = body[:len(body) * 9 // 10]
        body, cut = shorter[:shorter.rfind("\n") + 1] or shorter, True  # at a line's end


def plan_prompt(text, body, where):
    """A first prompt about a plan: your text (else "Implement this plan."),
    then the plan, which is saved as where."""
    return f"{(text or '').strip() or 'Implement this plan.'}\n\nThe plan (saved as {where}):\n\n{body.strip()}\n"


def prompt_arg(prompt):
    """The prompt as an argument claude takes as its prompt, not an option
    nor, one word alone, one of its commands (`claude update`, `claude doctor`)."""
    return " " + prompt if prompt.startswith("-") or ONE_WORD.fullmatch(prompt) else prompt


def prompt_word(prompt):
    return shlex.quote(prompt_arg(prompt))


def task_command(profile, prompt, home=None, claude="claude", options=()):
    """Shell command starting a chat on this profile with prompt sent as its first message."""
    return f"{new_chat_command(profile, home, claude, options)} {prompt_word(prompt)}"


def start_background(profile, folder, prompt, home=None, claude="claude", wait=BACKGROUND_WAIT, prefix=(),
                     resume=None, options=()):
    """Start a background session on this profile in folder, with prompt:
    (ok, message, its short id or None). prefix: words claude runs behind,
    e.g. systemd-run's, so a background service it starts lives on its own
    (not in the widget's group of processes, which a restart of the desktop
    shell ends: jump.scope_prefix). resume: the id of a chat it goes on
    with (claude --bg --resume: the same chat, if nothing has it open).
    options: claude's, e.g. chat_options' (going on with a chat: only
    go_on_options', else Claude would start a copy of it)."""
    import subprocess
    home = home or Path.home()
    if not prompt.strip():
        return False, "Write what Claude should do first", None
    if not os.path.isdir(folder):
        return False, f"The folder {short_folder(folder, home)} isn't there", None
    if not folder_trusted(profile, folder, home):
        where = "your home folder" if Path(folder) == home else short_folder(folder, home)
        return False, (f"Claude doesn't trust {where} yet, and a background session can't ask: start a chat there "
                       "in a terminal once (Claude asks whether you trust it), or pick a project folder"), None
    env = {k: v for k, v in os.environ.items() if k not in SESSION_ENV and k != NO_AGENT_VIEW}
    value = config_dir_value(profile, home, for_job=True)  # through the service's own name
    if value is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = value
    folder_bin = os.path.dirname(claude)
    if folder_bin and os.path.isfile(os.path.join(folder_bin, "node")):
        env["PATH"] = folder_bin + os.pathsep + env.get("PATH", "")
    argv = [*prefix, claude, "--bg", *(["--resume", resume] if resume else []), *options, prompt_arg(prompt)]
    try:
        r = subprocess.run(argv, cwd=folder, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=wait)
    except OSError as exc:
        return False, f"Couldn't run claude: {exc}", None
    except subprocess.TimeoutExpired:
        return False, f"claude --bg didn't answer within {wait} s", None
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    if r.returncode != 0:
        lines = [l.strip() for l in (err + "\n" + out).splitlines() if l.strip()]
        return False, lines[-1] if lines else f"claude --bg failed (exit code {r.returncode})", None
    found = SHORT_ID.findall(out)
    job = found[-1] if found else None
    return True, (f"Started in the background on {profile.label}" + (f" ({job})" if job else "")), job


def run_task(home, extra_dirs, background, folder, profile_key, text, plan_slug=None, plan_profile=None,
             claude=None, model=None, effort=None, mode=None):
    """A task on the profile profile_key in folder (here or on an SSH host,
    --start-task / --task-command), with that model, effort and permission
    mode: started in
    the background, or the shell command that starts it in a terminal.
    {"ok", "message", "id" | "command"}."""
    profiles = discover_profiles(home, extra_dirs)
    profile = next((p for p in profiles if matches_profile(p, profile_key)), None)
    if profile is None:
        return {"ok": False, "message": f"No Claude profile called {profile_key}"}
    options = chat_options(model, effort, mode) + picture_options(home, text)
    plan = None
    if plan_slug:
        plan = find_plan(profiles, plan_slug, plan_profile)
        if plan is None:
            return {"ok": False, "message": "That plan isn't there any more"}
    prompt = task_prompt(text, plan, home)
    folder = folder if isinstance(folder, str) and folder.startswith("/") else str(home)
    claude = claude or find_claude(home, ask_shell=True)
    if background:
        ok, message, job = start_background(profile, folder, prompt, home, claude, options=options)
        return {"ok": ok, "message": message, "id": job}
    command = (task_command(profile, prompt, home, claude, options) if prompt
               else new_chat_command(profile, home, claude, options))
    return {"ok": True, "message": "", "command": in_folder(folder, command)}


CONTINUE_PROMPT = "The usage limit has reset: go on where you stopped."
# The permission modes a chat goes on in, as it was. Not bypassPermissions:
# Claude refuses to start a background session in it unless you accepted it
# in a way it counts (and a go-on that doesn't start is worse than one that
# asks before it acts).
GO_ON_MODES = ("default", "manual", "acceptEdits", "plan", "auto", "dontAsk")


def go_on_options(profile, chat_id, mode):
    """claude's options for going on with a chat in the background (claude
    --bg --resume), whose permission mode is mode: that mode, so the
    background session it becomes saves it and goes on in it each time it's
    woken (with none, the first go-on keeps the chat's mode but the session
    saves none, and the next wakes up in Claude's default one). None when a
    background session of it is there already: Claude wakes that one with
    the options it saved, and with any given here it would start a copy."""
    if mode not in GO_ON_MODES or (profile.path / "jobs" / chat_id[:8]).exists():
        return []
    return ["--permission-mode", mode]


def continue_chat(home, extra_dirs, profile_key, chat_id, folder=None, text="", claude=None, prefix=(),
                  wait=None, force=False):
    """Go on with a chat a usage limit stopped, now that the limit has reset
    (a queued one: jump.py --start-queued; here or on an SSH host,
    --continue-chat): in the background on its account, with text (else
    CONTINUE_PROMPT) as your next message, in its permission mode
    (go_on_options). A session that has it open idle ends first
    (end_for_handover), then claude --bg --resume goes on with the same
    chat. {"ok", "message", "id"}; ok with no message when it went on
    already (its newest turn isn't the limit's any more): nothing to do.
    force: go on whatever its newest turn (a chat that keeps going whose turn
    broke off, or that the usage cap stopped)."""
    profiles = discover_profiles(home, extra_dirs)
    profile = next((p for p in profiles if matches_profile(p, profile_key)), None)
    if profile is None:
        return {"ok": False, "message": f"No Claude profile called {profile_key}"}
    path = transcript_path(profile, chat_id)
    if path is None:
        return {"ok": False, "message": "That chat isn't on this account any more"}
    peek = chat_peek(path) or {}
    if not force and (peek.get("stopped") or {}).get("kind") != "limit":
        return {"ok": True, "message": ""}
    if not (isinstance(folder, str) and os.path.isdir(folder)):
        folder = chat_folder(path) or str(home)
    claude = claude or find_claude(home, ask_shell=True)
    ok, message = end_for_handover(home, profiles, chat_id, claude)
    if not ok:
        return {"ok": False, "message": message}
    # (claude --bg --resume starts a copy while a process still has the chat open)
    deadline = time.time() + (END_WAIT if wait is None else wait)
    while any(e.get("sessionId") == chat_id for e, _, _ in live_entries_all(profiles)) and time.time() < deadline:
        time.sleep(0.2)
    # (a picture your message names can be opened, unless Claude wakes a
    # background session of it, with the options it saved)
    pictures = [] if (profile.path / "jobs" / chat_id[:8]).exists() else picture_options(home, text)
    ok, message, job = start_background(profile, folder, (text or "").strip() or CONTINUE_PROMPT, home, claude,
                                        prefix=prefix, resume=chat_id,
                                        options=go_on_options(profile, chat_id, peek.get("mode")) + pictures)
    if ok:
        message = f"Went on in the background on {profile.label}" + (f" ({job})" if job else "")
    return {"ok": ok, "message": message, "id": job}


def plan_markdown(home, extra_dirs, slug, profile_key=None):
    """{"ok", "title", "markdown", "path"} of a plan (--plan-text, for an SSH
    host's plan opened here)."""
    path = find_plan(discover_profiles(home, extra_dirs), slug, profile_key)
    if path is None:
        return {"ok": False, "message": "That plan isn't there any more"}
    with open(path, "rb") as f:
        markdown = f.read(TASK_PLAN_MAX * 10).decode("utf-8", "replace")
    return {"ok": True, "message": "", "title": plan_title(path) or slug, "markdown": markdown, "path": str(path),
            "where": short_folder(str(path), home)}


# ---------------------------------------------------------------- stopping and interrupting chats
#
# A chat with a process of its own (in a terminal, or in VS Code's Claude
# panel) can be stopped from the widget, whatever it's doing; one in tmux can
# be interrupted too.
#
# Stopping: Claude gets the signal its own host would send and shuts down
# cleanly. In a terminal that's SIGTERM, as when the terminal closes: the
# current step is cut off, the terminal stays open, and `claude --resume`
# reopens the chat. VS Code's panel runs Claude in print mode, where SIGINT
# cancels the turn and exits with code 0: the panel takes that like its own
# stop button (the tab says "Interrupted") and starts Claude again with the
# next message; SIGTERM would show an error there. Commands it was running
# that outlive it are ended too (the panel's Claude leaves them behind), and
# a Claude that hangs on is killed after END_WAIT seconds.
#
# Interrupting (tmux only): Ctrl+C, sent to its pane as if pressed there,
# which does what the stop button of VS Code's Claude tab does: the turn stops
# and so do its subagents, background ones too (Esc would leave those
# running), while the chat stays open, a draft you typed included. Other
# terminals take no input from outside.

END_WAIT = 10  # seconds Claude gets to shut down before it's killed
LEFTOVER_WAIT = 3  # seconds its leftover commands get after SIGTERM


def process_gone(entry):
    """Has the session's process exited (an unreaped zombie counts)?"""
    if not is_alive(entry):
        return True
    try:
        with open(f"/proc/{entry['pid']}/stat", encoding="utf-8", errors="replace") as f:
            stat = f.read()
    except OSError:
        return True
    return stat[stat.rfind(")") + 2:][:1] in ("Z", "X")


def process_tree(pid):
    """[(pid, start time)] of every process below pid."""
    children = {}
    try:
        names = [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return []
    for name in names:
        try:
            with open(f"/proc/{name}/stat", encoding="utf-8", errors="replace") as f:
                stat = f.read()
            fields = stat[stat.rfind(")") + 2:].split()
            children.setdefault(int(fields[1]), []).append((int(name), fields[19]))
        except (OSError, ValueError, IndexError):
            continue
    found, todo = [], [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            found.append(child)
            todo.append(child[0])
    return found


def end_leftovers(tree, wait=LEFTOVER_WAIT):
    """End what's left of a stopped chat's processes (same pid and start time
    as before, so never a newcomer with a reused pid): SIGTERM, then SIGKILL.
    Returns how many were still running."""
    import signal
    alive = lambda: [p for p in tree if not process_gone({"pid": p[0], "procStart": p[1]})]
    left = alive()
    for pid, _ in left:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + wait
    while alive() and time.time() < deadline:
        time.sleep(0.1)
    for pid, _ in alive():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return len(left)


def stop_chat(entry, busy_ok=False, wait=END_WAIT):
    """Stop the process of a chat (see above): (ok, message). Unless busy_ok
    (you were told it's working or waiting), only an idle one: a chat that got
    back to work since you asked isn't touched."""
    import signal
    if entry.get("kind") != "interactive":
        return False, "Only chats in a terminal or in VS Code can be stopped this way"
    state = session_state(entry)
    if not busy_ok and state != "idle":
        return False, ("It's working again" if state == "working" else "It's waiting for you now") + ", so it wasn't stopped"
    if process_gone(entry):
        return True, "It had quit already"
    tree = process_tree(entry["pid"])
    sig = signal.SIGINT if entry.get("entrypoint") == "claude-vscode" else signal.SIGTERM
    try:
        os.kill(entry["pid"], sig)
    except ProcessLookupError:
        return True, "It had quit already"
    except OSError as exc:
        return False, f"Couldn't stop it: {exc}"
    deadline = time.time() + wait
    while not process_gone(entry) and time.time() < deadline:
        time.sleep(0.1)
    killed = not process_gone(entry)
    if killed:  # checked again just before: the pid mustn't be someone else's by now
        try:
            os.kill(entry["pid"], signal.SIGKILL)
        except OSError:
            pass
    leftovers = end_leftovers(tree)
    message = (f"It hadn't quit after {wait} s, so it was killed; the chat is kept" if killed
               else "Stopped; the chat is kept")
    if leftovers:
        message += f" (and {leftovers} command{'s' if leftovers > 1 else ''} it was running ended)"
    return True, message


def tmux_socket(pid):
    """The socket of the tmux server a process runs under ($TMUX), or None."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            env = f.read().split(b"\0")
    except OSError:
        return None
    value = next((e[5:].decode(errors="replace") for e in env if e.startswith(b"TMUX=")), "")
    socket = value.split(",", 1)[0]
    return socket if socket.startswith("/") else None


def parent_pid(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            stat = f.read()
        return int(stat[stat.rfind(")") + 2:].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def is_vscode_terminal_host(pid):
    """Is pid VS Code's terminal host: one of VS Code's own processes on a
    desktop, `node … --type=ptyHost` in the VS Code server of a host that a
    Remote-SSH window works on?"""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
            if f.read().strip() in {c for c, _ in VSCODE_APPS}:
                return True
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"--type=ptyHost" in f.read().split(b"\0")
    except OSError:
        return False


def vscode_terminal(pid):
    """The process a VS Code terminal started, that pid runs under, or None
    when it runs in no VS Code terminal. VS Code's extensions know a terminal
    by that process (its processId), so the Talpy Den extension can bring
    the chat's terminal up, even with the panel closed."""
    child, seen = None, set()
    while isinstance(pid, int) and pid > 1 and pid not in seen and len(seen) < 64:
        if child is not None and is_vscode_terminal_host(pid):
            return child
        seen.add(pid)
        child, pid = pid, parent_pid(pid)
    return None


def interrupt_chat(entry):
    """Interrupt a working or waiting chat in tmux: Ctrl+C in its pane, as if
    you pressed it there (the turn and its subagents stop; the chat stays).
    Only while it works: at an idle prompt a second Ctrl+C would end Claude.
    (ok, message)"""
    import shutil
    import subprocess
    pane = entry.get("tmux")
    if not isinstance(pane, str) or not pane:
        return False, "It doesn't run in tmux, so it can't be interrupted from here; stop it instead"
    state = session_state(entry)
    if state not in ("working", "waiting"):
        return False, "It isn't working any more, so there's nothing to interrupt"
    if entry.get("status") == "shell":  # its turn is over: Claude is at its prompt
        return False, "Its turn is over (only a background command still runs), so a Ctrl+C would land at its prompt"
    tmux = shutil.which("tmux", path=os.pathsep.join([os.environ.get("PATH", ""), "/usr/bin", "/usr/local/bin", "/bin"]))
    if tmux is None:
        return False, "tmux isn't installed here"
    socket = tmux_socket(entry["pid"])
    target = pane.rsplit(".", 1)[-1] if "." in pane else pane  # its pane id, e.g. %3
    try:
        r = subprocess.run([tmux, *(["-S", socket] if socket else []), "send-keys", "-t", target, "C-c"],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"tmux didn't answer: {exc}"
    if r.returncode != 0:
        lines = [l for l in (r.stderr + "\n" + r.stdout).splitlines() if l.strip()]
        return False, f"tmux: {lines[-1].strip() if lines else f'exit {r.returncode}'}"
    return True, "Interrupted (Ctrl+C in its tmux pane); the chat stays open there"


def find_chat_entry(home, extra_dirs, session_id, profile_key=None):
    """The live session entry of a chat with a process (terminal or VS Code),
    found by its session id and the profile it's listed under (a shared chat
    can run on two), or None."""
    for entry, runs_on, holder in live_entries_all(discover_profiles(home, extra_dirs)):
        if entry.get("sessionId") != session_id or entry.get("kind") not in SESSION_KINDS \
                or entry.get("spare") or entry.get("parkedJobId"):
            continue
        if profile_key and profile_key not in (runs_on.key, holder.key):
            continue
        return entry
    return None


def stop_chat_by_id(home, extra_dirs, session_id, profile_key=None, busy_ok=False, wait=END_WAIT):
    entry = find_chat_entry(home, extra_dirs, session_id, profile_key)
    return stop_chat(entry, busy_ok, wait) if entry else (False, "That session isn't running any more")


def interrupt_chat_by_id(home, extra_dirs, session_id, profile_key=None):
    entry = find_chat_entry(home, extra_dirs, session_id, profile_key)
    return interrupt_chat(entry) if entry else (False, "That session isn't running any more")


def stop_background(profile, job_id, job=None, home=None, claude=None):
    """Stop a background session with `claude stop` (its conversation is
    kept): (ok, message)."""
    import subprocess
    home = home or Path.home()
    claude = claude or find_claude(home, ask_shell=True)
    env = {k: v for k, v in os.environ.items() if k != NO_AGENT_VIEW}
    value = config_dir_value(profile, home, job, for_job=True)
    if value is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = value
    folder = os.path.dirname(claude)
    if folder and os.path.isfile(os.path.join(folder, "node")):  # an npm install under nvm needs its node
        env["PATH"] = os.pathsep.join([folder, env.get("PATH", "")])
    try:
        r = subprocess.run([claude, "stop", job_id], env=env, cwd=str(home), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"claude stop didn't finish: {exc}"
    lines = [l.strip() for l in (r.stdout + "\n" + r.stderr).splitlines() if l.strip()]
    if r.returncode != 0:
        return False, lines[-1] if lines else f"claude stop failed (exit {r.returncode})"
    return True, lines[-1] if lines else "Stopped"


def end_for_handover(home, profiles, chat_id, claude=None):
    """Before a chat continues on another account (say a usage limit stopped
    it here): end the session that has it open, if it's idle. A chat in a
    terminal quits as with the stop button, a background session is stopped
    (claude stop); one in VS Code's Claude isn't touched (its tab would start
    it again with your next message). (ok, message); ok if none has it open."""
    for entry, runs_on, _holder in live_entries_all(profiles):
        if entry.get("sessionId") != chat_id or entry.get("kind") not in SESSION_KINDS or entry.get("spare"):
            continue
        if entry.get("entrypoint") == "claude-vscode":
            return False, "It's open in VS Code's Claude: close its tab there first"
        if session_state(entry) != "idle":
            return False, "It's at work again, so it stays where it is"
        job_id = entry.get("jobId")
        if entry.get("kind") == "bg" and valid_id(job_id):
            return stop_background(runs_on, job_id, read_job([runs_on], job_id), home, claude)
        if entry.get("kind") == "interactive":
            return stop_chat(entry)
        return False, "It's open in a session the widget can't end"
    return True, "Nothing had it open"


# ---------------------------------------------------------------- several widgets at once
#
# The Plasma widget can run twice, e.g. on a panel and in the system tray.
# Each copy says it's there on every status check (--instance). The copy
# that has been running longest sends the notifications and plays the sounds,
# so they come once. Settings are shared: a copy saves them on every change
# (jump.py --save-settings) and the others take them over at their next check;
# a new copy takes over what the others use.

INSTANCE_GRACE_MS = 15000  # a copy not heard from for 3 of its check intervals plus this has gone


def elect_notifier(cache_root, instance, interval_ms, now_ms):
    """Note that this copy of the widget is running; is it the one that notifies?"""
    path = cache_root / "instances.json"
    private_dir(cache_root)
    with open(cache_root / "instances.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read_json(path)
        seen = {}
        for key, v in (data.items() if isinstance(data, dict) else []):
            # (a copy noted "in the future" was noted before the clock was set back: it counts as gone, too,
            # or a copy that has closed would stay the one that notifies until the clock catches up)
            if isinstance(v, dict) and number(v.get("seen")) is not None and number(v.get("since")) is not None \
                    and abs(now_ms - v["seen"]) <= 3 * (number(v.get("interval")) or 2000) + INSTANCE_GRACE_MS:
                seen[key] = v
        me = seen.get(instance)
        seen[instance] = {"since": me["since"] if me else now_ms, "seen": now_ms, "interval": interval_ms}
        write_json_atomic(path, seen)
    first = min(seen.items(), key=lambda kv: (kv[1]["since"], kv[0]))[0]
    return first == instance


def config_defaults(main_xml=None):
    """{key: default} of the widget's settings (contents/config/main.xml)."""
    import xml.etree.ElementTree as ET
    path = main_xml or Path(__file__).resolve().parent.parent / "config" / "main.xml"
    try:
        root = ET.parse(str(path)).getroot()
    except (OSError, ET.ParseError):
        return {}
    ns = {"k": "http://www.kde.org/standards/kcfg/1.0"}
    defaults = {}
    for entry in root.iterfind(".//k:entry", ns):
        kind, text = entry.get("type"), (entry.findtext("k:default", "", ns) or "").strip()
        defaults[entry.get("name")] = (text == "true" if kind == "Bool" else int(text) if kind == "Int" and text
                                       else 0 if kind == "Int" else text)
    return defaults


def shared_settings(cache_root):
    """The settings the widgets share: {"at", "by", "values"}, or None."""
    data = read_json(cache_root / "settings.json")
    if not isinstance(data, dict) or not isinstance(data.get("values"), dict) or number(data.get("at")) is None:
        return None
    return {"at": data["at"], "by": str(data.get("by") or ""), "values": data["values"]}


def save_shared_settings(cache_root, by, values, now_ms, unless_default=False):
    """Save a widget's settings for the others (only known keys). With
    unless_default, settings that are all defaults (a widget nobody set up
    yet) aren't saved: they mustn't replace what another one uses."""
    defaults = config_defaults()
    values = {k: v for k, v in values.items() if k in defaults} if defaults else dict(values)
    if unless_default and defaults and all(values.get(k, d) == d for k, d in defaults.items()):
        return False, "Nothing set up yet"
    private_dir(cache_root)
    write_json_atomic(cache_root / "settings.json", {"at": now_ms, "by": by, "values": values})
    os.chmod(cache_root / "settings.json", 0o600)  # your SSH hosts are in it
    return True, "Settings saved"


# ---------------------------------------------------------------- prompt history

RECENT_PROJECTS = 6
HISTORY_TAIL_BYTES = 65536


def history_tail(profile):
    """Entries at the end of a profile's prompt history (history.jsonl)."""
    path = profile.path / "history.jsonl"
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - HISTORY_TAIL_BYTES))
            lines = f.read().splitlines()
    except OSError:
        return []
    if size > HISTORY_TAIL_BYTES:
        lines = lines[1:]  # probably cut in the middle
    entries = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            entries.append(e)
    return entries


def last_prompts(entries):
    """{session id: when you last sent it a prompt (ms)}."""
    last = {}
    for e in entries:
        sid, ts = e.get("sessionId"), number(e.get("timestamp"))
        if valid_id(sid) and ts:
            last[sid] = max(last.get(sid, 0), int(ts))
    return last


def recent_projects(histories, home):
    """Folders recently chatted in, newest first, from the profiles' prompt
    history (for "new chat in…"). Skips folders that are gone and scratch
    folders inside Claude's own config dirs."""
    last_used = {}
    for entries in histories:
        for e in entries:
            folder, ts = e.get("project"), number(e.get("timestamp"))
            if isinstance(folder, str) and folder.startswith("/") and ts:
                last_used[folder] = max(last_used.get(folder, 0), ts)
    result = []
    for folder in sorted(last_used, key=lambda f: -last_used[f]):
        if any(part.startswith(".claude") for part in Path(folder).parts) or not os.path.isdir(folder):
            continue
        result.append({"path": folder, "short": short_path(folder, home)})
        if len(result) >= RECENT_PROJECTS:
            break
    return result


# ---------------------------------------------------------------- plan limits


def snapshot_files(snapshot_root, profiles):
    """(mtime, path) of these profiles' status-line snapshots, newest first,
    leaving out ones filed under a link's name before the link was switched."""
    since = {}
    for p in profiles:
        for name, t in p.snapshot_sources.items():
            since[name] = max(t, since.get(name, t))
    files = []
    for name, t0 in since.items():
        try:
            with os.scandir(snapshot_root / name) as it:
                for e in it:
                    if e.name.endswith(".json") and not e.name.startswith("."):
                        try:
                            t = e.stat().st_mtime
                        except OSError:  # gone since it was listed (pruned by another copy): not the rest of the folder
                            continue
                        if t >= t0:
                            files.append((t, Path(e.path)))
        except OSError:
            continue
    files.sort(reverse=True)
    return files


def prune_snapshots(snapshot_root, now):
    try:
        dirs = list(snapshot_root.iterdir())
    except OSError:
        return
    for d in dirs:
        try:
            for f in d.iterdir():
                if now - f.stat().st_mtime > SNAPSHOT_MAX_AGE:
                    try:
                        f.unlink()
                    except FileNotFoundError:  # unlink(missing_ok=True) needs Python 3.8
                        pass
        except OSError:
            continue


def limit_window(key, pct, resets_at, now_ms):
    """A plan window as the widget shows it; one past its reset is at 0%."""
    resets = to_ms(resets_at)
    reset = resets is not None and resets <= now_ms
    return {
        "key": key,
        "label": WINDOW_LABELS.get(key, key.replace("_", " ")),
        "pct": 0.0 if reset else float(pct),
        "resetsAt": None if reset else resets,
        "reset": reset,
        "endedAt": resets if reset else None,  # when the window that's over ended
    }


def in_window_order(windows):
    rank = {k: i for i, k in enumerate(WINDOW_ORDER)}
    return sorted(windows, key=lambda w: rank.get(w["key"], len(rank)))


def cached_usage_limits(profiles, now_ms):
    """The group's plan windows from Claude Code's own usage cache (the newest
    of its profiles'), or None."""
    best = None
    for p in profiles:
        cache = getattr(p, "usage_cache", None)
        at = number(cache.get("fetchedAtMs")) if isinstance(cache, dict) else None
        usage = cache.get("utilization") if at is not None else None
        if not isinstance(usage, dict) or (best is not None and at <= best["updatedAt"]):
            continue
        windows = [limit_window(key, usage[key]["utilization"], usage[key].get("resets_at"), now_ms)
                   for key in WINDOW_ORDER
                   if isinstance(usage.get(key), dict) and number(usage[key].get("utilization")) is not None]
        if windows:
            best = {"updatedAt": int(at), "windows": in_window_order(windows)}
    return best


def plan_limits(snapshot_root, profiles, now_ms):
    """Rate-limit windows of the group: from its newest status-line snapshot,
    or from Claude Code's own usage cache when that's newer. (An account used
    only in VS Code's panel, which runs no status line, had numbers from the
    last time a terminal chat ran on it.)"""
    cached = cached_usage_limits(profiles, now_ms)
    for mtime, path in snapshot_files(snapshot_root, profiles):
        if cached is not None and cached["updatedAt"] >= int(mtime * 1000):
            break  # (newest first: the others are older still)
        snap = read_json(path)
        limits = snap.get("rate_limits") if isinstance(snap, dict) else None
        if not isinstance(limits, dict) or not limits:
            continue
        windows = [limit_window(key, w["used_percentage"], w.get("resets_at"), now_ms)
                   for key, w in limits.items()
                   if isinstance(w, dict) and number(w.get("used_percentage")) is not None]
        if not windows:
            continue
        return {"updatedAt": int(mtime * 1000), "windows": in_window_order(windows)}
    return cached


# ---------------------------------------------------------------- usage forecast
#
# Where a plan window's usage is heading at your pace. The status line only
# says how much is used now, so the checks keep a few points of each window
# (usage.json); the pace is the last hour's in the 5-hour window, and the
# window's so far in the weekly ones (or while there's no hour on record).

WINDOW_HOURS = {"five_hour": 5, "seven_day": 168, "seven_day_opus": 168, "seven_day_sonnet": 168}
PACE_LOOKBACK_MS = {"five_hour": 3600_000}
FORECAST_MIN_MS = 15 * 60_000  # too early in a window to tell its pace
USAGE_POINT_EVERY_MS = 10 * 60_000  # an unchanged value is still noted this often
USAGE_POINTS_KEPT = 300
USAGE_HISTORY_VERSION = 1


def window_hours(key):
    return WINDOW_HOURS.get(key) or (168 if str(key).startswith("seven_day") else None)


def same_window(a, b):
    """Two resets_at of the same window (they may differ by a few seconds)."""
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) < 120_000


class UsageHistory:
    """Each account's plan usage over its current windows: {group id:
    {window key: {"resetsAt", "points": [[ms, percent], …]}}}, in usage.json."""

    def __init__(self, path):
        self.path = path
        data = read_json(path)
        ok = isinstance(data, dict) and data.get("version") == USAGE_HISTORY_VERSION and isinstance(data.get("groups"), dict)
        self.groups = data["groups"] if ok else {}
        self.changed = False

    def points(self, gid, window):
        rec = (self.groups.get(gid) or {}).get(window["key"])
        if not isinstance(rec, dict) or not same_window(rec.get("resetsAt"), window.get("resetsAt")):
            return []
        return [p for p in rec.get("points") or [] if isinstance(p, list) and len(p) == 2]

    def record(self, gid, limits, now_ms):
        """Note the group's usage as of its newest status-line snapshot."""
        t = limits["updatedAt"]
        if not isinstance(self.groups.get(gid), dict):
            self.groups[gid] = {}
        group = self.groups[gid]
        for w in limits["windows"]:
            if w["reset"] or not w["resetsAt"] or not window_hours(w["key"]):
                continue
            rec = group.get(w["key"])
            if not isinstance(rec, dict) or not same_window(rec.get("resetsAt"), w["resetsAt"]) \
                    or not isinstance(rec.get("points"), list):
                rec = {"resetsAt": w["resetsAt"], "points": []}
            points = rec["points"]
            if points and t < points[-1][0] - 60_000:  # the clock was set back: those points are "from the future"
                points[:] = [p for p in points if p[0] <= t]
            last = points[-1] if points else None
            if last is None or (t > last[0] and (w["pct"] != last[1] or t - last[0] >= USAGE_POINT_EVERY_MS)):
                points.append([t, w["pct"]])
                del points[:-USAGE_POINTS_KEPT]
                group[w["key"]] = rec
                self.changed = True
        for key in [k for k, rec in group.items() if not isinstance(rec, dict)
                    or not isinstance(rec.get("resetsAt"), (int, float)) or rec["resetsAt"] < now_ms - 86400_000]:
            del group[key]  # a window that ended a day ago
            self.changed = True

    def save(self):
        if self.changed:
            try:
                write_json_atomic(self.path, {"version": USAGE_HISTORY_VERSION, "groups": self.groups})
            except OSError:
                pass


def usage_forecast(window, points, now_ms):
    """Where a plan window is heading at your pace, or None while that can't be told:
    evenPct: where an even pace through the window would be by now;
    rate: percent per hour; atReset: where it would be when the window resets;
    fullAt: when it would run out (ms), if that's before it resets;
    basis: "hour" (the last hour's pace) or "window" (the window's so far)."""
    hours = window_hours(window["key"])
    resets = window.get("resetsAt")
    if not hours or not resets or window.get("reset") or resets <= now_ms:
        return None
    length = hours * 3600_000
    start = resets - length
    elapsed = now_ms - start
    if elapsed < FORECAST_MIN_MS:
        return None
    pct = window["pct"]
    rate, basis = pct / (elapsed / 3600_000), "window"
    look = PACE_LOOKBACK_MS.get(window["key"])
    if look and now_ms - look > start:
        before = [p for p in points if p[0] <= now_ms - look]
        if before and now_ms > before[-1][0]:  # its pace since then (the point may be older than an hour)
            rate, basis = max(0.0, (pct - before[-1][1]) / ((now_ms - before[-1][0]) / 3600_000)), "hour"
    full_at = None
    if pct >= 100:
        at_reset = pct
    else:
        at_reset = pct + rate * (resets - now_ms) / 3600_000
        if rate > 0:
            when = now_ms + (100 - pct) / rate * 3600_000
            if when < resets:
                full_at = int(when)
    return {"evenPct": round(min(100.0, 100.0 * elapsed / length), 1), "rate": round(rate, 2),
            "atReset": round(min(at_reset, 999.0), 1), "fullAt": full_at, "basis": basis}


# ---------------------------------------------------------------- token totals


def empty_token_cache():
    return {"version": CACHE_VERSION, "scannedAt": 0, "files": {}, "messages": {}}


def transcript_files(profile):
    """(path, reached through a linked folder) of every transcript under the
    profile's projects/. Linked folders are followed: a shared chat's folder
    (its subagents) is a link into the shared store."""
    out, seen, stack = [], set(), [(profile.real / "projects", False)]
    while stack:
        folder, linked = stack.pop()
        try:
            real = os.path.realpath(folder)
            if real in seen:
                continue
            seen.add(real)
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir():
                    stack.append((Path(e.path), linked or e.is_symlink()))
                elif e.name.endswith(".jsonl") and e.is_file():
                    out.append((Path(e.path), linked))
            except OSError:
                continue
    return out


def token_cache_path(cache_root, profile):
    # Named after the real folder: ~/.claude can be switched between profiles
    return cache_root / f"tokens-{profile.key}-{short_id(str(profile.real))[:8]}.json"


TOKEN_CACHE_KEEP_S = 7 * 86400  # another profile's token cache, not updated for this long, goes
TOKEN_CACHE_NAME = re.compile(r"^tokens-.+-[0-9a-f]{8}\.json$")  # token_cache_path's names


def prune_token_caches(cache_root, profiles, now=None):
    """Delete token caches of profiles that are gone (or of an older format).
    Another copy of this script may watch other profiles (its own extra
    config dirs), so a cache this copy doesn't know is only deleted once no
    copy has updated it for a while."""
    keep = {token_cache_path(cache_root, p).name for p in profiles}
    now = time.time() if now is None else now
    try:
        files = list(cache_root.glob("tokens-*.json"))
    except OSError:
        return
    for f in files:
        older_format = not TOKEN_CACHE_NAME.match(f.name)
        if f.name not in keep and (older_format or now - (mtime(f) or now) > TOKEN_CACHE_KEEP_S):
            try:
                f.unlink()
            except OSError:
                pass


def scan_transcripts(profile, cache, now):
    """Incrementally read new transcript lines into cache["messages"].

    messages: {message id: [day, model, input, output, cacheRead, cacheWrite,
    shared, project folder, chat id]}, shared = 1 when the transcript is
    hard-linked elsewhere or sits in a linked folder (a chat shared with
    another profile: it counts on both accounts). Claude writes one line per
    content block, each repeating the message's usage (the last one sometimes
    with the final output count), so lines are merged by message id with an
    element-wise max. Messages are kept TOKEN_DAYS; their per-day sums
    (history, for the stats) STATS_DAYS, read back that far on a cache's first scan.
    """
    days_back = TOKEN_DAYS if "history" in cache else STATS_DAYS
    cutoff_day = (date.fromtimestamp(now) - timedelta(days=days_back - 1)).isoformat()
    cutoff_mtime = now - days_back * 86400
    if "history" in cache:
        messages, old_files = {k: v for k, v in cache["messages"].items() if v[0] >= cutoff_day}, cache["files"]
    else:  # a cache from before the stats: read everything again, STATS_DAYS back
        messages, old_files = {}, {}
    files = {}
    # each chat's tokens count for the folder it started in (Claude may cd elsewhere)
    started_in = cache.get("chatFolders") if isinstance(cache.get("chatFolders"), dict) else {}

    def add(line, shared, chat):
        if b'"usage"' not in line:
            return
        try:
            e = json.loads(line)
        except ValueError:
            return
        if not isinstance(e, dict) or e.get("type") != "assistant" or not isinstance(e.get("message"), dict):
            return
        m = e["message"]
        u = m.get("usage")
        if not isinstance(u, dict):
            return
        day = local_day(e.get("timestamp"))
        if day is None or day < cutoff_day:
            return
        vals = [int(number(u.get(k)) or 0) for k in (
            "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
        if not any(vals):
            return
        key = m.get("id") or e.get("requestId") or e.get("uuid")
        if not isinstance(key, str) or not key:
            return
        prev = messages.get(key)
        cwd = e.get("cwd") if isinstance(e.get("cwd"), str) else ""
        if cwd:
            cwd = started_in.setdefault(chat, cwd)
        if prev:
            vals = [max(a, b) for a, b in zip(prev[2:6], vals)]
            day = prev[0]
            shared = shared or bool(prev[6])
            cwd = cwd or (prev[7] if len(prev) > 7 else "")
        model = m.get("model") if isinstance(m.get("model"), str) and m.get("model") else "unknown"
        messages[key] = [day, model, *vals, int(shared), cwd, chat]

    shared_chats = set()  # the chats whose transcripts are shared now
    for path, linked in transcript_files(profile):
        try:
            st = path.stat()
        except OSError:
            continue
        chat = path.stem if path.parent.name != "subagents" else path.parent.parent.name
        if st.st_nlink > 1 or linked:
            shared_chats.add(chat)
        rec = old_files.get(str(path))
        offset = 0
        if rec and rec.get("ino") == st.st_ino and rec.get("offset", 0) <= st.st_size:
            offset = rec["offset"]
        if offset == 0 and st.st_mtime < cutoff_mtime:
            offset = st.st_size  # untouched for longer than we keep data
        elif offset < st.st_size:
            try:
                with open(path, "rb") as f:  # line by line: a first scan can be 50 MB
                    f.seek(offset)
                    for line in f:
                        if not line.endswith(b"\n"):
                            break  # never consume a half-written last line
                        add(line, st.st_nlink > 1 or linked, chat)
                        offset += len(line)
            except OSError:
                pass
        files[str(path)] = {"offset": offset, "ino": st.st_ino}

    for v in messages.values():  # shared after its messages were read: the other account counts them all, too
        if len(v) > 8 and not v[6] and v[8] in shared_chats:
            v[6] = 1

    stats_from = (date.fromtimestamp(now) - timedelta(days=STATS_DAYS - 1)).isoformat()
    history = {d: v for d, v in (cache.get("history") or {}).items() if d >= stats_from}
    history.update(day_history(messages))  # days still in messages: summed again
    keep_from = (date.fromtimestamp(now) - timedelta(days=TOKEN_DAYS - 1)).isoformat()
    messages = {k: v for k, v in messages.items() if v[0] >= keep_from}
    cache["files"], cache["messages"], cache["scannedAt"], cache["history"] = files, messages, now, history
    listed = {v[8] for v in messages.values() if len(v) > 8} | {c for h in history.values() for c in h.get("chats", [])}
    cache["chatFolders"] = {c: f for c, f in started_in.items() if c in listed}
    cache["days"], cache["daysAt"] = day_totals(messages), now
    cache["prompts"] = scan_prompts(profile, cache.get("prompts"), stats_from)
    return cache


def day_history(messages):
    """Messages summed per day for the stats: {day: {"models": {model:
    [input, output, cacheRead, cacheWrite, messages]}, "projects": {folder:
    [tokens, output]}, "chats": [chat ids]}}."""
    out = {}
    for day, model, *rest in messages.values():
        vals = rest[:4]
        project = rest[5] if len(rest) > 5 else ""
        chat = rest[6] if len(rest) > 6 else ""
        h = out.setdefault(day, {"models": {}, "projects": {}, "chats": []})
        row = h["models"].setdefault(model, [0, 0, 0, 0, 0])
        for i, v in enumerate(vals):
            row[i] += v
        row[4] += 1
        if project:
            pr = h["projects"].setdefault(project, [0, 0])
            pr[0] += sum(vals)
            pr[1] += vals[1]
        if chat and chat not in h["chats"]:
            h["chats"].append(chat)
    return out


def scan_prompts(profile, rec, stats_from):
    """Your prompts per day and folder, from the profile's history.jsonl
    (read where it grew): {"offset", "ino", "days": {day: {folder: count}}}."""
    path = profile.real / "history.jsonl"
    rec = rec if isinstance(rec, dict) and isinstance(rec.get("days"), dict) else {"offset": 0, "ino": None, "days": {}}
    try:
        st = path.stat()
    except OSError:
        return {"offset": 0, "ino": None, "days": {}}
    offset = rec.get("offset", 0) if rec.get("ino") == st.st_ino and rec.get("offset", 0) <= st.st_size else 0
    days = {d: v for d, v in rec["days"].items() if d >= stats_from} if offset else {}
    if offset < st.st_size:
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read()
        except OSError:
            chunk = b""
        end = chunk.rfind(b"\n")
        if end >= 0:
            for line in chunk[:end].splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                day = local_day(e.get("timestamp")) if isinstance(e, dict) else None
                if day is None or day < stats_from:
                    continue
                folder = e.get("project") if isinstance(e.get("project"), str) else ""
                by_folder = days.setdefault(day, {})
                by_folder[folder] = by_folder.get(folder, 0) + 1
            offset += end + 1
    return {"offset": offset, "ino": st.st_ino, "days": days}


def token_stats(caches, now, home, shown=STATS_SHOWN_DAYS):
    """The last days of an account, for its stats: {"days": [{"day", "total",
    "output", "messages", "chats", "prompts"}] oldest first, "projects":
    [{"name", "path", "total", "output", "prompts"}] busiest first, "models":
    [{"model", "total"}], "sum": {"total", "output", "chats", "prompts",
    "activeDays"}}, or None before any history is kept."""
    if not any(isinstance(c.get("history"), dict) for c in caches):
        return None
    today = date.fromtimestamp(now)
    names = [(today - timedelta(days=i)).isoformat() for i in range(shown - 1, -1, -1)]
    first = names[0]
    days = {d: {"day": d, "total": 0, "output": 0, "messages": 0, "chats": set(), "prompts": 0} for d in names}
    projects, models = {}, {}
    histories = [cache.get("history") or {} for cache in caches]
    if len(caches) > 1:  # the days the caches still have messages of: each message once
        recent = day_history(merged_messages(caches))
        histories = [{d: h for d, h in history.items() if d not in recent} for history in histories] + [recent]
    for history in histories:
        for day, h in history.items():
            if day < first or day not in days or not isinstance(h, dict):
                continue
            d = days[day]
            for model, row in (h.get("models") or {}).items():
                if isinstance(row, list) and len(row) == 5:
                    d["total"] += sum(row[:4])
                    d["output"] += row[1]
                    d["messages"] += row[4]
                    models[model] = models.get(model, 0) + sum(row[:4])
            for folder, sums in (h.get("projects") or {}).items():
                if isinstance(sums, list) and len(sums) == 2:
                    p = projects.setdefault(folder, {"total": 0, "output": 0, "prompts": 0})
                    p["total"] += sums[0]
                    p["output"] += sums[1]
            d["chats"].update(h.get("chats") or [])
    for cache in caches:  # each profile's own prompts (its history.jsonl)
        for day, by_folder in ((cache.get("prompts") or {}).get("days") or {}).items():
            if day in days and isinstance(by_folder, dict):
                for folder, n in by_folder.items():
                    days[day]["prompts"] += n
                    projects.setdefault(folder, {"total": 0, "output": 0, "prompts": 0})["prompts"] += n
    chats = set()
    for d in days.values():
        chats |= d["chats"]
        d["chats"] = len(d["chats"])
    rows = [days[n] for n in names]
    top = sorted(((f, p) for f, p in projects.items() if f and p["total"]), key=lambda fp: (-fp[1]["total"], -fp[1]["prompts"]))
    return {
        "days": rows,
        "projects": [{"name": "~" if short_path(f, home) == "~" else os.path.basename(f.rstrip("/")) or f,
                      "path": short_path(f, home), **p} for f, p in top[:6]],
        "models": [{"model": model_label(m) or m, "total": t}
                   for m, t in sorted(models.items(), key=lambda mt: -mt[1]) if t],
        "sum": {"total": sum(d["total"] for d in rows), "output": sum(d["output"] for d in rows),
                "chats": len(chats), "prompts": sum(d["prompts"] for d in rows),
                "activeDays": sum(1 for d in rows if d["messages"] or d["prompts"])},
    }


def day_totals(messages):
    """The messages summed per day and model: {day: {model: [input, output,
    cacheRead, cacheWrite, messages, shared tokens]}}. Kept with the cache, so
    a status check (every 2 s) adds up a few rows, not every message of the
    week (thousands for a busy week)."""
    days = {}
    for day, model, *rest in messages.values():
        vals, shared = rest[:4], bool(rest[4]) if len(rest) > 4 else False
        row = days.setdefault(day, {}).setdefault(model, [0, 0, 0, 0, 0, 0])
        for i, v in enumerate(vals):
            row[i] += v
        row[4] += 1
        if shared:
            row[5] += sum(vals)
    return days


def load_token_cache(cache_root, profile, now, lock_held):
    path = token_cache_path(cache_root, profile)
    cache = read_json(path)
    if not isinstance(cache, dict) or cache.get("version") != CACHE_VERSION:
        cache = empty_token_cache()
    since = now - (number(cache.get("scannedAt")) or 0)
    # a scan "from the future" (the clock was set back since) is due too
    if lock_held and (since >= TOKEN_SCAN_INTERVAL or since < -TOKEN_SCAN_INTERVAL):
        cache = scan_transcripts(profile, cache, now)
        try:
            write_json_atomic(path, cache)
        except OSError:
            pass
    return cache


def merged_messages(caches):
    """The messages of several profiles' caches, each once: profiles logged
    into one account can hold the same message (a chat shared between them,
    or a copy of one)."""
    merged = {}
    for cache in caches:
        for key, value in (cache.get("messages") or {}).items():
            merged.setdefault(key, value)
    return merged


def token_totals(caches, now):
    today = date.fromtimestamp(now).isoformat()
    week_start = (date.fromtimestamp(now) - timedelta(days=6)).isoformat()
    keys = ("input", "output", "cacheRead", "cacheWrite")

    def zero():
        return dict(dict.fromkeys(keys, 0), total=0, messages=0, shared=0)

    totals = {"today": zero(), "week": zero()}
    models = {}
    if len(caches) > 1:
        sums = [day_totals(merged_messages(caches))]
    else:
        sums = []
        for cache in caches:
            days = cache.get("days")
            if not isinstance(days, dict) or cache.get("daysAt") != cache.get("scannedAt"):
                # saved before the sums were kept, or rescanned by an older version
                days = day_totals(cache["messages"])
            sums.append(days)
    for days in sums:
        for day, by_model in days.items():
            if day < week_start or not isinstance(by_model, dict):
                continue
            buckets = [totals["week"]] + ([totals["today"]] if day == today else [])
            for model, row in by_model.items():
                if not isinstance(row, list) or len(row) != 6:
                    continue
                vals, count, shared = row[:4], row[4], row[5]
                for b in buckets:
                    for k, v in zip(keys, vals):
                        b[k] += v
                    b["total"] += sum(vals)
                    b["messages"] += count
                    b["shared"] += shared
                models[model] = models.get(model, 0) + sum(vals)
    totals["models"] = [{"model": m, "total": t} for m, t in sorted(models.items(), key=lambda x: -x[1])]
    return totals


class ScanLock:
    """Non-blocking lock so overlapping polls don't scan transcripts twice."""

    def __init__(self, path):
        self.path, self.fd, self.held = path, None, False

    def __enter__(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.held = True
        except OSError:
            self.held = False
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)


# ---------------------------------------------------------------- dismissed sessions


def cache_dir(home):
    return Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / "talpy-den"


def adopt_legacy_dirs(home):
    """The folders an earlier version kept under its first name, claude-widget
    (in the cache and in ~/.local/share), move to talpy-den. A link stays under
    the old name: Claude's settings (the hooks) and the status-line snippet
    still name it until the next ./install.sh brings them up to date."""
    for base in (Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache"),
                 Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")):
        old, new = base / "claude-widget", base / "talpy-den"
        try:
            if old.is_symlink() or not old.is_dir():
                continue
            new.mkdir(parents=True, exist_ok=True)
            os.chmod(new, 0o700)
            for child in list(old.iterdir()):
                if not os.path.lexists(new / child.name):
                    os.rename(child, new / child.name)
            old.rmdir()  # (not while something is left in it: then it stays as it was)
            os.symlink(new, old)
        except OSError:
            continue


# ---------------------------------------------------------------- starred chats and plans
#
# Chats and plans you starred stay at hand: each account lists its starred
# ones however old they are (group["starred"], group["starredPlans"]). The
# stars are this computer's (stars.json in the widget's cache, an SSH host's
# under its name): the status run on a host gets that host's from here
# (remote.status: --starred-chats, --starred-plans).

STAR_KINDS = ("chats", "plans")


def load_stars(cache_root):
    """{"chats": [{"id", "host"}], "plans": [{"id", "host"}]}: what you
    starred, in the order you did ("host": "" for this computer's)."""
    data = read_json(cache_root / "stars.json")
    data = data if isinstance(data, dict) else {}
    valid = {"chats": valid_id, "plans": lambda v: isinstance(v, str) and bool(PLAN_SLUG.match(v))}
    out = {}
    for kind in STAR_KINDS:
        items = data.get(kind) if isinstance(data.get(kind), list) else []
        out[kind] = [{"id": i["id"], "host": i.get("host") or ""} for i in items
                     if isinstance(i, dict) and valid[kind](i.get("id")) and isinstance(i.get("host") or "", str)]
    return out


def change_stars(cache_root, kind, item_id, host="", on=True):
    """Star a chat or a plan (kind "chats" or "plans"), or (not on) not any
    more, under a lock: every copy of the widget writes stars.json."""
    private_dir(cache_root)
    with open(cache_root / "stars.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stars = load_stars(cache_root)
        items = [i for i in stars[kind] if (i["id"], i["host"]) != (item_id, host or "")]
        if on:
            items.append({"id": item_id, "host": host or ""})
        if items != stars[kind]:
            stars[kind] = items
            write_json_atomic(cache_root / "stars.json", stars)


def starred_of(stars, host=""):
    """{"chats": ids, "plans": slugs} starred of this computer (or of host)."""
    return {kind: {i["id"] for i in stars[kind] if i["host"] == (host or "")} for kind in STAR_KINDS}


def load_dismissed(cache_root):
    """Finished sessions the user removed from the widget's list: {session id: when}."""
    data = read_json(cache_root / "dismissed.json")
    sessions = data.get("sessions") if isinstance(data, dict) else None
    return dict(sessions) if isinstance(sessions, dict) else {}


def save_dismissed(cache_root, sessions):
    write_json_atomic(cache_root / "dismissed.json", {"sessions": sessions})


DISMISSED_KEEP_MS = 30 * 86400_000  # a removed session not seen again for this long is forgotten


def change_dismissed(cache_root, add=None, forget=(), clear=False, now_ms=None):
    """Change dismissed.json (add a session id, forget some, or clear it)
    under a lock: every copy of this script and jump.py write it. The
    sessions removed now."""
    private_dir(cache_root)
    with open(cache_root / "dismissed.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        sessions = load_dismissed(cache_root)
        before = dict(sessions)
        if clear:
            sessions = {}
        for i in forget:
            sessions.pop(i, None)
        if add is not None:
            sessions[add] = int(time.time() * 1000) if now_ms is None else now_ms
        if sessions != before or not (cache_root / "dismissed.json").exists():
            save_dismissed(cache_root, sessions)
        return sessions


# ---------------------------------------------------------------- main


class Context:
    """What one poll knows, handed to the row builders."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def collect(home, cache_root, extra_dirs=(), with_tokens=True, now=None, keep_done_hours=KEEP_DONE_HOURS,
            recent=RECENT_CHATS, plans=PLANS_LISTED, usage_cap=0, starred=None):
    """starred: {"chats", "plans"} starred here (starred_of), else this
    computer's from the cache."""
    now = time.time() if now is None else now
    now_ms = int(now * 1000)
    snapshot_root = cache_root / "statusline"
    profiles = discover_profiles(home, extra_dirs)
    counts = dict.fromkeys(STATE_ORDER, 0)
    errors, groups = [], []
    try:
        private_dir(cache_root)
    except OSError as exc:
        errors.append(f"cache: {exc}")

    histories = {p.real: history_tail(p) for p in profiles}
    ctx = Context(
        home=home, profiles=profiles, snapshot_root=snapshot_root, now_ms=now_ms,
        keep_done_ms=int(keep_done_hours * 3600_000),
        agents=AgentCache(cache_root / "agents.json"), links=LinkCache(cache_root / "links.json"),
        chats=ChatCache(cache_root / "chats.json"), slugs=SlugCache(cache_root / "plans.json"), changed={},
        tests=TestsCache(cache_root / "tests.json"),
        own_folders=own_folders(profiles),
        prompts={real: last_prompts(entries) for real, entries in histories.items()},
        errors=errors, unknown={},
    )
    with ScanLock(cache_root / "tokens.lock") as lock:
        # read with the lock: the check holding it is the one that writes it
        usage = UsageHistory(cache_root / "usage.json")
        if lock.held:
            prune_snapshots(snapshot_root, now)
            if with_tokens:
                prune_token_caches(cache_root, profiles, now)

        # Every live session process, under the profile it runs on now
        rows = {p.real: [] for p in profiles}
        live_jobs = set()
        try:
            live = live_entries_all(profiles)
        except Exception as exc:  # internal format: fail soft
            errors.append(f"sessions: {exc}")
            live = []
        for entry, profile, holder in live:
            note_unknown(ctx, entry)
            if valid_id(entry.get("jobId")):
                live_jobs.add(entry["jobId"])
            try:
                row = live_row(entry, profile, holder, home, ctx)
            except Exception as exc:
                errors.append(f"{profile.label}: sessions: {exc}")
                continue
            if row is not None:
                rows[profile.real].append(row)
        # Background jobs with no process; a job held by two profiles (written
        # through a switched link) is listed once, from its newest record
        newest_job = {}
        newest = newest_job_records(profiles)
        for p in profiles:
            try:
                for row in parked_rows(p, home, live_jobs, ctx, newest):
                    prev = newest_job.get(row["jobId"])
                    if prev is None or (row["since"] or 0) > (prev[1]["since"] or 0):
                        newest_job[row["jobId"]] = (p, row)
            except Exception as exc:
                errors.append(f"{p.label}: background jobs: {exc}")
        for p, row in newest_job.values():
            rows[p.real].append(row)

        logged_out = logged_out_notes(cache_root)
        for gid, members in group_profiles(profiles):
            named = next((p for p in members if not p.is_default), members[0])
            plan = next((p.plan for p in members if p.plan), None)
            group = {
                # not logged in to an account: "Log in…" (accounts.py --login chatProfile)
                "loggedOut": all(needs_login(p, logged_out) for p in members),
                "id": short_id(gid),
                "name": named.label,
                "plan": plan,
                "title": f"{named.label} · {plan}" if plan else named.label,
                "profiles": [p.label for p in members],
                "profileKeys": [p.key for p in members],
                "sessions": [row for p in members for row in rows[p.real]],
                "limits": None,
                "tokens": None,
                "stats": None,  # the last days: per-day tokens, chats and prompts, busiest folders (token_stats)
                # for "new chat": the profile folder name jump.py --new-chat takes
                "chatProfile": named.key,
                # for sharing a chat with this account: the profile it goes to (jump.py --profile)
                "shareProfile": named.key,
                "projects": recent_projects([histories[p.real] for p in members], home),
            }
            # (a task can start in the background only where Claude trusts the folder)
            trusted = trusted_folders(named, home)
            for project in group["projects"]:
                project["trusted"] = folder_trusted(named, project["path"], home, trusted)
            try:
                # a logged-out account's last status lines are no one's plan usage now
                group["limits"] = None if group["loggedOut"] else plan_limits(snapshot_root, members, now_ms)
                if group["limits"]:
                    if lock.held:
                        usage.record(group["id"], group["limits"], now_ms)
                    for w in group["limits"]["windows"]:
                        w["forecast"] = usage_forecast(w, usage.points(group["id"], w), now_ms)
            except Exception as exc:
                errors.append(f"{group['title']}: limits: {exc}")
            if with_tokens:
                try:
                    caches = [load_token_cache(cache_root, p, now, lock.held) for p in members]
                    group["tokens"] = token_totals(caches, now)
                    group["stats"] = token_stats(caches, now, home)
                except Exception as exc:
                    errors.append(f"{group['title']}: tokens: {exc}")
            groups.append(group)
        usage.save()  # only this check noted anything (lock.held): others mustn't write it now

    try:
        place_chats(groups, ctx)
    except Exception as exc:
        errors.append(f"shared chats: {exc}")
    # Removed from the list by the user: hidden while finished; one that runs
    # again comes back (and is forgotten).
    dismissed = load_dismissed(cache_root)
    forget, hidden = set(), 0
    for g in groups:
        shown = []
        for s in g["sessions"]:
            # a record moved to the account the chat was used on last keeps
            # the ids it had where it was removed
            former = s.pop("formerIds", None) or []
            ids = [i for i in (s["sessionId"], s.get("chatId"), *former) if i in dismissed]
            if ids and s["state"] == "done":
                hidden += 1
            else:
                forget.update(ids)  # it runs again: it comes back (and is forgotten)
                shown.append(s)
        # The chats that need you first (the one asking longest first), the
        # others in the order they started, the newest last: a row (and its
        # pet) moves only when its chat starts or stops needing you
        g["sessions"] = sorted(shown, key=lambda s: (0, s["since"] or 0, s["key"]) if s["state"] == "waiting"
                               else (1, s.get("startedAt") or s["since"] or 0, s["key"]))
    # Only another copy of this script (with other settings) may list a
    # removed session now: forget it only after a long while
    forget.update(i for i, t in dismissed.items() if not isinstance(t, (int, float)) or now_ms - t > DISMISSED_KEEP_MS)
    link_shared_elsewhere(groups)
    for g in groups:  # (after place_chats: a row moved to another account goes by that one's limits)
        note_limit_stops(g, now_ms)
    try:
        note_overlaps(groups, ctx.changed, home)
    except Exception as exc:
        errors.append(f"chats changing the same files: {exc}")
    try:
        note_permissions(groups, ctx, permission_requests(cache_root, now_ms))
    except Exception as exc:
        errors.append(f"permission prompts: {exc}")
    try:
        note_live(groups, cache_root)
    except Exception as exc:
        errors.append(f"live mode: {exc}")
    try:
        if starred is None:
            starred = starred_of(load_stars(cache_root))
    except Exception as exc:
        errors.append(f"starred chats and plans: {exc}")
        starred = {kind: set() for kind in STAR_KINDS}
    try:
        # a finished session you removed from the list is still one of your
        # recent chats: it shows there
        recent_chats(groups, ctx, recent, starred["chats"])
    except Exception as exc:
        errors.append(f"recent chats: {exc}")
        for g in groups:
            g.setdefault("recent", [])
            g.setdefault("starred", [])
    for g in groups:
        for s in g["sessions"]:
            s["starred"] = (s.get("chatId") or s["sessionId"]) in starred["chats"]
        note_limit_hits(g)  # (of its listed chats and its recent ones)
        try:
            listed = account_plans(g, ctx, plans, starred["plans"])
            g["plans"] = listed[:max(0, plans)]
            g["starredPlans"] = [dict(pl, key=pl["key"][:-len("/plan")] + "/starred") for pl in listed if pl["starred"]]
        except Exception as exc:
            errors.append(f"{g['title']}: plans: {exc}")
            g["plans"], g["starredPlans"] = [], []
        try:
            g["memories"] = account_memories(g, ctx)
        except Exception as exc:
            errors.append(f"{g['title']}: memories: {exc}")
            g["memories"] = []
    for g in groups:
        for s in g["sessions"]:
            counts[s["state"]] += 1
    ctx.agents.save()
    ctx.links.save()
    ctx.chats.save()
    ctx.slugs.save()
    ctx.tests.save()
    if forget:
        try:
            change_dismissed(cache_root, forget=forget)
        except OSError:
            pass
    new_values = unknown_values_text(ctx.unknown)
    if new_values:
        errors.append(new_values)
    if counts["waiting"]:
        overall = "waiting"
    elif counts["working"]:
        overall = "working"
    elif any(s["state"] in ("idle", "done") and s["pid"] is not None for g in groups for s in g["sessions"]):
        overall = "idle"
    else:
        overall = "none"  # finished jobs kept for the record don't count as running
    try:
        queue = queued_tasks(cache_root, now_ms)
    except (OSError, ValueError) as exc:
        errors.append(f"queued tasks: {exc}")
        queue = []
    note_caps(groups, usage_cap, now_ms)  # (an account at the usage cap: g["cap"])
    return {
        "ok": True,
        "generatedAt": now_ms,
        "overall": overall,
        "queue": queue,  # tasks waiting for a usage limit to reset (queued_tasks)
        "keep": [],  # the chats that keep going (kept_chats, after look_after_kept)
        "counts": counts,
        "groups": groups,
        "dismissed": hidden,
        "canShare": True,  # jump.py shares chats between this computer's profiles (sharing.py)
        "reviewed": load_reviewed(cache_root),  # {review_key: when}: chats marked reviewed (pane.py --reviewed)
        "reviewedSince": reviewed_since(cache_root, now_ms),  # (what chats changed before isn't to review)
        "liveHook": live_hook_on(home),  # rows' "live" follow each step (the widget then checks less often)
        "errors": errors,
    }


# ---------------------------------------------------------------- VS Code
#
# "New chat in VS Code" starts a chat in VS Code's Claude extension. The
# extension runs Claude with VS Code's environment plus its own
# claudeCode.environmentVariables setting, so those decide the account (the
# Claude config folder), not the widget: it only offers the button on the
# account VS Code will use.

VSCODE_APPS = (("code", "Code"), ("code-insiders", "Code - Insiders"), ("codium", "VSCodium"),
               ("code-oss", "Code - OSS"))


def vscode_app():
    """(command, settings folder name) of the installed VS Code, or (None, None)."""
    import shutil
    for command, folder in VSCODE_APPS:
        path = shutil.which(command)
        if path:
            return path, folder
    return None, None


def jsonc(text):
    """Parse a VS Code settings file: JSON with comments and trailing commas."""
    out, i, n = [], 1 if text.startswith("\ufeff") else 0, len(text)  # (after a byte order mark)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c == ",":
            j = jsonc_skip(text, i + 1)
            if j >= n or text[j] not in "}]":  # (a trailing comma goes; one in a string stays)
                out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return json.loads("".join(out))


def jsonc_skip(text, i):
    """The index of text's next character from i that isn't space or a comment."""
    n = len(text)
    while i < n:
        if text[i] in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            break
    return i


def jsonc_string_end(text, i):
    """The end of the string that starts at text[i] (a quote)."""
    j = i + 1
    while j < len(text) and text[j] != '"':
        j += 2 if text[j] == "\\" else 1
    return j + 1


def jsonc_value_end(text, i):
    """The end of the value that starts at text[i]."""
    n, depth = len(text), 0
    while i < n:
        c = text[i]
        if c == '"':
            i = jsonc_string_end(text, i)
            if depth == 0:
                return i
            continue
        if text.startswith("//", i) or text.startswith("/*", i):
            if depth == 0:
                return i
            i = jsonc_skip(text, i)
            continue
        if c in "[{":
            depth += 1
        elif c in "]}":
            if depth == 0:
                return i
            depth -= 1
            if depth == 0:
                return i + 1
        elif depth == 0 and (c == "," or c in " \t\r\n"):
            return i
        i += 1
    return n


def jsonc_set(text, key, value):
    """text (a VS Code settings file: JSON with comments) with its top-level
    key set to value, and everything else (comments, order, layout) as it
    was: the edit VS Code makes when you change one setting."""
    n = len(text)
    eol = "\r\n" if "\r\n" in text else "\n"  # (new lines end as the file's do)
    i = jsonc_skip(text, 1 if text.startswith("\ufeff") else 0)  # (a byte order mark stays)
    if i >= n or text[i] != "{":
        raise ValueError("not a JSON object")
    i = jsonc_skip(text, i + 1)
    first, last_end = i, None
    while i < n and text[i] != "}":
        if text[i] != '"':
            raise ValueError(f"unexpected {text[i]!r} at {i}")
        key_end = jsonc_string_end(text, i)
        name = json.loads(text[i:key_end])
        colon = jsonc_skip(text, key_end)
        if colon >= n or text[colon] != ":":
            raise ValueError(f"no : after {name!r}")
        start = jsonc_skip(text, colon + 1)
        end = jsonc_value_end(text, start)
        if name == key:
            line_start = text.rfind("\n", 0, i) + 1
            indent = text[line_start:i] if not text[line_start:i].strip() else ""
            return text[:start] + json.dumps(value, indent=indent or 4).replace("\n", eol + indent) + text[end:]
        last_end = end
        i = jsonc_skip(text, end)
        if i < n and text[i] == ",":
            i = jsonc_skip(text, i + 1)
    if i >= n:
        raise ValueError("no closing }")
    # Not there yet: after the last setting, indented like the first one
    line_start = text.rfind("\n", 0, first) + 1
    indent = text[line_start:first] if last_end is not None and not text[line_start:first].strip() else "    "
    member = f'"{key}": ' + json.dumps(value, indent=indent).replace("\n", eol + indent)
    if last_end is None:
        return text[:i].rstrip() + eol + indent + member + eol + text[i:]
    after = jsonc_skip(text, last_end)
    if after < n and text[after] == ",":  # a trailing comma already
        return text[:after + 1] + eol + indent + member + text[after + 1:]
    return text[:last_end] + "," + eol + indent + member + text[last_end:]


def config_dir_in_settings(path):
    """CLAUDE_CONFIG_DIR from the Claude extension's environmentVariables
    setting in a VS Code settings file, or None."""
    try:
        settings = jsonc(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    entries = settings.get("claudeCode.environmentVariables") if isinstance(settings, dict) else None
    for e in entries if isinstance(entries, list) else []:
        if isinstance(e, dict) and e.get("name") == "CLAUDE_CONFIG_DIR" and isinstance(e.get("value"), str) and e["value"]:
            return e["value"]
    return None


def vscode_setting_config_dir(home, settings_folder):
    """CLAUDE_CONFIG_DIR from VS Code's user settings, or None."""
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return config_dir_in_settings(config / settings_folder / "User" / "settings.json")


# On an SSH host, VS Code's server keeps the host's own settings ("Remote
# [SSH: host]"): the Claude extension's environmentVariables is a machine
# setting, so windows connected to the host read it there, never from the
# settings on your computer. Per VS Code app: its server's folder.
VSCODE_SERVERS = {"Code": ".vscode-server", "Code - Insiders": ".vscode-server-insiders", "VSCodium": ".vscodium-server"}


def vscode_server_settings(home, server):
    return home / server / "data" / "Machine" / "settings.json"


def vscode_server_config_dir(home):
    """CLAUDE_CONFIG_DIR VS Code's Claude runs with on this host (the first
    VS Code server whose settings set it), or None."""
    for server in VSCODE_SERVERS.values():
        value = config_dir_in_settings(vscode_server_settings(home, server))
        if value is not None:
            return value
    return None


def set_config_dir_setting(path, value, home):
    """claudeCode.environmentVariables in the VS Code settings file at path
    gets CLAUDE_CONFIG_DIR=value (None: none): (ok, why not). Only that entry
    changes; the file's comments, other settings and layout stay (jsonc_set),
    and its permissions too."""
    shown = short_path(str(path), home)
    try:
        with open(str(path), encoding="utf-8", newline="") as f:  # (its line ends as they are)
            text = f.read()
    except FileNotFoundError:
        text = ""
    except (OSError, ValueError) as exc:
        return False, f"Can't read {shown}: {exc}"
    try:
        settings = jsonc(text) if text.strip() else {}
        if not isinstance(settings, dict):
            raise ValueError("not a JSON object")
        old = settings.get("claudeCode.environmentVariables")
        entries = [e for e in (old if isinstance(old, list) else [])
                   if not (isinstance(e, dict) and e.get("name") == "CLAUDE_CONFIG_DIR")]
        if value:
            entries.append({"name": "CLAUDE_CONFIG_DIR", "value": value})
        new = jsonc_set(text if text.strip() else "{\n}\n", "claudeCode.environmentVariables", entries)
        # it says what it said before, but for this setting
        if jsonc(new) != {**settings, "claudeCode.environmentVariables": entries}:
            raise ValueError("the change came out wrong")
    except ValueError as exc:
        return False, f"{shown} couldn't be changed ({exc}): switch the account in VS Code"
    # a link (to your dotfiles, say) stays one: the file it points to changes
    target = Path(os.path.realpath(str(path)))
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
        with open(str(tmp), "w", encoding="utf-8", newline="") as f:
            f.write(new)
        os.chmod(str(tmp), mode)
        os.replace(str(tmp), str(target))
    except OSError as exc:
        try:
            tmp.unlink()  # (not left next to the settings)
        except OSError:
            pass
        return False, f"Can't write {shown}: {exc}"
    return True, ""


def set_vscode_server_config_dir(home, value, server=".vscode-server", extra_dirs=()):
    """Have VS Code's Claude on this host run with CLAUDE_CONFIG_DIR=value
    (None: without it, on a plain ~/.claude), in the settings of its VS Code
    server: (ok, message). Only one of this host's Claude folders. The Claude
    extension follows the change (new chats, its panels)."""
    if server not in VSCODE_SERVERS.values():
        return False, f"Not a VS Code server folder: {server}"
    profiles = discover_profiles(home, extra_dirs)
    try:
        real = (Path(value).expanduser() if value else home / ".claude").resolve()
    except (OSError, RuntimeError):
        real = None
    profile = next((p for p in profiles if p.real == real), None)
    if profile is None:
        return False, f"{value or '~/.claude'} isn't a Claude folder on this host"
    ok, why = set_config_dir_setting(vscode_server_settings(home, server), value, home)
    return (True, f"VS Code's Claude runs on {profile.label} here now") if ok else (False, why)


def set_vscode_user_config_dir(home, value):
    """Have VS Code's Claude on this computer run with CLAUDE_CONFIG_DIR=value
    (None: without it) from now on, in VS Code's user settings: (ok,
    message). The Claude extension starts new chats with it; a window's
    Claude panel follows once the window reloads."""
    command, folder = vscode_app()
    if command is None:
        return False, "VS Code isn't installed"
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return set_config_dir_setting(config / folder / "User" / "settings.json", value, home)


def vscode_running_config_dir():
    """(VS Code runs, CLAUDE_CONFIG_DIR in its environment or None). Only the
    main process counts: helpers run as node (ELECTRON_RUN_AS_NODE) inherit
    the extensions' environment."""
    names = {Path(c).name for c, _ in VSCODE_APPS}
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return False, None
    for pid in pids:
        try:
            if Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip() not in names:
                continue
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            env = dict(item.partition(b"=")[::2] for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0") if item)
        except OSError:
            continue
        if any(a.startswith(b"--type=") for a in argv) or b"ELECTRON_RUN_AS_NODE" in env:
            continue
        value = env.get(b"CLAUDE_CONFIG_DIR")
        return True, value.decode(errors="replace") if value else None
    return False, None


VSCODE_CHECK_INTERVAL = 30  # seconds between looks at VS Code (see cached_vscode_lookup)


def vscode_lookup(home):
    """(VS Code installed, the CLAUDE_CONFIG_DIR its Claude would run with or None)."""
    command, settings_folder = vscode_app()
    if command is None:
        return False, None
    value = vscode_setting_config_dir(home, settings_folder)
    if value is None:
        running, env_value = vscode_running_config_dir()
        # not running: the widget starts it, with its own environment
        value = env_value if running else os.environ.get("CLAUDE_CONFIG_DIR")
    return True, value


def vscode_stamp(home):
    """When VS Code's settings files and the folders on PATH last changed: a
    new setting, or VS Code just installed, counts at once."""
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    settings = [config / folder / "User" / "settings.json" for _, folder in VSCODE_APPS]
    folders = [Path(d) for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    return [mtime(p) for p in settings + folders]


def cached_vscode_lookup(home, cache_root, now):
    """vscode_lookup, redone at most every VSCODE_CHECK_INTERVAL seconds or when
    VS Code's settings or the folders on PATH change: it searches PATH and
    maybe every process, too much to repeat in a status check every 2 s."""
    path = cache_root / "vscode.json"
    stamp = vscode_stamp(home)
    saved = read_json(path)
    if (isinstance(saved, dict) and saved.get("stamp") == stamp
            and 0 <= now - (number(saved.get("at")) or 0) < VSCODE_CHECK_INTERVAL):
        value = saved.get("value")
        return saved.get("installed") is True, value if isinstance(value, str) else None
    installed, value = vscode_lookup(home)
    try:
        private_dir(cache_root)
        write_json_atomic(path, {"at": now, "stamp": stamp, "installed": installed, "value": value})
    except OSError:
        pass
    return installed, value


def vscode_profile(home, profiles, remote=False, cache_root=None):
    """(VS Code installed, the profile a new chat in VS Code's Claude would
    use or None). On an SSH host (remote) the VS Code server's Claude uses what its
    server's settings say, else the host's ~/.claude; whether VS Code is
    installed is up to the other side."""
    installed, value = None, None
    if remote:
        value = vscode_server_config_dir(home)
    else:
        installed, value = (cached_vscode_lookup(home, cache_root, time.time()) if cache_root is not None
                            else vscode_lookup(home))
        if not installed:
            return False, None
    try:
        real = (Path(value).expanduser() if value else home / ".claude").resolve()
    except (OSError, RuntimeError):
        return installed, None
    return installed, next((p for p in profiles if p.real == real), None)


def plain_profile(home, profiles):
    """The profile plain `claude` runs on: ~/.claude, or the one it links to."""
    try:
        real = (home / ".claude").resolve()
    except (OSError, RuntimeError):
        return None
    return next((p for p in profiles if p.real == real), None)


def default_profile(home, profiles, remote=False, cache_root=None):
    """(VS Code installed, the profile VS Code's Claude runs on, plain
    claude's, the default account). The default is VS Code's Claude's where
    VS Code is (accounts.py --default points ~/.claude there too), else
    plain claude's: the same on every front end."""
    installed, vscode = vscode_profile(home, profiles, remote, cache_root)
    plain = plain_profile(home, profiles)
    return installed, vscode, plain, vscode or plain


def add_vscode(result, home, extra_dirs=(), remote=False, cache_root=None):
    """Which account a new chat in VS Code would be on (result["vscode"] =
    {installed, on (its label)}, each group's vscodeHere), plain claude's
    (plainHere) and the default account (defaultHere, default_profile),
    which is listed first."""
    installed, profile, plain, default = default_profile(home, discover_profiles(home, extra_dirs), remote, cache_root)
    result["vscode"] = {"installed": installed, "on": profile.label if profile else None}
    # (whether accounts.py --default can point anything elsewhere: VS Code's
    # Claude, or plain claude through a ~/.claude link)
    result["canDefault"] = bool(installed) or (home / ".claude").is_symlink()
    for g in result.get("groups", []):
        keys = g.get("profileKeys", [])
        g["vscodeHere"] = profile is not None and profile.key in keys
        g["plainHere"] = plain is not None and plain.key in keys
        g["defaultHere"] = default is not None and default.key in keys
    result.get("groups", []).sort(key=lambda g: not g["defaultHere"])  # (stable: the others keep their order)


# ---------------------------------------------------------------- remote hosts
#
# The widget runs this script on an SSH host (`ssh host python3 -I - --remote`
# with the script on stdin: nothing is installed there). In that mode every row
# also carries the shell command that opens it on the host, so the widget can
# run it in a local terminal as `ssh -t host <command>`.

REMOTE_MARK = "@@talpy-den@@"
# Where claude is often installed besides PATH: Claude's installer, its old
# local install, npm, bun, ~/bin, Volta, pnpm, mise and asdf; then nvm's
# Node versions (newest first) and the system-wide places
CLAUDE_PLACES = (".local/bin", ".claude/local", ".npm-global/bin", ".bun/bin", "bin", ".volta/bin",
                 ".local/share/pnpm", ".local/share/mise/shims", ".asdf/shims")
SYSTEM_PLACES = ("/usr/local/bin", "/home/linuxbrew/.linuxbrew/bin", "/opt/homebrew/bin")  # check-public: ok (Homebrew's prefix)


def version_key(name):
    """v20.11.1 -> (20, 11, 1)"""
    return tuple(int(n) for n in re.findall(r"\d+", name))


def claude_places(home):
    nvm = sorted((home / ".nvm/versions/node").glob("*/bin"), key=lambda p: version_key(p.parent.name), reverse=True)
    return [str(home / d) for d in CLAUDE_PLACES] + [str(p) for p in nvm] + list(SYSTEM_PLACES)


def login_shell_path(home):
    """PATH as your login shell sets it up (from ~/.profile, ~/.bashrc,
    ~/.zshrc and the like, where installers add their folders), or "".
    `env` prints it the same way whatever the shell."""
    import subprocess
    shell = os.environ.get("SHELL") or "/bin/sh"
    try:
        r = subprocess.run([shell, "-l", "-i", "-c", "env"], cwd=str(home), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    lines = r.stdout.decode(errors="replace").splitlines()
    return next((line[len("PATH="):] for line in reversed(lines) if line.startswith("PATH=")), "")


def find_claude(home, ask_shell=False):
    """Absolute path of the claude command, or "claude" if it isn't found.

    The desktop session and commands run over ssh don't read the shell
    startup files that usually put claude's folder on PATH. So after PATH
    come the usual install places and, with ask_shell (it starts your login
    shell: only for a click, never for a status check), its PATH."""
    import shutil
    found = shutil.which("claude", path=os.pathsep.join([os.environ.get("PATH", ""), *claude_places(home)]))
    if not found and ask_shell:
        shell_path = login_shell_path(home)
        found = shutil.which("claude", path=shell_path) if shell_path else None
    return found or "claude"


CLAUDE_SHELL_CHECK_INTERVAL = 3600  # seconds between asking the login shell (see cached_find_claude)


def cached_find_claude(home, cache_root, now):
    """find_claude for a status check: the claude command's path, or None.
    When it's on neither PATH nor the usual places, your login shell's PATH
    is asked at most once an hour (it starts your shell); a find is checked
    again on every call (it may have been removed)."""
    found = find_claude(home)
    if found != "claude":
        return found
    path = cache_root / "claude.json"
    saved = read_json(path)
    if isinstance(saved, dict) and 0 <= now - (number(saved.get("at")) or 0) < CLAUDE_SHELL_CHECK_INTERVAL:
        found = saved.get("path")
        return found if isinstance(found, str) and os.access(found, os.X_OK) else None
    found = find_claude(home, ask_shell=True)
    found = None if found == "claude" else found
    try:
        private_dir(cache_root)
        write_json_atomic(path, {"at": now, "path": found})
    except OSError:
        pass
    return found


def in_folder(folder, command):
    """command, run in folder when it exists (ssh starts in the home folder)."""
    if isinstance(folder, str) and folder.startswith("/"):
        return f"cd {shlex.quote(folder)} 2>/dev/null; {command}"
    return command


def add_remote_commands(result, home, extra_dirs=(), claude=None):
    """Shell commands that open, stop or start chats on this host (claude:
    the claude command's path, if it's been looked for already)."""
    claude = claude or find_claude(home)
    profiles = {p.key: p for p in discover_profiles(home, extra_dirs)}
    result["claude"] = claude
    for g in result.get("groups", []):
        named = profiles.get(g.get("chatProfile"))
        # what VS Code's Claude there needs to run on this account (jump.py
        # --remote-new-vscode --account, the VS Code extension's switch)
        g["configDir"] = config_dir_value(named, home) if named is not None else None
        g["loginCommand"] = (in_folder(str(home), login_command(named, home, claude))
                             if named is not None and g.get("loggedOut") else None)
        if named is not None:
            chat = new_chat_command(named, home, claude)
            g["homeChatCommand"] = in_folder(str(home), chat)
            for project in g.get("projects", []):
                project["newChatCommand"] = in_folder(project["path"], chat)
        for s in g["sessions"]:
            p = profiles.get(s["profileDir"])
            s["openCommand"] = s["stopCommand"] = None
            # a chat in the terminal of a VS Code window connected to this
            # host: that window shows it (jump.py --remote-terminal --vscode-terminal)
            s["vscodeTerminal"] = (vscode_terminal(s["pid"]) if s["pid"] is not None
                                   and s["entrypoint"] != "claude-vscode" else None)
            if p is None:
                continue
            if s.get("jobId"):
                job = read_job([p], s["jobId"]) or {}
                s["openCommand"] = in_folder(job.get("cwd") or s["cwd"], attach_command(p, s["jobId"], home, job, claude))
                if s["pid"] is not None:
                    s["stopCommand"] = stop_command(p, s["jobId"], home, job, claude)
            elif s["kind"] == "chat":
                s["openCommand"] = in_folder(s["cwd"], resume_command(p, s["chatId"], home, s["cwd"], claude))
        for c in g.get("recent", []):
            p = profiles.get(c["profileDir"])
            c["openCommand"] = in_folder(c["cwd"], resume_command(p, c["sessionId"], home, c["cwd"], claude)) if p else None
        for pl in g.get("plans", []):  # its chat, reopened
            p = profiles.get(pl.get("chatProfileDir"))
            pl["openCommand"] = (in_folder(pl["cwd"], resume_command(p, pl["chatId"], home, pl["cwd"], claude))
                                 if p is not None and pl.get("chatId") else None)
    rows = {s["key"]: s for g in result.get("groups", []) for s in g["sessions"]}
    for g in result.get("groups", []):
        for h in g.get("sharedHere", []):  # a link opens the chat's own row
            target = rows.get(h["key"][:-len("/shared")]) if h["key"].endswith("/shared") else None
            h["openCommand"] = target["openCommand"] if target else None


def add_found_commands(result, home, extra_dirs=()):
    """The commands reopening the chats --find found on this host."""
    claude = find_claude(home)
    profiles = {p.key: p for p in discover_profiles(home, extra_dirs)}
    for c in result.get("results", []):
        p = profiles.get(c["profileDir"])
        c["openCommand"] = in_folder(c["cwd"], resume_command(p, c["sessionId"], home, c["cwd"], claude)) if p else None


USAGE = """usage: talpy_den.py [--config-dir DIR]... [--done-hours H] [--recent N] [--plans N] [--no-tokens] [--pretty] [--remote [--local]]
       talpy_den.py --dismiss SESSION_ID | --undismiss-all
       talpy_den.py --end SESSION_ID [--profile PROFILE] [--busy] [--config-dir DIR]...
       talpy_den.py --interrupt SESSION_ID [--profile PROFILE] [--config-dir DIR]...
       talpy_den.py --agent-run AGENT_ID --chat CHAT_ID [--profile PROFILE] [--config-dir DIR]...
       talpy_den.py --vscode-account CONFIG_DIR|- [--vscode-server FOLDER]
       talpy_den.py --find QUERY [--limit N] [--remote]
       talpy_den.py --start-task|--task-command FOLDER --profile PROFILE [--prompt TEXT]
                        [--plan SLUG [--plan-profile PROFILE]] [--model MODEL] [--effort LEVEL]
                        [--permission-mode MODE] [--remote]
       talpy_den.py --plan-text SLUG [--profile PROFILE] [--remote]
       talpy_den.py --continue-chat CHAT_ID --profile PROFILE [--folder F] [--prompt TEXT] [--remote]
  --config-dir DIR  extra Claude config dir (repeatable)
  --done-hours H    keep finished background jobs listed for H hours (default 24, 0 = never)
  --recent N        list each account's N latest other chats (default 5, 0 = none)
  --plans N         list each account's N latest plans (default 8, 0 = none)
  --find QUERY      the chats with every word of QUERY in their name, folder or your prompts
  --start-task F    start a background session in folder F with the prompt (and plan)
  --task-command F  the shell command that starts a chat in folder F with the prompt (and plan)
  --plan-text SLUG  a plan's Markdown
  --continue-chat ID  go on with a chat a usage limit stopped, in the background (the limit has reset)
  --no-tokens       skip transcript token totals
  --pretty          indent the JSON output
  --remote          for the widget on another machine: add the shell commands that open
                    each chat here, and print the JSON after a marker line
  --local           with --remote: this computer's status for its VS Code extension (VS Code's
                    account is this computer's VS Code's, not a VS Code server's)
  --dismiss ID      hide a finished session in the widget (--undismiss-all shows them again)
  --end ID          stop a chat in a terminal or VS Code (it's kept); only an idle one
                    unless --busy (you were told it's working or waiting)
  --interrupt ID    interrupt a working chat in tmux (Ctrl+C in its pane)
  --agent-run ID    a subagent's whole run (of the chat --chat), as Markdown in the JSON
  --vscode-account  VS Code's Claude on this host runs with that CLAUDE_CONFIG_DIR (- for
                    none: a plain ~/.claude), in its VS Code server's settings (on an SSH host)
  --instance ID     this copy of the widget (with --interval-ms N, how often it checks):
                    says whether it sends the notifications, and the shared settings; the
                    one that does looks after the chats that keep going (look_after_kept)
  --keep-going      look after the chats that keep going (the VS Code window that notifies)
  --usage-cap N     stop what an account has at work once one of its plan windows reaches N%"""


def main(argv=None):
    # Hand-rolled instead of argparse, which alone costs ~10 ms of startup on
    # every poll.
    args = sys.argv[1:] if argv is None else list(argv)
    config_dirs, with_tokens, pretty, done_hours = [], True, False, KEEP_DONE_HOURS
    recent, remote, local, dismiss, undismiss = RECENT_CHATS, False, False, None, False
    end, interrupt, profile_key, busy, instance, interval_ms = None, None, None, False, None, 2000
    agent_run_id, chat_id, plans, find, find_limit = None, None, PLANS_LISTED, None, FIND_LIMIT
    task_folder, task_action, prompt, plan_slug, plan_profile, plan_text = None, None, "", None, None, None
    continue_id, continue_folder = None, None
    vscode_account, vscode_server = None, VSCODE_SERVERS["Code"]
    keep_going, usage_cap, model, effort, mode = False, 0, None, None, None
    starred_chats, starred_plans = set(), set()
    while args:
        arg = args.pop(0)
        if arg == "--config-dir" and args:
            config_dirs.append(args.pop(0))
        elif arg == "--end" and args:
            end = args.pop(0)
        elif arg == "--interrupt" and args:
            interrupt = args.pop(0)
        elif arg == "--agent-run" and args:
            agent_run_id = args.pop(0)
        elif arg == "--chat" and args:
            chat_id = args.pop(0)
        elif arg == "--find" and args:
            find = args.pop(0)
        elif arg in ("--start-task", "--task-command") and args:
            task_action, task_folder = arg, args.pop(0)
        elif arg == "--continue-chat" and args:
            continue_id = args.pop(0)
        elif arg == "--folder" and args:
            continue_folder = args.pop(0)
        elif arg == "--prompt" and args:
            prompt = args.pop(0)
        elif arg == "--model" and args:
            model = args.pop(0)
        elif arg == "--effort" and args:
            effort = args.pop(0)
        elif arg == "--permission-mode" and args:
            mode = args.pop(0)
        elif arg == "--plan" and args:
            plan_slug = args.pop(0)
        elif arg == "--plan-profile" and args:
            plan_profile = args.pop(0)
        elif arg == "--plan-text" and args:
            plan_text = args.pop(0)
        elif arg == "--busy":
            busy = True
        elif arg == "--vscode-account" and args:
            vscode_account = args.pop(0)
        elif arg == "--vscode-server" and args:
            vscode_server = args.pop(0)
        elif arg == "--profile" and args:
            profile_key = args.pop(0)
        elif arg == "--instance" and args:
            instance = args.pop(0)
        elif arg == "--interval-ms" and args:
            try:
                interval_ms = max(1000, int(args.pop(0)))
            except ValueError:
                sys.exit(USAGE)
        elif arg == "--done-hours" and args:
            try:
                done_hours = max(0.0, float(args.pop(0)))
            except ValueError:
                sys.exit(USAGE)
        elif arg in ("--recent", "--plans", "--limit") and args:
            try:
                n = max(0, int(args.pop(0)))
            except ValueError:
                sys.exit(USAGE)
            if arg == "--recent":
                recent = n
            elif arg == "--plans":
                plans = n
            else:
                find_limit = max(1, n)
        elif arg == "--remote":
            remote = True
        elif arg == "--local":
            local = True
        elif arg in ("--starred-chats", "--starred-plans") and args:
            (starred_chats if arg == "--starred-chats" else starred_plans).update(i for i in args.pop(0).split(",") if i)
        elif arg == "--dismiss" and args:
            dismiss = args.pop(0)
        elif arg == "--undismiss-all":
            undismiss = True
        elif arg.startswith("--config-dir="):
            config_dirs.append(arg.partition("=")[2])
        elif arg == "--no-tokens":
            with_tokens = False
        elif arg == "--pretty":
            pretty = True
        elif arg == "--keep-going":
            keep_going = True
        elif arg == "--usage-cap" and args and args[0].isdigit():
            usage_cap = min(100, int(args.pop(0)))
        else:
            sys.exit(USAGE)
    os.umask(0o077)  # cache files are private to the user
    home = Path.home()
    adopt_legacy_dirs(home)
    cache_root = cache_dir(home)
    if (agent_run_id is None) != (chat_id is None):
        sys.exit(USAGE)
    if (task_action is not None or continue_id is not None) and not profile_key:
        sys.exit(USAGE)
    if find is not None:
        try:
            result = find_chats(home, cache_root, config_dirs, find, find_limit)
            if remote:
                add_found_commands(result, home, config_dirs)
        except Exception as exc:
            result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    elif plan_text is not None:
        try:
            result = plan_markdown(home, config_dirs, plan_text, profile_key)
        except Exception as exc:
            result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    elif continue_id is not None:
        try:
            result = continue_chat(home, config_dirs, profile_key, continue_id, continue_folder, prompt)
        except Exception as exc:
            result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    elif task_action is not None:
        try:
            result = run_task(home, config_dirs, task_action == "--start-task", task_folder, profile_key, prompt,
                              plan_slug, plan_profile, model=model, effort=effort, mode=mode)
        except Exception as exc:
            result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    elif agent_run_id is not None:
        try:
            ok, message, title, markdown = agent_run(home, config_dirs, chat_id, agent_run_id, profile_key)
        except Exception as exc:
            ok, message, title, markdown = False, f"{type(exc).__name__}: {exc}", None, None
        result = {"ok": ok, "message": message, "title": title, "markdown": markdown}
    elif end is not None or interrupt is not None:
        try:
            ok, message = (stop_chat_by_id(home, config_dirs, end, profile_key, busy) if end is not None
                           else interrupt_chat_by_id(home, config_dirs, interrupt, profile_key))
        except Exception as exc:
            ok, message = False, f"{type(exc).__name__}: {exc}"
        result = {"ok": ok, "message": message}
    elif vscode_account is not None:
        ok, message = set_vscode_server_config_dir(home, None if vscode_account == "-" else vscode_account,
                                                   vscode_server, config_dirs)
        result = {"ok": ok, "message": message}
    elif dismiss is not None or undismiss:
        try:
            change_dismissed(cache_root, add=dismiss, clear=undismiss)
            result = {"ok": True, "message": "Removed from the list" if dismiss is not None else "Shown again"}
        except OSError as exc:
            result = {"ok": False, "message": str(exc)}
    else:
        try:
            result = collect(home, cache_root, config_dirs, with_tokens=with_tokens, keep_done_hours=done_hours,
                             recent=recent, plans=plans, usage_cap=usage_cap if not remote or local else 0,
                             # (on an SSH host: the host's stars, from the computer that asks)
                             starred={"chats": starred_chats, "plans": starred_plans} if remote and not local else None)
            try:
                add_vscode(result, home, config_dirs, remote and not local, cache_root)
            except Exception as exc:
                result["errors"].append(f"VS Code: {exc}")
            # Whether chats can open in a terminal here: the widget greys
            # that out when there's no claude command (and when unsure, doesn't)
            claude = None
            try:
                claude = cached_find_claude(home, cache_root, time.time())
                result["cli"] = {"installed": claude is not None}
            except Exception as exc:
                result["errors"].append(f"claude command: {exc}")
            if instance is not None:
                try:
                    result["notifier"] = elect_notifier(cache_root, instance, interval_ms, int(time.time() * 1000))
                    result["sharedSettings"] = shared_settings(cache_root)
                except (OSError, ValueError) as exc:
                    result["errors"].append(f"other widgets: {exc}")
            if not remote or local:  # (this computer's chats: an SSH host's keep going from here, not there)
                try:
                    if keep_going or result.get("notifier") is True:
                        look_after_kept(home, cache_root, config_dirs, result["groups"], int(time.time() * 1000),
                                        usage_cap)
                    result["keep"] = kept_chats(cache_root)
                except Exception as exc:
                    result["errors"].append(f"chats that keep going: {exc}")
            if remote:
                result["host"] = os.uname().nodename
                add_remote_commands(result, home, config_dirs, claude)
        except Exception as exc:  # the widget must always get parseable output
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "generatedAt": int(time.time() * 1000)}
    # ASCII only: a lone surrogate (half an emoji cut off in a title) can't be
    # written as UTF-8, and a host's locale may not be UTF-8 at all
    try:
        text = json.dumps(result, indent=2 if pretty and not remote else None)
    except (TypeError, ValueError) as exc:
        text = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}", "generatedAt": int(time.time() * 1000)})
    if remote:
        # Login scripts on the host may print things first: the JSON follows a marker
        sys.stdout.write("\n" + REMOTE_MARK + " ")
    sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
