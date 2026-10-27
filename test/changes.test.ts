import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import * as vscode from "vscode";
import { changesResources, manifestOf, openComparison, readComparison, type Comparison } from "../src/changes";

function world() {
  const cache = fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-changes-"));
  const root = path.join(cache, "changes");
  const folder = path.join(root, "20261018-1");
  fs.mkdirSync(path.join(folder, "0"), { recursive: true });
  const original = path.join(folder, "0", "parse.ts");
  fs.writeFileSync(original, "old\n");
  const comparison: Comparison = {
    title: "Fix the parser: all changes",
    folder: "/home/alice/app",
    items: [
      { label: "parse.ts", path: "/home/alice/app/parse.ts", original, created: false, deleted: false },
      { label: "new.ts", path: "/home/alice/app/new.ts", original, created: true, deleted: false },
      { label: "gone.ts", path: "/home/alice/app/gone.ts", original, created: false, deleted: true },
    ],
  };
  const manifest = path.join(folder, "changes.json");
  fs.writeFileSync(manifest, JSON.stringify(comparison));
  return { cache, root, manifest, comparison, original };
}

test("a changes link is only followed into the widget's own comparisons", () => {
  const { cache, root, manifest } = world();
  const q = (file: string) => `file=${encodeURIComponent(file)}`;
  assert.equal(manifestOf({ path: "/changes", query: q(manifest) }, root), manifest);
  assert.equal(manifestOf({ path: "/other", query: q(manifest) }, root), null);
  assert.equal(manifestOf({ path: "/changes", query: q("/etc/passwd") }, root), null);
  assert.equal(manifestOf({ path: "/changes", query: q(path.join(root, "..", "x.json")) }, root), null);
  assert.equal(manifestOf({ path: "/changes", query: "" }, root), null);
  // a link inside that leads out
  fs.symlinkSync("/etc", path.join(root, "out"));
  assert.equal(manifestOf({ path: "/changes", query: q(path.join(root, "out", "hostname")) }, root), null);
  assert.equal(readComparison(manifest, root)?.items.length, 3);
  // copies from elsewhere are refused
  const bad = path.join(root, "bad.json");
  fs.writeFileSync(bad, JSON.stringify({ title: "x", folder: "", items: [{ label: "a", path: "/a", original: "/etc/hostname", created: false, deleted: false }] }));
  assert.equal(readComparison(bad, root), null);
  fs.rmSync(cache, { recursive: true });
});

test("the changes view gets each file before and now", async () => {
  const { cache, comparison, original } = world();
  const res = changesResources(comparison).map((r) => r.map((u) => u?.path));
  assert.deepEqual(res, [
    ["/home/alice/app/parse.ts", original, "/home/alice/app/parse.ts"],
    ["/home/alice/app/new.ts", undefined, "/home/alice/app/new.ts"],
    ["/home/alice/app/gone.ts", original, undefined],
  ]);
  const calls: unknown[][] = [];
  const commands = vscode.commands as unknown as { executeCommand: (...a: unknown[]) => Promise<unknown> };
  const real = commands.executeCommand;
  try {
    commands.executeCommand = async (...a: unknown[]) => void calls.push(a);
    assert.equal(await openComparison(comparison), "");
    assert.equal(calls[0][0], "vscode.changes");
    assert.equal(calls[0][1], "Fix the parser: all changes");
    // an older VS Code without the changes view: one diff per file
    calls.length = 0;
    commands.executeCommand = async (...a: unknown[]) => {
      if (a[0] === "vscode.changes") throw new Error("command 'vscode.changes' not found");
      calls.push(a);
    };
    assert.equal(await openComparison(comparison), "");
    assert.deepEqual(calls.map((c) => c[0]), ["vscode.diff", "vscode.diff", "vscode.open"]);
  } finally {
    commands.executeCommand = real;
    fs.rmSync(cache, { recursive: true });
  }
});
