// ssh's connection sockets: a folder short enough for any home folder, only
// yours, and the same one the widget's remote.py uses (they share them).

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";
import { controlDir } from "../src/sshControl";

const posix = process.platform !== "win32";

test("the socket folder is short and only yours", { skip: !posix && "Unix sockets" }, () => {
  const tmp = fs.mkdtempSync("/tmp/cs-test-");
  try {
    const dir = controlDir({ XDG_RUNTIME_DIR: tmp }, "/nonexistent");
    assert.equal(dir, path.join(tmp, "talpy-den-ssh"));
    assert.equal(fs.statSync(dir!).mode & 0o777, 0o700);
    assert.ok(`${dir}/${"0".repeat(40)}.${"x".repeat(16)}`.length <= 107, "what ssh binds fits");
    // no runtime folder: one in /tmp, unless it's a link (or someone else's)
    const fallback = path.join(tmp, `talpy-den-ssh-${process.getuid!()}`);
    assert.equal(controlDir({}, tmp), fallback);
    fs.rmdirSync(fallback);
    fs.symlinkSync(tmp, fallback);
    assert.equal(controlDir({}, tmp), null);
    // a runtime folder so long that the socket path wouldn't fit: none either
    const long = path.join(tmp, "r".repeat(60));
    fs.mkdirSync(long);
    assert.equal(controlDir({ XDG_RUNTIME_DIR: long }, tmp), null);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("the same folder as the widget's remote.py", { skip: !posix && "Unix sockets" }, () => {
  const tmp = fs.mkdtempSync("/tmp/cs-test-");
  try {
    const script = "import sys; sys.path.insert(0, sys.argv[1]); import remote; print(remote.control_dir())";
    const r = spawnSync("python3", ["-I", "-B", "-c", script, path.resolve("vendor/code")], {
      env: { PATH: process.env.PATH, HOME: tmp, XDG_RUNTIME_DIR: tmp },
      encoding: "utf8",
    });
    assert.equal(r.status, 0, r.stderr);
    assert.equal(r.stdout.trim(), controlDir({ XDG_RUNTIME_DIR: tmp }));
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
