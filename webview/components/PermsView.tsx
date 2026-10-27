import { useEffect, useState } from "preact/hooks";
import type { PermsData, WeekGrant, WeekRule } from "../../src/shared/types";
import { GRANTED_FEW, grantedIntro, grantMeta, promptsText, ruleMeta } from "../../src/shared/week";
import { post } from "../env";
import { useViewVisible } from "../util/petStage";
import { useStore } from "../util/store";
import { permsAnswer, weekRevokeAnswer, weekRuleAnswer } from "../util/stores";

const REFRESH_MS = 10 * 60000; // while the tab is in sight

// The Permissions tab: the permissions you gave, every rule and mode that
// lets chats on this computer go without asking (each to revoke), and the
// permission prompts you answered in the last 7 days with the rules that
// would have let the most of them through (talpyDen.recapRules can leave
// those out). The extension runs the widget's week.py --permissions for it
// (only while this tab is open): read from your settings and the chats'
// transcripts, no tokens used.
export function PermsView({ rules }: { rules: boolean }) {
  const visible = useViewVisible();
  const answer = useStore(permsAnswer);
  const [busy, setBusy] = useState(true); // asked, and its answer hasn't come

  function ask() {
    setBusy(true);
    post({ type: "perms" });
  }

  // Ask when the tab is in sight, again every 10 minutes
  useEffect(() => {
    if (!visible) return;
    ask();
    const timer = setInterval(ask, REFRESH_MS);
    return () => clearInterval(timer);
  }, [visible]);
  useEffect(() => {
    if (answer) setBusy(false);
  }, [answer]);

  const data = answer?.data ?? null;
  return (
    <div class={"week" + (busy && data ? " busy" : "")}>
      {busy && !data && !answer?.error ? <div class="muted">Reading your settings and the chats of the last 7 days.</div> : null}
      {answer?.error ? <div class="error">Couldn't read your permissions: {answer.error}</div> : null}
      {data ? (
        <>
          {/* this computer's, then each SSH host's shown (as listed there) */}
          {[{ host: "", ok: true, error: "", prompts: data.prompts, granted: data.granted }, ...(data.hosts ?? [])].map((src) =>
            src.ok ? (
              <div key={src.host} class="week">
                <Granted granted={src.granted ?? []} host={src.host} reload={ask} />
                <Prompts prompts={src.prompts ?? null} host={src.host} rules={rules} reload={ask} />
              </div>
            ) : (
              <div key={src.host} class="msg error" role="alert">Couldn't read the permissions on {src.host}: {src.error || "no answer"}</div>
            ))}
          <div class="week-foot small muted">
            Read from your settings and the chats on this computer{data.hosts?.length ? " and on the SSH hosts shown" : ""}, without using any tokens.
          </div>
        </>
      ) : null}
    </div>
  );
}

// The permission prompts you answered, and the rules that would have let
// the most of them through (written only when you allow one; the settings
// can leave them out)
function Prompts({ prompts: p, host, rules: suggest, reload }: {
  prompts: PermsData["prompts"] | null; host: string; rules: boolean; reload: () => void;
}) {
  const ruleAnswer = useStore(weekRuleAnswer);
  const [asking, setAsking] = useState("");
  const [busy, setBusy] = useState("");
  useEffect(() => {
    if (!ruleAnswer) return;
    setBusy("");
    reload();
  }, [ruleAnswer]);
  if (!p) return null;
  const rules = suggest ? p.rules : [];

  function change(rule: WeekRule, allow: boolean) {
    setAsking("");
    setBusy(rule.id);
    post(allow
      ? { type: "weekRule", allow: true, kind: rule.kind, value: rule.value, project: rule.project, id: "", host }
      : { type: "weekRule", allow: false, kind: "", value: "", project: "", id: rule.id, host });
  }

  return (
    <section class="week-section">
      <h2>{host ? `Permission prompts on ${host}, last 7 days` : "Permission prompts, last 7 days"}</h2>
      <div>{p.answered ? promptsText(p) : `You answered no permission prompts on ${host || "this computer"} in the last 7 days.`}</div>
      {p.byTool.length ? <div class="small muted">For: {p.byTool.map(([tool, n]) => `${tool} ${n}`).join(" · ")}</div> : null}
      {rules.length ? (
        <div class="small muted">
          These rules would have let the most of them through. One you allow goes into the .claude/settings.local.json of the
          folder the chats started in (not shared through git), and only chats started there follow it.
        </div>
      ) : null}
      <ul class="week-rules">
        {rules.map((r) => (
          <li key={r.id} class="week-rule">
            <span class="week-rule-text">
              <span>{r.title}</span>
              <span class="small muted">{ruleMeta(r)}</span>
              {asking === r.id ? (
                <span class="week-rule-ask">
                  <span class="small">This {r.change}.</span>
                  <span class="week-rule-buttons">
                    <button type="button" class="chip on" onClick={() => change(r, true)}>Allow</button>
                    <button type="button" class="chip" onClick={() => setAsking("")}>Cancel</button>
                  </span>
                </span>
              ) : null}
            </span>
            {busy === r.id ? <span class="small muted">…</span>
              : r.state === "allowed" ? <span class="small muted">Allowed now</span>
              : r.state === "added" ? (
                <button type="button" class="chip" disabled={!!busy} onClick={() => change(r, false)}
                  title="You allowed it here: Undo removes it from the file it went into, as it was before.">Undo</button>
              ) : asking !== r.id ? (
                <button type="button" class="chip" disabled={!!busy} onClick={() => setAsking(r.id)}>Allow…</button>
              ) : null}
          </li>
        ))}
      </ul>
      {ruleAnswer?.message && ruleAnswer.host === host ? (
        <div class={"small" + (ruleAnswer.ok ? "" : " error")} role="status">{ruleAnswer.ok ? ruleAnswer.message : `Nothing changed: ${ruleAnswer.message}`}</div>
      ) : null}
    </section>
  );
}

// The permissions you gave: every rule and mode that lets chats on this
// computer go without asking, each to revoke (it asks first)
function Granted({ granted, host, reload }: { granted: WeekGrant[]; host: string; reload: () => void }) {
  const answer = useStore(weekRevokeAnswer);
  const [asking, setAsking] = useState("");
  const [busy, setBusy] = useState("");
  const [all, setAll] = useState(false);
  useEffect(() => {
    if (!answer) return;
    setBusy("");
    reload();
  }, [answer]);

  function revoke(g: WeekGrant) {
    setAsking("");
    setBusy(g.id);
    post({ type: "weekRevoke", id: g.id, host });
  }

  const shown = all ? granted : granted.slice(0, GRANTED_FEW);
  return (
    <section class="week-section">
      <h2>{host ? `Permissions you gave on ${host}` : "Permissions you gave"}</h2>
      <div class="small muted">{grantedIntro(granted, host)}</div>
      <ul class="week-rules">
        {shown.map((g) => (
          <li key={g.id} class="week-rule">
            <span class="week-rule-text">
              <span>{g.title}</span>
              <span class="small muted">{grantMeta(g)}</span>
              {asking === g.id ? (
                <span class="week-rule-ask">
                  <span class="small">This {g.change}.</span>
                  <span class="week-rule-buttons">
                    <button type="button" class="chip on" onClick={() => revoke(g)}>Revoke</button>
                    <button type="button" class="chip" onClick={() => setAsking("")}>Cancel</button>
                  </span>
                </span>
              ) : null}
            </span>
            {busy === g.id ? <span class="small muted">…</span>
              : g.shared ? (
                g.managed ? <span class="small muted" title="Set by your administrator (/etc/claude-code): it can't be changed here.">Administrator's</span>
                : <span class="small muted" title="This folder's .claude/settings.json is shared with whoever uses its repository: change it there.">In git</span>
              ) : asking !== g.id ? (
                <button type="button" class="chip" disabled={!!busy} onClick={() => setAsking(g.id)}>Revoke…</button>
              ) : null}
          </li>
        ))}
      </ul>
      {!all && granted.length > GRANTED_FEW ? (
        <button type="button" class="chip" onClick={() => setAll(true)}>Show {granted.length - GRANTED_FEW} more</button>
      ) : null}
      {answer?.message && answer.host === host ? (
        <div class={"small" + (answer.ok ? "" : " error")} role="status">{answer.ok ? answer.message : `Nothing changed: ${answer.message}`}</div>
      ) : null}
    </section>
  );
}
