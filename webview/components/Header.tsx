import { summaryText, tooltipLines } from "../../src/shared/model";
import type { Counts, Group, HostState, Overall } from "../../src/shared/types";
import { editorTab, post } from "../env";
import { useViewVisible } from "../util/petStage";
import { useStore } from "../util/store";
import { setFocus, setTab, uiState } from "../util/ui";
import { IconButton } from "./IconButton";
import { TalpyIcon } from "./TalpyIcon";

// The icon and the summary. In the sidebar the view's title bar has the
// buttons; opened as an editor tab there's no title bar, so they're here.
// With the big Talpy shown the summary is left out: its headline says it.
export function Header(props: {
  overall: Overall;
  counts: Counts;
  groups: Group[];
  hosts: HostState[];
  answered: boolean; // this computer's status came, or a host's
  unavailable: boolean; // no status, and the reason is known
  heroShown: boolean;
}) {
  const visible = useViewVisible();
  const { tab, focus } = useStore(uiState);
  const { overall, counts, groups, hosts } = props;
  // "No sessions running" only once something answered; until then the list
  // says it's looking
  const summary = props.answered ? summaryText(counts) : props.unavailable ? "Status unavailable" : "";

  function newChat() {
    // one account: no need to ask which; else the extension asks
    const only = groups.length === 1 ? groups[0] : null;
    if (only?.host) post({ type: "newChat", groupId: only.id, host: only.host });
    else post({ type: "newChat", groupId: only ? only.id : "" });
  }

  return (
    <header class="header">
      <TalpyIcon state={overall} moving={visible} />
      {/* The app's name is the first tab: your chats now. The others: your last 7 days with Claude (WeekView.tsx),
          the permissions you gave (PermsView.tsx), your projects (ProjectsView.tsx) */}
      <div class="tabs" role="tablist">
        {(["chats", "week", "perms", "projects"] as const).map((t) => (
          <button key={t} type="button" role="tab" aria-selected={tab === t} class={"tab-btn" + (tab === t ? " on" : "")}
            title={t === "chats" ? tooltipLines(groups, counts, hosts).join("\n") : undefined}
            onClick={() => setTab(t)}>
            {t === "chats" ? "Talpy Den" : t === "week" ? "Recap" : t === "perms" ? "Permissions" : "Projects"}
          </button>
        ))}
      </div>
      <span class="summary" title={tooltipLines(groups, counts, hosts).join("\n")}>
        {props.heroShown && tab === "chats" ? "" : summary}
      </span>
      {tab === "chats" ? (
        <IconButton
          icon="filter"
          label={focus ? "Show everything" : "Focus"}
          title={focus ? "Show everything: back to every account, chat, pet and usage bar."
            : "Focus: only what needs you, the chats that finished lately, and one line for each chat at work."}
          class={focus ? "on" : ""}
          onClick={() => setFocus(!focus)}
        />
      ) : null}
      {editorTab ? (
        <div class="header-actions">
          <IconButton icon="add" label="New chat…" onClick={newChat} />
          <IconButton
            icon="search"
            label="Find a chat…"
            title="Find a chat: every chat with all the words you type in its name, its folder or your prompts, here and on the SSH hosts shown."
            onClick={() => post({ type: "findChat" })}
          />
          <IconButton
            icon="server"
            label="SSH hosts…"
            title={
              hosts.length
                ? `SSH hosts: showing Claude on ${hosts.map((h) => h.name).join(", ")}. Click to pick hosts from ~/.ssh/config.`
                : "SSH hosts: show the Claude chats of your SSH hosts (from ~/.ssh/config) here too."
            }
            class={hosts.length ? "with-count" : ""}
            onClick={() => post({ type: "pickHosts" })}
          >
            {hosts.length ? <span class="count">{hosts.length}</span> : null}
          </IconButton>
          <IconButton icon="refresh" label="Refresh" onClick={() => post({ type: "refresh" })} />
        </div>
      ) : null}
    </header>
  );
}
