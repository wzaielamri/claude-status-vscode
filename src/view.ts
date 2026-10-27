// The webview: in the Activity Bar view, and optionally as an editor tab.
// Both get the same page and the same state; the page only talks to the
// extension through messages (src/shared/types.ts), never to the network.

import * as crypto from "node:crypto";
import * as vscode from "vscode";
import type { Actions } from "./actions";
import type { ChatPanes } from "./chatPane";
import { setReopenIn, settings } from "./config";
import type { Dashboard } from "./dashboard";
import { findChatRef, findMemory, findPlan, goOnAt, machineStatus, shareAccounts } from "./shared/model";
import { answersOf } from "./shared/pane";
import { permsHost, weekAsked, weekRevokeAsked, weekRuleAsked } from "./shared/week";
import type { ChatRef, Group, HostMessage, PlanItem, WebviewMessage } from "./shared/types";
import { isDecision } from "./shared/types";

const AGENT_ID = /^[A-Za-z0-9_-]{1,100}$/;
const CHAT_ID = /^[A-Za-z0-9_-]{1,80}$/;
const PROFILE_DIR = /^[A-Za-z0-9._-]{1,100}$/;
const TIP_COMMAND = /^\/[a-z][a-z-]{0,30}( [a-z0-9-]{1,30})?$/; // what a Recap tab tip suggests, e.g. "/effort xhigh"

export class ViewHost implements vscode.WebviewViewProvider, vscode.Disposable {
  static readonly viewId = "talpyDen.view";
  private webviews = new Set<vscode.Webview>();
  private visibleCount = 0;
  private panel: vscode.WebviewPanel | undefined;
  private view: vscode.WebviewView | undefined;
  private disposables: vscode.Disposable[] = [];
  panes?: ChatPanes; // the chats' tabs (set once they're there)

  constructor(
    private extensionUri: vscode.Uri,
    private dash: Dashboard,
    private actions: Actions,
  ) {
    this.disposables.push(
      dash.onState((state) => this.broadcast({ type: "state", state })),
      dash.onPetEvent((e) => this.broadcast({ type: "petEvent", kind: e.kind, text: e.text, key: e.key })),
      // (each step between checks: only what the chats do now, not the whole state)
      dash.onLive((e) => this.broadcast({ type: "live", live: e.live })),
    );
  }

  dispose(): void {
    this.panel?.dispose();
    for (const d of this.disposables) d.dispose();
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    this.setUp(view.webview, false);
    let visible = view.visible;
    if (visible) this.visibleCount++;
    view.onDidChangeVisibility(() => {
      if (view.visible === visible) return;
      visible = view.visible;
      this.visibleCount += visible ? 1 : -1;
      this.visibilityChanged(view.webview, visible);
    });
    view.onDidDispose(() => {
      if (visible) this.visibleCount--;
      this.webviews.delete(view.webview);
      this.view = undefined;
      this.dash.setVisible(this.visibleCount > 0);
    });
    this.dash.setVisible(this.visibleCount > 0);
  }

  openInEditor(): void {
    if (this.panel) {
      this.panel.reveal();
      return;
    }
    const panel = vscode.window.createWebviewPanel("talpyDen.panel", "Talpy Den", vscode.ViewColumn.Active, {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: this.roots(),
    });
    panel.iconPath = vscode.Uri.joinPath(this.extensionUri, "media", "talpy.svg");
    this.panel = panel;
    this.setUp(panel.webview, true);
    let visible = panel.visible;
    if (visible) this.visibleCount++;
    panel.onDidChangeViewState(() => {
      if (panel.visible === visible) return;
      visible = panel.visible;
      this.visibleCount += visible ? 1 : -1;
      this.visibilityChanged(panel.webview, visible);
    });
    panel.onDidDispose(() => {
      if (visible) this.visibleCount--;
      this.webviews.delete(panel.webview);
      this.panel = undefined;
      this.dash.setVisible(this.visibleCount > 0);
    });
    this.dash.setVisible(this.visibleCount > 0);
  }

  // The waiting count on the Activity Bar icon
  setBadge(waiting: number): void {
    if (this.view) this.view.badge = waiting ? { value: waiting, tooltip: `${waiting} chat${waiting === 1 ? " needs" : "s need"} you` } : undefined;
  }

  private visibilityChanged(webview: vscode.Webview, visible: boolean): void {
    void webview.postMessage({ type: "visible", visible } satisfies HostMessage);
    this.dash.setVisible(this.visibleCount > 0);
  }

  private roots(): vscode.Uri[] {
    return [vscode.Uri.joinPath(this.extensionUri, "dist"), vscode.Uri.joinPath(this.extensionUri, "vendor", "pets")];
  }

  private setUp(webview: vscode.Webview, editorTab: boolean): void {
    webview.options = { enableScripts: true, localResourceRoots: this.roots() };
    webview.html = this.html(webview, editorTab);
    this.webviews.add(webview);
    webview.onDidReceiveMessage((m: unknown) => this.receive(webview, m));
  }

  // A page that loads while its view is hidden mustn't start its pets
  private isVisible(webview: vscode.Webview): boolean {
    if (this.view?.webview === webview) return this.view.visible;
    if (this.panel?.webview === webview) return this.panel.visible;
    return true;
  }

  // A tab of the view (also for a page that loads after this: it asks when it's ready)
  private shownTab: "chats" | "week" | "perms" | null = null;
  showTab(tab: "chats" | "week" | "perms"): void {
    this.shownTab = tab;
    this.broadcast({ type: "showTab", tab });
  }

  private broadcast(message: HostMessage): void {
    for (const w of this.webviews) void w.postMessage(message);
  }

  // A chat the page names, as the extension knows it: what runs for it (its
  // open or stop command, its process) comes from the extension's own state,
  // never from the page
  private chatOf(m: { chat?: unknown }): ChatRef | null {
    const ref = m.chat && typeof m.chat === "object" ? (m.chat as { host?: unknown; key?: unknown }) : null;
    const state = this.dash.state();
    return ref ? findChatRef(state.status, state.hosts, ref.host, ref.key) : null;
  }

  // One of an account's plans the page names, as the extension knows it now
  private planOf(m: { groupId?: unknown; host?: unknown; plan?: unknown }): { plan: PlanItem; group: Group } | null {
    const state = this.dash.state();
    return findPlan(state.status, state.hosts, m.host, m.groupId, m.plan);
  }

  private receive(webview: vscode.Webview, raw: unknown): void {
    if (!raw || typeof raw !== "object" || typeof (raw as { type?: unknown }).type !== "string") return;
    const m = raw as WebviewMessage;
    const text = (v: unknown) => (typeof v === "string" && v ? v : undefined);
    const chat = "chat" in m ? this.chatOf(m) : null;
    switch (m.type) {
      case "ready":
        void webview.postMessage({ type: "state", state: this.dash.state() } satisfies HostMessage);
        void webview.postMessage({ type: "visible", visible: this.isVisible(webview) } satisfies HostMessage);
        if (this.shownTab) void webview.postMessage({ type: "showTab", tab: this.shownTab } satisfies HostMessage);
        this.shownTab = null;
        break;
      case "open":
        // (a click on a chat: its tab, unless clicks open the chat itself)
        if (chat && this.panes && settings.clickOpensPane()) this.panes.open(chat, "chat");
        else if (chat) void this.actions.open(chat);
        break;
      case "openPane":
        if (chat && this.panes) this.panes.open(chat, m.tab === "review" ? "review" : "chat");
        break;
      case "openWindow":
        if (chat) void this.actions.open(chat);
        break;
      case "answerPermission":
        if (chat && this.panes && isDecision(m.decision))
          void this.panes.answer(chat, m.decision, typeof m.note === "string" ? m.note : "", answersOf(m.answers) ?? undefined);
        break;
      case "openInClaudeCode":
        if (chat) void this.actions.openInClaudeCode(chat);
        break;
      case "moveToClaudeCode":
        if (chat) void this.actions.moveToClaudeCode(chat);
        break;
      case "setReopenIn":
        if (m.where === "terminal" || m.where === "claudeCode") void setReopenIn(m.where);
        break;
      case "stop":
        if (chat) void this.actions.stop(chat);
        break;
      case "dismiss":
        if (chat) void this.actions.dismiss(chat);
        break;
      case "share":
        if (chat) void this.actions.share(chat);
        break;
      case "compareChanges":
        if (chat) void this.actions.compareChanges(chat, m.turn === true);
        break;
      case "openAgentRun":
        if (chat && typeof m.agentId === "string" && AGENT_ID.test(m.agentId)) void this.actions.openAgentRun(chat, m.agentId);
        break;
      case "dismissDigest":
        this.dash.dismissDigest();
        break;
      case "newChat":
        void this.actions.newChat(text(m.groupId), text(m.host));
        break;
      case "handOver": {
        // only to one of the chat's machine's accounts, as the extension knows them
        const state = this.dash.state();
        const to = shareAccounts(machineStatus(state, chat?.host)).find((g) => g.shareProfile === m.profile);
        if (chat && to?.shareProfile) void this.actions.handOver(chat, to.shareProfile);
        break;
      }
      case "openPlan": {
        if (chat?.plan) void this.actions.openPlan({ ...chat.plan, profileDir: chat.profileDir }, chat.host, m.edit === true);
        else {
          const found = this.planOf(m);
          if (found) void this.actions.openPlan(found.plan, found.group.host, m.edit === true);
        }
        break;
      }
      case "sendPlanFeedback": {
        // (this computer's plans: an SSH host's chat isn't sent feedback from here)
        if (chat?.plan && !chat.host) void this.actions.sendPlanFeedback({ ...chat.plan, profileDir: chat.profileDir, chatId: chat.chatId ?? chat.sessionId });
        else if (!chat) {
          const found = this.planOf(m);
          if (found && !found.group.host) void this.actions.sendPlanFeedback(found.plan);
        }
        break;
      }
      case "openMemory": {
        const state = this.dash.state();
        const found = findMemory(state.status, state.hosts, m.host, m.groupId, m.memory);
        if (found) void this.actions.openMemory(found.memory, found.group.host, m.edit === true);
        break;
      }
      case "carryOutPlan": {
        const found = this.planOf(m);
        if (found) void this.actions.newChat(found.group.id, found.group.host, found.plan);
        break;
      }
      case "continuePlanChat": {
        const found = this.planOf(m);
        if (found) void this.actions.continuePlanChat(found.plan, found.group);
        break;
      }
      case "sharePlan": {
        const found = this.planOf(m);
        if (found) void this.actions.sharePlan(found.plan, found.group);
        break;
      }
      case "findChat":
        void this.actions.findChat();
        break;
      case "startQueued": {
        // (a task of this computer's queue, as the extension knows it)
        const t = this.dash.state().status?.queue?.find((q) => q.id === m.id);
        if (t) void this.dash.startQueued(t);
        break;
      }
      case "openOverlap": {
        // (found in the extension's own state: the row, then its first overlap's row)
        const state = this.dash.state();
        const where = chat?.host ? state.hosts.find((h) => h.name === chat.host)?.result : state.status;
        const row = where?.groups.flatMap((g) => g.sessions).find((s) => s.key === chat?.key);
        const other = row?.overlaps?.[0];
        const ref = other ? findChatRef(state.status, state.hosts, chat?.host, other.key) : null;
        if (ref) void this.actions.open(ref);
        break;
      }
      case "unqueue":
        if (this.dash.state().status?.queue?.some((q) => q.id === m.id)) void this.actions.unqueue(m.id);
        break;
      case "star": {
        // (its id from the extension's own state: a chat, or one of an account's plans)
        const found = chat ? null : this.planOf(m);
        if (chat) void this.actions.star("chats", chat.chatId || chat.sessionId, chat.host, m.on === true);
        else if (found) void this.actions.star("plans", found.plan.slug, found.group.host, m.on === true);
        break;
      }
      case "keepGoing":
        if (chat && !chat.host) void this.actions.keepGoing(chat, m.on === true); // (this computer's chats)
        break;
      case "queueContinue": {
        // (found in the extension's own state: the row or recent chat, when it can go on, and its account)
        const state = this.dash.state();
        const where = chat?.host ? state.hosts.find((h) => h.name === chat.host)?.result : state.status;
        for (const g of where?.groups ?? []) {
          const item = [...(g.sessions ?? []), ...(g.recent ?? []), ...(g.starred ?? [])].find((x) => x.key === chat?.key);
          if (!item) continue;
          const at = goOnAt(item);
          if (chat && at) void this.actions.queueContinue(chat, at, g.title, m.keep === true);
          break;
        }
        break;
      }
      case "loginHost":
        if (text(m.host) && this.dash.shownHosts().includes(m.host)) void this.actions.loginHost(m.host);
        break;
      case "disconnectHost":
        if (text(m.host) && this.dash.shownHosts().includes(m.host)) void this.actions.disconnectHost(m.host);
        break;
      case "login":
        void this.actions.login(text(m.groupId), text(m.host));
        break;
      case "refresh":
        if (text(m.host)) void this.dash.pollHost(m.host as string);
        else this.dash.refreshAll();
        break;
      case "showRemoved":
        void this.actions.showRemoved(text(m.host) ?? null);
        break;
      case "hideHost":
        if (text(m.host)) void this.actions.hideHost(m.host);
        break;
      case "pickHosts":
        void this.actions.pickHosts();
        break;
      case "setupUsageBars":
        void vscode.commands.executeCommand("talpyDen.setupUsageBars");
        break;
      case "week": {
        const asked = weekAsked(m.back, m.account, m.span);
        void this.dash.week(asked.back, asked.account, asked.span).then(({ recap, error }) =>
          webview.postMessage({ type: "week", ...asked, recap: recap ?? null, error: error ?? "" } satisfies HostMessage),
        );
        break;
      }
      case "weekRule": {
        const ask = weekRuleAsked(m);
        const host = permsHost(m.host);
        if (!ask || host === null) break;
        const id = ask.allow ? "" : ask.id;
        void this.dash.weekRule(ask, host).then(({ ok, message }) =>
          webview.postMessage({ type: "weekRule", id, host, ok, message } satisfies HostMessage),
        );
        break;
      }
      case "projects":
        void this.dash.projects().then(({ data, error }) =>
          webview.postMessage({ type: "projects", data: data ?? null, error: error ?? "" } satisfies HostMessage),
        );
        break;
      case "newChatIn": {
        // (a folder the projects list named, on an account of this computer's)
        const state = this.dash.state();
        const group = state.status?.groups.find((g) => (g.profileKeys ?? []).includes(m.profileDir)) ?? state.status?.groups[0];
        if (group && typeof m.path === "string" && m.path.startsWith("/"))
          void this.actions.newChat(group.id, undefined, undefined, undefined, { path: m.path, short: m.path.replace(/^\/home\/[^/]+/, "~") });
        break;
      }
      case "perms":
        void this.dash.perms().then(({ data, error }) =>
          webview.postMessage({ type: "perms", data: data ?? null, error: error ?? "" } satisfies HostMessage),
        );
        break;
      case "weekRevoke": {
        const id = weekRevokeAsked(m.id);
        const host = permsHost(m.host);
        if (!id || host === null) break;
        void this.dash.weekRevoke(id, host).then(({ ok, message }) =>
          webview.postMessage({ type: "weekRevoke", id, host, ok, message } satisfies HostMessage),
        );
        break;
      }
      case "shareCard":
        if ((m.how === "save" || m.how === "copy") && typeof m.png === "string" && m.png.length < 8_000_000
            && typeof m.name === "string" && /^claude-(day|week|all)-[\w-]{1,20}\.png$/.test(m.name))
          void this.actions.shareCard(m.how, m.name, m.png);
        break;
      case "openWeekChat":
        if (typeof m.chatId === "string" && CHAT_ID.test(m.chatId) && typeof m.profileDir === "string" && PROFILE_DIR.test(m.profileDir))
          void this.actions.openWeekChat(m.chatId, m.profileDir, text(m.name) ?? m.chatId.slice(0, 8),
                                         typeof m.cwd === "string" && m.cwd.startsWith("/") ? m.cwd : "");
        break;
      case "copy":
        if (typeof m.text === "string" && TIP_COMMAND.test(m.text)) void vscode.env.clipboard.writeText(m.text);
        break;
      case "openUsagePage":
        void vscode.commands.executeCommand("talpyDen.openUsagePage");
        break;
    }
  }

  private html(webview: vscode.Webview, editorTab: boolean): string {
    const nonce = crypto.randomBytes(16).toString("base64");
    const asset = (...p: string[]) => webview.asWebviewUri(vscode.Uri.joinPath(this.extensionUri, ...p)).toString();
    const config = JSON.stringify({ editorTab });
    // Scripts run only with the nonce, and nothing loads from outside the
    // extension. Styles may also be set inline (the pets move by element
    // styles).
    const csp = [
      "default-src 'none'",
      "base-uri 'none'",
      "form-action 'none'",
      `img-src ${webview.cspSource} data:`,
      `style-src ${webview.cspSource} 'unsafe-inline'`,
      `font-src ${webview.cspSource}`,
      `script-src 'nonce-${nonce}'`,
    ].join("; ");
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${asset("dist", "webview-style.css")}">
<title>Talpy Den</title>
</head>
<body>
<script nonce="${nonce}">window.TALPY_DEN = ${config.replace(/</g, "\\u003c")};</script>
<script nonce="${nonce}" src="${asset("dist", "webview.js")}"></script>
</body>
</html>`;
  }
}
