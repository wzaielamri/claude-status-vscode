#!/usr/bin/env python3
"""Claude accounts on this computer: add one, log in or out, remove one
(the Talpy Den widget's Accounts settings, its GNOME extension's and the
VS Code extension's).

    python3 -I accounts.py --list [--check]      the accounts (--check: ask Claude which are logged in)
    python3 -I accounts.py --add NAME [--share-from PROFILE] [--command] [--print-command]
    python3 -I accounts.py --login PROFILE [--print-command]
    python3 -I accounts.py --logout PROFILE
    python3 -I accounts.py --remove PROFILE
    python3 -I accounts.py --command PROFILE | --no-command PROFILE
    python3 -I accounts.py --default PROFILE [--no-vscode]
    [--config-dir DIR]...                         extra Claude config dirs
    [--remote]                                    on an SSH host (remote.py --accounts HOST …)

An account is a Claude config folder, ~/.claude-NAME (what CLAUDE_CONFIG_DIR
points at), with a login, history and chats of its own; --add writes its
name into its .profile-name file (the marker other profile tools use too). Logging
in is Claude's own `claude auth login`, in a terminal (--print-command: the
command instead, for a terminal of your own): your browser signs you in and
Claude keeps the login in the folder. This script never reads it.

- --add makes the folder and logs it in. --share-from links your settings
  from another account into it: settings.json (your status line too),
  CLAUDE.md, keybindings.json, and your own skills, agents, commands and
  output styles one by one. Links point at the real files, never through
  ~/.claude, which may be switched to another account. Skills claude.ai
  syncs for an account (skills/synced) stay that account's, as do the
  login, MCP servers and plugins. --command adds ~/.local/bin/claude-NAME,
  which runs Claude on the account in any terminal.
- --logout is `claude auth logout`: Claude revokes the login and deletes it.
  Refused while chats run on the account.
- --remove logs out, then moves the folder to the Trash (without the login)
  and removes its claude-NAME command. Only for ~/.claude-NAME folders that
  no chat, ~/.claude or VS Code's Claude uses.
- --default makes the account the default one, listed first everywhere
  (talpy_den.default_profile): VS Code's Claude runs on it (only that
  entry of VS Code's settings changes; --no-vscode: the VS Code extension
  does it itself), and plain claude too where ~/.claude is a link (it's
  pointed there; a ~/.claude folder stays as it is). Refused while a Claude
  process runs through the name ~/.claude: it would go on on the other account.

On an SSH host, remote.py runs it there the way it runs sharing.py (on
python3's stdin, with talpy_den.py loaded first): nothing is installed
there. A login there runs in a terminal here, over ssh (--add and --login
print the command), VS Code's Claude there is the VS Code server's, and the
Trash is the host's.

Prints one JSON line, {"ok": bool, "message": str, ...} (with --remote,
after a marker line).
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ inside the installed widget
# Run from its file: the widget's folder (-I leaves it off sys.path). On an SSH
# host it comes on python3's stdin, after talpy_den.py (remote.py).
if os.path.isfile(globals().get("__file__") or ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import talpy_den as cs  # noqa: E402

REMOTE = False  # on an SSH host (--remote): VS Code there is its VS Code server, and no terminal opens there
NO_CLAUDE = ("Can't find the claude command: it isn't on PATH, in ~/.local/bin or npm's or nvm's folders, "
             "nor on your login shell's PATH")

NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")  # (what other profile tools allow too)
SHARED_FILES = ("settings.json", "CLAUDE.md", "keybindings.json")
SHARED_FOLDERS = ("skills", "agents", "commands", "output-styles")
# skills/synced: the skills claude.ai syncs for that account; .trash: Claude's
NOT_SHARED = ("synced", ".trash")
COMMAND_MARK = "Made by Talpy Den"
SERVICE_WAIT = 10  # seconds a stopped background service gets to go


# ---------------------------------------------------------------- helpers


def short(path, home):
    return cs.short_folder(str(path), home)


def claude_path(home, ask_shell=True):
    """The claude command's full path, or None (ask_shell: your login shell's
    PATH may be asked, which starts it: for a click, not for a quick look)."""
    found = shutil.which("claude") or cs.find_claude(home, ask_shell=ask_shell)
    return found if found != "claude" else None


def vscode_chats(profiles):
    """How many chats in VS Code's Claude panel work or wait for you right now
    (switching VS Code's Claude to another account cuts them off once their
    window reloads)."""
    return sum(1 for entry, _, _ in cs.live_entries_all(profiles)
               if entry.get("entrypoint") == "claude-vscode" and cs.session_state(entry) in ("working", "waiting"))


def find_profile(home, extra, key):
    """(the profile, every profile); (None, …) if there's none called key."""
    profiles = cs.discover_profiles(home, extra)
    return next((p for p in profiles if cs.matches_profile(p, key)), None), profiles


def claude_env(profile, home, claude):
    """The environment that runs claude on this profile."""
    env = dict(os.environ)
    value = cs.config_dir_value(profile, home)
    if value is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = value
    folder = os.path.dirname(claude)
    if os.path.isfile(os.path.join(folder, "node")):  # an npm install under nvm needs its node
        env["PATH"] = os.pathsep.join([folder, env.get("PATH", "")])
    return env


def run_claude(profile, home, claude, args, timeout=60):
    """(exit code, last line of its output) of claude args on this profile."""
    try:
        r = subprocess.run([claude, *args], env=claude_env(profile, home, claude), cwd=str(home),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    lines = [line.strip() for line in (r.stdout + "\n" + r.stderr).splitlines() if line.strip()]
    return r.returncode, lines[-1] if lines else ""


def running_chats(profile, profiles):
    """How many chats (in a terminal, VS Code or the background) run on this profile."""
    count = 0
    for entry, runs_on, holder in cs.live_entries_all(profiles):
        if entry.get("spare") or entry.get("parkedJobId") or entry.get("kind") not in cs.SESSION_KINDS:
            continue
        if profile.real in (runs_on.real, holder.real):
            count += 1
    return count


def chats_text(n):
    return "a chat runs" if n == 1 else f"{n} chats run"


def note_logged_out(cache_root, profile, out):
    """Remember (out) or forget that this profile was logged out here."""
    path = cache_root / "accounts.json"
    data = cs.read_json(path)
    data = data if isinstance(data, dict) else {}
    notes = data.get("loggedOut") if isinstance(data.get("loggedOut"), dict) else {}
    if out:
        notes[str(profile.real)] = int(time.time() * 1000)
    elif notes.pop(str(profile.real), None) is None:
        return
    data["loggedOut"] = notes
    cs.private_dir(cache_root)
    cs.write_json_atomic(path, data)


# ---------------------------------------------------------------- claude-NAME commands


def bin_dir(home):
    return home / ".local" / "bin"


def command_name(profile, home):
    """claude-NAME for a ~/.claude-NAME profile, else None (~/.claude is plain claude)."""
    if profile.path.parent != home or not profile.path.name.startswith(".claude-"):
        return None
    suffix = profile.path.name[len(".claude-"):]
    return "claude-" + suffix if re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z", suffix) else None


def ours(path):
    """Is the file at path a command this script made?"""
    try:
        with open(str(path), encoding="utf-8", errors="replace") as f:
            return COMMAND_MARK in f.read(1000)
    except OSError:
        return False


def command_script(folder_name, label):
    # Claude keys its background service by the folder's name: where ~/.claude
    # links to this folder, terminals use that name, so this does too (the
    # label on one line: .profile-name is other tools' too, and a second line
    # would run as a command)
    return f"""#!/bin/sh
# Claude Code on {cs.clean_text(label)} (~/{folder_name}).
# {COMMAND_MARK} (its Accounts settings remove it again).
dir="$HOME/{folder_name}"
if [ "$(readlink -f "$HOME/.claude" 2>/dev/null)" = "$(readlink -f "$dir" 2>/dev/null)" ]; then
    dir="$HOME/.claude"
fi
CLAUDE_CONFIG_DIR="$dir"
export CLAUDE_CONFIG_DIR
exec claude "$@"
"""


def command_problem(name, home):
    """Why claude-NAME can't be made (another program has the name), or ""."""
    path = bin_dir(home) / name
    if os.path.lexists(str(path)):
        return "" if ours(path) else f"{short(path, home)} is there already (not made by Talpy Den)"
    other = shutil.which(name)
    return f"{name} is a command already ({other})" if other else ""


def on_path(home):
    """Is ~/.local/bin on your PATH (the desktop's, else your login shell's)?"""
    want = str(bin_dir(home))
    if want in os.environ.get("PATH", "").split(os.pathsep):
        return True
    return want in cs.login_shell_path(home).split(os.pathsep)


def add_command(profile, home):
    """(ok, message): ~/.local/bin/claude-NAME runs Claude on the profile."""
    name = command_name(profile, home)
    if name is None:
        return False, f"{profile.label} has no command of its own: plain claude runs on it"
    problem = command_problem(name, home)
    if problem:
        return False, problem
    path = bin_dir(home) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{name}.{os.getpid()}.tmp")
    tmp.write_text(command_script(profile.path.name, profile.label), encoding="utf-8")
    os.chmod(str(tmp), 0o755)
    os.replace(str(tmp), str(path))
    if not on_path(home):
        return True, f"Added {name}, but {short(bin_dir(home), home)} isn't on your PATH: add it there to run {name}"
    return True, f"Added {name}: it runs Claude on {profile.label}"


def remove_command(profile, home):
    """(ok, message): takes ~/.local/bin/claude-NAME away, if this script made it."""
    name = command_name(profile, home)
    path = bin_dir(home) / name if name else None
    if path is None or not os.path.lexists(str(path)):
        return True, ""
    if not ours(path):
        return False, f"{short(path, home)} wasn't made by Talpy Den; it's left as it is"
    os.unlink(str(path))
    return True, f"Removed {name}"


# ---------------------------------------------------------------- the list


def auth_status(profile, home, claude):
    """What `claude auth status` says about the profile: {loggedIn, method, email}."""
    try:
        r = subprocess.run([claude, "auth", "status", "--json"], env=claude_env(profile, home, claude),
                           cwd=str(home), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        data = json.loads(r.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if not isinstance(data, dict):
        return None
    method = data.get("authMethod")
    return {"loggedIn": data.get("loggedIn") is True,
            "method": method if isinstance(method, str) and method != "none" else None,
            "email": cs.clean_text(data.get("email"), 80) or None}


def linked_from(profile, profiles):
    """What the other profiles link to in this profile's folder (settings
    shared from it, a projects/ folder shared through a link, …): "side:
    settings.json, skills/mine", or "". Gone to the Trash, they'd lose it."""
    real = str(profile.real)
    found = []
    for p in profiles:
        if p.real == profile.real:
            continue
        names = []
        try:
            entries = sorted(os.listdir(str(p.path)))
        except OSError:
            continue
        for e in entries:
            items = [e]
            if e in SHARED_FOLDERS and not os.path.islink(str(p.path / e)) and cs.is_dir(p.path / e):
                try:  # (shared one by one)
                    items = [f"{e}/{c}" for c in sorted(os.listdir(str(p.path / e)))]
                except OSError:
                    items = []
            for item in items:
                if os.path.islink(str(p.path / item)):  # (never raises, unlike Path's on Python 3.7)
                    to = os.path.realpath(str(p.path / item))
                    if to == real or to.startswith(real + os.sep):
                        names.append(item)
        if names:
            found.append(f"{p.label}: {', '.join(names)}")
    return "; ".join(found)


def not_removable(profile, profiles, home, vscode_on, chats):
    """Why the profile can't be removed here, or ""."""
    if profile.path.parent != home or not profile.path.name.startswith(".claude-"):
        return (f"{short(profile.path, home)} is your main Claude folder: log out of it instead" if profile.is_default
                else f"Only ~/.claude-NAME folders are removed here, not {short(profile.path, home)}")
    if profile.path.is_symlink():
        return f"{short(profile.path, home)} is a link to {short(profile.real, home)}: remove it yourself"
    default = home / ".claude"
    if default.is_symlink():
        try:
            if default.resolve() == profile.real:
                return "~/.claude links to it (plain claude runs on it): point ~/.claude at another account first"
        except (OSError, RuntimeError):
            pass
    linked = linked_from(profile, profiles)
    if linked:
        return f"other accounts link to files in it ({linked}): give them copies first"
    if vscode_on is not None and vscode_on.real == profile.real:
        return "VS Code's Claude runs on it: switch VS Code's Claude to another account first"
    if chats:
        return f"{chats_text(chats).capitalize()} on it: close them first"
    return ""


def list_accounts(home, extra, check=False):
    profiles = cs.discover_profiles(home, extra)
    cache_root = cs.cache_dir(home)
    logged_out = cs.logged_out_notes(cache_root)
    try:
        installed, vscode_on, _, chosen = cs.default_profile(home, profiles, REMOTE, cache_root)
    except Exception:  # VS Code's settings are only one of the reasons
        installed, vscode_on, chosen = False, None, cs.plain_profile(home, profiles)
    found = cs.cached_find_claude(home, cache_root, time.time())  # (a quick look: asks the shell at most hourly)
    claude = found if check else None
    statuses = {}
    if claude:
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = dict(zip([p.key for p in profiles], pool.map(lambda p: auth_status(p, home, claude), profiles)))
    default = home / ".claude"
    linked = None
    if default.is_symlink():
        try:
            linked = default.resolve()
        except (OSError, RuntimeError):
            pass
    accounts = []
    for p in profiles:
        chats = running_chats(p, profiles)
        name = command_name(p, home)
        status = statuses.get(p.key)
        accounts.append({
            "key": p.key,
            "label": p.label,
            "folder": short(p.path, home),
            "main": p.is_default or linked == p.real,  # plain claude runs on it
            "account": p.display_name,
            "plan": p.plan,
            # Claude's own answer with --check (an API key counts), else
            # whether its .claude.json has an account
            "loggedIn": status["loggedIn"] if status else not cs.needs_login(p, logged_out),
            "quick": not cs.needs_login(p, logged_out),  # what the folder says (changes when you log in or out)
            "checked": status is not None,
            "method": status["method"] if status else None,
            "email": status["email"] if status else None,
            "chats": chats,
            "vscode": vscode_on is not None and vscode_on.real == p.real,
            "default": chosen is not None and chosen.real == p.real,
            "commandName": name,
            "command": bool(name) and ours(bin_dir(home) / name),
            "notRemovable": not_removable(p, profiles, home, vscode_on, chats),
        })
    switchable = vscode_state(home, profiles)[0]
    return True, "", {"accounts": accounts, "canCheck": found is not None, "linked": default_link(home),
                      "vscodeInstalled": bool(installed),
                      # Make Default can point VS Code's Claude or plain claude somewhere
                      "canDefault": bool(switchable) or default_link(home)}


# ---------------------------------------------------------------- adding


def name_problem(name, home):
    """Why ~/.claude-NAME can't be a new account, or ""."""
    if not name:
        return "Give the account a name"
    if name in ("default", "main"):
        return f"{name} is taken: main is what ~/.claude is called, default marks the default account"
    if not NAME.match(name):
        return "A name is up to 32 letters, digits, - and _, starting with a letter"
    folder = home / f".claude-{name}"
    if os.path.lexists(str(folder)):
        return (f"There's an account called {name} already" if cs.is_profile_dir(folder)
                else f"{short(folder, home)} is there already")
    return ""


def share_into(source, folder, home):
    """Link source's settings, CLAUDE.md, … into the new folder: the names linked."""
    shared = []
    for name in SHARED_FILES:
        item = source.path / name
        if os.path.isfile(str(item)):
            os.symlink(os.path.realpath(str(item)), str(folder / name))
            shared.append(name)
    for name in SHARED_FOLDERS:
        item = source.path / name
        if not cs.is_dir(item):
            continue
        if item.is_symlink():  # the source shares it whole: so does this one
            os.symlink(os.path.realpath(str(item)), str(folder / name))
            shared.append(name)
            continue
        entries = []
        try:
            entries = sorted(e for e in os.listdir(str(item)) if e not in NOT_SHARED and not e.startswith("."))
        except OSError:
            pass
        if not entries:
            continue
        (folder / name).mkdir(mode=0o700)
        for e in entries:
            os.symlink(os.path.realpath(str(item / e)), str(folder / name / e))
        shared.append(f"{name} ({len(entries)})")
    return shared


def add_account(home, extra, name, share_from=None, with_command=False):
    """(ok, message, the new profile or None): make ~/.claude-NAME."""
    name = (name or "").strip()
    problem = name_problem(name, home)
    if problem:
        return False, problem, None
    if with_command:
        problem = command_problem("claude-" + name, home)
        if problem:
            return False, f"{problem}: pick another name, or leave out the command", None
    source = None
    if share_from:
        source, _ = find_profile(home, extra, share_from)
        if source is None:
            return False, f"No Claude profile called {share_from} to share settings with", None
    folder = home / f".claude-{name}"
    folder.mkdir(mode=0o700)
    (folder / ".profile-name").write_text(name + "\n", encoding="utf-8")
    shared = share_into(source, folder, home) if source is not None else []
    profile = cs.Profile(folder, home)
    message = f"Added {name} ({short(folder, home)})"
    if shared:
        message += f", sharing {', '.join(shared)} with {source.label}"
    if with_command:
        ok, said = add_command(profile, home)
        message += ". " + said if ok else f". {said}"
    return True, message, profile


# ---------------------------------------------------------------- logging in and out


def login(profile, home, print_only=False):
    """(ok, message, command): a terminal with Claude's login for the profile
    (print_only, or on an SSH host: just the command)."""
    if print_only or REMOTE:
        claude = claude_path(home)
        if claude is None:
            return False, NO_CLAUDE, None
        return True, "", cs.in_folder(str(home), cs.login_command(profile, home, claude))
    import jump  # (the desktop's terminals)
    claude = jump.claude_in_terminal(home)
    if claude is None:
        return False, NO_CLAUDE, None
    command = cs.login_command(profile, home, claude)
    if not jump.open_terminal(str(home), command):
        return False, "No terminal found to log in in", None
    return True, f"Log in to {profile.label} in the terminal that opened", None


def logout(home, extra, key):
    profile, profiles = find_profile(home, extra, key)
    if profile is None:
        return False, f"No Claude profile called {key}"
    chats = running_chats(profile, profiles)
    if chats:
        return False, f"{chats_text(chats).capitalize()} on {profile.label}: close them first, they'd lose the login"
    claude = claude_path(home)
    if claude is None:
        return False, NO_CLAUDE
    code, said = run_claude(profile, home, claude, ["auth", "logout"])
    if code != 0:
        return False, f"Couldn't log out of {profile.label}: {said or f'claude exited with {code}'}"
    note_logged_out(cs.cache_dir(home), profile, True)
    return True, f"Logged out of {profile.label}"


# ---------------------------------------------------------------- removing


def trash_root(home):
    data = os.environ.get("XDG_DATA_HOME", "")
    return (Path(data) if os.path.isabs(data) else home / ".local/share") / "Trash"


def device(path):
    return os.stat(str(path)).st_dev


MOUNTINFO = "/proc/self/mountinfo"


def mount_inside(path):
    """A mount point at or inside the folder path, or None. A folder
    bind-mounted from the same disk shows only here (its device is the
    disk's): moved into the Trash with the folder, it stays mounted there,
    and emptying the Trash would delete what it shows."""
    real = os.path.realpath(str(path))
    try:
        with open(MOUNTINFO, encoding="utf-8", errors="replace") as f:
            for line in f:
                fields = line.split(" ")
                if len(fields) < 5:
                    continue
                point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])  # (\040: a space)
                if point == real or point.startswith(real + "/"):
                    return point
    except OSError:
        pass
    return None


def trash_problem(path, home):
    """Why path can't be moved to your Trash, or "". A move can't cross
    filesystems; gio, the desktop's own tool, knows the Trash of other disks."""
    mounted = mount_inside(path)
    if mounted:
        return (f"{short(mounted, home)} is a mount point (emptying the Trash would delete what's mounted there): "
                "unmount it first")
    root = trash_root(home)
    try:
        for d in (root, root / "files", root / "info"):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        if device(path) == device(root / "files") or shutil.which("gio"):
            return ""
    except OSError as exc:
        return f"the Trash can't be used ({exc.strerror or exc})"
    return f"{short(path, home)} is on another disk than your Trash: remove it yourself"


def move_to_trash(path, home):
    """Move path into your Trash (the freedesktop.org one, which Dolphin and
    Files show); its name there."""
    root = trash_root(home)
    files, info = root / "files", root / "info"
    if device(path) != device(files):
        subprocess.run(["gio", "trash", str(path)], check=True, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
        return path.name
    n = 1
    while True:
        name = path.name if n == 1 else f"{path.name}.{n}"
        n += 1
        if os.path.lexists(str(files / name)):
            continue
        try:  # the info file claims the name
            fd = os.open(str(info / f"{name}.trashinfo"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        break
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"[Trash Info]\nPath={urllib.parse.quote(str(path))}\nDeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    try:
        os.rename(str(path), str(files / name))
    except OSError:
        os.unlink(str(info / f"{name}.trashinfo"))
        raise
    return name


def stop_service(profile, home, claude):
    """Stop the profile's background service, if one runs (it would write
    into the folder again): "" when it's gone, else why not."""
    name = cs.service_config_dir(profile)
    if name is None:
        return ""
    env = claude_env(profile, home, claude)
    env["CLAUDE_CONFIG_DIR"] = name  # the service is keyed by the name it runs under
    try:
        subprocess.run([claude, "daemon", "stop", "--any"], env=env, cwd=str(home), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass
    deadline = time.time() + SERVICE_WAIT
    while cs.service_config_dir(profile) is not None:
        if time.time() > deadline:
            return "its background service is still running"
        time.sleep(0.3)
    return ""


def remove_account(home, extra, key):
    profile, profiles = find_profile(home, extra, key)
    if profile is None:
        return False, f"No Claude profile called {key}"
    try:
        _, vscode_on = cs.vscode_profile(home, profiles, REMOTE, cs.cache_dir(home))
    except Exception as exc:  # unsure whether VS Code's Claude uses it: keep it
        return False, f"{profile.label} isn't removed: can't tell which account VS Code's Claude uses ({exc})"
    why = (not_removable(profile, profiles, home, vscode_on, running_chats(profile, profiles))
           or trash_problem(profile.path, home))
    if why:
        return False, f"{profile.label} isn't removed: {why}"
    claude = claude_path(home)
    credentials = profile.path / ".credentials.json"
    notes = []
    if claude is not None:
        why = stop_service(profile, home, claude)
        if why:
            return False, f"{profile.label} isn't removed: {why}"
        if profile.account_id is not None or os.path.lexists(str(credentials)):
            code, said = run_claude(profile, home, claude, ["auth", "logout"])
            if code != 0:
                notes.append(f"couldn't log out first ({said or f'exit {code}'})")
    # The Trash gets the folder without the login (after a logout Claude
    # keeps a device id in that file)
    try:
        os.unlink(str(credentials))
    except FileNotFoundError:
        pass
    _, said = remove_command(profile, home)
    if said:
        notes.append(said)
    name = move_to_trash(profile.path, home)
    note_logged_out(cs.cache_dir(home), profile, False)
    message = f"Removed {profile.label}: {short(profile.path, home)} is in the Trash ({name}), without its login"
    return True, message + ("; " + "; ".join(notes) if notes else "")


# ---------------------------------------------------------------- the default account


def default_link(home):
    """Can plain claude be pointed at any account: is ~/.claude a link? One
    is only ever re-pointed, never made: without CLAUDE_CONFIG_DIR set (a
    shell of its own: the profile tools that link ~/.claude set it), plain
    claude reads its account file from ~/.claude.json, whatever the link
    says, and would mix two accounts."""
    return (home / ".claude").is_symlink()


def process_env(pid, names):
    """These variables of a process's environment (as it was started)."""
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            items = f.read().split(b"\0")
    except OSError:
        return {}
    out = {}
    for item in items:
        key, sep, value = item.partition(b"=")
        if sep and key.decode(errors="replace") in names:
            out[key.decode()] = value.decode(errors="replace")
    return out


def through_link(home, profiles):
    """The live Claude processes (chats, background ones, Claude's service)
    that use the name ~/.claude: pointed elsewhere, they'd go on there."""
    link = str(home / ".claude")
    pids = set()
    for entry, _, _ in cs.live_entries_all(profiles):
        env = process_env(entry["pid"], ("CLAUDE_CONFIG_DIR", "HOME"))
        # (without the variable: its home's ~/.claude)
        value = env.get("CLAUDE_CONFIG_DIR") or (env["HOME"] + "/.claude" if env.get("HOME") else "")
        if value and os.path.normpath(os.path.expanduser(value)) == link:
            pids.add(entry["pid"])
    for p in profiles:
        if os.path.normpath(cs.service_config_dir(p) or "") == link:
            pids.add(-1)  # its background service
    return len(pids)


def vscode_value(profile, home):
    """The CLAUDE_CONFIG_DIR VS Code's Claude needs for this profile (None:
    none, a plain ~/.claude). The one ~/.claude links to is named by the link,
    as terminals name it: Claude keys its background service by the name."""
    link = home / ".claude"
    if link.is_symlink():
        try:
            if link.resolve() == profile.real:
                return str(link)
        except (OSError, RuntimeError):
            pass
    elif link == profile.path:
        return None
    return str(profile.real)


def vscode_state(home, profiles):
    """(VS Code's Claude can be switched, the profile it runs on now): here,
    VS Code's own; on an SSH host, its VS Code server's (where VS Code ever
    connected to it)."""
    if REMOTE:
        _, on = cs.vscode_profile(home, profiles, remote=True)
        return any(cs.is_dir(home / s) for s in cs.VSCODE_SERVERS.values()), on
    installed, on = cs.vscode_profile(home, profiles)
    return bool(installed), on


def vscode_setting(home):
    """The CLAUDE_CONFIG_DIR VS Code's settings give its Claude (None: none)."""
    return cs.vscode_server_config_dir(home) if REMOTE else cs.vscode_setting_config_dir(home, cs.vscode_app()[1])


def set_vscode(home, value, extra):
    """VS Code's Claude runs with value from now on: (ok, why not)."""
    if not REMOTE:
        return cs.set_vscode_user_config_dir(home, value)
    for server in cs.VSCODE_SERVERS.values():
        if cs.is_dir(home / server):
            ok, why = cs.set_vscode_server_config_dir(home, value, server, extra)
            if not ok:
                return False, why
    return True, ""


def make_default(home, extra, key, vscode=True):
    """(ok, message): the profile is the default account from now on. What
    a switch would cut off is checked before anything changes."""
    profile, profiles = find_profile(home, extra, key)
    if profile is None:
        return False, f"No Claude profile called {key}"
    link = home / ".claude"
    linked = default_link(home)
    try:
        current = link.resolve() if linked else None
    except (OSError, RuntimeError):
        current = None
    moves_link = linked and current != profile.real
    switchable, on = vscode_state(home, profiles) if vscode else (False, None)
    moves_vscode = switchable and (on is None or on.real != profile.real)
    if moves_link:
        busy = through_link(home, profiles)
        if busy:
            what = "a Claude process runs" if busy == 1 else f"{busy} Claude processes run"
            return False, (f"{profile.label} isn't the default yet: {what} through ~/.claude (in a terminal, VS Code "
                           f"or the background), and would go on on {profile.label}. Close those chats first")
    if moves_vscode:
        working = vscode_chats(profiles)
        if working:
            what = "a chat works" if working == 1 else f"{working} chats work"
            return False, (f"{profile.label} isn't the default yet: {what} in VS Code's Claude, which a switch would cut "
                           "off once its window reloads. Try again once it's done")
    done, notes, failed = [], [], []
    if moves_link:
        target = os.readlink(str(link))
        # (a relative link names a folder next to it: one elsewhere, from
        # --config-dir, gets its full path)
        new = profile.path.name if not os.path.isabs(target) and profile.path.parent == home else str(profile.real)
        tmp = home / f".claude.{os.getpid()}.tmp"
        os.symlink(new, str(tmp))
        os.replace(str(tmp), str(link))
        done.append("plain claude")
    elif not linked and profile.path != link:
        notes.append(("plain claude stays on ~/.claude (a folder, not a link)" if os.path.lexists(str(link))
                      else "plain claude has no ~/.claude here") + ": run "
                     + (command_name(profile, home) or "claude") + " for this account")
    if switchable:
        value = vscode_value(profile, home)
        # the same account under another name counts too: as the link, it
        # follows ~/.claude from now on
        if moves_vscode or vscode_setting(home) != value:
            ok, why = set_vscode(home, value, extra)
            now = vscode_state(home, cs.discover_profiles(home, extra))[1] if ok else None
            if not ok:
                failed.append(why)
            elif now is None or now.real != profile.real:
                failed.append("VS Code was started with another CLAUDE_CONFIG_DIR, which its Claude keeps: "
                              "start VS Code without it")
            elif moves_vscode:
                done.append("VS Code's Claude (new chats there; a window's Claude panel once the window reloads)")
    message = f"{profile.label} is the default account" + (f": {' and '.join(done)} run on it" if done
                                                            else "" if failed else " already")
    return not (failed and not done), "; ".join([message, *failed, *notes])


# ---------------------------------------------------------------- main

USAGE = ("usage: accounts.py --list [--check] [--config-dir DIR]...\n"
         "       accounts.py --add NAME [--share-from PROFILE] [--command] [--print-command]\n"
         "       accounts.py --login PROFILE [--print-command]\n"
         "       accounts.py --logout PROFILE | --remove PROFILE\n"
         "       accounts.py --command PROFILE | --no-command PROFILE\n"
         "       accounts.py --default PROFILE [--no-vscode]\n"
         "       … [--config-dir DIR]... [--remote]")


def main(argv=None):
    global REMOTE
    args = sys.argv[1:] if argv is None else list(argv)
    action, target, extra, check, share_from, with_command, print_only = None, None, [], False, None, False, False
    vscode = True
    while args:
        arg = args.pop(0)
        if arg == "--list" and action is None:
            action = arg
        elif arg in ("--add", "--login", "--logout", "--remove", "--no-command", "--default") and args and action is None:
            action, target = arg, args.pop(0)
        elif arg == "--command" and action is None and args and not args[0].startswith("-"):
            action, target = arg, args.pop(0)
        elif arg == "--command":  # with --add
            with_command = True
        elif arg == "--check":
            check = True
        elif arg == "--share-from" and args:
            share_from = args.pop(0)
        elif arg == "--print-command":
            print_only = True
        elif arg == "--no-vscode":
            vscode = False
        elif arg == "--remote":
            REMOTE = True
        elif arg == "--config-dir" and args:
            extra.append(args.pop(0))
        else:
            sys.exit(USAGE)
    if (action is None or (with_command and action != "--add") or (print_only and action not in ("--add", "--login"))
            or (not vscode and action != "--default")):
        sys.exit(USAGE)
    os.umask(0o077)
    home, more, command = Path.home(), {}, None
    try:
        if action == "--list":
            ok, message, more = list_accounts(home, extra, check)
        elif action == "--add":
            ok, message, profile = add_account(home, extra, target, share_from, with_command)
            if ok:
                more = {"key": profile.key}
                logged, said, command = login(profile, home, print_only)
                if not logged:
                    ok, message = False, f"{message}, but {said[0].lower()}{said[1:]}"
                elif said:
                    message += f". {said}"
        elif action == "--login":
            profile, _ = find_profile(home, extra, target)
            if profile is None:
                ok, message = False, f"No Claude profile called {target}"
            else:
                ok, message, command = login(profile, home, print_only)
        elif action == "--logout":
            ok, message = logout(home, extra, target)
        elif action == "--remove":
            ok, message = remove_account(home, extra, target)
        elif action == "--default":
            ok, message = make_default(home, extra, target, vscode)
            profile, _ = find_profile(home, extra, target)
            if ok and profile is not None:  # what VS Code's Claude needs for it now (the VS Code extension sets it)
                more = {"configDir": vscode_value(profile, home)}
        else:
            profile, _ = find_profile(home, extra, target)
            if profile is None:
                ok, message = False, f"No Claude profile called {target}"
            else:
                ok, message = (add_command if action == "--command" else remove_command)(profile, home)
                message = message or f"{profile.label} has no command to remove"
    except Exception as exc:  # report, don't crash: the widget shows the message
        ok, message = False, f"{type(exc).__name__}: {exc}"
    if REMOTE:  # login scripts on the host may print things first: the JSON follows a marker
        sys.stdout.write("\n" + cs.REMOTE_MARK + " ")
    print(json.dumps({"ok": ok, "message": message, **more, **({"command": command} if command else {})}))


if __name__ == "__main__":
    main()
