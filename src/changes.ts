// What a chat changed, side by side in VS Code's changes view (several files
// in one editor). The widget's backend (jump.py --changes) finds the files
// and copies what they were before the chat (or before its last turn) into
// the cache; the Plasma and GNOME widgets hand such a comparison over with
// vscode://wzaielamri.talpy-den/changes?file=<its changes.json>.

import * as fs from "node:fs";
import * as path from "node:path";
import * as vscode from "vscode";
import { cacheDir } from "./config";

const ONE_BY_ONE = 25; // diffs opened one by one where the changes view is missing

export interface ComparisonItem {
  label: string;
  path: string; // the file now
  original: string; // a copy of it from before
  created: boolean; // it didn't exist before
  deleted: boolean; // it's gone now
}

export interface Comparison {
  title: string;
  folder: string;
  items: ComparisonItem[];
}

export function changesRoot(root = cacheDir()): string {
  return path.join(root, "changes");
}

export function isComparison(x: unknown): x is Comparison {
  const c = x as Comparison;
  return (
    !!c &&
    typeof c.title === "string" &&
    Array.isArray(c.items) &&
    c.items.every(
      (i) => !!i && typeof i.path === "string" && i.path.startsWith("/") && typeof i.original === "string" && typeof i.label === "string",
    )
  );
}

// Is file (followed through links) inside folder?
export function inside(file: string, folder: string): boolean {
  try {
    const real = fs.realpathSync(file);
    const base = fs.realpathSync(folder);
    return real.startsWith(base + path.sep);
  } catch {
    return false;
  }
}

// The comparison file a changes link names, if it's one of the backend's
export function manifestOf(uri: { path: string; query: string }, root = changesRoot()): string | null {
  if (uri.path.replace(/\/+$/, "") !== "/changes") return null;
  const file = new URLSearchParams(uri.query).get("file");
  return file && path.isAbsolute(file) && inside(file, root) ? file : null;
}

export function readComparison(file: string, root = changesRoot()): Comparison | null {
  try {
    const c: unknown = JSON.parse(fs.readFileSync(file, "utf8"));
    // its copies come from the same place
    return isComparison(c) && c.items.every((i) => inside(i.original, root)) ? c : null;
  } catch {
    return null;
  }
}

// vscode.changes' resource list: [the file, before, now]; a created file has
// no "before", a deleted one no "now"
export function changesResources(c: Comparison): [vscode.Uri, vscode.Uri | undefined, vscode.Uri | undefined][] {
  return c.items.map((i) => [
    vscode.Uri.file(i.path),
    i.created ? undefined : vscode.Uri.file(i.original),
    i.deleted ? undefined : vscode.Uri.file(i.path),
  ]);
}

// Opens it; the error to report, or "" when it opened
export async function openComparison(c: Comparison): Promise<string> {
  try {
    await vscode.commands.executeCommand("vscode.changes", c.title, changesResources(c));
    return "";
  } catch (e) {
    // a VS Code without the changes view: a diff per file
    try {
      for (const i of c.items.slice(0, ONE_BY_ONE)) {
        if (i.deleted) await vscode.commands.executeCommand("vscode.open", vscode.Uri.file(i.original));
        else await vscode.commands.executeCommand("vscode.diff", vscode.Uri.file(i.original), vscode.Uri.file(i.path), `${i.label} (${c.title})`);
      }
      return "";
    } catch (e2) {
      return e2 instanceof Error ? e2.message : String(e2 ?? e);
    }
  }
}

// The widgets' link: vscode://wzaielamri.talpy-den/changes?file=…
export async function openChangesLink(uri: vscode.Uri, log: vscode.LogOutputChannel): Promise<void> {
  const file = manifestOf(uri);
  const comparison = file ? readComparison(file) : null;
  if (!comparison) {
    log.warn(`Ignored a link that isn't a comparison of the Talpy Den widget: ${uri.toString()}`);
    void vscode.window.showErrorMessage("Talpy Den: that link doesn't name one of the widget's comparisons (they're kept for a day).");
    return;
  }
  const error = await openComparison(comparison);
  if (error) void vscode.window.showErrorMessage(`Talpy Den: couldn't show the changes: ${error}`);
}
