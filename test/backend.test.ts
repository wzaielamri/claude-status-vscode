// The vendored backend on a stock machine: just ~/.claude (no ~/.claude-*
// profiles, no profile-switching tools), and with a custom CLAUDE_CONFIG_DIR.
// Runs the real Python against made-up files.

import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { after, test } from "node:test";
import { Backend } from "../src/backend";

const children: ChildProcess[] = [];
after(() => children.forEach((c) => c.kill()));

function fakeHome(): string {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-test-"));
  process.env.HOME = home;
  process.env.XDG_CACHE_HOME = path.join(home, ".cache");
  delete process.env.CLAUDE_CONFIG_DIR;
  return home;
}

// A process standing in for a running claude: its environment says which
// config dir it uses, like a real session's.
function liveSession(configDir: string, env: NodeJS.ProcessEnv, entry: Record<string, unknown>) {
  const child = spawn("sleep", ["60"], { env: { PATH: process.env.PATH, ...env }, stdio: "ignore" });
  children.push(child);
  const stat = fs.readFileSync(`/proc/${child.pid}/stat`, "utf8");
  const procStart = stat.slice(stat.lastIndexOf(")") + 2).split(" ")[19];
  fs.mkdirSync(path.join(configDir, "sessions"), { recursive: true });
  fs.mkdirSync(path.join(configDir, "projects"), { recursive: true });
  const now = Date.now();
  fs.writeFileSync(
    path.join(configDir, "sessions", `${child.pid}.json`),
    JSON.stringify({ pid: child.pid, procStart, kind: "interactive", entrypoint: "cli", startedAt: now - 60000, statusUpdatedAt: now - 30000, ...entry }),
  );
}

const linux = process.platform === "linux";

test("a stock ~/.claude is all it takes", { skip: !linux && "sessions are detected through /proc" }, async () => {
  const home = fakeHome();
  fs.writeFileSync(
    path.join(home, ".claude.json"),
    JSON.stringify({ oauthAccount: { accountUuid: "00000000-0000-4000-8000-000000000001", displayName: "Alice", organizationType: "claude_max" } }),
  );
  liveSession(path.join(home, ".claude"), { HOME: home }, {
    sessionId: "11111111-1111-4111-8111-111111111111",
    cwd: path.join(home, "project"),
    status: "busy",
    name: "Fix the parser",
  });
  const backend = new Backend(path.resolve("."), () => "python3");
  assert.equal(await backend.checkPython(), "");
  const { status, error } = await backend.status(["--done-hours", "24", "--recent", "5"]);
  assert.equal(error, undefined);
  assert.ok(status);
  assert.equal(status.canShare, true, "sharing is built in; the view offers it with two or more accounts");
  assert.equal(status.groups.length, 1);
  const g = status.groups[0];
  assert.equal(g.profiles.length, 1);
  assert.match(g.title, /Alice/);
  assert.equal(g.sessions.length, 1);
  assert.equal(g.sessions[0].state, "working");
  assert.equal(g.sessions[0].name, "Fix the parser");
  assert.ok(g.homeChatCommand, "--remote mode gives the command that starts a chat");
});

test("a chat that mentions the answer's marker can't break the answer", { skip: !linux && "sessions are detected through /proc" }, async () => {
  const home = fakeHome();
  liveSession(path.join(home, ".claude"), { HOME: home }, {
    sessionId: "33333333-3333-4333-8333-333333333333",
    cwd: home,
    status: "idle",
    name: "Why does @@talpy-den@@ break the view?",
  });
  const backend = new Backend(path.resolve("."), () => "python3");
  const { status, error } = await backend.status([]);
  assert.equal(error, undefined);
  assert.equal(status?.groups[0].sessions[0].name, "Why does @@talpy-den@@ break the view?");
});

test("a custom CLAUDE_CONFIG_DIR is found when passed along", { skip: !linux && "sessions are detected through /proc" }, async () => {
  const home = fakeHome();
  const custom = path.join(home, "config", "claude");
  liveSession(custom, { HOME: home, CLAUDE_CONFIG_DIR: custom }, {
    sessionId: "22222222-2222-4222-8222-222222222222",
    cwd: home,
    status: "waiting",
    waitingFor: "permission prompt",
    name: "Release notes",
  });
  const backend = new Backend(path.resolve("."), () => "python3");
  const without = await backend.status([]);
  assert.equal(without.status?.groups.flatMap((g) => g.sessions).length ?? 0, 0);
  const { status } = await backend.status(["--config-dir", custom]);
  const sessions = status!.groups.flatMap((g) => g.sessions);
  assert.equal(sessions.length, 1);
  assert.equal(sessions[0].state, "waiting");
  assert.equal(sessions[0].reason, "permission");
});

test("a chat is shared between a stock ~/.claude and ~/.claude-work", async () => {
  const home = fakeHome();
  process.env.XDG_DATA_HOME = path.join(home, ".local", "share");
  const id = "33333333-3333-4333-8333-333333333333";
  const folder = path.join(home, "project");
  const key = folder.replace(/[^A-Za-z0-9]/g, "-");
  const chat = path.join(home, ".claude", "projects", key, `${id}.jsonl`);
  fs.mkdirSync(path.dirname(chat), { recursive: true });
  fs.writeFileSync(chat, JSON.stringify({ type: "user", cwd: folder, sessionId: id }) + "\n");
  fs.writeFileSync(path.join(home, ".claude.json"), JSON.stringify({ oauthAccount: { accountUuid: "00000000-0000-4000-8000-000000000001" } }));
  fs.mkdirSync(path.join(home, ".claude-work", "sessions"), { recursive: true });
  fs.writeFileSync(
    path.join(home, ".claude-work", ".claude.json"),
    JSON.stringify({ oauthAccount: { accountUuid: "00000000-0000-4000-8000-000000000002" } }),
  );
  const backend = new Backend(path.resolve("."), () => "python3");
  const answer = await backend.action("jump.py", ["--share", id, "--profile", ".claude"]);
  assert.equal(answer.ok, true, answer.message);
  const there = path.join(home, ".claude-work", "projects", key, `${id}.jsonl`);
  assert.equal(fs.statSync(there).ino, fs.statSync(chat).ino, "one file in both profiles");
  const { status } = await backend.status(["--recent", "5"]);
  assert.ok(status);
  assert.equal(status.canShare, true);
  assert.deepEqual(status.groups.map((g) => g.shareProfile).sort(), [".claude", ".claude-work"]);
  assert.ok(status.groups.flatMap((g) => g.recent).some((r) => r.sessionId === id && r.shared));
  const back = await backend.action("jump.py", ["--unshare", id, "--profile", ".claude"]);
  assert.equal(back.ok, true, back.message);
  assert.equal(fs.existsSync(there), false);
  assert.equal(fs.statSync(chat).nlink, 1);
});

test("an action's answer keeps how plan feedback reaches the chat", async () => {
  // jump.py --plan-feedback --print-command: the command that reopens the
  // chat, or the text to type in or paste; without them nothing is sent
  const ext = fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-ext-"));
  fs.mkdirSync(path.join(ext, "vendor", "code"), { recursive: true });
  fs.writeFileSync(
    path.join(ext, "vendor", "code", "jump.py"),
    'import json, sys\nprint(json.dumps({"ok": True, "message": "m", "how": sys.argv[1], "command": "claude --resume x", "text": "feedback"}))\n',
  );
  const backend = new Backend(ext, () => "python3");
  const resumed = await backend.action("jump.py", ["resumed"]);
  assert.deepEqual(resumed, { ok: true, message: "m", command: "claude --resume x", how: "resumed", text: "feedback" });
  assert.equal((await backend.action("jump.py", ["clipboard"])).how, "clipboard");
  assert.equal((await backend.action("jump.py", ["something else"])).how, undefined);
});

test("a missing Python is explained", async () => {
  const backend = new Backend(path.resolve("."), () => "python3-that-does-not-exist");
  assert.match(await backend.checkPython(), /Install Python 3\.7 or newer/);
});
