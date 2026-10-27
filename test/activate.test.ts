// The whole extension, activated outside VS Code against a made-up home with
// one live chat: every command and the view get registered, the first poll
// reaches the status bar.

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import * as vscode from "vscode";
import { activate } from "../src/extension";

const stub = vscode as unknown as typeof import("./vscode-stub");

test("activates, registers everything, and shows the first poll", { skip: process.platform !== "linux" && "needs /proc" }, async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-test-"));
  process.env.HOME = home;
  process.env.XDG_CACHE_HOME = path.join(home, ".cache");
  delete process.env.CLAUDE_CONFIG_DIR;
  const child = spawn("sleep", ["60"], { env: { PATH: process.env.PATH, HOME: home }, stdio: "ignore" });
  try {
    const cfg = path.join(home, ".claude");
    fs.mkdirSync(path.join(cfg, "sessions"), { recursive: true });
    fs.mkdirSync(path.join(cfg, "projects"), { recursive: true });
    fs.writeFileSync(
      path.join(cfg, "sessions", `${child.pid}.json`),
      JSON.stringify({ pid: child.pid, kind: "interactive", sessionId: "33333333-3333-4333-8333-333333333333", cwd: home, status: "waiting", waitingFor: "permission prompt", name: "Deploy", startedAt: Date.now() }),
    );
    const manifest = JSON.parse(fs.readFileSync("package.json", "utf8"));
    const subscriptions: { dispose(): void }[] = [];
    activate({ subscriptions, extensionPath: path.resolve("."), extensionUri: vscode.Uri.file(path.resolve(".")) } as unknown as vscode.ExtensionContext);

    for (const c of manifest.contributes.commands) assert.ok(stub.recorded.commands.has(c.command), `command ${c.command} registered`);
    assert.ok(stub.recorded.viewProviders.has("talpyDen.view"));

    const item = stub.recorded.statusBarItems[0];
    for (let i = 0; i < 100 && !item.text.includes("bell-dot"); i++) await new Promise((r) => setTimeout(r, 100));
    assert.equal(item.text, "$(bell-dot) 1", "the waiting chat shows in the status bar");
    assert.deepEqual(item.backgroundColor, new stub.ThemeColor("statusBarItem.warningBackground"));
    for (const s of subscriptions) s.dispose();
  } finally {
    child.kill();
  }
});
