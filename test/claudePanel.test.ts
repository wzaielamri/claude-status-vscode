import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { panelConfigDir, profileKey } from "../src/claudePanel";

test("the panel's config dir: its own setting, else VS Code's, else ~/.claude", () => {
  const home = os.homedir();
  assert.equal(panelConfigDir([{ name: "CLAUDE_CONFIG_DIR", value: "~/.claude-work" }], {}), path.join(home, ".claude-work"));
  assert.equal(panelConfigDir([{ name: "CLAUDE_CONFIG_DIR", value: "  " }], { CLAUDE_CONFIG_DIR: "/opt/claude" }), "/opt/claude");
  assert.equal(panelConfigDir([], {}), path.join(home, ".claude"));
  assert.equal(panelConfigDir(undefined, {}), path.join(home, ".claude"));
  assert.equal(panelConfigDir([null, "x", { name: "OTHER", value: "1" }], {}), path.join(home, ".claude"), "odd entries are skipped");
});

test("a profile is named by its real folder, like the backend does", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-test-"));
  fs.mkdirSync(path.join(dir, ".claude-personal"));
  fs.symlinkSync(path.join(dir, ".claude-personal"), path.join(dir, ".claude"));
  assert.equal(profileKey(path.join(dir, ".claude")), ".claude-personal", "a link names the profile it points to");
  assert.equal(profileKey(path.join(dir, ".claude-personal")), ".claude-personal");
  assert.equal(profileKey(path.join(dir, ".claude-gone")), ".claude-gone", "a missing folder keeps its name");
});
