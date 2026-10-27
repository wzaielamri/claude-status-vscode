import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import type * as vscode from "vscode";
import type { AccountSwitch } from "../src/accountSwitch";
import { Actions } from "../src/actions";
import type { Backend } from "../src/backend";
import type { Dashboard } from "../src/dashboard";
import { canRaiseWindows, gnomeRaiserRunning, isGnome, isKde } from "../src/desktop";
import type { ChatRef } from "../src/shared/types";
import type { Terminals } from "../src/terminals";

const on = (desktop: string) => ({ XDG_CURRENT_DESKTOP: desktop });

test("desktops are told apart like jump.py does", () => {
  assert.ok(isKde(on("KDE")));
  assert.ok(isGnome(on("GNOME")));
  assert.ok(isGnome(on("ubuntu:GNOME")), "Ubuntu's session");
  assert.ok(!isGnome(on("GNOME:KDE")), "KDE wins, as in jump.py's on_gnome()");
  assert.ok(!isKde(on("X-Cinnamon")) && !isGnome(on("X-Cinnamon")));
  assert.ok(!isGnome({}));
});

test("windows can be raised on Plasma, and on GNOME while the Talpy Den GNOME Shell extension runs", async () => {
  const running = async () => true;
  const notRunning = async () => false;
  assert.ok(await canRaiseWindows(on("KDE"), notRunning), "KWin does it");
  assert.ok(await canRaiseWindows(on("ubuntu:GNOME"), running));
  assert.ok(!(await canRaiseWindows(on("ubuntu:GNOME"), notRunning)), "GNOME with only this extension");
  assert.ok(!(await canRaiseWindows(on("XFCE"), running)));
  assert.ok(!(await canRaiseWindows({}, running)));
});

// A gdbus on PATH that answers introspection like GNOME Shell does
function fakeGdbus(answer: { code: number; stdout: string }): { restore: () => void } {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-gdbus-"));
  fs.writeFileSync(path.join(dir, "out"), answer.stdout);
  fs.writeFileSync(path.join(dir, "gdbus"), `#!/bin/sh\nPATH=/usr/bin:/bin cat "${dir}/out"\nexit ${answer.code}\n`, { mode: 0o755 });
  const saved = process.env.PATH;
  process.env.PATH = dir;
  return {
    restore: () => {
      process.env.PATH = saved;
      fs.rmSync(dir, { recursive: true, force: true });
    },
  };
}

const RAISER_INTROSPECTION = `node /org/gnome/Shell/Extensions/TalpyDen {
  interface org.freedesktop.DBus.Properties {
  };
  interface org.gnome.Shell.Extensions.TalpyDen {
    methods:
      RaiseWindow(in  s want,
                  out b handled);
  };
};
`;

test("the GNOME Shell extension runs when GNOME Shell has its D-Bus object", async () => {
  const cases: [{ code: number; stdout: string }, boolean, string][] = [
    [{ code: 0, stdout: RAISER_INTROSPECTION }, true, "it's there"],
    [{ code: 0, stdout: "node /org/gnome/Shell/Extensions/TalpyDen {\n};\n" }, false, "turned off: GNOME Shell 50 still answers, with nothing in it"],
    [{ code: 1, stdout: "" }, false, "No such object path (never loaded since login)"],
  ];
  for (const [answer, running, why] of cases) {
    const gdbus = fakeGdbus(answer);
    try {
      assert.equal(await gnomeRaiserRunning(), running, why);
    } finally {
      gdbus.restore();
    }
  }
  const saved = process.env.PATH;
  process.env.PATH = fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-empty-"));
  try {
    assert.equal(await gnomeRaiserRunning(), false, "no gdbus at all");
  } finally {
    fs.rmSync(process.env.PATH, { recursive: true, force: true });
    process.env.PATH = saved;
  }
});

test("on GNOME without the GNOME Shell extension, a chat in another window says where it runs", async () => {
  // (a chat in a VS Code terminal on an SSH host, in another window than this one)
  const chat = {
    host: "lab", key: "lab|s1", sessionId: "s1", profileDir: ".claude", name: "long-covid", entrypoint: "cli",
    pid: 4242, cwd: "/home/me/project", kind: "interactive", openCommand: null, vscodeTerminal: 4200,
  } as ChatRef;
  const helpers: string[][] = [];
  const errors: string[] = [];
  const dash = { demo: false, currentHost: undefined, setActionError: (m: string) => errors.push(m), isVisible: () => true };
  const actions = new Actions(
    { action: async (_script: string, args: string[]) => (helpers.push(args), { ok: true, message: "" }) } as unknown as Backend,
    dash as unknown as Dashboard,
    { find: async () => undefined } as unknown as Terminals,
    { warn: () => undefined } as unknown as vscode.LogOutputChannel,
    {} as AccountSwitch,
  );
  const desktop = process.env.XDG_CURRENT_DESKTOP;
  process.env.XDG_CURRENT_DESKTOP = "ubuntu:GNOME";
  try {
    let gdbus = fakeGdbus({ code: 1, stdout: "" });
    try {
      await actions.open(chat);
    } finally {
      gdbus.restore();
    }
    assert.deepEqual(helpers, [], "not jump.py: nothing raises the window");
    assert.deepEqual(errors, [
      "long-covid runs in a terminal of a VS Code window connected to lab. Bringing it up on GNOME needs the Talpy Den GNOME Shell extension, and it isn't running.",
    ]);
    gdbus = fakeGdbus({ code: 0, stdout: RAISER_INTROSPECTION });
    try {
      await actions.open(chat);
    } finally {
      gdbus.restore();
    }
    assert.deepEqual(helpers, [["--remote-terminal", "lab", "--name", "long-covid", "--vscode-terminal", "4200", "--cwd", "/home/me/project"]], "with it, jump.py raises that window");
  } finally {
    if (desktop === undefined) delete process.env.XDG_CURRENT_DESKTOP;
    else process.env.XDG_CURRENT_DESKTOP = desktop;
  }
});
