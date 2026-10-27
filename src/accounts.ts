// The Claude accounts on this computer or an SSH host (the widget's
// accounts.py; a host's through remote.py --accounts, over ssh, with nothing
// installed there): add one (a ~/.claude-NAME folder that Claude's own login
// signs in, in a terminal), log in or out, remove one, its claude-NAME
// command, and make one the default (accountSwitch.ts). The login is
// Claude's: the extension never sees it.

import * as vscode from "vscode";
import type { AccountSwitch } from "./accountSwitch";
import type { Backend } from "./backend";
import { configDirArgs } from "./config";
import type { Dashboard } from "./dashboard";
import { accountNameProblem, machineStatus } from "./shared/model";
import type { Group } from "./shared/types";
import type { Place, Terminals } from "./terminals";

// One account, as accounts.py --list says
export interface AccountInfo {
  key: string; // its profile folder's name (.claude-work)
  label: string;
  folder: string; // ~/.claude-work
  main: boolean; // plain claude runs on it (~/.claude, or what ~/.claude links to)
  account: string | null; // the account's display name
  plan: string | null;
  loggedIn: boolean;
  checked: boolean; // loggedIn, method and email are Claude's own answer
  method: string | null;
  email: string | null;
  chats: number; // running on it now
  vscode: boolean; // VS Code's Claude runs on it
  default: boolean; // the default account (listed first; VS Code's Claude and plain claude run on it)
  commandName: string | null; // claude-work
  command: boolean; // ~/.local/bin/claude-work is there (made by Talpy Den)
  notRemovable: string; // why it can't be removed, or ""
}

export function accountDetail(a: AccountInfo): string {
  const parts: string[] = [];
  if (a.default) parts.push("default");
  if (!a.loggedIn) parts.push("not logged in");
  else if (a.method && a.method !== "claude.ai") parts.push(`logged in (${a.method})`);
  else parts.push(...[a.email || a.account || "logged in", a.plan].filter((t): t is string => !!t));
  if (a.main) parts.push("plain claude");
  if (a.vscode) parts.push("VS Code's Claude");
  if (a.chats > 0) parts.push(a.chats === 1 ? "1 chat running" : `${a.chats} chats running`);
  return parts.join(" · ");
}

type Item<T> = vscode.QuickPickItem & { run?: T };

export class Accounts {
  constructor(
    private backend: Backend,
    private dash: Dashboard,
    private terminals: Terminals,
    private switcher: AccountSwitch,
    private log: vscode.LogOutputChannel,
  ) {}

  private linked = false; // ~/.claude is a link there: plain claude can follow the default
  private canDefault = false; // Make Default can point VS Code's Claude or plain claude somewhere there

  // Where a terminal for host opens: here, in this window's host (Remote-SSH), or over ssh
  private place(host?: string): Place {
    if (!host) return { kind: "local" };
    return host === this.dash.currentHost ? { kind: "windowHost" } : { kind: "ssh", host };
  }

  private where(host?: string): string {
    return host ? ` on ${host}` : "";
  }

  private call(args: string[], host?: string): Promise<Record<string, any>> {
    return this.backend.accounts(host ? args : [...args, ...configDirArgs()], host);
  }

  private async list(host?: string): Promise<AccountInfo[] | null> {
    const r = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Window, title: `Claude accounts${this.where(host)}…` },
      () => this.call(["--list", "--check"], host),
    );
    if (!r.ok || !Array.isArray(r.accounts)) {
      this.fail(`Couldn't list the Claude accounts${this.where(host)}: ${String(r.message ?? "no answer")}`);
      return null;
    }
    this.linked = r.linked === true;
    this.canDefault = r.canDefault !== false;
    return r.accounts as AccountInfo[];
  }

  private fail(message: string): void {
    this.log.warn(message);
    void vscode.window.showErrorMessage(message);
  }

  // What accounts.py said, as a message (and the dashboard looks again)
  private said(r: Record<string, any>, failure: string, host?: string): boolean {
    const message = String(r.message ?? "");
    if (r.ok) {
      if (message) void vscode.window.showInformationMessage(message);
    } else this.fail(`${failure}: ${message || "no answer"}`);
    if (host) void this.dash.pollHost(host);
    else this.dash.refreshAll();
    return !!r.ok;
  }

  // This computer, or one of the SSH hosts the view shows
  private async machine(): Promise<{ host?: string } | undefined> {
    const hosts = this.dash.state().hosts.filter((h) => h.result && !h.error).map((h) => h.name);
    if (!hosts.length) return {};
    const pick = await vscode.window.showQuickPick(
      [
        { label: "$(device-desktop) This computer", host: undefined as string | undefined },
        ...hosts.map((h) => ({
          label: `$(server) ${h}`,
          description: h === this.dash.currentHost ? "this window's host" : "over ssh",
          host: h as string | undefined,
        })),
      ],
      { title: "Claude Accounts", placeHolder: "Whose accounts? A host's go over ssh; nothing is installed there" },
    );
    return pick ? { host: pick.host } : undefined;
  }

  async manage(): Promise<void> {
    const on = await this.machine();
    if (!on) return;
    const host = on.host;
    const accounts = await this.list(host);
    if (!accounts) return;
    const items: Item<() => Promise<void>>[] = [
      { label: "$(add) Add Account…", detail: "A ~/.claude-NAME folder, logged in with Claude's own login", run: () => this.add(accounts, host) },
      { label: `Accounts${host ? ` on ${host}` : " on this computer"}`, kind: vscode.QuickPickItemKind.Separator },
      ...accounts.map((a) => ({
        label: `$(${a.loggedIn ? "account" : "circle-slash"}) ${a.label}`,
        description: a.folder,
        detail: accountDetail(a),
        run: () => this.actionsFor(a, host),
      })),
    ];
    const pick = await vscode.window.showQuickPick(items, {
      title: `Claude Accounts${this.where(host)}`,
      placeHolder: "Add an account, or pick one to log in or out, make it the default, remove it, …",
    });
    if (pick?.run) await pick.run();
  }

  private async actionsFor(a: AccountInfo, host?: string): Promise<void> {
    const items: Item<() => Promise<void>>[] = [];
    if (!a.loggedIn)
      items.push({ label: "$(sign-in) Log In…", detail: "A terminal with Claude's own login: sign in in your browser", run: () => this.login(a.key, host) });
    else if (this.canDefault && (!a.default || (this.linked && !a.main)))
      items.push({
        label: "$(star-full) Make Default",
        detail: this.linked
          ? "Listed first, its usage in the status bar; VS Code's Claude and plain claude (~/.claude) run on it"
          : "Listed first, its usage in the status bar; VS Code's Claude runs on it (plain claude stays on ~/.claude, a folder)",
        run: () => this.makeDefault(a, host),
      });
    if (a.commandName)
      items.push(
        a.command
          ? { label: `$(terminal) Remove the ${a.commandName} Command`, run: () => this.command(a, false, host) }
          : {
              label: `$(terminal) Add a ${a.commandName} Command`,
              detail: `In ~/.local/bin${this.where(host)}: runs Claude on this account in any terminal`,
              run: () => this.command(a, true, host),
            },
      );
    if (a.loggedIn)
      items.push({
        label: "$(sign-out) Log Out…",
        detail: a.chats > 0 ? "Close its chats first: they'd lose the login" : "Claude deletes this account's login",
        run: () => this.logout(a, host),
      });
    items.push({ label: "$(trash) Remove…", detail: a.notRemovable || "Logs out and moves the folder to the Trash", run: () => this.remove(a, host) });
    const pick = await vscode.window.showQuickPick(items, { title: `${a.label} (${a.folder}${this.where(host)})`, placeHolder: accountDetail(a) });
    if (pick?.run) await pick.run();
  }

  async add(known?: AccountInfo[], host?: string): Promise<void> {
    const accounts = known ?? (await this.list(host));
    if (!accounts) return;
    const folders = accounts.map((a) => a.folder);
    const name = (
      await vscode.window.showInputBox({
        title: `Add a Claude Account${this.where(host)} (1/3)`,
        prompt: "Its name. The account's folder is ~/.claude-NAME.",
        placeHolder: "e.g. work",
        validateInput: (v) => accountNameProblem(v.trim(), folders) || null,
      })
    )?.trim();
    if (!name) return;
    const share = await vscode.window.showQuickPick(
      [
        ...accounts.map((a) => ({ label: `Shared with ${a.label}`, description: a.folder, key: a.key })),
        { label: "Claude's defaults, nothing shared", description: "a fresh folder", key: "" },
      ],
      {
        title: `Add a Claude Account${this.where(host)} (2/3): settings`,
        placeHolder:
          "Shared: settings.json (your status line), CLAUDE.md, keybindings, your own skills, agents and commands are linked. The login, MCP servers, plugins stay per account.",
      },
    );
    if (!share) return;
    const command = await vscode.window.showQuickPick(
      [
        { label: `Add a claude-${name} command`, description: "in ~/.local/bin: runs Claude on the account in any terminal", on: true },
        { label: "No command", on: false },
      ],
      { title: `Add a Claude Account${this.where(host)} (3/3)` },
    );
    if (!command) return;
    const r = await this.call(
      ["--add", name, ...(share.key ? ["--share-from", share.key] : []), ...(command.on ? ["--command"] : []), "--print-command"],
      host,
    );
    if (!this.said(r, `Couldn't add ${name}`, host) || typeof r.command !== "string") return;
    await this.terminals.run(`login|${host ?? ""}|${r.key}`, `log in to ${name}`, r.command, this.place(host), { private: true });
    void vscode.window.showInformationMessage(
      (host
        ? `Log in to ${name} on ${host} in the terminal: open the address Claude shows in your browser, sign in with the other account, and paste the code you get back.`
        : `Log in to ${name} in the terminal: sign in in your browser with the other account.`) +
        ` Afterwards, Claude Accounts → ${name} → Make Default makes it the account VS Code's Claude and plain claude run on.`,
    );
  }

  private async login(key: string, host?: string): Promise<void> {
    const r = await this.call(["--login", key, "--print-command"], host);
    if (!r.ok || typeof r.command !== "string") return this.fail(`Couldn't log in: ${String(r.message ?? "no answer")}`);
    await this.terminals.run(`login|${host ?? ""}|${key}`, "log in", r.command, this.place(host), { private: true });
  }

  private async logout(a: AccountInfo, host?: string): Promise<void> {
    if (a.chats > 0) return this.fail(`${a.chats === 1 ? "A chat runs" : `${a.chats} chats run`} on ${a.label}: close them first, they'd lose the login.`);
    const ok = await vscode.window.showWarningMessage(
      `Log out of ${a.label}${this.where(host)}?`,
      { modal: true, detail: "Claude deletes its login (claude auth logout). Its chats and history stay, and Log In signs it in again, also with another account." },
      "Log Out",
    );
    if (ok === "Log Out") this.said(await this.call(["--logout", a.key], host), `Couldn't log out of ${a.label}`, host);
  }

  private async remove(a: AccountInfo, host?: string): Promise<void> {
    if (a.notRemovable) return this.fail(`${a.label} isn't removed: ${a.notRemovable}`);
    const ok = await vscode.window.showWarningMessage(
      `Remove ${a.label}${this.where(host)}?`,
      {
        modal: true,
        detail: `It logs out, then moves ${a.folder} to the Trash${host ? ` of ${host}` : ""} with its chats, history and settings (restore it from there if you change your mind). Chats shared with your other accounts stay on them.`,
      },
      "Remove",
    );
    if (ok === "Remove") this.said(await this.call(["--remove", a.key], host), `Couldn't remove ${a.label}`, host);
  }

  private async command(a: AccountInfo, on: boolean, host?: string): Promise<void> {
    this.said(await this.call([on ? "--command" : "--no-command", a.key], host), `Couldn't change ${a.commandName}`, host);
  }

  // This computer's default: VS Code's Claude and plain claude (the switch,
  // which reloads the window). This window's host: Claude Code there (a
  // machine setting) and the host's ~/.claude. Another host: accounts.py
  // there does it all, its VS Code server's settings included.
  private async makeDefault(a: AccountInfo, host?: string): Promise<void> {
    const status = machineStatus(this.dash.state(), host);
    const group: Group | undefined = (status?.groups ?? []).find((g) => g.profileKeys.includes(a.key));
    if (!host || host === this.dash.currentHost) {
      if (!group) return this.fail(`${a.label} isn't in the list yet: try again in a moment.`);
      if (host) {
        if (await this.switcher.switchHost({ ...group, host }, true))
          void vscode.window.showInformationMessage(`${a.label} is the default account on ${host} now.`);
        return;
      }
      return this.switcher.switchTo(group);
    }
    this.said(await this.call(["--default", a.key], host), `Couldn't make ${a.label} the default`, host);
  }
}
