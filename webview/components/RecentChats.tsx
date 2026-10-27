import {
  ago,
  clockText,
  goOnAt,
  claudeCodeHere,
  claudeCodeReopenHint,
  cliInstalled,
  keptChat,
  keptTitle,
  machineStatus,
  queuedChat,
  reopensInClaudeCode,
  stopText,
  terminalReopenHint,
} from "../../src/shared/model";
import { reviewPending, testsText } from "../../src/shared/pane";
import type { Group, RecentChat, ReopenIn } from "../../src/shared/types";
import { post } from "../env";
import { canShareRecent, groupKeyOf, recentRef, shareTitle } from "../util/chat";
import { Icon } from "../util/icons";
import { useStore } from "../util/store";
import { toggleRecent, toggleStarred, uiState } from "../util/ui";
import { useView } from "./context";
import { Elided } from "./Elided";
import { IconButton } from "./IconButton";
import { Tag } from "./Tag";

// The account's latest other chats, to pick one up again. Folded by default:
// the running chats come first. A switch above them says where they reopen:
// in a terminal, or in a Claude Code tab (kept in talpyDen.reopenIn). A
// half is greyed out where it can't work: no claude command on the chat's
// machine, or Claude Code not here or on another account. The chats you
// starred come first, under their own fold (★ Starred), however old.
export function RecentChats({ group }: { group: Group }) {
  const view = useView();
  const ui = useStore(uiState);
  const key = groupKeyOf(group);
  const items = (group.recent ?? []).slice(0, Math.max(0, view.settings.recentChats));
  const starred = group.starred ?? [];
  if (!items.length && !starred.length) return null;
  const open = !!ui.recent[key];
  const starredOpen = !!ui.starred[`${key}|chats`];
  const status = machineStatus(view, group.host);
  const share = canShareRecent(status);
  const cliHere = cliInstalled(status);
  const codeHere = claudeCodeHere(group, view.claudeCode);
  const inCode = reopensInClaudeCode(view.settings.reopenIn, cliHere, codeHere);
  const groups = view.status?.groups ?? [];
  const terminalHint = terminalReopenHint(group.host, cliHere);
  const codeHint = claudeCodeReopenHint(group, view.claudeCode, groups);
  const canOpen = inCode || cliHere;
  const reopen = !canOpen ? `${terminalHint}\n${codeHint}` : inCode ? codeHint : terminalHint;

  const half = (where: ReopenIn, text: string, available: boolean, checked: boolean, title: string) => (
    <button
      type="button"
      class={"tag open-in-half" + (available ? " clickable" : " unavailable")}
      aria-pressed={checked}
      aria-disabled={!available}
      title={title}
      onClick={() => available && post({ type: "setReopenIn", where })}
    >
      {text}
    </button>
  );

  return (
    <div class="recent">
      <button
        type="button"
        class="recent-toggle small"
        aria-expanded={open}
        title="This account's latest chats that aren't listed above, and the ones you starred. Click one to reopen it, in a terminal or in a Claude Code tab (the switch at the top of the list)."
        onClick={() => toggleRecent(key)}
      >
        <Icon name="chevron" class={open ? "open" : ""} />
        Recent chats ({items.length})
      </button>
      {open ? (
        <>
          <div class="open-in small" role="group" aria-label="Where recent chats reopen">
            <span class="muted">Open in:</span>
            {half("terminal", "Terminal", cliHere, cliHere && !inCode, (cliHere ? "Reopen chats in a terminal\n" : "No claude command\n") + terminalHint)}
            {half("claudeCode", "Claude Code tab", codeHere, inCode, (codeHere ? "Reopen chats in a Claude Code tab\n" : "Not here\n") + codeHint)}
          </div>
          {!cliHere && !codeHere ? <div class="open-in-why small muted">{`${terminalHint} ${codeHint}`}</div> : null}
          {starred.length ? (
            <button
              type="button"
              class="recent-toggle starred-toggle small"
              aria-expanded={starredOpen}
              title="The chats of this account you starred (☆ next to a chat), newest first, however old they are; not the ones listed above. Click a chat's ★ to unstar it."
              onClick={() => toggleStarred(`${key}|chats`)}
            >
              <Icon name="chevron" class={starredOpen ? "open" : ""} />★ Starred ({starred.length})
            </button>
          ) : null}
          {starredOpen && starred.length ? <ul class="recent-list starred-list">{starred.map((c) => line(c))}</ul> : null}
          <ul class="recent-list">{items.map((c) => line(c))}</ul>
        </>
      ) : null}
    </div>
  );

  function line(c: RecentChat) {
    const chat = recentRef(c, group.host);
    const goOn = goOnAt(c);
    const goingOn = queuedChat(view.status, c.sessionId, group.host);
    const kept = group.host ? null : keptChat(view.status, c.sessionId);
    const later = group.host ? "" : " Once: ↻ beside it goes on after later limits too, until it's done.";
    const canKeep = !group.host && !kept && !goingOn && goOn > 0;
    // a click opens its tab (else the chat itself, as the switch above says)
    const paneFirst = view.settings.clickOpensPane !== false;
    const reopenIt = () => post(inCode ? { type: "openInClaudeCode", chat } : { type: "openWindow", chat });
    const toReview = reviewPending({ ...c, chatId: c.sessionId, state: "done" }, view.status, group.host);
    const tests = testsText(c.review);
    return (
      <li key={c.key} class={"recent-item" + (canOpen ? "" : " unavailable")}>
        <button
          type="button"
          class="link recent-name"
          aria-disabled={!canOpen}
          title={
            `${c.name}\n${reopen}` +
            (c.cwd ? `\nFolder: ${group.host ? `${group.host}:` : ""}${c.cwd}` : "") +
            (c.shared ? `\nShared between accounts; last used on ${c.profile}.` : "") +
            (c.stopped ? `\n\n${stopText(c.stopped)}` : "") +
            (c.recap?.text ? `\n\nRecap: ${c.recap.text}` : "")
          }
          onClick={() => (paneFirst ? post({ type: "openPane", chat }) : canOpen && reopenIt())}
        >
          {c.name}
        </button>
        {toReview ? (
          <Tag
            text="to review"
            active
            title="It changed files. Click to see what it changed, against how the files were before the chat: undo it, commit only its files, or mark it reviewed."
            onClick={() => post({ type: "openPane", chat, tab: "review" })}
          />
        ) : null}
        {toReview && tests ? <Tag text={tests.text} title={tests.title} active={tests.ok === true} warning={tests.ok === false} /> : null}
        {c.shared ? (
          <span class="muted" title="Shared between accounts">
            ⇄
          </span>
        ) : null}
        {goingOn ? (
          <Tag
            text={`⏱ ${clockText(goingOn.startAt)}`}
            active
            title={`Goes on by itself at ${clockText(goingOn.startAt)}, in the background, in the permission mode it had, telling Claude that the limit has reset (if it didn't go on before then).\n\nClick to cancel it.`}
            onClick={() => post({ type: "unqueue", id: goingOn.id })}
          />
        ) : c.stopped?.kind === "limit" && !c.stopped.over ? (
          <Tag
            text="limit"
            warning
            title={`A usage limit stopped it\n${c.stopped.text}` + (goOn ? `\n\nClick to have it go on by itself in the background at ${clockText(goOn)}, a minute after the limit resets.${later}` : "")}
            onClick={goOn ? () => post({ type: "queueContinue", chat }) : undefined}
          />
        ) : null}
        {kept ? (
          <Tag
            text="↻"
            active={!kept.error}
            warning={!!kept.error || !!kept.note}
            title={keptTitle(kept)}
            onClick={() => post({ type: "keepGoing", chat, on: false })}
          />
        ) : canKeep ? (
          <Tag
            text="↻"
            title={`Keep going until it's done\nClick to have it go on by itself in the background at ${clockText(goOn)}, a minute after the limit resets, in the permission mode it had, and again after each later limit that stops it, until a turn of it ends by itself.`}
            onClick={() => post({ type: "queueContinue", chat, keep: true })}
          />
        ) : null}
        {c.starred ? (
          <button type="button" class="star-mark" title="Starred: it stays listed under Starred, however old it is. Click to unstar it." onClick={() => post({ type: "star", chat, on: false })}>
            ★
          </button>
        ) : null}
        <Elided text={c.cwdShort || "~"} class="recent-folder small muted" title={c.cwd} />
        <span class="small muted nowrap">· {ago(view.now - c.lastActive)}</span>
        <div class="row-actions">
          {paneFirst && canOpen ? (
            <IconButton icon="external" label="Reopen this chat" title={reopen} onClick={reopenIt} />
          ) : null}
          {c.starred ? null : (
            <IconButton
              icon="star"
              label="Star"
              title="Star this chat: it stays listed under Recent chats → Starred, however old it is."
              onClick={() => post({ type: "star", chat, on: true })}
            />
          )}
          {share ? (
            <IconButton
              icon="share"
              label="Continue on another account…"
              title={shareTitle(group.host)}
              onClick={() => post({ type: "share", chat })}
            />
          ) : null}
        </div>
      </li>
    );
  }
}
