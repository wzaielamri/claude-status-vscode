import assert from "node:assert/strict";
import { test } from "node:test";
import { AwayTracker } from "../src/away";
import { demoState } from "../src/shared/demo";
import type { Group, Session } from "../src/shared/types";
import { TransitionTracker } from "../src/transitions";

const NOW = 1_800_000_000_000;
const MIN = 60000;
const OPTS = { notifyWaiting: true, notifyDone: true, notifyDoneMinSeconds: 30 };

function groups(sessions: Partial<Session>[]): Group[] {
  const g = demoState(NOW).status!.groups[0];
  // each its own chat (its own id), not one chat's rows
  return [{ ...g, sessions: sessions.map((p, i) => ({ ...g.sessions[0], key: `c${i}`, sessionId: `c${i}`, chatId: `c${i}`, ...p }) as Session) }];
}

test("coming back after a while sums up what happened", () => {
  const tracker = new TransitionTracker();
  const away = new AwayTracker();
  tracker.track("", groups([{ name: "parser", state: "working" }, { name: "deploy", state: "working" }, { name: "train", state: "working" }]), NOW, undefined, OPTS);
  away.leave(NOW);
  const later = NOW + 30 * MIN;
  const now = groups([
    { name: "parser", state: "idle", since: later },
    { name: "deploy", state: "waiting", since: NOW + 8 * MIN },
    { name: "train", state: "done", reason: "failed", since: later },
  ]);
  away.note(tracker.track("", now, later, undefined, OPTS), [{ kind: "reset", text: "5h limit reset!", key: "" }], later);
  const d = away.back(NOW + 72 * MIN, NOW, now, 5 * MIN)!;
  assert.equal(d.title, "While you were away (1h 12m)");
  assert.deepEqual(d.lines, ["Needs you: deploy (waiting 1h 4m)", "Finished: parser", "Failed: train", "5h limit reset"]);
  assert.equal(d.message, "While you were away (1h 12m): Needs you: deploy (waiting 1h 4m) · Finished: parser · Failed: train · 5h limit reset");
  assert.equal(d.first!.session.name, "deploy");
});

test("no digest for a short absence, for nothing new, or for time spent in another window", () => {
  const away = new AwayTracker();
  const quiet = groups([{ name: "parser", state: "idle" }]);
  away.leave(NOW);
  assert.equal(away.back(NOW + 2 * MIN, NOW, quiet, 5 * MIN), null, "short");
  away.leave(NOW);
  assert.equal(away.back(NOW + 60 * MIN, NOW, quiet, 5 * MIN), null, "nothing happened");
  const finished = { finished: [{ id: "c0", session: quiet[0].sessions[0], took: MIN, at: NOW + 10 * MIN, stop: "" as const }] };
  away.leave(NOW);
  away.note(finished, [], NOW + 10 * MIN);
  assert.equal(away.back(NOW + 60 * MIN, NOW + 59 * MIN, quiet, 5 * MIN), null, "another VS Code window was in use until a minute ago");
  away.leave(NOW);
  away.note(finished, [], NOW + 10 * MIN);
  assert.deepEqual(away.back(NOW + 60 * MIN, NOW + 5 * MIN, quiet, 5 * MIN)!.lines, ["Finished: parser"]);
  assert.equal(away.since, null);
});

test("chats a usage limit or the login stopped are said so, also when the limit showed a poll late", () => {
  const limit = { kind: "limit" as const, window: "five_hour", text: "You've hit your session limit · resets 4am (Europe/Berlin)", at: NOW };
  const login = { ...limit, kind: "login" as const, window: null, text: "Login expired" };
  const tracker = new TransitionTracker();
  const away = new AwayTracker();
  tracker.track("", groups([{ name: "parser", state: "working" }, { name: "deploy", state: "working" }, { name: "train", state: "working" }]), NOW, undefined, OPTS);
  away.leave(NOW);
  const later = NOW + 30 * MIN;
  const at = (deployStopped: boolean) => groups([
    { name: "parser", state: "idle", since: later, stopped: limit },
    { name: "deploy", state: "idle", since: later, stopped: deployStopped ? limit : null },
    { name: "train", state: "idle", since: later, stopped: login },
  ]);
  away.note(tracker.track("", at(false), later, undefined, OPTS), [], later);
  away.note(tracker.track("", at(true), later + 2000, undefined, OPTS), [], later + 2000); // deploy's limit shows a poll later
  assert.deepEqual(away.back(NOW + 72 * MIN, NOW, at(true), 5 * MIN)!.lines, ["Hit the usage limit: parser, deploy", "Login expired: train"]);
});

test("long lists are cut short", () => {
  const away = new AwayTracker();
  away.leave(NOW);
  const g = groups([{ state: "idle" }]);
  away.note({ finished: ["a", "b", "c", "d", "e"].map((name, i) => ({ id: `c${i}`, session: { ...g[0].sessions[0], name }, took: MIN, at: NOW + MIN, stop: "" as const })) }, [], NOW);
  assert.deepEqual(away.back(NOW + 10 * MIN, 0, g, 5 * MIN)!.lines, ["Finished: a, b, c +2 more"]);
});

test("while you're away, finished chats wait for the digest; chats that need you still notify", async () => {
  const fs = await import("node:fs");
  const os = await import("node:os");
  const path = await import("node:path");
  const vscode = (await import("vscode")) as unknown as typeof import("./vscode-stub");
  process.env.XDG_CACHE_HOME = fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-away-"));
  const { Notifier } = await import("../src/notify");
  const sounds: string[] = [];
  const backend = { sound: (s: string) => sounds.push(s) } as never;
  const notifier = new Notifier(backend, () => undefined);
  const g = groups([{ name: "parser", state: "idle" }, { name: "deploy", state: "waiting" }]);
  const notice = (kind: "done" | "waiting" | "stopped", s: Session) => ({ key: s.key, kind, title: `${s.name} ${kind}`, body: "", session: s });
  const changes = {
    notices: [notice("done", g[0].sessions[0]), notice("waiting", g[0].sessions[1]), notice("stopped", g[0].sessions[0])],
    petEvents: [], sound: "waiting" as const, finished: [],
  };
  vscode.recorded.messages.length = 0;
  notifier.handle({ ...changes, sound: "done" }, { holdDone: true });
  assert.deepEqual(vscode.recorded.messages, ["deploy waiting — "]);
  notifier.handle(changes, {});
  assert.deepEqual(vscode.recorded.messages, ["deploy waiting — ", "parser done — ", "deploy waiting — ", "parser stopped — "]);
  notifier.dispose();
});
