// The settings an earlier version kept under its first name (claudeStatus.*)
// carry over to talpyDen.*: VS Code keeps keys it doesn't know in
// settings.json, so they can still be read and copied once.

export interface Inspected {
  globalValue?: unknown;
  workspaceValue?: unknown;
}

export interface SettingsScope {
  inspect(key: string): Inspected | undefined;
  update(key: string, value: unknown, target: number): PromiseLike<void>;
}

/** Copy each of keys (names without the prefix) the old scope has set, in the
 *  user's settings and in the workspace's, to the new scope unless that has its
 *  own value there. How many values were copied. */
export async function carryOverSettings(
  oldScope: SettingsScope,
  newScope: SettingsScope,
  keys: string[],
  targets: { global: number; workspace: number },
): Promise<number> {
  let copied = 0;
  for (const key of keys) {
    const before = oldScope.inspect(key);
    if (!before) continue;
    const now = newScope.inspect(key);
    for (const [field, target] of [
      ["globalValue", targets.global],
      ["workspaceValue", targets.workspace],
    ] as const) {
      const value = before[field];
      if (value === undefined || now?.[field] !== undefined) continue;
      try {
        await newScope.update(key, value, target);
        copied++;
      } catch {
        // (a setting that can't be set there, e.g. a machine-wide one in a workspace)
      }
    }
  }
  return copied;
}

/** The names (after "talpyDen.") of the settings the extension has. */
export function settingNames(manifest: { contributes?: { configuration?: { properties?: Record<string, unknown> } } }): string[] {
  const properties = manifest.contributes?.configuration?.properties ?? {};
  return Object.keys(properties)
    .filter((k) => k.startsWith("talpyDen."))
    .map((k) => k.slice("talpyDen.".length));
}
