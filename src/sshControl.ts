// Folder for ssh's shared connections (one per host): the same as the
// widget's remote.py control_dir(), so the status checks and the terminals
// share them. Short on purpose: a socket path may have 107 characters, and
// ssh adds 58 to the folder's (/%C and a temporary suffix). Under ~/.cache a
// long home folder name made it too long. $XDG_RUNTIME_DIR (/run/user/<uid>)
// is short and private; else a folder in /tmp that only you own. null: go
// without connection sharing.

import * as fs from "node:fs";
import * as path from "node:path";

const SOCKET_DIR_MAX = 49;

function isDir(p: string): boolean {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

export function controlDir(env: NodeJS.ProcessEnv = process.env, tmp = "/tmp"): string | null {
  const uid = process.getuid ? process.getuid() : -1;
  const runtime = env.XDG_RUNTIME_DIR ?? "";
  const dir = path.isAbsolute(runtime) && isDir(runtime)
    ? path.join(runtime, "talpy-den-ssh")
    : path.join(tmp, `talpy-den-ssh-${uid}`);
  if (uid < 0 || dir.length > SOCKET_DIR_MAX) return null;
  try {
    try {
      fs.mkdirSync(dir, { mode: 0o700 });
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code !== "EEXIST") return null;
    }
    const st = fs.lstatSync(dir);
    if (!st.isDirectory() || st.uid !== uid) return null; // a link, or someone else's: no sockets there
    if (st.mode & 0o077) fs.chmodSync(dir, 0o700);
  } catch {
    return null;
  }
  return dir;
}

// ssh options that share one connection per host (none without a folder)
export function controlOptions(): string[] {
  const dir = controlDir();
  return dir ? ["-o", "ControlMaster=auto", "-o", `ControlPath=${path.join(dir, "%C")}`, "-o", "ControlPersist=300"] : [];
}
