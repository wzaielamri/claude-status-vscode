import { drawCard } from "../util/card";
import { useEffect, useRef, useState } from "preact/hooks";
import type { WeekChat, WeekDay, WeekPlanWindow, WeekRecap, WeekSpan } from "../../src/shared/types";
import {
  cacheText,
  coldText,
  costliestText,
  currencyOf,
  dayTip,
  factsText,
  fillPaths,
  footText,
  hoursText,
  hoverText,
  moneyText,
  periodText,
  periodWaiting,
  resetText,
  rowDay,
  shareText,
  toolsText,
  weekTiles,
  whenText,
  workText,
  type Currency,
} from "../../src/shared/week";
import { post } from "../env";
import { Icon } from "../util/icons";
import { useViewVisible } from "../util/petStage";
import { useStore } from "../util/store";
import { weekAnswer } from "../util/stores";

const REFRESH_MS = 10 * 60000; // while the tab is in sight

// The Recap tab: your last 7 days with Claude. How long you were at it and
// how long Claude worked, a day per row on a timeline, what the same work
// would cost at API prices, where it went, and what would use less of your
// plan (the rules of Claude Code's own /usage). The extension runs the
// widget's week.py for it (only while this tab is open): read from the chats'
// transcripts, no tokens used.
export function WeekView({ hosts, currency }: { hosts: string[]; currency: string }) {
  const visible = useViewVisible();
  const answer = useStore(weekAnswer);
  const [span, setSpan] = useState<WeekSpan>(answer?.span ?? "week");
  const [back, setBack] = useState(answer?.back ?? 0);
  const [account, setAccount] = useState(answer?.account ?? "");
  const [busy, setBusy] = useState(true); // asked, and its answer hasn't come
  const [sharing, setSharing] = useState(false);
  const [copied, setCopied] = useState("");
  const hostsKey = hosts.join(",");

  function ask() {
    setBusy(true);
    post({ type: "week", back, account, span });
  }

  // Ask for these days when the tab is in sight, again every 10 minutes
  useEffect(() => {
    if (!visible) return;
    ask();
    const timer = setInterval(ask, REFRESH_MS);
    return () => clearInterval(timer);
  }, [visible, span, back, account, hostsKey, currency]);

  const mine = answer && answer.span === span && answer.back === back && answer.account === account ? answer : null;
  useEffect(() => {
    if (mine) setBusy(false);
  }, [mine]);
  const recap: WeekRecap | null = mine?.recap ?? answer?.recap ?? null; // (the days before, dimmed, while these come)
  const error = mine?.error ?? "";
  const s = recap?.sum;
  const hasData = !!s && (s.workedMs > 0 || s.prompts > 0 || s.cost > 0);

  function copy(text: string) {
    post({ type: "copy", text });
    setCopied(text);
    setTimeout(() => setCopied((c) => (c === text ? "" : c)), 2000);
  }

  return (
    <div class={"week" + (busy && recap ? " busy" : "")}>
      {/* A day, 7 days or everything kept */}
      <div class="week-spans" role="radiogroup" aria-label="What to show">
        {([["day", "Day"], ["week", "Week"], ["all", "Overall"]] as const).map(([sp, text]) => (
          <button key={sp} type="button" role="radio" aria-checked={span === sp} class={"chip" + (span === sp ? " on" : "")}
            onClick={() => {
              setSpan(sp);
              setBack(0);
            }}>
            {text}
          </button>
        ))}
      </div>
      <div class="week-period">
        {span !== "all" ? (
          <button type="button" class="icon-btn flip" title={span === "day" ? "The day before" : "The week before"}
            aria-label={span === "day" ? "The day before" : "The week before"}
            disabled={!mine || back >= (recap?.backMax ?? 0)} onClick={() => setBack(back + 1)}>
            <Icon name="chevron" />
          </button>
        ) : null}
        <span class="week-span">{mine && recap ? periodText({ ...recap, back }) : periodWaiting(span, back)}</span>
        {span !== "all" ? (
          <button type="button" class="icon-btn" title={span === "day" ? "The day after" : "The week after"}
            aria-label={span === "day" ? "The day after" : "The week after"} disabled={back <= 0}
            onClick={() => setBack(back - 1)}>
            <Icon name="chevron" />
          </button>
        ) : null}
        <button type="button" class="icon-btn" title="Read again" aria-label="Read again" onClick={ask}>
          <Icon name="refresh" />
        </button>
        <button type="button" class="icon-btn" title="Share as a picture: these days as a card with Talpy, to save or copy"
          aria-label="Share as a picture" disabled={!(recap && hasData)} onClick={() => setSharing(true)}>
          <Icon name="share" />
        </button>
      </div>
      {sharing && recap ? <ShareCard recap={recap} onClose={() => setSharing(false)} /> : null}

      {recap && recap.accounts.length > 1 ? (
        <div class="week-accounts" role="radiogroup" aria-label="Whose chats">
          {[{ id: "", name: "All accounts", plan: null }, ...recap.accounts].map((a) => (
            <button key={a.id} type="button" role="radio" aria-checked={account === a.id} class={"chip" + (account === a.id ? " on" : "")}
              title={a.id ? "This account's chats (a chat shared with others counts on each account that has it)" : "All your chats"}
              onClick={() => setAccount(a.id)}>
              {a.name + (a.plan ? ` · ${a.plan}` : "")}
            </button>
          ))}
        </div>
      ) : null}

      {busy && !recap && !error ? (
        <div class="empty muted">
          Reading your chats of these days. The first time takes a few seconds; after that only what's new is read.
        </div>
      ) : null}
      {error ? <div class="msg error" role="alert">Couldn't read your week: {error}</div> : null}
      {recap && !hasData ? (
        <div class="empty muted">
          {recap.span === "day" ? "No Claude on this day: none of your chats here (or on the SSH hosts shown) has a prompt or a reply on it."
            : recap.span === "all" ? "No Claude yet: none of your chats here (or on the SSH hosts shown) has a prompt or a reply."
              : "No Claude in these days: none of your chats here (or on the SSH hosts shown) has a prompt or a reply in them."}
        </div>
      ) : null}

      {recap && s && hasData ? (
        <>
          <div class="week-tiles">
            {weekTiles(recap).map((t) => (
              <div key={t.label} class="week-tile" title={`${t.label}\n${t.tip}`}>
                <span class="week-tile-label muted">{t.label}</span>
                <span class="week-tile-value">{t.value}</span>
                {t.delta ? <span class="week-tile-delta muted">{t.delta}</span> : null}
                <Trend values={t.trend} />
              </div>
            ))}
          </div>
          {factsText(s) ? <div class="week-facts small muted">{factsText(s)}</div> : null}
          <Timeline recap={recap} />
          <Plan recap={recap} />
          <Projects recap={recap} />
          <Lanes recap={recap} />
          <Chats recap={recap} />
          <Work recap={recap} />
          <Models recap={recap} />
          <Spend recap={recap} />
          <Tips recap={recap} copied={copied} onCopy={copy} />
          <div class="week-foot small muted">{footText(recap)}</div>
        </>
      ) : null}
    </div>
  );
}

// The card to share (util/card.ts): a preview, the projects named or not, Save or Copy
function ShareCard({ recap, onClose }: { recap: WeekRecap; onClose: () => void }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [names, setNames] = useState(false);
  useEffect(() => {
    if (canvas.current) drawCard(canvas.current, recap, names);
  }, [recap, names]);
  const send = (how: "save" | "copy") => {
    const url = canvas.current?.toDataURL("image/png") ?? "";
    post({ type: "shareCard", how, name: `claude-${recap.span ?? "week"}-${recap.to}.png`, png: url.slice(url.indexOf(",") + 1) });
  };
  return (
    <div class="week-share" role="dialog" aria-label="Share as a picture">
      <canvas ref={canvas} class="week-share-card" />
      <label class="small"><input type="checkbox" checked={names} onChange={(e) => setNames((e.target as HTMLInputElement).checked)} /> Name the projects on it</label>
      <div class="ask-buttons">
        <button type="button" class="btn" onClick={() => send("save")}>Save</button>
        <button type="button" class="btn secondary" onClick={() => send("copy")}>Copy</button>
        <button type="button" class="btn secondary" onClick={onClose}>Close</button>
      </div>
    </div>
  );
}

// The same number over the 5 weeks up to these days, these last (a week
// the widget no longer has: a dot)
function Trend({ values }: { values: (number | null)[] }) {
  if (values.length < 2) return null;
  const top = Math.max(1e-9, ...values.filter((v): v is number => v !== null));
  return (
    <span class="week-trend" aria-hidden="true">
      {values.map((v, i) => (
        <span key={i} class={(v === null ? "gone" : "") + (i === values.length - 1 ? " now" : "")}
          style={v === null ? undefined : { height: `max(1px, ${(100 * v) / top}%)` }} />
      ))}
    </span>
  );
}

// A chat in the tab's lists: click one of this computer's to bring it up
function ChatLine({ c, cur, where, sub }: { c: WeekChat; cur: Currency; where?: string; sub?: boolean }) {
  return (
    <li>
      <button type="button" class={"week-chat" + (sub ? " sub" : "")} disabled={!!c.host || !c.chatId}
        title={c.host ? `${c.name} on ${c.host}: its row under ${c.host} opens it` : `Open “${c.name}”`}
        onClick={() => post({ type: "openWeekChat", chatId: c.chatId, profileDir: c.profileDir, name: c.name, cwd: c.cwd })}>
        <span class="week-chat-name">{c.name}</span>
        <span class="week-chat-folder small muted">{where ?? (c.host ? c.host + ": " : "") + c.folder}</span>
        <span class="week-chat-value small muted">{hoursText(c.workedMs)} · {moneyText(c.cost, cur)}</span>
      </button>
    </li>
  );
}

// A day per row: when you were at it, when Claude worked (darker where
// several chats worked at once), when it waited for your answer, where a
// usage limit stopped a chat. Click a day for its chats.
function Timeline({ recap }: { recap: WeekRecap }) {
  const [hover, setHover] = useState<{ row: number; x: number; ms: number; px: number } | null>(null);
  const [open, setOpen] = useState("");
  const cur = currencyOf(recap);
  const weeks = recap.span === "all"; // (a row per 7 days, with their numbers)
  const last = weeks ? 0 : recap.days.length - 1;
  const most = Math.max(1, ...recap.days.map((d) => Math.max(d.youMs, d.workedMs)));
  const pct = (d: WeekDay, ms: number) => `${Math.max(0, Math.min(100, (100 * (ms - d.start)) / Math.max(1, d.end - d.start)))}%`;
  const width = (d: WeekDay, a: number, b: number) =>
    `max(1px, ${Math.max(0, (100 * (Math.min(b, d.end) - Math.max(a, d.start))) / Math.max(1, d.end - d.start))}%)`;
  const limits = recap.days.some((d) => d.limits?.length);
  return (
    <section class="week-section">
      <div class="week-heading">
        <h2>{weeks ? "Week by week" : recap.span === "day" ? "Through the day" : "Day by day"}</h2>
        <span class="week-legend small muted">
          <span><span class="swatch you" /> You</span>
          <span><span class="swatch work" /> {weeks ? "Claude worked" : "Claude working"}</span>
          {weeks ? null : <span><span class="swatch wait" /> Waiting for you</span>}
          {limits && !weeks ? <span><span class="swatch limit" /> A usage limit stopped a chat</span> : null}
        </span>
      </div>
      <div class="week-row week-axis small muted" aria-hidden="true">
        <span class="week-day" />
        <span class="week-hours">
          {(weeks ? [] : [0, 6, 12, 18, 24]).map((h) => (
            <span key={h} style={{ left: `${(100 * h) / 24}%` }}>{h}</span>
          ))}
        </span>
        <span class="week-num">You</span>
        <span class="week-num">Claude</span>
      </div>
      {recap.days.map((d, i) => {
        const shown = open === d.day && !!d.chats?.length;
        return (
          <div key={d.day}>
            <div class={"week-row" + (i === last && recap.back === 0 ? " today" : "") + (d.chats?.length ? " opens" : "") + (shown ? " open" : "")}
              title={dayTip(d, cur)} onClick={() => setOpen(shown || !d.chats?.length ? "" : d.day)}>
              <span class="week-day small">{rowDay(d.day, weeks)}</span>
              {weeks ? (
                <span class="week-track">
                  {d.youMs > 0 ? <span class="week-you" style={{ left: 0, width: `max(1px, ${(100 * d.youMs) / most}%)` }} /> : null}
                  {d.workedMs > 0 ? <span class="week-work many" style={{ left: 0, width: `max(1px, ${(100 * d.workedMs) / most}%)` }} /> : null}
                </span>
              ) : (
              <span
                class="week-track"
                onMouseMove={(e) => {
                  const box = (e.currentTarget as HTMLElement).getBoundingClientRect();
                  const x = Math.max(0, Math.min(box.width, e.clientX - box.left));
                  const span = d.end - d.start;
                  setHover({ row: i, x, ms: d.start + (x / Math.max(1, box.width)) * span, px: span / Math.max(1, box.width) });
                }}
                onMouseLeave={() => setHover((h) => (h && h.row === i ? null : h))}
              >
                {d.you.map(([a, b]) => <span key={"y" + a} class="week-you" style={{ left: pct(d, a), width: width(d, a, b) }} />)}
                {d.wait.map(([a, b]) => <span key={"w" + a} class="week-wait" style={{ left: pct(d, a), width: width(d, a, b) }} />)}
                {d.work.map(([a, b, n]) => (
                  <span key={"c" + a} class={"week-work" + (n >= 3 ? " many" : n === 2 ? " two" : "")} style={{ left: pct(d, a), width: width(d, a, b) }} />
                ))}
                {(d.limits ?? []).map((t) => <span key={"l" + t} class="week-limit" style={{ left: pct(d, t) }} />)}
                {i === last && recap.back === 0 ? <span class="week-now" style={{ left: pct(d, recap.generatedAt) }} /> : null}
                {hover && hover.row === i ? <span class="week-cross" style={{ left: `${hover.x}px` }} /> : null}
              </span>
              )}
              <span class="week-num small muted">{d.youMs >= 60000 ? hoursText(d.youMs) : "–"}</span>
              <span class="week-num small">{d.workedMs >= 60000 ? hoursText(d.workedMs) : "–"}</span>
            </div>
            {shown ? (
              <ul class="week-chats week-day-chats">
                {d.chats.map((c) => <ChatLine key={c.host + c.chatId} c={c} cur={cur} sub />)}
              </ul>
            ) : null}
          </div>
        );
      })}
      <div class={"week-readout small" + (hover ? "" : " muted")}>
        {weeks ? "Click a week for its chats."
          : hover ? hoverText(recap.days[hover.row], hover.ms, recap.generatedAt, hover.px) : "Point at a day to see what went on then; click it for its chats."}
      </div>
    </section>
  );
}

// How full your plan's windows are now, and how they filled up (this
// computer's accounts, from the widget's usage history)
function Plan({ recap }: { recap: WeekRecap }) {
  const plans = recap.plan ?? [];
  if (!plans.length) return null;
  return (
    <section class="week-section">
      <h2>Your plan's limits now</h2>
      <ul class="week-bars week-plan">
        {plans.flatMap((p) =>
          p.windows.map((w) => (
            <li key={p.account + w.key} title={`${w.label}: ${Math.round(w.pct)}% used\nHow this window of your plan filled up, from its start (on the left) to when it resets (on the right), as the extension saw it.`}>
              <span class="week-bar-name">{(plans.length > 1 ? p.name + " · " : "") + w.label}</span>
              <FillChart w={w} now={recap.generatedAt} />
              <span class={"week-bar-value small" + (w.pct >= 90 ? " over" : " muted")}>
                {Math.round(w.pct)}%{w.resetsAt ? ` · ${resetText(w.resetsAt, recap.generatedAt)}` : ""}
              </span>
            </li>
          )),
        )}
      </ul>
    </section>
  );
}

function FillChart({ w, now }: { w: WeekPlanWindow; now: number }) {
  const { line, area } = fillPaths(w, now, 100, 20);
  const level = w.pct >= 90 ? " high" : w.pct >= 70 ? " mid" : "";
  return (
    <svg class={"week-fill" + level} viewBox="0 0 100 20" preserveAspectRatio="none" aria-hidden="true">
      {area ? <path class="area" d={area} /> : null}
      {line ? <path class="line" d={line} vector-effect="non-scaling-stroke" /> : null}
    </svg>
  );
}

// The project folders Claude worked in most (a chat started in a subfolder
// of a repository counts for the repository): click one for its chats
function Projects({ recap }: { recap: WeekRecap }) {
  const [open, setOpen] = useState("");
  if (!recap.projects.length) return null;
  const cur = currencyOf(recap);
  const most = Math.max(1, ...recap.projects.map((p) => p.workedMs));
  return (
    <section class="week-section">
      <h2>Where Claude worked</h2>
      <ul class="week-bars">
        {recap.projects.map((p) => {
          const key = p.host + "|" + p.path;
          const shown = open === key;
          const chats = Array.isArray(p.chats) ? p.chats : [];
          return [
            <li key={key} class={"opens" + (shown ? " open" : "")} onClick={() => setOpen(shown ? "" : key)}
              title={`${p.host ? p.host + ": " : ""}${p.path}\nClaude worked ${hoursText(p.workedMs)} · ${moneyText(p.cost, cur)} at API prices\n` +
                `${p.prompts} prompts · ${p.chatCount ?? chats.length} chats\nClick for its chats`}>
              <span class="week-bar-name">
                <Icon name="chevron" class={shown ? "turn" : ""} />
                {(p.host ? p.host + ": " : "") + p.name}
              </span>
              <span class="week-bar-meter work"><span style={{ width: `${(100 * p.workedMs) / most}%` }} /></span>
              <span class="week-bar-value small muted">{hoursText(p.workedMs)} · {moneyText(p.cost, cur)}</span>
            </li>,
            shown ? (
              <li key={key + "/chats"} class="week-sub">
                <ul class="week-chats">
                  {chats.map((c) => <ChatLine key={c.host + c.chatId} c={c} cur={cur} where="" sub />)}
                </ul>
                {(p.chatCount ?? 0) > chats.length ? (
                  <span class="small muted week-more">and {(p.chatCount ?? 0) - chats.length} more {(p.chatCount ?? 0) - chats.length === 1 ? "chat" : "chats"}</span>
                ) : null}
              </li>
            ) : null,
          ];
        })}
      </ul>
    </section>
  );
}

// The Day view, chat by chat: when each chat worked, waited for you (a
// permission, a question) or was paused (asleep, a usage limit); between
// those it was your turn. A name brings the chat up.
function Lanes({ recap }: { recap: WeekRecap }) {
  const lanes = recap.lanes ?? [];
  if (!lanes.length) return null;
  const t0 = Math.min(...lanes.map((l) => l.parts[0][0]));
  const t1 = Math.max(...lanes.map((l) => l.parts[l.parts.length - 1][1]));
  const span = Math.max(60000, t1 - t0);
  const time = (ms: number) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const kinds = { w: "Claude worked", y: "Waited for you", p: "Paused (asleep, a limit)" };
  return (
    <section class="week-section">
      <h2>Chat by chat</h2>
      <div class="week-lanes-legend small muted">
        {(["w", "y", "p"] as const).map((k) => <span key={k}><i class={`lane-${k}`} aria-hidden="true" />{kinds[k]}</span>)}
      </div>
      <ul class="week-lanes">
        {lanes.map((l) => (
          <li key={l.key}>
            <button type="button" class="week-lane-name small" disabled={!!l.host || !l.id}
              title={l.host ? `${l.name} on ${l.host}` : `Open “${l.name}”`}
              onClick={() => post({ type: "openWeekChat", chatId: l.id, profileDir: l.profileDir ?? "", name: l.name, cwd: "" })}>
              {(l.host ? l.host + ": " : "") + l.name}
            </button>
            <span class="week-lane" title={`${l.name}\nClaude worked ${hoursText(l.workedMs)}, waited for you ${hoursText(l.waitedMs)}${l.project ? `\n${l.project}` : ""}`}>
              {l.parts.map(([a, b, k], i) => (
                <i key={i} class={`lane-${k}`} style={{ left: `${((a - t0) / span) * 100}%`, width: `max(1.5px, ${((b - a) / span) * 100}%)` }} />
              ))}
            </span>
          </li>
        ))}
      </ul>
      <div class="week-lanes-times small muted"><span>{time(t0)}</span><span>{time(t1)}</span></div>
    </section>
  );
}

// The chats Claude worked on most
function Chats({ recap }: { recap: WeekRecap }) {
  if (!recap.chats.length) return null;
  const cur = currencyOf(recap);
  return (
    <section class="week-section">
      <h2>Chats it worked on most</h2>
      <ul class="week-chats">
        {recap.chats.map((c) => <ChatLine key={c.host + c.chatId} c={c} cur={cur} />)}
      </ul>
    </section>
  );
}

// What Claude did: its tool calls, commands, edits, subagents
function Work({ recap }: { recap: WeekRecap }) {
  const text = workText(recap.work);
  if (!text) return null;
  return (
    <section class="week-section">
      <h2>What Claude did</h2>
      <div>{text}</div>
      {toolsText(recap.work) ? <div class="small muted">{toolsText(recap.work)}</div> : null}
    </section>
  );
}

// What it'd cost on the API, by model, and what the cache saved
function Models({ recap }: { recap: WeekRecap }) {
  if (!recap.models.length) return null;
  const cur = currencyOf(recap);
  return (
    <section class="week-section">
      <h2>At API prices</h2>
      <ul class="week-bars">
        {recap.models.map((m) => (
          <li key={m.model} title={`${m.model}: ${moneyText(m.cost, cur)} · ${m.requests} requests`}>
            <span class="week-bar-name">{m.model}</span>
            <span class="week-bar-meter"><span style={{ width: `${100 * m.share}%` }} /></span>
            <span class="week-bar-value small muted">{moneyText(m.cost, cur)} · {shareText(m.share)}</span>
          </li>
        ))}
      </ul>
      {cacheText(recap) ? <div class="small muted">{cacheText(recap)}</div> : null}
    </section>
  );
}

// Where the money went: the turns that cost most, the chats, and coming
// back to chats whose cache had expired
function Spend({ recap }: { recap: WeekRecap }) {
  const spend = recap.spend;
  if (!spend || (!spend.turns.length && !spend.cold.count)) return null;
  const cur = currencyOf(recap);
  return (
    <section class="week-section">
      {spend.turns.length ? (
        <>
          <h2>Turns that cost the most</h2>
          <ul class="week-chats">
            {spend.turns.map((t) => <ChatLine key={t.host + t.chatId + t.startedAt} c={t} cur={cur} where={t.startedAt ? whenText(t.startedAt) : ""} />)}
          </ul>
        </>
      ) : null}
      {costliestText(recap) ? <div class="small muted">{costliestText(recap)}</div> : null}
      {coldText(recap) ? <div class="small muted">{coldText(recap)}</div> : null}
    </section>
  );
}

// What would use less of your plan (Claude Code's /usage rules)
function Tips({ recap, copied, onCopy }: { recap: WeekRecap; copied: string; onCopy: (text: string) => void }) {
  return (
    <section class="week-section">
      <h2>What could use less of your plan</h2>
      {!recap.tips.length ? <div class="muted">Nothing stands out: no habit takes a tenth of the usage shown.</div> : null}
      <ul class="week-tips">
        {recap.tips.map((t) => (
          <li key={t.key} class="week-tip">
            <span class="week-tip-text">
              <span class="week-tip-title">{t.title}</span>
              {t.detail ? <span class="small muted">{t.detail}</span> : null}
            </span>
            {t.pct !== null ? (
              <span class="week-tip-pct small"
                title={`${t.pct}% of the usage shown: of the usage of these days, as Claude Code's /usage weighs it toward your plan's limits. A request can count for several of these.`}>
                {t.pct}%
              </span>
            ) : null}
            {t.command ? (
              <button type="button" class="chip code" title={`Copy ${t.command}, to type it in a chat of yours`} onClick={() => onCopy(t.command)}>
                {copied === t.command ? "Copied" : t.command}
              </button>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}
