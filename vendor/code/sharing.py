#!/usr/bin/env python3
"""Share a Claude Code chat between your Claude profiles, so another account
can continue it (jump.py's --share, --continue and --unshare).

A shared chat is one transcript file hard-linked into each profile's
projects/<project>/ folder: every profile lists it with its own chats, and
whichever account continues it writes to that same file. The chat's two
folders (projects/<project>/<id>/: subagents and tool results;
file-history/<id>/: undo checkpoints) move to a store, and each profile gets
a symlink to them, so Claude cleaning up one profile never takes them from
the others:

    ~/.local/share/claude-status/shared-chats/<project>/   ($XDG_DATA_HOME)
        <id>.jsonl           one more link to the transcript
        <id>/                the chat's folder
        <id>.file-history/   its undo checkpoints

A chat shared this way by another tool, into a store of its own, is found
through the profiles' links and handled where it is. talpy_den.py tells
shared chats apart by the transcript's inode, whoever shared them.

A chat can be open in only one Claude session at a time (two would write the
same file): a chat a session has open isn't shared or unshared.
Nothing here starts Claude or sends anything anywhere.

On an SSH host, remote.py runs this file's main() (python3 -I - with
talpy_den.py loaded first, remote.host_script), for the host's own
profiles:

    --share CHAT_ID [--profile PROFILE]   share it with every profile (from PROFILE's copy)
    --continue CHAT_ID --profile PROFILE  share it with PROFILE if needed; the command reopening it there
    --unshare CHAT_ID --profile PROFILE   keep it only on PROFILE
    [--config-dir DIR]... [--remote]      extra Claude config dirs; the JSON after a marker line
"""

import json
import os
import re
import shutil
import sys
from pathlib import Path

import talpy_den as cs

# Claude's projects/<project> folder: the project's path with every other character as "-"
PROJECT_KEY = re.compile(r"^[A-Za-z0-9._-]+$")
TRANSCRIPT, FOLDER, CHECKPOINTS = "transcript", "folder", "checkpoints"
PARTS = (TRANSCRIPT, FOLDER, CHECKPOINTS)
FOLDERS = (FOLDER, CHECKPOINTS)


def store_root(home):
    data = os.environ.get("XDG_DATA_HOME", "")
    base = Path(data) if os.path.isabs(data) else home / ".local/share"
    # (the folder keeps its first name: each shared chat's profile links point into it)
    return base / "claude-status" / "shared-chats"


def in_profile(profile, key, chat_id, part):
    """Where a part of the chat is in a profile (its real folder)."""
    if part == TRANSCRIPT:
        return profile.real / "projects" / key / f"{chat_id}.jsonl"
    if part == FOLDER:
        return profile.real / "projects" / key / chat_id
    return profile.real / "file-history" / chat_id


def in_store(folder, chat_id, part):
    """Where a part of the chat is in a store folder (<store>/<project>)."""
    return folder / {TRANSCRIPT: f"{chat_id}.jsonl", FOLDER: chat_id, CHECKPOINTS: f"{chat_id}.file-history"}[part]


def same(a, b):
    try:
        return os.path.samefile(str(a), str(b))
    except OSError:
        return False


def present(path):
    return path.exists() or path.is_symlink()


def target(link):
    """What a symlink points to, resolved (also when it's gone)."""
    return Path(os.path.realpath(str(link)))


def inside(path, folder):
    try:
        return os.path.commonpath([str(path), str(folder)]) == str(folder)
    except ValueError:
        return False


def links(path):
    try:
        return os.stat(str(path)).st_nlink
    except OSError:
        return 0


def entry(path):
    """The directory entry a path names, through linked folders: two profiles
    whose projects/ is one folder (a symlink, or a bind mount, which no path
    shows: hence its folder's device and inode) reach the same entry, which is
    one copy of the chat, not two."""
    real = os.path.realpath(str(path))
    try:
        folder = os.stat(os.path.dirname(real))
    except OSError:
        return real
    return folder.st_dev, folder.st_ino, os.path.basename(real)


def merge_into(src, dst):
    """Move what's in the folder src into the folder dst (whatever dst
    doesn't have yet), then remove src if that emptied it."""
    for child in list(src.iterdir()):
        there = dst / child.name
        if not present(there):
            shutil.move(str(child), str(there))
        elif child.is_dir() and not child.is_symlink() and there.is_dir():
            merge_into(child, there)
    try:
        src.rmdir()
    except OSError:
        pass  # something both had: left where it was


def copies(profiles, chat_id):
    """The profiles that have this chat, grouped by file: [[(profile, transcript)]],
    the file most profiles link to first (then the newest)."""
    groups = []
    for p in profiles:
        path = cs.transcript_path(p, chat_id)
        if path is None or path.is_symlink() or not path.is_file():
            continue
        for g in groups:
            if same(g[0][1], path):
                g.append((p, path))
                break
        else:
            groups.append([(p, path)])
    groups.sort(key=lambda g: (-len(g), -(cs.mtime(g[0][1]) or 0)))
    return groups


def store_paths(sharers, key, chat_id, root):
    """{part: where it is in the store}. A chat shared before keeps its folders
    where the profiles' symlinks point (maybe another tool's store); the rest
    goes to <root>/<project>."""
    ours = root / key
    paths = {part: in_store(ours, chat_id, part) for part in PARTS}
    for part in FOLDERS:
        link = next((in_profile(p, key, chat_id, part) for p in sharers
                     if in_profile(p, key, chat_id, part).is_symlink()), None)
        if link is not None:
            paths[part] = target(link)
    # that store's own link to the transcript sits next to the chat's folder
    beside = paths[FOLDER].parent / f"{chat_id}.jsonl"
    if paths[FOLDER].parent != ours and beside.is_file() and same(beside, in_profile(sharers[0], key, chat_id, TRANSCRIPT)):
        paths[TRANSCRIPT] = beside
    return paths


def device(path):
    """The filesystem (st_dev) a path is on, or would be on: that of its
    nearest folder that exists."""
    for p in (path, *path.parents):
        try:
            return os.stat(str(p)).st_dev
        except OSError:
            continue
    return None


def elsewhere(path, places):
    """Labels of the places (label, path) on another filesystem than path:
    hard links can't cross filesystems (another disk, partition or btrfs
    subvolume)."""
    here = device(path)
    return [label for label, p in places if device(p) != here]


def link_into(link, stored):
    """A profile's hard link to the transcript, or symlink to a folder."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if stored.is_dir():
        os.symlink(str(stored), str(link))
    else:
        os.link(str(stored), str(link))


def share(profiles, chat_id, into, root, source=None):
    """Share a chat into the profiles `into`: from `source`'s copy, else the one
    most profiles have. (ok, message)."""
    groups = copies(profiles, chat_id)
    if not groups:
        return False, "That chat has no transcript on this computer"
    chat = next((g for g in groups if source is not None and any(p.real == source.real for p, _ in g)), None)
    if chat is None:
        if len(groups) > 1 and len(groups[1]) == len(groups[0]):
            names = ", ".join(p.label for g in groups for p, _ in g)
            return False, f"Different chats have this id on {names}; left everything as it is"
        chat = groups[0]
    path = chat[0][1]
    key = path.parent.name
    if not PROJECT_KEY.match(key) or key in (".", ".."):
        return False, f"Unexpected project folder {key}; left the chat as it is"
    sharers = [p for p, _ in chat]
    have = {p.real for p in sharers}
    store = store_paths(sharers, key, chat_id, root)
    # Checked first, so nothing is left half-done
    apart = elsewhere(path, [("the shared-chat folder", store[TRANSCRIPT])] +
                      [(q.label, in_profile(q, key, chat_id, TRANSCRIPT)) for q in into if q.real not in have])
    if apart:
        return False, (f"Can't share it: {', '.join(apart)} {'is' if len(apart) == 1 else 'are'} on another disk or "
                       f"partition than this chat, and a shared chat is one file linked into each account. "
                       f"Keep the Claude folders and {store[TRANSCRIPT].parent.parent} on one filesystem")
    if present(store[TRANSCRIPT]) and not same(store[TRANSCRIPT], path):
        return False, f"{store[TRANSCRIPT]} is a different chat with the same id; left everything as it is"
    for part in FOLDERS:  # also checked before anything changes
        there = store[part]
        heres = [in_profile(p, key, chat_id, part) for p in sharers]
        linked = any(here.is_symlink() and target(here) == there for here in heres)
        stray = there.is_dir() and any(there.iterdir()) or present(there) and not there.is_dir()
        if not linked and stray and any(here.is_dir() and not here.is_symlink() for here in heres):
            return False, f"{there} is already there; left the chat as it is"
    old_umask = os.umask(0o077)  # the store holds chats: only you can read it
    try:
        if not present(store[TRANSCRIPT]):
            store[TRANSCRIPT].parent.mkdir(parents=True, exist_ok=True)
            os.link(str(path), str(store[TRANSCRIPT]))
        # The chat's folders move to the store (the ones Claude makes later are
        # made there now), and the profiles that have it link to them.
        for part in FOLDERS:
            there = store[part]
            linked = any(here.is_symlink() and target(here) == there
                         for here in (in_profile(p, key, chat_id, part) for p in sharers))
            for p in sharers:
                here = in_profile(p, key, chat_id, part)
                if here.is_symlink():
                    if target(here) == there and not there.exists():
                        there.mkdir(parents=True)  # its store folder went missing
                elif here.is_dir():
                    if not linked and there.is_dir() and not any(there.iterdir()):
                        there.rmdir()  # left over, empty
                    if not present(there):
                        there.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(here), str(there))
                        os.symlink(str(there), str(here))
                        linked = True
                    # else: this profile keeps a folder of its own
                elif not present(here):
                    there.mkdir(parents=True, exist_ok=True)
                    link_into(here, there)
                    linked = True
        added, skipped = [], []
        for q in into:
            if q.real in have:
                continue
            plan = [(in_profile(q, key, chat_id, part), path if part == TRANSCRIPT else store[part]) for part in PARTS]
            if any(present(link) and not same(link, stored) for link, stored in plan):
                skipped.append(q.label)  # a chat of its own with this id
                continue
            for link, stored in plan:
                if not present(link):
                    link_into(link, stored)
            have.add(q.real)
            added.append(q.label)
    except OSError as exc:
        return False, f"Couldn't share the chat: {exc}"
    finally:
        os.umask(old_umask)
    on = [p.label for p in profiles if p.real in have]
    if added:
        message = f"Chat shared: it's now on {', '.join(on)}."
    elif skipped:
        return False, f"Not shared: {', '.join(skipped)} already has a different chat with the same id"
    else:
        message = f"This chat is already shared with {', '.join(on)}."
    if skipped:
        message += f" Not added to {', '.join(skipped)}: it has a different chat with the same id."
    return True, message


def keep_only(profiles, chat_id, keep, root):
    """Make a shared chat a normal chat of `keep` again: it keeps the
    transcript and gets the chat's folders back; the other profiles lose it.
    (ok, message)."""
    groups = copies(profiles, chat_id)
    mine = next((g for g in groups if any(p.real == keep.real for p, _ in g)), None)
    if mine is None and len(groups) > 1:
        return False, f"Several profiles have different chats with this id; {keep.label} has none of them"
    chat = mine or (groups[0] if groups else None)
    if chat is None:
        return False, "That chat has no transcript on this computer"
    path = chat[0][1]
    key = path.parent.name
    if not PROJECT_KEY.match(key) or key in (".", ".."):
        return False, f"Unexpected project folder {key}; left the chat as it is"
    sharers = sorted((p for p, _ in chat), key=lambda p: p.real != keep.real)  # keep's own links first
    entries = {entry(t) for _, t in chat}
    shared = len(entries) > 1 or links(path) > 1 or any(
        in_profile(p, key, chat_id, part).is_symlink() for p in sharers for part in FOLDERS)
    if not shared:
        if any(len({entry(t) for _, t in g}) > 1 for g in groups):
            return False, f"{keep.label} has its own, different copy of this chat; left everything as it is"
        return False, "This chat isn't shared"
    kept = in_profile(keep, key, chat_id, TRANSCRIPT)
    if not present(kept) and elsewhere(path, [(keep.label, kept)]):
        return False, (f"Can't keep it only on {keep.label}: its folder is on another disk or partition than this "
                       f"chat, and the chat would have to be copied there. Left everything as it is")
    store = store_paths(sharers, key, chat_id, root)
    own_dirs = [p.real for p in profiles]
    moved = {}  # part: where its folder was
    removed = []
    old_umask = os.umask(0o077)
    try:
        # keep gets the chat's folders back
        for part in FOLDERS:
            here = in_profile(keep, key, chat_id, part)
            if here.is_symlink():
                was = target(here)
                os.unlink(str(here))
                if was.is_dir():
                    shutil.move(str(was), str(here))
                moved[part] = was
            elif not present(here) and store[part].is_dir():
                here.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(store[part]), str(here))
                moved[part] = store[part]
            elif here.is_dir() and store[part].is_dir() and entry(store[part]) != entry(here):
                # A folder of its own stays; what the others kept in the store
                # (their subagents, checkpoints of this chat) joins it
                merge_into(store[part], here)
                moved[part] = store[part]
        if not present(kept):
            link_into(kept, path)
        # the others lose it (not keep's own entry, seen through a linked folder)
        for q in profiles:
            if q.real == keep.real:
                continue
            gone = False
            t = in_profile(q, key, chat_id, TRANSCRIPT)
            if not t.is_symlink() and t.is_file() and same(t, kept) and entry(t) != entry(kept):
                os.unlink(str(t))
                gone = True
            for part in FOLDERS:
                link = in_profile(q, key, chat_id, part)
                if link.is_symlink() and target(link) in (moved.get(part), store[part]):
                    os.unlink(str(link))
                    gone = True
            if gone:
                removed.append(q.label)
        # and so does the store
        stored = store[TRANSCRIPT]
        if (entry(stored) != entry(kept) and not any(inside(stored, d) for d in own_dirs) and stored.is_file()
                and same(stored, kept)):
            os.unlink(str(stored))
        for folder in {stored.parent, *(m.parent for m in moved.values())}:
            if not any(inside(folder, d) for d in own_dirs):
                try:
                    folder.rmdir()  # only if it's empty now
                except OSError:
                    pass
    except OSError as exc:
        return False, f"Couldn't finish unsharing the chat: {exc}"
    finally:
        os.umask(old_umask)
    message = f"This chat is now a normal chat of {keep.label} again."
    if removed:
        message += f" Removed from {', '.join(removed)}."
    return True, message


# ---------------------------------------------------------------- plans
#
# A plan (<cfg>/plans/<slug>.md, what a chat in plan mode writes) is shared
# the same way: one file hard-linked into each profile's plans/ folder, so a
# chat continued on another account finds its plan there, and a change shows
# on every account (Claude writes plans in place). Sharing a chat shares its
# plan too.


def plan_copies(profiles, slug):
    """[(profile, path)] of the profiles that have the plan called slug."""
    out = []
    for p in profiles:
        path = cs.plan_path(p, slug)
        if path is not None and path.is_file() and not path.is_symlink():
            out.append((p, path))
    return out


def share_plan(profiles, slug, into, source=None):
    """Link the plan called slug into the profiles `into`, from source's copy
    (else the newest one): (ok, message)."""
    have = plan_copies(profiles, slug)
    if not have:
        return False, "That plan isn't there any more"
    src = next((path for p, path in have if source is not None and p.real == source.real), None)
    if src is None:
        src = max(have, key=lambda c: cs.mtime(c[1]) or 0)[1]
    added, skipped, apart = [], [], []
    old_umask = os.umask(0o077)
    try:
        for q in into:
            dest = cs.plan_path(q, slug)
            if present(dest):
                if not same(dest, src):
                    skipped.append(q.label)  # a plan of its own with that name
                continue
            if device(dest) != device(src):
                apart.append(q.label)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.link(str(src), str(dest))
            added.append(q.label)
    except OSError as exc:
        return False, f"Couldn't share the plan: {exc}"
    finally:
        os.umask(old_umask)
    on = [p.label for p in profiles if same(cs.plan_path(p, slug), src)]
    if not added and (skipped or apart):
        why = (f"{', '.join(skipped)} already {'has' if len(skipped) == 1 else 'have'} another plan of that name"
               if skipped else f"{', '.join(apart)} {'is' if len(apart) == 1 else 'are'} on another disk or partition")
        return False, f"Not shared: {why}"
    message = f"Plan shared: it's on {', '.join(on)} now." if added else f"This plan is on {', '.join(on)} already."
    if skipped:
        message += f" Not on {', '.join(skipped)}: there's another plan of that name there."
    if apart:
        message += f" Not on {', '.join(apart)}: another disk or partition."
    return True, message


def keep_plan_only(profiles, slug, keep):
    """The plan called slug stays on keep only; the other profiles' links to
    it go (a plan of their own with that name stays): (ok, message)."""
    have = plan_copies(profiles, slug)
    kept = next((path for p, path in have if p.real == keep.real), None)
    if kept is None:
        return False, f"{keep.label} doesn't have this plan"
    removed = []
    try:
        for q, path in have:
            if q.real != keep.real and same(path, kept) and entry(path) != entry(kept):
                os.unlink(str(path))
                removed.append(q.label)
    except OSError as exc:
        return False, f"Couldn't finish: {exc}"
    if not removed:
        return False, "This plan isn't shared"
    return True, f"The plan is only on {keep.label} now. Removed from {', '.join(removed)}."


def share_chat_plan(profiles, chat_id, into):
    """With a chat, its plan (best effort: a chat without one, or a plan
    that can't be linked, just stays as it is)."""
    for p in profiles:
        path = cs.transcript_path(p, chat_id)
        slug = cs.transcript_slug(path, path.stat().st_size) if path is not None and path.is_file() else None
        if slug and cs.plan_path(p, slug).is_file():
            try:
                share_plan(profiles, slug, into, p)
            except OSError:
                pass
            return


# ---------------------------------------------------------------- what the buttons do


def open_in(profiles, chat_id):
    """Label of the profile where a running session has this chat open, or None."""
    for entry, runs_on, _holder in cs.live_entries_all(profiles):
        if entry.get("sessionId") == chat_id and entry.get("kind") in cs.SESSION_KINDS and not entry.get("spare"):
            return runs_on.label
    return cs.shared_holder(chat_id, profiles)


def named(profiles, name):
    """The profile called name (a folder name like .claude-work, or a path), or None."""
    return next((p for p in profiles if name and cs.matches_profile(p, name)), None)


def give_to(profiles, chat_id, name, root, end_first=False, home=None):
    """Give the profile `name` a chat, shared if it doesn't have it yet:
    (its profile, its transcript of the chat), or (None, why not). With
    end_first, a session that has it open idle (e.g. stopped by a usage
    limit) is ended first (talpy_den.end_for_handover)."""
    target = named(profiles, name)
    if target is None:
        return None, f"No Claude profile called {name}"
    if end_first:
        ok, message = cs.end_for_handover(home or Path.home(), profiles, chat_id)
        if not ok:
            return None, message
    busy = open_in(profiles, chat_id)
    if busy:
        return None, f"It's open in a session on {busy}. A chat can be open in one session at a time: exit that one first."
    paths = [path for p in profiles for path in [cs.transcript_path(p, chat_id)] if path]
    if not paths:
        return None, "That chat has no transcript on this computer"
    there = cs.transcript_path(target, chat_id)
    if there is None:
        ok, message = share(profiles, chat_id, [target], root)
        if not ok:
            return None, message
        there = cs.transcript_path(target, chat_id)
        if there is None:
            return None, f"It wasn't added to {target.label}: {message}"
    elif any(not os.path.samefile(there, other) for other in paths):
        return None, f"{target.label} has a different chat with the same id; left everything as it is"
    share_chat_plan(profiles, chat_id, [target])
    return target, there


def continue_command(profiles, chat_id, name, root, home, claude, end_first=False):
    """Continue a chat on the profile `name`: share it with that profile if
    needed, and hand back the shell command that reopens it there (claude:
    how to run claude): (ok, message, command or None)."""
    target, there = give_to(profiles, chat_id, name, root, end_first, home)
    if target is None:
        return False, there, None
    folder = cs.chat_folder(there)
    command = cs.in_folder(folder, cs.resume_command(target, chat_id, home, folder, claude))
    return True, f"Continuing it on {target.label}", command


def share_with_all(profiles, chat_id, root, name=None):
    """Share a chat with every profile (from the copy of the profile `name`, if given)."""
    busy = open_in(profiles, chat_id)
    if busy:
        return False, f"It's open in a session on {busy}. Exit that session first."
    ok, message = share(profiles, chat_id, profiles, root, named(profiles, name))
    if ok:
        share_chat_plan(profiles, chat_id, profiles)
    return ok, message


def keep_only_on(profiles, chat_id, name, root):
    """Make a shared chat a normal chat of the profile `name` again; the others lose it."""
    keep = named(profiles, name)
    if keep is None:
        return False, f"No Claude profile called {name}"
    busy = open_in(profiles, chat_id)
    if busy:
        return False, f"It's open in a session on {busy}. Exit that session first."
    return keep_only(profiles, chat_id, keep, root)


def plan_sharing(profiles, slug, name, share_it, to=None):
    """--share-plan (from the profile name's copy, with every profile or only
    to) and --unshare-plan (keep it on name only): (ok, message)."""
    if not cs.PLAN_SLUG.match(slug or ""):
        return False, f"Not a plan name: {slug}"
    source = named(profiles, name) if name else None
    if name and source is None:
        return False, f"No Claude profile called {name}"
    if not share_it:
        return keep_plan_only(profiles, slug, source)
    into = profiles
    if to:
        target = named(profiles, to)
        if target is None:
            return False, f"No Claude profile called {to}"
        into = [target]
    return share_plan(profiles, slug, into, source)


USAGE = ("usage: sharing.py (--share CHAT_ID | --continue CHAT_ID [--end-first] | --unshare CHAT_ID) [--profile PROFILE]\n"
         "       sharing.py (--share-plan SLUG [--to PROFILE] | --unshare-plan SLUG) [--profile PROFILE]\n"
         "                  [--config-dir DIR]... [--remote]   (--continue, --unshare and --unshare-plan need --profile)")


def main(argv=None):
    """On an SSH host (remote.py): prints {"ok", "message"} and, for
    --continue, "command"."""
    args = sys.argv[1:] if argv is None else list(argv)
    action, chat_id, name, extra, remote, end_first, to = None, None, None, [], False, False, None
    while args:
        arg = args.pop(0)
        if arg in ("--share", "--continue", "--unshare", "--share-plan", "--unshare-plan") and args and action is None:
            action, chat_id = arg, args.pop(0)
        elif arg == "--profile" and args:
            name = args.pop(0)
        elif arg == "--to" and args:
            to = args.pop(0)
        elif arg == "--end-first":
            end_first = True
        elif arg == "--config-dir" and args:
            extra.append(args.pop(0))
        elif arg == "--remote":
            remote = True
        else:
            sys.exit(USAGE)
    if action is None or (action not in ("--share", "--share-plan") and not name):
        sys.exit(USAGE)
    home, command = Path.home(), None
    try:
        profiles, root = cs.discover_profiles(home, extra), store_root(home)
        if action == "--share":
            ok, message = share_with_all(profiles, chat_id, root, name)
        elif action == "--unshare":
            ok, message = keep_only_on(profiles, chat_id, name, root)
        elif action in ("--share-plan", "--unshare-plan"):
            ok, message = plan_sharing(profiles, chat_id, name, action == "--share-plan", to)
        else:
            # A click: the login shell may be asked where claude is
            ok, message, command = continue_command(profiles, chat_id, name, root, home,
                                                    cs.find_claude(home, ask_shell=True), end_first)
    except Exception as exc:  # report, don't crash: the widget shows the message
        ok, message = False, f"{type(exc).__name__}: {exc}"
    if remote:
        # Login scripts on the host may print things first: the JSON follows a marker
        sys.stdout.write("\n" + cs.REMOTE_MARK + " ")
    # ASCII: a lone surrogate in a name can't break the answer
    json.dump({"ok": ok, "message": message, **({"command": command} if command else {})}, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
