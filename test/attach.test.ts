// A background job a terminal shows already (`claude attach <job>`): a click
// brings that terminal up instead of attaching a second client.

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import type * as vscode from "vscode";
import type { AccountSwitch } from "../src/accountSwitch";
import { Actions } from "../src/actions";
import type { Backend } from "../src/backend";
import type { Dashboard } from "../src/dashboard";
import type { ChatRef } from "../src/shared/types";
import { attachClients, type Terminals } from "../src/terminals";

const JOB = "c0ffee12";

test("attach clients are told by their command line", () => {
  const proc = fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-proc-"));
  const add = (pid: number, comm: string, argv: string[]) => {
    fs.mkdirSync(path.join(proc, String(pid)));
    fs.writeFileSync(path.join(proc, String(pid), "comm"), comm + "\n");
    fs.writeFileSync(path.join(proc, String(pid), "cmdline"), argv.join("\0") + "\0");
  };
  try {
    add(10, "claude", ["claude", "attach", JOB]);
    add(11, "claude", ["/home/me/.local/bin/claude", "attach", JOB]);
    add(12, "claude", ["claude", "attach", "ffffffff"]);
    add(13, "claude", ["claude", "--resume", JOB]);
    add(14, "bash", ["bash", "-c", `claude attach ${JOB}`]);
    fs.mkdirSync(path.join(proc, "self"));
    assert.deepEqual(attachClients(JOB, proc).sort(), [10, 11]);
    assert.deepEqual(attachClients(JOB, path.join(proc, "missing")), [], "no /proc");
  } finally {
    fs.rmSync(proc, { recursive: true, force: true });
  }
});

test("a background job a terminal shows already comes up there, not attached twice", async () => {
  // a stand-in `claude attach <job>`: xargs named claude, waiting for its input
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-attach-"));
  fs.copyFileSync("/usr/bin/xargs", path.join(dir, "claude"));
  fs.chmodSync(path.join(dir, "claude"), 0o755);
  const client = spawn(path.join(dir, "claude"), ["attach", JOB], { stdio: ["pipe", "ignore", "ignore"] });
  const desktop = process.env.XDG_CURRENT_DESKTOP;
  try {
    for (let i = 0; i < 100 && !attachClients(JOB).includes(client.pid!); i++) await new Promise((r) => setTimeout(r, 20));
    assert.deepEqual(attachClients(JOB), [client.pid]);

    const chat = {
      key: "bg|s2", sessionId: "s2", profileDir: ".claude", name: "logo", entrypoint: "cli", pid: process.pid,
      kind: "bg", jobId: JOB, openCommand: `claude attach ${JOB}`,
    } as ChatRef;
    let inThisWindow = false;
    const helpers: string[][] = [];
    const runs: string[] = [];
    const errors: string[] = [];
    let shown = 0;
    const actions = new Actions(
      { action: async (_script: string, args: string[]) => (helpers.push(args), { ok: true, message: "" }) } as unknown as Backend,
      { demo: false, currentHost: undefined, setActionError: (m: string) => errors.push(m), isVisible: () => true } as unknown as Dashboard,
      {
        find: async (ancestors: Set<number>) => (inThisWindow && ancestors.has(client.pid!) ? { show: () => shown++ } : undefined),
        run: async (_key: string, _name: string, command: string) => void runs.push(command),
      } as unknown as Terminals,
      { warn: () => undefined } as unknown as vscode.LogOutputChannel,
      {} as AccountSwitch,
    );

    process.env.XDG_CURRENT_DESKTOP = "KDE";
    await actions.open(chat);
    assert.deepEqual(helpers.map((a) => a[0]), ["s2"], "jump.py brings up the terminal it's in");
    assert.deepEqual(runs, [], "not attached a second time");

    process.env.XDG_CURRENT_DESKTOP = "XFCE";
    await actions.open(chat);
    assert.deepEqual(errors, ["logo is open in a terminal outside this VS Code window."]);
    assert.deepEqual(runs, []);

    inThisWindow = true;
    await actions.open(chat);
    assert.equal(shown, 1, "this window's terminal");
    assert.equal(helpers.length, 1);

    client.kill();
    await new Promise((r) => client.once("exit", r));
    inThisWindow = false;
    await actions.open(chat);
    assert.deepEqual(runs, [`claude attach ${JOB}`], "no terminal shows it: attached here");
  } finally {
    client.kill();
    if (desktop === undefined) delete process.env.XDG_CURRENT_DESKTOP;
    else process.env.XDG_CURRENT_DESKTOP = desktop;
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
