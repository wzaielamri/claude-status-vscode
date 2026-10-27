import type { ViewState } from "../../src/shared/types";
import { post } from "../env";
import { IconButton } from "./IconButton";

// What went wrong (or right) outside the list: the status itself, the last
// action, the platform; and a reminder when the data is made up.
export function Messages({ state }: { state: ViewState }) {
  return (
    <>
      {state.demo ? (
        <div class="msg banner" role="note">
          Demo data — run “Talpy Den: Toggle Demo Data” to go back
        </div>
      ) : null}
      {state.errorText ? (
        <div class="msg error" role="alert">
          {state.errorText}
        </div>
      ) : null}
      {state.actionError ? (
        <div class="msg error" role="alert">
          {state.actionError}
        </div>
      ) : null}
      {state.actionInfo ? (
        <div class="msg info" role="status">
          {state.actionInfo}
        </div>
      ) : null}
      {state.platformNote ? <div class="msg muted">{state.platformNote}</div> : null}
      {state.digest ? (
        // What happened while you were away (away.ts)
        <div class="msg digest" role="status">
          <div class="digest-head">
            <strong class="digest-title">{state.digest.title}</strong>
            <IconButton icon="close" label="Close" title="Close this summary" onClick={() => post({ type: "dismissDigest" })} />
          </div>
          {state.digest.lines.map((line, i) => (
            <div key={i} class="digest-line">
              {line}
            </div>
          ))}
          {state.digest.first ? (
            <button type="button" class="link" onClick={() => post({ type: "open", chat: state.digest!.first! })}>
              Open {state.digest.first.name}
            </button>
          ) : null}
        </div>
      ) : null}
    </>
  );
}
