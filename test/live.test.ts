// Live mode: what a working chat does now (its live view against what the
// last check read), the ticker under the big Talpy, and the watcher that
// asks for a check the moment a chat changes.

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { Dashboard } from "../src/dashboard";
import { LiveWatch, sessionDirs, type LiveChange } from "../src/live";
import { demoState } from "../src/shared/demo";
import { doingText, liveFromRows, liveOf, peekLine, tickerItems, TickerTurns, withLive, type TickerItem } from "../src/shared/model";
import type { Group, LiveView, Session } from "../src/shared/types";

const NOW = 1_800_000_000_000;

function chat(partial: Partial<Session>): Session {
  return { ...demoState(NOW).status!.groups[0].sessions[0], peek: null, tasks: null, changes: null, stats: null, detail: "", ...partial };
}

function view(partial: Partial<LiveView>): LiveView {
  return { sessionId: "s1", pid: 42, state: "working", doing: "", at: NOW, steps: [], agents: [], ...partial };
}

const peek = { activity: "Bash: npm test", prompt: "fix it", reply: "", replyLine: "", at: NOW };

test("a working chat's line: its live step when that's newer than its transcript", () => {
  const s = chat({ sessionId: "s1", pid: 42, state: "working", peek });
  const edit = view({ doing: "Edit: parser.py", at: NOW + 500,
    steps: [{ id: "t1", tool: "Edit", text: "Edit: parser.py", at: NOW + 400, end: NOW + 500, ok: true, add: 12, del: 3 }] });
  assert.equal(doingText(s, edit), "Edit: parser.py +12 −3");
  assert.deepEqual(peekLine(s, edit), { kind: "activity", text: "▸ Edit: parser.py +12 −3" });
  assert.equal(doingText(s, view({ doing: "Read: a.txt", at: NOW - 1 })), "Bash: npm test"); // (older than the transcript)
  assert.equal(doingText(s, view({ doing: "", at: NOW + 1 })), "Bash: npm test"); // (between turns: the check says)
  const agentStep = { id: "t2", tool: "Grep", text: "Grep: x", at: NOW, end: null, ok: null, agent: "Explore" };
  assert.equal(doingText(s, view({ doing: "Agent: look", at: NOW + 1, steps: [agentStep] })), "Agent: look");
  assert.equal(doingText(s, null), "Bash: npm test");
});

test("a live view counts for its own process only, and not once it ended", () => {
  const s = chat({ sessionId: "s1", pid: 42 });
  assert.ok(liveOf(s, { s1: view({}) }));
  assert.equal(liveOf(s, { s1: view({ pid: 7 }) }), null);
  assert.equal(liveOf(s, { s1: view({ state: "ended" }) }), null);
  assert.equal(liveOf(chat({ sessionId: "s1", pid: null }), { s1: view({}) }), null);
});

test("the views: the newest wins, a check keeps only the chats listed", () => {
  const a = view({ sessionId: "a", at: 10 });
  assert.equal(withLive({ a }, [view({ sessionId: "a", at: 5 })]), null);
  const next = withLive({ a }, [view({ sessionId: "a", at: 20, doing: "x" }), view({ sessionId: "b", at: 1 })])!;
  assert.equal(next.a.doing, "x");
  assert.ok(next.b);
  assert.deepEqual(withLive(next, [view({ sessionId: "b", at: 2, state: "ended" })])!.b, undefined);
  const groups = [{ sessions: [chat({ sessionId: "a", pid: 42, live: view({ at: 30, doing: "row" }) }), chat({ sessionId: "c", pid: 42 })] }] as Group[];
  const kept = liveFromRows(groups, { ...next, gone: view({ sessionId: "gone" }) });
  assert.deepEqual(Object.keys(kept), ["a"]);
  assert.equal(kept.a.doing, "row"); // (the row's is newer)
  assert.equal(kept.a.sessionId, "a");
});

function items(...texts: [string, string][]): TickerItem[] {
  return texts.map(([key, text]) => ({ key, name: key, text, session: chat({}) }));
}

test("the ticker: a chat's new step at once, another's after a moment, and turns", () => {
  const t = new TickerTurns(1500, 4000);
  assert.equal(t.update(items(["a", "Read: x"]), 0)?.text, "Read: x");
  assert.equal(t.update(items(["a", "Edit: x"]), 100)?.text, "Edit: x"); // its own new step: at once
  assert.equal(t.update(items(["a", "Edit: x"], ["b", "Bash: ls"]), 200)?.key, "a"); // b waits
  assert.equal(t.tick(1000)?.key, "a");
  assert.equal(t.tick(1700)?.key, "b"); // (a was on 1.5 s: b's new step)
  assert.equal(t.tick(3000)?.key, "b");
  assert.equal(t.tick(5800)?.key, "a"); // nothing new for 4 s: the next chat's turn
  assert.equal(t.update(items(["b", "Bash: ls"]), 6000)?.key, "b"); // a's chat stopped working
  assert.equal(t.update([], 6100), null);
});

test("the ticker's items: working chats with something to say; a host's from its check", () => {
  const groups = [
    { sessions: [chat({ sessionId: "s1", pid: 42, state: "working", name: "fix", peek }), chat({ state: "idle", peek })] },
    { host: "lab", sessions: [chat({ sessionId: "s1", pid: 42, state: "working", name: "far", peek })] },
  ] as Group[];
  const got = tickerItems(groups, { s1: view({ doing: "Edit: a.py", at: NOW + 1 }) });
  assert.deepEqual(got.map((i) => [i.name, i.text, i.host]), [["fix", "Edit: a.py", undefined], ["far", "Bash: npm test", "lab"]]);
});

test("the sessions folders to watch: each profile's, once", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "cs-live-"));
  fs.mkdirSync(path.join(home, ".claude-work/sessions"), { recursive: true });
  fs.symlinkSync(path.join(home, ".claude-work"), path.join(home, ".claude"));
  fs.mkdirSync(path.join(home, ".claude-empty"));
  fs.mkdirSync(path.join(home, "extra/sessions"), { recursive: true });
  assert.deepEqual(sessionDirs(home, [path.join(home, "extra")]),
    [fs.realpathSync(path.join(home, ".claude-work/sessions")), fs.realpathSync(path.join(home, "extra/sessions"))]);
  fs.rmSync(home, { recursive: true });
});

async function next(changes: LiveChange[], n: number, ms = 3000): Promise<void> {
  const until = Date.now() + ms;
  while (changes.length < n && Date.now() < until) await new Promise((r) => setTimeout(r, 20));
}

test("the watcher: a session file asks for a check at once, a live file is handed on", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "cs-live-"));
  const sessions = path.join(home, ".claude-work/sessions");
  fs.mkdirSync(sessions, { recursive: true });
  const cache = path.join(home, ".cache/talpy-den");
  const changes: LiveChange[] = [];
  const watch = new LiveWatch((c) => changes.push(c), () => [], cache, home);
  watch.start();
  try {
    assert.equal(watch.healthy, true);
    assert.ok(fs.statSync(path.join(cache, "live")).isDirectory());
    fs.writeFileSync(path.join(sessions, "123.json"), "{}");
    await next(changes, 1);
    assert.deepEqual(changes[0], { urgent: true, views: [] });
    // a step (written as the hook does: a temporary file, then renamed)
    const file = path.join(cache, "live/s1.json");
    fs.writeFileSync(file + ".9.tmp", JSON.stringify(view({ event: "PreToolUse", doing: "Bash: ls" })));
    fs.renameSync(file + ".9.tmp", file);
    await next(changes, 2);
    assert.equal(changes[1].urgent, false);
    assert.equal(changes[1].views[0].doing, "Bash: ls");
    // another step right after: a moment later
    const started = Date.now();
    fs.writeFileSync(file, JSON.stringify(view({ event: "PostToolUse", doing: "Bash: ls", at: NOW + 1 })));
    await next(changes, 3);
    assert.equal(changes[2].views[0].event, "PostToolUse");
    assert.ok(Date.now() - started >= 250);
    // the end of a turn doesn't wait
    const stopped = Date.now();
    fs.writeFileSync(file, JSON.stringify(view({ event: "Stop", state: "idle", at: NOW + 2 })));
    await next(changes, 4);
    assert.equal(changes[3].urgent, true);
    assert.ok(Date.now() - stopped < 300);
    fs.writeFileSync(path.join(cache, "live/s1.lock"), ""); // (not a live file)
    fs.writeFileSync(path.join(cache, "live/.seen-x.json"), "{}");
    await new Promise((r) => setTimeout(r, 200));
    assert.equal(changes.length, 4);
  } finally {
    watch.dispose();
    fs.rmSync(home, { recursive: true });
  }
  assert.equal(watch.healthy, false);
});

test("a chat that changes while a check runs is checked again once that's done", async () => {
  let calls = 0;
  let release: () => void = () => undefined;
  const backend = {
    status: async () => {
      calls += 1;
      if (calls === 1) await new Promise<void>((r) => (release = r));
      return { status: structuredClone(demoState(NOW).status!) };
    },
  };
  const notifier = { handle() {}, isLeader: () => true };
  const log = { warn() {}, info() {}, error() {} };
  const dash = new Dashboard(backend as never, notifier as never, log as never);
  const first = dash.refresh();
  await dash.refresh(); // (a check running: not doubled)
  assert.equal(calls, 1);
  await dash.refresh(true); // a chat changed meanwhile
  release();
  await first;
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(calls, 2);
  dash.dispose();
});
