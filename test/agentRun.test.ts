// A subagent's whole run: which helper saves it, that only its own files are
// opened, and how they open (the Markdown preview, else as text). The last
// test runs the vendored backend on a made-up subagent.

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import * as vscode from "vscode";
import { agentRunArgs, agentRunsRoot, isAgentRunFile, openAgentRunFile } from "../src/agentRun";
import { Backend } from "../src/backend";
import { cacheDir } from "../src/config";

test("the run is saved by jump.py here and by remote.py for an SSH host", () => {
  const chat = { sessionId: "s1", chatId: "c1", profileDir: ".claude-work" };
  assert.deepEqual(agentRunArgs(chat, "a1", ["--config-dir", "/x"]), [
    "jump.py",
    ["--agent-run", "a1", "--chat", "c1", "--profile", ".claude-work", "--no-open", "--config-dir", "/x"],
  ]);
  assert.deepEqual(agentRunArgs({ ...chat, host: "lab" }, "a1"), ["remote.py", ["--agent-run", "lab", "c1", "a1", ".claude-work", "--no-open"]]);
  // a row without a chat id of its own, or a profile
  assert.deepEqual(agentRunArgs({ sessionId: "s1", profileDir: "" }, "a1"), ["jump.py", ["--agent-run", "a1", "--chat", "s1", "--no-open"]]);
});

test("only files in the cache's agent-runs folder are opened", () => {
  const cache = fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-runs-"));
  const root = agentRunsRoot(cache);
  fs.mkdirSync(root);
  const run = path.join(root, "Find the parser (a1).md");
  fs.writeFileSync(run, "# Find the parser\n");
  fs.writeFileSync(path.join(root, "notes.txt"), "x");
  fs.symlinkSync("/etc/hostname", path.join(root, "out.md"));
  try {
    assert.equal(isAgentRunFile(run, root), true);
    assert.equal(isAgentRunFile(undefined, root), false);
    assert.equal(isAgentRunFile(path.join(root, "notes.txt"), root), false);
    assert.equal(isAgentRunFile(path.join(root, "out.md"), root), false); // a link out
    assert.equal(isAgentRunFile(path.join(root, "..", "x.md"), root), false);
    assert.equal(isAgentRunFile("Find the parser (a1).md", root), false);
  } finally {
    fs.rmSync(cache, { recursive: true });
  }
});

test("it opens in the Markdown preview, as text where that's missing", async () => {
  const calls: unknown[][] = [];
  const commands = vscode.commands as unknown as { executeCommand: (...a: unknown[]) => Promise<unknown> };
  const window = vscode.window as unknown as { showTextDocument: (...a: unknown[]) => Promise<unknown> };
  const [realCommand, realShow] = [commands.executeCommand, window.showTextDocument];
  try {
    commands.executeCommand = async (...a: unknown[]) => void calls.push(a);
    window.showTextDocument = async (...a: unknown[]) => void calls.push(["text", ...a]);
    assert.equal(await openAgentRunFile("/c/agent-runs/run.md"), "");
    assert.equal(calls[0][0], "markdown.showPreview");
    assert.equal((calls[0][1] as vscode.Uri).path, "/c/agent-runs/run.md");
    calls.length = 0;
    commands.executeCommand = async () => {
      throw new Error("command 'markdown.showPreview' not found");
    };
    assert.equal(await openAgentRunFile("/c/agent-runs/run.md"), "");
    assert.equal(calls[0][0], "text");
    window.showTextDocument = async () => {
      throw new Error("cannot open");
    };
    assert.equal(await openAgentRunFile("/c/agent-runs/run.md"), "cannot open");
  } finally {
    commands.executeCommand = realCommand;
    window.showTextDocument = realShow;
  }
});

test("the vendored backend saves a subagent's run where it's opened from", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-test-"));
  process.env.HOME = home;
  process.env.XDG_CACHE_HOME = path.join(home, ".cache");
  delete process.env.CLAUDE_CONFIG_DIR;
  const chat = "44444444-4444-4444-8444-444444444444";
  const agents = path.join(home, ".claude", "projects", "-p", chat, "subagents");
  fs.mkdirSync(agents, { recursive: true });
  fs.writeFileSync(path.join(home, ".claude", "projects", "-p", `${chat}.jsonl`), JSON.stringify({ type: "user", cwd: home, sessionId: chat }) + "\n");
  fs.writeFileSync(path.join(agents, "agent-a1.meta.json"), JSON.stringify({ agentType: "Explore", description: "Find the parser" }));
  const lines = [
    { type: "user", isSidechain: true, message: { content: "Find where dates are parsed." } },
    { type: "assistant", isSidechain: true, message: { content: [{ type: "tool_use", id: "t1", name: "Grep", input: { pattern: "parseDate" } }] } },
    { type: "user", isSidechain: true, message: { content: [{ type: "tool_result", tool_use_id: "t1", content: "src/date.ts:3" }] } },
    { type: "assistant", isSidechain: true, message: { content: [{ type: "tool_use", id: "h", name: "SubagentHandback", input: { message: "In src/date.ts" } }] } },
  ];
  fs.writeFileSync(path.join(agents, "agent-a1.jsonl"), lines.map((l) => JSON.stringify(l) + "\n").join(""));
  try {
    const backend = new Backend(path.resolve("."), () => "python3");
    const [script, args] = agentRunArgs({ sessionId: chat, chatId: chat, profileDir: "" }, "a1");
    const answer = await backend.action(script, args);
    assert.equal(answer.ok, true, answer.message);
    assert.ok(isAgentRunFile(answer.file, agentRunsRoot(cacheDir())), answer.file);
    const md = fs.readFileSync(answer.file!, "utf8");
    assert.match(md, /^# Find the parser\n/);
    assert.match(md, /## Task\n\nFind where dates are parsed\./);
    assert.match(md, /## Report\n\nIn src\/date\.ts/);
    const missing = await backend.action(script, ["--agent-run", "nope", "--chat", chat, "--no-open"]);
    assert.deepEqual([missing.ok, missing.file], [false, undefined]);
  } finally {
    fs.rmSync(home, { recursive: true });
  }
});
