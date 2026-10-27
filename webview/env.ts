// What the extension hands the webview, and the way back to it.

import type { WebviewMessage } from "../src/shared/types";

interface VsCodeApi {
  postMessage(message: unknown): void;
  getState(): unknown;
  setState(state: unknown): void;
}

declare global {
  interface Window {
    TALPY_DEN: { editorTab: boolean };
  }
  function acquireVsCodeApi(): VsCodeApi;
}

const api = acquireVsCodeApi();

export function post(message: WebviewMessage): void {
  api.postMessage(message);
}

export const editorTab: boolean = window.TALPY_DEN.editorTab;

export function isDark(): boolean {
  return document.body.classList.contains("vscode-dark") || document.body.classList.contains("vscode-high-contrast")
    && !document.body.classList.contains("vscode-high-contrast-light");
}

// Remembered across reloads of the webview (expanded sections and the like)
export function savedState<T extends object>(): Partial<T> {
  return (api.getState() as Partial<T>) ?? {};
}

export function saveState<T extends object>(state: T): void {
  api.setState(state);
}
