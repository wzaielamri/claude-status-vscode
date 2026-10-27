#!/usr/bin/env python3
"""Play a notification sound for the Talpy Den widget.

    python3 -I notify_sound.py SOUND [--volume PERCENT] [--force] [--theme NAME]

SOUND is a name from the sound theme (message-new-instant, complete,
dialog-error, ... as in the freedesktop sound naming spec; several, comma
separated, play the first one found) or the path of a sound file. The theme is the desktop's (gtk-sound-theme-name, which Plasma
keeps in sync; Ocean by default), falling back to the freedesktop theme.
--theme names it instead (GNOME keeps it in its own settings).

Nothing plays while Do Not Disturb is on, unless --force (the settings page's
preview button). The player is started in the background: PipeWire's pw-play
with the "Notification" role (so the system's notification volume applies),
else paplay, else canberra-gtk-play. Prints one JSON line,
{"ok": bool, "message": str}.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

EXTENSIONS = (".oga", ".ogg", ".wav")
DEFAULT_THEME = "ocean"


def data_dirs():
    home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    others = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    return [Path(d) / "sounds" for d in [home, *others.split(":")] if d]


def theme_name():
    """The desktop's sound theme (GTK setting, which Plasma keeps in sync)."""
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    for name in ("gtk-4.0/settings.ini", "gtk-3.0/settings.ini"):
        try:
            for line in (config / name).read_text(encoding="utf-8", errors="replace").splitlines():
                key, sep, value = line.partition("=")
                if sep and key.strip() == "gtk-sound-theme-name" and value.strip():
                    return value.strip()
        except OSError:
            continue
    return DEFAULT_THEME


def inherited(theme, dirs):
    """theme, the themes it inherits from, then freedesktop (the spec's fallback)."""
    order, todo = [], [theme]
    while todo:
        name = todo.pop(0)
        if not name or name in order or "/" in name:
            continue
        order.append(name)
        for d in dirs:
            try:
                text = (d / name / "index.theme").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                if line.startswith("Inherits="):
                    todo += [t.strip() for t in line[len("Inherits="):].split(",")]
            break
    if "freedesktop" not in order:
        order.append("freedesktop")
    return order


def find_sound(sound, dirs=None, theme=None):
    """Path of the sound file to play, or None. sound can list alternatives,
    comma separated: the first one found plays."""
    if not sound:
        return None
    if "," in sound and "/" not in sound:
        return next((p for p in (find_sound(s.strip(), dirs, theme) for s in sound.split(",")) if p), None)
    if sound.startswith("file://"):  # from a file dialog
        sound = unquote(sound[len("file://"):])
    if "/" in sound:
        path = Path(sound).expanduser()
        return path if path.is_file() else None
    dirs = dirs if dirs is not None else data_dirs()
    for name in inherited(theme or theme_name(), dirs):
        for d in dirs:
            for sub in ("stereo", ""):
                for ext in EXTENSIONS:
                    path = d / name / sub / f"{sound}{ext}"
                    if path.is_file():
                        return path
    return None


def do_not_disturb():
    """Is Do Not Disturb on (the notification server's Inhibited property)?"""
    gdbus = shutil.which("gdbus")
    if not gdbus:
        return False
    try:
        r = subprocess.run([gdbus, "call", "--session", "--dest", "org.freedesktop.Notifications",
                            "--object-path", "/org/freedesktop/Notifications",
                            "--method", "org.freedesktop.DBus.Properties.Get",
                            "org.freedesktop.Notifications", "Inhibited"],
                           capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and "true" in r.stdout


def player_command(path, volume):
    """Command line playing path at volume (0..100), or None."""
    volume = max(0, min(100, volume))
    if shutil.which("pw-play"):
        return ["pw-play", "--media-role=Notification", f"--volume={volume / 100:.2f}", str(path)]
    if shutil.which("paplay"):
        return ["paplay", f"--volume={int(volume / 100 * 65536)}", str(path)]
    if shutil.which("canberra-gtk-play"):
        import math
        db = 20 * math.log10(volume / 100) if volume > 0 else -60
        return ["canberra-gtk-play", "-f", str(path), "-V", f"{db:.1f}", "-d", "Talpy Den"]
    if shutil.which("aplay") and path.suffix == ".wav":
        return ["aplay", "-q", str(path)]
    return None


def play(sound, volume=100, force=False, theme=None):
    if volume <= 0:
        return True, "Muted"
    if not force and do_not_disturb():
        return True, "Quiet: Do Not Disturb is on"
    path = find_sound(sound, theme=theme)
    if path is None:
        return False, f"No sound file found for {sound!r}"
    command = player_command(path, volume)
    if command is None:
        return False, "No sound player found (pw-play, paplay or canberra-gtk-play)"
    try:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return False, f"Couldn't start {command[0]}: {exc}"
    return True, f"Playing {path.name}"


def main(argv=None):
    args = sys.argv[1:] if argv is None else list(argv)
    sound, volume, force, theme = None, 100, False, None
    while args:
        arg = args.pop(0)
        if arg == "--volume" and args:
            try:
                volume = int(float(args.pop(0)))
            except ValueError:
                sys.exit(__doc__)
        elif arg == "--force":
            force = True
        elif arg == "--theme" and args:
            theme = args.pop(0) or None
        elif sound is None:
            sound = arg
        else:
            sys.exit(__doc__)
    if not sound:
        sys.exit(__doc__)
    try:
        ok, message = play(sound, volume, force, theme) if theme else play(sound, volume, force)
    except Exception as exc:  # report, don't crash
        ok, message = False, f"{type(exc).__name__}: {exc}"
    print(json.dumps({"ok": ok, "message": message}))


if __name__ == "__main__":
    main()
