#!/usr/bin/env python3
"""Claude Code's live hook for the Talpy Den widget: each step of each
chat as it happens, so the widget shows what a chat does the moment it does
it, not at its next check.

Claude runs it on its events without waiting for it (async): a prompt, a
tool call starting and ending, a permission it asks for, a subagent starting
and ending, the end of a turn, a session starting and ending. It folds the
event into the chat's live file, ~/.cache/talpy-den/live/<session id>.json:
what the chat does now, its last steps (with the lines an edit added and
removed), its running subagents and the first line of what it said last.
The widget watches that folder (live.py; the VS Code extension: fs.watch).

It is copied to ~/.local/share/talpy-den/live-hook.py and named in each
Claude profile's settings.json by pane.py --live on (the widget's setting
"Live mode"); --live off takes it out again. Standard library only, and
Python 3.7 (SSH hosts).
"""
import fcntl
import json
import os
import re
import sys
import time

VERSION = 1
STEPS = 8  # the last steps a live file keeps
AGENTS = 20
TEXT = 70  # characters of a step's words
SAID = 200  # of what Claude said last
KEEP_S = 3 * 86400  # live files of chats not heard from for this long go
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
# tools that ask you something (their permission request is the question)
ASKS = {"AskUserQuestion": "question", "ExitPlanMode": "plan"}


def folder():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "talpy-den", "live")


def short(value, limit=TEXT):
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe(name, inp):
    """What a tool call does, in a few words: "Bash: npm test", "Edit:
    parser.py" (talpy_den.describe_tool's words)."""
    name = name or "tool"
    inp = inp if isinstance(inp, dict) else {}
    if name.startswith("mcp__"):
        parts = name.split("__")
        name = "%s: %s" % (parts[1], parts[2]) if len(parts) >= 3 else name
    if name == "Bash" and inp.get("command"):
        return "Bash: " + short(inp["command"], 60)
    for key in ("file_path", "notebook_path", "path"):
        if inp.get(key):
            return "%s: %s" % (name, os.path.basename(str(inp[key]).rstrip("/")) or inp[key])
    for key in ("description", "pattern", "query", "url", "skill", "prompt"):
        if inp.get(key):
            return "%s: %s" % (name, short(inp[key], 60))
    return name


def line_count(text):
    if not isinstance(text, str) or not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def lines_changed(name, inp, response):
    """(added, removed) lines of an edit, from its result (a new file: its
    length), or None for a tool that doesn't edit."""
    if name not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return None
    added = removed = 0
    patch = response.get("structuredPatch") if isinstance(response, dict) else None
    if isinstance(patch, list) and patch:
        for hunk in patch:
            for line in (hunk.get("lines") if isinstance(hunk, dict) else None) or []:
                if isinstance(line, str) and line.startswith("+"):
                    added += 1
                elif isinstance(line, str) and line.startswith("-"):
                    removed += 1
        return added, removed
    if name == "Write" and isinstance(inp, dict):
        return line_count(inp.get("content")), 0
    return None


def first_line(text, limit=SAID):
    for line in (text or "").splitlines():
        line = line.replace("**", "").replace("`", "").strip().lstrip("#*>-| ").strip()
        if line:
            return short(line, limit)
    return ""


def fold(view, event, now):
    """The live file view with the hook's event in it; None when the event
    changes nothing the widget shows."""
    kind = event.get("hook_event_name")
    agent = event.get("agent_id") if isinstance(event.get("agent_id"), str) else None
    tool = event.get("tool_name") if isinstance(event.get("tool_name"), str) else ""
    inp = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    tid = event.get("tool_use_id") if isinstance(event.get("tool_use_id"), str) else None
    steps = [s for s in view.get("steps") or [] if isinstance(s, dict)]
    agents = [a for a in view.get("agents") or [] if isinstance(a, dict)]

    def step_of(create):
        text = describe(tool, inp)
        for s in steps:
            if tid and s.get("id") == tid:
                return s
        if not tid:  # (a permission request names no tool call: the newest open one of that tool and input)
            for s in reversed(steps):
                if s.get("end") is None and s.get("tool") == tool and s.get("text") == text and not s.get("agent"):
                    return s
        if not create:
            return None
        s = {"id": tid, "tool": tool, "text": text, "at": now, "end": None, "ok": None}
        if agent:
            s["agent"] = next((a.get("type") for a in agents if a.get("id") == agent), None) or "agent"
        steps.append(s)
        return s

    if kind == "SessionStart":
        view.update(state="idle", doing="", asks=None, turnAt=None)
        if event.get("source") in ("startup", "clear"):
            steps, agents = [], []
    elif kind == "UserPromptSubmit":
        view.update(state="working", doing="thinking…", asks=None, turnAt=now, said="", saidAt=None)
    elif kind == "PreToolUse":
        s = step_of(True)  # (a quick tool's end can come first: then it stays ended)
        if agent:
            for a in agents:
                if a.get("id") == agent:
                    a["doing"] = s["text"]
        else:
            view.update(state="working", doing=s["text"], asks=None)
    elif kind in ("PostToolUse", "PostToolUseFailure"):
        s = step_of(True)
        s["end"] = now
        s["ok"] = kind == "PostToolUse"
        changed = lines_changed(tool, inp, event.get("tool_response")) if s["ok"] else None
        if changed:
            s["add"], s["del"] = changed
        if not s["ok"] and isinstance(event.get("error"), str):
            s["error"] = short(event["error"], 120)
        if not agent and view.get("state") == "waiting":
            view.update(state="working", asks=None)  # (answered)
    elif kind == "PermissionRequest":
        if agent:
            return None  # (a subagent's: Claude's own dialog and the session file say it)
        step_of(True)
        view.update(state="waiting", doing=describe(tool, inp), asks=ASKS.get(tool, "permission"))
    elif kind == "Notification":
        what = event.get("notification_type")
        if what == "permission_prompt" or what == "elicitation_dialog":
            view.update(state="waiting", asks=view.get("asks") or ("permission" if what == "permission_prompt" else "input"))
        elif what == "idle_prompt":
            view.update(state="idle", doing="", asks=None)
        else:
            return None
    elif kind == "Stop":
        view.update(state="idle", doing="", asks=None,
                    said=first_line(event.get("last_assistant_message")), saidAt=now)
    elif kind == "StopFailure":
        error = event.get("error")
        view.update(state="idle", doing="", asks=None, failed=short(error if isinstance(error, str) else "error", 60),
                    said=first_line(event.get("error_details") or event.get("last_assistant_message")), saidAt=now)
    elif kind == "SubagentStart":
        agent_id = event.get("agent_id")
        if isinstance(agent_id, str) and not any(a.get("id") == agent_id for a in agents):
            agents.append({"id": agent_id, "type": short(event.get("agent_type") or "agent", 40), "at": now, "doing": ""})
    elif kind == "SubagentStop":
        agents = [a for a in agents if a.get("id") != event.get("agent_id")]
    elif kind == "PreCompact":
        view.update(state="working", doing="compacting…", asks=None)
    elif kind == "SessionEnd":
        view.update(state="ended", doing="", asks=None)
        agents = []
    else:
        return None
    if kind in ("UserPromptSubmit", "SessionStart"):
        view.pop("failed", None)
    view["steps"] = steps[-STEPS:]
    view["agents"] = agents[-AGENTS:]
    view["event"] = kind
    view["at"] = now
    return view


def tidy(where, now):
    """Live files of chats not heard from for days go (once per new session)."""
    try:
        names = os.listdir(where)
    except OSError:
        return
    for name in names:
        path = os.path.join(where, name)
        try:
            if now - os.stat(path).st_mtime > KEEP_S:
                os.unlink(path)
        except OSError:
            pass


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    sid = event.get("session_id") if isinstance(event, dict) else None
    if not isinstance(sid, str) or not SESSION_ID.match(sid):
        return 0
    now = int(time.time() * 1000)
    where = folder()
    try:
        os.makedirs(where, mode=0o700, exist_ok=True)
    except OSError:
        return 0
    path = os.path.join(where, sid + ".json")
    old = os.umask(0o077)
    try:
        with open(os.path.join(where, sid + ".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # (Claude doesn't wait for one event's hook before the next)
            try:
                with open(path, encoding="utf-8") as f:
                    view = json.load(f)
            except (OSError, ValueError):
                view = None
            if not isinstance(view, dict) or view.get("v") != VERSION:
                view = {"v": VERSION, "sessionId": sid, "state": "idle", "doing": "", "steps": [], "agents": []}
            view = fold(view, event, now)
            if view is None:
                return 0
            if event.get("hook_event_name") != "SessionEnd":
                view["pid"] = os.getppid()  # Claude (its hooks are its children); gone by SessionEnd
            for key in ("cwd", "transcript_path"):
                if isinstance(event.get(key), str):
                    view["transcript" if key == "transcript_path" else key] = event[key]
            tmp = "%s.%d.tmp" % (path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(view, f, ensure_ascii=True, separators=(",", ":"))
            os.replace(tmp, path)
    except OSError:
        return 0
    finally:
        os.umask(old)
    if event.get("hook_event_name") == "SessionStart":
        tidy(where, now / 1000)
    return 0


if __name__ == "__main__":
    sys.exit(main())
