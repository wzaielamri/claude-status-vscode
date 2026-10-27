#!/usr/bin/env python3
"""SSH hosts for the Talpy Den widget.

    python3 -I remote.py --hosts                    hosts named in ~/.ssh/config
    python3 -I remote.py --status HOST [--done-hours H] [--recent N] [--no-tokens]
    python3 -I remote.py --open HOST COMMAND        a terminal running COMMAND on HOST
    python3 -I remote.py --run HOST COMMAND         run COMMAND on HOST (e.g. claude stop)
    python3 -I remote.py --end HOST SESSION_ID [PROFILE] [--busy]   stop a chat in a terminal or VS Code there
    python3 -I remote.py --interrupt HOST SESSION_ID [PROFILE]      interrupt a working chat in tmux there
    python3 -I remote.py --dismiss HOST SESSION_ID  hide a finished session of HOST
    python3 -I remote.py --undismiss-all HOST
    python3 -I remote.py --agent-run HOST CHAT_ID AGENT_ID [PROFILE] [--no-open]   a subagent's run there, as a Markdown file here
    python3 -I remote.py --share HOST CHAT_ID [PROFILE]    share a chat with every account there (from PROFILE's copy)
    python3 -I remote.py --continue HOST CHAT_ID PROFILE [--print-command]   share it with PROFILE there if needed,
                                                    and reopen it there in a terminal (or print the command)
    python3 -I remote.py --unshare HOST CHAT_ID PROFILE    keep a shared chat only on PROFILE there
    python3 -I remote.py --find HOST QUERY          the chats there with every word of QUERY
    python3 -I remote.py --task HOST PROFILE FOLDER [--prompt TEXT] [--plan SLUG PLAN_PROFILE] [--model M] [--effort E]
                         [--permission-mode MODE] [--bg | --print-command]
                                                    a new chat there with a task: in the background there,
                                                    or in a terminal here (or print its command)
    python3 -I remote.py --put-image HOST PATH      a picture pasted here (jump.py --paste-image) to HOST, for a task there
    python3 -I remote.py --plan HOST SLUG [PROFILE] [--no-open]   a plan there, as a Markdown file here
    python3 -I remote.py --share-plan HOST SLUG PROFILE [TO]      share a plan with every account there (or TO)
    python3 -I remote.py --unshare-plan HOST SLUG PROFILE         keep a shared plan only on PROFILE there
    python3 -I remote.py --login HOST               a terminal where you log in to HOST for the widget
    python3 -I remote.py --disconnect HOST          close the connection you logged in with
    python3 -I remote.py --vscode-account HOST CONFIG_DIR|-   VS Code's Claude on HOST runs on that account
    python3 -I remote.py --accounts HOST ACCOUNTS_ARGS... [--print-command]   the host's Claude accounts
                                                    (accounts.py there: --list, --add, --login, --default, …)

--status runs talpy_den.py on HOST: the script goes to `python3 -I -` on
the host's stdin, so nothing is installed there (it keeps its caches in the
host's ~/.cache/talpy-den, like here). Sharing sends sharing.py the same
way, with talpy_den.py in front (host_script), and so do the accounts
(accounts.py: adding one, logging in or out, removing one, the default one).
A login there opens a terminal here, over ssh: Claude's own login, whose
code you paste into it. SSH runs in batch mode, so
it never asks for a password or a host key: log in once in a terminal for
that. One connection per host is kept open for a few minutes (ControlMaster),
so polls are quick even through a jump host. Prints one JSON line.

For a host that wants a password (or a passphrase, a code, a new host key),
--login opens a terminal where ssh itself asks for it: the widget never sees
it. That login's connection becomes the shared one the checks use, until
LOGIN_MINUTES after their last use, logging out (the sockets are in
$XDG_RUNTIME_DIR), or --disconnect.
"""

import glob
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ inside the installed widget
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # -I leaves the script dir off sys.path
import talpy_den as cs  # noqa: E402

HERE = Path(__file__).resolve().parent
HOST_NAME = re.compile(r"^[A-Za-z0-9_.@][A-Za-z0-9_.@:-]*$")
STATUS_TIMEOUT = 45
RUN_TIMEOUT = 60
LOGIN_MINUTES = 30  # a connection you logged in to stays this long after its last use
LOGIN_SHOWN = 3  # seconds the terminal shows "Logged in" before it closes


# ---------------------------------------------------------------- ~/.ssh/config


def config_words(value):
    """The words of a config value as ssh splits them: quotes kept together,
    and a word starting with # begins a comment."""
    try:
        words = shlex.split(value, comments=True)
    except ValueError:
        words = value.split()
    out = []
    for word in words:
        if word.startswith("#"):
            break
        out.append(word)
    return out


def config_lines(path, depth=0, seen=None):
    """(keyword, value) of an ssh config file, with Include files spliced in
    between ("include", "begin") and ("include", "end"): an Include inside a
    Host block doesn't end that block."""
    seen = set() if seen is None else seen
    try:
        real = path.resolve()
        if real in seen or depth > 8:
            return
        seen.add(real)
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([A-Za-z]+)\s*(?:=\s*|\s+)(.*)", line)
        if not m:
            continue
        key, value = m.group(1).lower(), m.group(2).strip()
        if key == "include":
            for pattern in config_words(value):
                pattern = os.path.expanduser(pattern)
                if not os.path.isabs(pattern):
                    pattern = str(Path.home() / ".ssh" / pattern)
                for match in sorted(glob.glob(pattern)):
                    yield "include", "begin"
                    yield from config_lines(Path(match), depth + 1, seen)
                    yield "include", "end"
        else:
            yield key, value


def ssh_hosts(path=None):
    """Hosts with a name of their own in ~/.ssh/config (no wildcard patterns),
    in file order, with what's useful to show next to the name."""
    path = path or Path.home() / ".ssh" / "config"
    hosts, current, outer = {}, [], []
    for key, value in config_lines(path):
        if key == "include":
            if value == "begin":
                outer.append(current)
            elif outer:
                current = outer.pop()
        elif key == "host":
            current = []
            for name in config_words(value):
                if any(c in name for c in "*?!") or not HOST_NAME.match(name):
                    continue
                hosts.setdefault(name, {"name": name, "hostname": "", "user": "", "port": "", "jump": ""})
                current.append(name)
        elif key == "match":
            current = []
        elif current and key in ("hostname", "user", "port", "proxyjump"):
            field = "jump" if key == "proxyjump" else key
            words = config_words(value)
            for name in current:
                if not hosts[name][field] and words:  # ssh uses the first value it sees
                    hosts[name][field] = words[0]
    return list(hosts.values())


# ---------------------------------------------------------------- talking to a host


SOCKET_TMP = "/tmp"
SOCKET_DIR_MAX = 49  # a socket path may have 107 characters; ssh adds 58 (/%C and a temporary suffix)


def control_dir():
    """Folder for ssh's shared connections (one per host), or None to go
    without. Short on purpose: under ~/.cache a long home folder name made
    the socket path too long. $XDG_RUNTIME_DIR (/run/user/<uid>) is short and
    private; else a folder in /tmp that only you own. The VS Code extension
    uses the same folder (sshControl.ts), so both share the connections."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if os.path.isabs(runtime) and os.path.isdir(runtime):
        d = Path(runtime) / "talpy-den-ssh"
    else:
        d = Path(SOCKET_TMP) / f"talpy-den-ssh-{os.getuid()}"
    if len(str(d)) > SOCKET_DIR_MAX:
        return None
    try:
        d.mkdir(mode=0o700, exist_ok=True)
        st = os.lstat(str(d))
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return None  # a link, or someone else's: no sockets there
        if st.st_mode & 0o077:
            os.chmod(str(d), 0o700)
    except OSError:
        return None
    return d


def ssh_options(batch=True):
    """Options for every connection: batch mode (never ask, never hang), keep
    one connection per host open for reuse, no X11/agent forwarding. A host's
    RemoteCommand (e.g. starting tmux) and port forwards from ~/.ssh/config
    are left out: they'd replace the widget's command, or hold your tunnels'
    ports in the shared connection."""
    d = control_dir()
    opts = ["-o", "ControlMaster=auto", "-o", f"ControlPath={d}/%C", "-o", "ControlPersist=300"] if d else []
    opts += ["-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
             "-o", "RemoteCommand=none", "-o", "ClearAllForwardings=yes"]
    if batch:
        opts += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ForwardX11=no", "-o", "ForwardAgent=no"]
    return opts


def posix(command):
    """command for the host's login shell, run by sh: the commands the
    widget builds (`cd … 2>/dev/null; VAR=… claude …`) are POSIX shell, and
    the login shell may be csh or fish."""
    return "sh -c " + shlex.quote(command)


def answer_after_mark(stdout):
    """The JSON answer a script printed after the marker line, or None. The
    answer is one line, and the text inside it (a chat's prompt or reply) may
    mention the marker too: only a marker at the start of a line counts."""
    mark = stdout.rfind("\n" + cs.REMOTE_MARK)
    if mark < 0:
        return None
    try:
        answer = json.loads(stdout[mark + 1 + len(cs.REMOTE_MARK):])
    except ValueError:
        return None
    return answer if isinstance(answer, dict) else None


def ssh(host, command, stdin=None, timeout=RUN_TIMEOUT):
    """Run command on host (batch mode): the CompletedProcess. Output that
    isn't UTF-8 (a login script's greeting) can't break the answer."""
    return subprocess.run(["ssh", *ssh_options(), "-T", host, posix(command)], input=stdin, capture_output=True,
                          text=True, errors="replace", timeout=timeout)


def host_script(name="talpy_den.py"):
    """What runs on a host, on python3's stdin: talpy_den.py, or another
    script of this folder that imports it (sharing.py), after talpy_den.py
    loaded as that module."""
    status = (HERE / "talpy_den.py").read_text(encoding="utf-8")
    if name == "talpy_den.py":
        return status
    return ("import sys, types\n"
            "module = sys.modules['talpy_den'] = types.ModuleType('talpy_den')\n"
            f"exec(compile({status!r}, 'talpy_den.py', 'exec'), module.__dict__)\n"
            + (HERE / name).read_text(encoding="utf-8"))


def check_host(host):
    if not isinstance(host, str) or not HOST_NAME.match(host):
        raise ValueError(f"not a host name: {host!r}")
    return host


def explain(host, code, stderr):
    """A sentence for why talking to host failed."""
    text = stderr.strip()
    last = text.splitlines()[-1] if text else ""
    low = text.lower()
    if "host key verification failed" in low or "no matching host key" in low:
        return f"{host}'s host key isn't known yet. Run ssh {host} once in a terminal to accept it."
    if "permission denied" in low or "too many authentication failures" in low:
        return f"{host} refused the login. The widget can't type a password: load your key into the agent (ssh-add) or run ssh {host} once in a terminal."
    if "could not resolve hostname" in low or "name or service not known" in low:
        return f"Can't find {host} (DNS). Are you on the right network or VPN?"
    if any(s in low for s in ("connection timed out", "operation timed out", "no route to host",
                              "network is unreachable", "connection refused", "connection closed by")):
        return f"Can't reach {host}: {last}"
    if code == 127 or "python3: command not found" in low or "python3: not found" in low:
        return f"python3 isn't installed on {host}; the widget needs it there."
    return f"{host}: {last}" if last else f"{host}: ssh failed (exit {code})"


def status(host, done_hours=None, recent=None, tokens=True):
    """The widget's status of host, as talpy_den.py --remote prints it there."""
    host = check_host(host)
    args = ["--remote"]
    if done_hours is not None:
        args += ["--done-hours", str(done_hours)]
    if recent is not None:
        args += ["--recent", str(recent)]
    if not tokens:
        args.append("--no-tokens")
    stars = cs.starred_of(cs.load_stars(cs.cache_dir(Path.home())), host)  # (the stars are kept here)
    for kind in cs.STAR_KINDS:
        if stars[kind]:
            args += [f"--starred-{kind}", ",".join(sorted(stars[kind]))]
    started = time.time()
    try:
        r = ssh(host, "python3 -I - " + " ".join(shlex.quote(a) for a in args), host_script(), STATUS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "host": host, "error": f"{host} didn't answer within {STATUS_TIMEOUT} s."}
    except OSError as exc:
        return {"ok": False, "host": host, "error": f"Can't run ssh: {exc}"}
    elapsed = int((time.time() - started) * 1000)
    if "\n" + cs.REMOTE_MARK not in r.stdout:
        return {"ok": False, "host": host, "error": explain(host, r.returncode, r.stderr), "elapsedMs": elapsed,
                "login": needs_login(r.stderr)}
    result = answer_after_mark(r.stdout)
    if result is None:
        return {"ok": False, "host": host, "error": f"{host} sent an answer the widget can't read.", "elapsedMs": elapsed}
    if not result.get("ok"):
        return {"ok": False, "host": host, "error": f"{host}: {result.get('error') or 'status failed'}", "elapsedMs": elapsed}
    return {"ok": True, "host": host, "result": result, "elapsedMs": elapsed, "loggedIn": logged_in(host)}


def last_line(text):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return lines[-1] if lines else ""


def outcome(host, r):
    """(ok, message) of a command run on host: its last line of output, or
    why it failed. Only stdout says how it went: the host's shell may print
    warnings on stderr (e.g. about a missing locale) after it."""
    if r.returncode != 0:
        if r.returncode == 255:
            return False, explain(host, r.returncode, r.stderr)
        return False, last_line(r.stderr) or last_line(r.stdout) or f"exit {r.returncode}"
    return True, last_line(r.stdout) or "Done"


def run(host, command, stdin=None):
    """Run a shell command on host; (ok, last line of its output)."""
    host = check_host(host)
    try:
        r = ssh(host, command, stdin)
    except subprocess.TimeoutExpired:
        return False, f"{host} didn't answer within {RUN_TIMEOUT} s"
    except OSError as exc:
        return False, f"Can't run ssh: {exc}"
    return outcome(host, r)


def host_answer(host, args, name="talpy_den.py"):
    """Run a script of this folder on host with args (it goes to python3's
    stdin, as for --status): its answer, {"ok", "message", ...}."""
    host = check_host(host)
    command = "python3 -I - --remote " + " ".join(shlex.quote(a) for a in args)
    try:
        r = ssh(host, command, host_script(name))
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"{host} didn't answer within {RUN_TIMEOUT} s"}
    except OSError as exc:
        return {"ok": False, "message": f"Can't run ssh: {exc}"}
    answer = answer_after_mark(r.stdout)
    if answer is not None:
        return {**answer, "ok": bool(answer.get("ok")), "message": answer.get("message") or ""}
    ok, message = outcome(host, r)
    if ok:  # it ran, but didn't answer as the script does
        return {"ok": False, "message": f"{host} sent an answer the widget can't read."}
    return {"ok": False, "message": message}


def run_status_script(host, args):
    """Run talpy_den.py on host with args: (ok, its message)."""
    answer = host_answer(host, args)
    return answer["ok"], answer["message"]


def vscode_account(host, config_dir):
    """Have VS Code's Claude on host run on the account whose folder is
    config_dir there (None: a plain ~/.claude), in the settings of its VS Code
    server, which windows connected to the host read: (ok, message)."""
    server = cs.VSCODE_SERVERS.get(cs.vscode_app()[1] or "", cs.VSCODE_SERVERS["Code"])
    return run_status_script(host, ["--vscode-account", config_dir or "-", "--vscode-server", server])


def dismiss(host, session_id=None):
    """Hide a finished session in the host's list (or show all again)."""
    return run_status_script(host, ["--dismiss", session_id] if session_id else ["--undismiss-all"])


def end(host, session_id, profile=None, busy=False):
    """Stop a chat that runs on host (talpy_den.stop_chat); only an idle one unless busy."""
    return run_status_script(host, ["--end", session_id] + (["--profile", profile] if profile else [])
                             + (["--busy"] if busy else []))


def interrupt(host, session_id, profile=None):
    """Interrupt a working chat in tmux on host (talpy_den.interrupt_chat)."""
    return run_status_script(host, ["--interrupt", session_id] + (["--profile", profile] if profile else []))


def agent_run(host, chat_id, agent_id, profile=None, open_it=True):
    """A chat's subagent on host: its run (talpy_den.agent_run there),
    saved here as Markdown and opened, as jump.show_agent_run does for one
    here: (ok, message, file)."""
    host = check_host(host)
    import jump
    args = ["--remote", "--agent-run", agent_id, "--chat", chat_id] + (["--profile", profile] if profile else [])
    try:
        r = ssh(host, "python3 -I - " + " ".join(shlex.quote(a) for a in args), host_script())
    except subprocess.TimeoutExpired:
        return False, f"{host} didn't answer within {RUN_TIMEOUT} s", None
    except OSError as exc:
        return False, f"Can't run ssh: {exc}", None
    if "\n" + cs.REMOTE_MARK not in r.stdout:
        return False, explain(host, r.returncode, r.stderr), None
    answer = answer_after_mark(r.stdout)
    if answer is None:
        return False, f"{host} sent an answer the widget can't read.", None
    if not answer.get("ok") or not isinstance(answer.get("markdown"), str):
        return False, f"{host}: {answer.get('message') or 'no such subagent'}", None
    path = jump.save_run(Path.home(), answer.get("title"), agent_id, answer["markdown"])
    if open_it:
        ok, why = jump.open_markdown(path)
        if not ok:
            return False, why, str(path)
    return True, f"{answer.get('message')} on {host}", str(path)


def share(host, chat_id, profile=None):
    """Share a chat with every account on host (sharing.py there), from
    profile's copy if given."""
    answer = host_answer(host, ["--share", chat_id] + (["--profile", profile] if profile else []), "sharing.py")
    return answer["ok"], answer["message"]


def unshare(host, chat_id, profile):
    """Keep a shared chat of host only on profile; the host's other accounts lose it."""
    answer = host_answer(host, ["--unshare", chat_id, "--profile", profile], "sharing.py")
    return answer["ok"], answer["message"]


def continue_on(host, chat_id, profile, open_it=True, end_first=False):
    """Continue a chat of host on another account there: shared with it if
    needed, then reopened in a local terminal (ssh -t), or, without open_it,
    the command that reopens it there: (ok, message, command). end_first: an
    idle session that has it open there quits first (talpy_den.end_for_handover)."""
    answer = host_answer(host, ["--continue", chat_id, "--profile", profile] + (["--end-first"] if end_first else []),
                         "sharing.py")
    if not answer["ok"]:
        return False, answer["message"], None
    command = answer.get("command")
    if not isinstance(command, str) or not command:
        return False, f"{host} didn't say how to reopen it", None
    if not open_it:
        return True, answer["message"], command
    ok, message = open_remote(host, command)
    return ok, message, command


def find(host, query):
    """The chats on host with every word of query (talpy_den.find_chats
    there), each with the command that reopens it there."""
    answer = host_answer(host, ["--find", query])
    return {**answer, "results": answer.get("results") or [], "more": bool(answer.get("more"))}


def plan_text(host, slug, profile=None):
    """A plan of host: {"ok", "title", "markdown", "where"} (talpy_den.plan_markdown there)."""
    return host_answer(host, ["--plan-text", slug] + (["--profile", profile] if profile else []))


def open_plan(host, slug, profile=None, open_it=True):
    """A plan of host, saved here as Markdown (a copy) and opened: (ok, message, file)."""
    import jump
    answer = plan_text(host, slug, profile)
    if not answer.get("ok") or not isinstance(answer.get("markdown"), str):
        return False, answer.get("message") or f"{host} doesn't have that plan", None
    folder = jump.runs_folder(Path.home())
    path = folder / f"plan-{re.sub(r'[^A-Za-z0-9_.-]', '_', host)}-{slug}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(answer["markdown"])
    if open_it:
        ok, why = jump.open_markdown(path)
        if not ok:
            return False, why, str(path)
    return True, f"Opened the plan {answer.get('title') or slug} from {host} (a copy)", str(path)


def plan_sharing(host, slug, profile, share_it, to=None):
    """Share a plan of host with its other accounts (or keep it on profile only), there."""
    args = (["--share-plan", slug] if share_it else ["--unshare-plan", slug]) + ["--profile", profile] \
        + (["--to", to] if to else [])
    answer = host_answer(host, args, "sharing.py")
    return answer["ok"], answer["message"]


def task(host, profile, folder, text="", plan=None, plan_profile=None, background=False, open_it=True,
         model=None, effort=None, mode=None):
    """A new chat on host's profile in folder there, with a task (and that
    model, effort and permission mode): in the background there, or in a terminal here (ssh
    -t), or (without open_it) the command that starts it there. {"ok",
    "message", "id" | "command"}."""
    args = ["--start-task" if background else "--task-command", folder or "~", "--profile", profile]
    if text:
        args += ["--prompt", text]
    if plan:
        args += ["--plan", plan] + (["--plan-profile", plan_profile] if plan_profile else [])
    if model:
        args += ["--model", model]
    if effort:
        args += ["--effort", effort]
    if mode:
        args += ["--permission-mode", mode]
    answer = host_answer(host, args)
    if background or not answer["ok"]:
        return answer
    command = answer.get("command")
    if not isinstance(command, str) or not command:
        return {"ok": False, "message": f"{host} didn't say how to start it"}
    if not open_it:
        return {"ok": True, "message": "", "command": command}
    ok, message = open_remote(host, command)
    return {"ok": ok, "message": message}


# A picture pasted here for a task there goes to the host's pictures folder
# (talpy_den.images_dir there: its XDG_CACHE_HOME or ~/.cache), on python3's
# stdin. Pictures older than IMAGES_KEEP_DAYS go, as here.
PUT_IMAGE = """
import json, os, sys, time
from pathlib import Path
name, stem = sys.argv[1], sys.argv[1].rsplit(".", 1)
folder = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "talpy-den" / "images"
folder.mkdir(parents=True, exist_ok=True)
folder.chmod(0o700)
for old in folder.iterdir():
    try:
        if old.stat().st_mtime < time.time() - %d * 86400:
            old.unlink()
    except OSError:
        pass
n = 1
while True:
    path = folder / (name if n == 1 else "%%s-h%%d.%%s" %% (stem[0], n, stem[1]))
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        break
    except FileExistsError:
        n += 1
with os.fdopen(fd, "wb") as f:
    f.write(sys.stdin.buffer.read())
sys.stdout.write("\\n%s " + json.dumps({"ok": True, "message": "", "path": str(path)}) + "\\n")
""" % (cs.IMAGES_KEEP_DAYS, cs.REMOTE_MARK)
PICTURE_NAME = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]+)?\.(png|jpg|webp|gif)$")


def put_image(host, path):
    """Copy a picture pasted here (jump.py --paste-image) to host, for a task
    there: {"ok", "message", "path": the copy there, "local": path}."""
    host, picture = check_host(host), Path(path)
    if picture.parent != cs.images_dir(Path.home()) or not PICTURE_NAME.match(picture.name) or not picture.is_file():
        return {"ok": False, "message": "That isn't a picture pasted into a task"}
    command = "python3 -I -c " + shlex.quote(PUT_IMAGE) + " " + shlex.quote(picture.name)
    try:
        r = subprocess.run(["ssh", *ssh_options(), "-T", host, posix(command)], input=picture.read_bytes(),
                           capture_output=True, timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"{host} didn't answer within {RUN_TIMEOUT} s"}
    except OSError as exc:
        return {"ok": False, "message": f"Can't run ssh: {exc}"}
    r = subprocess.CompletedProcess(r.args, r.returncode, r.stdout.decode("utf-8", "replace"),
                                    r.stderr.decode("utf-8", "replace"))
    answer = answer_after_mark(r.stdout)
    if answer is not None and answer.get("path"):
        return {"ok": True, "message": "", "path": answer["path"], "local": str(picture)}
    ok, message = outcome(host, r)
    return {"ok": False, "message": f"{host} sent an answer the widget can't read." if ok else message}


def continue_chat(host, profile, chat_id, folder="", text=""):
    """Go on with host's chat a usage limit stopped, in the background there
    (talpy_den.continue_chat): {"ok", "message", "id"}."""
    args = ["--continue-chat", chat_id, "--profile", profile]
    if folder:
        args += ["--folder", folder]
    if text:
        args += ["--prompt", text]
    return host_answer(host, args)


ACCOUNT_ACTIONS = ("--list", "--add", "--login", "--logout", "--remove", "--command", "--no-command", "--default")


def accounts(host, args):
    """accounts.py on host with args: its answer. A login (--add, --login)
    opens a terminal here running it there over ssh, unless --print-command
    asks for the command itself (the VS Code extension runs it in its own)."""
    if not args or args[0] not in ACCOUNT_ACTIONS:
        raise ValueError("not an accounts.py action: " + (args[0] if args else "none"))
    printing = "--print-command" in args
    args = [a for a in args if a != "--print-command"]
    answer = host_answer(host, args, "accounts.py")
    command = answer.pop("command", None)
    if not answer["ok"] or not isinstance(command, str) or not command:
        return answer
    if printing:
        return {**answer, "command": command}
    ok, message = open_remote(host, command)
    return {**answer, "ok": ok, "message": answer["message"] if ok else message}


def open_remote(host, command):
    """A local terminal running command on host (ssh -t: Claude needs a terminal).
    Not in batch mode: ssh may ask for a password or host key there."""
    host = check_host(host)
    import jump
    line = " ".join(shlex.quote(a) for a in ["ssh", *ssh_options(batch=False), "-t", host, posix(command)])
    if not jump.open_terminal(str(Path.home()), line):
        return False, "No terminal found to open the session in"
    return True, f"Opened a terminal on {host}"


def needs_login(stderr):
    """Whether logging in once in a terminal (--login) would let the checks
    through: the host wants a password, a passphrase or a key the agent
    doesn't hold, or its host key isn't known yet."""
    low = stderr.lower()
    return any(s in low for s in ("permission denied", "too many authentication failures",
                                  "host key verification failed"))


def login_marker(host):
    """A file next to the shared connections saying you logged in to host
    through the widget (so it offers to disconnect), or None."""
    d = control_dir()
    return d / f"{check_host(host)}.login" if d else None


def logged_in(host):
    """Whether you logged in to host with --login and that connection is still open."""
    marker = login_marker(host)
    if marker is None or not marker.exists():
        return False
    try:
        r = subprocess.run(["ssh", *ssh_options(), "-O", "check", host], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def login(host):
    """A terminal where you log in to host for the widget (login_here)."""
    host = check_host(host)
    import jump
    line = " ".join(shlex.quote(a) for a in [sys.executable or "python3", "-I", str(HERE / "remote.py"),
                                             "--login-here", host])
    # (the window closes by itself once you're in; it stays when it failed)
    if not jump.open_terminal(str(Path.home()), line + " && exit"):
        return False, "No terminal found to log in"
    return True, f"Log in to {host} in the terminal"


def login_here(host):
    """Log in to host in this terminal: ssh asks for the password (passphrase,
    code, host key) itself, so it goes to ssh only. The connection stays as
    the one the checks share. Host keys are checked as always. Exit status:
    ssh's, or 1 when there's no private folder for the connection."""
    host = check_host(host)
    d = control_dir()
    if d is None:
        print("There's no private folder for the shared connection ($XDG_RUNTIME_DIR), so the widget can't keep a login.")
        return 1
    print(f"Logging in to {host} for Talpy Den. Type what ssh asks for (password, passphrase, code):")
    print("it goes to ssh only, the widget never sees it.\n")
    # The window closes once you're in, and what's left of its session gets
    # SIGHUP: the shared connection detaches from it, but the connection to a
    # jump host it goes through (ProxyJump) doesn't, and ended there (the
    # next check needed a new login). Ignored here, it's ignored there too.
    hup = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    # ssh takes an option's first value: these go before the shared ones
    try:
        r = subprocess.run(["ssh", "-o", "ControlMaster=auto", "-o", f"ControlPersist={LOGIN_MINUTES}m",
                            "-o", "ForwardAgent=no", "-o", "ForwardX11=no",  # (whatever ~/.ssh/config says)
                            *ssh_options(batch=False), "-T", host, posix("true")])
    except OSError as exc:
        print(f"Can't run ssh: {exc}")
        return 1
    finally:
        signal.signal(signal.SIGHUP, hup)
    if r.returncode != 0:
        print(f"\nThe login to {host} didn't work (ssh exit {r.returncode}).")
        return r.returncode
    marker = login_marker(host)
    marker.touch(mode=0o600)
    os.chmod(str(marker), 0o600)
    print(f"\nLogged in. The widget checks {host} through this connection now. It closes {LOGIN_MINUTES} minutes "
          "after the widget stops using it, when you log out, or when you disconnect.")
    time.sleep(LOGIN_SHOWN)
    return 0


def disconnect(host):
    """Close the shared connection to host (the one you logged in with)."""
    host = check_host(host)
    marker = login_marker(host)
    if marker is not None:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
    try:
        r = subprocess.run(["ssh", *ssh_options(), "-O", "exit", host], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Can't close the connection to {host}: {exc}"
    return True, f"Disconnected from {host}" if r.returncode == 0 else f"No open connection to {host}"


USAGE = __doc__.split("\n\n")[1]


def stop_on_term(signum, frame):
    """A widget that gives up on a check (or is turned off) ends it with
    SIGTERM: exiting through Python lets subprocess.run end its ssh too."""
    sys.exit(128 + signum)


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    if not args:
        sys.exit(USAGE)
    signal.signal(signal.SIGTERM, stop_on_term)
    action, args = args[0], args[1:]
    try:
        if action == "--hosts":
            out = {"ok": True, "hosts": ssh_hosts()}
        elif action == "--status" and args:
            host, rest, kw = args[0], args[1:], {}
            while rest:
                arg = rest.pop(0)
                if arg == "--done-hours" and rest:
                    kw["done_hours"] = float(rest.pop(0))
                elif arg == "--recent" and rest:
                    kw["recent"] = int(rest.pop(0))
                elif arg == "--no-tokens":
                    kw["tokens"] = False
                else:
                    sys.exit(USAGE)
            out = status(host, **kw)
        elif action in ("--open", "--run") and len(args) == 2:
            ok, message = (open_remote if action == "--open" else run)(args[0], args[1])
            out = {"ok": ok, "message": message}
        elif action == "--dismiss" and len(args) == 2:
            ok, message = dismiss(args[0], args[1])
            out = {"ok": ok, "message": message}
        elif action == "--end" and len([a for a in args if a != "--busy"]) in (2, 3):
            ok, message = end(*[a for a in args if a != "--busy"], busy="--busy" in args)
            out = {"ok": ok, "message": message}
        elif action == "--interrupt" and len(args) in (2, 3):
            ok, message = interrupt(*args)
            out = {"ok": ok, "message": message}
        elif action == "--agent-run" and len([a for a in args if a != "--no-open"]) in (3, 4):
            ok, message, path = agent_run(*[a for a in args if a != "--no-open"], open_it="--no-open" not in args)
            out = {"ok": ok, "message": message, **({"file": path} if path else {})}
        elif action == "--share" and len(args) in (2, 3):
            ok, message = share(*args)
            out = {"ok": ok, "message": message}
        elif action == "--unshare" and len(args) == 3:
            ok, message = unshare(*args)
            out = {"ok": ok, "message": message}
        elif action == "--continue" and len([a for a in args if a not in ("--print-command", "--end-first")]) == 3:
            printing = "--print-command" in args
            ok, message, command = continue_on(*[a for a in args if a not in ("--print-command", "--end-first")],
                                               open_it=not printing, end_first="--end-first" in args)
            out = {"ok": ok, "message": message, **({"command": command} if command and printing else {})}
        elif action == "--find" and len(args) == 2:
            out = find(*args)
        elif action == "--plan" and len([a for a in args if a != "--no-open"]) in (2, 3):
            ok, message, path = open_plan(*[a for a in args if a != "--no-open"], open_it="--no-open" not in args)
            out = {"ok": ok, "message": message, **({"file": path} if path else {})}
        elif action in ("--share-plan", "--unshare-plan") and len(args) in (3, 4):
            ok, message = plan_sharing(args[0], args[1], args[2], action == "--share-plan",
                                       args[3] if len(args) == 4 else None)
            out = {"ok": ok, "message": message}
        elif action == "--task" and len(args) >= 3:
            host, profile, folder, rest, kw = args[0], args[1], args[2], args[3:], {}
            while rest:
                arg = rest.pop(0)
                if arg == "--prompt" and rest:
                    kw["text"] = rest.pop(0)
                elif arg == "--plan" and len(rest) >= 2:
                    kw["plan"], kw["plan_profile"] = rest.pop(0), rest.pop(0)
                elif arg in ("--model", "--effort") and rest:
                    kw[arg[2:]] = rest.pop(0) or None
                elif arg == "--permission-mode" and rest:
                    kw["mode"] = rest.pop(0) or None
                elif arg == "--bg":
                    kw["background"] = True
                elif arg == "--print-command":
                    kw["open_it"] = False
                else:
                    sys.exit(USAGE)
            out = task(host, profile, folder, **kw)
        elif action == "--put-image" and len(args) == 2:
            out = put_image(*args)
        elif action == "--login" and len(args) == 1:
            ok, message = login(args[0])
            out = {"ok": ok, "message": message}
        elif action == "--login-here" and len(args) == 1:  # (in the terminal --login opens)
            sys.exit(login_here(args[0]))
        elif action == "--vscode-account" and len(args) == 2:
            ok, message = vscode_account(args[0], None if args[1] == "-" else args[1])
            out = {"ok": ok, "message": message}
        elif action == "--accounts" and len(args) >= 2:
            out = accounts(args[0], args[1:])
        elif action == "--disconnect" and len(args) == 1:
            ok, message = disconnect(args[0])
            out = {"ok": ok, "message": message}
        elif action == "--undismiss-all" and len(args) == 1:
            ok, message = dismiss(args[0])
            out = {"ok": ok, "message": message}
        else:
            sys.exit(USAGE)
    except (ValueError, OSError) as exc:
        out = {"ok": False, "error": str(exc), "message": str(exc)}
    print(json.dumps(out))  # ASCII: a lone surrogate in a host's text can't break it


if __name__ == "__main__":
    main()
