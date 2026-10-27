#!/usr/bin/env python3
"""The chat pane of Talpy Den: one chat's conversation, a reply to it,
its permission prompt and its review.

    python3 -I pane.py ACTION [--host HOST] [--config-dir DIR]... [--remote] [--pretty]

  --conversation CHAT [--profile P] [--limit N] [--if-changed STAMP]
        the chat as the pane shows it: your prompts, Claude's replies
        (markdown), each tool call as one line with how it went, and notes
        (a usage limit, an interruption); the newest N items. STAMP: the
        "stamp" of the last answer; while the transcript is as it was then,
        the answer is only {"ok", "unchanged"}.
  --send CHAT --profile P --text T [--session ID] [--how auto|background|copy] [--after-turn] [--name N]
        your message to the chat, the way it can take one (send):
        typed into its tmux pane; put in the input of its VS Code tab; or
        Claude goes on with it in the background (claude --bg --resume),
        after a background session that has it stops. A chat in a terminal
        that takes no text from outside asks first (how background: it goes
        on in the background; copy: the message goes to the clipboard).
        --after-turn: queued, it's sent once the chat's turn ends.
  --answer REQUEST --decision allow|always|deny|answer|approve|approve-edits|keep [--note T] [--answers JSON]
        answer a permission request the hook relayed (permission_hook.py);
        Claude's questions (AskUserQuestion): answer, with JSON {question:
        [chosen labels, or what you typed]}, or deny (decline, with a note)
  --keys CHAT --decision allow|always|deny [--note T] [--profile P]
        answer a permission prompt of a chat in tmux with its keys
  --review CHAT [--profile P]
        what the chat changed: each file with its added and removed lines
        and a diff (against the file before the chat, from Claude's own
        backups), whether tests ran after its last edit and how they went,
        and a commit message from its name and recap
  --undo CHAT [--file PATH]... [--profile P]
        put the files back as they were before the chat (all, or those)
  --commit CHAT --message M [--file PATH]... [--profile P]
        commit only the files the chat changed (or those), in each git
        repository they are in
  --reviewed CHAT [--off]
        mark the chat reviewed (until it changes files again), or not
  --relay on|off|status
        the permission hook in each Claude profile's settings.json
  --live on|off|status
        live mode: the live hook (live_hook.py) in each Claude profile's
        settings.json, so the widget shows each step of each chat as it happens
  --complete CHAT [--profile P]
        what the reply box offers as you type: / commands (Claude's own, yours
        and the project's) and skills, the project's files for @, and your
        past prompts in its folder (newest first) for ↑
  --logs JOB [--profile P]
        a background session's screen now (claude logs), as lines of text
  --oldest allow|deny [--osd]
        answer the oldest prompt a chat waits on (one the hook relayed; a plan:
        approve it, or keep planning), for a keyboard shortcut; --osd: say on
        screen what was answered
  --shortcuts on|off|status
        keyboard answers: the shortcuts for --oldest (Plasma's global
        shortcuts: Meta+Alt+Y allows, Meta+Alt+N denies, where they're free)

--host HOST: the same on an SSH host (remote.py runs this file there, after
talpy_den.py); --remote: the JSON after a marker line, as there.
Python 3.7+ and the standard library only.
"""
import base64
import difflib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ inside the installed widget...
if os.path.isfile(globals().get("__file__") or ""):  # (on an SSH host it comes on stdin, after talpy_den.py)
    if sys.version_info >= (3, 8):  # ...but talpy_den.py compiled in the cache, as poll.py does
        _cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "talpy-den"
        if _cache.is_dir():
            sys.pycache_prefix = str(_cache / "pycache")
            sys.dont_write_bytecode = False
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I leaves the script dir off sys.path
import talpy_den as cs  # noqa: E402

# ---------------------------------------------------------------- the conversation

PANE_ITEMS = 120  # items the pane gets at first ("Show earlier" asks for more)
PANE_ITEMS_MAX = 2000
PANE_TAIL_BYTES = 512 << 10
PANE_TAIL_MAX = 64 << 20
TEXT_CHARS = 20000  # of one prompt or reply
DETAIL_CHARS = 1200  # of a tool call's input or output, unfolded
DETAIL_LINES = 24
PANE_LINE = re.compile(rb'"type":\s*"(?:user|assistant|system)"')
INTERRUPTED = "[Request interrupted"


def clip(text, chars=DETAIL_CHARS, lines=DETAIL_LINES):
    """text cut to lines lines and chars characters (… where it was cut)."""
    text = (text or "").rstrip()
    rows = text.split("\n")
    cut = len(rows) > lines
    text = "\n".join(rows[:lines])
    if len(text) > chars:
        text, cut = text[:chars - 1].rstrip(), True
    return text + ("\n…" if cut else "")


def result_text(block):
    """The text of a tool_result block."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text") for c in content
                         if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str))
    return ""


def tool_input_text(block):
    """What a tool call was asked to do, for its unfolded line: the command,
    the file and the change, the search…"""
    name = block.get("name") or ""
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    if name == "Bash" and isinstance(inp.get("command"), str):
        return "$ " + inp["command"]
    path = inp.get("file_path") or inp.get("notebook_path")
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit") and isinstance(path, str):
        return "\n".join([path] + cs.edit_diff(name, inp, None)[:DETAIL_LINES])
    parts = []
    for key, value in inp.items():
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            parts.append(f"{key}: {value}")
    return "\n".join(parts)


EDITS = ("Edit", "MultiEdit", "Write", "NotebookEdit")
EDIT_DIFF_LINES = 40  # of one edit's diff, in the chat (the Review tab has whole files)
DIFF_BUDGET = 600  # of all the edits' diffs in one answer, the newest first: older ones keep a glimpse
DIFF_GLIMPSE = 6
READ_LINE = re.compile(r"^\s*\d+[\t\u2192]")
TOOL_ERROR = re.compile(r"</?tool_use_error>")


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def edit_lines(name, inp, result, current=None):
    """The lines of a unified diff (@@ lines included) of what an Edit,
    MultiEdit, Write or NotebookEdit call changed, as the chat shows it under
    the call: from what the call gave back (its patch, at the file's line
    numbers; all of a new file), else from what it was asked (numbered from
    the change, with current: the file's text, at its line numbers; nothing
    for a Write, which may replace a whole file)."""
    lines = None
    if isinstance(result, dict):
        patch = result.get("structuredPatch")
        if result.get("type") == "create" and isinstance(result.get("content"), str):
            body = result["content"].splitlines()
            lines = [f"@@ -0,0 +1,{len(body)} @@"] + ["+" + l for l in body]
        elif isinstance(patch, list) and patch:
            lines = []
            for h in patch:
                if isinstance(h, dict) and isinstance(h.get("lines"), list):
                    lines.append(f"@@ -{h.get('oldStart', 0)},{h.get('oldLines', 0)} +{h.get('newStart', 0)},{h.get('newLines', 0)} @@")
                    lines += [l for l in h["lines"] if isinstance(l, str)]
    if lines is None:
        lines = cs.edit_diff(name, inp, current, limit=None) if name != "Write" else []
    return lines


def capped(lines):
    """An edit's diff lines as the chat gets them: the first EDIT_DIFF_LINES, and how many more."""
    shown = [l[:500] for l in lines[:EDIT_DIFF_LINES]]
    if len(lines) > EDIT_DIFF_LINES:
        shown.append(f"… ({len(lines) - EDIT_DIFF_LINES} more lines)")
    return shown


def change_summary(name, diff, fallback=""):
    """What an edit changed, in a line: from its diff's lines."""
    added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
    if name != "Edit" and name != "MultiEdit" and not removed:
        return plural(added, "line")
    if added and removed:
        return f"{plural(added, 'line')} added, {removed} removed"
    return f"{plural(added, 'line')} added" if added else f"{plural(removed, 'line')} removed" if removed else fallback


def tool_summary(name, inp, output, error):
    """What came of a tool call, in one line, as Claude's terminal shows it
    under the call: the error, the lines it read or changed, else the first
    line of what came back and how many more there are."""
    rows = [r for r in TOOL_ERROR.sub("", output or "").strip().split("\n") if r.strip()]
    first = cs.short_text(rows[0], 120) if rows else ""
    if error:
        return first or "Failed"
    if name == "Read":
        read = sum(1 for r in rows if READ_LINE.match(r))
        return plural(read, "line") if read else first   # (an image, a PDF: no numbered lines)
    if name in EDITS:
        return change_summary(name, cs.edit_diff(name, inp, None, limit=None), first)
    if not rows:
        return "(no output)" if name == "Bash" else ""
    return first + (f" … +{plural(len(rows) - 1, 'line')}" if len(rows) > 1 else "")


def conversation_items(lines):
    """The pane's items from transcript lines (oldest first): {"kind": "you",
    "text"} for a prompt of yours, {"kind": "claude", "text"} for Claude's
    reply (markdown), {"kind": "tool", "name", "line", "status":
    "ok"|"error"|"open", "input", "output", "summary" (what came of it, in a
    line), "diff" (an edit's: edit_lines)} for a tool call and {"kind":
    "note", "text"} for an interruption, a usage limit or a compaction;
    each with "at" (ms) and "key" (the same in the next answer, as long as
    the item is listed). Entries of subagents (sidechains) aren't in it."""
    items, tools = [], {}
    for line in lines:
        if not PANE_LINE.search(line):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("isSidechain"):
            continue
        kind, at = e.get("type"), cs.to_ms(e.get("timestamp"))
        uid = e["uuid"] if isinstance(e.get("uuid"), str) else f"{kind}-{at}-{len(items)}"
        if kind == "system":
            if e.get("subtype") == "compact_boundary":
                items.append({"kind": "note", "key": uid, "text": "The conversation was compacted", "at": at})
            continue
        msg, blocks = cs.blocks_of(e)
        if kind == "user":
            if e.get("isCompactSummary") or e.get("isMeta"):
                continue
            results = [c for c in blocks if c.get("type") == "tool_result"]
            for c in results:
                if c.get("tool_use_id") in tools:
                    item, inp = tools.pop(c["tool_use_id"])
                    output = result_text(c)
                    item["status"] = "error" if c.get("is_error") else "ok"
                    item["output"] = clip(output)
                    item["summary"] = tool_summary(item["name"], inp, output, bool(c.get("is_error")))
                    if item["name"] in EDITS and c.get("is_error"):
                        item["diff"] = []
                    elif item["name"] in EDITS:  # (what came back is the entry's, when it has one result)
                        full = edit_lines(item["name"], inp, e.get("toolUseResult") if len(results) == 1 else None)
                        item["diff"] = capped(full)
                        if full:
                            item["summary"] = change_summary(item["name"], full, item["summary"])
            prompt = cs.human_prompt(e)
            if prompt is not None:
                items.append({"kind": "you", "key": uid, "text": clip(prompt, TEXT_CHARS, 10 ** 6), "at": at})
                continue
            text = cs.message_text(msg).strip()
            if text.startswith(INTERRUPTED):
                items.append({"kind": "note", "key": uid, "text": "Interrupted", "at": at})
            continue
        if kind != "assistant":
            continue
        if e.get("isApiErrorMessage"):
            text = " ".join(cs.message_text(msg).split())
            if text:
                items.append({"kind": "note", "key": uid, "text": cs.short_text(text, 300), "at": at, "error": True})
            continue
        mid = msg.get("id") if isinstance(msg.get("id"), str) else None
        for c in blocks:
            if c.get("type") == "text" and isinstance(c.get("text"), str) and c["text"].strip():
                last = items[-1] if items else None
                if last is not None and last["kind"] == "claude" and mid is not None and last["key"] == mid:
                    last["text"] = clip(last["text"] + "\n\n" + c["text"].strip(), TEXT_CHARS, 10 ** 6)
                else:
                    items.append({"kind": "claude", "key": mid or uid, "text": clip(c["text"].strip(), TEXT_CHARS, 10 ** 6),
                                  "at": at})
            elif c.get("type") == "tool_use" and isinstance(c.get("id"), str):
                inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                item = {"kind": "tool", "key": c["id"], "name": c.get("name") or "tool", "line": cs.describe_tool(c),
                        "status": "open", "input": clip(tool_input_text(c)), "output": "", "summary": "", "at": at,
                        "diff": capped(edit_lines(c.get("name"), inp, None)) if c.get("name") in EDITS else []}
                tools[c["id"]] = (item, inp)
                items.append(item)
    for item, inp in tools.values():  # (an edit not made yet: at the file's line numbers)
        path = inp.get("file_path")
        if item["name"] == "Edit" and isinstance(path, str) and path.startswith("/"):
            try:
                data = read_bytes(path) if os.path.getsize(path) < (2 << 20) else None
            except OSError:
                data = None
            if data is not None:
                item["diff"] = capped(edit_lines("Edit", inp, None, data.decode("utf-8", "replace")))
    return items


def within_budget(items):
    """The edits' diffs cut to DIFF_BUDGET lines in all, the newest whole:
    an older one keeps its first DIFF_GLIMPSE lines (and says how many more)."""
    left = DIFF_BUDGET
    for item in reversed(items):
        diff = item.get("diff")
        if not diff:
            continue
        if len(diff) > left:
            keep = min(len(diff), max(left, DIFF_GLIMPSE))
            more = sum(1 for l in diff[keep:] if not l.startswith("@@"))
            item["diff"] = diff[:keep] + ([f"… ({more} more lines)"] if more else [])
        left = max(0, left - len(diff))
    return items


def tail_lines(path, size, nbytes):
    """The whole lines in the last nbytes of a file (its first, cut, one
    dropped), and whether they start at the file's start."""
    with open(path, "rb") as f:
        f.seek(max(0, size - nbytes))
        lines = f.read(min(size, nbytes)).split(b"\n")
    whole = size <= nbytes
    if not whole:
        lines = lines[1:]
    return lines, whole


def conversation(path, limit=PANE_ITEMS):
    """(items, more) of a chat: its newest limit items (conversation_items),
    and whether there are older ones."""
    size = path.stat().st_size
    nbytes = PANE_TAIL_BYTES
    while True:
        lines, whole = tail_lines(path, size, nbytes)
        items = conversation_items(lines)
        if whole or len(items) > limit or nbytes >= PANE_TAIL_MAX:
            return within_budget(items[-limit:]), len(items) > limit or not whole
        nbytes *= 4


def stamp_of(path):
    st = path.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def find_transcript(home, extra_dirs, chat_id, profile_key=None):
    """(profile, transcript) of a chat: under the profile named first, else any that has it."""
    profiles = cs.discover_profiles(home, extra_dirs)
    ordered = sorted(profiles, key=lambda p: not (profile_key and cs.matches_profile(p, profile_key)))
    for p in ordered:
        path = cs.transcript_path(p, chat_id)
        if path is not None:
            return p, path
    return None, None


def chat_conversation(home, extra_dirs, chat_id, profile_key=None, limit=PANE_ITEMS, if_changed=None):
    if not cs.valid_id(chat_id):
        return {"ok": False, "message": f"Not a chat: {chat_id}"}
    profile, path = find_transcript(home, extra_dirs, chat_id, profile_key)
    if path is None:
        return {"ok": False, "message": "That chat's transcript isn't there any more"}
    stamp = stamp_of(path)
    if if_changed and if_changed == f"{stamp}/{limit}":
        return {"ok": True, "unchanged": True}
    items, more = conversation(path, max(1, min(PANE_ITEMS_MAX, limit)))
    name, cwd, _ = cs.read_chat_info(path, path.stat().st_size)
    peek = cs.chat_peek(path) or {}
    return {"ok": True, "chatId": chat_id, "profileDir": profile.key, "name": name, "cwd": cwd or cs.chat_folder(path) or "",
            "items": items, "more": more, "stamp": f"{stamp}/{limit}", "mode": peek.get("mode"),
            "permission": cs.pending_permission(path, cwd)}


# ---------------------------------------------------------------- permission requests the hook relays

DECISIONS = ("allow", "always", "deny", "answer")
PLAN_DECISIONS = ("approve", "approve-edits", "keep")   # a plan's (ExitPlanMode)


def answer_text(values, multi):
    """One question's answer as Claude's dialog sends it: the option chosen
    (or what you typed); several, comma-separated (one with ", " or a quote
    in it quoted, as Claude quotes it)."""
    values = [v.strip() for v in values if isinstance(v, str) and v.strip()]
    if not multi:
        return values[0] if values else ""
    return ", ".join(json.dumps(v, ensure_ascii=False) if ", " in v or '"' in v else v for v in values)


def question_answers(inp, answers):
    """{question: answer text} for the AskUserQuestion call inp, from
    answers {question: [labels or text]}: the questions it asks only."""
    out = {}
    for q in cs.question_view(inp):
        given = answers.get(q["question"]) if isinstance(answers, dict) else None
        text = answer_text(given if isinstance(given, list) else [given], q["multiSelect"])
        if text:
            out[q["question"]] = text
    return out


def decision_of(decision, note, suggestions, inp=None, answers=None):
    """The hook's answer to Claude (its "decision") for the widget's button
    (a question's: the call's input with the answers in it)."""
    question = isinstance(inp, dict) and answers is not None
    if decision == "deny":
        note = (note or "").strip()
        if note:  # (said as yours: Claude reads it in the tool's result)
            said = "didn't answer" if question else "denied this"
            return {"behavior": "deny", "message": f"The user {said} and said: {note}"}
        return {"behavior": "deny", "message": "The user didn't answer." if question else "The user denied this.",
                "interrupt": True}
    out = {"behavior": "allow"}
    if question:
        out["updatedInput"] = dict(inp, answers=answers)
    elif decision == "always" and suggestions:
        out["updatedPermissions"] = suggestions
    return out


def plan_decision(decision, note, inp):
    """The hook's answer to a plan (ExitPlanMode): approve it (an allow
    with the call's input; accept edits from then on: a setMode), or keep
    planning with what to change (a deny: its message is the feedback)."""
    if decision == "keep":
        note = (note or "").strip()
        return {"behavior": "deny", "message": f"The user wants to keep planning and said: {note}" if note
                else "The user wants to keep planning: don't start yet, work more on the plan and present it again."}
    out = {"behavior": "allow", "updatedInput": dict(inp)}
    if decision == "approve-edits":
        out["updatedPermissions"] = [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]
    return out


def answer_request(cache_root, request_id, decision, note="", answers=None):
    """Answer a relayed request: (ok, message). A question takes "answer"
    (answers: {question: [labels or text]}) or "deny"."""
    if decision not in DECISIONS + PLAN_DECISIONS:
        return False, f"Not an answer: {decision}"
    if not re.fullmatch(r"[0-9]+-[0-9a-f]{8}", request_id or ""):
        return False, f"Not a request: {request_id}"
    folder = cs.requests_dir(cache_root)
    req = cs.read_json(folder / (request_id + ".json"))
    if not isinstance(req, dict) or not cs.pid_alive(req.get("hookPid")):
        return False, "It isn't asking any more (answered in the chat?)"
    inp, given = None, None
    plan = req.get("tool") == cs.PLAN_TOOL
    if plan != (decision in PLAN_DECISIONS) and not (plan and decision == "deny"):
        return False, "Approve the plan or keep planning" if plan else "It asks for a permission, not about a plan"
    if plan:
        inp = req.get("input") if isinstance(req.get("input"), dict) else {}
        answer = plan_decision("keep" if decision == "deny" else decision, note, inp)
    elif req.get("tool") == cs.ASK_TOOL:
        inp = req.get("input") if isinstance(req.get("input"), dict) else {}
        if decision in ("allow", "always"):
            return False, "Choose your answers first"
        given = question_answers(inp, answers) if decision == "answer" else {}
        if decision == "answer" and not given:
            return False, "Choose your answers first"
    elif decision == "answer":
        return False, "It asks for a permission, not a question"
    if not plan:
        answer = decision_of(decision, note, req.get("suggestions") or [], inp, given)
    cs.write_json_atomic(folder / (request_id + ".answer.json"), answer)
    deadline = time.time() + 5
    while (folder / (request_id + ".json")).exists() and time.time() < deadline:
        time.sleep(0.1)
    if (folder / (request_id + ".json")).exists():
        return False, "The chat didn't take the answer: answer it there"
    if plan:
        return True, {"approve": "Approved: it starts", "approve-edits": "Approved: it starts, accepting edits"}.get(
            decision, "It keeps planning" + (" with your note" if (note or "").strip() else ""))
    if inp is not None:
        return True, "Answered" if decision == "answer" else "Declined"
    return True, {"allow": "Allowed", "always": "Allowed, and from now on", "deny": "Denied"}[decision]


# ---------------------------------------------------------------- what the reply box offers
#
# Claude's own commands (as of Claude Code 2.1.311, the ones that make sense
# typed into a chat), your commands and skills (the profile's commands/ and
# skills/), the project's (.claude/commands, .claude/skills), its files (git
# ls-files, else a walk) and your past prompts there (history.jsonl).

BUILTIN_COMMANDS = (
    ("add-dir", "Add a new working directory"), ("agents", "Manage subagents"), ("branch", "Branch the conversation here"),
    ("btw", "Ask a quick side question without interrupting"), ("clear", "Start a new session with empty context"),
    ("compact", "Free up context by summarizing the conversation so far"), ("config", "Open settings"),
    ("context", "Show how full the context is"), ("copy", "Copy Claude's last response"), ("cost", "Show what this session cost"),
    ("doctor", "Check Claude Code's installation"), ("effort", "Set the effort level"), ("export", "Export the conversation"),
    ("fork", "Copy this conversation into a new background session"), ("goal", "Set a goal Claude checks before stopping"),
    ("help", "Show help and the commands"), ("hooks", "Show the hooks"), ("init", "Write a CLAUDE.md for this project"),
    ("insights", "A report about your Claude Code sessions"), ("mcp", "Manage MCP servers"), ("memory", "Edit CLAUDE.md and memory"),
    ("model", "Set the model"), ("permissions", "Manage what Claude may do without asking"),
    ("plan", "Plan mode, or the session's plan"), ("pr-comments", "Get the comments of a pull request"),
    ("recap", "A one-line recap of the session"), ("resume", "Resume another conversation"), ("review", "Review a pull request"),
    ("rewind", "Go back to an earlier point"), ("security-review", "Review the pending changes for security"),
    ("skills", "List the skills"), ("status", "Version, model, account and connections"),
    ("subtask", "Send a subagent off with your full context"), ("todos", "Show the task list"), ("usage", "Show plan usage"),
)
COMPLETE_FILES_MAX = 20000
COMPLETE_HISTORY_MAX = 200
COMMAND_FILES_MAX = 500
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "dist", "build", ".tox",
             ".cache", "target", ".gradle", ".idea"}


def front_matter(text):
    """{key: value} of a Markdown file's front matter (--- … ---), and its first line of text after it."""
    lines, meta, first = text.splitlines(), {}, ""
    i = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                break
            key, sep, value = lines[i].partition(":")
            if sep and key.strip():
                meta[key.strip().lower()] = value.strip().strip("'\"")
        i += 1
    for line in lines[i:]:
        line = line.strip().lstrip("#").strip()
        if line:
            first = line
            break
    return meta, first


def command_files(folder, scope):
    """Commands in a commands/ folder: [{name, about, kind}] (a file in a subfolder: dir:name)."""
    out = []
    try:
        files = sorted(folder.rglob("*.md"))[:COMMAND_FILES_MAX]
    except OSError:
        return out
    for f in files:
        try:
            meta, first = front_matter(f.read_text(encoding="utf-8", errors="replace")[:4000])
        except OSError:
            continue
        name = ":".join(f.relative_to(folder).with_suffix("").parts)
        out.append({"name": "/" + name, "about": cs.short_text(meta.get("description") or first, 100), "kind": scope})
    return out


def skill_dirs(folder, scope):
    """Skills in a skills/ folder (each its SKILL.md): [{name, about, kind}]."""
    out = []
    try:
        dirs = sorted(d for d in folder.iterdir() if d.is_dir())[:COMMAND_FILES_MAX]
    except OSError:
        return out
    for d in dirs:
        try:
            meta, first = front_matter((d / "SKILL.md").read_text(encoding="utf-8", errors="replace")[:4000])
        except OSError:
            continue
        out.append({"name": "/" + (meta.get("name") or d.name), "about": cs.short_text(meta.get("description") or first, 100),
                    "kind": scope + " skill"})
    return out


def project_files(cwd):
    """The project's files, relative to cwd (git ls-files: tracked and new, not ignored; else a walk
    that leaves out dependencies and build output), at most COMPLETE_FILES_MAX: (files, cut)."""
    git = shutil.which("git")
    if git and cwd:
        try:
            r = subprocess.run([git, "-C", cwd, "ls-files", "-co", "--exclude-standard", "-z"], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=10)
            if r.returncode == 0:
                files = [f for f in r.stdout.decode("utf-8", "replace").split("\0") if f]
                return files[:COMPLETE_FILES_MAX], len(files) > COMPLETE_FILES_MAX
        except (OSError, subprocess.TimeoutExpired):
            pass
    files = []
    if not cwd or not os.path.isdir(cwd) or os.path.realpath(cwd) == str(Path.home()):
        return files, False   # (not all of your home)
    for top, dirs, names in os.walk(cwd):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        rel = os.path.relpath(top, cwd)
        for n in sorted(names):
            files.append(n if rel == "." else os.path.join(rel, n))
            if len(files) >= COMPLETE_FILES_MAX:
                return files, True
    return files, False


def past_prompts(profile, cwd):
    """Your prompts in that folder (or the one it's in: a chat that started
    there and went into it), newest first, each once (history.jsonl)."""
    out, seen = [], set()
    for e in reversed(cs.history_tail(profile)):
        text, project = e.get("display"), e.get("project")
        if not isinstance(text, str) or not text.strip() or text in seen:
            continue
        if cwd and not (isinstance(project, str) and (cwd == project or cwd.startswith(project.rstrip("/") + "/"))):
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= COMPLETE_HISTORY_MAX:
            break
    return out


def completions(home, extra_dirs, chat_id, profile_key=None):
    if not cs.valid_id(chat_id):
        return {"ok": False, "message": f"Not a chat: {chat_id}"}
    profile, path = find_transcript(home, extra_dirs, chat_id, profile_key)
    if path is None:
        return {"ok": False, "message": "That chat's transcript isn't there any more"}
    _, cwd, _ = cs.read_chat_info(path, path.stat().st_size)
    cwd = cwd or cs.chat_folder(path) or ""
    commands = [{"name": "/" + n, "about": a, "kind": "Claude"} for n, a in BUILTIN_COMMANDS]
    commands += command_files(profile.path / "commands", "yours") + skill_dirs(profile.path / "skills", "your")
    if cwd:
        commands += command_files(Path(cwd) / ".claude" / "commands", "project")
        commands += skill_dirs(Path(cwd) / ".claude" / "skills", "project")
    seen, unique = set(), []
    for c in reversed(commands):   # (yours and the project's before Claude's of the same name)
        if c["name"] not in seen:
            seen.add(c["name"])
            unique.append(c)
    files, cut = project_files(cwd)
    return {"ok": True, "cwd": cwd, "commands": sorted(unique, key=lambda c: c["name"]), "files": files, "filesCut": cut,
            "history": past_prompts(profile, cwd)}


# ---------------------------------------------------------------- a background session's screen
#
# claude logs ID gives a background session's recent terminal output: its
# screen as Claude draws it (cursor moves, colors). screen_of plays that into
# a grid and gives the lines that show, for the pane's Output tab. It only
# asks the background service that runs (never starts one: claude would).

ESCAPE = re.compile(r"\x1b(?:\[([0-9;?]*)([A-Za-z@`])|\]([^\x07\x1b]*)(?:\x07|\x1b\\)|([78])|[()][0-9A-Za-z]|.)")


def screen_of(stream, width=200, height=60):
    """The lines a terminal shows after stream (a TUI's output), trailing
    spaces and empty lines at the end left out."""
    grid = [[" "] * width for _ in range(height)]
    row = col = 0
    saved = (0, 0)
    pos = 0
    text = stream.replace("\r\n", "\n")
    for m in ESCAPE.finditer(text):
        for ch in text[pos:m.start()]:
            if ch == "\r":
                col = 0
            elif ch == "\n":
                row, col = min(height - 1, row + 1), 0
            elif ch == "\b":
                col = max(0, col - 1)
            elif ch >= " ":
                if col < width:
                    grid[row][col] = ch
                col += 1
        pos = m.end()
        args, op = m.group(1), m.group(2)
        if m.group(4) == "7":
            saved = (row, col)
        elif m.group(4) == "8":
            row, col = saved
        elif op:
            nums = [int(n) if n.isdigit() else 0 for n in (args or "").replace("?", "").split(";")]
            n = nums[0] if nums and nums[0] else 1
            if op == "A":
                row = max(0, row - n)
            elif op == "B":
                row = min(height - 1, row + n)
            elif op == "C":
                col = min(width - 1, col + n)
            elif op == "D":
                col = max(0, col - n)
            elif op == "G":
                col = min(width - 1, n - 1)
            elif op in "Hf":
                row = min(height - 1, max(0, (nums[0] if nums and nums[0] else 1) - 1))
                col = min(width - 1, max(0, (nums[1] if len(nums) > 1 and nums[1] else 1) - 1))
            elif op == "K":
                mode = nums[0] if nums else 0
                for c in (range(col, width) if mode == 0 else range(0, col + 1) if mode == 1 else range(width)):
                    grid[row][c] = " "
            elif op == "J":
                mode = nums[0] if nums else 0
                rows = range(row + 1, height) if mode == 0 else range(0, row) if mode == 1 else range(height)
                for r in rows:
                    grid[r] = [" "] * width
                if mode == 0:
                    grid[row][col:] = [" "] * (width - col)
    for ch in text[pos:]:
        if ch >= " " and col < width:
            grid[row][col] = ch
            col += 1
    lines, out = ["".join(r).rstrip() for r in grid], []
    for line in lines:
        if line or (out and out[-1]):   # (the empty part of the screen: one empty line)
            out.append(line)
    while out and not out[-1]:
        out.pop()
    return out


def job_screen(home, extra_dirs, job_id, profile_key=None):
    """A background session's screen now (claude logs): {"ok", "lines"}."""
    if not re.fullmatch(r"[0-9a-f]{8}", job_id or ""):
        return {"ok": False, "message": f"Not a background session: {job_id}"}
    profiles = [p for p in cs.discover_profiles(home, extra_dirs) if profile_key in (None, p.key)]
    profile = next((p for p in profiles if (p.path / "jobs" / job_id / "state.json").is_file()), None)
    if profile is None:
        return {"ok": False, "message": "That background session isn't there any more"}
    service = cs.service_config_dir(profile)
    if service is None:
        return {"ok": False, "message": "Its background service isn't running: it shows nothing now"}
    claude = cs.find_claude(home)
    if not claude:
        return {"ok": False, "message": "Claude Code isn't installed here"}
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    env["CLAUDE_CONFIG_DIR"] = service   # (the service's own name: another name of the folder would wait for ever)
    try:
        r = subprocess.run([claude, "logs", job_id], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           env=env, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "message": f"claude logs didn't answer: {exc}"}
    if r.returncode != 0:
        return {"ok": False, "message": (r.stderr or r.stdout).decode(errors="replace").strip()[:300] or "claude logs failed"}
    return {"ok": True, "lines": screen_of(r.stdout.decode("utf-8", "replace"))}


# ---------------------------------------------------------------- keyboard answers
#
# Two global shortcuts answer the oldest prompt a chat waits on (relayed by
# the hook): Allow once (a plan: Approve) and Deny (a plan: Keep planning),
# then a note on screen says what was answered (Plasma's on-screen display,
# else a notification). --shortcuts on puts a desktop file with the two
# actions in ~/.local/share/kglobalaccel (Plasma loads those at login, and
# runs an action's command when its keys are pressed) and registers them
# with kglobalaccel right away over D-Bus, with Meta+Alt+Y and Meta+Alt+N
# where no other shortcut has them (else none: you pick them in System
# Settings → Shortcuts → Talpy Den). --shortcuts off takes them out.

SHORTCUTS_COMPONENT = "org.wzaielamri.talpyden.answers.desktop"
SHORTCUT_ACTIONS = (  # (action, its name, default keys, the same as Qt's key code)
    ("allow", "Allow the oldest prompt", "Meta+Alt+Y", 0x10000000 | 0x08000000 | 0x59),
    ("deny", "Deny the oldest prompt", "Meta+Alt+N", 0x10000000 | 0x08000000 | 0x4E),
)
KGLOBALACCEL = ("--dest", "org.kde.kglobalaccel", "--object-path", "/kglobalaccel")


def oldest_request(cache_root):
    """The oldest relayed request a key can answer (not a question: those
    take answers), and whether a question waits too."""
    requests = list(cs.permission_requests(cache_root).values())
    usable = [r for r in requests if r.get("tool") != cs.ASK_TOOL]
    oldest = min(usable, key=lambda r: r.get("at") or 0) if usable else None
    return oldest, len(usable) < len(requests)


def chat_name(req):
    path = req.get("transcript")
    try:
        name, _, _ = cs.read_chat_info(Path(path), Path(path).stat().st_size) if isinstance(path, str) else ("", "", "")
    except (OSError, ValueError, TypeError):
        name = ""
    return name or "A chat"


def answer_oldest(cache_root, decision):
    """Allow (a plan: approve) or deny (a plan: keep planning) the oldest
    prompt: (ok, what to say)."""
    if decision not in ("allow", "deny"):
        return False, f"Not an answer: {decision}"
    req, question = oldest_request(cache_root)
    if req is None:
        return False, "A question waits: answer it in the panel" if question else "No chat waits for a permission"
    plan = req.get("tool") == cs.PLAN_TOOL
    ok, said = answer_request(cache_root, req["id"], ("approve" if decision == "allow" else "keep") if plan else decision)
    if not ok:
        return False, said
    return True, f"{chat_name(req)}: {said} · {cs.request_view(req)['line']}"


def show_note(text, ok):
    """A note on screen: Plasma's on-screen display, else a notification."""
    icon = "dialog-ok-apply" if ok else "dialog-information"
    gdbus = shutil.which("gdbus")
    tries = [[gdbus, "call", "--session", "--dest", "org.kde.plasmashell", "--object-path", "/org/kde/osdService",
              "--method", "org.kde.osdService.showText", icon, text]] if gdbus else []
    if shutil.which("notify-send"):
        tries.append(["notify-send", "--app-name=Talpy Den", f"--icon={icon}", "--expire-time=4000", "Talpy Den", text])
    for cmd in tries:
        try:
            if subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            continue
    return False


def shortcuts_file(home):
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    return data / "kglobalaccel" / SHORTCUTS_COMPONENT


def desktop_quote(arg):
    """An argument of a desktop file's Exec (its quoting rules)."""
    if re.fullmatch(r"[\w@%+=:,./-]+", arg):
        return arg
    return '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'


def shortcuts_desktop(script, free=None):
    """The desktop file with the two actions (script: this pane.py); keys
    only for the actions in free (None: all)."""
    run = lambda action: " ".join(desktop_quote(a) for a in ("python3", "-I", str(script), "--oldest", action, "--osd"))
    lines = ["[Desktop Entry]", "Type=Application", "Name=Talpy Den",
             "Comment=Answer the oldest permission prompt of your Claude chats", "Icon=dialog-question", "NoDisplay=true",
             "Exec=true", "Actions=" + "".join(a + ";" for a, _, _, _ in SHORTCUT_ACTIONS), ""]
    for action, name, keys, _ in SHORTCUT_ACTIONS:
        given = keys if free is None or action in free else ""
        lines += [f"[Desktop Action {action}]", f"Name={name}", f"Exec={run(action)}", f"X-KDE-Shortcuts={given}", ""]
    return "\n".join(lines)


def kglobalaccel(method, *args):
    """Call kglobalaccel over D-Bus: (ok, its answer)."""
    gdbus = shutil.which("gdbus")
    if gdbus is None:
        return False, "gdbus isn't installed"
    try:
        r = subprocess.run([gdbus, "call", "--session", *KGLOBALACCEL, "--method", "org.kde.KGlobalAccel." + method, *args],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return r.returncode == 0, (r.stdout if r.returncode == 0 else r.stderr).decode(errors="replace").strip()


def gv_strings(items):
    return "[" + ", ".join(json.dumps(i) for i in items) + "]"


def key_owners(code):
    """The components that have key code (Qt's) as a global shortcut, or None
    (kglobalaccel didn't answer)."""
    ok, out = kglobalaccel("getGlobalShortcutsByKey", str(code))
    return re.findall(r"\('([^']*)'", out) if ok else None


def shortcuts(home, on, script=None):
    """Keyboard answers on or off (or, on None, whether they're on): (ok, message)."""
    path = shortcuts_file(home)
    if on is None:
        return True, "on" if path.is_file() else "off"
    if not on:
        for action, _, _, _ in SHORTCUT_ACTIONS:
            kglobalaccel("unregister", json.dumps(SHORTCUTS_COMPONENT), json.dumps(action))
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return True, "Keyboard answers are off"
    # (a key another shortcut has stays its: that action gets none)
    owners = {action: key_owners(code) for action, _, _, code in SHORTCUT_ACTIONS}
    free = {a for a, o in owners.items() if not o or set(o) <= {SHORTCUTS_COMPONENT}}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(shortcuts_desktop(script or Path(__file__).resolve(), free), encoding="utf-8")
    os.replace(tmp, path)
    for action, name, keys, code in SHORTCUT_ACTIONS:
        ids = gv_strings([SHORTCUTS_COMPONENT, action, "Talpy Den", name])
        ok, out = kglobalaccel("doRegister", ids)
        if not ok:
            return True, "Keyboard answers are set up for your next login (Plasma's shortcuts didn't answer: " + out + ")"
        keys_now = f"[([{code}],)]" if action in free else "@a(ai) []"
        kglobalaccel("setShortcutKeys", ids, keys_now, "1")   # (its default)
        kglobalaccel("setShortcutKeys", ids, keys_now, "6")   # (and now: SetPresent, NoAutoloading)
    taken = [keys for action, _, keys, _ in SHORTCUT_ACTIONS if action not in free]
    if taken:
        return True, ("Keyboard answers are on; " + " and ".join(taken) + (" is" if len(taken) == 1 else " are")
                      + " another shortcut's: pick yours in System Settings → Shortcuts → Talpy Den")
    return True, "Keyboard answers are on: Meta+Alt+Y allows the oldest prompt, Meta+Alt+N denies it"


# ---------------------------------------------------------------- answering in tmux with keys

def tmux_base(entry):
    tmux = shutil.which("tmux", path=os.pathsep.join([os.environ.get("PATH", ""), "/usr/bin", "/usr/local/bin", "/bin"]))
    pane = entry.get("tmux") if isinstance(entry, dict) else None
    if tmux is None or not isinstance(pane, str) or not pane:
        return None, None
    socket = cs.tmux_socket(entry["pid"])
    target = pane.rsplit(".", 1)[-1] if "." in pane else pane  # its pane id, e.g. %3
    return [tmux, *(["-S", socket] if socket else [])], target


def tmux_run(base, args, data=None):
    try:
        r = subprocess.run(base + args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"tmux didn't answer: {exc}"
    if r.returncode != 0:
        return False, "tmux: " + (r.stderr.decode(errors="replace").strip() or f"exit {r.returncode}")
    return True, r.stdout.decode(errors="replace")


def tmux_type(entry, text):
    """Paste text into a chat's tmux pane and send it (Enter): (ok, message)."""
    base, target = tmux_base(entry)
    if base is None:
        return False, "It doesn't run in tmux (or tmux isn't installed)"
    for args, data in ((["load-buffer", "-b", "talpy-den", "-"], text.encode()),
                       (["paste-buffer", "-p", "-d", "-b", "talpy-den", "-t", target], None),
                       (["send-keys", "-t", target, "Enter"], None)):
        if args[0] == "send-keys":
            time.sleep(0.3)  # (Claude takes the paste in first)
        ok, out = tmux_run(base, args, data)
        if not ok:
            return False, out
    return True, ""


DIALOG_OPTION = re.compile(r"^\s*(?:❯\s*)?(\d)\.\s+(.*)$")


def dialog_options(screen):
    """{number: text} of the permission dialog on a chat's screen, if it shows one."""
    if "Do you want to" not in screen:
        return {}
    options = {}
    for line in screen.splitlines():
        m = DIALOG_OPTION.match(line)
        if m:
            options[m.group(1)] = m.group(2).strip()
    return options


def answer_with_keys(entry, decision, note=""):
    """Answer the permission dialog of a chat in tmux by pressing its option's
    number (Deny with a note: No, then the note as your next message)."""
    if decision not in DECISIONS:
        return False, f"Not an answer: {decision}"
    if decision == "answer":
        return False, "Answer its questions in the chat"
    base, target = tmux_base(entry)
    if base is None:
        return False, "It doesn't run in tmux: answer it in the chat"
    ok, screen = tmux_run(base, ["capture-pane", "-p", "-t", target])
    if not ok:
        return False, screen
    options = dialog_options(screen)
    if not options:
        return False, "Its screen shows no permission question now"
    if decision == "allow":
        key = next((n for n, t in sorted(options.items()) if t.startswith("Yes")), None)
    elif decision == "always":
        key = next((n for n, t in sorted(options.items()) if t.startswith("Yes, and don")), None)
    else:
        key = next((n for n, t in sorted(options.items()) if t == "No" or t.startswith("No,") or t.startswith("No ")), None)
    if key is None:
        return False, "That answer isn't one of the choices it shows"
    ok, out = tmux_run(base, ["send-keys", "-t", target, key])
    if not ok:
        return False, out
    if decision == "deny" and (note or "").strip():
        time.sleep(0.6)  # ("What should Claude do instead?")
        ok, out = tmux_type(entry, note.strip())
        if not ok:
            return False, "Denied, but the note didn't go in: " + out
        return True, "Denied, and your note went to the chat"
    return True, {"allow": "Allowed", "always": "Allowed, and from now on", "deny": "Denied"}[decision]


# ---------------------------------------------------------------- sending a message

HOWS = ("auto", "background", "copy")


def vscode_prompt_link(session_id, text):
    from urllib.parse import quote
    return f"vscode://anthropic.claude-code/open?session={quote(session_id)}&prompt={quote(text)}"


def go_on_in_background(home, extra_dirs, profile, chat_id, text, claude):
    answer = cs.continue_chat(home, extra_dirs, profile.key, chat_id, text=text, claude=claude, force=True)
    if answer.get("ok"):
        answer["message"] = "Sent: the chat goes on with it in the background" + (
            f" ({answer['id']})" if answer.get("id") else "")
    return answer


def send(home, extra_dirs, profile_key, chat_id, text, session_id=None, how="auto", claude=None):
    """Send your message to a chat the way it takes one (cs.reply_route):
    {"ok", "message", "how": "typed"|"background"|"vscode"|"copy"|"ask",
    "link" (vscode), "copied" (copy)}. how "ask": the chat runs in a
    terminal that takes no text from outside, and you choose (how
    background or copy)."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "message": "Write your message first"}
    if how not in HOWS:
        return {"ok": False, "message": f"Not a way to send: {how}"}
    if not cs.valid_id(chat_id):
        return {"ok": False, "message": f"Not a chat: {chat_id}"}
    profiles = cs.discover_profiles(home, extra_dirs)
    profile, path = find_transcript(home, extra_dirs, chat_id, profile_key)
    if path is None:
        return {"ok": False, "message": "That chat's transcript isn't there any more"}
    if how == "copy":
        copied = copy_to_clipboard(text)
        return {"ok": True, "how": "copy", "copied": copied, "text": "" if copied else text,
                "message": "Your message is in the clipboard: paste it into the chat" if copied
                else "Couldn't use the clipboard here"}
    entry = None
    for sid in (session_id, chat_id):
        if cs.valid_id(sid or ""):
            entry = cs.find_chat_entry(home, extra_dirs, sid)
            if entry is not None:
                break
    row = {"pid": entry.get("pid") if entry else None, "kind": entry.get("kind") if entry else None,
           "entrypoint": entry.get("entrypoint") if entry else "", "tmux": bool(entry and entry.get("tmux"))}
    route = cs.reply_route(row)
    state = cs.session_state(entry) if entry else "done"
    if route == "tmux":
        if state == "waiting":
            return {"ok": False, "message": "It's asking you something: answer that first"}
        ok, message = tmux_type(entry, text)
        return {"ok": ok, "how": "typed", "message": "Sent (typed into its tmux pane)" if ok else message}
    if route == "vscode":
        return {"ok": True, "how": "vscode", "link": vscode_prompt_link(entry["sessionId"], text),
                "message": "Your message is in the chat's input in VS Code: press Enter there"}
    if route == "terminal" and how != "background":
        return {"ok": True, "how": "ask", "message": "Its terminal takes no text from outside: send it, and the chat "
                "goes on in the background, or copy it and open its tab"}
    claude = claude or cs.find_claude(home, ask_shell=True)
    if claude is None:
        return {"ok": False, "message": "No claude command found"}
    if entry is not None and entry.get("kind") == "bg":
        job_id = entry.get("jobId")
        if not cs.valid_id(job_id or ""):
            return {"ok": False, "message": "It runs in a background session the widget can't stop"}
        runs_on = next((p for p in profiles if cs.matches_profile(p, profile_key)), profile)
        ok, message = cs.stop_background(runs_on, job_id, cs.read_job([runs_on], job_id), home, claude)
        if not ok:
            return {"ok": False, "message": message}
    elif entry is not None:
        if state != "idle":
            return {"ok": False, "message": "It's at work: send it when this turn ends"}
        ok, message = cs.stop_chat(entry)
        if not ok:
            return {"ok": False, "message": message}
    answer = go_on_in_background(home, extra_dirs, profile, chat_id, text, claude)
    answer["how"] = "background"
    return answer


def send_later(home, extra_dirs, profile_key, chat_id, text, session_id=None, how="auto", host="", name=""):
    """Queue a message for when the chat's turn ends (the widget sends it then: jump.py --start-queued)."""
    text = (text or "").strip()
    if not text:
        return False, "Write your message first"
    if how not in HOWS:
        return False, f"Not a way to send: {how}"
    entry = cs.queue_entry(profile_key, "", text, int(time.time() * 1000), host, chat=chat_id, name=name)
    entry.update(afterTurn=True, how=how, session=session_id if cs.valid_id(session_id or "") else None)
    cache_root = cs.cache_dir(home)
    for old in cs.load_queue(cache_root):  # (one message waits per chat: the new one replaces it)
        if old.get("afterTurn") and old.get("chat") == chat_id and (old.get("host") or "") == (host or ""):
            cs.change_queue(cache_root, take=old["id"])
    cs.change_queue(cache_root, add=entry)
    return True, "It's sent when this turn ends"


def copy_to_clipboard(text):
    """Put text on the clipboard (wl-copy on Wayland, else xclip or xsel): whether it worked."""
    tools = ([["wl-copy"]] if os.environ.get("WAYLAND_DISPLAY") else []) \
        + [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    for args in tools:
        exe = shutil.which(args[0])
        if exe is None:
            continue
        try:  # (they stay on to hand it out: not with our output, which the widget waits for)
            r = subprocess.run([exe, *args[1:]], input=text.encode(), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            return True
    return False


# ---------------------------------------------------------------- review: what a chat changed

def read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def git(folder, *args, data=None, timeout=20):
    """(ok, stdout, stderr) of git in folder."""
    if not shutil.which("git"):
        return False, "", "git isn't installed"
    try:
        r = subprocess.run(["git", "-C", folder, *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, "", str(exc)
    return r.returncode == 0, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def git_root(folder):
    ok, out, _ = git(folder, "rev-parse", "--show-toplevel", timeout=5) if os.path.isdir(folder) else (False, "", "")
    return out.strip() if ok and out.strip() else None


def git_original(path, missing=b""):
    """A file's content as of git's HEAD: missing (b"": new since) for a
    file git doesn't have there, None outside a git repository."""
    root = git_root(os.path.dirname(path))
    if root is None:
        return None
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    try:
        r = subprocess.run(["git", "-C", root, "show", f"HEAD:{rel}"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else missing


def backup_content(profiles, sessions, name):
    """A file-history backup's content, from the folder of a session that wrote the chat."""
    for p in profiles:
        for sid in sessions:
            data = read_bytes(p.path / "file-history" / sid / name)
            if data is not None:
                return data
    return None


def tool_originals(transcripts, wanted):
    """{file: its content before the chat's first Edit or Write of it (b""
    for one a Write made)} for the files in wanted, from what those calls
    gave back (toolUseResult: type "create", else originalFile), oldest
    first. A first call that gave neither leaves the file out."""
    found, seen = {}, set()
    for t in transcripts:
        try:
            f = open(t, "rb")
        except OSError:
            continue
        with f:
            for line in f:
                if b'"toolUseResult"' not in line or b'"filePath"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                r = e.get("toolUseResult") if isinstance(e, dict) else None
                if not isinstance(r, dict) or not isinstance(r.get("filePath"), str) or not r["filePath"].startswith("/"):
                    continue
                full = os.path.normpath(r["filePath"])
                if full not in wanted or full in seen:
                    continue
                seen.add(full)
                if r.get("type") == "create":
                    found[full] = b""
                elif isinstance(r.get("originalFile"), str):
                    found[full] = r["originalFile"].encode("utf-8")
    return found


def originals(profiles, path, cwd):
    """[(file, its content before the chat (b"" if it didn't exist; None if
    unknown), created)] of each file the chat changed (outside Claude's own
    folders), from Claude's backups, else what its Edit or Write calls gave
    back (a file outside the chat's folder has no backups), else git's last
    commit."""
    rec = cs.chat_edits(path)
    skip = cs.own_folders(profiles)
    unbacked = {cs.absolute_path(key, cwd) for key, first in rec["files"].items() if first[0] is False}
    told = tool_originals([path, *cs.subagent_transcripts(path)], unbacked) if unbacked else {}
    out = {}
    for key, first in rec["files"].items():
        full = cs.absolute_path(key, cwd)
        if not cs.project_file(full, skip) or full in out:
            continue
        backup = first[0]
        if backup is None:
            original = b""  # it didn't exist yet
        elif backup is False:  # no backup on record: what its first call gave back, else git's last commit
            original = told[full] if full in told else git_original(full)
        else:
            original = backup_content(profiles, rec["sessions"], backup)
            if original is None:  # Claude removed the backup since
                original = git_original(full, missing=None)
        out[full] = (original, backup is None or (original == b"" and backup is False))
    return [(f, o, c) for f, (o, c) in sorted(out.items())]


def is_text(data):
    return data is not None and b"\0" not in data[:8192]


def text_lines(data):
    """A text file's first lines (as many as a diff shows), [] for a binary one."""
    if not is_text(data):
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    shown, size = [], 0
    for l in lines:
        if len(shown) >= cs.DIFF_LINES or size > cs.DIFF_CHARS:
            shown.append(f"… ({len(lines) - len(shown)} more lines)")
            break
        shown.append(l[:500])
        size += len(shown[-1])
    return shown


def file_diff(original, current, label):
    """(added, removed, diff lines) between two versions of a file."""
    if not (is_text(original or b"") and is_text(current or b"")):
        return None, None, []
    old = (original or b"").decode("utf-8", "replace").splitlines()
    new = (current or b"").decode("utf-8", "replace").splitlines()
    diff = list(difflib.unified_diff(old, new, f"a/{label}", f"b/{label}", lineterm="", n=3))
    added = sum(1 for l in diff[2:] if l.startswith("+"))
    removed = sum(1 for l in diff[2:] if l.startswith("-"))
    shown, size = [], 0
    for l in diff[2:]:
        if len(shown) >= cs.DIFF_LINES or size > cs.DIFF_CHARS:
            shown.append(f"… ({len(diff) - 2 - len(shown)} more lines)")
            break
        shown.append(l[:500])
        size += len(shown[-1])
    return added, removed, shown


def commit_message(name, recap, reply):
    """A commit message from the chat's name and Claude's recap of it (else its last reply's first lines)."""
    title = cs.short_text(name or "Changes from a Claude chat", 72)
    body = (recap or {}).get("text") if isinstance(recap, dict) else ""
    if not body and reply:
        body = "\n".join(reply.strip().split("\n\n")[:2])
    body = cs.tidy_text(body or "", 1500)
    return title + ("\n\n" + body if body else "")


def chat_review(home, extra_dirs, chat_id, profile_key=None, reviewed_at=None):
    """{"ok", "files": [{"label", "path", "added", "removed", "diff",
    "created", "deleted", "binary", "unknown", "now" (with no earlier
    version kept: its first lines as they are now)}], "tests", "editedAt",
    "message" (a commit message), "repos" (git roots of the files), "cwd",
    "name"} of what a chat changed, against the files before the chat."""
    if not cs.valid_id(chat_id):
        return {"ok": False, "message": f"Not a chat: {chat_id}"}
    profiles = cs.discover_profiles(home, extra_dirs)
    profile, path = find_transcript(home, extra_dirs, chat_id, profile_key)
    if path is None:
        return {"ok": False, "message": "That chat's transcript isn't there any more"}
    name, cwd, _ = cs.read_chat_info(path, path.stat().st_size)
    cwd = cwd or cs.chat_folder(path) or ""
    files, repos = [], set()
    for full, original, created in originals(profiles, path, cwd):
        current = read_bytes(full)
        if original is not None and (current == original or (current is None and original == b"")):
            continue  # unchanged (or made and removed again)
        label = cs.file_label(full, cwd, home)
        item = {"label": label, "path": full, "created": created, "deleted": current is None,
                "unknown": original is None, "binary": False, "added": None, "removed": None, "diff": []}
        if original is not None:
            added, removed, diff = file_diff(original, current, label)
            item.update(added=added, removed=removed, diff=diff, binary=added is None)
        elif current is not None:  # (no earlier version: how it is now)
            item.update(now=text_lines(current), binary=not is_text(current))
        files.append(item)
        root = git_root(os.path.dirname(full) if os.path.isdir(os.path.dirname(full)) else cwd)
        if root:
            repos.add(root)
    tests = cs.chat_tests(path)
    peek = cs.chat_peek(path) or {}
    return {"ok": True, "chatId": chat_id, "profileDir": profile.key, "name": name, "cwd": cwd, "files": files,
            "tests": tests["run"], "editedAt": tests["editAt"], "repos": sorted(repos),
            "message": commit_message(name, peek.get("recap"), peek.get("reply")),
            "reviewedAt": reviewed_at}


def chosen(files, wanted):
    return [f for f in files if not wanted or f["path"] in wanted]


def undo(home, extra_dirs, chat_id, profile_key=None, wanted=()):
    """Put the files a chat changed (all, or those in wanted) back as they
    were before it: (ok, message). A file it made is removed, one it
    removed comes back; one whose earlier version isn't known stays."""
    profiles = cs.discover_profiles(home, extra_dirs)
    profile, path = find_transcript(home, extra_dirs, chat_id, profile_key)
    if path is None:
        return False, "That chat's transcript isn't there any more"
    for entry, _, _ in cs.live_entries_all(profiles):
        if entry.get("sessionId") == chat_id and cs.session_state(entry) == "working":
            return False, "It's at work: undo once its turn has ended"
    _, cwd, _ = cs.read_chat_info(path, path.stat().st_size)
    cwd = cwd or cs.chat_folder(path) or ""
    done, unknown, failed = 0, 0, []
    for full, original, created in originals(profiles, path, cwd):
        if wanted and full not in wanted:
            continue
        if original is None:
            unknown += 1
            continue
        current = read_bytes(full)
        try:
            if original == b"" and created:
                if current is not None:
                    os.unlink(full)
                    done += 1
            elif current != original:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                tmp = full + ".talpy-den-undo"
                with open(tmp, "wb") as f:
                    f.write(original)
                if current is not None:
                    shutil.copymode(full, tmp)
                os.replace(tmp, full)
                done += 1
        except OSError as exc:
            failed.append(f"{cs.file_label(full, cwd, home)}: {exc.strerror or exc}")
    if failed:
        return False, "Couldn't put back " + "; ".join(failed[:3])
    message = f"Put back {done} file{'s' if done != 1 else ''} as before the chat" if done else "Nothing to put back"
    if unknown:
        message += f" ({unknown} with no earlier version kept)"
    return True, message


def commit(home, extra_dirs, chat_id, message, profile_key=None, wanted=()):
    """Commit only the files a chat changed (or those in wanted), in each
    git repository they're in, with message: (ok, what happened)."""
    message = (message or "").strip()
    if not message:
        return False, "Write a commit message first"
    review = chat_review(home, extra_dirs, chat_id, profile_key)
    if not review.get("ok"):
        return False, review.get("message") or "Can't read the chat"
    by_repo = {}
    for f in chosen(review["files"], set(wanted)):
        folder = os.path.dirname(f["path"])
        while folder and not os.path.isdir(folder):
            folder = os.path.dirname(folder)
        root = git_root(folder) if folder else None
        if root:
            by_repo.setdefault(root, []).append(os.path.relpath(f["path"], root))
    if not by_repo:
        return False, "None of its files is in a git repository"
    done = []
    for root, paths in sorted(by_repo.items()):
        ok, _, err = git(root, "add", "-A", "--", *paths)
        if not ok:
            return False, f"git add in {cs.short_path(root, home)}: {last_line(err)}"
        ok, out, err = git(root, "commit", "-q", "-F", "-", "--only", "--", *paths, data=message.encode(), timeout=120)
        if not ok:
            return False, f"git commit in {cs.short_path(root, home)}: {last_line(err or out)}"
        ok, head, _ = git(root, "rev-parse", "--short", "HEAD")
        done.append(f"{cs.short_path(root, home)} ({head.strip()})" if ok else cs.short_path(root, home))
    n = sum(len(p) for p in by_repo.values())
    return True, f"Committed {n} file{'s' if n != 1 else ''} in " + ", ".join(done)


def last_line(text):
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return lines[-1] if lines else "failed"


# ---------------------------------------------------------------- marked reviewed

def mark_reviewed(cache_root, chat_id, host="", on=True, now_ms=None):
    """Mark a chat (of host) reviewed now, or not reviewed (0: also if it
    changed its files before cs.reviewed_since)."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    cs.private_dir(cache_root)
    with open(cache_root / "reviewed.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        since = cs.reviewed_since(cache_root, now_ms)
        chats = {k: v for k, v in cs.load_reviewed(cache_root).items() if v == 0 or now_ms - v < cs.REVIEWED_KEEP_MS}
        chats[cs.review_key(chat_id, host)] = now_ms if on else 0
        cs.write_json_atomic(cache_root / cs.REVIEWED, {"since": since, "chats": chats})


# ---------------------------------------------------------------- the hooks in Claude's settings
#
# Two hooks the widget can put in each Claude profile's settings.json, and
# take out again: the permission hook (--relay: permission prompts and
# Claude's questions answered from the widget) and the live hook (--live:
# each step of each chat as it happens). Each runs from a copy in
# ~/.local/share/talpy-den, named in the hook list of each of its events.

HOOK_FILE = "permission-hook.py"
HOOK_TIMEOUT_S = 86400
# the events the live hook folds (live_hook.py); Claude doesn't wait for it (async)
LIVE_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
               "PermissionRequest", "Notification", "Stop", "StopFailure", "SubagentStart", "SubagentStop",
               "PreCompact", "SessionEnd")
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest")
HOOKS = {
    "relay": {"file": HOOK_FILE, "source": "permission_hook.py", "events": ("PermissionRequest",),
              "python": "-I", "entry": {"timeout": HOOK_TIMEOUT_S}},
    "live": {"file": cs.LIVE_HOOK_FILE, "source": "live_hook.py", "events": LIVE_EVENTS,
             "python": "-I -S", "entry": {"async": True, "timeout": 10}},
}


def hook_command(path, kind="relay"):
    return f"python3 {HOOKS[kind]['python']} {path}"


def settings_files(home, extra_dirs):
    """Each Claude profile's settings.json, once per real file (also of a
    ~/.claude that Claude has had no chat in yet)."""
    seen, out = set(), []
    folders = [p.path for p in cs.discover_profiles(home, extra_dirs)]
    if (home / ".claude").is_dir():
        folders.append(home / ".claude")
    for folder in folders:
        path = folder / "settings.json"
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            out.append(Path(real))
    return out


def is_ours(hook, kind="relay"):
    command = str(hook.get("command") or "") if isinstance(hook, dict) else ""
    # (also the copy an earlier version put in its first folder, claude-widget)
    return HOOKS[kind]["file"] in command and ("talpy-den" in command or "claude-widget" in command)


def has_hook(settings, kind="relay"):
    """Whether settings name the hook, in each of its events."""
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return False
    return all(any(is_ours(h, kind) for g in hooks.get(event) or [] if isinstance(g, dict) for h in g.get("hooks") or [])
               for event in HOOKS[kind]["events"])


def set_hook(settings, command, on, kind="relay"):
    """settings with the widget's hook in each of its events (on) or out of them."""
    hooks = settings.setdefault("hooks", {}) if on else settings.get("hooks") or {}
    for event in HOOKS[kind]["events"]:
        kept = []
        for g in hooks.get(event) or []:
            if not isinstance(g, dict):
                continue
            rest = [h for h in g.get("hooks") or [] if not is_ours(h, kind)]
            if rest:
                kept.append(dict(g, hooks=rest))
        if on:
            entry = dict({"type": "command", "command": command}, **HOOKS[kind]["entry"])
            kept.append(dict({"matcher": "*"} if event in TOOL_EVENTS else {}, hooks=[entry]))
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks and "hooks" in settings:
        settings.pop("hooks")
    return settings


def hook_path(home, kind="relay"):
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "talpy-den" / HOOKS[kind]["file"]


def install_hook(home, extra_dirs, on, kind, source=None):
    """Put the hook in each profile's settings (on) or take it out: how many
    settings files changed (ValueError: one isn't JSON). source: the hook's
    code (else its file next to this one)."""
    target = hook_path(home, kind)
    if on:
        if source is None:
            source = (Path(__file__).resolve().parent / HOOKS[kind]["source"]).read_text(encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        if read_bytes(target) != source.encode():
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(source, encoding="utf-8")
            os.replace(tmp, target)
    changed = 0
    for path in settings_files(home, extra_dirs):
        data = cs.read_json(path)
        if data is None and not path.exists():
            if not on:
                continue
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"{cs.short_path(str(path), home)} isn't JSON the widget can change")
        before = json.dumps(data, sort_keys=True)
        set_hook(data, hook_command(target, kind), on, kind)
        if json.dumps(data, sort_keys=True) == before:
            continue  # (as it was: the file isn't touched)
        tmp = path.with_name(path.name + ".talpy-den.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
        changed += 1
    if not on:
        try:
            target.unlink()
        except OSError:
            pass
    return changed


def relay(home, extra_dirs, on, source=None):
    """Put the permission hook in each profile's settings (on) or take it
    out: (ok, message). source: the hook's code (else permission_hook.py
    next to this file)."""
    try:
        changed = install_hook(home, extra_dirs, on, "relay", source)
    except ValueError as exc:
        return False, str(exc)
    if on:
        return True, "Permission prompts can be answered from the widget" + ("" if changed else " (they could already)")
    return True, "Permission prompts are answered in the chats only"


def live(home, extra_dirs, on, source=None):
    """Live mode: the live hook in each profile's settings (on) or out of
    them: (ok, message)."""
    try:
        changed = install_hook(home, extra_dirs, on, "live", source)
    except ValueError as exc:
        return False, str(exc)
    if on:
        return True, "Chats show each step as it happens" + ("" if changed else " (they did already)")
    return True, "Chats show their steps at each check"


def relay_status(home, extra_dirs, kind="relay"):
    files = settings_files(home, extra_dirs)
    on = [has_hook(cs.read_json(p) or {}, kind) for p in files]
    return {"ok": True, "on": bool(on) and all(on), "some": any(on), "settings": [str(p) for p in files]}


# ---------------------------------------------------------------- command line

def forward(host, args):
    """Run this file on an SSH host with args: its answer."""
    import remote
    return remote.host_answer(host, args, name="pane.py")


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    rest, host, i = [], "", 0  # rest: the arguments as a host gets them (no --host, --remote, --pretty)
    while i < len(args):
        if args[i] == "--host" and i + 1 < len(args):
            host, i = args[i + 1], i + 2
            continue
        if args[i] not in ("--remote", "--pretty"):
            rest.append(args[i])
        i += 1
    action, target = None, None
    profile, how, note, decision, message, name = None, "auto", "", None, "", ""
    session, text, stamp, limit, files, config_dirs, answers = None, None, None, PANE_ITEMS, [], [], None
    after, off, remote, pretty, source = False, False, False, False, None
    actions = ("--conversation", "--send", "--answer", "--keys", "--review", "--undo", "--commit", "--reviewed",
               "--relay", "--live", "--oldest", "--shortcuts", "--complete", "--logs")
    osd = False
    while args:
        arg = args.pop(0)
        if arg in actions and args and action is None:
            action, target = arg, args.pop(0)
        elif arg == "--host" and args:
            args.pop(0)
        elif arg == "--profile" and args:
            profile = args.pop(0)
        elif arg == "--session" and args:
            session = args.pop(0)
        elif arg == "--text" and args:
            text = args.pop(0)
        elif arg == "--how" and args:
            how = args.pop(0)
        elif arg == "--note" and args:
            note = args.pop(0)
        elif arg == "--decision" and args:
            decision = args.pop(0)
        elif arg == "--answers" and args:
            try:
                answers = json.loads(args.pop(0))
            except ValueError:
                answers = None
        elif arg == "--message" and args:
            message = args.pop(0)
        elif arg == "--name" and args:
            name = args.pop(0)
        elif arg == "--file" and args:
            files.append(args.pop(0))
        elif arg == "--if-changed" and args:
            stamp = args.pop(0)
        elif arg == "--limit" and args and args[0].isdigit():
            limit = int(args.pop(0))
        elif arg == "--config-dir" and args:
            config_dirs.append(args.pop(0))
        elif arg == "--hook-source" and args:
            source = base64.b64decode(args.pop(0)).decode("utf-8")
        elif arg == "--after-turn":
            after = True
        elif arg == "--osd":
            osd = True
        elif arg == "--off":
            off = True
        elif arg == "--remote":
            remote = True
        elif arg == "--pretty":
            pretty = True
        else:
            sys.exit(__doc__)
    if action is None:
        sys.exit(__doc__)
    os.umask(0o077)
    home = Path.home()
    cs.adopt_legacy_dirs(home)
    cache_root = cs.cache_dir(home)

    def emit(result):
        out = json.dumps(result, indent=2 if pretty and not remote else None)
        sys.stdout.write(("\n" + cs.REMOTE_MARK + " " if remote else "") + out + "\n")

    try:
        if host and action == "--reviewed":  # (kept here, as stars are)
            mark_reviewed(cache_root, target, host, on=not off)
            return emit({"ok": True, "message": "Marked reviewed" if not off else "Not marked reviewed"})
        if host and action == "--send" and how == "copy":  # (the clipboard is this computer's)
            copied = copy_to_clipboard((text or "").strip())
            return emit({"ok": True, "how": "copy", "copied": copied, "text": "" if copied else text,
                         "message": "Your message is in the clipboard: paste it into the chat" if copied
                         else "Couldn't use the clipboard here"})
        if host and action == "--send" and after:  # (queued here: the widget here sends it then)
            ok, said = send_later(home, config_dirs, profile, target, text, session, how, host, name)
            return emit({"ok": ok, "message": said})
        if host:
            if action in ("--relay", "--live") and target == "on" and source is None:
                path = Path(__file__).resolve().parent / HOOKS[action[2:]]["source"]
                rest += ["--hook-source", base64.b64encode(path.read_bytes()).decode()]
            answer = forward(host, rest)
            if action == "--review" and answer.get("ok"):
                answer["reviewedAt"] = cs.load_reviewed(cache_root).get(cs.review_key(target, host))
            return emit(answer)
        if action == "--conversation":
            result = chat_conversation(home, config_dirs, target, profile, limit, stamp)
        elif action == "--send":
            if after:
                ok, said = send_later(home, config_dirs, profile, target, text, session, how, "", name)
                result = {"ok": ok, "message": said}
            else:
                result = send(home, config_dirs, profile, target, text, session, how)
        elif action == "--answer":
            ok, said = answer_request(cache_root, target, decision, note, answers)
            result = {"ok": ok, "message": said}
        elif action == "--logs":
            result = job_screen(home, config_dirs, target, profile)
        elif action == "--complete":
            result = completions(home, config_dirs, target, profile)
        elif action == "--oldest":
            ok, said = answer_oldest(cache_root, target)
            if osd:
                show_note(said, ok)
            result = {"ok": ok, "message": said}
        elif action == "--shortcuts":
            ok, said = shortcuts(home, {"on": True, "off": False}.get(target))
            result = {"ok": ok, "message": said} if target != "status" else {"ok": ok, "on": said == "on"}
        elif action == "--keys":
            entry = cs.find_chat_entry(home, config_dirs, target, profile)
            ok, said = answer_with_keys(entry, decision, note) if entry else (False, "That chat isn't running any more")
            result = {"ok": ok, "message": said}
        elif action == "--review":
            result = chat_review(home, config_dirs, target, profile,
                                 cs.load_reviewed(cache_root).get(cs.review_key(target)) if not remote else None)
        elif action == "--undo":
            ok, said = undo(home, config_dirs, target, profile, set(files))
            result = {"ok": ok, "message": said}
        elif action == "--commit":
            ok, said = commit(home, config_dirs, target, message, profile, set(files))
            result = {"ok": ok, "message": said}
        elif action == "--reviewed":
            mark_reviewed(cache_root, target, "", on=not off)
            result = {"ok": True, "message": "Marked reviewed" if not off else "Not marked reviewed"}
        elif action in ("--relay", "--live"):
            kind = action[2:]
            if target == "status":
                result = relay_status(home, config_dirs, kind)
            elif target in ("on", "off"):
                ok, said = (relay if kind == "relay" else live)(home, config_dirs, target == "on", source)
                result = {"ok": ok, "message": said}
            else:
                sys.exit(__doc__)
        else:
            sys.exit(__doc__)
    except Exception as exc:  # the widget must always get parseable output
        result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    emit(result)


if __name__ == "__main__":
    main()
