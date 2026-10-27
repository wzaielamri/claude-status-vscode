// The status bar item: the panel icon of the widget. A status bar can only
// show text and icons, so the count of chats that need you goes in the text
// (with the warning background), the 5-hour usage after it: of the account
// VS Code's Claude runs on, when it has numbers.

import * as vscode from "vscode";
import { settings } from "./config";
import { allGroups, claudeCodeGroup, countStates, overallState, panelWindow, tooltipLines } from "./shared/model";
import type { ViewState } from "./shared/types";

export class StatusBar implements vscode.Disposable {
  private item = vscode.window.createStatusBarItem("talpyDen.status", vscode.StatusBarAlignment.Right, 100);

  constructor() {
    this.item.name = "Talpy Den";
    this.item.command = "talpyDen.view.focus";
    this.item.text = "$(sparkle)";
    this.item.show();
  }

  dispose(): void {
    this.item.dispose();
  }

  update(state: ViewState): void {
    const groups = allGroups(state.status, state.hosts);
    const counts = countStates(groups);
    const overall = overallState(groups, counts);
    const parts: string[] = [];
    if (overall === "waiting") parts.push(`$(bell-dot) ${counts.waiting}`);
    else if (overall === "working") parts.push(`$(loading~spin) ${counts.working}`);
    else parts.push("$(sparkle)");
    // (the account VS Code's Claude runs on first: its chats are the ones here)
    const usage = settings.showUsageInStatusBar() ? panelWindow(groups, claudeCodeGroup(groups, state.hosts, state.claudeCode)?.id) : null;
    if (usage) parts.push(`${Math.round(usage.window.pct)}%`);
    this.item.text = parts.join(" ");
    this.item.backgroundColor = overall === "waiting" ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
    this.item.color =
      overall !== "waiting" && usage && usage.window.pct >= 90 ? new vscode.ThemeColor("errorForeground") : undefined;
    const tip = new vscode.MarkdownString(undefined, true);
    tip.appendMarkdown("**Talpy Den**" + (state.demo ? " (demo data)" : "") + "\n\n");
    const lines = !state.status && state.errorText ? [state.errorText] : tooltipLines(groups, counts, state.hosts);
    for (const line of lines) tip.appendText(line + "\n\n");
    this.item.tooltip = tip;
    this.item.accessibilityInformation = { label: `Talpy Den: ${lines[0] ?? ""}` };
  }
}
