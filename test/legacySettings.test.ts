import assert from "node:assert/strict";
import { test } from "node:test";
import { carryOverSettings, type Inspected, type SettingsScope, settingNames } from "../src/legacySettings";

const GLOBAL = 1;
const WORKSPACE = 2;

function scope(values: Record<string, Inspected>): SettingsScope & { writes: [string, unknown, number][] } {
  const writes: [string, unknown, number][] = [];
  return {
    writes,
    inspect: (key) => values[key],
    update: async (key, value, target) => {
      writes.push([key, value, target]);
    },
  };
}

test("the old settings carry over, user's and workspace's", async () => {
  const old = scope({ newChatModel: { globalValue: "opus" }, sshHosts: { globalValue: [], workspaceValue: ["lab"] } });
  const now = scope({});
  const n = await carryOverSettings(old, now, ["newChatModel", "sshHosts", "reopenIn"], { global: GLOBAL, workspace: WORKSPACE });
  assert.equal(n, 3);
  assert.deepEqual(now.writes, [
    ["newChatModel", "opus", GLOBAL],
    ["sshHosts", [], GLOBAL],
    ["sshHosts", ["lab"], WORKSPACE],
  ]);
});

test("a value the new setting has already is kept", async () => {
  const old = scope({ newChatModel: { globalValue: "opus", workspaceValue: "haiku" } });
  const now = scope({ newChatModel: { globalValue: "sonnet" } });
  await carryOverSettings(old, now, ["newChatModel"], { global: GLOBAL, workspace: WORKSPACE });
  assert.deepEqual(now.writes, [["newChatModel", "haiku", WORKSPACE]]);
});

test("setting names come from the manifest", () => {
  const manifest = { contributes: { configuration: { properties: { "talpyDen.a": {}, "talpyDen.b": {}, "other.c": {} } } } };
  assert.deepEqual(settingNames(manifest), ["a", "b"]);
  assert.deepEqual(settingNames({}), []);
});
