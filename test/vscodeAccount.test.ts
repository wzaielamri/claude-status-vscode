// Switching the account VS Code's Claude runs on: the folder it's given and
// the settings list.

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { configDirFor, withConfigDir } from "../src/vscodeAccount";

function home(withLink: boolean): string {
  const h = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "talpy-den-test-")));
  fs.mkdirSync(path.join(h, ".claude-work"));
  fs.mkdirSync(path.join(h, ".claude-personal"));
  if (withLink) fs.symlinkSync(path.join(h, ".claude-work"), path.join(h, ".claude"));
  else fs.mkdirSync(path.join(h, ".claude"));
  return h;
}

test("the folder VS Code's Claude gets for an account", () => {
  const plain = home(false);
  assert.equal(configDirFor(path.join(plain, ".claude"), plain), null, "a plain ~/.claude: no variable (its account is ~/.claude.json)");
  assert.equal(configDirFor(path.join(plain, ".claude-work"), plain), path.join(plain, ".claude-work"));
  const linked = home(true);
  assert.equal(configDirFor(path.join(linked, ".claude-work"), linked), path.join(linked, ".claude"), "named by the link, like terminals");
  assert.equal(configDirFor(path.join(linked, ".claude"), linked), path.join(linked, ".claude"));
  assert.equal(configDirFor(path.join(linked, ".claude-personal"), linked), path.join(linked, ".claude-personal"));
});

test("only CLAUDE_CONFIG_DIR changes in the settings list", () => {
  const vars = [{ name: "FOO", value: "1" }, { name: "CLAUDE_CONFIG_DIR", value: "/old" }];
  assert.deepEqual(withConfigDir(vars, "/new"), [{ name: "FOO", value: "1" }, { name: "CLAUDE_CONFIG_DIR", value: "/new" }]);
  assert.deepEqual(withConfigDir(vars, null), [{ name: "FOO", value: "1" }]);
  assert.deepEqual(withConfigDir(undefined, "/x"), [{ name: "CLAUDE_CONFIG_DIR", value: "/x" }]);
});
