// The chat tab and what rows and notifications do with it: a click on a
// chat opens its tab; answers and replies go to pane.py with what the
// extension's own state says (the chat's request, its process), never with
// what the page claims; to-review and the tests badge; Claude's Markdown made
// into elements only (no link that runs anything).

import assert from "node:assert/strict";
import { test } from "node:test";
import * as vscode from "vscode";
import { ChatPanes } from "../src/chatPane";
import { demoState } from "../src/shared/demo";
import { chatRefOf } from "../src/shared/model";
import { answersOf, oneChoice, picturesIn, queuedReply, reviewPending, routeHint, testsText, textScale, TEXT_SCALES, withoutPicture } from "../src/shared/pane";
import type { ViewState } from "../src/shared/types";
import { ViewHost } from "../src/view";
import { clipboardWrites, overrides, recorded } from "./vscode-stub";
import { inline, Markdown } from "../webview/util/markdown";

const NOW = 1_800_000_000_000;

function waitingState(answer: "hook" | "keys" | ""): ViewState {
  const state = demoState(NOW);
  const s = state.status!.groups.flatMap((g) => g.sessions).find((x) => x.state === "waiting")!;
  s.permission = { tool: "Bash", line: "Bash: git tag v2.0.0", command: "git tag v2.0.0", file: "", diff: [], always: "Bash(git tag:*)",
                   request: answer === "hook" ? "1792957203525-37d298fb" : null, answer };
  return state;
}

// (without the --config-dir of the folders this test runs with)
const own = (args: string[]) => (args.includes("--config-dir") ? args.slice(0, args.indexOf("--config-dir")) : args);

function panes(state: ViewState, answers: Record<string, unknown> = {}) {
  const calls: string[][] = [];
  const backend = { pane: async (args: string[]) => (calls.push(own(args)), { ok: true, message: "Allowed", ...answers }) };
  const dash = { state: () => state, refresh: async () => undefined, pollHost: async () => undefined, setActionError() {}, setActionInfo() {} };
  const opened: unknown[] = [];
  const actions = { open: async (c: unknown) => void opened.push(c), unqueue: async () => undefined };
  const terminals = { find: async () => undefined };
  const p = new ChatPanes(vscode.Uri.file("/ext") as never, backend as never, dash as never, actions as never, terminals as never, {} as never);
  return { p, calls, opened };
}

test("an answer goes to the hook's request the state names, else to the chat's keys in tmux", async () => {
  const state = waitingState("hook");
  const row = state.status!.groups.flatMap((g) => g.sessions).find((x) => x.state === "waiting")!;
  const { p, calls } = panes(state);
  assert.deepEqual(await p.answer(chatRefOf(row), "deny", " use make "), { ok: true, message: "Allowed" });
  assert.deepEqual(calls[0], ["--answer", "1792957203525-37d298fb", "--decision", "deny", "--note", "use make"]);

  const keys = waitingState("keys");
  const row2 = keys.status!.groups.flatMap((g) => g.sessions).find((x) => x.state === "waiting")!;
  const two = panes(keys);
  await two.p.answer(chatRefOf(row2), "always");
  assert.deepEqual(two.calls[0], ["--keys", row2.sessionId, "--profile", row2.profileDir, "--decision", "always"]);
});

test("a question's answers go to the hook's request as JSON; none chosen, nothing goes", async () => {
  const state = waitingState("hook");
  const row = state.status!.groups.flatMap((g) => g.sessions).find((x) => x.state === "waiting")!;
  row.permission!.questions = [{ question: "Which crust?", header: "Crust", multiSelect: false, options: [{ label: "Thin", description: "" }] }];
  const { p, calls } = panes(state);
  await p.answer(chatRefOf(row), "answer", "", { "Which crust?": ["Thin"] });
  assert.deepEqual(calls[0], ["--answer", "1792957203525-37d298fb", "--decision", "answer", "--answers", '{"Which crust?":["Thin"]}']);
  assert.equal((await p.answer(chatRefOf(row), "answer", "", {})).ok, false);
  assert.equal(calls.length, 1);
});

test("answers a page sends: {question: [strings]}, not too many", () => {
  assert.deepEqual(answersOf({ Q: ["a", "b"] }), { Q: ["a", "b"] });
  for (const bad of [null, [], "x", {}, { Q: "a" }, { Q: [1] }, Object.fromEntries(Array.from({ length: 17 }, (_, i) => [`q${i}`, ["a"]]))])
    assert.equal(answersOf(bad), null);
  const ask = { tool: "AskUserQuestion", line: "", command: "", file: "", diff: [], always: "", request: "r", answer: "hook" as const };
  const q = (multiSelect: boolean, n: number) => ({ question: "Q", header: "", multiSelect, options: Array.from({ length: n }, (_, i) => ({ label: `o${i}`, description: "" })) });
  assert.equal(oneChoice({ ...ask, questions: [q(false, 5)] }).length, 4);
  assert.deepEqual(oneChoice({ ...ask, questions: [q(true, 2)] }), []);
  assert.deepEqual(oneChoice({ ...ask, questions: [q(false, 2), q(false, 2)] }), []);
  assert.deepEqual(oneChoice(null), []);
});

test("nothing is answered for a chat that doesn't ask, or that only its own dialog can answer", async () => {
  const state = waitingState("");
  const rows = state.status!.groups.flatMap((g) => g.sessions);
  const { p, calls } = panes(state);
  assert.equal((await p.answer(chatRefOf(rows.find((x) => x.state === "waiting")!), "allow")).ok, false);
  assert.equal((await p.answer(chatRefOf(rows.find((x) => x.state !== "waiting")!), "allow")).ok, false);
  assert.deepEqual(calls, []);
});

test("a reply goes to pane.py --send; copy goes to the clipboard and opens the chat; a Claude Code tab gets its link", async () => {
  const state = demoState(NOW);
  const row = state.status!.groups.flatMap((g) => g.sessions)[0];
  const { p, calls, opened } = panes(state);
  assert.equal((await p.send(chatRefOf(row), "  and the docs  ", "auto", false)).ok, true);
  assert.deepEqual(calls[0].slice(0, 6), ["--send", row.chatId, "--text", "and the docs", "--how", "auto"]);
  assert.ok(calls[0].includes("--session"));
  await p.send(chatRefOf(row), "later on", "background", true);
  assert.deepEqual(calls[1].slice(calls[1].indexOf("--after-turn"), calls[1].indexOf("--after-turn") + 3), ["--after-turn", "--name", row.name]);
  await p.send(chatRefOf(row), "copy me", "copy", false);
  assert.equal(clipboardWrites.at(-1), "copy me");
  assert.equal(opened.length, 1);
  assert.equal(calls.length, 2);
  assert.equal((await p.send(chatRefOf(row), "   ", "auto", false)).ok, false);
  const ask = panes(state, { how: "ask" });
  assert.equal((await ask.p.send(chatRefOf(row), "hi", "auto", false)).ask, true);
});

test("a click on a chat opens its tab, unless clicks open the chat itself; answers use the extension's chat", () => {
  const state = waitingState("hook");
  const calls: { name: string; args: unknown[] }[] = [];
  const record = (name: string) => (...args: unknown[]) => void calls.push({ name, args });
  const actions = new Proxy({}, { get: (_t, name: string) => record(name) });
  const dash = { onState: () => ({ dispose() {} }), onPetEvent: () => ({ dispose() {} }), onLive: () => ({ dispose() {} }), liveHook: false, state: () => state, setVisible() {} };
  const host = new ViewHost(vscode.Uri.file("/ext") as never, dash as never, actions as never);
  host.panes = { open: record("panes.open"), answer: record("panes.answer") } as never;
  const receive = (m: unknown) => (host as unknown as { receive(w: unknown, m: unknown): void }).receive({ postMessage: async () => true }, m);
  const row = state.status!.groups.flatMap((g) => g.sessions).find((x) => x.state === "waiting")!;
  receive({ type: "open", chat: { key: row.key, sessionId: "forged", profileDir: "x", name: "x" } });
  assert.equal(calls[0].name, "panes.open");
  assert.equal((calls[0].args[0] as { sessionId: string }).sessionId, row.sessionId); // (the state's chat, not the page's)
  overrides.set("clickOpensPane", false);
  try {
    receive({ type: "open", chat: { key: row.key } });
    assert.equal(calls[1].name, "open");
  } finally {
    overrides.delete("clickOpensPane");
  }
  receive({ type: "answerPermission", chat: { key: row.key }, decision: "always", note: "" });
  assert.equal(calls[2].name, "panes.answer");
  assert.equal(calls[2].args[1], "always");
  receive({ type: "answerPermission", chat: { key: row.key }, decision: "rm -rf", note: "" });
  receive({ type: "openPane", chat: { key: "not-a-row" } });
  assert.equal(calls.length, 3);
});

test("to review: finished, changed files after the first look, not marked since (0: marked not reviewed)", () => {
  const status = { ...demoState(NOW).status!, reviewed: { a: NOW, "gpu|b": NOW, c: 0 }, reviewedSince: NOW - 10_000 };
  const item = (id: string, editedAt: number, state: "done" | "working" = "done") =>
    ({ sessionId: id, chatId: id, state, review: { editedAt, tests: null } });
  assert.equal(reviewPending(item("x", NOW - 5_000), status), true);
  assert.equal(reviewPending(item("x", NOW - 20_000), status), false); // (before the first look)
  assert.equal(reviewPending(item("x", NOW - 5_000, "working"), status), false);
  assert.equal(reviewPending(item("a", NOW - 1), status), false);
  assert.equal(reviewPending(item("a", NOW + 1), status), true); // (changed files again)
  assert.equal(reviewPending(item("b", NOW - 1), status, "gpu"), false);
  assert.equal(reviewPending(item("c", NOW - 20_000), status), true);
  assert.equal(reviewPending({ sessionId: "y", review: null }, status), false);
});

test("a chat waiting for a permission is told with its question, once more when the hook's request comes a poll late", async () => {
  const { TransitionTracker } = await import("../src/transitions");
  const t = new TransitionTracker();
  const opts = { notifyWaiting: true, notifyDone: true, notifyDoneMinSeconds: 0 };
  const g = demoState(NOW).status!.groups[0];
  const row = (state: "working" | "waiting", permission: unknown) => [{ ...g, sessions: [{ ...g.sessions[0], key: "k", chatId: "k", sessionId: "k", name: "k", state, permission } as never] }];
  t.track("", row("working", null), NOW, undefined, opts);
  const first = t.track("", row("waiting", null), NOW + 2000, undefined, opts);
  assert.deepEqual(first.notices.map((n) => n.title), ["k needs your input"]);
  const ask = { tool: "Bash", line: "Bash: make", command: "make deploy", file: "", diff: [], always: "", request: "1-a", answer: "hook" };
  const late = t.track("", row("waiting", ask), NOW + 4000, undefined, opts);
  assert.deepEqual(late.notices.map((n) => [n.title, n.body.split(" · ")[0]]), [["k asks for a permission", "make deploy"]]);
  assert.deepEqual(t.track("", row("waiting", ask), NOW + 6000, undefined, opts).notices, []); // (told once)
  // Claude's questions: "asks you", each question
  const t2 = new TransitionTracker();
  const question = { ...ask, tool: "AskUserQuestion", command: "", questions: [
    { question: "Which crust?", header: "", multiSelect: false, options: [] }, { question: "Which toppings?", header: "", multiSelect: true, options: [] }] };
  t2.track("", row("working", null), NOW, undefined, opts);
  const asked = t2.track("", row("waiting", question), NOW + 2000, undefined, opts);
  assert.deepEqual(asked.notices.map((n) => [n.title, n.body.split(" · ")[0]]), [["k asks you", "Which crust? Which toppings?"]]);
});

test("the tests badge, a queued reply and how a reply goes", () => {
  assert.equal(testsText({ editedAt: 1, tests: null })!.text, "not tested");
  assert.equal(testsText({ editedAt: 1, tests: { command: "npm test", ok: true, at: 2 } })!.text, "tests ✓");
  assert.equal(testsText({ editedAt: 1, tests: { command: "npm test", ok: false, at: 2 } })!.ok, false);
  assert.equal(testsText(null), null);
  const status = { ...demoState(NOW).status!, queue: [{ id: "q1", afterTurn: true, chat: "c1", host: "" } as never] };
  assert.equal(queuedReply(status, "c1")?.id, "q1");
  assert.equal(queuedReply(status, "c1", "gpu"), null);
  assert.match(routeHint("terminal", true, "interactive", ""), /takes no text from outside/);
  assert.match(routeHint("tmux", true, "interactive", "gpu"), /tmux pane\. \(on gpu\)/);
});

test("every file a page loads from dist/ is packaged (.vscodeignore names each one)", async () => {
  const fs = await import("node:fs");
  const shipped = fs.readFileSync(".vscodeignore", "utf8").split("\n").filter((l) => l.startsWith("!dist/")).map((l) => l.slice(6).trim());
  const loaded = new Set<string>();
  for (const f of fs.readdirSync("src").filter((n) => n.endsWith(".ts")))
    for (const m of fs.readFileSync(`src/${f}`, "utf8").matchAll(/asset\("dist", "([^"]+)"\)/g)) loaded.add(m[1]);
  assert.ok(loaded.has("chat.js") && loaded.has("webview.js"), [...loaded].join(", "));
  for (const name of loaded) assert.ok(shipped.includes(name), `${name} isn't in .vscodeignore`);
});

test("Claude's Markdown: links only to the web or mail; the rest stays text", () => {
  const out = inline("see **[the docs](https://example.org/x)** and [this](javascript:alert(1)) and `rm -rf /`");
  const flat = (nodes: unknown[]): { type: unknown; href?: string }[] =>
    nodes.flatMap((n) => {
      if (!n || typeof n !== "object") return [];
      const v = n as { type: unknown; props: { href?: string; children?: unknown } };
      const kids = Array.isArray(v.props.children) ? v.props.children : [v.props.children];
      return [{ type: v.type, href: v.props.href }, ...flat(kids)];
    });
  const nodes = flat(out);
  assert.deepEqual(nodes.filter((n) => n.type === "a").map((n) => n.href), ["https://example.org/x"]);
  assert.ok(nodes.some((n) => n.type === "span")); // (the javascript: link, as text)
  assert.ok(nodes.some((n) => n.type === "code"));
  assert.ok(nodes.some((n) => n.type === "strong"));
});

test("the chat tab's text size: a number in range, else the default", () => {
  assert.equal(textScale(1.5), 1.5);
  assert.equal(textScale(9), 2);
  assert.equal(textScale(0.1), 0.8);
  assert.equal(textScale("big"), 1.15);
  assert.equal(textScale(Number.NaN), 1.15);
  assert.ok(TEXT_SCALES.includes(1.15));
});

test("Claude's Markdown: ~~struck~~ text, and a code block keeps its text for Copy", () => {
  const nodes = inline("was ~~52~~ now `weeksInYear`") as { type?: unknown }[];
  assert.ok(nodes.some((n) => n && typeof n === "object" && n.type === "s"));
  const copied: string[] = [];
  const tree = Markdown({ text: "```ts\nif (a < b) return 1;\n```", onCopy: (code) => copied.push(code) }) as {
    props: { children: { props: { children: { props: { children: { props: { onClick?: () => void } }[] } }[] } }[] };
  };
  const block = tree.props.children[0];
  const copy = block.props.children[0].props.children[1];
  copy.props.onClick?.();
  assert.deepEqual(copied, ["if (a < b) return 1;"]);
});

test("a picture pasted in a chat's tab is saved, an SSH host's chat gets a copy there; nothing else is taken", async () => {
  const saved: string[][] = [];
  const backend = {
    pane: async () => ({ ok: true, unchanged: true }),
    saveImage: async (mime: string, data: string) => (saved.push(["save", mime, data]), { path: "/cache-here/talpy-den/images/20261026-101500.png", message: "" }),
    putImage: async (host: string, path: string) => (saved.push(["put", host, path]), host === "down" ? { message: "no route" } : { path: "/cache-there/talpy-den/images/20261026-101500.png", message: "" }),
  };
  const dash = { state: () => demoState(NOW), refresh: async () => undefined, pollHost: async () => undefined, onState: () => ({ dispose() {} }), onLive: () => ({ dispose() {} }), liveHook: false };
  const p = new ChatPanes(vscode.Uri.file("/ext") as never, backend as never, dash as never, {} as never, { find: async () => undefined } as never, {} as never);
  let tabs = 0;
  const tab = async (host: string, m: unknown) => {
    const chatId = `chat-${++tabs}`; // (a tab of its own)
    p.open({ chatId, sessionId: chatId, profileDir: ".claude", name: "x", host: host || undefined } as never);
    const panel = recorded.panels.at(-1)!;
    panel.receive(m);
    await new Promise((r) => setTimeout(r, 10));
    return panel.sent.filter((x) => (x as { type: string }).type === "picture");
  };
  assert.deepEqual(await tab("", { type: "picture", id: "1", mime: "image/png", data: "iVBORw0K" }),
                   [{ type: "picture", id: "1", path: "/cache-here/talpy-den/images/20261026-101500.png", error: "" }]);
  assert.deepEqual(await tab("lab", { type: "picture", id: "2", mime: "image/png", data: "iVBORw0K" }),
                   [{ type: "picture", id: "2", path: "/cache-there/talpy-den/images/20261026-101500.png", error: "" }]);
  assert.deepEqual((await tab("down", { type: "picture", id: "3", mime: "image/png", data: "iVBORw0K" }))[0],
                   { type: "picture", id: "3", path: "", error: "Couldn't copy the picture to down: no route" });
  assert.deepEqual(await tab("", { type: "picture", id: "4", mime: "image/svg+xml", data: "PHN2Zz4=" }),
                   [{ type: "picture", id: "4", path: "", error: "Claude takes PNG, JPEG, WebP and GIF pictures" }]);
  assert.deepEqual(saved.map((c) => c[0] + " " + c[1]), ["save image/png", "save image/png", "put lab", "save image/png", "put down"]);
});

test("the pictures a reply names, each once, and taking one out", () => {
  const a = "/cache-here/talpy-den/images/a.png";
  const text = `look [Image: ${a}] and [Image: /x/b.jpg] again [Image: ${a}]`;
  assert.deepEqual(picturesIn(text), [a, "/x/b.jpg"]);
  assert.equal(withoutPicture(text, a), "look and [Image: /x/b.jpg] again ");
});

test("a file dropped on a reply is named as Claude's prompt names it; a drop's links are its paths", async () => {
  const { droppedPaths, fileMention } = await import("../src/shared/pane");
  assert.equal(fileMention("/home/me/src/app.py"), "@/home/me/src/app.py");
  assert.equal(fileMention("/home/me/My Notes/todo.md"), '@"/home/me/My Notes/todo.md"');
  assert.deepEqual(droppedPaths("file:///home/me/a%20b.txt\r\nhttps://example.com/x\n# a comment\nfile:///tmp/c.png"),
    ["/home/me/a b.txt", "/tmp/c.png"]);
  assert.deepEqual(droppedPaths(""), []);
});

test("the reply box offers commands at the start and files after @, best first", async () => {
  const { suggestFor } = await import("../src/shared/pane");
  const offers = {
    commands: [{ name: "/compact", about: "Free up context", kind: "Claude" }, { name: "/ship", about: "Ship it", kind: "yours" },
               { name: "/git:compare", about: "Compare", kind: "project" }],
    files: ["src/parser/compiler.ts", "docs/compiling.md", "comp.py", "tests/my notes.txt"],
    history: [],
  };
  const c = suggestFor("/comp", offers);
  assert.deepEqual(c.items.map((i) => i.text), ["/compact", "/git:compare"]);
  assert.equal(c.items[1].about, "Compare · project");
  assert.equal(c.start, 0);
  assert.deepEqual(suggestFor("fix /comp", offers).items, []); // (a command only at the start)
  const f = suggestFor("look at @comp", offers);
  assert.deepEqual(f.items.map((i) => i.text), ["@comp.py", "@docs/compiling.md", "@src/parser/compiler.ts"]); // (names that start with it, shorter paths first)
  assert.equal(f.start, "look at ".length);
  assert.equal(suggestFor("see @notes", offers).items[0].insert, '@"tests/my notes.txt" ');
  assert.deepEqual(suggestFor("look at @comp", offers, "@comp").items, []); // (Esc closed it for that word)
  assert.deepEqual(suggestFor("/comp", null).items, []);
});

test("the microphone: recorded, then written down; a missing model is offered; Remote windows can't", async () => {
  const calls: string[][] = [];
  let status: Record<string, unknown> = { ok: true, tool: "/usr/bin/whisper-cli", model: "/m.bin" };
  const backend = {
    pane: async () => ({ ok: true, unchanged: true }),
    dictate: async (args: string[]) => (calls.push(args), args[0] === "--status" ? status
      : args[0] === "--stop" ? { ok: true, text: "Fix the parser" } : { ok: true, message: "" }),
  };
  const dash = { state: () => demoState(NOW), refresh: async () => undefined, pollHost: async () => undefined, onState: () => ({ dispose() {} }), onLive: () => ({ dispose() {} }), liveHook: false };
  const p = new ChatPanes(vscode.Uri.file("/ext") as never, backend as never, dash as never, {} as never, { find: async () => undefined } as never, {} as never);
  p.open({ chatId: "mic-1", sessionId: "mic-1", profileDir: ".claude", name: "x" } as never);
  const panel = recorded.panels.at(-1)!;
  const say = async (action: string) => {
    panel.receive({ type: "dictate", action });
    await new Promise((r) => setTimeout(r, 10));
    return panel.sent.filter((x) => (x as { type: string }).type === "dictation").slice(-1)[0];
  };
  assert.deepEqual(await say("toggle"), { type: "dictation", state: "recording" });
  assert.deepEqual(await say("toggle"), { type: "dictation", state: "", text: "Fix the parser", message: "" });
  assert.deepEqual(calls.map((c) => c[0]), ["--status", "--start", "--stop"]);
  assert.equal(calls[1][1], calls[2][1]); // (the same recording)
  assert.ok(calls[2].includes("--language"));
  status = { ok: false, tool: "/usr/bin/whisper-cli", model: null };
  assert.deepEqual(await say("toggle"), { type: "dictation", state: "", offerModel: true });
  status = { ok: false, tool: null, model: null, missing: ["whisper.cpp"] };
  assert.equal(((await say("toggle")) as { message?: string }).message, "To dictate, install whisper.cpp.");
});
