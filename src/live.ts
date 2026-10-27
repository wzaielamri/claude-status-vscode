// The moment a chat changes. Claude rewrites a chat's session file
// (<config dir>/sessions/<pid>.json) whenever the chat starts or ends,
// starts working, waits for you or finishes its turn (also when you answer
// or interrupt it in its terminal), and live mode's hook (the widget's
// live_hook.py) notes each step in ~/.cache/talpy-den/live/<session
// id>.json. This watches both folders (fs.watch: inotify on Linux): a
// session file that changes asks for a check now, a live file is read and
// handed on. (The widget's live.py does the same for Plasma.)

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { LiveView } from "./shared/types";

// what a live file's last event must be to ask for a check now (live.py's URGENT_EVENTS)
const URGENT_EVENTS = new Set(["UserPromptSubmit", "Stop", "StopFailure", "PermissionRequest", "Notification", "SessionStart",
  "SessionEnd", "PreCompact", "SubagentStart", "SubagentStop"]);
const GATHER_MS = 50; // after a change: what comes with it (a session file and a live file)
const STEP_GAP_MS = 400; // steps alone are handed on at most this often
const RESCAN_MS = 60000; // a new profile's sessions folder is watched within this

export interface LiveChange {
  urgent: boolean; // a chat started, ended, works, waits or finished its turn: check now
  views: LiveView[]; // live files that changed
}

// Each Claude profile's sessions folder (~/.claude, ~/.claude-*, and the
// extra config dirs), once per real folder, as the backend finds them
export function sessionDirs(home: string, extra: string[]): string[] {
  let names: string[] = [];
  try {
    names = fs.readdirSync(home).filter((n) => n === ".claude" || n.startsWith(".claude-")).sort();
  } catch {
    names = [];
  }
  const out: string[] = [];
  for (const folder of [...names.map((n) => path.join(home, n)), ...extra]) {
    let real: string;
    try {
      real = fs.realpathSync(path.join(folder, "sessions"));
      if (!fs.statSync(real).isDirectory()) continue;
    } catch {
      continue;
    }
    if (!out.includes(real)) out.push(real);
  }
  return out;
}

export function liveDir(cacheDir: string): string {
  return path.join(cacheDir, "live");
}

export function isView(v: unknown): v is LiveView {
  return !!v && typeof v === "object" && typeof (v as LiveView).sessionId === "string" && Array.isArray((v as LiveView).steps);
}

export class LiveWatch {
  private watchers = new Map<string, fs.FSWatcher>();
  private files = new Set<string>(); // live files that changed, not read yet
  private urgent = false;
  private timer?: NodeJS.Timeout;
  private rescanTimer?: NodeJS.Timeout;
  private lastSteps = 0;
  private stopped = false;
  private failed = false;

  constructor(
    private readonly onChange: (change: LiveChange) => void,
    private readonly extraDirs: () => string[],
    private readonly cacheDir: string,
    private readonly home = os.homedir(),
  ) {}

  // Whether it watches everything: else the checks go on at their old pace
  get healthy(): boolean {
    return !this.stopped && !this.failed && this.watchers.has(liveDir(this.cacheDir));
  }

  start(): void {
    this.rescan();
    this.rescanTimer = setInterval(() => this.rescan(), RESCAN_MS);
  }

  dispose(): void {
    this.stopped = true;
    clearTimeout(this.timer);
    clearInterval(this.rescanTimer);
    for (const w of this.watchers.values()) w.close();
    this.watchers.clear();
  }

  // The folders to watch now (a profile can come or go)
  rescan(): void {
    if (this.stopped) return;
    const live = liveDir(this.cacheDir);
    try {
      fs.mkdirSync(live, { recursive: true, mode: 0o700 });
    } catch {
      // (watched below if it's there)
    }
    const wanted = [live, ...sessionDirs(this.home, this.extraDirs())];
    let failed = false;
    for (const dir of wanted) {
      if (this.watchers.has(dir)) continue;
      try {
        const w = fs.watch(dir, { persistent: false }, (_kind, name) => this.changed(dir, name ? String(name) : ""));
        w.on("error", () => {
          w.close();
          this.watchers.delete(dir); // (gone: the next rescan watches it again if it's back)
        });
        this.watchers.set(dir, w);
      } catch {
        failed = true; // e.g. out of inotify watches
      }
    }
    for (const [dir, w] of this.watchers) {
      if (!wanted.includes(dir)) {
        w.close();
        this.watchers.delete(dir);
      }
    }
    this.failed = failed;
  }

  private changed(dir: string, name: string): void {
    if (dir === liveDir(this.cacheDir)) {
      if (!name.endsWith(".json") || name.startsWith(".")) return;
      this.files.add(name);
    } else {
      this.urgent = true;
    }
    if (!this.timer) this.timer = setTimeout(() => void this.flush(), GATHER_MS);
  }

  private async flush(): Promise<void> {
    this.timer = undefined;
    if (this.stopped) return;
    const names = [...this.files];
    this.files.clear();
    const views: LiveView[] = [];
    for (const name of names) {
      try {
        const v: unknown = JSON.parse(await fs.promises.readFile(path.join(liveDir(this.cacheDir), name), "utf8"));
        if (isView(v)) views.push(v);
      } catch {
        // (gone, or half there: the next change brings it)
      }
    }
    const urgent = this.urgent || views.some((v) => URGENT_EVENTS.has(v.event ?? ""));
    const now = Date.now();
    if (!urgent && views.length && now - this.lastSteps < STEP_GAP_MS) {
      // steps alone, just after the last ones: a moment later, with what comes meanwhile
      for (const name of names) this.files.add(name);
      this.timer = setTimeout(() => void this.flush(), STEP_GAP_MS - (now - this.lastSteps));
      return;
    }
    this.urgent = false;
    if (!urgent && !views.length) return;
    if (views.length) this.lastSteps = now;
    this.onChange({ urgent, views });
  }
}
