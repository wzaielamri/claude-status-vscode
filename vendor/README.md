# vendor/

Copied from the [Talpy Den widget](https://github.com/wzaielamri/talpy-den) (KDE Plasma and GNOME)
by `scripts/sync-widget.sh`, at the commit in `WIDGET_COMMIT`. Change these
files in the widget and sync again, so the two never drift apart.

- `code/` — the Python backend (standard library only, Python 3.7+):
  `talpy_den.py` reads Claude Code's own files and prints the status as
  JSON, `remote.py` runs it on SSH hosts, `jump.py` opens, stops and shares
  chats, `notify_sound.py` plays notification sounds.
- `pets/` — the Clawd pet art, MIT © Abderrahim Ghazali (see `pets/LICENSE`).
- `statusline-snippet.sh` — what the usage-bars setup adds to a Claude Code
  status line.
