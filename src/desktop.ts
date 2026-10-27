// What the desktop can do for us. KWin (Plasma) and the Talpy Den GNOME
// Shell extension (from the widget's repository) let jump.py bring another
// window to the front; GNOME keeps its sound theme in its own settings.
// The checks match jump.py's, so both sides take the same path.

import { runProcess } from "./backend";

function desktops(env: NodeJS.ProcessEnv): string[] {
  return (env.XDG_CURRENT_DESKTOP ?? "").split(":");
}

export function isKde(env: NodeJS.ProcessEnv = process.env): boolean {
  return desktops(env).includes("KDE");
}

// jump.py's on_gnome(): Ubuntu's session says "ubuntu:GNOME"
export function isGnome(env: NodeJS.ProcessEnv = process.env): boolean {
  const d = desktops(env);
  return d.includes("GNOME") && !d.includes("KDE");
}

// The Talpy Den GNOME Shell extension's D-Bus object (jump.py's GNOME_RAISER)
const GNOME_RAISER_PATH = "/org/gnome/Shell/Extensions/TalpyDen";
const GNOME_RAISER_INTERFACE = "org.gnome.Shell.Extensions.TalpyDen";

// Does GNOME Shell run the Talpy Den extension? Not installed (it can't be
// on GNOME 45 to 48), turned off, or installed since you logged in (GNOME loads
// it at login), it doesn't, and nothing raises windows. (Turned off, its path
// still answers, with nothing in it: hence the look for its interface.)
export async function gnomeRaiserRunning(): Promise<boolean> {
  const r = await runProcess("gdbus", ["introspect", "--session", "--dest", "org.gnome.Shell", "--object-path", GNOME_RAISER_PATH], { timeoutMs: 5000 });
  return r.code === 0 && r.stdout.includes(`interface ${GNOME_RAISER_INTERFACE} {`);
}

// jump.py can raise the window a chat runs in: KWin does it on Plasma, the
// Talpy Den GNOME Shell extension on GNOME (when it runs)
export async function canRaiseWindows(env: NodeJS.ProcessEnv = process.env, gnomeRaiser = gnomeRaiserRunning): Promise<boolean> {
  return isKde(env) || (isGnome(env) && (await gnomeRaiser()));
}

// GNOME's sound theme, for notify_sound.py --theme (elsewhere it finds the
// theme by itself)
export async function gnomeSoundTheme(env: NodeJS.ProcessEnv = process.env): Promise<string | undefined> {
  if (!isGnome(env)) return undefined;
  const r = await runProcess("gsettings", ["get", "org.gnome.desktop.sound", "theme-name"], { timeoutMs: 5000 });
  const m = /^'(.+)'$/.exec(r.stdout.trim());
  return m ? m[1] : undefined;
}
