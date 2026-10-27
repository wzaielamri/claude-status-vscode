// The Recap tab's words and numbers (webview/components/WeekView.tsx), from
// the recap of the widget's week.py; here, so the tests can read them.

import { clockText, dayLabel, plural } from "./model";
import type { WeekDay, WeekGrant, WeekNumbers, WeekPlanWindow, WeekRecap, WeekRule, WeekSpan, WeekSum } from "./types";

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const WEEKDAYS_LONG = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
  "November", "December"];

// A local YYYY-MM-DD as a Date at local midnight
function localDate(day: string): Date {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(y, (m || 1) - 1, d || 1);
}

// 3h 5m, 45m: a week of work reads better in hours than in days
export function hoursText(ms: number): string {
  const m = Math.round(Math.max(0, ms) / 60000);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

// The amounts are API prices in US dollars; in another currency (the
// talpyDen.recapCurrency setting) week.py sends the European Central
// Bank's rate of the day
export interface Currency {
  code: string;
  rate: number; // per US dollar
}

const USD: Currency = { code: "USD", rate: 1 };
const SYMBOLS: Record<string, string> = {
  USD: "$", EUR: "€", GBP: "£", JPY: "¥", CNY: "¥", INR: "₹", KRW: "₩", ILS: "₪", TRY: "₺", PHP: "₱", THB: "฿",
};
const WHOLE_UNITS = ["JPY", "KRW", "HUF", "ISK", "IDR"];

export function currencyOf(recap: Pick<WeekRecap, "currency">): Currency {
  const c = recap.currency;
  return c && !c.error && c.rate > 0 && /^[A-Z]{3}$/.test(c.code) ? c : USD;
}

export function moneyText(usd: number, cur: Currency = USD): string {
  const amount = (usd || 0) * cur.rate;
  const symbol = SYMBOLS[cur.code];
  const put = (text: string) => (symbol ? symbol + text : `${text} ${cur.code}`);
  if (!amount) return put("0");
  const whole = amount >= 100 || WHOLE_UNITS.includes(cur.code);
  if (amount < (whole ? 1 : 0.01)) return "<" + put(whole ? "1" : "0.01");
  const digits = whole ? 0 : 2;
  return put(amount.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }));
}

// "24 Oct"
export function shortDay(day: string): string {
  const d = localDate(day);
  return `${d.getDate()} ${MONTHS[d.getMonth()]}`;
}

export function longDay(day: string): string {
  const d = localDate(day);
  return `${WEEKDAYS_LONG[d.getDay()]} ${d.getDate()} ${MONTHS_LONG[d.getMonth()]}`;
}

// "24 October 2026"
export function dateText(day: string): string {
  const d = localDate(day);
  return `${d.getDate()} ${MONTHS_LONG[d.getMonth()]} ${d.getFullYear()}`;
}

// "Sat 24": a timeline row's day ("19 Oct": Overall's 7 days)
export function rowDay(day: string, weeks = false): string {
  return weeks ? shortDay(day) : dayLabel(day).split(" ").slice(0, 2).join(" ");
}

// "Wed 14:05": when a turn started
export function whenText(ms: number): string {
  return `${WEEKDAYS[new Date(ms).getDay()]} ${clockText(ms)}`;
}

export function periodText(recap: WeekRecap): string {
  if (recap.span === "day") {
    const day = rowDay(recap.from) + " " + shortDay(recap.from).split(" ")[1];
    return recap.back === 0 ? `Today · ${day}` : recap.back === 1 ? `Yesterday · ${day}` : day;
  }
  if (recap.span === "all") return recap.first ? `Everything since ${dateText(recap.first)}` : "Everything";
  const span = `${shortDay(recap.from)} – ${shortDay(recap.to)}`;
  return recap.back === 0 ? `Last 7 days · ${span}` : span;
}

// What the period line says before the answer for a span comes
export function periodWaiting(span: WeekSpan, back: number): string {
  return back !== 0 ? "" : span === "day" ? "Today" : span === "all" ? "Everything" : "Last 7 days";
}

// The 7 days before, when there was anything in them
export function previousOf(recap: WeekRecap): WeekNumbers | null {
  const p = recap.previous;
  return p && (p.workedMs || p.prompts || p.cost) ? p : null;
}

// The change against the 7 days before: "+4h 44m", "−$12"
export function deltaText(now: number, before: number | null, kind: Kind, cur: Currency = USD): string {
  if (before === null) return "";
  const d = now - before;
  const abs = Math.abs(d);
  if (kind === "time" ? abs < 60000 : kind === "money" ? abs < 0.01 : kind === "tokens" ? abs < 50 : d === 0) return "same as before";
  return (d > 0 ? "+" : "−") + amountText(abs, kind, cur);
}

type NumberKey = "youMs" | "workedMs" | "cost" | "tokenTotal" | "prompts";
type Kind = "time" | "money" | "tokens" | "count";

// A number of the tiles, in its own unit
function amountText(v: number, kind: Kind, cur: Currency): string {
  return kind === "time" ? hoursText(v) : kind === "money" ? moneyText(v, cur) : kind === "tokens" ? shortCount(v) : String(v);
}

export interface WeekTile {
  label: string;
  value: string;
  delta: string;
  tip: string;
  trend: (number | null)[]; // the 5 weeks up to these days, these last (null: older than what's kept)
}

// One number over the weeks up to these days
export function trendOf(recap: WeekRecap, key: NumberKey): (number | null)[] {
  return (recap.trend ?? []).map((w) => (w ? w[key] ?? 0 : null));
}

export function weekTiles(recap: WeekRecap): WeekTile[] {
  const s = recap.sum;
  const p = previousOf(recap);
  const cur = currencyOf(recap);
  const shown = (v: number, kind: Kind) => amountText(v, kind, cur);
  const day = recap.span === "day";
  const before = (v: number, kind: Kind) => (p ? `\n${day ? "The day before" : "The 7 days before"}: ${shown(v, kind)}` : "");
  const weeks = (key: NumberKey, kind: Kind) => {
    const values = trendOf(recap, key);
    return values.length > 1
      ? `\n${day ? "Day by day, this day last" : "Week by week, these 7 days last"}: ${values.map((v) => (v === null ? "?" : shown(v, kind))).join(" · ")}`
      : "";
  };
  return [
    {
      label: "You",
      value: hoursText(s.youMs),
      delta: deltaText(s.youMs, p ? p.youMs : null, "time"),
      tip:
        "The time you spent writing to Claude, reading its replies and answering its questions, in all your chats and " +
        "accounts: the time between your prompts and its replies counts when it's under 15 minutes." + before(p ? p.youMs : 0, "time") +
        weeks("youMs", "time"),
      trend: trendOf(recap, "youMs"),
    },
    {
      label: "Claude worked",
      value: hoursText(s.workedMs),
      delta: deltaText(s.workedMs, p ? p.workedMs : null, "time"),
      tip:
        "Claude's own count of its turns (its “Worked for …”), all chats added up: chats working at the same time each " +
        `count. On the clock it worked ${hoursText(s.busyMs)}, and it waited ${hoursText(s.waitMs)} for your answers.` +
        before(p ? p.workedMs : 0, "time") + weeks("workedMs", "time"),
      trend: trendOf(recap, "workedMs"),
    },
    {
      label: "At API prices",
      value: moneyText(s.cost, cur),
      delta: deltaText(s.cost, p ? p.cost : null, "money", cur),
      tip:
        `What the same tokens would cost on Claude's API, at its list prices of ${dateText(recap.prices.asOf)}. ` +
        "Your plan isn't billed this way: this is the value of what you used." + before(p ? p.cost : 0, "money") + weeks("cost", "money"),
      trend: trendOf(recap, "cost"),
    },
    {
      label: "Tokens",
      value: shortCount(s.tokenTotal),
      delta: deltaText(s.tokenTotal, p ? p.tokenTotal : null, "tokens"),
      tip: tokensText(s.tokens) + before(p ? p.tokenTotal : 0, "tokens") + weeks("tokenTotal", "tokens"),
      trend: trendOf(recap, "tokenTotal"),
    },
    {
      label: "Prompts",
      value: String(s.prompts),
      delta: deltaText(s.prompts, p ? p.prompts : null, "count"),
      tip: `${plural(s.chats, "In %1 chat", "In %1 chats")} · ${plural(s.turns, "%1 turn of Claude's", "%1 turns of Claude's")}` +
        before(p ? p.prompts : 0, "count") + weeks("prompts", "count"),
      trend: trendOf(recap, "prompts"),
    },
  ];
}

// What the tokens were: all that Claude read and wrote, by kind
function tokensText(t: WeekSum["tokens"]): string {
  return `Everything Claude read and wrote: ${shortCount(t.input)} of new input, ${shortCount(t.cacheRead)} read from the cache, ` +
    `${shortCount(t.cacheWrite5m + t.cacheWrite1h)} written to the cache and ${shortCount(t.output)} that Claude wrote.`;
}

export function factsText(s: WeekSum): string {
  const parts: string[] = [];
  if (s.longestTurn && s.longestTurn.workedMs >= 60000) parts.push(`longest turn ${hoursText(s.longestTurn.workedMs)} (${s.longestTurn.name || "?"})`);
  if (s.peakAtOnce > 1) parts.push(`up to ${s.peakAtOnce} chats at once`);
  if (s.busiestDay) parts.push(`busiest day ${WEEKDAYS_LONG[localDate(s.busiestDay).getDay()]}`);
  if (s.limits) parts.push(s.limits === 1 ? "a usage limit stopped you once" : `usage limits stopped you ${s.limits} times`);
  return parts.join(" · ");
}

// What went on at ms, on its day's row. px: a pixel's ms, so a mark a pixel
// or two wide can be hit
export function hoverText(d: WeekDay, ms: number, now: number, px: number): string {
  const when = `${dayLabel(d.day)}, ${clockText(ms)}: `;
  if (ms > now) return when + "still to come";
  const at = <T extends number[]>(list: T[]): T | undefined =>
    list.find((s) => s[0] <= ms && ms < s[1]) ?? list.find((s) => s[1] - s[0] < 3 * px && s[0] - 2 * px <= ms && ms < s[1] + 2 * px);
  const span = (s: number[]) => `${clockText(s[0])}–${clockText(s[1])}`;
  const work = at(d.work);
  const wait = at(d.wait);
  const you = at(d.you);
  const parts: string[] = [];
  const limit = (d.limits ?? []).find((t) => Math.abs(t - ms) <= 3 * px);
  if (limit !== undefined) parts.push(`a usage limit stopped a chat at ${clockText(limit)}`);
  if (work) parts.push(work[2] > 1 ? `Claude worked ${span(work)}, ${work[2]} chats at once` : `Claude worked ${span(work)}`);
  else if (wait) parts.push(`Claude waited for your answer ${span(wait)}`);
  if (you) parts.push(`you were at it ${span(you)}`);
  return when + (parts.length ? parts.join(" · ") : "nothing went on");
}

export function dayTip(d: WeekDay, cur: Currency = USD): string {
  return (
    `${d.to ? `${shortDay(d.day)} – ${shortDay(d.to)}` : longDay(d.day)}\nYou: ${hoursText(d.youMs)} · Claude worked ${hoursText(d.workedMs)} (on the clock ${hoursText(d.busyMs)}) · ` +
    `it waited ${hoursText(d.waitMs)} for you\n${plural(d.prompts, "%1 prompt", "%1 prompts")} · ${shortCount(d.tokenTotal)} tokens · ${moneyText(d.cost, cur)} at API prices` +
    (d.chats?.length ? "\nClick for its chats" : "")
  );
}

export function cacheText(recap: WeekRecap): string {
  const t = recap.sum.tokens;
  const input = t.input + t.cacheRead + t.cacheWrite5m + t.cacheWrite1h;
  if (!t.cacheRead) return "";
  const pct = Math.round((100 * t.cacheRead) / Math.max(1, input));
  return `${pct}% of the ${shortCount(input)} input tokens came from the cache: it saved ${moneyText(recap.cacheSaved, currencyOf(recap))}. ` +
    `Claude wrote ${shortCount(t.output)} tokens.`;
}

function shortCount(n: number): string {
  return n >= 999.95e6 ? `${(n / 1e9).toFixed(1)}B` : n >= 999.95e3 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n);
}

function count(n: number): string {
  return (n || 0).toLocaleString("en-US");
}

export function shareText(share: number): string {
  return share < 0.01 ? "<1%" : `${Math.round(share * 100)}%`;
}

// What Claude did, in a line: its tool calls, commands, edits, subagents
export function workText(w: WeekRecap["work"] | undefined): string {
  if (!w || !w.toolCalls) return "";
  const n = (v: number, one: string, many: string) => (v === 1 ? one : many).replace("%1", count(v));
  const parts = [n(w.toolCalls, "%1 tool call", "%1 tool calls")];
  if (w.commands) parts.push(n(w.commands, "%1 command run", "%1 commands run"));
  if (w.edits)
    parts.push(`${n(w.edits, "%1 edit", "%1 edits")} ${n(w.files, "in %1 file", "in %1 files")} (+${count(w.linesAdded)} −${count(w.linesRemoved)})`);
  if (w.subagents) parts.push(n(w.subagents, "%1 subagent started", "%1 subagents started"));
  if (w.webSearches) parts.push(n(w.webSearches, "%1 web search", "%1 web searches"));
  if (w.webFetches) parts.push(n(w.webFetches, "%1 page fetched", "%1 pages fetched"));
  if (w.compactions) parts.push(n(w.compactions, "%1 compaction", "%1 compactions"));
  return parts.join(" · ");
}

export function toolsText(w: WeekRecap["work"] | undefined): string {
  return w && w.tools.length ? `Its tools: ${w.tools.map(([name, n]) => `${name} ${count(n)}`).join(" · ")}` : "";
}

// A plan window, from its start (its reset less its length) to its reset
const WINDOW_MS: Record<string, number> = {
  five_hour: 5 * 3600000, seven_day: 7 * 86400000, seven_day_opus: 7 * 86400000, seven_day_sonnet: 7 * 86400000,
};

export function windowSpan(w: WeekPlanWindow): [number, number] {
  const from = w.resetsAt && WINDOW_MS[w.key] ? w.resetsAt - WINDOW_MS[w.key] : w.points.length ? w.points[0][0] : 0;
  return [from, Math.max(w.resetsAt ?? 0, from + 1)];
}

// The window's fill as SVG paths in a width × height box: the line, and the
// area under it (up to now)
export function fillPaths(w: WeekPlanWindow, now: number, width: number, height: number): { line: string; area: string } {
  const [from, until] = windowSpan(w);
  const points = w.points.filter((p) => p[0] >= from);
  if (!points.length) return { line: "", area: "" };
  const x = (ms: number) => Math.max(0, Math.min(width, ((ms - from) / (until - from)) * width)).toFixed(1);
  const y = (pct: number) => (height - 0.5 - (Math.max(0, Math.min(100, pct)) / 100) * (height - 1)).toFixed(1);
  const end = Math.min(now, until);
  const last = points[points.length - 1][1];
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(p[0])},${y(p[1])}`).join(" ") + ` L${x(end)},${y(last)}`;
  return { line, area: `M${x(points[0][0])},${height} L${line.slice(1)} L${x(end)},${height} Z` };
}

export function resetText(ms: number | null, now: number): string {
  if (!ms) return "";
  return `resets ${ms - now < 86400000 ? clockText(ms) : whenText(ms)}`;
}

export function promptsText(p: WeekRecap["prompts"] | undefined): string {
  if (!p || !p.answered) return "";
  const answered = plural(p.answered, "%1 permission prompt", "%1 permission prompts");
  return (p.refused ? `You answered ${answered} and refused ${p.refused} of them.` : `You answered ${answered}.`) +
    (p.waitMs >= 60000 ? ` They waited for your answer ${hoursText(p.waitMs)} in all.` : "");
}

// The permissions you gave: the first few, until you ask for all
export const GRANTED_FEW = 8;

export function grantedIntro(granted: WeekGrant[], host = ""): string {
  if (!granted.length) return `Nothing lets chats on ${host || "this computer"} skip a permission prompt: they ask you each time.`;
  return host
    ? `What lets chats on ${host} go without asking you, as listed there: its settings, its trusted folders, and the .claude ` +
      `settings of the folders Claude ran in. Revoke takes one out of its file on ${host}.`
    : "What lets chats on this computer go without asking you: your settings and your administrator's, the folders you " +
      "trusted, the .claude settings of the folders Claude ran in, and what you approved in them. Revoke takes one out of its file.";
}

export function grantMeta(g: WeekGrant): string {
  return `${g.place} · ${g.where}`;
}

// A revoke message from the page, as the extension takes it: an id of
// week.py's list (which checks again that it's one of it)
export function weekRevokeAsked(id: unknown): string | null {
  return typeof id === "string" && /^[0-9a-f]{12}$/.test(id) ? id : null;
}

// The SSH host a permission message is for: a plain name or "" (this
// computer); the extension acts on it only if it's one of the hosts shown
export function permsHost(host: unknown): string | null {
  if (host === undefined || host === "") return "";
  return typeof host === "string" && /^[A-Za-z0-9_.@-]{1,255}$/.test(host) && !host.startsWith("-") ? host : null;
}

export function ruleMeta(r: WeekRule): string {
  return [plural(r.count, "%1 prompt", "%1 prompts"), r.waitMs >= 60000 ? `waited ${hoursText(r.waitMs)} for you` : "", `e.g. ${r.example}`]
    .filter(Boolean).join(" · ");
}

export function coldText(recap: WeekRecap): string {
  const cold = recap.spend?.cold;
  if (!cold || !cold.count) return "";
  const cur = currencyOf(recap);
  const most = cold.chats.map((c) => `${c.name} (${c.resumes ?? 0}×, ${moneyText(c.cost, cur)})`).join(", ");
  return (cold.count === 1
    ? "You came back to a chat after its cache had expired (an hour without a reply) once: writing its context again cost " +
      `${moneyText(cold.cost, cur)} more than reading it from the cache.`
    : `You came back to chats after their cache had expired (an hour without a reply) ${cold.count} times: writing their ` +
      `context again cost ${moneyText(cold.cost, cur)} more than reading it from the cache.`) + (most ? ` Most in: ${most}.` : "");
}

export function costliestText(recap: WeekRecap): string {
  const chats = recap.spend?.chats ?? [];
  const cur = currencyOf(recap);
  return chats.length ? `Costliest chats: ${chats.map((c) => `${c.name} ${moneyText(c.cost, cur)}`).join(" · ")}` : "";
}

export function footText(recap: WeekRecap): string {
  const hosts = recap.sources.filter((s) => s.host && s.ok).map((s) => s.host);
  const guessed = recap.prices.guessed.length ? ` (${recap.prices.guessed.join(", ")} at the price of its family's newest model)` : "";
  const lines = [
    `Read from the chats on this computer${hosts.length ? ` and on ${hosts.join(", ")}` : ""}, without using any tokens. ` +
      `Prices: Claude's API list prices of ${dateText(recap.prices.asOf)}${guessed}.`,
  ];
  const c = recap.currency;
  if (c?.error) lines.push(`Amounts in US dollars: ${c.error}.`);
  else if (c && c.code !== "USD" && c.date)
    lines.push(`Amounts in ${c.code} at the European Central Bank's rate of ${dateText(c.date)}: 1 USD = ${c.rate.toFixed(4)} ${c.code}.`);
  for (const s of recap.sources) if (!s.ok) lines.push(`${s.host} couldn't be read: ${s.error}`);
  return lines.join("\n");
}

// Week messages from the page, as the extension takes them: a span, days or
// weeks back (any: week.py stops at the first chat kept), an account id or all
export function weekAsked(back: unknown, account: unknown, span: unknown = "week"): { back: number; account: string; span: WeekSpan } {
  const b = typeof back === "number" && Number.isInteger(back) ? Math.min(3650, Math.max(0, back)) : 0;
  const a = typeof account === "string" && /^[0-9a-f]{12}$/.test(account) ? account : "";
  const sp: WeekSpan = span === "day" || span === "all" ? span : "week";
  return { back: b, account: a, span: sp };
}

export type WeekRuleAsk = { allow: true; kind: string; value: string; project: string } | { allow: false; id: string };

// A rule message from the page, as the extension takes it (week.py checks
// again that it's a rule the tab offers, for a folder a chat started in)
export function weekRuleAsked(m: { allow?: unknown; kind?: unknown; value?: unknown; project?: unknown; id?: unknown }): WeekRuleAsk | null {
  const plain = (v: unknown): v is string => typeof v === "string" && v.length > 0 && v.length <= 1000 && !/[\0\n\r]/.test(v);
  if (m.allow === true) {
    if (typeof m.kind !== "string" || !["dir", "edits", "allow"].includes(m.kind)) return null;
    if (!plain(m.value) || !plain(m.project) || !m.project.startsWith("/")) return null;
    return { allow: true, kind: m.kind, value: m.value, project: m.project };
  }
  if (m.allow === false && typeof m.id === "string" && /^[0-9a-f]{12}$/.test(m.id)) return { allow: false, id: m.id };
  return null;
}
