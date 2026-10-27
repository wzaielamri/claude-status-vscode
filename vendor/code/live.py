#!/usr/bin/env python3
"""What changed in your chats, the moment it changes: the widget's watcher.

    python3 -I live.py --wait [--as NAME] [--timeout S] [--config-dir DIR]...

Waits until a chat changes, prints what changed and exits (the widget runs
it again at once). It watches two things:
  - each Claude profile's session files (<config dir>/sessions/<pid>.json),
    which Claude writes whenever a chat starts or ends, starts working,
    waits for you or ends its turn (also when you answer or interrupt it in
    its terminal);
  - the live files of the live hook (live_hook.py, ~/.cache/talpy-den/
    live/<session id>.json), with what each chat does now, step by step.

    {"ok": true, "at": ms, "sessions": bool, "urgent": bool, "live": [...],
     "timeout": bool}

sessions: a session file changed (the status is worth checking now);
urgent: that, or a live file says a turn started or ended, a chat asks for
something, or a session or subagent started or ended; live: the live files
that changed. A chat's steps alone (tool calls) come at most every
MIN_GAP_S, so a busy chat doesn't wake the widget all the time. After S
seconds (TIMEOUT_S) with nothing new it answers anyway (timeout true).

NAME: who asks (the Plasma widget's copies in one plasmashell are one
NAME). What it was told last is kept in the live folder, so what changes
between two runs isn't missed, and the command line stays the same from
run to run (Plasma keeps every command's name; hub/Exec.qml).

Linux (inotify); elsewhere it looks every half second. Standard library
only, Python 3.7+.
"""
import ctypes
import hashlib
import json
import os
import select
import sys
import time

TIMEOUT_S = 50
MIN_GAP_S = 0.4  # between two answers about steps alone
GATHER_S = 0.05  # after a change: what comes with it (a session file and a live file)
FRESH_S = 86400  # live files the first answer sends (younger than this)
SEEN_KEEP_S = 7 * 86400
# what a live file's last event must be to make an answer urgent (live_hook.py)
URGENT_EVENTS = {"UserPromptSubmit", "Stop", "StopFailure", "PermissionRequest", "Notification", "SessionStart",
                 "SessionEnd", "PreCompact", "SubagentStart", "SubagentStop"}
# inotify: IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO | IN_DELETE
WATCH_MASK = 0x8 | 0x40 | 0x80 | 0x200


def cache_root(home):
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache"), "talpy-den")


def live_dir(home):
    return os.path.join(cache_root(home), "live")


def session_dirs(home, extra=()):
    """Each Claude profile's sessions folder (~/.claude, ~/.claude-*, and
    the extra config dirs), once per real folder."""
    try:
        names = sorted(n for n in os.listdir(home) if n == ".claude" or n.startswith(".claude-"))
    except OSError:
        names = []
    seen, out = set(), []
    for folder in [os.path.join(home, n) for n in names] + [os.path.expanduser(d) for d in extra]:
        real = os.path.realpath(os.path.join(folder, "sessions"))
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            out.append(real)
    return out


def listing(folder):
    """{name: mtime ns} of the .json files in a folder."""
    out = {}
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name.endswith(".json") and not e.name.startswith("."):
                    try:
                        out[e.name] = e.stat().st_mtime_ns
                    except OSError:
                        pass
    except OSError:
        pass
    return out


def snapshot(live, sessions):
    return {"live": listing(live), "sessions": {os.path.join(d, n): t for d in sessions for n, t in listing(d).items()}}


def read_view(path):
    try:
        with open(path, encoding="utf-8") as f:
            view = json.load(f)
    except (OSError, ValueError):
        return None
    return view if isinstance(view, dict) and isinstance(view.get("sessionId"), str) else None


def seen_path(home, name):
    return os.path.join(live_dir(home), ".seen-" + hashlib.sha1(name.encode()).hexdigest()[:12] + ".json")


def load_seen(path):
    try:
        with open(path, encoding="utf-8") as f:
            seen = json.load(f)
    except (OSError, ValueError):
        return None
    ok = isinstance(seen, dict) and isinstance(seen.get("live"), dict) and isinstance(seen.get("sessions"), dict)
    return seen if ok else None


def save_seen(path, snap, at):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(snap, at=at), f, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass


def tidy_seen(home, now):
    """What other NAMEs were told goes after a week without them."""
    folder = live_dir(home)
    try:
        names = [n for n in os.listdir(folder) if n.startswith(".seen-")]
    except OSError:
        return
    for name in names:
        try:
            if now - os.stat(os.path.join(folder, name)).st_mtime > SEEN_KEEP_S:
                os.unlink(os.path.join(folder, name))
        except OSError:
            pass


def changes(seen, snap, live, now):
    """(sessions changed, [live views changed]) since what was told (seen;
    None: nothing was told yet)."""
    if seen is None:
        fresh = [n for n, t in snap["live"].items() if now - t / 1e9 < FRESH_S]
        return True, [v for v in (read_view(os.path.join(live, n)) for n in sorted(fresh)) if v]
    told = seen["live"]
    names = [n for n, t in snap["live"].items() if told.get(n) != t]
    views = [v for v in (read_view(os.path.join(live, n)) for n in sorted(names)) if v]
    return seen["sessions"] != snap["sessions"], views


class Watch:
    """inotify on the folders (None: not on this system: look every half second)."""

    def __init__(self, folders):
        self.fd = None
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            fd = libc.inotify_init1(0o4000 | 0o2000000)  # IN_NONBLOCK | IN_CLOEXEC
        except (OSError, AttributeError):
            return
        if fd < 0:
            return
        added = [libc.inotify_add_watch(fd, f.encode(), WATCH_MASK) for f in folders]
        if not any(w >= 0 for w in added):
            os.close(fd)
            return
        self.fd = fd

    def wait(self, seconds):
        """Wait up to seconds for a change: whether one came."""
        if self.fd is None:
            time.sleep(min(seconds, 0.5))
            return True  # (the caller looks again)
        ready, _, _ = select.select([self.fd], [], [], max(0.0, seconds))
        return bool(ready)

    def drain(self):
        if self.fd is None:
            return
        try:
            while os.read(self.fd, 65536):
                pass
        except OSError:
            pass


def wait(home, extra=(), name="default", timeout=TIMEOUT_S, clock=time.time):
    """The answer of --wait (see the top)."""
    live = live_dir(home)
    try:
        os.makedirs(live, mode=0o700, exist_ok=True)
    except OSError:
        pass
    sessions = session_dirs(home, extra)
    watch = Watch([live] + sessions)  # (watching first: nothing between the look and the wait is missed)
    path = seen_path(home, name)
    seen = load_seen(path)
    parent = os.getppid()
    start = clock()
    if seen is None or start - (seen.get("at") or 0) > SEEN_KEEP_S:
        tidy_seen(home, start)
    while True:
        if os.getppid() != parent:
            sys.exit(0)  # (the widget is gone)
        now = clock()
        snap = snapshot(live, sessions)
        moved, views = changes(seen, snap, live, now)
        urgent = moved or any(v.get("event") in URGENT_EVENTS for v in views)
        if views and not urgent and seen is not None and now - (seen.get("at") or 0) < MIN_GAP_S:
            # (steps alone, just after the last answer: a moment later, with what comes meanwhile)
            if watch.wait(MIN_GAP_S - (now - seen["at"])):
                time.sleep(GATHER_S)
                watch.drain()
            continue
        if moved or views:
            save_seen(path, snap, now)
            return {"ok": True, "at": int(now * 1000), "sessions": moved, "urgent": urgent, "live": views,
                    "timeout": False}
        left = timeout - (now - start)
        if left <= 0:
            return {"ok": True, "at": int(now * 1000), "sessions": False, "urgent": False, "live": [], "timeout": True}
        if watch.wait(min(left, 1.0)):
            time.sleep(GATHER_S)
            watch.drain()


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    name, timeout, extra, waiting = "default", TIMEOUT_S, [], False
    while args:
        arg = args.pop(0)
        if arg == "--wait":
            waiting = True
        elif arg == "--as" and args:
            name = args.pop(0)
        elif arg == "--timeout" and args:
            try:
                timeout = max(1.0, float(args.pop(0)))
            except ValueError:
                sys.exit(__doc__)
        elif arg == "--config-dir" and args:
            extra.append(args.pop(0))
        else:
            sys.exit(__doc__)
    if not waiting:
        sys.exit(__doc__)
    os.umask(0o077)
    try:
        answer = wait(os.path.expanduser("~"), extra, name, timeout)
    except Exception as exc:  # the widget must always get parseable output
        answer = {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
    sys.stdout.write(json.dumps(answer, ensure_ascii=True) + "\n")


if __name__ == "__main__":
    main()
