// VS Code terminals for chats: on this computer, on the host this window is
// connected to (Remote-SSH), or on another host through ssh -t. The extension
// runs on this computer even in a Remote-SSH window ("extensionKind": ["ui"]):
// a terminal created with a file: cwd runs here, one without a cwd runs on
// the window's host.

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as vscode from "vscode";
import { shellQuote } from "./shared/model";
import { controlOptions } from "./sshControl";

export type Place = { kind: "local" } | { kind: "windowHost" } | { kind: "ssh"; host: string };

// The same connection sharing as remote.py, without batch mode: ssh may ask
// for a password or a host key in the terminal. Like remote.py, it leaves out
// the host's own RemoteCommand and port forwards, and has sh run the command
// (it's POSIX shell; the login shell there may be csh or fish).
export function sshCommand(host: string, command: string): string {
  const opts = [...controlOptions(), "-o", "ServerAliveInterval=10", "-o", "RemoteCommand=none", "-o", "ClearAllForwardings=yes"];
  return ["ssh", ...opts, "-t", host, "sh -c " + shellQuote(command)].map(shellQuote).join(" ");
}

const JUST_STARTED_MS = 10000; // a second click this soon shows the terminal the first one opened

// Parent of a process here, from /proc (Linux); null when unknown
export function parentPid(pid: number): number | null {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
    // pid (comm) state ppid …; comm may hold spaces and parentheses
    const fields = stat.slice(stat.lastIndexOf(")") + 2).split(" ");
    const ppid = Number(fields[1]);
    return Number.isInteger(ppid) && ppid > 0 ? ppid : null;
  } catch {
    return null;
  }
}

export function localAncestors(pid: number): Set<number> {
  const out = new Set<number>();
  for (let p: number | null = pid; p && p > 1 && !out.has(p) && out.size < 64; p = parentPid(p)) out.add(p);
  return out;
}

// The `claude attach <jobId>` processes here: terminals showing that
// background job (jump.py's attach_clients)
export function attachClients(jobId: string, procDir = "/proc"): number[] {
  const out: number[] = [];
  let entries: string[];
  try {
    entries = fs.readdirSync(procDir);
  } catch {
    return out; // not Linux
  }
  for (const p of entries) {
    if (!/^\d+$/.test(p)) continue;
    try {
      if (fs.readFileSync(`${procDir}/${p}/comm`, "utf8").trim() !== "claude") continue;
      const argv = fs.readFileSync(`${procDir}/${p}/cmdline`, "utf8").split("\0");
      if (argv[1] === "attach" && argv[2] === jobId) out.push(Number(p));
    } catch {
      // gone, or not ours to read
    }
  }
  return out;
}

function hasChildren(pid: number): boolean {
  try {
    for (const task of fs.readdirSync(`/proc/${pid}/task`)) {
      if (fs.readFileSync(`/proc/${pid}/task/${task}/children`, "utf8").trim()) return true;
    }
  } catch {
    // not Linux, or gone
  }
  return false;
}

// The widgets' link to a chat's terminal (jump.py's TERMINAL_LINK):
// vscode://wzaielamri.talpy-den/terminal?pid=<the process the terminal
// started>[&host=<the SSH host it runs on, for a Remote-SSH window>]. VS Code
// itself can't show one of its terminals from outside, nor bring the panel
// back once you closed it.
export interface TerminalLink {
  pid: number;
  host?: string;
}

export function terminalLinkOf(uri: { path: string; query: string }): TerminalLink | null {
  if (uri.path.replace(/\/+$/, "") !== "/terminal") return null;
  const q = new URLSearchParams(uri.query);
  const pid = Number(q.get("pid"));
  if (!/^\d+$/.test(q.get("pid") ?? "") || !Number.isSafeInteger(pid) || pid <= 1) return null;
  const host = q.get("host");
  return host ? { pid, host } : { pid };
}

export class Terminals implements vscode.Disposable {
  private local = new WeakSet<vscode.Terminal>(); // the ones made here on this computer
  private byKey = new Map<string, vscode.Terminal>();
  private startedAt = new WeakMap<vscode.Terminal, number>();
  private subscription: vscode.Disposable;

  constructor() {
    this.subscription = vscode.window.onDidCloseTerminal((t) => {
      for (const [k, v] of this.byKey) if (v === t) this.byKey.delete(k);
    });
  }

  dispose(): void {
    this.subscription.dispose();
  }

  private isLocal(t: vscode.Terminal): boolean {
    return !vscode.env.remoteName || this.local.has(t);
  }

  // The terminal of this window that the process runs in (one of its ancestors
  // is the terminal's shell); ancestors are this computer's or the window host's.
  async find(ancestors: Set<number>, onWindowHost: boolean): Promise<vscode.Terminal | undefined> {
    if (!ancestors.size) return undefined;
    for (const t of vscode.window.terminals) {
      if (t.exitStatus) continue;
      if (this.isLocal(t) === onWindowHost) continue;
      const pid = await t.processId;
      if (pid && ancestors.has(pid)) return t;
    }
    return undefined;
  }

  // Show the terminal a widget's link names (windowHost: the host this window
  // is connected to, if known); false when this window has none such
  async showLinked(link: TerminalLink, windowHost: string | undefined): Promise<boolean> {
    if (link.host && windowHost && link.host !== windowHost) return false;
    const t = await this.find(new Set([link.pid]), !!link.host);
    t?.show();
    return !!t;
  }

  // Run command in a new terminal, or show the one still running it for key
  // (a local terminal whose command hasn't finished: its shell has a child).
  // private: VS Code doesn't keep it (a login: its screen isn't saved for
  // restoring the terminal after a reload, nor the terminal restored).
  async run(key: string, name: string, command: string, place: Place, opts: { private?: boolean } = {}): Promise<vscode.Terminal> {
    const existing = this.byKey.get(key);
    if (existing && !existing.exitStatus) {
      // just opened (its command may not have started yet, and a host's can't
      // be looked at), or here and still running its command
      const fresh = Date.now() - (this.startedAt.get(existing) ?? 0) < JUST_STARTED_MS;
      const pid = fresh || !this.local.has(existing) ? undefined : await existing.processId;
      if (fresh || (pid && hasChildren(pid))) {
        existing.show();
        return existing;
      }
    }
    const line = place.kind === "ssh" ? sshCommand(place.host, command) : command;
    // Ctrl+C belongs to Claude, not to the wrapper; the shell stays afterwards.
    const script = `trap : INT; ${line}; exec "\${SHELL:-/bin/sh}"`;
    const options: vscode.TerminalOptions = {
      name: `Claude: ${name}`.slice(0, 48),
      iconPath: new vscode.ThemeIcon("sparkle"),
      shellPath: "/bin/sh",
      shellArgs: ["-c", script],
      ...(opts.private ? { isTransient: true } : {}),
    };
    if (place.kind !== "windowHost") options.cwd = vscode.Uri.file(os.homedir());
    const t = vscode.window.createTerminal(options);
    if (place.kind !== "windowHost") this.local.add(t);
    this.byKey.set(key, t);
    this.startedAt.set(t, Date.now());
    t.show();
    return t;
  }
}
