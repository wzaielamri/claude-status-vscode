// VS Code's Claude, the Claude Code extension's panel: which account it runs
// on (a status bar item) and switching it, through VS Code's own setting
// (vscodeAccount.ts). In a Remote-SSH window the panel runs on the host: the
// setting is a machine setting, which VS Code keeps per host (its Remote
// Settings, written from a window connected to it), so the host's accounts
// are listed and switched there, and this computer's setting stays as it is.

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import * as vscode from "vscode";
import { localPanelProfileKey, panelConfigDir, profileKey } from "./claudePanel";
import type { Backend } from "./backend";
import { configDirArgs } from "./config";
import type { Dashboard } from "./dashboard";
import { machineStatus } from "./shared/model";
import type { Group, ViewState } from "./shared/types";
import { profileDirs } from "./statusline";
import { configDirFor, withConfigDir } from "./vscodeAccount";

const RELOAD = "workbench.action.reloadWindow";

function tildify(p: string): string {
  const home = os.homedir();
  return p === home || p.startsWith(home + "/") ? "~" + p.slice(home.length) : p;
}

function realOr(p: string): string {
  try {
    return fs.realpathSync(p);
  } catch {
    return p;
  }
}

function panelDir(): string {
  return panelConfigDir(vscode.workspace.getConfiguration("claudeCode").get("environmentVariables"));
}

export class AccountSwitch implements vscode.Disposable {
  private item = vscode.window.createStatusBarItem("talpyDen.account", vscode.StatusBarAlignment.Right, 99);
  private startedOn = realOr(panelDir()); // the folder this window's panel started on
  private switching = false;
  private told = "";

  constructor(
    private dash: Dashboard,
    private backend?: Backend, // for pointing ~/.claude at the default account too (accounts.py)
  ) {
    this.item.name = "Talpy Den: VS Code's Claude account";
    this.item.command = "talpyDen.switchAccount";
  }

  dispose(): void {
    this.item.dispose();
  }

  update(state: ViewState): void {
    if (vscode.env.remoteName) return this.updateOnHost(state);
    const groups = state.status?.groups ?? [];
    if (state.demo || groups.length < 2) {
      this.item.hide();
      return;
    }
    const key = localPanelProfileKey();
    const group = groups.find((g) => g.profileKeys.includes(key));
    const dir = panelDir();
    this.item.text = `$(account) Claude: ${group ? group.name : key}`;
    const plain = groups.find((g) => g.plainHere);
    this.item.tooltip =
      `VS Code's Claude runs on ${group ? group.title : key} (${tildify(dir)})` +
      (group?.defaultHere ? ", the default account" : "") +
      (plain && plain !== group ? `; plain claude on ${plain.title}` : "") +
      ". Click to change the default account.";
    this.item.show();
    // Switched elsewhere (another window, or by hand): this window's panel
    // runs on the old account until the window reloads
    const now = realOr(dir);
    if (now !== this.startedOn && !this.switching && this.told !== now) {
      this.told = now;
      void vscode.window
        .showInformationMessage(
          `VS Code's Claude runs on ${group ? group.name : path.basename(now)} now. Reload this window so its Claude panel does too.`,
          "Reload Window",
        )
        .then((c) => c && vscode.commands.executeCommand(RELOAD));
    }
  }

  // A Remote-SSH window: the host's accounts, and which one Claude Code there
  // runs on (its backend reads the VS Code server's settings)
  private updateOnHost(state: ViewState): void {
    const host = this.dash.currentHost;
    const groups = host ? (machineStatus(state, host)?.groups ?? []) : [];
    if (!host || state.demo || groups.length < 2) {
      this.item.hide();
      return;
    }
    const on = groups.find((g) => g.vscodeHere);
    this.item.text = `$(account) Claude: ${on ? on.name : "?"}`;
    this.item.tooltip = `Claude Code on ${host} runs on ${on ? on.title : "an account the widget can't tell"}. Click to switch.`;
    this.item.show();
  }

  async switch(): Promise<void> {
    if (vscode.env.remoteName) return this.switchOnHost();
    if (!vscode.extensions.getExtension("anthropic.claude-code")) {
      void vscode.window.showInformationMessage("The Claude Code extension isn't installed, so there's no VS Code Claude to switch.");
      return;
    }
    const groups = this.dash.state().status?.groups ?? [];
    if (groups.length < 2) {
      const add = await vscode.window.showInformationMessage(
        "There's one Claude account on this computer. Add another one (a ~/.claude-NAME folder, logged in with Claude's own login) to switch between them.",
        "Add Account…",
      );
      if (add) await vscode.commands.executeCommand("talpyDen.addAccount");
      return;
    }
    const key = localPanelProfileKey();
    const pick = await vscode.window.showQuickPick<vscode.QuickPickItem & { group?: Group }>(
      [
        ...groups.map((g) => ({
          label: g.title,
          description: [
            g.defaultHere ? "$(star-full) default" : "",
            g.profileKeys.includes(key) ? "VS Code's Claude" : "",
            g.plainHere ? "plain claude" : "",
            g.loggedOut ? "not logged in" : "",
          ]
            .filter((d) => d)
            .join(" · "),
          group: g,
        })),
        { label: "", kind: vscode.QuickPickItemKind.Separator },
        { label: "$(add) Add Account…" },
      ],
      {
        title: "Default Claude account",
        placeHolder: "Listed first, its usage in the status bar; VS Code's Claude and plain claude (~/.claude, where it's a link) run on it",
      },
    );
    if (pick && !pick.group) await vscode.commands.executeCommand("talpyDen.addAccount");
    else if (pick?.group) await this.switchTo(pick.group);
  }

  // group becomes the default account (the widget's accounts.py --default):
  // VS Code's Claude runs on it (this setting; the window reloads) and plain
  // claude too where ~/.claude is a link. Not while that would cut a chat
  // off: one working in VS Code's Claude panel (the reload), or any chat
  // running through ~/.claude (accounts.py refuses: it would go on on the
  // other account).
  async switchTo(group: Group): Promise<void> {
    const onIt = group.profileKeys.includes(localPanelProfileKey());
    const link = path.join(os.homedir(), ".claude");
    // ~/.claude a link: plain claude can follow the default (one is only
    // re-pointed, never made: without CLAUDE_CONFIG_DIR, plain claude reads
    // ~/.claude.json whatever the link says)
    let linked = false;
    try {
      linked = fs.lstatSync(link).isSymbolicLink();
    } catch {
      // no ~/.claude
    }
    const moveLink = linked && !group.plainHere && !!this.backend;
    if (onIt && !moveLink) {
      void vscode.window.showInformationMessage(`${group.name} is the default account already.`);
      return;
    }
    const dir = profileDirs().find((d) => profileKey(d) === group.chatProfile);
    if (!dir) return this.fail(`Can't find the Claude folder of ${group.name}.`);
    if (!onIt) {
      const working = (this.dash.state().status?.groups ?? [])
        .flatMap((g) => g.sessions)
        .filter((s) => s.entrypoint === "claude-vscode" && s.pid !== null && (s.state === "working" || s.state === "waiting"));
      if (working.length)
        return this.fail(
          `${working.length === 1 ? `“${working[0].name}” is` : `${working.length} chats are`} working in VS Code's Claude: ` +
            "making another account the default reloads this window, which would stop it. Try again once it's done.",
        );
    }
    const ok = await vscode.window.showWarningMessage(
      `Make ${group.name} the default account?`,
      {
        modal: true,
        detail:
          "It's listed first and its usage shows in the status bar. " +
          (onIt ? "" : "VS Code's Claude runs on it: this window reloads (other windows when they reload). ") +
          (moveLink
            ? "Plain `claude` in a terminal runs on it too (~/.claude is pointed at it); refused while a chat runs through ~/.claude."
            : linked
              ? ""
              : "Plain `claude` stays on ~/.claude, a folder.") +
          (onIt
            ? ""
            : "\n\nRemote-SSH windows read this setting too: on a host that doesn't have this folder, set \"claudeCode.environmentVariables\": [] in its Remote Settings."),
      },
      "Make Default",
    );
    if (ok !== "Make Default") return;
    if (moveLink && this.backend) {
      const r = await this.backend.accounts(["--default", group.chatProfile, "--no-vscode", ...configDirArgs()]);
      if (!r.ok) return this.fail(String(r.message ?? "Couldn't make it the default"));
      this.dash.refreshAll();
    }
    // (after ~/.claude moved: the account it links to is named by the link)
    const value = configDirFor(dir);
    if (value === null && process.env.CLAUDE_CONFIG_DIR) {
      // A plain ~/.claude runs without the variable, but VS Code itself has
      // one: its Claude would stay on that
      return this.fail(
        `VS Code was started with CLAUDE_CONFIG_DIR=${process.env.CLAUDE_CONFIG_DIR}, so its Claude keeps using that. ` +
          `Start VS Code without it to use ${group.name}.`,
      );
    }
    const cc = vscode.workspace.getConfiguration("claudeCode");
    const next = withConfigDir(cc.get("environmentVariables"), value);
    if (onIt) {
      // the same account, maybe under the link's name now: no reload needed
      if (realOr(panelDir()) === realOr(dir) && panelDir() !== (value ?? link)) {
        try {
          await cc.update("environmentVariables", next.length ? next : undefined, vscode.ConfigurationTarget.Global);
        } catch {
          // it still runs on the account
        }
      }
      void vscode.window.showInformationMessage(`${group.name} is the default account: plain claude runs on it too now.`);
      return;
    }
    this.switching = true;
    try {
      await cc.update("environmentVariables", next.length ? next : undefined, vscode.ConfigurationTarget.Global);
    } catch (err) {
      this.switching = false;
      return this.fail(`Couldn't change VS Code's Claude settings: ${(err as Error).message}`);
    }
    setTimeout(() => void vscode.commands.executeCommand(RELOAD), 300);
    // the reload can be declined (unsaved files): then other switches count again
    setTimeout(() => (this.switching = false), 15000);
  }

  private async switchOnHost(): Promise<void> {
    const host = this.dash.currentHost;
    if (!host) {
      void vscode.window.showInformationMessage(
        "This window isn't connected to an SSH host (Remote-SSH), so there's no Claude account here to switch.",
      );
      return;
    }
    const groups = machineStatus(this.dash.state(), host)?.groups ?? [];
    if (groups.length < 2) {
      void vscode.window.showInformationMessage(`There's one Claude account on ${host}, so there's nothing to switch to.`);
      return;
    }
    const pick = await vscode.window.showQuickPick(
      groups.map((g) => ({
        label: g.title,
        description: [
          g.defaultHere ? "$(star-full) default" : "",
          g.vscodeHere ? `Claude Code on ${host}` : "",
          g.plainHere ? "plain claude" : "",
        ]
          .filter((d) => d)
          .join(" · "),
        group: g,
      })),
      {
        title: `Default Claude account on ${host}`,
        placeHolder: "Claude Code there (its new chats, in its other windows too) and plain claude there (~/.claude, where it's a link) run on it",
      },
    );
    if (!pick) return;
    if (pick.group.vscodeHere && pick.group.plainHere !== false) {
      void vscode.window.showInformationMessage(`${pick.group.name} is the default account on ${host} already.`);
      return;
    }
    if (await this.switchHost(pick.group, true))
      void vscode.window.showInformationMessage(
        `${pick.group.name} is the default account on ${host} now: new chats start there. Chats already open keep their account.`,
      );
  }

  // Claude Code on this window's SSH host runs on group's account from now
  // on (its new chats, its other windows there too). Written as a user
  // setting from a window connected to the host, VS Code puts this machine
  // setting into the host's Remote Settings; the Claude extension follows it.
  // asDefault: plain claude there too (the host's ~/.claude, where it's a
  // link, through accounts.py there; refused while a chat runs through it).
  async switchHost(group: Group, asDefault = false): Promise<boolean> {
    if (group.configDir === undefined) {
      this.fail(`Can't tell which folder ${group.name} has on ${group.host}: that host's status comes from an older Talpy Den.`);
      return false;
    }
    let value = group.configDir;
    if (asDefault && this.backend && group.host) {
      const r = await this.backend.accounts(["--default", group.chatProfile, "--no-vscode"], group.host);
      if (!r.ok) {
        this.fail(String(r.message || `Couldn't make ${group.name} the default on ${group.host}`));
        return false;
      }
      // (~/.claude may lead there now: the name terminals there use)
      if (typeof r.configDir === "string" || r.configDir === null) value = r.configDir;
    }
    const cc = vscode.workspace.getConfiguration("claudeCode");
    const next = withConfigDir(cc.get("environmentVariables"), value);
    try {
      await cc.update("environmentVariables", next.length ? next : undefined, vscode.ConfigurationTarget.Global);
    } catch (err) {
      this.fail(`Couldn't change Claude Code's settings on ${group.host}: ${(err as Error).message}`);
      return false;
    }
    void this.dash.refreshAll(); // the host's accounts and this item follow
    return true;
  }

  private fail(message: string): void {
    void vscode.window.showErrorMessage(message);
  }
}
