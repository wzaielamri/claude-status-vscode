// What vendor/code/talpy_den.py prints (its collect(), plus the --remote
// additions), and the messages between the extension and its webview.

export type SessionState = "waiting" | "working" | "idle" | "done";

export interface Agent {
  id: string;
  parent: string | null;
  depth: number;
  type: string;
  description: string;
  background?: boolean;
  status: string; // running | done | stopped | failed
  activity: string;
  started: number | null;
  updated: number | null;
  model?: string | null; // e.g. "Haiku 4.5", from its replies
  task?: string; // what it was asked to do (plain text, may have newlines)
  steps?: string[]; // its last tool calls, oldest first, e.g. "Bash: npm test"
  report?: string; // what it handed back (plain text); "" until it's done
  toolCalls?: number | null; // tool calls so far
  tokens?: number | null; // tokens so far
}

export interface SharedInfo {
  profiles: string[];
  openIn: string | null;
  stoppedCopies?: string[];
  forkOf?: string;
  movedHere?: boolean;
}

// What a chat is doing, from the end of its transcript
export interface Peek {
  activity: string; // its newest step, e.g. "Bash: npm test"; "" once its reply is finished
  prompt: string; // your last prompt
  reply: string; // Claude's last reply (up to 1500 characters, several lines)
  replyLine: string; // its first line
  at: number | null;
  // Claude's recap of the chat, while nothing was said after it (Claude Code
  // writes one when you come back to a chat after a while). Older backends leave it out.
  recap?: { text: string; at: number | null } | null;
  mode?: string | null; // its permission mode: default, plan, auto, acceptEdits, bypassPermissions, dontAsk
  slug?: string | null; // its plan's name, <config>/plans/<slug>.md
  stopped?: Stop | null; // (as read; the row's own `stopped` says when the limit resets)
}

// A step of a chat, as the live hook noted it (the widget's live_hook.py)
export interface LiveStep {
  id: string | null; // the tool call's id
  tool: string;
  text: string; // e.g. "Edit: parser.py", "Bash: pytest"
  at: number;
  end: number | null; // when it ended (null: it runs)
  ok: boolean | null;
  add?: number; // the lines an edit added and removed
  del?: number;
  agent?: string; // a subagent's step: its type
  error?: string;
}

// What a running chat does now, step by step (live mode: the live hook's
// ~/.cache/talpy-den/live/<session id>.json; a row carries it at each
// check, and the extension follows the files in between)
export interface LiveView {
  sessionId?: string; // (the files have them; a row's view doesn't)
  pid?: number | null;
  state: "working" | "waiting" | "idle" | "ended" | string;
  doing: string; // its step now ("" between turns)
  asks?: "permission" | "question" | "plan" | "input" | null;
  at: number; // its newest event
  turnAt?: number | null;
  steps: LiveStep[];
  agents: { id: string; type: string; at: number; doing: string }[];
  said?: string; // the first line of what Claude said last
  saidAt?: number | null;
  failed?: string; // the API error that ended the turn
  event?: string;
}

// What stopped a chat: a usage limit (window: the plan window it hit, e.g.
// "five_hour", when Claude named it) or an expired login
export interface Stop {
  kind: "limit" | "login";
  window: string | null;
  text: string; // Claude's message, e.g. "You've hit your session limit · resets 7:10pm (Europe/Berlin)"
  at: number | null;
  resetsAt?: number | null; // when that limit resets
  over?: boolean; // it has reset since: the chat can go on
}

// Another chat with a process that changed some of the same files as this one
export interface Overlap {
  key: string; // its row
  name: string;
  sessionId: string;
  profileDir: string;
  count: number;
  files: string[]; // relative to this chat's folder, at most 8
}

// A background task waiting for its account's usage limit to reset
// (talpy_den.py: queued_tasks); an SSH host's starts from here, over ssh
export interface QueuedTask {
  id: string;
  host: string; // "" for this computer
  profile: string;
  account: string;
  folder: string;
  folderShort: string;
  prompt: string;
  plan: string | null;
  planProfile: string | null;
  planTitle: string;
  startAt: number;
  createdAt: number;
  error: string; // why it didn't start (it waits for you then)
  due: boolean;
  chat?: string | null; // goes on with this chat (one a usage limit stopped) instead
  name?: string; // that chat's name
  scheduled?: boolean; // it starts at a time you picked, not when a limit resets
  why?: string; // a chat's go-on: "retry" (its turn broke off) or "cap" (the usage cap stopped it), else ""
  model?: string | null; // a new chat's model and effort (claude --model, --effort)
  effort?: string | null;
  // a reply for when the chat's turn ends (pane.py --send --after-turn):
  // sent once the chat isn't working any more, the way `how` says
  afterTurn?: boolean;
  how?: string;
  session?: string | null;
}

// A chat of this computer that keeps going until it's done: after each usage
// limit that stops it, it goes on by itself (talpy_den.py: look_after_kept)
export interface KeptChat {
  chat: string;
  profile: string;
  name: string;
  since: number;
  wentOnAt: number | null; // when it last went on by itself
  tries: number; // how often it went on again after a go-on that didn't take, or a turn that broke off
  note: string; // why it can't go on right now (open in a Claude Code tab, its login expired)
  error: string; // it gave up: why
}

// The plan a chat made (talpy_den.py: plan_of)
export interface ChatPlan {
  slug: string;
  title: string;
  path: string; // on the machine the chat is on
  updatedAt: number;
  edited?: boolean; // changed since its chat last did anything: by you (talpy_den.py: plan_edited)
}

// One of an account's plans (talpy_den.py: account_plans)
export interface PlanItem extends ChatPlan {
  key: string;
  profileDir: string; // the profile whose plans/ it's in
  profile: string;
  shared: boolean; // linked into another profile's plans/ too
  chatId: string | null; // the chat that made it, where it's still there
  chatProfileDir: string | null;
  cwd: string; // that chat's folder
  cwdShort: string;
  openCommand?: string | null; // --remote: reopens that chat there
  starred?: boolean; // you starred it (older backends leave it out)
}

// One of Claude's memories on an account (talpy_den.py: account_memories):
// a note it keeps in <config>/projects/<folder>/memory
export interface Memory {
  key: string;
  name: string;
  description: string;
  type: string; // user, feedback, project, reference ("" when it doesn't say)
  path: string; // on the machine the account is on
  folder: string; // the project folder it's for ("" when that's gone)
  folderShort: string;
  profileDir: string;
  profile: string;
  updatedAt: number;
}

// The files a chat changed (labels relative to its folder)
export interface ChangeSummary {
  count: number;
  files: string[];
}

export type TaskStatus = "pending" | "in_progress" | "completed";

// A chat's task list (Claude's TaskCreate/TaskUpdate)
export interface TaskProgress {
  done: number;
  total: number;
  current: string; // what it's on now, e.g. "Running the tests"
  items: { subject: string; status: TaskStatus | string }[];
}

export interface Session {
  key: string;
  profileDir: string;
  sessionId: string;
  chatId: string;
  starred?: boolean; // you starred it: once it isn't listed, it stays under Starred
  pid: number | null;
  name: string;
  cwd: string;
  cwdShort: string;
  profile: string;
  kind: string; // interactive | bg | job | chat
  entrypoint: string;
  state: SessionState;
  reason: string;
  waitingFor: string;
  detail: string;
  since: number | null;
  startedAt: number | null;
  context: number | null;
  model: string | null;
  effort?: string | null; // reasoning effort, from the status line (low … max)
  costUsd: number | null;
  agents: { running: number; done: number; stopped: number; failed?: number; total: number; tree: Agent[] } | null;
  shared: SharedInfo | null;
  jobId: string | null;
  jobSessionId: string | null;
  tmux?: boolean; // runs in a tmux pane: a working one can be interrupted (Ctrl+C)
  peek?: Peek | null;
  changes?: ChangeSummary | null;
  tasks?: TaskProgress | null;
  stats?: ChatStats | null;
  plan?: ChatPlan | null; // the plan it made
  stopped?: Stop | null; // a usage limit (or an expired login) stopped it
  // other chats with a process that changed some of the same files
  overlaps?: Overlap[] | null;
  // --remote: shell commands, run on the machine the session is on
  openCommand?: string | null;
  stopCommand?: string | null;
  // --remote: the process of the VS Code terminal it runs in (that terminal's
  // processId, on the machine the session is on), if it does
  vscodeTerminal?: number | null;
  // How it takes a message from the chat tab (talpy_den.py: reply_route);
  // older backends leave these out
  reply?: ReplyRoute;
  review?: Review | null; // it changed files: its last edit, and tests since
  permission?: PermissionAsk | null; // what it asks while it waits for a permission
  live?: LiveView | null; // what it does now, step by step (live mode)
}

// "tmux": typed into its pane; "vscode": its Claude Code tab's input;
// "background": claude --bg --resume (no process, or a background session);
// "terminal": a terminal that takes no text from outside
export type ReplyRoute = "tmux" | "vscode" | "background" | "terminal";

// A chat that changed files: when it last edited one, and the newest test run
// after that (talpy_den.py: TestsCache.review)
export interface Review {
  editedAt: number | null;
  tests: { command: string; ok: boolean; at: number | null } | null;
}

// What a chat waiting for a permission asks (talpy_den.py: note_permissions)
export interface PermissionAsk {
  tool: string;
  line: string; // e.g. "Bash: npm publish"
  command: string; // a Bash command, or ""
  file: string; // the file it would change, or ""
  diff: string[]; // the change, as unified diff lines
  always: string; // what Always allow lets through, or "" (nothing to remember)
  // Claude's multiple-choice questions (its AskUserQuestion tool), answered
  // with "answer"; older backends leave it out
  questions?: Question[];
  // A chat leaving plan mode (ExitPlanMode): its plan, approved with "approve"
  // or "approve-edits", or planned on with "keep" (a note: what to change);
  // older backends leave it out
  plan?: PlanAsk | null;
  request: string | null; // the hook's request, which the extension can answer
  answer: "hook" | "keys" | ""; // how it can be answered from here ("": only in the chat)
}

export interface PlanAsk {
  text: string; // Markdown
  cut: boolean; // longer than shown
  file: string; // its plan file
  title: string;
}

// One of Claude's questions (talpy_den.py: question_view): pick one of
// its options, or several for a multiSelect one, or answer in your own words
export interface Question {
  question: string;
  header: string; // a short label, e.g. "Database"
  multiSelect: boolean;
  options: { label: string; description: string }[];
}

// Answers to Claude's questions: {question: [the labels chosen, or what you typed]}
export type Answers = Record<string, string[]>;

// A chat's numbers (talpy_den.py: stats_summary): your prompts, its tool
// calls by name, tokens (subagents included), how long Claude worked
export interface ChatStats {
  prompts: number;
  toolCalls: number;
  tools: [string, number][]; // most used first, at most 5
  tokens: { input: number; output: number; cacheRead: number; cacheWrite: number; total: number };
  workedMs: number; // each turn from your prompt to its last step
  startedAt: number | null;
  lastAt: number | null;
}

// An account's last days (talpy_den.py: token_stats)
export interface StatsDay {
  day: string; // YYYY-MM-DD, local
  total: number;
  output: number;
  messages: number;
  chats: number;
  prompts: number;
}

export interface UsageStats {
  days: StatsDay[]; // oldest first, today last
  projects: { name: string; path: string; total: number; output: number; prompts: number }[]; // busiest first
  models: { model: string; total: number }[];
  sum: { total: number; output: number; chats: number; prompts: number; activeDays: number };
}

// Where a plan window is heading at your pace
export interface Forecast {
  evenPct: number; // where an even pace through the window would be by now
  rate: number; // percent per hour
  atReset: number; // where it would be when the window resets
  fullAt: number | null; // when it would run out, if that's before it resets
  basis: "hour" | "window"; // the last hour's pace, or the window's so far
}

export interface LimitWindow {
  key: string;
  label: string;
  pct: number;
  resetsAt: number | null;
  reset: boolean;
  forecast?: Forecast | null;
}

export interface Limits {
  updatedAt: number;
  windows: LimitWindow[];
}

export interface TokenTotals {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  total: number;
  messages: number;
  shared: number;
}

export interface Tokens {
  today: TokenTotals;
  week: TokenTotals;
  models: { model: string; total: number }[];
}

export interface Project {
  path: string;
  short: string;
  newChatCommand?: string;
  trusted?: boolean; // Claude trusts the folder: a task can start there in the background
}

export interface RecentChat {
  key: string;
  sessionId: string;
  name: string;
  cwd: string;
  cwdShort: string;
  lastActive: number;
  profile: string;
  profileDir: string;
  shared: boolean;
  openCommand?: string | null;
  recap?: { text: string; at: number | null } | null;
  stopped?: Stop | null;
  starred?: boolean; // you starred it
  changes?: ChangeSummary | null; // the files it changed (older backends leave it out)
  review?: Review | null;
}

// A chat "Find a Chat" found (talpy_den.py: find_chats)
export interface FoundChat {
  key: string;
  sessionId: string;
  name: string;
  cwd: string;
  cwdShort: string;
  lastActive: number;
  profile: string;
  profileDir: string;
  groupId: string;
  account: string; // e.g. "work · Team"
  shared: boolean;
  match: string; // the prompt of yours that has the words, around them ("" when the name or folder did)
  openCommand?: string | null; // an SSH host's: reopens it there
}

// What the search found besides chats (talpy_den.py: find_more)
export interface FoundProject {
  folder: string;
  folderShort: string;
  lastActive: number;
  chats: number;
  profile: string;
  profileDir: string;
  groupId: string;
  account: string;
}

export interface FoundPlan {
  key: string;
  slug: string;
  title: string;
  path: string;
  profile: string;
  profileDir: string;
  groupId: string;
  account: string;
  updatedAt: number;
}

export type FoundMemory = Memory & { groupId: string; account: string };

export interface SharedPointer {
  key: string;
  sessionId: string;
  name: string;
  cwdShort: string;
  state: SessionState;
  reason: string;
  since: number | null;
  profile: string;
  profileDir: string;
  copy: boolean;
  stoppedHere: boolean;
  openCommand?: string | null;
}

export interface Group {
  id: string;
  name: string;
  plan: string | null;
  title: string;
  profiles: string[];
  profileKeys: string[];
  sessions: Session[];
  limits: Limits | null;
  tokens: Tokens | null;
  stats?: UsageStats | null;
  chatProfile: string;
  shareProfile: string | null;
  projects: Project[];
  sharedHere: SharedPointer[];
  recent: RecentChat[];
  starred?: RecentChat[]; // the chats you starred that aren't listed, newest first, however old. Older backends leave it out.
  plans?: PlanItem[]; // newest first. Older backends leave it out.
  starredPlans?: PlanItem[]; // the plans you starred, newest first, however old
  memories?: Memory[]; // newest first. Older backends leave it out.
  homeChatCommand?: string;
  // VS Code's Claude runs on this account (the backend's view of it; an SSH
  // host's: what its VS Code server's settings say, else its ~/.claude)
  vscodeHere?: boolean;
  // plain `claude` runs on it (~/.claude, or the one it links to), and the
  // default account (VS Code's Claude's where VS Code is, else plain claude's;
  // listed first). Older backends leave them out.
  plainHere?: boolean;
  defaultHere?: boolean;
  // On an SSH host: the CLAUDE_CONFIG_DIR VS Code's Claude there needs to run
  // on this account (null: none, a plain ~/.claude). Older backends leave it out.
  configDir?: string | null;
  // Not logged in to an account (a new one, or logged out): loginCommand runs
  // Claude's own login on it (`claude auth login`). Older backends leave them out.
  loggedOut?: boolean;
  loginCommand?: string | null;
  // Set by the extension for an SSH host's accounts
  host?: string;
  // Set by shareUsage: its plan usage shows on another machine's copy of
  // the account (or no host's shows), or comes from that host
  limitsHidden?: boolean;
  limitsFrom?: string;
  // With limitsHidden: the account's numbers all the same (the newest of its
  // machines'), for what weighs them (handoverTarget, limitReset)
  accountLimits?: Limits | null;
  // One of its plan windows is at the usage cap (talpyDen.usageCap): until
  // it resets, queued tasks wait. Older backends leave it out.
  cap?: { pct: number; label: string; key: string; resetsAt: number } | null;
}

export interface Status {
  ok: true;
  generatedAt: number;
  overall: string;
  counts: Counts;
  groups: Group[];
  dismissed: number;
  canShare: boolean;
  errors: string[];
  host?: string;
  claude?: string;
  // Whether the claude command is on this machine (missing when unsure)
  cli?: { installed: boolean };
  queue?: QueuedTask[]; // this computer's status only: tasks waiting for a usage limit
  keep?: KeptChat[]; // this computer's status only: the chats that keep going
  // this computer's status only: chats marked reviewed ("[host|]chat id" ->
  // when; 0: marked not reviewed), and since when chats are to review at all
  reviewed?: Record<string, number>;
  reviewedSince?: number;
  liveHook?: boolean; // live mode is on: rows' "live" follow each step
}

export interface Counts {
  waiting: number;
  working: number;
  idle: number;
  done: number;
  limit: number; // a usage limit stopped them (idle or done): they wait for it, not for you
}

export type Overall = "waiting" | "working" | "idle" | "none";

// One SSH host as the extension sees it
export interface HostState {
  name: string;
  loading: boolean;
  ok: boolean;
  error: string;
  result: Status | null; // the last good answer, kept (dimmed) after an error
  updatedAt: number;
  current: boolean; // the host this VS Code window is connected to
  // It wants a password (or a passphrase, a code, its host key confirmed):
  // Log in… opens a terminal where ssh asks for it (remote.py --login-here)
  login?: boolean;
  loggedIn?: boolean; // the checks go through a connection you logged in with: Disconnect closes it
}

export interface ViewSettings {
  showPet: boolean;
  chatPets: number;
  showTokens: boolean;
  recentChats: number;
  reopenIn: ReopenIn; // where recent chats reopen (the switch above them)
  recapCurrency?: string; // the Recap tab's amounts: "USD", "EUR", …
  recapRules?: boolean; // the Recap tab suggests rules for the permission prompts you answered
  clickOpensPane?: boolean; // a click on a chat opens its tab (else the chat itself)
}

export type ReopenIn = "terminal" | "claudeCode";

// Plan usage on SSH hosts' accounts (shareUsage): once per account, on every host, or on none
export type SshUsage = "once" | "all" | "none";

// The Claude Code extension in this window: whether it's there, and the
// account (profile key) it runs on for this computer's chats
export interface ClaudeCodeInfo {
  available: boolean;
  profileKey: string | null;
}

// What happened while you were away (away.ts), as a card at the top of the view
export interface DigestCard {
  title: string; // "While you were away (1h 12m)"
  lines: string[]; // "Needs you: Deploy (waiting 22m)", "Finished: a, b", …
  at: number;
  first: ChatRef | null; // the chat waiting longest, to open
}

// Everything the webview draws
export interface ViewState {
  status: Status | null;
  errorText: string; // why this computer's status is missing or partial
  hosts: HostState[];
  settings: ViewSettings;
  demo: boolean;
  platformNote: string; // e.g. local sessions need Linux
  claudeCode: ClaudeCodeInfo;
  actionError: string;
  actionInfo: string;
  digest?: DigestCard | null;
  now: number;
  live?: Record<string, LiveView>; // this computer's running chats by session id (live mode), newest
}

export type PetEventKind = "waiting" | "done" | "failed" | "stopped" | "limit" | "reset" | "oops";

// The Recap tab: your last 7 days with Claude (the widget's week.py, read
// from the chats' transcripts when the tab asks, never in the status checks)
export interface WeekDay {
  to?: string; // (Overall's rows: 7 days, from day to this one)
  day: string; // "2026-10-24"
  start: number; // its local midnight
  end: number; // the next one
  you: [number, number][]; // when you were at it
  work: [number, number, number][]; // when Claude worked: [from, to, chats at once]
  wait: [number, number][]; // when it waited for your answer (and no chat worked)
  limits: number[]; // when a usage limit stopped a chat
  youMs: number;
  workedMs: number;
  busyMs: number;
  waitMs: number;
  prompts: number;
  cost: number;
  tokenTotal: number; // all tokens: input, output, cache reads and writes
  chats: WeekChat[]; // the day's chats, what Claude worked on most first
}

export interface WeekNumbers {
  youMs: number;
  workedMs: number; // Claude's own count of its turns, all chats added up
  busyMs: number; // that on the clock: chats at once count once
  waitMs: number; // Claude waiting for your answers
  prompts: number;
  cost: number; // $ at API prices
  tokenTotal: number; // all tokens: input, output, cache reads and writes
}

export interface WeekSum extends WeekNumbers {
  turns: number;
  limits: number; // times a usage limit stopped you
  chats: number;
  peakAtOnce: number;
  busiestDay: string | null;
  longestTurn: { workedMs: number; startedAt: number; name: string; host: string } | null;
  tokens: { input: number; output: number; cacheRead: number; cacheWrite5m: number; cacheWrite1h: number; webSearches: number };
}

export interface WeekProject {
  name: string;
  path: string;
  host: string;
  workedMs: number;
  cost: number;
  prompts: number;
  chats: WeekChat[]; // its chats Claude worked on most
  chatCount: number; // all of them
}

export interface WeekChat {
  name: string;
  chatId: string;
  host: string;
  profileDir: string;
  folder: string; // short, e.g. "~/repos/app"
  cwd?: string; // where it started
  workedMs: number;
  cost: number;
  prompts: number;
  startedAt?: number; // (a turn: when it started and ended)
  endedAt?: number;
  resumes?: number; // (a chat come back to after its cache expired: how often)
}

export interface WeekModel {
  model: string; // "Opus 5.5"
  cost: number;
  tokens: number;
  requests: number;
  share: number; // of the cost, 0-1
}

// What would use less of your plan: Claude Code's /usage rules (pct: the
// share of the week's usage it touches), how long Claude waited for you, the limits hit
export interface WeekTip {
  key: string;
  pct: number | null;
  title: string;
  detail: string;
  command: string; // e.g. "/compact", or ""
}

// A plan window now, and how it filled up (the widget's usage history)
export interface WeekPlanWindow {
  key: string; // "five_hour", "seven_day", …
  label: string; // "5h", "7d", …
  resetsAt: number | null;
  pct: number;
  points: [number, number][]; // [ms, percent used]
}

// A rule that would have let permission prompts you answered go without
// asking: kind "dir" (permissions.additionalDirectories), "edits"
// (defaultMode acceptEdits), "allow" (a permissions.allow rule); it goes
// into project/.claude/settings.local.json
export interface WeekRule {
  kind: "dir" | "edits" | "allow";
  value: string;
  project: string;
  count: number;
  waitMs: number;
  example: string;
  id: string;
  state: "new" | "allowed" | "added"; // added: by the Recap tab, it can take it back
  title: string;
  change: string; // what allowing it changes, in words
}

// A permission you gave: a rule or mode that lets chats on this computer go
// without asking (week.py's granted list): kind "allow" (a permissions.allow
// rule), "dir" (permissions.additionalDirectories), "mode" (a defaultMode
// that skips prompts), in your user settings or a folder's .claude settings
export interface WeekGrant {
  id: string;
  kind: "allow" | "dir" | "mode" | "flag" | "mcp" | "trust";
  folder?: string; // (what .claude.json keeps per folder: its trust, the tools and servers approved there)
  value: string;
  title: string;
  place: string; // whose chats it lets through
  where: string; // the file, from ~
  shared: boolean; // a folder's settings.json (shared through git) or the administrator's: not changed here
  managed?: boolean; // the administrator's (/etc/claude-code)
  added: boolean; // the Recap tab's Allow put it there
  change: string; // what revoking it does, in words
}

// The Permissions tab (week.py --permissions): the prompts you answered in
// the last 7 days on this computer, with the rules that would have let them
// through, and the permissions you gave
export interface PermsData {
  ok: true;
  generatedAt: number;
  from: string;
  to: string;
  prompts: WeekRecap["prompts"];
  granted: WeekGrant[];
  hosts?: { host: string; ok: boolean; error: string; prompts?: WeekRecap["prompts"]; granted?: WeekGrant[] }[]; // the SSH hosts shown, as listed there
}

// What the Recap tab shows: a day, the last 7 days (or 7 before), or everything kept
export type WeekSpan = "day" | "week" | "all";

// The Projects tab (week.py --projects): each project (a git repository, else
// a folder), newest first, with its chats newest first
export interface ProjectsData {
  ok: true;
  generatedAt: number;
  projects: ProjectRow[];
  currency?: { code: string; rate: number; date?: string; error?: string };
}

export interface ProjectRow {
  name: string;
  path: string;
  short: string;
  workedMs: number;
  weekMs: number; // the last 7 days
  cost: number;
  lastAt: number;
  chatCount: number;
  chats: { id: string; name: string; profileDir: string; cwd: string; lastAt: number; workedMs: number; cost: number }[];
}

export interface WeekLane {
  key: string;
  id: string;
  host: string;
  name: string;
  profileDir?: string;
  project: string;
  parts: [number, number, "w" | "y" | "p"][];
  workedMs: number;
  waitedMs: number;
}

export interface WeekRecap {
  ok: true;
  generatedAt: number;
  span?: WeekSpan; // (a widget older than the spans sends none: week)
  first?: string | null; // the first day with a chat kept
  back: number; // days (span day) or weeks before today's
  backMax: number;
  account: string; // an account's id, or "" for all
  accounts: { id: string; name: string; plan: string | null; hosts: string[] }[];
  from: string;
  to: string;
  days: WeekDay[];
  sum: WeekSum;
  previous: WeekNumbers | null; // the day or 7 days before (none for everything)
  trend: ((WeekNumbers & { start: number }) | null)[]; // the 5 weeks up to these days (null: older than what's kept)
  projects: WeekProject[];
  chats: WeekChat[];
  // The Day view: a lane per chat, its stretches [from, to, w (worked) | y (waited for you) | p (paused)]
  lanes?: WeekLane[];
  models: WeekModel[];
  cacheSaved: number;
  spend: {
    turns: WeekChat[]; // the turns that cost most
    chats: WeekChat[]; // the chats that cost most
    cold: { count: number; cost: number; chats: WeekChat[] }; // coming back after a chat's cache expired
  };
  work: {
    toolCalls: number;
    tools: [string, number][];
    commands: number;
    edits: number;
    files: number;
    linesAdded: number;
    linesRemoved: number;
    subagents: number;
    compactions: number;
    webSearches: number;
    webFetches: number;
  };
  plan: { account: string; name: string; windows: WeekPlanWindow[] }[]; // (this computer's accounts, these 7 days only)
  prompts: { answered: number; refused: number; waitMs: number; byTool: [string, number][]; rules: WeekRule[] };
  tips: WeekTip[];
  prices: { asOf: string; guessed: string[] };
  currency?: { code: string; rate: number; date: string; error: string }; // rate: per US dollar
  sources: { host: string; ok: boolean; error: string }[];
}

export interface ChatRef {
  host?: string;
  key: string;
  sessionId: string;
  chatId?: string;
  profileDir: string;
  name: string;
  entrypoint?: string;
  pid?: number | null;
  cwd?: string;
  kind?: string;
  state?: SessionState;
  tmux?: boolean;
  jobId?: string | null;
  openCommand?: string | null;
  stopCommand?: string | null;
  vscodeTerminal?: number | null;
  shared?: boolean;
  plan?: { slug: string; path: string } | null; // the plan it made
}

// "answer": Claude's questions, with Answers; approve / approve-edits / keep: a plan (ExitPlanMode)
export type Decision = "allow" | "always" | "deny" | "answer" | "approve" | "approve-edits" | "keep";
export const DECISIONS: readonly Decision[] = ["allow", "always", "deny", "answer", "approve", "approve-edits", "keep"];
export function isDecision(d: unknown): d is Decision {
  return typeof d === "string" && (DECISIONS as readonly string[]).includes(d);
}

// webview -> extension
export type WebviewMessage =
  | { type: "ready" }
  | { type: "shareCard"; how: "save" | "copy"; name: string; png: string } // the Recap's card (base64 PNG)
  | { type: "open"; chat: ChatRef }
  | { type: "openPane"; chat: ChatRef; tab?: "chat" | "review" } // the chat's tab: its conversation, a reply box, its review
  | { type: "openWindow"; chat: ChatRef } // its own terminal or Claude Code tab, whatever a click does
  | { type: "answerPermission"; chat: ChatRef; decision: Decision; note?: string; answers?: Answers } // what a waiting chat asks
  | { type: "openInClaudeCode"; chat: ChatRef }
  | { type: "moveToClaudeCode"; chat: ChatRef } // a chat in a terminal: Claude quits there, the chat reopens in a Claude Code tab
  | { type: "setReopenIn"; where: ReopenIn }
  | { type: "stop"; chat: ChatRef }
  | { type: "dismiss"; chat: ChatRef }
  | { type: "share"; chat: ChatRef }
  | { type: "compareChanges"; chat: ChatRef; turn: boolean }
  | { type: "openAgentRun"; chat: ChatRef; agentId: string }
  | { type: "dismissDigest" }
  | { type: "newChat"; groupId: string; host?: string }
  | { type: "handOver"; chat: ChatRef; profile: string }
  | { type: "openPlan"; chat?: ChatRef; groupId?: string; host?: string; plan?: string; edit?: boolean } // a chat's plan, or an account's (edit: its file, to change it)
  | { type: "sendPlanFeedback"; chat?: ChatRef; groupId?: string; host?: string; plan?: string } // your changes and comments, to the chat that made it
  | { type: "openMemory"; groupId: string; host?: string; memory: string; edit?: boolean } // to read, or (edit) its file
  | { type: "carryOutPlan"; groupId: string; host?: string; plan: string }
  | { type: "continuePlanChat"; groupId: string; host?: string; plan: string }
  | { type: "sharePlan"; groupId: string; host?: string; plan: string }
  | { type: "findChat" }
  | { type: "startQueued"; id: string }
  | { type: "openOverlap"; chat: ChatRef } // the first other chat changing the same files
  | { type: "unqueue"; id: string }
  | { type: "queueContinue"; chat: ChatRef; keep?: boolean } // go on by itself once the limit resets (keep: and after later limits)
  | { type: "keepGoing"; chat: ChatRef; on: boolean } // keep going after each usage limit (or not any more)
  | { type: "star"; chat?: ChatRef; groupId?: string; host?: string; plan?: string; on: boolean } // a chat or an account's plan: starred, or not any more
  | { type: "loginHost"; host: string }
  | { type: "disconnectHost"; host: string }
  | { type: "login"; groupId: string; host?: string }
  | { type: "refresh"; host?: string }
  | { type: "showRemoved"; host?: string }
  | { type: "hideHost"; host: string }
  | { type: "pickHosts" }
  | { type: "setupUsageBars" }
  | { type: "openUsagePage" }
  | { type: "week"; back: number; account: string; span?: WeekSpan } // the Recap tab wants these days' recap
  | { type: "openWeekChat"; chatId: string; profileDir: string; name: string; cwd?: string } // one of the week's chats here
  | { type: "weekRule"; allow: boolean; kind: string; value: string; project: string; id: string; host?: string } // allow a rule the tab offers, or take one back (on an SSH host shown, if host)
  | { type: "weekRevoke"; id: string; host?: string } // take a permission you gave out of its file
  | { type: "perms" } // the Permissions tab wants what it shows
  | { type: "projects" } // the Projects tab wants its projects (week.py --projects)
  | { type: "newChatIn"; path: string; profileDir: string } // + of a project: a new chat there, on the account of its newest chat
  | { type: "copy"; text: string }; // a tip's command, to the clipboard

// extension -> webview
export type HostMessage =
  | { type: "state"; state: ViewState }
  | { type: "showTab"; tab: "chats" | "week" | "perms" } // (the Monday recap's Open the Recap)
  | { type: "petEvent"; kind: PetEventKind; text: string; key: string }
  | { type: "visible"; visible: boolean }
  | { type: "live"; live: Record<string, LiveView> }
  | { type: "week"; back: number; account: string; span: WeekSpan; recap: WeekRecap | null; error: string }
  | { type: "weekRule"; id: string; host: string; ok: boolean; message: string }
  | { type: "weekRevoke"; id: string; host: string; ok: boolean; message: string }
  | { type: "perms"; data: PermsData | null; error: string }
  | { type: "projects"; data: ProjectsData | null; error: string };
