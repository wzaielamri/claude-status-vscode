// Feeds the chat tab's page made-up data, the way src/chatPane.ts would: the
// chat, its conversation, and its review when the page asks for it.

import "../webview/chat";
import type { PaneChat, PaneHostMessage, PaneItem, ReviewAnswer } from "../src/shared/pane";

const params = new URLSearchParams(location.search);
const now = Date.now();
const ask = params.get("ask");
// ?ask=question: Claude's questions, answered through the hook
const questions = [
  { question: "Which weeks should the fix cover?", header: "Weeks", multiSelect: false,
    options: [{ label: "ISO weeks only (Recommended)", description: "Week 1 holds the year's first Thursday; 52 or 53 weeks." },
              { label: "US weeks too", description: "Weeks start on Sunday; a setting picks the rule." }] },
  { question: "Which tests should run after the fix?", header: "Tests", multiSelect: true,
    options: [{ label: "Parser tests", description: "npm test -- parser" }, { label: "All unit tests", description: "" },
              { label: "End-to-end", description: "Slow: about 4 minutes" }] },
];

function send(message: PaneHostMessage): void {
  window.postMessage(message, "*");
}

const chat: PaneChat = {
  host: params.get("host") ?? "", // ?host=lab: an SSH host's chat
  chatId: "demo-chat",
  sessionId: "demo-chat",
  profileDir: ".claude",
  name: "Fix the date parser",
  cwd: "/home/alice/projects/webapp",
  state: ask ? "waiting" : "working",
  stateText: ask ? "Needs input · 1m" : "Working · 6m",
  route: params.get("route") === "terminal" ? "terminal" : "tmux",
  canMove: params.get("route") === "terminal",
  busy: true,
  activity: "Bash: npm test -- parser",
  permission: ask === "question"
    ? { tool: "AskUserQuestion", line: questions[0].question, command: "", file: "", diff: [], always: "", questions,
        request: "1-abcdef02", answer: "hook" }
    : ask
    ? {
        tool: "Edit",
        line: "Edit: src/parse.ts",
        command: "",
        file: "src/parse.ts",
        diff: ["@@ -12,3 +12,3 @@", "   const week = isoWeek(date);", "-  if (week > 52) return 1;", "+  if (week > weeksInYear(date)) return 1;", "   return week;"],
        always: ask === "hook" ? "Edit(src/**)" : "",
        request: ask === "hook" ? "1-abcdef01" : null,
        answer: ask === "none" ? "" : (ask as "hook" | "keys"),
      }
    : null,
  changes: 2,
  reviewPending: false,
  review: { editedAt: now - 60_000, tests: { command: "npm test -- parser", ok: true, at: now - 30_000 } },
  queued: params.get("queued") === "1" ? { id: "q1", prompt: "Then update the changelog.", error: "" } : null,
};

const items: PaneItem[] = [
  { kind: "you", key: "u1", text: "Week 53 dates come out as week 1. Find out why and fix it, with a test.", at: now - 400_000 },
  { kind: "claude", key: "m1", text: "I'll look at how **week numbers** are computed in `src/parse.ts` first.", at: now - 390_000 },
  { kind: "tool", key: "t1", name: "Read", line: "Read: parse.ts", status: "ok", input: "file_path: /home/alice/projects/webapp/src/parse.ts", output: "     1\timport { isoWeek } from './dates';\n     2\t\n     3\texport function parse(text: string) {\n     4\t  const date = new Date(text); // UTC\n     5\t  return { date, week: isoWeek(date) };\n     6\t}", summary: "6 lines", at: now - 380_000 },
  { kind: "tool", key: "t2", name: "Bash", line: "Bash: npm test -- parser", status: "error", input: "$ npm test -- parser", output: "FAIL src/parse.test.ts\n  ✕ week 53 of 2026 (4 ms)", summary: "FAIL src/parse.test.ts … +1 line", at: now - 300_000 },
  {
    kind: "claude",
    key: "m2",
    text: "Found it: `isoWeek` caps weeks at 52.\n\n## What changes\n\n1. Use `weeksInYear(date)` instead of `52`\n2. Add a test for 2026-12-31\n\n```ts\nif (week > weeksInYear(date)) return 1;\n```\n\nSee [the ISO week rules](https://en.wikipedia.org/wiki/ISO_week_date).",
    at: now - 200_000,
  },
  { kind: "tool", key: "t3", name: "Edit", line: "Edit: parse.ts", status: "ok", input: "/home/alice/projects/webapp/src/parse.ts\n-  if (week > 52) return 1;\n+  if (week > weeksInYear(date)) return 1;", output: "", summary: "2 lines added, 1 removed", at: now - 100_000,
    diff: ["@@ -10,5 +10,6 @@ export function isoWeek(date: Date)", "   const start = startOfIsoYear(date);", "   const week = Math.floor((date - start) / WEEK) + 1;",
           "-  if (week > 52) return 1;", "+  if (week > weeksInYear(date)) return 1;", "+  // (a year has 53 weeks when it starts or ends on a Thursday)", "   return week;", " }"] },
  { kind: "tool", key: "t5", name: "Write", line: "Write: parse.test.ts", status: "ok", input: "", output: "", summary: "5 lines", at: now - 95_000,
    diff: ["@@ -0,0 +1,5 @@", "+import { parse } from './parse';", "+", "+test('week 53 of 2026', () => {", "+  expect(parse('2026-12-31').week).toBe(53);", "+});"] },
  { kind: "note", key: "n1", text: "Interrupted", at: now - 90_000 },
  { kind: "you", key: "u2", text: "Go on, and run the parser tests again.", at: now - 80_000 },
  { kind: "tool", key: "t4", name: "Bash", line: "Bash: npm test -- parser", status: "open", input: "$ npm test -- parser", output: "", at: now - 10_000 },
];

const review: ReviewAnswer = {
  ok: true,
  message: "Fix the date parser\n\nWeek 53 dates came out as week 1: isoWeek capped weeks at 52.",
  files: [
    { label: "src/parse.ts", path: "/home/alice/projects/webapp/src/parse.ts", added: 1, removed: 1, created: false, deleted: false, binary: false, unknown: false,
      diff: ["@@ -12,3 +12,3 @@", "   const week = isoWeek(date);", "-  if (week > 52) return 1;", "+  if (week > weeksInYear(date)) return 1;", "   return week;"] },
    { label: "src/parse.test.ts", path: "/home/alice/projects/webapp/src/parse.test.ts", added: 6, removed: 0, created: true, deleted: false, binary: false, unknown: false,
      diff: ["@@ -0,0 +1,6 @@", "+import { parse } from './parse';", "+", "+test('week 53 of 2026', () => {", "+  expect(parse('2026-12-31').week).toBe(53);", "+});", "+"] },
    { label: "~/notes/dates.md", path: "/home/alice/notes/dates.md", added: null, removed: null, created: false, deleted: false, binary: false, unknown: true,
      diff: [], now: ["# Dates", "", "- ISO weeks: week 1 holds the first Thursday", "- 2026 has 53 weeks"] },
  ],
  tests: { command: "npm test -- parser", ok: true, at: now - 30_000 },
  editedAt: now - 60_000,
  repos: ["/home/alice/projects/webapp"],
};

// ?open=1: every tool call unfolded (clicked once the conversation shows)
if (params.get("open") === "1")
  setTimeout(() => document.querySelectorAll<HTMLButtonElement>(".pane-tool-line").forEach((b, i) => setTimeout(() => b.click(), 60 * i)), 900);

// what the page sends, answered as the extension would
// ?type=TEXT: typed into the reply box (to see what it offers)
const typed = params.get("type");
if (typed)
  setTimeout(() => {
    const box = document.querySelector<HTMLTextAreaElement>(".pane-reply");
    if (!box) return;
    box.focus();
    box.value = typed;
    box.setSelectionRange(typed.length, typed.length);
    box.dispatchEvent(new Event("input", { bubbles: true }));
  }, 300);

window.addEventListener("harness-post", (e: Event) => {
  const m = (e as CustomEvent).detail as { type: string };
  if (m.type === "ready") {
    send({ type: "chat", chat });
    send({ type: "conversation", answer: { ok: true, items, more: true, stamp: "1" } });
  } else if (m.type === "picture") {
    const id = (m as unknown as { id: string }).id;
    setTimeout(() => send({ type: "picture", id, path: `/home/alice/.cache/talpy-den/images/20261026-1015${id.slice(-2)}.png`, error: "" }), 200);
  } else if (m.type === "review") {
    send({ type: "review", answer: review, error: "" });
  } else if (m.type === "complete") {
    send({ type: "offers", offers: {
      commands: [{ name: "/compact", about: "Free up context by summarizing the conversation so far", kind: "Claude" },
                 { name: "/context", about: "Show how full the context is", kind: "Claude" },
                 { name: "/release", about: "Cut a release", kind: "project" }],
      files: ["src/parser.ts", "src/parse_date.ts", "tests/parser.test.ts", "README.md"],
      history: ["Fix the date parser", "Run the tests again"],
    } });
  }
});

// ?paste=1: a picture pasted into the reply, as Ctrl+V gives one (a browser
// leaves out the clipboard of a paste event a page makes: it's put on the event)
if (params.get("paste") === "1") {
  setTimeout(() => {
    const canvas = document.createElement("canvas");
    canvas.width = 320;
    canvas.height = 200;
    const g = canvas.getContext("2d")!;
    g.fillStyle = "#3d6fb6";
    g.fillRect(0, 0, 320, 200);
    g.fillStyle = "#d97757";
    g.fillRect(40, 40, 120, 80);
    canvas.toBlob((blob) => {
      const box = document.querySelector<HTMLTextAreaElement>(".pane-reply");
      if (!blob || !box) return;
      const data = new DataTransfer();
      data.items.add(new File([blob], "shot.png", { type: "image/png" }));
      box.focus();
      const event = new Event("paste", { bubbles: true, cancelable: true });
      Object.defineProperty(event, "clipboardData", { value: data });
      box.dispatchEvent(event);
    });
  }, 100);
}
