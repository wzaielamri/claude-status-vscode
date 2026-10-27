import assert from "node:assert/strict";
import { test } from "node:test";
import { demoState } from "../src/shared/demo";
import type { PermissionAsk, Session } from "../src/shared/types";

// A permission prompt's notification: its buttons in Claude's own order
// (Yes, Yes and don't ask again, No), as in its row and its tab
test("a permission prompt's notification lists Allow once, Always allow, Deny, Deny with a note…", async () => {
  const fs = await import("node:fs");
  const os = await import("node:os");
  const path = await import("node:path");
  const vscode = (await import("vscode")) as unknown as typeof import("./vscode-stub");
  process.env.XDG_CACHE_HOME = fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-notify-"));
  const { Notifier } = await import("../src/notify");
  const window = vscode.window as unknown as Record<string, unknown>;
  const shown: string[][] = [];
  let pick = "";
  let note: string | undefined = "";
  window.showWarningMessage = async (_m: string, ...buttons: string[]) => (shown.push(buttons), pick);
  window.showInputBox = async () => note;
  const answers: [string, string][] = [];
  const notifier = new Notifier({ sound() {} } as never, () => undefined);
  notifier.panes = { answer: async (_c: unknown, d: string, n = "") => (answers.push([d, n]), { ok: true, message: "" }) } as never;
  const ask = (p: Partial<PermissionAsk>): Session => ({
    ...demoState(Date.now()).status!.groups[0].sessions[0], state: "waiting", name: "deploy",
    permission: { tool: "Bash", line: "Bash: npm publish", command: "npm publish", file: "", diff: [], always: "", request: "r1", answer: "hook", ...p },
  }) as Session;
  const notify = async (s: Session) => {
    notifier.handle({ notices: [{ key: s.key, kind: "waiting", title: "deploy asks", body: "", session: s }], petEvents: [], sound: "", finished: [] });
    await new Promise((r) => setTimeout(r, 10));
  };

  await notify(ask({}));
  await notify(ask({ always: "Bash(npm publish:*)" }));
  assert.deepEqual(shown, [
    ["Allow once", "Deny", "Deny with a note…", "Open"],
    ["Allow once", "Always allow", "Deny", "Deny with a note…", "Open"],
  ]);

  pick = "Deny with a note…";
  note = "  publish to the test registry  ";
  await notify(ask({}));
  note = "";   // (Enter with no note: a plain deny)
  await notify(ask({}));
  note = undefined;   // (Escape: no answer)
  await notify(ask({}));
  assert.deepEqual(answers, [["deny", "publish to the test registry"], ["deny", ""]]);
  notifier.dispose();
});

// A plan (ExitPlanMode): Approve, accepting edits or not, or Keep planning
// with what to change
test("a plan's notification: Approve, accept edits / Approve / Keep planning…", async () => {
  const fs = await import("node:fs");
  const os = await import("node:os");
  const path = await import("node:path");
  const vscode = (await import("vscode")) as unknown as typeof import("./vscode-stub");
  process.env.XDG_CACHE_HOME = fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-notify-"));
  const { Notifier } = await import("../src/notify");
  const window = vscode.window as unknown as Record<string, unknown>;
  const shown: string[][] = [];
  let pick = "";
  let note: string | undefined = "";
  window.showWarningMessage = async (_m: string, ...buttons: string[]) => (shown.push(buttons), pick);
  window.showInputBox = async () => note;
  const answers: [string, string][] = [];
  const notifier = new Notifier({ sound() {} } as never, () => undefined);
  notifier.panes = { answer: async (_c: unknown, d: string, n = "") => (answers.push([d, n]), { ok: true, message: "" }), open() {} } as never;
  const s = {
    ...demoState(Date.now()).status!.groups[0].sessions[0], state: "waiting", name: "parser",
    permission: { tool: "ExitPlanMode", line: "Plan: Fix it", command: "", file: "", diff: [], always: "", request: "r2", answer: "hook",
                  plan: { text: "# Fix it", cut: false, file: "/p.md", title: "Fix it" } },
  } as Session;
  const notify = async () => {
    notifier.handle({ notices: [{ kind: "waiting", title: "parser", body: "plan", session: s, host: undefined }], finished: [], sound: "" } as never);
    await new Promise((r) => setTimeout(r, 10));
  };
  for (const [choice, typed] of [["Approve, accept edits", ""], ["Approve", ""], ["Keep planning…", " add tests "]] as const) {
    pick = choice;
    note = typed;
    await notify();
  }
  assert.deepEqual(shown[0], ["Approve, accept edits", "Approve", "Keep planning…", "Open"]);
  assert.deepEqual(answers, [["approve-edits", ""], ["approve", ""], ["keep", "add tests"]]);
  notifier.dispose();
});

// Claude's questions: one with one answer has its options as buttons, then
// Other… (your own words); with more, Answer… goes through each in a picker
test("a question's notification: its options, Other…; several questions: Answer… with pickers", async () => {
  const fs = await import("node:fs");
  const os = await import("node:os");
  const path = await import("node:path");
  const vscode = (await import("vscode")) as unknown as typeof import("./vscode-stub");
  process.env.XDG_CACHE_HOME = fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-notify-"));
  const { Notifier } = await import("../src/notify");
  const window = vscode.window as unknown as Record<string, unknown>;
  const shown: string[][] = [];
  let pick = "";
  const typed: (string | undefined)[] = [];
  const picks: string[][] = [];
  const pickers: { title: string; canPickMany: boolean; items: string[] }[] = [];
  window.showWarningMessage = async (_m: string, ...items: { title: string }[]) => (shown.push(items.map((i) => i.title)), items.find((i) => i.title === pick));
  window.showInputBox = async () => typed.shift();
  window.showQuickPick = async (items: { label: string }[], o: { title: string; canPickMany?: boolean }) => {
    pickers.push({ title: o.title, canPickMany: !!o.canPickMany, items: items.map((i) => i.label) });
    const wanted = picks.shift();
    const chosen = items.filter((i) => wanted?.includes(i.label));
    return o.canPickMany ? chosen : chosen[0];
  };
  const answers: unknown[] = [];
  const notifier = new Notifier({ sound() {} } as never, () => undefined);
  notifier.panes = { answer: async (_c: unknown, d: string, n: string, a: unknown) => (answers.push([d, n, a]), { ok: true, message: "" }) } as never;
  const crust = { question: "Which crust?", header: "Crust", multiSelect: false,
                  options: [{ label: "Thin", description: "crispy" }, { label: "Deep dish", description: "" }] };
  const toppings = { question: "Which toppings?", header: "Toppings", multiSelect: true,
                     options: [{ label: "Cheese", description: "" }, { label: "Olives", description: "" }] };
  const ask = (questions: unknown[]): Session => ({
    ...demoState(Date.now()).status!.groups[0].sessions[0], state: "waiting", name: "pizza",
    permission: { tool: "AskUserQuestion", line: "Which crust?", command: "", file: "", diff: [], always: "", questions,
                  request: "r1", answer: "hook" },
  }) as Session;
  const notify = async (s: Session) => {
    notifier.handle({ notices: [{ key: s.key, kind: "waiting", title: "pizza asks you", body: "", session: s }], petEvents: [], sound: "", finished: [] });
    await new Promise((r) => setTimeout(r, 10));
  };

  pick = "Deep dish";
  await notify(ask([crust]));
  pick = "Other…";
  typed.push("  half and half  ");
  await notify(ask([crust]));
  typed.push(undefined); // (Escape: no answer)
  await notify(ask([crust]));
  pick = "Answer…";
  picks.push(["Thin"], ["Cheese", "Olives", "Other…"]);
  typed.push("basil");
  await notify(ask([crust, toppings]));
  picks.push([]); // (Escape in the first picker: no answer)
  await notify(ask([crust, toppings]));
  assert.deepEqual(shown, [
    ["Thin", "Deep dish", "Other…", "Open"],
    ["Thin", "Deep dish", "Other…", "Open"],
    ["Thin", "Deep dish", "Other…", "Open"],
    ["Answer…", "Open"],
    ["Answer…", "Open"],
  ]);
  assert.deepEqual(pickers.slice(0, 2), [
    { title: "pizza: Crust (1/2)", canPickMany: false, items: ["Thin", "Deep dish", "Other…"] },
    { title: "pizza: Toppings (2/2)", canPickMany: true, items: ["Cheese", "Olives", "Other…"] },
  ]);
  assert.deepEqual(answers, [
    ["answer", "", { "Which crust?": ["Deep dish"] }],
    ["answer", "", { "Which crust?": ["half and half"] }],
    ["answer", "", { "Which crust?": ["Thin"], "Which toppings?": ["Cheese", "Olives", "basil"] }],
  ]);
  notifier.dispose();
});
