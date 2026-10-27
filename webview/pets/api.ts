// The contract between the webview's UI (webview/components) and the pets
// (webview/pets). The UI owns the layout and the data; the pets own their
// stages: the hero (big Talpy with the headline and counts) and one stage per
// account (a pet per chat). Ported from the widget's PetStrip.qml + Pet.qml.

import type { ChatRef, Counts, Session } from "../../src/shared/types";

// A chat that may get a pet on its account's stage
export interface StageChat {
  key: string; // petKeyOf(session, host): stays the same while the chat's row changes
  session: Session;
  host?: string;
  order: number; // its place in the account's list: the pets stand in that order, left to right
}

export interface HeroInput {
  mood: string; // heroMood(): error | loading | needs | agents | working | idle | done
  headline: string; // headlineText()
  counts: Counts;
  highUsageText: string; // "work: 95% of the 5-hour limit!" or ""; said now and then
  contextFullText: string; // "fix parser: context 91% full" or ""; said now and then
  maxFiveHour: number; // highest 5-hour usage %: at 100 the hero is out of steam
  waiting: ChatRef | null; // the chat that needs you (clicking the headline opens it)
}

export interface AccountInput {
  chats: StageChat[]; // sorted: waiting, working, idle, done (then list order)
  maxPets: number; // talpyDen.chatPets
  fiveHour: number; // this account's 5-hour usage %, for alerts
}

export interface StageCallbacks {
  open(chat: ChatRef): void; // a pet, its name tag or the hero's headline was clicked
}

export interface PetStage {
  update(input: HeroInput | AccountInput): void;
  // Run the stage's animations only while shown (view visible, stage on screen)
  setLive(live: boolean): void;
  destroy(): void;
}

export interface PetsModule {
  createHeroStage(el: HTMLElement, cb: StageCallbacks): PetStage;
  createAccountStage(el: HTMLElement, stageKey: string, cb: StageCallbacks): PetStage;
  // A host or UI event: the chat's own pet reacts, else the hero ("" = hero)
  dispatchPetEvent(kind: string, text: string, key: string): void;
  // Color of a chat's pet (for the bar next to its row), or "" if it has none
  petColor(key: string): string;
  // Called whenever pet colors change, so rows can repaint
  onColorsChanged(listener: () => void): () => void;
  // Light or dark theme changed (body.vscode-dark / vscode-light / vscode-high-contrast)
  setDark(dark: boolean): void;
}
