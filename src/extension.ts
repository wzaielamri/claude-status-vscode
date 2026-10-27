// Talpy Den for VS Code. Runs on this computer only ("extensionKind":
// ["ui"]), also in Remote-SSH windows: it reads this computer's Claude Code
// files and reaches SSH hosts with this computer's ssh, so one view shows them
// all, whichever machine the window is connected to.

import * as path from "node:path";
import * as vscode from "vscode";
import { Accounts } from "./accounts";
import { AccountSwitch } from "./accountSwitch";
import { Actions } from "./actions";
import { Backend } from "./backend";
import { configDirArgs } from "./config";
import { openChangesLink } from "./changes";
import { ChatPanes } from "./chatPane";
import { answerPermissionsHere, liveModeHere } from "./relay";
import { extraConfigDirs, settings } from "./config";
import { markdownLinkFile, openAgentRunFile } from "./agentRun";
import { Dashboard } from "./dashboard";
import { carryOverSettings, settingNames } from "./legacySettings";
import { Notifier } from "./notify";
import { PlanReview, planFileOf } from "./planReview";
import { allGroups, countStates } from "./shared/model";
import { StatusBar } from "./statusBar";
import { registerPreview, setUpUsageBars } from "./statusline";
import { terminalLinkOf, Terminals } from "./terminals";
import { ViewHost } from "./view";

/** Once: the settings of the first name (claudeStatus.*) become talpyDen.* */
async function carryOverOldSettings(context: vscode.ExtensionContext, log: vscode.LogOutputChannel): Promise<void> {
  const flag = "settingsCarriedOver";
  try {
    if (context.globalState?.get(flag)) return;
    const old = vscode.workspace.getConfiguration("claudeStatus");
    if (typeof old.inspect !== "function") return;
    const copied = await carryOverSettings(old, vscode.workspace.getConfiguration("talpyDen"), settingNames(context.extension.packageJSON), {
      global: vscode.ConfigurationTarget.Global,
      workspace: vscode.ConfigurationTarget.Workspace,
    });
    if (copied > 0) log.info(`Took over ${copied} setting(s) of Claude Status (claudeStatus.*)`);
    await context.globalState.update(flag, true);
  } catch (e) {
    log.warn(`Couldn't take over the old settings: ${(e as Error).message}`);
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const log = vscode.window.createOutputChannel("Talpy Den", { log: true });
  void carryOverOldSettings(context, log);
  const backend = new Backend(context.extensionPath, settings.python);
  const terminals = new Terminals();
  // Notifications open chats through the actions, which need the dashboard
  let actions: Actions | undefined;
  const notifier = new Notifier(backend, (chat) => void actions?.open(chat));
  const dash = new Dashboard(backend, notifier, log);
  const statusBar = new StatusBar(); // (first: the main status bar item)
  const account = new AccountSwitch(dash, backend);
  const answerOldest = async (decision: "allow" | "deny"): Promise<void> => {
    const r = await backend.pane(["--oldest", decision], 20000);
    const message = String(r.message ?? "");
    if (r.ok) void vscode.window.setStatusBarMessage(`$(check) ${message}`, 5000);
    else void vscode.window.showInformationMessage(`Talpy Den: ${message || "Couldn't answer"}`);
    dash.refreshAll();
  };
  actions = new Actions(backend, dash, terminals, log, account);
  const accounts = new Accounts(backend, dash, terminals, account, log);
  const view = new ViewHost(context.extensionUri, dash, actions);
  // The Monday recap: once a week, the last 7 days in a few words; Open the
  // Recap shows the view on its Recap tab (week.py marks the week: the widget
  // and other windows don't show it again)
  const mondayRecap = async (): Promise<void> => {
    if (!notifier.isLeader() || !vscode.workspace.getConfiguration("talpyDen").get<boolean>("mondayRecap", true)) return;
    const r = await backend.mondayRecap(configDirArgs());
    if (!r?.ok || !r.due) return;
    const h = (ms: number) => Math.round(ms / 3600000);
    const parts = [`Claude worked ${h(r.workedMs)} h over ${r.chats} chats; you, ${h(r.youMs)} h and ${r.prompts} prompts.`];
    if (r.project) parts.push(`Most of it in ${r.project}.`);
    if (r.workedBefore > 0) {
      const change = Math.round(((r.workedMs - r.workedBefore) / r.workedBefore) * 100);
      parts.push(change >= 0 ? `${change}% more than the week before.` : `${-change}% less than the week before.`);
    }
    const choice = await vscode.window.showInformationMessage(`Your week with Claude: ${parts.join(" ")}`, "Open the Recap");
    if (choice === "Open the Recap") {
      view.showTab("week");
      await vscode.commands.executeCommand("talpyDen.view.focus");
    }
  };
  const mondayTimer = setInterval(() => void mondayRecap(), 30 * 60000);
  const mondayFirst = setTimeout(() => void mondayRecap(), 60000);
  context.subscriptions.push({ dispose: () => (clearInterval(mondayTimer), clearTimeout(mondayFirst)) });
  const a = actions;
  const review = new PlanReview(extraConfigDirs);
  a.planReview = review;
  // each chat's tab: its conversation, a reply box, what it asks, its review
  const panes = new ChatPanes(context.extensionUri, backend, dash, actions, terminals, log);
  view.panes = panes;
  notifier.panes = panes;

  // Send Feedback to Claude, for a plan's file (the editor's, or the active one)
  const sendFeedback = (uri?: vscode.Uri) => {
    const target = uri instanceof vscode.Uri ? uri : vscode.window.activeTextEditor?.document.uri;
    const file = target ? planFileOf(target, extraConfigDirs()) : null;
    if (!file) return void vscode.window.showInformationMessage("Talpy Den: open a plan's file first (✎ next to a plan).");
    const state = dash.state();
    const groups = state.status?.groups ?? [];
    const listed = groups.flatMap((g) => g.plans ?? []).find((p) => p.path === file.file);
    const row = groups.flatMap((g) => g.sessions).find((s) => s.plan?.path === file.file);
    void a.sendPlanFeedback(
      listed ?? {
        slug: file.slug,
        path: file.file,
        profileDir: row?.profileDir ?? path.dirname(path.dirname(file.file)),
        chatId: row ? row.chatId : null,
        title: row?.plan?.title,
      },
    );
  };

  context.subscriptions.push(
    log,
    terminals,
    notifier,
    dash,
    view,
    statusBar,
    account,
    registerPreview(),
    vscode.window.registerWebviewViewProvider(ViewHost.viewId, view, { webviewOptions: { retainContextWhenHidden: true } }),
    dash.onState((state) => {
      statusBar.update(state);
      account.update(state);
      view.setBadge(countStates(allGroups(state.status, state.hosts)).waiting);
    }),
    review,
    vscode.commands.registerCommand("talpyDen.planComment.add", (reply: vscode.CommentReply) => review.add(reply)),
    vscode.commands.registerCommand("talpyDen.planComment.delete", (thread: vscode.CommentThread) => review.remove(thread)),
    vscode.commands.registerCommand("talpyDen.sendPlanFeedback", sendFeedback),
    vscode.commands.registerCommand("talpyDen.switchAccount", () => account.switch()),
    vscode.commands.registerCommand("talpyDen.accounts", () => accounts.manage()),
    vscode.commands.registerCommand("talpyDen.addAccount", () => accounts.add()),
    vscode.commands.registerCommand("talpyDen.nextChat", () => a.nextChat()),
    // Keyboard answers: the chat that has waited longest for a permission (a
    // plan: approve it, or keep planning), through the hook (pane.py --oldest)
    vscode.commands.registerCommand("talpyDen.allowOldest", () => answerOldest("allow")),
    vscode.commands.registerCommand("talpyDen.denyOldest", () => answerOldest("deny")),
    // the Plasma and GNOME widgets hand over what a chat changed (changes.ts)
    // and which terminal a chat runs in (terminals.ts)
    vscode.window.registerUriHandler({
      handleUri: async (uri) => {
        if (uri.path.replace(/\/+$/, "") === "/markdown") {
          // a plan or a subagent's run, in the Markdown preview
          const file = markdownLinkFile(uri, undefined, extraConfigDirs());
          if (!file) {
            log.warn(`Ignored a link that isn't a plan or a run of the Talpy Den widget: ${uri.toString()}`);
            return;
          }
          const error = await openAgentRunFile(file);
          if (error) void vscode.window.showErrorMessage(`Talpy Den: couldn't show ${file}: ${error}`);
          return;
        }
        const link = terminalLinkOf(uri);
        if (!link) return openChangesLink(uri, log);
        if (await terminals.showLinked(link, dash.currentHost)) return;
        log.warn(`No terminal of this window runs that chat: ${uri.toString()}`);
        void vscode.window.showWarningMessage("Talpy Den: this window has no terminal running that chat.");
      },
    }),
    vscode.commands.registerCommand("talpyDen.refresh", () => dash.refreshAll()),
    vscode.commands.registerCommand("talpyDen.forgetRecapHistory", () => dash.forgetRecapHistory()),
    vscode.commands.registerCommand("talpyDen.pickHosts", () => a.pickHosts()),
    vscode.commands.registerCommand("talpyDen.openInEditor", () => view.openInEditor()),
    vscode.commands.registerCommand("talpyDen.newChat", () => a.newChat()),
    vscode.commands.registerCommand("talpyDen.findChat", () => a.findChat()),
    vscode.commands.registerCommand("talpyDen.showRemoved", () => a.showRemoved()),
    vscode.commands.registerCommand("talpyDen.setupUsageBars", () => setUpUsageBars(context.extensionUri)),
    vscode.commands.registerCommand("talpyDen.removeUsageBars", () => setUpUsageBars(context.extensionUri, true)),
    vscode.commands.registerCommand("talpyDen.openUsagePage", () =>
      vscode.env.openExternal(vscode.Uri.parse("https://claude.ai/settings/usage")),
    ),
    vscode.commands.registerCommand("talpyDen.toggleDemo", () => {
      const on = dash.toggleDemo();
      void vscode.window.showInformationMessage(on ? "Talpy Den shows made-up demo chats now." : "Talpy Den shows your chats again.");
    }),
    vscode.commands.registerCommand("talpyDen.showLog", () => log.show()),
    vscode.commands.registerCommand("talpyDen.answerPermissions", () => answerPermissionsHere(backend, dash)),
    vscode.commands.registerCommand("talpyDen.liveMode", () => liveModeHere(backend, dash)),
    panes,
  );
  statusBar.update(dash.state());
  void dash.start();
}

export function deactivate(): void {
  // everything is in context.subscriptions
}
