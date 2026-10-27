// One Talpy on a stage (stage.ts). Ported from the widget's Pet.qml; the
// creature itself (looks, moods, what it does) is the widget's talpy.mjs
// (vendor/talpy), drawn on a canvas. This is where it lives on the stage and
// what it does with you.
//
// The hero (key "") mirrors all your sessions and has a life of its own: it
// digs over to wherever your mouse is, has hobbies, naps and reacts to you.
// Click to pet it (many quick clicks tickle), double-click to make it dance,
// drag it out of its burrow and drop it.
//
// A chat's pet stands for one chat in the list: it acts that chat out (works,
// calls you, rests while it's idle, sleeps once it's done, looks sad if it
// failed), wears its name and a calm version of its color, pops up out of the
// ground when the chat shows up and digs away when it leaves the list, and
// opens the chat when clicked (drag it for fun: it digs back to its spot).

import type { ChatRef, Session } from "../../src/shared/types";
import { stateText } from "../../src/shared/model";
import { Films } from "../../vendor/talpy/films.mjs";
import { BOX, LILAC, Talpy, calmColors, drawHole, drawHoleSide } from "../../vendor/talpy/talpy.mjs";
import type { StageChat } from "./api";
import { PALETTE, textOn } from "./colors";
import { Timer, Tween, ease, reducedMotion } from "./motion";
import type { Stage } from "./stage";

// Kirigami's small and large spacing at gridUnit 16
export const SMALL = 4;
export const LARGE = 8;

function holidayGreeting(): string {
  const d = new Date(),
    m = d.getMonth() + 1,
    day = d.getDate();
  if ((m === 12 && day === 31) || (m === 1 && day === 1)) return "Happy New Year!";
  if (m === 12 && day >= 24 && day <= 26) return "Merry Christmas!";
  if (m === 10 && day === 31) return "Boo! Happy Halloween!";
  if (m === 2 && day === 14) return "Happy Valentine's Day!";
  return "";
}

// What the UI opens for a chat (the extension finds its terminal or tab)
export function chatRef(c: StageChat): ChatRef {
  const s = c.session;
  return {
    host: c.host || undefined,
    key: s.key,
    sessionId: s.sessionId,
    chatId: s.chatId,
    profileDir: s.profileDir,
    name: s.name,
    entrypoint: s.entrypoint,
    pid: s.pid,
    cwd: s.cwd,
    kind: s.kind,
    jobId: s.jobId,
    openCommand: s.openCommand,
    stopCommand: s.stopCommand,
    vscodeTerminal: s.vscodeTerminal,
  };
}

function div(className: string): HTMLDivElement {
  const el = document.createElement("div");
  el.className = className;
  return el;
}

export class Pet {
  readonly stage: Stage;
  readonly key: string; // the chat's key ("" for the hero)
  readonly lone: boolean;
  readonly colorIndex: number; // -1 for the hero
  chat: Session | null = null; // the chat's session, null once it left the list
  lastChat: StageChat | null = null; // the last one seen (opened when clicked)
  label = ""; // short name on its tag
  homeX: number;
  leaving = false;

  // Where it is: px = horizontal centre, lift = how high you hold it
  px: number;
  lift = 0;
  held = false;
  private arriving = false;
  private travelTo = -1; // where the next dig takes it (-1: it picks)
  private quickClicks = 0;
  private sleepyClicks = 0;
  private lastMood: string;
  private landing = false; // picked up, until it has landed and dug in: drawn live

  readonly talpy: Talpy;
  // Talpy on its view (a canvas), as a film (vendor/talpy/films.mjs): each
  // picture of a move drawn once on a sheet (a canvas off the page) and shown
  // again as the move goes round, what flies about drawn over it; the open
  // hole is drawn once on sheets of its own and copied into each picture
  private readonly film: Films;
  private readonly sheets: HTMLDivElement;
  private readonly view: HTMLCanvasElement;
  private readonly canvases: HTMLCanvasElement[] = [];
  private readonly holes: HTMLCanvasElement[] = [];
  // Where it's drawn: at px, but while you hold it it trails your hand on a
  // spring and swings with how fast you move it (and wobbles back after)
  private shownX: number;
  private shownV = 0;
  private swing = 0;
  private swingV = 0;
  private lastStep = 0;
  // The hole it was pulled out of: empty where it was while you hold it, under
  // the pet (the hole doesn't come along), filled in once the pet has landed
  private readonly leftHole: HTMLCanvasElement;
  private leftAt = 0; // its horizontal centre
  private leftOpen = 1; // 0 closed, 1 open
  private leftFlip = false; // (the burrow isn't quite the same on both sides)
  private leftDrawn = "";
  private readonly fill: Tween;
  private readonly tag: HTMLDivElement | null;
  private readonly grab: HTMLDivElement;
  private readonly drawn: Record<string, string> = {};

  private readonly stride: Tween;
  private readonly faller: Tween;
  private readonly lookTimer: Timer;
  private readonly tickleTimer: Timer;
  private readonly contextTimer: Timer;
  private readonly usageTimer: Timer;

  private pressedAt: { x: number; y: number } | null = null;
  private dragging = false;
  private lastClick = 0;
  private destroyed = false;

  constructor(stage: Stage, key: string, chat: StageChat | null, colorIndex: number, homeX: number) {
    this.stage = stage;
    this.key = key;
    this.lone = key === "";
    this.colorIndex = colorIndex;
    this.homeX = homeX;
    this.px = homeX;
    this.shownX = homeX;
    if (chat) this.takeChat(chat);

    const clock = stage.clock;
    this.talpy = new Talpy({
      colors: this.lone ? LILAC : calmColors(PALETTE[colorIndex]),
      theme: stage.theme(),
      travels: this.lone && !reducedMotion(), // (no digging about, then)
    });
    this.talpy.onTravel = (ms) => this.travel(ms);
    this.talpy.tick(clock.now());
    this.lastMood = this.mood;
    this.talpy.setMood(this.lastMood);

    this.stride = new Tween(stage, (v) => (this.px = v));
    this.faller = new Tween(stage, (v) => (this.lift = v));
    this.fill = new Tween(stage, (v) => (this.leftOpen = v));
    // The hero turns towards the mouse while it hovers the stage
    this.lookTimer = new Timer(clock, 350, () => {
      const x = this.stage.cursorX();
      if (x >= 0 && Math.abs(x - this.px) > this.stage.bodyWidth * 0.4) this.talpy.facingLeft = x < this.px;
    }, true);
    this.tickleTimer = new Timer(clock, 1500, () => {
      this.quickClicks = 0;
      this.sleepyClicks = 0;
    });
    // A working session's context window is nearly full: point it out now and
    // then (a chat's pet for its own chat, the hero for chats without a pet)
    this.contextTimer = new Timer(clock, 40000, () => {
      if (this.held || this.talpy.busy) return;
      this.talpy.react("contextFull");
      this.say(this.contextFullText);
    }, true, true);
    // Close to a usage limit: mind-blown now and then; at the limit out of steam
    this.usageTimer = new Timer(clock, 25000, () => {
      if (this.held || this.talpy.busy) return;
      const input = this.stage.heroInput;
      this.talpy.react((input?.maxFiveHour ?? 0) >= 100 ? "battery" : "mindblown");
      this.say(input?.highUsageText ?? "");
    }, true, true);

    // Drawing: Talpy on its sheets (with room around it for what it throws
    // and holds up), its name tag, and a grab area around its body
    // The hero: every frame, live until a new pose has settled, breathing at
    // rest; a chat's pet: 30 pictures a second, pictures in between poses,
    // still at rest; both: a move too slow for a picture every frame is drawn
    // live (as in the widget, where such moves stepped)
    this.film = new Films(
      this.talpy,
      this.lone
        ? { frameMs: 16.7, maxFrames: 32, sheets: 36, tweenMs: 0, restMs: 200, smooth: true, clockMs: 1000 / 60 }
        : { frameMs: 33.3, maxFrames: 32, sheets: 32, tweenMs: 200, tweens: 5, smooth: true, clockMs: 1000 / 60 },
    );
    this.sheets = div("cs-pet-sheets");
    this.view = document.createElement("canvas");
    this.view.className = "cs-pet-sheet";
    this.sheets.append(this.view);
    this.leftHole = document.createElement("canvas");
    this.leftHole.className = "cs-pet-hole";
    this.leftHole.hidden = true;
    this.grab = div("cs-pet-grab");
    this.grab.style.width = `${stage.bodyWidth + LARGE}px`;
    this.grab.style.height = `${stage.bodyHeight + LARGE}px`;
    this.tag = null;
    if (!this.lone) {
      this.tag = div("cs-pet-tag");
      this.tag.style.background = PALETTE[colorIndex];
      this.tag.style.color = textOn(PALETTE[colorIndex]);
      this.tag.addEventListener("click", () => this.clicked());
    }
    // (the hole first: under the pet)
    stage.petLayer.append(this.leftHole, this.sheets, ...(this.tag ? [this.tag] : []), this.grab);
    this.listen();
    this.describe();

    // The first time the hero's stage shows it's just there; a chat's pet
    // digs its way up
    if (!this.lone || stage.shownOnce()) this.arrive();
    this.changed();
  }

  // ------------------------------------------------------------ state

  get isMain(): boolean {
    return this.stage.hero; // shows the account-wide things (usage limits)
  }

  get ground(): number {
    return this.lone ? this.stage.loneGround : this.stage.chatGround;
  }

  // Talpy's moods: idle working agents needs done failed limit (error loading for the hero)
  get mood(): string {
    if (this.lone) return this.stage.heroInput?.mood || "loading";
    const c = this.chat;
    if (!c) return "idle";
    if (c.state === "waiting") return "needs";
    if (c.state === "working") return c.agents && c.agents.running > 0 ? "agents" : "working";
    if (c.stopped && !c.stopped.over) return "limit"; // a usage limit (or the login) stopped it
    if (c.state === "idle") return "idle"; // ready for your next message
    if (c.reason === "failed") return "failed";
    return "done"; // done: results to look at
  }

  private get contextFullText(): string {
    if (this.lone) return this.stage.heroInput?.contextFullText ?? "";
    const c = this.chat;
    return c && c.state === "working" && c.context !== null && c.context >= 85
      ? `${this.label}: context ${Math.round(c.context)}% full`
      : "";
  }

  // Where it can be: anywhere on the stage, its burrow (wider than its body)
  // staying on it; the hero walks over the headline
  private get reach(): number {
    return (this.stage.box * 27) / 64 + SMALL;
  }

  private get minX(): number {
    return this.reach;
  }

  private get maxX(): number {
    return Math.max(this.minX, this.stage.width - this.reach);
  }

  private clampX(x: number): number {
    return Math.max(this.minX, Math.min(this.maxX, x));
  }

  // The timers that only run in some states (QML `running:` bindings)
  private updateTimers(): void {
    const live = this.stage.live,
      mood = this.mood,
      working = mood === "working" || mood === "agents";
    this.lookTimer.setRunning(live && this.lone && this.stage.hovering && !this.held && mood !== "done");
    this.contextTimer.setRunning(live && this.contextFullText !== "" && working && !this.leaving);
    this.usageTimer.setRunning(
      live && this.isMain && (this.stage.heroInput?.highUsageText ?? "") !== "" && mood !== "done" && !this.leaving,
    );
  }

  changed(): void {
    if (this.destroyed) return;
    this.updateTimers();
    this.stage.invalidate();
  }

  // ------------------------------------------------------------ from the stage

  private takeChat(c: StageChat): void {
    this.chat = c.session;
    this.lastChat = c;
    this.label = (c.session.name || "").trim();
  }

  // The chat's new row (or null: it left the list)
  setChat(c: StageChat | null): void {
    if (c) this.takeChat(c);
    else this.chat = null;
    this.describe();
    this.checkMood();
    const leaving = !c;
    if (leaving !== this.leaving) {
      this.leaving = leaving;
      if (leaving && !this.held) this.goAway();
      else if (!leaving) this.arrive(); // its chat came back: it pops up again
    }
    this.changed();
  }

  // The hero's mood (or a chat's state) may have changed
  checkMood(): void {
    const mood = this.mood;
    if (mood === this.lastMood) return;
    this.talpy.setMood(mood);
    if (this.lastMood === "done" && mood !== "done" && !this.leaving) this.talpy.react("yawn"); // woken up by work
    this.lastMood = mood;
    this.changed();
  }

  setHome(x: number): void {
    if (x !== this.homeX) {
      this.homeX = x;
      if (this.held || this.leaving) {
        // stays: in your hand (put down, it digs back), or on its way out
      } else if (!this.stage.shownOnce() || this.arriving) {
        this.px = x; // not seen yet, or still underground: it's just there
      } else if (!this.lone) {
        this.digTo(x, true);
      }
    }
    // the stage got narrower: back inside it
    if (!this.held && this.px > this.maxX) this.digTo(this.maxX, true);
    this.changed();
  }

  // The stage was shown or hidden (offFor: ms it was hidden)
  liveChanged(offFor: number): void {
    this.changed();
    const mood = this.mood;
    if (!this.stage.live || !this.lone || mood === "done" || mood === "loading" || offFor < 1500) return;
    const holiday = holidayGreeting();
    if (holiday) {
      this.talpy.react("celebrate");
      this.say(holiday);
    } else if (Math.random() < 0.6) {
      this.talpy.react("wave");
      this.say("Hi!");
    }
  }

  themeChanged(): void {
    this.talpy.setTheme(this.stage.theme());
    this.film.reset(); // (the pictures are drawn again)
    this.changed();
  }

  // Tooltip: which chat this is, and what clicking does
  private describe(): void {
    const c = this.lastChat;
    const title = this.lone || !c
      ? ""
      : `${c.host ? c.host + ": " : ""}${c.session.name}\n${stateText(c.session, Date.now())}\nClick to open this chat`;
    if (this.drawn.title !== title) {
      this.drawn.title = title;
      this.grab.title = title;
      if (this.tag) this.tag.title = title;
    }
    if (this.tag && this.tag.textContent !== this.label) {
      this.tag.textContent = this.label;
      this.tag.hidden = this.label === "";
    }
  }

  // ------------------------------------------------------------ actions

  // It digs up out of the ground
  private arrive(): void {
    this.arriving = true;
    this.talpy.react("arrive", () => (this.arriving = false));
  }

  // Its chat left the list: it waves and digs away
  private goAway(): void {
    this.talpy.react("leave", (ok) => {
      if (ok && this.leaving && !this.destroyed) this.stage.removePet(this);
    });
  }

  // Digs over to x (it dives in, tunnels across, pops up there); quick: its
  // spot moved (a new chat makes room), so it hurries
  private digTo(x: number, quick = false): void {
    if (this.held || this.leaving || this.arriving) return;
    this.travelTo = this.clampX(x);
    if (Math.abs(this.travelTo - this.px) < this.stage.bodyWidth * 0.3) {
      this.travelTo = -1;
      return;
    }
    if (reducedMotion()) {
      this.px = this.travelTo; // no digging: it's just there
      this.travelTo = -1;
      this.changed();
      return;
    }
    this.talpy.react(quick ? "scoot" : "dig");
  }

  // It dug in: tunnel across to its new spot within ms
  private travel(ms: number): void {
    let to = this.travelTo;
    this.travelTo = -1;
    if (to < 0) {
      // its own idea: towards the mouse if it hovers the stage, else anywhere
      const cursor = this.stage.cursorX();
      to = cursor >= 0
        ? cursor + (Math.random() - 0.5) * this.stage.bodyWidth
        : this.minX + Math.random() * (this.maxX - this.minX);
    }
    to = this.clampX(to);
    this.talpy.facingLeft = to < this.px;
    this.stride.run(this.px, to, Math.max(200, ms - 100), ease.inOutSine);
    this.changed();
  }

  say(text: string): void {
    this.stage.say(this, text);
  }

  // Clicked: the hero gets petted, a chat's pet opens its chat
  private clicked(): void {
    if (this.lone) {
      this.petIt();
      return;
    }
    if (this.lastChat) this.stage.open(chatRef(this.lastChat));
  }

  private petIt(): void {
    if (this.mood === "done") {
      this.sleepyClicks += 1;
      this.tickleTimer.restart();
      if (this.sleepyClicks >= 3) {
        this.sleepyClicks = 0;
        this.talpy.react("angry");
        this.say("I said let me SLEEP!");
      } else {
        this.talpy.react("grumpy");
        this.say("Zzz… let me sleep.");
      }
      return;
    }
    this.quickClicks += 1;
    this.tickleTimer.restart();
    if (this.quickClicks >= 4) {
      this.quickClicks = 0;
      this.talpy.react("laugh");
      this.say("Hehe, that tickles!");
    } else if (this.quickClicks === 3) {
      this.talpy.react("shy");
      this.say("Aww, stop it…");
    } else {
      const r = Math.random();
      this.talpy.react(r < 0.2 ? "love" : r < 0.35 ? "wink" : r < 0.45 ? "blowkiss" : "pet");
      this.say(this.petLine());
    }
  }

  private dance(): void {
    if (this.mood === "done") {
      this.talpy.react("yawn");
      this.say("…five more minutes.");
      return;
    }
    this.talpy.react(Math.random() < 0.4 ? "boogie" : "dance");
    this.say("♪ ♫");
  }

  private pickUp(): void {
    this.held = true; // first: nothing else moves it while you hold it
    this.landing = true;
    this.stride.stop();
    this.faller.stop(); // grabbed again while falling
    // (its hole doesn't come along: it stays where it was, until it has landed)
    const open = this.talpy.setHeld(true);
    if (open > 0) this.leaveHole(open);
    this.say("Whoa!");
    this.changed();
  }

  private leaveHole(open: number): void {
    this.fill.stop();
    this.leftAt = this.px;
    this.leftFlip = this.talpy.facingLeft;
    this.leftOpen = open;
    this.leftHole.hidden = false;
  }

  private drop(): void {
    const from = this.lift;
    this.faller.run(from, 0, Math.max(160, Math.sqrt(from / this.stage.gu) * 230), ease.inQuad, () => this.landed(from));
    this.changed();
  }

  // On the floor again: dizzy if it fell from high up, then it digs in where it landed
  private landed(fromHeight: number): void {
    this.held = false;
    // (the one it left is filled in; it digs a new one where it is)
    if (!this.leftHole.hidden) this.fill.run(this.leftOpen, 0, 350, ease.linear, () => (this.leftHole.hidden = true));
    const feel = this.talpy.land(fromHeight / (this.stage.box / 64), () => {
      this.landing = false;
      if (this.destroyed) return;
      if (this.leaving) this.goAway(); // its chat left while you held it: now it goes
      else if (!this.lone && Math.abs(this.px - this.homeX) > this.stage.bodyWidth * 0.5) this.digTo(this.homeX);
    });
    // (in the words for how it looks)
    if (feel === "sick") this.say("Ugh… I don't feel so good.");
    else if (feel === "dizzy") this.say("Ouch… the room is spinning.");
    this.changed();
  }

  private petLine(): string {
    const mood = this.mood;
    const waiting = this.stage.heroInput?.waiting?.name ?? "";
    if (mood === "needs" && waiting) return `${waiting} needs you!`;
    if (mood === "agents") return "Juggling subagents!";
    if (mood === "working") return "Busy coding… thanks!";
    const lines = ["Hi!", "♥", "Ship it!", "All quiet here.", "Pet me more!"];
    return lines[Math.floor(Math.random() * lines.length)];
  }

  // Something happened to its chat (or, for the hero, to an account)
  event(kind: string, text: string): void {
    if (this.held || this.leaving) return; // (a reaction now would stop it digging away)
    const action = ({ done: Math.random() < 0.4 ? "yay" : "finished", failed: "failed", stopped: "shrug", limit: "battery", oops: "confused", reset: "charged" } as
      Record<string, string>)[kind];
    this.talpy.react(action || "alert");
    this.say(text);
  }

  // ------------------------------------------------------------ input

  private listen(): void {
    const g = this.grab;
    g.addEventListener("pointerenter", () => this.describe()); // its "Working · 3m" as of now
    g.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault(); // no text selection or native drag
      g.setPointerCapture(e.pointerId);
      this.pressedAt = this.stage.local(e);
      this.dragging = false;
    });
    g.addEventListener("pointermove", (e) => {
      if (!this.pressedAt) return;
      const p = this.stage.local(e);
      if (!this.dragging && Math.hypot(p.x - this.pressedAt.x, p.y - this.pressedAt.y) > this.stage.gu * 0.5) {
        this.dragging = true;
        this.pickUp();
      }
      if (this.dragging) {
        const ground = this.ground,
          bh = this.stage.bodyHeight;
        this.px = this.clampX(p.x);
        this.lift = Math.max(0, Math.min(ground - bh, ground - p.y - bh / 2));
        this.changed();
      }
    });
    g.addEventListener("pointerup", () => {
      if (!this.pressedAt) return;
      this.pressedAt = null;
      if (this.dragging) {
        this.dragging = false;
        this.drop();
        return;
      }
      // Like Qt: a click, then a double-click instead of the second click
      const now = performance.now();
      const second = now - this.lastClick < 400;
      this.lastClick = second ? 0 : now;
      if (!second) this.clicked();
      else if (this.lone) this.dance();
    });
    const cancel = (): void => {
      if (!this.pressedAt) return;
      this.pressedAt = null;
      if (this.dragging) {
        this.dragging = false;
        this.drop();
      }
    };
    g.addEventListener("pointercancel", cancel);
    g.addEventListener("lostpointercapture", cancel);
  }

  // ------------------------------------------------------------ drawing

  private put(el: HTMLElement, name: string, prop: "transform" | "transformOrigin" | "cursor" | "maxWidth", value: string): void {
    if (this.drawn[name] === value) return;
    this.drawn[name] = value;
    el.style[prop] = value;
  }

  // Called from the stage's animation frame (and once while it's hidden)
  render(): void {
    const s = this.stage,
      box = s.box,
      ground = this.ground;
    const now = s.clock.now();
    this.spring(now);
    const px = Math.round(this.px * 10) / 10,
      shown = Math.round(this.shownX * 10) / 10,
      lift = Math.round(this.lift * 10) / 10,
      swing = Math.round(this.swing * 10) / 10;
    this.put(this.sheets, "origin", "transformOrigin", `${s.canvasW / 2}px ${s.floorY - (box * 27) / 64}px`);
    this.put(this.sheets, "sheets", "transform",
      `translate(${shown - s.canvasW / 2}px, ${ground - lift - s.floorY}px)${swing ? ` rotate(${swing}deg)` : ""}`);
    if (this.tag) {
      this.put(this.tag, "tag", "transform", `translate(${px}px, ${ground + (box * 6) / 64}px) translateX(-50%)`);
      this.put(this.tag, "tagWidth", "maxWidth", `${s.tagWidth}px`);
    }
    const gw = s.bodyWidth + LARGE,
      gh = s.bodyHeight + LARGE;
    this.put(this.grab, "grab", "transform", `translate(${px - gw / 2}px, ${ground - lift - gh}px)`);
    this.put(this.grab, "cursor", "cursor", this.held ? "grabbing" : this.lone ? "grab" : "pointer");
    if (!this.leftHole.hidden) {
      this.put(this.leftHole, "hole", "transform", `translate(${Math.round(this.leftAt * 10) / 10 - s.canvasW / 2}px, ${ground - s.floorY}px)`);
      this.paintHole();
    }

    this.talpy.tick(now);
    this.draw(now);
  }

  // A damped spring for where it's drawn and how it swings while you hold it
  // (a QML SpringAnimation in the widget); at rest it's simply where it is
  private spring(now: number): void {
    const dt = Math.min(0.05, Math.max(0, (now - (this.lastStep || now)) / 1000));
    this.lastStep = now;
    const unit = this.stage.box / 64;
    const moving = this.held || Math.abs(this.shownV) > 0.5 || Math.abs(this.px - this.shownX) > 0.2
      || Math.abs(this.swing) > 0.2 || Math.abs(this.swingV) > 0.5;
    if (!moving || reducedMotion()) {
      this.shownX = this.px;
      this.shownV = this.swing = this.swingV = 0;
      return;
    }
    // (stiffness and damping per second, about the widget's spring 5 / damping 0.32 and 3 / 0.16)
    if (this.held) {
      this.shownV += (140 * (this.px - this.shownX) - 8.3 * this.shownV) * dt;
      this.shownX += this.shownV * dt;
    } else {
      this.shownX = this.px;
      this.shownV = 0;
    }
    const target = this.held ? Math.max(-30, Math.min(30, ((this.px - this.shownX) * 1.8) / unit)) : 0;
    this.swingV += (90 * (target - this.swing) - 3 * this.swingV) * dt;
    this.swing += this.swingV * dt;
    this.stage.invalidate();
  }

  // As a film (vendor/talpy/films.mjs): a picture of a move drawn once and
  // shown again round after round, a new pose on its way through pictures in
  // between (the hero: live until it has settled), a big move live. Drawn
  // live while you hold it, until it has landed.
  private draw(now: number): void {
    const f = this.film;
    f.on = !this.landing && !reducedMotion();
    let changed = f.tick(now);
    if (this.paintSheets()) changed = true; // (drawn at once: shown at once)
    if (changed) this.compose();
  }

  // Draws the sheets that need it; true if there were any
  private paintSheets(): boolean {
    const s = this.stage,
      f = this.film,
      k = devicePixelRatio || 1,
      w = Math.round(s.canvasW * k),
      h = Math.round(s.canvasH * k);
    if (this.view.width !== w || this.view.height !== h) {
      // (also when the zoom changes: every picture is drawn again)
      for (const canvas of [this.view, ...this.canvases]) {
        canvas.width = w;
        canvas.height = h;
      }
      this.view.style.width = `${s.canvasW}px`;
      this.view.style.height = `${s.canvasH}px`;
      this.paintHoles(w, h);
      f.reset();
    }
    let painted = false;
    for (const sheet of f.sheets) {
      if (!sheet.paint) continue;
      if (!this.canvases[sheet.index]) {
        this.canvases[sheet.index] = document.createElement("canvas");
        this.canvases[sheet.index].width = w;
        this.canvases[sheet.index].height = h;
      }
      const ctx = this.begin(this.canvases[sheet.index]);
      if (!ctx) continue;
      f.paint(sheet, ctx);
      painted = true;
    }
    return painted;
  }

  // A sheet cleared, its context at the box's transform
  private begin(canvas: HTMLCanvasElement): CanvasRenderingContext2D | null {
    const box = this.stage.box,
      k = devicePixelRatio || 1;
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = "source-over";
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.setTransform((k * box) / 64, 0, 0, (k * box) / 64, k * box * 0.3, k * box * 0.45);
    return ctx;
  }

  // The open hole's back and front, turned right and left: drawn once at
  // this size, copied into each picture pixel for pixel
  private paintHoles(w: number, h: number): void {
    for (let i = 0; i < 4; i++) {
      const c = this.holes[i] ?? (this.holes[i] = document.createElement("canvas"));
      c.width = w;
      c.height = h;
      const ctx = this.begin(c);
      if (!ctx) {
        this.talpy.holeImage = null;
        return;
      }
      drawHoleSide(ctx, i % 2 ? "front" : "back", i >= 2);
    }
    this.talpy.holeImage = (part, left) => this.holes[(left ? 2 : 0) + (part === "front" ? 1 : 0)];
  }

  // The empty hole it left, when it has changed (or the zoom has)
  private paintHole(): void {
    const s = this.stage,
      c = this.leftHole,
      box = s.box,
      k = devicePixelRatio || 1,
      w = Math.round(s.canvasW * k),
      h = Math.round(s.canvasH * k);
    const state = `${w}x${h},${box},${this.leftOpen},${this.leftFlip}`;
    if (state === this.leftDrawn) return;
    this.leftDrawn = state;
    if (c.width !== w || c.height !== h) {
      c.width = w;
      c.height = h;
      c.style.width = `${s.canvasW}px`;
      c.style.height = `${s.canvasH}px`;
    }
    const ctx = c.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.setTransform((k * box) / 64, 0, 0, (k * box) / 64, k * box * 0.3, k * box * 0.45);
    if (this.leftFlip) {
      ctx.translate(BOX, 0);
      ctx.scale(-1, 1);
    }
    drawHole(ctx, this.leftOpen);
  }

  // The view: the sheet in front, and what flies about over it
  private compose(): void {
    const f = this.film,
      front = this.canvases[f.front.index];
    const ctx = this.view.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this.view.width, this.view.height);
    if (front) ctx.drawImage(front, 0, 0);
    if (f.fx.on) {
      const box = this.stage.box,
        k = devicePixelRatio || 1;
      ctx.setTransform((k * box) / 64, 0, 0, (k * box) / 64, k * box * 0.3, k * box * 0.45);
      f.paintFx(ctx);
    } else {
      f.fx.paint = false;
    }
  }

  destroy(): void {
    this.destroyed = true;
    for (const t of [this.stride, this.faller, this.fill]) t.stop();
    for (const t of [this.lookTimer, this.tickleTimer, this.contextTimer, this.usageTimer]) t.stop();
    for (const el of [this.leftHole, this.sheets, this.tag, this.grab]) el?.remove();
  }
}
