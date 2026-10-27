// A subagent's whole run, in VS Code's Markdown preview. Claude Code can't
// open a subagent from outside, so the widget's backend renders its
// transcript as Markdown into the cache (jump.py --agent-run; remote.py
// --agent-run fetches one from an SSH host) and this opens that file.

import * as os from "node:os";
import * as path from "node:path";
import * as vscode from "vscode";
import { inside } from "./changes";
import { cacheDir } from "./config";
import type { ChatRef } from "./shared/types";

export function agentRunsRoot(root = cacheDir()): string {
  return path.join(root, "agent-runs");
}

// The helper to run, and its arguments (it saves the file, --no-open: we open it)
export function agentRunArgs(
  chat: Pick<ChatRef, "host" | "chatId" | "sessionId" | "profileDir">,
  agentId: string,
  extra: string[] = [],
): ["jump.py" | "remote.py", string[]] {
  const chatId = chat.chatId || chat.sessionId;
  if (chat.host) return ["remote.py", ["--agent-run", chat.host, chatId, agentId, ...(chat.profileDir ? [chat.profileDir] : []), "--no-open"]];
  return ["jump.py", ["--agent-run", agentId, "--chat", chatId, ...(chat.profileDir ? ["--profile", chat.profileDir] : []), "--no-open", ...extra]];
}

// Is it a file the backend saved, in the cache's agent-runs folder?
export function isAgentRunFile(file: string | undefined, root = agentRunsRoot()): file is string {
  return !!file && path.isAbsolute(file) && file.endsWith(".md") && inside(file, root);
}

// The widgets' link to show a plan or a subagent's run in VS Code's Markdown
// preview (jump.py's MARKDOWN_LINK, the widget's "Open plans and runs in:
// VS Code"): vscode://wzaielamri.talpy-den/markdown?path=<file>. Only a
// Markdown file of the widget's runs folder, or of a Claude config folder's
// plans (~/.claude*/plans, or of one named in configDirs), since any page
// could hand VS Code such a link: else null.
export function markdownLinkFile(
  uri: { path: string; query: string },
  home = os.homedir(),
  configDirs: string[] = [],
  root = agentRunsRoot(),
): string | null {
  if (uri.path.replace(/\/+$/, "") !== "/markdown") return null;
  const file = new URLSearchParams(uri.query).get("path") ?? "";
  if (!path.isAbsolute(file) || !file.endsWith(".md") || path.normalize(file) !== file) return null;
  if (isAgentRunFile(file, root)) return file;
  const folder = path.dirname(file);
  if (path.basename(folder) !== "plans") return null;
  const config = path.dirname(folder);
  const ownName = path.dirname(config) === home && /^\.claude(-[^/]+)?$/.test(path.basename(config));
  return ownName || configDirs.includes(config) ? file : null;
}

// Opens it in the Markdown preview (as text where that's missing); the error
// to report, or "" when it opened
export async function openAgentRunFile(file: string): Promise<string> {
  // a file of this computer: in a Remote-SSH window, through vscode-local
  return showMarkdown(vscode.env.remoteName ? vscode.Uri.from({ scheme: "vscode-local", path: file }) : vscode.Uri.file(file));
}

// A Markdown file in the preview (its Open Source button edits it), else as
// text; the error to report, or ""
export async function showMarkdown(uri: vscode.Uri): Promise<string> {
  try {
    await vscode.commands.executeCommand("markdown.showPreview", uri);
    return "";
  } catch (e) {
    try {
      await vscode.window.showTextDocument(uri, { preview: true });
      return "";
    } catch (e2) {
      return e2 instanceof Error ? e2.message : String(e2 ?? e);
    }
  }
}
