// Talpy: the pet that lives in a burrow. One creature for the Plasma widget,
// the GNOME extension and the VS Code extension: each host calls tick(now)
// and draw(ctx) on its own 2D canvas (QML Canvas, Cairo through an adapter,
// an HTML canvas) and moves it around its stage.
//
//   const t = new Talpy({ colors, words: s => i18n(s), travels: true });
//   t.setMood('working');          // idle working agents needs done failed limit error loading
//   t.react('pet');                // an action now (see actions.mjs)
//   t.onTravel = ms => { ... };    // it dug in: move it to its new spot within ms
//   every frame: t.tick(Date.now()); then draw(ctx) in a 64 × 64 box, floor at y = 57
//
// While you hold it (setHeld) nothing else moves it; land() drops it, and it
// digs back in where it landed.
//
// Plain ES2017 (see paths.mjs).

import * as art from './art.mjs';
import { ACTIONS, MOODS, dirt, random } from './actions.mjs';

export const BOX = 64;
export const FLOOR = 57;     // where the stage's floor is, in the box
export const TOP = 2;        // the lamp's top (highest pose), for room above
export const LILAC = art.LILAC;
export const calmColors = art.calmColors;

const KEYS = ['eyes', 'mouth', 'arms', 'plume', 'body', 'prop', 'tail', 'blush', 'think', 'orbit', 'walk', 'held', 'sip',
              'fail', 'depth', 'tunnel', 'hole', 'lamp', 'gum', 'bite'];
const DEPTH = { out: -0.5, high: 6, half: 10, low: 13, peek: 19, hidden: 42, gone: 74 };
const ARMS = { rest: [16, -16], up: [150, -150], hold: [16, 35], sip: [16, 62], both: [-50, 50], chin: [16, 60], scratch: [16, -165],
               ear: [16, 153], scope: [16, -164], sign: [16, -150], balloon: [150, -16], cover: [-101, 101], juggle: [-20, 20],
               type: [-38, 38], held: [60, -60], wave: [16, -145], waveboth: [145, -145], dance: [90, -90], flail: [70, -70], throw: [16, -140],
               selfie: [16, -152] };
const BLINKING = { open: 1, wide: 1, sad: 1, tired: 1, wink: 1 };
// How often a picture is worth drawing (frameMs): quick moves want 25 a
// second, slow ones 12; at rest (breathing, the lamp's glow) 5 show it all.
// What's eased towards where it's going, and how much it moves in a tick
// (40 ms) to count as a quick move (a twentieth of it: a slow one). Sleepy
// z's float up slowly enough for 5, and a sleepy slump too.
const EASED = { depth: 0.3, hole: 0.02, armL: 1.5, armR: 1.5, armLy: 0.1, armRy: 0.1, plume: 3, lx: 0.08, ly: 0.08, sip: 0.05 };
const QUICK_BODY = { hop: 1, jump: 1, bounce: 1, wiggle: 1, shake: 1, spin: 1, jolt: 1, shiver: 1, tumble: 1, land: 1, bob: 1 };
const RESTING_BODY = { slump: 1 };   // (as slow as breathing)
const QUICK_PROPS = { phone: 1, juggle: 1 };
const SLOW_PROPS = { gum: 1, magnifier: 1 };

// ---------------------------------------------------------------- easing

const clamp01 = x => Math.max(0, Math.min(1, x));
const lerp = (a, b, p) => a + (b - a) * p;
const sine = p => 0.5 - 0.5 * Math.cos(Math.PI * p);           // ease in-out
const alt = (ms, period) => sine((ms / period) % 2 > 1 ? 2 - (ms / period) % 2 : (ms / period) % 2);
const outBack = p => {
    const c1 = 1.70158, c3 = c1 + 1;
    return 1 + c3 * Math.pow(p - 1, 3) + c1 * Math.pow(p - 1, 2);
};
// value at p along [[pos, value], ...] (eased between frames)
function kf(p, frames) {
    if (p <= frames[0][0])
        return frames[0][1];
    for (let i = 1; i < frames.length; i++) {
        if (p <= frames[i][0]) {
            const a = frames[i - 1], b = frames[i];
            return lerp(a[1], b[1], sine((p - a[0]) / Math.max(1e-6, b[0] - a[0])));
        }
    }
    return frames[frames.length - 1][1];
}
const ease = (kind, p) => kind === 'in' ? p * p : kind === 'linear' ? p : 1 - (1 - p) * (1 - p);

// ---------------------------------------------------------------- body moves

// What a body move does at ms into it: whole (x, y, rotation about an
// origin) and torso (scale, rotation about its base). null: it's over.
function bodyMove(name, ms, it) {
    const once = (T, n) => ms < T * (n || 1) ? (ms % T) / T : null;
    const m = { wx: 0, wy: 0, wr: 0, ox: 32, oy: 57, sx: 1, sy: 1, tr: 0, ty: 0, torso: false };
    let p;
    switch (name) {
    case 'hop': case 'jump':
        p = once(name === 'hop' ? 550 : 800, it);
        if (p === null)
            return null;
        m.wy = kf(p, [[0, 0], [0.45, name === 'hop' ? -8 : -14], [0.8, 0], [1, 0]]);
        m.sx = kf(p, [[0, 1.06], [0.2, 0.95], [0.7, 1], [0.85, 1.07], [1, 1]]);
        m.sy = kf(p, [[0, 0.94], [0.2, 1.06], [0.7, 1], [0.85, 0.93], [1, 1]]);
        m.torso = true;
        return m;
    case 'bounce': m.wy = kf((ms % 420) / 420, [[0, 0], [0.5, -4], [1, 0]]); return m;
    case 'wiggle': m.wr = lerp(-6, 6, alt(ms, 180)); return m;
    case 'shake': m.wr = lerp(-6, 6, alt(ms, 90)); return m;
    case 'sway': m.wr = lerp(-10, 10, alt(ms, 500)); return m;
    case 'spin':
        p = once(1100);
        if (p === null)
            return null;
        if (ms < 700)
            m.wy = kf(ms / 700, [[0, 0], [0.45, -8], [0.8, 0], [1, 0]]);
        m.sx = kf(p, [[0, 1], [0.3, 1.04], [0.6, 0.98], [1, 1]]);
        m.sy = kf(p, [[0, 1], [0.3, 0.97], [0.6, 1.03], [1, 1]]);
        m.torso = true;
        m.turn = p;
        return m;
    case 'stretch':
        p = Math.min(1, ms / 1300);
        m.sx = kf(p, [[0, 1], [0.35, 0.92], [0.8, 0.92], [1, 1]]);
        m.sy = kf(p, [[0, 1], [0.35, 1.13], [0.8, 1.13], [1, 1]]);
        m.torso = true;
        return m;
    case 'inhale':
        p = sine(Math.min(1, ms / 450));
        m.sx = lerp(1, 0.95, p);
        m.sy = lerp(1, 1.08, p);
        m.torso = true;
        return m;
    case 'jolt':
        p = once(400);
        if (p === null)
            return null;
        m.sx = kf(p, [[0, 0.95], [0.25, 1.14], [1, 1]]);
        m.sy = kf(p, [[0, 1.08], [0.25, 0.85], [1, 1]]);
        m.wx = kf(p, [[0, 0], [0.25, -3], [1, 0]]);
        m.torso = true;
        return m;
    case 'shiver': m.wx = lerp(-0.7, 0.7, alt(ms, 70)); return m;
    case 'nod':
        p = (ms % 1700) / 1700;
        m.tr = kf(p, [[0, 0], [0.45, 0], [0.72, 7], [0.8, 1], [1, 0]]);
        m.ty = kf(p, [[0, 0], [0.45, 0], [0.72, 1], [0.8, 0], [1, 0]]);
        m.torso = true;
        return m;
    case 'slump':
        p = (ms % 3600) / 3600;
        m.sx = kf(p, [[0, 1.05], [0.5, 1.07], [1, 1.05]]);
        m.sy = kf(p, [[0, 0.92], [0.5, 0.9], [1, 0.92]]);
        m.wy = 1;
        m.torso = true;
        return m;
    case 'tumble':
        p = once(1000);
        if (p === null)
            return null;
        m.oy = 40;
        m.wy = kf(p, [[0, 0], [0.25, -12], [0.75, -6], [1, 0]]);
        m.wr = kf(p, [[0, 0], [0.25, 0], [0.75, 340], [1, 360]]);
        return m;
    case 'float':
        m.wy = lerp(-6, -12, alt(ms, 1400));
        m.wr = lerp(-3, 3, alt(ms, 1400));
        m.shadow = 0.6;
        return m;
    case 'trip':
        p = Math.min(1, ms / 1500);
        m.ox = 47;
        m.wr = kf(p, [[0, 0], [0.12, -8], [0.32, 82], [0.7, 82], [1, 0]]);
        return m;
    case 'land':
        p = once(400);
        if (p === null)
            return null;
        m.sx = lerp(1.14, 1, sine(p));
        m.sy = lerp(0.84, 1, sine(p));
        m.torso = true;
        return m;
    case 'bob':
        p = (ms % 460) / 460;
        m.wr = kf(p, [[0, -2.5], [0.5, 2.5], [1, -2.5]]);
        m.wy = kf(p, [[0, 0], [0.5, -1.8], [1, 0]]);
        return m;
    default:
        return null;
    }
}

// The arms' angles for poses that move by themselves (null: a still pose)
function armMove(name, ms) {
    switch (name) {
    case 'wave': return [16, lerp(-120, -170, alt(ms, 380)), 0, 0];
    case 'waveboth': return [lerp(120, 170, alt(ms + 170, 340)), lerp(-120, -170, alt(ms, 340)), 0, 0];
    case 'sign': return [16, lerp(-138, -160, alt(ms, 500)), 0, 0];
    case 'type': {
        const l = alt(ms, 130), r = alt(ms + 130, 130);
        return [lerp(-38, -31, l), lerp(38, 31, r), -1.4 * l, -1.4 * r];
    }
    case 'juggle': return [lerp(-20, -6, alt(ms, 375)), lerp(20, 6, alt(ms + 190, 375)), -2 * alt(ms, 375), -2 * alt(ms + 190, 375)];
    case 'dance': return [lerp(30, 145, alt(ms, 500)), lerp(-145, -30, alt(ms, 500)), 0, 0];
    case 'flail': return [lerp(50, 95, alt(ms, 180)), lerp(-50, -95, alt(ms, 180)), 0, 0];
    case 'scratch': return [16, lerp(-158, -172, alt(ms, 160)), 0, 0];
    case 'throw': return [16, kf(Math.min(1, ms / 550), [[0, -16], [0.35, 30], [0.75, -175], [1, -140]]), 0, 0];
    default: return null;
    }
}

function plumeAngle(name, ms, it) {
    switch (name) {
    case 'up': return -22;
    case 'droop': return 66;
    case 'bob': return kf((ms % 1100) / 1100, [[0, -9], [0.5, 11], [1, -9]]);
    case 'twirl': return ms < 800 * (it || 1) ? ((ms % 800) / 800) * 360 : 0;
    case 'flick': return ms < 400 ? kf(ms / 400, [[0, 0], [0.4, -45], [1, 0]]) : 0;
    case 'wild': return lerp(-24, 14, alt(ms, 160));
    default: return 0;
    }
}

// ---------------------------------------------------------------- stills

// A host may show a pet as pictures instead of drawing it every frame (each
// drawing costs; the widget does it for the chats' pets, the big one moves
// all the time; see pictures.mjs): stillKey() says what a picture of it shows
// now, drawStill() draws that picture, settled. A quick move goes round four
// pictures, a beat each: one end of its swing, halfway, the other end,
// halfway back (the beats below: ms from one end to the other). A beat is a
// quarter of the quickest move's round, in whole steps of BEAT_MS, so all
// pets change pictures on the same ticks (each costs a window). A big move
// has no picture: one that happens once (a jump, a spin), a long way in or out
// of the burrow, its hole opening or closing; stillKey() is '' until it's over
// and what flew off meanwhile has landed, and the host draws it live.
const BEAT_MS = 140;
const ARM_BEATS = { wave: 380, waveboth: 340, sign: 500, type: 130, juggle: 375, dance: 500, flail: 180, scratch: 160 };
const BODY_BEATS = { bounce: 210, bob: 230, wiggle: 180, shake: 90, sway: 500, shiver: 70, nod: 1200, float: 1400 };
const PLUME_BEATS = { bob: 550, wild: 160 };
const PROP_BEATS = { phone: 78, juggle: 250 };   // (its prop's own motion)
const TUNNEL_BEAT = 260;
const ONE_WAY = { waveboth: 1, juggle: 1, nod: 1, phone: 1 };   // (halfway back doesn't look like halfway there)
const ONCE = { hop: 550, jump: 800, spin: 1100, jolt: 400, tumble: 1000, land: 400 };   // (ms; a hop or a jump × it)
const BIG_DEPTH = 8;   // (from low to high is no big move, from half to peeking out is)
// A film (films.mjs) plays a quick move as it is, round after round: each of
// its pictures drawn once. What's eased between poses settles within
// SETTLE_MS (the arms in about four of their 70 ms); TWEENED is what moves
// between two pictures of a film when a pose changes.
// A move that happens once and isn't big (a stretch, a throw, a twirl of the
// lamp) has no pictures either: a film draws it live (ms it takes, × it).
const SETTLE_MS = 280;
const TWEENED = ['depth', 'armL', 'armR', 'armLy', 'armRy', 'plume', 'lx', 'ly', 'sip'];
const ONE_SHOT = { body: { stretch: 1300, inhale: 450, trip: 1500 }, arms: { throw: 550 }, plume: { flick: 400, twirl: 800 } };

// ---------------------------------------------------------------- the hole

// The hole as it opens or closes (hole 0 to 1.1, a little over at the end of
// opening), the back of it or the front: the creature is drawn between them
function holePart(ctx, hole, part) {
    const size = Math.min(1.1, hole);
    ctx.save();
    art.about(ctx, 32, 56, 0, lerp(0.05, 1, size), lerp(0.2, 1, size));
    ctx.globalAlpha = clamp01(hole);
    part(ctx);
    ctx.restore();
}

// An empty hole in the 64 × 64 box (hole as above), for what a host keeps
// where a creature you hold was pulled out of it (setHeld)
export function drawHole(ctx, hole) {
    holePart(ctx, hole, art.holeBack);
    holePart(ctx, hole, art.holeFront);
}

// The open hole's back or front ('back', 'front'), turned left or not, in
// the 64 × 64 box: a host may draw these once on sheets of their own and
// give them to Talpy (holeImage): half of what drawing it costs is its hole
export function drawHoleSide(ctx, part, left) {
    ctx.save();
    if (left) {
        ctx.translate(BOX, 0);
        ctx.scale(-1, 1);
    }
    holePart(ctx, 1, part === 'front' ? art.holeFront : art.holeBack);
    ctx.restore();
}

// ---------------------------------------------------------------- the creature

export class Talpy {
    constructor(opts) {
        opts = opts || {};
        this.colors = opts.colors || art.LILAC;
        this.theme = opts.theme || { fx: '#7d87a3', text: '#161a24' };
        this.words = opts.words || (s => s);
        this.travels = !!opts.travels;    // may dig over to another spot (the host moves it: onTravel)
        this.onTravel = null;
        this.facingLeft = false;
        // (part, left) => the open hole's side drawn by drawHoleSide on a sheet the
        // size of the canvas Talpy's drawn on, at the same transform; null: drawn
        this.holeImage = null;
        this.mood = 'idle';
        this.base = Object.assign({}, MOODS.idle.base);
        this.st = {};
        this.since = {};          // when each part last changed (ms)
        this.now = 0;
        this.last = 0;
        this.run = null;          // the action under way: {name, steps, i, until, done, auto}
        this.lastAuto = '';
        this.nextBrain = 0;
        this.brainOn = opts.brain !== false;
        this.held = false;
        this.heldAt = 0;
        this.frameMs = 40;        // see tick()
        this.stepAt = 0;          // when the pose shown began
        this.big = false;         // a big move is under way (see stills), since bigAt
        this.bigAt = 0;
        this.landsAt = 0;         // what flew off during it has landed by then
        this.away = false;        // it left (leave): it stays down there until it arrives again
        this.settled = false;     // what's eased between poses is where it's going (see films)
        this.easeAt = 0;          // when that last changed
        this.fx = [];
        this.v = { depth: DEPTH.half, depthFrom: DEPTH.half, depthTo: DEPTH.half, depthAt: -1e9, hole: 1, holeFrom: 1, holeTo: 1,
                   holeAt: -1e9, armL: 16, armR: -16, plume: 0, lx: 0, ly: 0, sip: 0 };
        this.blinkUntil = 0;
        this.nextBlink = 0;
        this.glance = [0, 0];
        this.nextGlance = 0;
        this.apply({}, true);
    }

    // ------------------------------------------------------------ what the host calls

    setColors(colors) {
        this.colors = colors || art.LILAC;
    }

    setTheme(theme) {
        this.theme = theme;
    }

    setMood(mood) {
        if (!MOODS[mood] || mood === this.mood)
            return;
        this.mood = mood;
        this.base = Object.assign({}, MOODS[mood].base);
        this.lastAuto = '';
        if (this.held)
            return;
        if (!this.run || this.run.auto)
            this.cancel();
        if (!this.run)
            this.apply({});
        this.nextBrain = this.now + random.rnd(200, 700);
    }

    // An action now: a reaction to you or to an event (done(true) when it ran to the end)
    react(name, done) {
        if (this.held || !ACTIONS[name]) {
            if (done)
                done(false);
            return;
        }
        this.play(name, done, false);
    }

    get busy() {
        return this.run !== null || this.held;
    }

    // Grabbed: out of the burrow, flailing; nothing else moves it until land()
    // Returns how much of its hole it leaves behind (0: none, it was tunnelling
    // or down; 1: the whole hole): the host keeps drawing that where it was
    // (drawHole), since the hole doesn't come along
    setHeld(held) {
        if (!held)
            return 0;
        const left = this.st.tunnel === '1' ? 0 : clamp01(this.v.hole);
        this.cancel();
        this.held = true;
        this.heldAt = this.now;
        this.apply({ depth: 'out', hole: 0, held: 1, arms: 'flail', eyes: 'wide', mouth: 'o', body: '', fx: dirt(8, 16) });
        // out at once, its hole gone from under it (a creature half in a hole that
        // closes while it moves would show where there's no hole)
        const v = this.v;
        v.depth = v.depthFrom = v.depthTo = DEPTH.out;
        v.hole = v.holeFrom = v.holeTo = 0;
        return left > 0.02 ? left : 0;
    }

    // Dropped from `height` box units: lands, maybe dizzy, then digs in right there
    // Returns how it feels, for the words the host has it say: 'sick' (from
    // high up), 'dizzy' or 'fine'
    land(height, done) {
        this.held = false;
        const feel = height > 28 ? 'sick' : height > 16 ? 'dizzy' : 'fine';
        const first = feel === 'sick' ? { eyes: 'spiral', mouth: 'wavy', plume: 'droop', orbit: 1, t: 1800 }
                    : feel === 'dizzy' ? { eyes: 'spiral', mouth: 'o', orbit: 1, t: 1200 }
                    : { eyes: 'happy', mouth: 'smile', t: 700 };
        this.play({ steps: () => [
            Object.assign({ depth: 'out', hole: 0, body: 'land' }, first),
            { depth: 'out', hole: 1, eyes: feel === 'sick' ? 'tired' : 'open', mouth: feel === 'sick' ? 'wavy' : 'flat', look: [-1.5, 0.5],
              t: 500 },   // the hole opens under it first
            { body: 'spin', eyes: 'squint', fx: dirt(10), t: 700 },
            { eyes: 'happy', t: 500 }] }, done, false);
        return feel;
    }

    // Shown at rest in a pose right away, no motion in between (icons, previews)
    pose(step) {
        this.cancel();
        this.apply(step || {}, true);
        this.nextBlink = this.now + 1e9;   // eyes open for a still picture
        this.nextGlance = this.now + 1e9;
        this.glance = [0, 0];
        const look = this.st.look || [0, 0];
        this.v.lx = look[0];
        this.v.ly = look[1];
    }

    // ------------------------------------------------------------ steps

    play(nameOrDef, done, auto) {
        const def = typeof nameOrDef === 'string' ? ACTIONS[nameOrDef] : nameOrDef;
        this.cancel();
        if (nameOrDef === 'arrive')
            this.away = false;
        if (!def) {
            if (done)
                done(false);
            return;
        }
        const keep = def.keep || {};
        const steps = def.steps(this.words).map(s => Object.assign({}, keep, s));
        this.run = { name: typeof nameOrDef === 'string' ? nameOrDef : '', steps: steps, i: -1, until: 0, done: done || null, auto: !!auto };
        this.next();
    }

    cancel() {
        const r = this.run;
        this.run = null;
        if (r && r.done)
            r.done(false);
    }

    next() {
        const r = this.run;
        r.i += 1;
        if (r.i >= r.steps.length) {
            this.run = null;
            if (r.name === 'leave')
                this.away = true;
            this.apply({});
            const p = (MOODS[this.mood] || MOODS.idle).pause;
            this.nextBrain = this.now + random.rnd(p[0], p[1]);
            if (r.done)
                r.done(true);
            return;
        }
        const step = r.steps[r.i];
        r.until = this.now + (step.t || 600);
        this.apply(step, step.snap === 1);
        if (step.travel && this.onTravel)
            this.onTravel(step.t || 1500);
    }

    apply(step, snap) {
        this.stepAt = this.now;
        const prev = this.st, st = {};
        for (const k of KEYS) {
            const v = step[k] !== undefined ? step[k] : this.base[k];
            st[k] = v === undefined || v === null ? '' : String(v);
            if (st[k] !== prev[k] || (step[k] !== undefined && (k === 'body' || k === 'plume' || k === 'arms')))
                this.since[k] = this.now;
        }
        if (this.away && step.depth === undefined) {
            st.depth = 'gone';   // (left: not back up by itself)
            st.hole = '0';
        }
        st.it = step.it || 1;
        st.look = step.look || this.base.look || null;
        if (st.depth !== prev.depth || st.arms !== prev.arms || st.plume !== prev.plume || st.sip !== prev.sip
            || String(st.look) !== String(prev.look))
            this.easeAt = this.now;
        if (step.gum)
            st.gum = String(step.gum);
        this.st = st;
        const depth = DEPTH[st.depth] !== undefined ? DEPTH[st.depth] : DEPTH.half;
        // (out for a moment, a jump or a sneeze, its hole stays: only held or
        // tunnelling it has none)
        const hole = st.hole === '0' ? 0 : 1;
        const v = this.v;
        if (snap) {
            v.depth = v.depthFrom = v.depthTo = depth;
            v.hole = v.holeFrom = v.holeTo = hole;
            const a = ARMS[st.arms] || ARMS.rest;
            v.armL = a[0];
            v.armR = a[1];
            v.plume = plumeAngle(st.plume, 0, 1);
        } else {
            if (depth !== v.depthTo) {
                v.depthFrom = v.depth;
                v.depthTo = depth;
                v.depthAt = this.now;
            }
            if (hole !== v.holeTo) {
                v.holeFrom = v.hole;
                v.holeTo = hole;
                v.holeAt = this.now;
            }
        }
        for (const f of (step.fx || [])) {
            const o = f[3] || {};
            this.fx.push({ kind: f[0], x: f[1], y: f[2], o: o, born: this.now + (o.delay || 0) * 1000 });
        }
    }

    // ------------------------------------------------------------ time

    tick(now) {
        const dt = this.last ? Math.min(100, Math.max(0, now - this.last)) : 0;
        this.last = now;
        this.now = now;
        if (this.run && now >= this.run.until)
            this.next();
        if (!this.run && !this.held && this.brainOn && now >= this.nextBrain)
            this.think();
        if (this.held && now - this.heldAt > 2200 && this.st.eyes !== 'tiny') {
            this.st.eyes = 'tiny';     // held up for long: scared
            this.st.mouth = 'wavy';
        }
        const v = this.v, st = this.st;
        const before = {};
        for (const key in EASED)
            before[key] = v[key] || 0;
        // the burrow depth: pops up with a little overshoot, sinks smoothly
        const dp = clamp01((now - v.depthAt) / 450);
        v.depth = lerp(v.depthFrom, v.depthTo, v.depthTo > v.depthFrom && v.depthTo >= DEPTH.hidden ? dp * dp : outBack(dp));
        const hp = clamp01((now - v.holeAt) / 350);
        v.hole = lerp(v.holeFrom, v.holeTo, v.holeTo > v.holeFrom ? outBack(hp) : hp);
        // arms, lamp stalk, eyes: eased towards where they're going
        const k = tau => 1 - Math.exp(-dt / tau);
        const moving = armMove(st.arms, now - (this.since.arms || 0));
        const target = moving || ARMS[st.arms] || ARMS.rest;
        v.armL += (target[0] - v.armL) * k(moving ? 40 : 70);
        v.armR += (target[1] - v.armR) * k(moving ? 40 : 70);
        v.armLy = moving ? moving[2] : 0;
        v.armRy = moving ? moving[3] : 0;
        const plumeTarget = st.tunnel === '1' ? plumeAngle('wild', now, 1) : plumeAngle(st.plume, now - (this.since.plume || 0), Number(st.it));
        v.plume = st.plume === 'twirl' || st.plume === 'wild' || st.tunnel === '1' ? plumeTarget : v.plume + (plumeTarget - v.plume) * k(70);
        v.sip += ((st.sip === '1' ? 1 : 0) - v.sip) * k(90);
        // glances and blinks of its own
        if (now >= this.nextGlance) {
            this.glance = st.prop || Math.random() < 0.4 ? [0, 0] : [random.rnd(-1.6, 1.6), random.rnd(-1, 0.8)];
            this.nextGlance = now + random.rnd(900, 3200);
        }
        const look = st.look || this.glance;
        v.lx += (look[0] - v.lx) * k(90);
        v.ly += (look[1] - v.ly) * k(90);
        if (!this.nextBlink)
            this.nextBlink = now + random.rnd(1200, 4000);   // not straight away
        if (now >= this.nextBlink && BLINKING[st.eyes]) {
            this.blinkUntil = now + 110;
            this.nextBlink = now + (Math.random() < 0.22 ? 250 : random.rnd(1800, 5200));
        }
        this.fx = this.fx.filter(p => now - p.born < (p.o.dur || 1.6) * 1000);
        // How often the host should draw it now (each picture costs)
        let quick = false, slow = false;
        for (const key in EASED) {
            const moved = Math.abs((v[key] || 0) - before[key]) * 40 / Math.max(dt, 1);
            quick = quick || moved > EASED[key];
            slow = slow || moved > EASED[key] / 20;
        }
        const body = st.body === '' ? null : st.body === 'fast' ? true : bodyMove(st.body, now - (this.since.body || 0), Number(st.it));
        quick = quick || this.held || st.walk === '1' || QUICK_PROPS[st.prop] === 1 || (body !== null && QUICK_BODY[st.body] === 1)
            || (BLINKING[st.eyes] === 1 && now >= this.nextBlink - 45) || now < this.blinkUntil + 45;
        slow = slow || (body !== null && RESTING_BODY[st.body] !== 1) || this.fx.some(p => p.kind !== 'z') || st.tunnel === '1' || st.orbit === '1'
            || st.think === '1' || SLOW_PROPS[st.prop] === 1;
        this.frameMs = quick ? 40 : slow ? 80 : 200;
        // A big move (no picture shows it, see stills), and when what it threw meanwhile lands
        const once = ONCE[st.body];
        const big = (once !== undefined && now - (this.since.body || 0) < once * (st.body === 'hop' || st.body === 'jump' ? Number(st.it) : 1))
            || (Math.abs(v.depthTo - v.depthFrom) >= BIG_DEPTH && dp < 1) || (v.holeTo !== v.holeFrom && hp < 1);
        if (big && !this.big)
            this.bigAt = this.stepAt;   // (the step that started it: maybe a reaction since the last tick)
        this.big = big;
        if (big)
            this.landsAt = this.fx.reduce((t, p) => p.born >= this.bigAt ? Math.max(t, p.born + (p.o.dur || 1.6) * 1000) : t, now);
        // The new pose has settled (a film shows it from its pictures again)
        this.settled = !big && dp >= 1 && hp >= 1 && now - this.easeAt >= SETTLE_MS && !this.oneShot();
    }

    // A move that happens once and has no pictures is under way (see films)
    oneShot() {
        for (const key in ONE_SHOT) {
            const ms = ONE_SHOT[key][this.st[key]];
            if (ms !== undefined && this.now - (this.since[key] || 0) < ms * (key === 'plume' ? Number(this.st.it) : 1))
                return true;
        }
        return false;
    }

    think() {
        if (this.away) {
            this.nextBrain = this.now + 2000;
            return;
        }
        const pool = Object.assign({}, (MOODS[this.mood] || MOODS.idle).pool);
        if (!this.travels || !this.onTravel)
            delete pool.dig;
        delete pool[this.lastAuto];
        const names = Object.keys(pool);
        if (!names.length) {
            this.nextBrain = this.now + 2000;
            return;
        }
        let r = Math.random() * names.reduce((s, n) => s + pool[n], 0);
        let name = names[0];
        for (const n of names) {
            r -= pool[n];
            if (r < 0) {
                name = n;
                break;
            }
        }
        this.lastAuto = name;
        this.play(name, null, true);
    }

    // ------------------------------------------------------------ stills (see above)

    beat() {
        const st = this.st;
        const beats = [ARM_BEATS[st.arms], BODY_BEATS[st.body], PLUME_BEATS[st.plume], PROP_BEATS[st.prop], st.tunnel === '1' ? TUNNEL_BEAT : undefined]
            .filter(b => b !== undefined);
        if (!beats.length)
            return -1;
        const step = BEAT_MS * Math.max(1, Math.round(Math.min.apply(null, beats) / 2 / BEAT_MS));
        const beat = Math.floor(this.now / step) % 4;
        return beat === 3 && !ONE_WAY[st.arms] && !ONE_WAY[st.body] && !ONE_WAY[st.prop] && st.tunnel !== '1' ? 1 : beat;
    }

    // (busy with a quick move, it looks ahead: each glance would take four new pictures)
    stillLook() {
        const look = this.st.look || (this.beat() >= 0 ? [0, 0] : this.glance);
        return [Math.round(look[0] * 2) / 2, Math.round(look[1] * 2) / 2];
    }

    // What a picture of it shows now, as "pose#details": the host fades over
    // to a new pose, and just changes the picture for new details (a blink, a
    // glance, a beat); '' during a big move
    stillKey() {
        if (this.big || this.now < this.landsAt)
            return '';
        const st = this.st;
        const blink = this.now < this.blinkUntil && BLINKING[st.eyes] === 1;
        return KEYS.map(k => st[k]).join(',') + ',' + st.it + ',' + (st.gum || '') + (this.facingLeft ? ',L' : ',R')
            + '#' + this.stillLook().join(',') + (blink ? ',blink' : '') + ',' + this.beat();
    }

    drawStill(ctx) {
        let s = this.stillOne;
        if (!s)
            s = this.stillOne = new Talpy({ brain: false });
        s.colors = this.colors;
        s.theme = this.theme;
        s.facingLeft = this.facingLeft;
        const st = this.st, step = { it: st.it, look: this.stillLook() };
        for (const k of KEYS) {
            if (st[k] !== '')
                step[k] = st[k];
        }
        if (st.gum)
            step.gum = st.gum;
        if (this.now < this.blinkUntil && BLINKING[st.eyes] === 1)
            step.eyes = 'closed';
        // a quick move where this beat has it (ms into it)
        const beat = Math.max(0, this.beat());
        const at = beats => beat * beats / 2;
        s.now = 1e6 + at(PROP_BEATS[st.prop] || (st.tunnel === '1' ? TUNNEL_BEAT : 0));
        s.pose(step);
        this.placeMoves(s, st, at);
        s.draw(ctx);
    }

    // (the still one's quick moves where at(beats) has them)
    placeMoves(s, st, at) {
        const arms = ARM_BEATS[st.arms] !== undefined ? armMove(st.arms, at(ARM_BEATS[st.arms])) : null;
        s.v.armLy = arms ? arms[2] : 0;
        s.v.armRy = arms ? arms[3] : 0;
        if (arms) {
            s.v.armL = arms[0];
            s.v.armR = arms[1];
        }
        if (st.tunnel === '1')
            s.v.plume = plumeAngle('wild', at(PLUME_BEATS.wild), 1);
        else if (PLUME_BEATS[st.plume] !== undefined)
            s.v.plume = plumeAngle(st.plume, at(PLUME_BEATS[st.plume]), 1);
        // (a move that happens once shows as done)
        s.since.body = s.now - (BODY_BEATS[st.body] !== undefined ? at(BODY_BEATS[st.body]) : 1e7);
    }

    // ------------------------------------------------------------ films (see films.mjs)

    // The quick moves under way: [what moves, its beat] (beats as above)
    moves() {
        const st = this.st, out = [];
        if (ARM_BEATS[st.arms] !== undefined)
            out.push(['arms', ARM_BEATS[st.arms]]);
        if (BODY_BEATS[st.body] !== undefined)
            out.push(['body', BODY_BEATS[st.body]]);
        if (PLUME_BEATS[st.plume] !== undefined)
            out.push(['plume', PLUME_BEATS[st.plume]]);
        if (PROP_BEATS[st.prop] !== undefined)
            out.push(['prop', PROP_BEATS[st.prop]]);
        if (st.tunnel === '1')
            out.push(['tunnel', TUNNEL_BEAT]);
        return out;
    }

    // What a film's picture of it shows now: its pose and details (as for
    // stillKey) and where it is in a quick move's round, pictures at least
    // frameMs apart and at most maxFrames a round (a move that swings back
    // shows the same ones on the way back); null during a big move. rest: no
    // quick move under way; every: ms from one picture to the next, a whole
    // number of the host's ticks (clockMs apart). ticksSince(id, ms), from
    // the film: the ticks since the move (id) began, ms ago; a picture then
    // shows for exactly its ticks, 2 each, not 2, 2, 3 (by time, a picture
    // due right at a tick showed for 1 tick or 3 as the ticks jittered).
    still(frameMs, maxFrames, clockMs, ticksSince) {
        if (this.big || this.now < this.landsAt || this.oneShot())
            return null;
        const st = this.st, moves = this.moves();
        let frames = 1, frame = 0, round = 0, every = Infinity;
        if (moves.length) {
            let quick = moves[0];
            for (const m of moves) {
                if (m[1] < quick[1])
                    quick = m;
            }
            round = 2 * quick[1];
            const oneWay = ONE_WAY[st.arms] === 1 || ONE_WAY[st.body] === 1 || ONE_WAY[st.prop] === 1 || st.tunnel === '1';
            const tick = Math.max(1, frameMs);
            const clock = clockMs > 0 ? clockMs : tick;
            const ticks = Math.ceil(round / (tick * (maxFrames || 12)));   // frameMs a picture shows
            const q = Math.max(1, Math.round(ticks * tick / clock));        // the host's ticks a picture shows
            every = q * clock;
            frames = Math.max(4, Math.round(round / (ticks * tick)));
            if (!oneWay)
                frames += frames % 2;
            // (counted from when that move began, as it's drawn live)
            const ms = Math.max(0, this.now - (this.since[quick[0]] || 0));
            frame = ticksSince ? Math.floor(ticksSince(quick[0] + '@' + (this.since[quick[0]] || 0), ms) / q) % frames
                               : Math.floor((ms + clock / 2) / every) % frames;
            if (!oneWay && frame > frames / 2)
                frame = frames - frame;
        }
        const blink = this.now < this.blinkUntil && BLINKING[st.eyes] === 1;
        const look = this.stillLook();
        const pose = KEYS.map(k => st[k]).join(',') + ',' + st.it + ',' + (st.gum || '') + (this.facingLeft ? ',L' : ',R');
        return { key: pose + '#' + look.join(',') + (blink ? ',blink' : '') + ',' + frame + '/' + frames, pose: pose, st: st,
                 look: look, blink: blink, phase: frame / frames, rest: !moves.length, left: this.facingLeft,
                 every: every };
    }

    // Draws film picture d (still()), or one on its way there from picture
    // `from` (p: 0 there, 1 arrived), without what flies about
    drawFilm(ctx, d, from, p) {
        let was = null;
        if (from && p < 1) {
            const v = this.posed(from).v;
            was = TWEENED.map(k => v[k] || 0);
        }
        const s = this.posed(d);
        if (was) {
            const e = sine(clamp01(p));
            TWEENED.forEach((k, i) => {
                s.v[k] = lerp(was[i], s.v[k] || 0, e);
            });
        }
        s.draw(ctx, true);
    }

    // (the still one, posed as film picture d)
    posed(d) {
        let s = this.stillOne;
        if (!s)
            s = this.stillOne = new Talpy({ brain: false });
        s.colors = this.colors;
        s.theme = this.theme;
        s.holeImage = this.holeImage;
        s.facingLeft = d.left;
        const st = d.st, step = { it: st.it, look: d.look };
        for (const k of KEYS) {
            if (st[k] !== '')
                step[k] = st[k];
        }
        if (st.gum)
            step.gum = st.gum;
        if (d.blink)
            step.eyes = 'closed';
        const at = beats => d.phase * 2 * beats;
        s.now = 1e6 + at(PROP_BEATS[st.prop] || (st.tunnel === '1' ? TUNNEL_BEAT : 0));
        s.pose(step);
        this.placeMoves(s, st, at);
        return s;
    }

    // ------------------------------------------------------------ drawing

    // Draws the 64 × 64 box at the context's current transform (without what
    // flies about: a host that draws that on its own, see films.mjs)
    draw(ctx, withoutParticles) {
        const now = this.now, st = this.st, v = this.v, pal = this.colors;
        const ms = key => now - (this.since[key] || 0);
        const move = bodyMove(st.body, ms('body'), Number(st.it)) || {};
        ctx.save();
        if (this.facingLeft) {
            ctx.translate(BOX, 0);
            ctx.scale(-1, 1);
        }
        // its shadow when it's out on the floor
        if (st.depth === 'out' && v.depth < 3) {
            ctx.save();
            art.about(ctx, 32, 57.6, 0, st.held === '1' ? 0.5 : move.shadow || 1, 1);
            art.ellipse(ctx, 32, 57.6, 15, 2.1, 'rgba(20,25,45,0.16)');
            ctx.restore();
        }
        const tunneling = st.tunnel === '1';
        if (!tunneling && v.hole > 0.02)
            this.drawHolePart(ctx, 'back');
        // the creature, cut along the burrow (art.traceCut) while it's in it or
        // its hole is there (out for a moment, its feet would hang in the hole)
        ctx.save();
        const holed = !tunneling && v.hole > 0.02;
        if (st.depth !== 'out' || v.depth > 0 || holed) {
            const h = holed ? Math.min(1.1, v.hole) : 1;
            ctx.beginPath();
            art.traceCut(ctx, lerp(0.05, 1, h), lerp(0.2, 1, h));
            ctx.clip();
        }
        ctx.translate(0, v.depth);
        this.drawCreature(ctx, now, st, v, pal, move, ms);
        ctx.restore();
        if (tunneling) {
            ctx.save();
            const p = alt(now, 260);
            art.about(ctx, 32, 61.4, 0, lerp(1, 1.08, p), lerp(0.9, 1.1, p));
            art.tunnel(ctx);
            ctx.restore();
        } else if (v.hole > 0.02) {
            this.drawHolePart(ctx, 'front');
        }
        ctx.restore();
        if (!withoutParticles)
            this.drawParticles(ctx, now);
    }

    // Whether anything flies about now (drawParticles draws it), and whether
    // it's quick: z's float up slowly
    particles(now) {
        let any = false;
        for (const p of this.fx) {
            if (now < p.born)
                continue;
            if (p.kind !== 'z')
                return 2;
            any = true;
        }
        return any ? 1 : 0;
    }

    // (the open hole from the host's sheet when it has one: pixel for pixel,
    // since it was drawn at this transform)
    drawHolePart(ctx, part) {
        const image = this.holeImage && Math.abs(this.v.hole - 1) < 0.001 ? this.holeImage(part, this.facingLeft) : null;
        if (!image) {
            holePart(ctx, this.v.hole, part === 'front' ? art.holeFront : art.holeBack);
            return;
        }
        ctx.save();
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.drawImage(image, 0, 0);
        ctx.restore();
    }

    drawCreature(ctx, now, st, v, pal, move, ms) {
        // the whole creature: hops, sways, tumbles
        ctx.save();
        ctx.translate(move.wx || 0, move.wy || 0);
        if (st.walk === '1')
            ctx.translate(0, -1.3 * alt(now, 250));
        if (move.wr)
            art.about(ctx, move.ox || 32, move.oy || 57, move.wr);
        if (st.depth === 'out')
            this.drawTail(ctx, now, st, pal, ms);
        let feet = null;
        if (st.held === '1')
            feet = [lerp(-18, 18, alt(now, 450)), lerp(-18, 18, alt(now + 225, 450)), 0, 0];
        else if (st.walk === '1')
            feet = [0, 0, kf((now % 500) / 500, [[0, 0], [0.25, -2.4], [0.5, 0], [1, 0]]), kf(((now + 250) % 500) / 500, [[0, 0], [0.25, -2.4], [0.5, 0], [1, 0]])];
        // feet stand on the ground: over its hole they'd hang in it (unless it jumps clear)
        if (st.tunnel === '1' || v.hole <= 0.02 || (move.wy || 0) < -3)
            art.feet(ctx, pal, feet);
        // the torso: breathing, squash and stretch
        ctx.save();
        if (move.torso) {
            ctx.translate(0, move.ty || 0);
            art.about(ctx, 32, 55, move.tr || 0, move.sx, move.sy);
        } else {
            const p = (now % (st.body === 'fast' ? 600 : 2800)) / (st.body === 'fast' ? 600 : 2800);
            art.about(ctx, 32, 55, 0, kf(p, [[0, 1], [0.5, 1.022], [1, 1]]), kf(p, [[0, 1], [0.5, 0.978], [1, 1]]));
        }
        this.drawLamp(ctx, now, st, v, pal, move);
        art.body(ctx, pal);
        if (st.prop === 'headphones') {
            ctx.save();
            ctx.translate(0, art.PLUME_Y);
            art.headphones(ctx);
            ctx.restore();
        }
        this.drawFace(ctx, now, st, v, pal, move, ms);
        art.propBack(ctx, st.prop, now, this.theme.fx, v.sip, art.FACE_Y);
        this.drawArm(ctx, 14.5, v.armL, v.armLy || 0, pal, st.prop === 'balloon' ? 'balloon' : '', now);
        this.drawArm(ctx, 49.5, v.armR, v.armRy || 0, pal, st.prop === 'sign' ? 'sign' : st.arms === 'selfie' ? 'selfie' : '', now);
        art.propFront(ctx, st.prop, now, ms('prop'), { fail: st.fail === '1', gum: Number(st.gum) || 2000, bite: Number(st.bite) || 0, skin: pal.skin });
        if (st.think === '1')
            art.think(ctx, now, this.theme.fx);
        ctx.restore();
        if (st.prop === 'juggle')
            art.juggle(ctx, now);
        if (st.prop === 'battery')
            art.battery(ctx, now, this.theme.fx);
        if (st.orbit === '1') {
            ctx.save();
            ctx.translate(0, art.PLUME_Y);
            art.orbit(ctx, now);
            ctx.restore();
        }
        ctx.restore();
    }

    drawTail(ctx, now, st, pal, ms) {
        let a = lerp(-6, 9, alt(now, 2400));
        if (st.tail === 'wag')
            a = lerp(-16, 22, alt(now, 200));
        else if (st.tail === 'droop')
            a = 38;
        else if (st.tail === 'up')
            a = -30;
        ctx.save();
        art.about(ctx, 48.5, 50.5, a);
        art.tail(ctx, pal);
        ctx.restore();
    }

    drawLamp(ctx, now, st, v, pal, move) {
        const lamp = art.LAMP[st.lamp] || art.LAMP.idle;
        let glow = alt(now, st.lamp === 'needs' ? 700 : 2400), flicker = 1;
        if (st.lamp === 'limit') {
            glow = 0;
            const p = (now % 1600) / 1600;
            flicker = p < 0.75 ? 1 : p < 0.85 ? 0.3 : p < 0.9 ? 0.9 : 0.2;
        }
        ctx.save();
        ctx.translate(0, art.PLUME_Y);
        art.about(ctx, 31.4, 25.6, v.plume);
        if (move.turn !== undefined)
            art.about(ctx, 31.4, 25.6, 0, kf(move.turn, [[0, 1], [0.32, -1], [0.46, -1], [0.8, 1], [1, 1]]), 1);
        art.plume(ctx, pal, lamp, glow, flicker);
        ctx.restore();
    }

    // (no clip of its own: Qt's canvas puts the cut back in the wrong place
    // after a clip inside it, and hands showed under the burrow)
    drawFace(ctx, now, st, v, pal, move, ms) {
        ctx.save();
        ctx.translate(0, art.FACE_Y);
        if (move.turn !== undefined) {
            const p = move.turn;
            const tx = p < 0.22 ? lerp(0, 13, p / 0.22) : p < 0.32 ? lerp(13, 27, (p - 0.22) / 0.1)
                     : p < 0.46 ? -27 : p < 0.58 ? lerp(-27, -13, (p - 0.46) / 0.12) : p < 0.8 ? lerp(-13, 0, sine((p - 0.58) / 0.22)) : 0;
            const sx = p < 0.22 ? lerp(1, 0.55, p / 0.22) : p < 0.32 ? lerp(0.55, 0.3, (p - 0.22) / 0.1)
                     : p < 0.46 ? 0.3 : p < 0.58 ? lerp(0.3, 0.55, (p - 0.46) / 0.12) : p < 0.8 ? lerp(0.55, 1, sine((p - 0.58) / 0.22)) : 1;
            // it fades as it slides round the body's edge
            const fade = clamp01((24 - Math.abs(tx)) / 8);
            if (fade <= 0) {
                ctx.restore();
                return;
            }
            ctx.globalAlpha = ctx.globalAlpha * fade;
            ctx.translate(tx, 0);
            art.about(ctx, 32, 40, 0, sx, 1);
        }
        if (st.blush === '1')
            art.blush(ctx);
        const blink = now < this.blinkUntil ? 0.08 : 1;
        // a magnifier or telescope covers the right eye with its own
        art.eyes(ctx, pal, st.eyes || 'open', [v.lx, v.ly], blink, now);
        art.mouth(ctx, st.mouth);
        ctx.restore();
    }

    drawArm(ctx, x, angle, dy, pal, holds, now) {
        ctx.save();
        ctx.translate(x, 41 + dy);
        ctx.rotate(angle * Math.PI / 180);
        art.arm(ctx, pal);
        if (holds === 'balloon')
            art.balloon(ctx, lerp(-6, 6, alt(now, 1600)));
        else if (holds === 'sign')
            art.sign(ctx);
        else if (holds === 'selfie')
            art.selfie(ctx);
        ctx.restore();
    }

    drawParticles(ctx, now) {
        for (const p of this.fx) {
            const o = p.o, life = (o.dur || 1.6) * 1000, q = (now - p.born) / life;
            if (q < 0 || q > 1)
                continue;
            let x = p.x, y = p.y, s = 1, rot = 0, a = 1;
            if (o.path === 'fly') {
                const pts = [[0, 0], [-14, -9], [-4, -19], [9, -7], [1, -15], [-8, -26]];
                const seg = Math.min(4.999, q * 5), i = Math.floor(seg), f = sine(seg - i);
                x += lerp(pts[i][0], pts[i + 1][0], f);
                y += lerp(pts[i][1], pts[i + 1][1], f);
                a = q < 0.08 ? q / 0.08 : q > 0.85 ? (1 - q) / 0.15 : 1;
            } else if (o.path === 'plane') {
                const pts = [[0, 0, 0, 0], [0.4, 28, -14, -10], [0.7, 50, -5, 14], [1, 78, -20, -6]];
                let i = 1;
                while (i < 3 && q > pts[i][0])
                    i++;
                const f = sine((q - pts[i - 1][0]) / (pts[i][0] - pts[i - 1][0]));
                x += lerp(pts[i - 1][1], pts[i][1], f);
                y += lerp(pts[i - 1][2], pts[i][2], f);
                rot = lerp(pts[i - 1][3], pts[i][3], f);
                a = q > 0.7 ? (1 - q) / 0.3 : 1;
            } else if (o.path === 'crawl') {
                x -= 14 * q;
                y -= q < 0.5 ? 2 * q : 2 * (1 - q);
                a = q < 0.15 ? q / 0.15 : q > 0.85 ? (1 - q) / 0.15 : 1;
            } else if (o.path === 'pop') {
                s = lerp(0.4, 1.5, q);
                a = 1 - q;
            } else if (o.path === 'rise') {        // a soap bubble: wobbles up, then pops
                const e = ease('out', q);
                x += (o.dx || 0) * e + Math.sin(q * 9) * 1.4;
                y += (o.dy === undefined ? -20 : o.dy) * e;
                s = q > 0.88 ? lerp(1, 1.5, (q - 0.88) / 0.12) : lerp(0.5, 1, Math.min(1, q * 4));
                a = q > 0.88 ? (1 - q) / 0.12 : q < 0.1 ? q / 0.1 : 1;
            } else if (o.path === 'grow') {        // a flower: grows up, sways, fades away
                s = q < 0.2 ? outBack(q / 0.2) : 1;
                rot = q > 0.2 ? Math.sin((q - 0.2) * 12) * 4 : 0;
                a = q > 0.85 ? (1 - q) / 0.15 : 1;
            } else if (o.path === 'breathe') {     // a bubble at its nose as it sleeps: in, out, bigger
                s = (0.4 + 0.8 * (0.5 - 0.5 * Math.cos(q * 3 * 2 * Math.PI))) * Math.min(1, 0.5 + q);
            } else if (o.path === 'shoot') {       // a shooting star across the sky
                const e = ease('in', q);
                x += 76 * e;
                y += 12 * e;
                a = q < 0.1 ? q / 0.1 : 1 - e;
            } else if (o.path === 'bulb') {
                s = q < 0.25 ? lerp(0, 1.25, q / 0.25) : q < 0.4 ? lerp(1.25, 1, (q - 0.25) / 0.15) : 1;
                a = q > 0.85 ? (1 - q) / 0.15 : 1;
            } else if (p.kind === 'clod') {
                const dx = o.dx || 0;
                x += q < 0.45 ? dx * 0.55 * (q / 0.45) : lerp(dx * 0.55, dx, (q - 0.45) / 0.55);
                y += q < 0.45 ? -9 * (1 - (1 - q / 0.45) * (1 - q / 0.45)) : lerp(-9, 3, ((q - 0.45) / 0.55) * ((q - 0.45) / 0.55));
                a = 1 - q;
            } else {
                const e = ease(o.ease, q);
                x += (o.dx || 0) * e;
                y += (o.dy === undefined ? -12 : o.dy) * e;
                s = lerp(o.s0 === undefined ? 0.6 : o.s0, o.s1 === undefined ? 1 : o.s1, e);
                rot = (o.rot || 0) * e;
                a = q < 0.15 ? q / 0.15 : q > 0.7 ? (1 - q) / 0.3 : 1;
            }
            ctx.save();
            ctx.globalAlpha = ctx.globalAlpha * clamp01(a);
            ctx.translate(this.facingLeft ? BOX - x : x, y);
            if (rot)
                ctx.rotate((this.facingLeft ? -rot : rot) * Math.PI / 180);
            const k = s * (o.scale || 1);
            ctx.scale(this.facingLeft && p.kind !== 'text' && p.kind !== 'z' ? -k : k, k);
            art.particle(ctx, p.kind, o, now, this.theme);
            ctx.restore();
        }
    }
}
