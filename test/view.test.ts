// The page is only a picture of the state: what it sends back is checked
// against the extension's own state (the chat, its commands, the host), and
// the page itself loads nothing but its own script and files.

import assert from "node:assert/strict";
import { test } from "node:test";
import * as vscode from "vscode";
import { demoState } from "../src/shared/demo";
import { chatRefOf } from "../src/shared/model";
import type { ViewState } from "../src/shared/types";
import { ViewHost } from "../src/view";

const NOW = 1_800_000_000_000;

function setup(state: ViewState = demoState(NOW)) {
  const calls: { name: string; args: unknown[] }[] = [];
  const record = (name: string) => (...args: unknown[]) => {
    calls.push({ name, args });
    return Promise.resolve();
  };
  const actions = new Proxy({}, { get: (_target, name: string) => record(name) });
  const dash = {
    onState: () => ({ dispose() {} }),
    onPetEvent: () => ({ dispose() {} }),
    onLive: () => ({ dispose() {} }),
    state: () => state,
    shownHosts: () => state.hosts.map((h) => h.name),
    setVisible() {},
    dismissDigest: record("dismissDigest"),
    pollHost: record("pollHost"),
    refreshAll: record("refreshAll"),
    startQueued: (t: { id: string }) => record("startQueued")(t.id),
  };
  const host = new ViewHost(vscode.Uri.file("/ext") as never, dash as never, actions as never);
  const priv = host as unknown as { receive(webview: unknown, m: unknown): void; html(webview: unknown, editorTab: boolean): string };
  return {
    state,
    calls,
    receive: (m: unknown) => priv.receive({ postMessage: () => Promise.resolve(true) }, m),
    html: (webview: unknown, editorTab = false) => priv.html(webview, editorTab),
  };
}

test("a chat the page names is the extension's own row, not what the page says about it", () => {
  const { state, calls, receive } = setup();
  const local = state.status!.groups[0].sessions[0];
  receive({ type: "open", chat: { key: local.key, openCommand: "touch /tmp/pwned", stopCommand: "reboot", pid: 1, name: "forged" } });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].name, "open");
  assert.deepEqual(calls[0].args[0], chatRefOf(local, undefined));

  calls.length = 0;
  const hostName = state.hosts[0].name;
  const remote = state.hosts[0].result!.groups[0].sessions[0];
  receive({ type: "stop", chat: { host: hostName, key: remote.key, stopCommand: "reboot" } });
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].args[0], chatRefOf(remote, hostName));
});

test("a chat that isn't in the extension's state, and a message that isn't one, do nothing", () => {
  const { state, calls, receive } = setup();
  const local = state.status!.groups[0].sessions[0];
  receive({ type: "open", chat: { key: "nope" } });
  receive({ type: "open", chat: { host: "nope", key: local.key } }); // that host's rows only
  receive({ type: "open", chat: { host: 7, key: local.key } });
  receive({ type: "open", chat: "x" });
  receive({ type: "open" });
  receive({ type: 7 });
  receive({});
  receive("open");
  receive(null);
  assert.deepEqual(calls, []);
});

test("agent ids, hosts and accounts in a message are checked", () => {
  const state = demoState(NOW);
  const mine = state.status!;
  mine.canShare = true;
  mine.groups[0].shareProfile ??= ".claude";
  mine.groups.push({ ...structuredClone(mine.groups[0]), id: "work", profileKeys: [".claude-work"], shareProfile: ".claude-work", sessions: [] });
  const { calls, receive } = setup(state);
  const local = mine.groups[0].sessions[0];
  const names = () => calls.splice(0).map((c) => c.name);

  receive({ type: "openAgentRun", chat: { key: local.key }, agentId: "../../etc/passwd" });
  receive({ type: "openAgentRun", chat: { key: local.key }, agentId: 5 });
  assert.deepEqual(names(), []);
  receive({ type: "openAgentRun", chat: { key: local.key }, agentId: "a1b2_c-3" });
  assert.deepEqual(names(), ["openAgentRun"]);

  receive({ type: "loginHost", host: "evil" });
  receive({ type: "disconnectHost", host: "evil" });
  assert.deepEqual(names(), []);
  receive({ type: "loginHost", host: state.hosts[0].name });
  receive({ type: "disconnectHost", host: state.hosts[0].name });
  assert.deepEqual(names(), ["loginHost", "disconnectHost"]);

  // handed over only to an account of the chat's own machine, as the extension knows it
  receive({ type: "handOver", chat: { key: local.key }, profile: "../../evil" });
  assert.deepEqual(names(), []);
  receive({ type: "handOver", chat: { key: local.key }, profile: ".claude-work" });
  assert.deepEqual(names(), ["handOver"]);

  // a task the queue doesn't have can't be cancelled or started
  receive({ type: "unqueue", id: "nope" });
  receive({ type: "startQueued", id: "nope" });
  assert.deepEqual(names(), []);
});

test("the page only runs its own script", () => {
  const { html } = setup();
  const webview = {
    cspSource: "https://example.vscode-cdn.net",
    asWebviewUri: (uri: vscode.Uri) => ({ toString: () => "https://example.vscode-cdn.net" + uri.path }),
  };
  const page = html(webview, true);
  const csp = /http-equiv="Content-Security-Policy" content="([^"]+)"/.exec(page)![1];
  assert.match(csp, /^default-src 'none'; base-uri 'none'; form-action 'none'; /);
  const scriptSrc = /script-src ([^;]+)/.exec(csp)![1];
  const nonce = /^'nonce-([A-Za-z0-9+/=]+)'$/.exec(scriptSrc)![1];
  assert.doesNotMatch(csp, /unsafe-eval/);
  assert.doesNotMatch(csp, /(^|; )(script-src|default-src) [^;]*unsafe-inline/);
  assert.doesNotMatch(csp, /https?:\/\/(?!example\.vscode-cdn\.net)/); // nothing from outside the extension
  const scripts = page.match(/<script\b[^>]*>/g)!;
  assert.equal(scripts.length, 2);
  for (const tag of scripts) assert.ok(tag.includes(`nonce="${nonce}"`), tag);
  assert.equal(page.match(/<\/script>/g)!.length, 2);
  assert.match(page, /window\.TALPY_DEN = \{"editorTab":true\};/);
  // a nonce for each page
  assert.notEqual(/'nonce-([^']+)'/.exec(html(webview))![1], nonce);
});
