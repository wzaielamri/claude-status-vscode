// Plan usage bars come from Claude Code's status line: the snippet
// (vendor/statusline-snippet.sh) keeps a copy of what Claude passes it. This
// adds the snippet to each profile's status line, like the widget's install.sh
// (same marker block, so the two never add it twice):
// - a status-line script (*.sh) that reads its input with input=$(cat) gets
//   the snippet right after that line;
// - a profile without a status line, or with another kind of command, gets a
//   small wrapper script as its status line, which saves the copy and then
//   runs the previous command, so the status line looks as it did.
// Every change is shown first, asked for, and backed up.

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as vscode from "vscode";
import { runProcess } from "./backend";
import { expandHome, extraConfigDirs } from "./config";
import { shellQuote } from "./shared/model";

export const MARK_START = "# >>> talpy-den >>>";
// (an older copy of it is named claude-widget, after the first name)
const BLOCK = /^# >>> (talpy-den|claude-widget) >>>\n[\s\S]*?^# <<< \1 <<<\n/m;
// input=$(cat), input="$(cat)", input=$(cat -): a line of its own (not one
// that goes on with a \ or a trailing && or |)
const INPUT_LINE = /^[ \t]*input=("?)\$\(cat(?:[ \t]+-)?\)\1[ \t]*(?:;[ \t]*)?(?:#[^\n]*)?(?:\n|$)/m;

export interface Change {
  file: string;
  before: string; // "" for a new file
  after: string;
  why: string;
  mode?: number;
}

function wrapperIn(folder: string, env: NodeJS.ProcessEnv): string {
  return path.join(env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share"), folder, "statusline.sh");
}

export function wrapperPath(env: NodeJS.ProcessEnv = process.env): string {
  return wrapperIn("talpy-den", env);
}

// Where a wrapper may have been put: here, or where it goes without
// XDG_DATA_HOME (set up from a VS Code started another way), or in the folder
// of the first name (claude-status): a status line that names that one is
// pointed at this one when the usage bars are set up again
function wrapperPaths(env: NodeJS.ProcessEnv = process.env): string[] {
  return [...new Set([wrapperPath(env), wrapperPath({}), wrapperIn("claude-status", env), wrapperIn("claude-status", {})])];
}

export function wrapperScript(snippet: string): string {
  return `#!/usr/bin/env bash
# Talpy Den (VS Code): keeps a copy of the status-line input for the plan
# usage bars, then shows the status line this profile had before, if any (its
# command is the first argument). Remove it with "Talpy Den: Remove the
# Plan Usage Status-Line Snippet".
input=$(cat)
${snippet.trimEnd()}
if [ -n "\${1:-}" ]; then
  printf '%s' "$input" | bash -c "$1"
fi
`;
}

// The snippet added after the input=$(cat) line, or null if there's none. An
// older copy of it is replaced (the first ones only ran in bash scripts).
export function patchScript(text: string, snippet: string): string | null {
  if (BLOCK.test(text)) return text.replace(BLOCK, () => snippet.trimEnd() + "\n");
  const m = INPUT_LINE.exec(text);
  if (!m) return null;
  const at = m.index + m[0].length;
  const line = m[0].endsWith("\n") ? "" : "\n"; // the script's last line, without its newline
  return text.slice(0, at) + line + snippet.trimEnd() + "\n" + text.slice(at);
}

export function unpatchScript(text: string): string {
  return text.replace(BLOCK, "");
}

// "WRAPPER 'previous command'" -> "previous command"; "WRAPPER" -> ""; else null
export function unwrapCommand(command: string, wrapper: string | string[]): string | null {
  const forms = (Array.isArray(wrapper) ? wrapper : [wrapper]).flatMap((w) => [w, shellQuote(w), w.replace(os.homedir(), "~")]);
  for (const w of forms) {
    if (command === w) return "";
    if (command.startsWith(w + " ")) {
      const rest = command.slice(w.length + 1).trim();
      if (rest.startsWith("'") && rest.endsWith("'")) return rest.slice(1, -1).replace(/'"'"'/g, "'");
      return rest;
    }
  }
  return null;
}

// The status line's own script, if its command runs one
export function scriptOf(command: string): string | null {
  for (const word of command.split(/\s+/)) {
    const w = expandHome(word.replace(/^["']|["']$/g, "").replace(/^\$\{?HOME\}?(?=\/)/, os.homedir()));
    if (w.endsWith(".sh") && fs.existsSync(w)) return fs.realpathSync(w);
  }
  return null;
}

export function profileDirs(): string[] {
  const home = os.homedir();
  const dirs = [path.join(home, ".claude")];
  try {
    for (const name of fs.readdirSync(home).sort()) if (name.startsWith(".claude-")) dirs.push(path.join(home, name));
  } catch {
    // no home listing
  }
  dirs.push(...extraConfigDirs());
  const seen = new Set<string>();
  return dirs.filter((d) => {
    try {
      const st = fs.statSync(d);
      const real = fs.realpathSync(d);
      // (a new one: .profile-name, or Claude's .claude.json once you've started to log in)
      const isProfile =
        st.isDirectory() && ["sessions", "projects", "settings.json", ".profile-name", ".claude.json"].some((f) => fs.existsSync(path.join(d, f)));
      if (!isProfile || seen.has(real)) return false;
      seen.add(real);
      return true;
    } catch {
      return false;
    }
  });
}

// The file a link leads to (also a dangling one), so that a settings.json that
// is a link stays one, and profiles that link one file share one change
function throughLinks(file: string): string {
  let f = file;
  for (let i = 0; i < 20; i++) {
    try {
      return fs.realpathSync(f);
    } catch {
      try {
        if (!fs.lstatSync(f).isSymbolicLink()) return f;
        f = path.resolve(path.dirname(f), fs.readlinkSync(f));
      } catch {
        return f;
      }
    }
  }
  return f;
}

function readJson(file: string): Record<string, any> | null {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (e) {
    return (e as NodeJS.ErrnoException).code === "ENOENT" ? {} : null;
  }
}

function indentOf(text: string): number {
  const m = /\n( +)"/.exec(text);
  return m ? m[1].length : 2;
}

function withStatusLine(file: string, before: string, command: string | null): string {
  const data = before ? JSON.parse(before) : {};
  if (command === null) delete data.statusLine;
  else data.statusLine = { ...(data.statusLine ?? {}), type: "command", command };
  return JSON.stringify(data, null, indentOf(before)) + "\n";
}

// What adding (or removing) the snippet would change, profile by profile
export function plan(snippet: string, remove: boolean): { changes: Change[]; notes: string[] } {
  const changes = new Map<string, Change>();
  const notes: string[] = [];
  const wrapper = wrapperPath();
  const wrappers = wrapperPaths();
  const wantWrapper = (): void => {
    // the wrapper that runs: written (or brought up to date) before any
    // status line points at it
    if (changes.has(wrapper)) return;
    const existing = fs.existsSync(wrapper) ? fs.readFileSync(wrapper, "utf8") : "";
    const after = wrapperScript(snippet);
    if (existing !== after) changes.set(wrapper, { file: wrapper, before: existing, after, why: "the wrapper script", mode: 0o755 });
  };
  for (const dir of profileDirs()) {
    const label = dir.replace(os.homedir(), "~");
    const settingsFile = throughLinks(path.join(dir, "settings.json"));
    if (changes.has(settingsFile)) continue; // shared with a profile planned already
    const settings = readJson(settingsFile);
    if (settings === null) {
      notes.push(`${label}: settings.json isn't valid JSON; left alone.`);
      continue;
    }
    const before = fs.existsSync(settingsFile) ? fs.readFileSync(settingsFile, "utf8") : "";
    const command: string = typeof settings.statusLine?.command === "string" ? settings.statusLine.command.trim() : "";
    const previous = command ? unwrapCommand(command, wrappers) : null;
    if (remove) {
      if (previous !== null) {
        changes.set(settingsFile, {
          file: settingsFile,
          before,
          after: withStatusLine(settingsFile, before, previous || null),
          why: `${label}: ${previous ? "back to the previous status line" : "no status line again"}`,
        });
        continue;
      }
      const script = command ? scriptOf(command) : null;
      if (script && !changes.has(script)) {
        const text = fs.readFileSync(script, "utf8");
        if (BLOCK.test(text))
          changes.set(script, { file: script, before: text, after: unpatchScript(text), why: `${label}: snippet removed from its status line` });
      }
      continue;
    }
    if (previous !== null) {
      if (unwrapCommand(command, wrapper) === null) {
        // set up with the wrapper somewhere else: point it at this one
        changes.set(settingsFile, {
          file: settingsFile,
          before,
          after: withStatusLine(settingsFile, before, previous ? `${shellQuote(wrapper)} ${shellQuote(previous)}` : shellQuote(wrapper)),
          why: `${label}: its status line's wrapper moved`,
        });
      } else notes.push(`${label}: already set up.`);
      wantWrapper(); // gone, or from an older snippet: written again
      continue;
    }
    const script = command ? scriptOf(command) : null;
    if (script) {
      if (changes.has(script)) continue; // a script shared by several profiles is patched once
      const text = fs.readFileSync(script, "utf8");
      const patched = patchScript(text, snippet);
      if (patched === text) {
        notes.push(`${label}: already set up.`);
        continue;
      }
      if (patched !== null) {
        const why = BLOCK.test(text) ? "snippet in its status-line script updated" : "snippet added to its status-line script";
        changes.set(script, { file: script, before: text, after: patched, why: `${label}: ${why}` });
        continue;
      }
    }
    const newCommand = command ? `${shellQuote(wrapper)} ${shellQuote(command)}` : shellQuote(wrapper);
    changes.set(settingsFile, {
      file: settingsFile,
      before,
      after: withStatusLine(settingsFile, before, newCommand),
      why: command ? `${label}: status line wrapped (it keeps showing the same)` : `${label}: a status line that only saves the usage (shows nothing)`,
    });
    wantWrapper();
  }
  // the wrapper first: a status line must never point at a script not written yet
  const ordered = [...changes.values()].sort((a, b) => Number(b.file === wrapper) - Number(a.file === wrapper));
  return { changes: ordered, notes };
}

function stamp(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

// Each file is written next to itself, then moved over it in one step: Claude
// may run a status-line script (or read settings.json) at any moment
export function apply(changes: Change[]): void {
  for (const c of changes) {
    fs.mkdirSync(path.dirname(c.file), { recursive: true });
    let mode = c.mode;
    if (c.before) {
      fs.copyFileSync(c.file, `${c.file}.bak.${stamp()}`);
      mode ??= fs.statSync(c.file).mode & 0o7777;
    }
    const tmp = path.join(path.dirname(c.file), `.${path.basename(c.file)}.${process.pid}.tmp`);
    try {
      fs.writeFileSync(tmp, c.after, { mode: mode ?? 0o644 });
      if (mode !== undefined) fs.chmodSync(tmp, mode);
      fs.renameSync(tmp, c.file);
    } catch (e) {
      fs.rmSync(tmp, { force: true });
      throw e;
    }
  }
}

// ---------------------------------------------------------------- commands

class Preview implements vscode.TextDocumentContentProvider {
  static scheme = "talpy-den-preview";
  texts = new Map<string, string>();
  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.texts.get(uri.path) ?? "";
  }
}

const preview = new Preview();

export function registerPreview(): vscode.Disposable {
  return vscode.workspace.registerTextDocumentContentProvider(Preview.scheme, preview);
}

async function showChanges(changes: Change[]): Promise<void> {
  for (const c of changes) {
    const name = path.basename(c.file);
    preview.texts.set(`/before/${c.file}`, c.before);
    preview.texts.set(`/after/${c.file}`, c.after);
    await vscode.commands.executeCommand(
      "vscode.diff",
      vscode.Uri.from({ scheme: Preview.scheme, path: `/before/${c.file}` }),
      vscode.Uri.from({ scheme: Preview.scheme, path: `/after/${c.file}` }),
      `${name} (proposed)`,
      { preview: false },
    );
  }
}

export async function setUpUsageBars(extensionUri: vscode.Uri, remove = false): Promise<void> {
  if (!remove) {
    const jq = await runProcess("jq", ["--version"], { timeoutMs: 10000 });
    if (jq.spawnError) {
      void vscode.window.showErrorMessage(
        "The usage bars need jq (it reads the status line's input). Install it (e.g. `sudo apt install jq`, `sudo dnf install jq`, `brew install jq`) and run this again.",
      );
      return;
    }
  }
  const snippet = fs.readFileSync(vscode.Uri.joinPath(extensionUri, "vendor", "statusline-snippet.sh").fsPath, "utf8");
  let result: ReturnType<typeof plan>;
  try {
    result = plan(snippet, remove);
  } catch (e) {
    void vscode.window.showErrorMessage(`Couldn't read the Claude Code settings: ${(e as Error).message}`);
    return;
  }
  const { changes, notes } = result;
  if (!changes.length) {
    void vscode.window.showInformationMessage(
      (remove ? "Nothing to remove." : "Plan usage bars are already set up.") + (notes.length ? " " + notes.join(" ") : ""),
    );
    return;
  }
  const detail =
    changes.map((c) => `• ${c.why}\n   ${c.file.replace(os.homedir(), "~")}`).join("\n") +
    (notes.length ? "\n\n" + notes.join("\n") : "") +
    "\n\nEach changed file is backed up next to itself (.bak.<time>).";
  for (;;) {
    const choice = await vscode.window.showInformationMessage(
      remove ? "Remove the plan-usage snippet from Claude Code's status line?" : "Set up plan usage bars? This changes Claude Code's status line:",
      { modal: true, detail },
      "Apply",
      "Show Changes",
    );
    if (choice === "Show Changes") {
      await showChanges(changes);
      continue;
    }
    if (choice !== "Apply") return;
    break;
  }
  try {
    // planned again from the files as they are now: what Claude Code (or you)
    // changed in them while this asked is kept
    apply(plan(snippet, remove).changes);
  } catch (e) {
    void vscode.window.showErrorMessage(`Couldn't change the files: ${(e as Error).message}`);
    return;
  }
  void vscode.window.showInformationMessage(
    remove ? "Removed. Usage bars stop updating." : "Done. Usage bars fill in after the next reply in each account's chats.",
  );
}
