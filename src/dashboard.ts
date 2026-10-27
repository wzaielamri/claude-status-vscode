// What the view and the status bar show: this computer's status and the SSH
// hosts', polled with the widget's backend. This computer is checked the
// moment a chat changes (LiveWatch: its session file), and in between every
// pollInterval while the view is open and a chat works (every 10 s while
// it's hidden: the status bar and notifications still need it); with live
// mode, whose hook hands on each step as it happens, every 6 s at most
// often; while no chat works, every idlePollInterval (20 s). Without the
// watching: every pollInterval (10 s hidden). Hosts every remotePollSeconds.

import * as vscode from "vscode";
import { AwayWatch } from "./away";
import type { Backend } from "./backend";
import { claudePanelAvailable, localPanelProfileKey } from "./claudePanel";
import { cacheDir, extraConfigDirs, settings, statusArgs, configDirArgs, viewSettings } from "./config";
import { LiveWatch } from "./live";
import type { Notifier } from "./notify";
import { currentRemoteHost } from "./remoteHost";
import { demoPerms, demoProjects, demoState, demoWeek } from "./shared/demo";
import { allGroups, chatsNeedingYou, keptChat, limitReset, liveFromRows, queuedWhat, shareUsage, toLocalClock, trackKey, withLive, type ChatSpot } from "./shared/model";
import { chatRef } from "./notify";
import type { ClaudeCodeInfo, DigestCard, HostState, LiveView, PermsData, PetEventKind, ProjectsData, QueuedTask, Status, ViewState, WeekRecap, WeekSpan } from "./shared/types";
import type { WeekRuleAsk } from "./shared/week";
import { mergeChanges, TransitionTracker, type Changes, type PetEvent } from "./transitions";

const HIDDEN_POLL_MS = 10000;
const LIVE_WORKING_MS = 6000; // watched, a chat works, live mode (or the window isn't focused)
const VIEW_ID = "talpyDen.view";
const DIGEST_SHOWN_MS = 60 * 60000; // the away card stays this long, unless closed
const DEMO_STEP_MS = 8000;
const MESSAGE_MS = { error: 10000, info: 12000 };

export class Dashboard implements vscode.Disposable {
  private status: Status | null = null;
  private errorText = "";
  private hosts = new Map<string, HostState>();
  private actionError = "";
  private actionInfo = "";
  private messageTimers: { error?: NodeJS.Timeout; info?: NodeJS.Timeout } = {};
  private visible = false;
  private localBusy = false;
  private hostBusy = new Set<string>();
  private localTimer?: NodeJS.Timeout;
  private hostTimer?: NodeJS.Timeout;
  private retryTimer?: NodeJS.Timeout; // Python didn't work: try again (its own timer: polls mustn't cancel it)
  private demoTimer?: NodeJS.Timeout;
  private demoPhase: number | null = null;
  private tracker = new TransitionTracker();
  private disposables: vscode.Disposable[] = [];
  private disposed = false;
  private windowHost: string | undefined;
  private claudeCodeAvailable = false;
  private claudeCodeCheckedAt = 0;
  // Chats that finished a turn while you weren't looking (trackKey -> when)
  private unseen = new Map<string, number>();
  private away: AwayWatch;
  private digest: DigestCard | null = null;
  private readonly onStateEmitter = new vscode.EventEmitter<ViewState>();
  private readonly onPetEventEmitter = new vscode.EventEmitter<PetEvent>();
  private readonly onLiveEmitter = new vscode.EventEmitter<{ live: Record<string, LiveView>; changed: string[] }>();
  readonly onState = this.onStateEmitter.event;
  readonly onPetEvent = this.onPetEventEmitter.event;
  // What this computer's running chats do now, between checks (live mode);
  // changed: the session ids whose view is new
  readonly onLive = this.onLiveEmitter.event;
  private live: LiveWatch;
  private liveViews: Record<string, LiveView> = {};
  private again = false; // a chat changed while a check ran: check again once it's done

  constructor(
    private backend: Backend,
    private notifier: Notifier,
    private log: vscode.LogOutputChannel,
  ) {
    this.windowHost = currentRemoteHost(vscode);
    this.away = new AwayWatch((now, lastUse) => this.cameBack(now, lastUse));
    this.live = new LiveWatch(
      ({ urgent, views }) => {
        if (views.length) this.noteLive(views);
        // (a window nobody looks at that doesn't notify: its next check is soon enough)
        if (urgent && (this.visible || vscode.window.state.focused || this.notifier.isLeader()))
          void this.refresh(true).then(() => this.scheduleLocal()); // (the next check counts from this one)
      },
      () => extraConfigDirs(),
      cacheDir(),
    );
    this.disposables.push(
      this.away,
      this.live,
      this.onLiveEmitter,
      // looking at the view: what finished is seen
      vscode.window.onDidChangeWindowState((s) => {
        if (s.focused && this.visible) this.unseen.clear();
      }),
      this.onStateEmitter,
      this.onPetEventEmitter,
      vscode.workspace.onDidChangeConfiguration((e) => {
        if (!e.affectsConfiguration("talpyDen")) return;
        if (e.affectsConfiguration("talpyDen.recapHistory")) void this.setRecapHistory(settings.recapHistory());
        this.syncHosts();
        this.scheduleLocal(0);
        this.scheduleHosts(0);
      }),
      // the Claude Code extension installed, removed or (in a new folder) enabled
      vscode.extensions.onDidChange(() => void this.checkClaudeCode()),
      vscode.workspace.onDidChangeWorkspaceFolders(() => {
        void this.checkClaudeCode();
        const host = currentRemoteHost(vscode);
        if (host !== this.windowHost) {
          this.windowHost = host;
          this.syncHosts();
          this.scheduleHosts(0);
        }
      }),
    );
  }

  // The host this window is connected to (Remote-SSH), if any
  get currentHost(): string | undefined {
    return this.windowHost;
  }

  get demo(): boolean {
    return this.demoPhase !== null;
  }

  async start(): Promise<void> {
    if (this.disposed) return;
    void this.checkClaudeCode();
    const problem = await this.backend.checkPython();
    if (this.disposed) return;
    if (problem) {
      this.errorText = problem;
      this.log.error(problem);
      this.emit();
      // try again later: the setting may get fixed
      clearTimeout(this.retryTimer);
      this.retryTimer = setTimeout(() => void this.start(), 30000);
      return;
    }
    this.syncHosts();
    this.live.start();
    this.scheduleLocal(0);
    this.scheduleHosts(0);
  }

  // Whether chats are followed as they change (their session files): the checks in between are rarer then
  get watching(): boolean {
    return this.live.healthy && !this.demo;
  }

  // With live mode's hook on, each step comes as it happens
  get liveHook(): boolean {
    return this.watching && !!this.status?.liveHook;
  }

  private noteLive(views: LiveView[]): void {
    const next = withLive(this.liveViews, views);
    if (!next || this.demo || this.disposed) return;
    this.liveViews = next;
    this.onLiveEmitter.fire({ live: next, changed: views.map((v) => v.sessionId ?? "") });
  }

  // How long until the next check of this computer (see the top)
  private paceMs(): number {
    const poll = settings.pollInterval();
    if (!this.watching) return this.visible ? poll : Math.max(HIDDEN_POLL_MS, poll);
    // (with live mode, a working chat's steps come as they happen: pollInterval doesn't count then)
    if (!this.status || !this.status.counts.working) return this.liveHook ? settings.idlePollInterval() : Math.max(poll, settings.idlePollInterval());
    if (this.liveHook) return this.visible ? LIVE_WORKING_MS : HIDDEN_POLL_MS;
    if (!this.visible) return Math.max(HIDDEN_POLL_MS, poll);
    if (!vscode.window.state.focused) return Math.max(poll, LIVE_WORKING_MS);
    return poll;
  }

  dispose(): void {
    this.disposed = true;
    clearTimeout(this.localTimer);
    clearTimeout(this.hostTimer);
    clearTimeout(this.retryTimer);
    clearInterval(this.demoTimer);
    clearTimeout(this.messageTimers.error);
    clearTimeout(this.messageTimers.info);
    for (const d of this.disposables) d.dispose();
  }

  // Whether the Claude Code extension can open chats in this window (its
  // commands are registered wherever it runs)
  private async checkClaudeCode(): Promise<void> {
    this.claudeCodeCheckedAt = Date.now();
    const available = await claudePanelAvailable();
    if (available === this.claudeCodeAvailable || this.disposed) return;
    this.claudeCodeAvailable = available;
    this.emit();
  }

  // The Claude Code extension here, and the account it runs this computer's chats on
  claudeCodeInfo(): ClaudeCodeInfo {
    return { available: this.claudeCodeAvailable, profileKey: this.claudeCodeAvailable ? localPanelProfileKey() : null };
  }

  setVisible(visible: boolean): void {
    if (visible === this.visible) return;
    this.visible = visible;
    // Just opened: show fresh numbers now, then poll at the faster pace
    if (visible) this.scheduleLocal(0);
    if (visible && vscode.window.state.focused) this.unseen.clear();
  }

  isVisible(): boolean {
    return this.visible;
  }

  // The hosts to check: the window's own host first, then the picked ones
  shownHosts(): string[] {
    const list = settings.sshHosts();
    if (this.windowHost && settings.includeCurrentRemote() && !list.includes(this.windowHost)) list.unshift(this.windowHost);
    return list;
  }

  private syncHosts(): void {
    const shown = this.shownHosts();
    for (const name of [...this.hosts.keys()]) {
      if (!shown.includes(name)) {
        this.hosts.delete(name);
        this.tracker.forget("ssh:" + name); // a host that comes back starts over
      }
    }
    for (const name of shown) {
      const h = this.hosts.get(name);
      if (h) h.current = name === this.windowHost;
      else
        this.hosts.set(name, { name, loading: false, ok: false, error: "", result: null, updatedAt: 0, current: name === this.windowHost });
    }
    this.emit();
  }

  private scheduleLocal(delay?: number): void {
    if (this.disposed) return;
    clearTimeout(this.localTimer);
    const wait = delay ?? this.paceMs();
    this.localTimer = setTimeout(() => void this.refresh().finally(() => this.scheduleLocal()), wait);
  }

  private scheduleHosts(delay?: number): void {
    if (this.disposed) return;
    clearTimeout(this.hostTimer);
    const wait = delay ?? settings.remotePollSeconds() * 1000;
    this.hostTimer = setTimeout(() => {
      for (const host of this.shownHosts()) void this.pollHost(host);
      this.scheduleHosts();
    }, wait);
  }

  // This computer's check only: the usage cap, and (in the window that
  // notifies) looking after the chats that keep going
  private localArgs(): string[] {
    const cap = settings.usageCap();
    return [...(cap > 0 ? ["--usage-cap", String(Math.round(cap))] : []), ...(this.notifier.isLeader() ? ["--keep-going"] : [])];
  }

  // Check this computer now (a check still running isn't doubled; changed:
  // a chat changed, so one that's running then runs again once it's done)
  async refresh(changed = false): Promise<void> {
    if (this.localBusy && changed) this.again = true;
    if (this.localBusy || this.demo || this.disposed) return;
    // Claude Code starts alongside this extension and can register its commands
    // after the first check, with no event for it: look again until it's there
    if (!this.claudeCodeAvailable && Date.now() - this.claudeCodeCheckedAt >= HIDDEN_POLL_MS) void this.checkClaudeCode();
    this.localBusy = true;
    try {
      const { status, error } = await this.backend.status([...configDirArgs(), ...statusArgs(), ...this.localArgs()]);
      if (status) {
        this.errorText = status.errors.length ? status.errors.join("\n") : "";
        const opts = { ...this.notifyOptions(), kept: (chat: string) => !!keptChat(status, chat) };
        const changes = mergeChanges(
          mergeChanges(
            this.tracker.track("", status.groups, status.generatedAt, undefined, opts),
            this.tracker.limitResets("", status.groups, undefined, opts),
          ),
          this.tracker.overlaps("", status.groups, undefined, opts),
        );
        const resets = this.tracker.resets("", status.groups);
        this.status = status;
        this.liveViews = liveFromRows(status.groups, this.liveViews);
        this.react(changes, resets);
        this.startDueTasks(status);
      } else {
        this.errorText = error ?? "Status unavailable";
        this.log.warn(this.errorText);
      }
    } finally {
      this.localBusy = false;
    }
    this.emit();
    if (this.again) {
      this.again = false;
      void this.refresh();
    }
  }

  refreshAll(): void {
    void this.refresh();
    for (const host of this.shownHosts()) void this.pollHost(host);
  }

  async pollHost(host: string): Promise<void> {
    if (this.hostBusy.has(host) || this.demo || this.disposed) return;
    const h = this.hosts.get(host);
    if (!h) return;
    this.hostBusy.add(host);
    h.loading = true;
    this.emit();
    try {
      const answer = await this.backend.hostStatus(host, statusArgs());
      const now = this.hosts.get(host);
      if (!now) return; // unpicked while it was being checked
      if (answer.ok && answer.result) {
        // on this computer's clock: the host's may be off
        const result = toLocalClock(answer.result, Date.now() - (answer.result.generatedAt || Date.now()));
        const opts = this.notifyOptions();
        const changes = mergeChanges(
          mergeChanges(
            this.tracker.track("ssh:" + host, result.groups, result.generatedAt, host, opts),
            this.tracker.limitResets("ssh:" + host, result.groups, host, opts),
          ),
          this.tracker.overlaps("ssh:" + host, result.groups, host, opts),
        );
        const resets = this.tracker.resets("ssh:" + host, result.groups);
        Object.assign(now, { loading: false, ok: true, error: "", result, updatedAt: Date.now(), login: false, loggedIn: !!answer.loggedIn });
        this.react(changes, resets);
      } else {
        // keep the last answer to show (dimmed) with the error
        Object.assign(now, { loading: false, ok: false, error: answer.error || "no answer", login: !!answer.login, loggedIn: false });
        this.log.warn(`${host}: ${now.error}`);
      }
    } finally {
      this.hostBusy.delete(host);
      const h2 = this.hosts.get(host);
      if (h2) h2.loading = false;
      this.emit();
    }
  }

  // Queued tasks whose usage limit has reset start now: from the window
  // that notifies (of several windows, and the Plasma widget, only one
  // starts each: jump.py --start-queued takes it out of the queue first)
  // (a task whose account is at its usage limit right now waits on until
  // that resets: jump.py --requeue)
  private startDueTasks(status: Status): void {
    if (this.demo || !this.notifier.isLeader()) return;
    const groups = allGroups(status, this.state().hosts);
    for (const t of status.queue ?? []) {
      if (t.afterTurn) {
        // a reply for when the chat's turn ends: now it has (or the chat isn't listed)
        const known = !t.host || !!this.hosts.get(t.host)?.result;
        const row = groups
          .filter((g) => (g.host ?? "") === (t.host || ""))
          .flatMap((g) => g.sessions)
          .find((s) => s.chatId === t.chat || s.sessionId === t.chat);
        if (known && !t.error && !this.starting.has(t.id) && (!row || (row.state !== "working" && row.state !== "waiting")))
          void this.startQueued(t);
        continue;
      }
      if (!t.due || this.starting.has(t.id)) continue;
      const group = groups.find((g) => (g.host ?? "") === (t.host || "") && (g.profileKeys ?? []).includes(t.profile));
      const reset = group ? limitReset(group, Date.now()) : null;
      if (!reset) {
        void this.startQueued(t);
        continue;
      }
      this.starting.add(t.id);
      void this.backend
        .action("jump.py", ["--requeue", t.id, "--at", String(Math.round(reset.at + 60_000)), ...configDirArgs()])
        .finally(() => {
          this.starting.delete(t.id);
          void this.refresh();
        });
    }
  }

  private starting = new Set<string>();

  async startQueued(t: QueuedTask): Promise<void> {
    if (this.starting.has(t.id)) return;
    this.starting.add(t.id);
    this.emit();
    try {
      const answer = await this.backend.action("jump.py", ["--start-queued", t.id, ...configDirArgs()], 120000);
      const what = queuedWhat(t).split("\n")[0];
      if (t.afterTurn) {
        // your reply, sent as the chat's turn ended
        if (answer.ok && answer.message && settings.notifyDone() && this.notifier.isLeader() && !this.visible)
          void vscode.window.showInformationMessage(`Sent to ${t.name || "the chat"}: ${t.prompt} — ${answer.message}`);
        else if (!answer.ok) this.setActionError(`Couldn't send your reply to ${t.name || "the chat"}: ${answer.message}`);
      } else if (answer.ok && answer.message) {
        // ("": another window, or the widget, started it)
        this.petEvent("reset", `Started: ${what}`);
        if (settings.notifyDone() && this.notifier.isLeader())
          void vscode.window.showInformationMessage(`Started on ${t.account}: ${what} — ${answer.message}${t.host ? ` · ${t.host}` : ""}`);
      } else if (!answer.ok) {
        this.setActionError(`Couldn't start the queued task: ${answer.message}`);
        if (!this.visible) void vscode.window.showErrorMessage(`Talpy Den: couldn't start the queued task “${what}”: ${answer.message}`);
      }
    } finally {
      this.starting.delete(t.id);
      void this.refresh();
      if (t.host) void this.pollHost(t.host);
    }
  }

  isStarting(id: string): boolean {
    return this.starting.has(id);
  }

  // After Log in… (a host that wants a password): check it every few seconds
  // until it answers, for up to three minutes
  watchLogin(host: string): void {
    const until = Date.now() + 180000;
    const tick = () => {
      const h = this.hosts.get(host);
      if (this.disposed || !h || h.ok || Date.now() > until) return;
      void this.pollHost(host);
      setTimeout(tick, 3000);
    };
    setTimeout(tick, 3000);
  }

  private notifyOptions() {
    return {
      notifyWaiting: settings.notifyWaiting(),
      notifyDone: settings.notifyDone(),
      notifyDoneMinSeconds: settings.notifyDoneMinSeconds(),
    };
  }

  // Pets and notifications, once the new status is stored: a reaction that
  // fails can't hold the dashboard back (the widget froze that way once, at
  // every plan-limit reset).
  private react(changes: Changes, resets: PetEvent[]): void {
    if (this.disposed) return; // a poll that finished after the window closed: no toasts or sounds
    try {
      this.petEvents([...changes.petEvents, ...resets]);
      const now = Date.now();
      this.away.tracker.note(changes, resets, now);
      this.noteUnseen(changes);
      const holdDone = settings.awayDigest() && this.away.holding(now, settings.awayMinutes() * 60000);
      if (!this.demo) this.notifier.handle(changes, { holdDone });
    } catch (e) {
      this.log.error(`A reaction to the new status failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  // A turn that finished while you weren't looking at the view (and that was
  // long enough to notify about) waits for you: the shortcut to the chat that
  // needs you goes there too
  private noteUnseen(changes: Changes): void {
    for (const f of changes.finished) if (f.stop) this.unseen.delete(f.id); // (a limit broke it off: nothing to look at)
    if (this.visible && vscode.window.state.focused) return;
    const min = settings.notifyDoneMinSeconds() * 1000;
    for (const f of changes.finished) if (!f.stop && f.took >= min) this.unseen.set(f.id, f.at);
  }

  // The chats that need you, most urgent first (model.chatsNeedingYou)
  needingYou(): ChatSpot[] {
    const st = this.state();
    const groups = allGroups(st.status, st.hosts);
    // a finished chat that works again, or is gone, no longer waits for you
    const states = new Map<string, string>();
    for (const g of groups) for (const s of g.sessions) states.set(trackKey(s, g.host), s.state);
    for (const id of [...this.unseen.keys()]) {
      const state = states.get(id);
      if (state !== "idle" && state !== "done") this.unseen.delete(id);
    }
    return chatsNeedingYou(groups, this.unseen);
  }

  seen(id: string): void {
    this.unseen.delete(id);
  }

  // Back at VS Code after a while: what happened, in one notification
  private cameBack(now: number, lastUse: number): void {
    const st = this.state();
    const digest = this.away.tracker.back(now, lastUse, allGroups(st.status, st.hosts), settings.awayMinutes() * 60000);
    if (!digest || this.demo || !settings.awayDigest()) return;
    // a card in the view; a notification too, unless notifications are off
    this.digest = { title: digest.title, lines: digest.lines, at: now, first: digest.first ? chatRef(digest.first.session, digest.first.host) : null };
    if (settings.notifyWaiting() || settings.notifyDone())
      this.notifier.digest(digest, () => void vscode.commands.executeCommand(`${VIEW_ID}.focus`));
    this.emit();
  }

  dismissDigest(): void {
    this.digest = null;
    this.emit();
  }

  private petEvents(events: PetEvent[]): void {
    for (const e of events) this.onPetEventEmitter.fire(e);
  }

  petEvent(kind: PetEventKind, text: string, key = ""): void {
    this.onPetEventEmitter.fire({ kind, text, key });
  }

  setActionError(message: string): void {
    this.actionError = message;
    clearTimeout(this.messageTimers.error);
    this.messageTimers.error = setTimeout(() => {
      this.actionError = "";
      this.emit();
    }, MESSAGE_MS.error);
    if (message) this.petEvent("oops", "Hmm, that didn't work.");
    this.emit();
  }

  setActionInfo(message: string): void {
    this.actionInfo = message;
    clearTimeout(this.messageTimers.info);
    this.messageTimers.info = setTimeout(() => {
      this.actionInfo = "";
      this.emit();
    }, MESSAGE_MS.info);
    this.emit();
  }

  // Demo mode: made-up chats whose story moves on every few seconds, for
  // screenshots and for trying the view without Claude Code.
  toggleDemo(): boolean {
    if (this.demo) {
      clearInterval(this.demoTimer);
      this.demoPhase = null;
      this.tracker.forget("demo");
      this.scheduleLocal(0);
      this.scheduleHosts(0);
    } else {
      this.demoPhase = 0;
      const step = () => {
        const state = demoState(Date.now(), this.demoPhase ?? 0);
        const groups = [...(state.status?.groups ?? []), ...state.hosts.flatMap((h) => h.result?.groups ?? [])];
        this.petEvents(this.tracker.track("demo", groups, Date.now(), undefined, this.notifyOptions()).petEvents);
      };
      step();
      this.demoTimer = setInterval(() => {
        this.demoPhase = (this.demoPhase ?? 0) + 1;
        step();
        this.emit();
      }, DEMO_STEP_MS);
    }
    this.emit();
    return this.demo;
  }

  // The Recap tab's recap of these days, with the SSH hosts shown whose last
  // check answered (one out of reach would hold it up)
  async week(back: number, account: string, span: WeekSpan = "week"): Promise<{ recap?: WeekRecap; error?: string }> {
    if (this.demoPhase !== null) return { recap: demoWeek(Date.now(), back, account, span) };
    const hosts = this.shownHosts().filter((name) => {
      const h = this.hosts.get(name);
      return !!h && h.ok && !!h.result;
    });
    const currency = settings.recapCurrency();
    return this.backend.week([
      "--span", span,
      "--back", String(back),
      ...(account ? ["--account", account] : []),
      ...(currency !== "USD" ? ["--currency", currency] : []),
      ...hosts.flatMap((h) => ["--host", h]),
      ...configDirArgs(),
    ]);
  }

  // A rule the Recap tab offers for permission prompts you answered: into
  // the chats' folder's .claude/settings.local.json, or taken back
  async weekRule(ask: WeekRuleAsk, host = ""): Promise<{ ok: boolean; message: string }> {
    if (this.demoPhase !== null) return { ok: false, message: "Not in the demo: nothing was changed" };
    if (host && !this.shownHosts().includes(host)) return { ok: false, message: `${host} isn't one of the SSH hosts shown` };
    return this.backend.action("week.py", [...(ask.allow ? ["--allow", ask.kind, ask.value, ask.project] : ["--undo", ask.id]),
      ...(host ? ["--on", host] : [])]);
  }

  // The Recap's history: kept past Claude's own cleanup or not (one choice
  // for every copy of the widget's week.py: a file next to their cache), or
  // deleted (it starts again from the transcripts Claude still has)
  async setRecapHistory(keep: boolean): Promise<void> {
    if (this.demoPhase !== null) return;
    const { ok, message } = await this.backend.action("week.py", ["--history", keep ? "on" : "off"]);
    if (ok) void vscode.window.showInformationMessage(`Talpy Den: ${message}.`);
    else void vscode.window.showWarningMessage(`Talpy Den: the Recap's history didn't change: ${message}`);
  }

  async forgetRecapHistory(): Promise<void> {
    const pick = await vscode.window.showWarningMessage("Delete the Recap's saved history?", {
      modal: true,
      detail: "It starts again from the chats Claude still has (Claude Code keeps them 30 days by default).",
    }, "Delete");
    if (pick !== "Delete" || this.demoPhase !== null) return;
    const { ok, message } = await this.backend.action("week.py", ["--forget"]);
    if (ok) void vscode.window.showInformationMessage(`Talpy Den: ${message}.`);
    else void vscode.window.showWarningMessage(`Talpy Den: nothing was deleted: ${message}`);
  }

  // The Permissions tab: the permissions you gave and the prompts of the
  // last 7 days (this computer's)
  async projects(): Promise<{ data?: ProjectsData; error?: string }> {
    if (this.demoPhase !== null) return { data: demoProjects(Date.now()) };
    const currency = vscode.workspace.getConfiguration("talpyDen").get<string>("recapCurrency") || "USD";
    return this.backend.projects([...(currency !== "USD" ? ["--currency", currency] : []), ...configDirArgs()]);
  }

  async perms(): Promise<{ data?: PermsData; error?: string }> {
    if (this.demoPhase !== null) return { data: demoPerms(Date.now()) };
    const hosts = this.shownHosts().filter((name) => {
      const h = this.hosts.get(name);
      return !!h && h.ok && !!h.result; // (one out of reach would hold it up)
    });
    return this.backend.perms([...hosts.flatMap((h) => ["--host", h]), ...configDirArgs()]);
  }

  // Takes one of the permissions you gave (what lets chats go without
  // asking) out of its file
  async weekRevoke(id: string, host = ""): Promise<{ ok: boolean; message: string }> {
    if (this.demoPhase !== null) return { ok: false, message: "Not in the demo: nothing was changed" };
    if (host && !this.shownHosts().includes(host)) return { ok: false, message: `${host} isn't one of the SSH hosts shown` };
    return this.backend.action("week.py", ["--revoke", id, ...(host ? ["--on", host] : configDirArgs())]);
  }

  state(): ViewState {
    const now = Date.now();
    if (this.demoPhase !== null) {
      const demo = demoState(now, this.demoPhase);
      return { ...demo, ...shareUsage(demo.status, demo.hosts, settings.sshUsage()), settings: viewSettings(), actionError: this.actionError, actionInfo: this.actionInfo };
    }
    // (an account's plan usage once: shareUsage; the notifications see each machine's own)
    const hosts = this.shownHosts().map((name) => this.hosts.get(name)).filter((h): h is HostState => !!h);
    return {
      ...shareUsage(this.status, hosts, settings.sshUsage()),
      errorText: this.errorText,
      settings: viewSettings(),
      demo: false,
      platformNote:
        process.platform === "linux"
          ? ""
          : process.platform === "win32"
            ? "Talpy Den needs Linux or macOS: its status script doesn't run on Windows, so nothing shows here."
            : "Running chats on this computer are only detected on Linux for now; SSH hosts still work.",
      claudeCode: this.claudeCodeInfo(),
      actionError: this.actionError,
      actionInfo: this.actionInfo,
      digest: this.digest && now - this.digest.at < DIGEST_SHOWN_MS ? this.digest : null,
      now,
      live: this.liveViews,
    };
  }

  emit(): void {
    if (!this.disposed) this.onStateEmitter.fire(this.state());
  }
}
