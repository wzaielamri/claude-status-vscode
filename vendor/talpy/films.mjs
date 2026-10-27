// Talpy (talpy.mjs) as a film: it moves smoothly without being drawn every
// frame. Each picture of a quick move (a round of typing, a wave, a bounce)
// is drawn once, kept, and shown again as the move goes round; a new pose
// moves there through a few pictures in between (or is drawn live until it
// has settled: tween 0); a big move (a jump, popping out of its burrow) is
// drawn live; what flies about (dirt, hearts, z's) is drawn on a sheet of its
// own over it, only while there is any. pictures.mjs did the same with four
// pictures a move and a fade between poses (GNOME still does).
// smooth: a move too slow for a picture every frame (fewer than maxFrames a
// round) is drawn live instead of stepping from picture to picture.
//
//   const film = new Films(talpy, { sheets: 12, frameMs: 33, clockMs: 16.67 });
//   (clockMs: about how far apart the host's ticks come, the screen's frame
//   or two; the film measures them from then on. It counts its ticks: a
//   picture, one in between and what's drawn live each show for a whole
//   number of them, the same every time, whenever a tick comes.)
//   every tick: talpy.tick(now); const changed = film.tick(now); then
//   - each sheet s with s.paint set: clear it and draw it with
//     film.paint(s, ctx), ctx at the box's transform as for talpy.draw;
//   - show film.front, no other sheet, once it's drawn (an HTML canvas at
//     once; a QML Canvas shows its new picture a frame later: until then
//     keep the sheet shown before);
//   - film.fx: if fx.paint, clear what flies about and draw it with
//     film.paintFx(ctx); show it over Talpy while fx.on.
//
// Plain ES2017 (see paths.mjs).

// (what pictures in between would show moving: else the new pose just shows)
const moved = (a, b) => a.st.depth !== b.st.depth || a.st.arms !== b.st.arms || a.st.plume !== b.st.plume || a.st.sip !== b.st.sip;

export class Films {
    constructor(talpy, opts) {
        opts = opts || {};
        this.talpy = talpy;
        this.on = true;                          // false: drawn live (a pet you hold)
        this.frameMs = opts.frameMs || 33;       // a quick move's pictures at least this far apart; live ones too
        this.maxFrames = opts.maxFrames || 12;   // and at most this many a round
        this.restMs = opts.restMs || 0;          // at rest: drawn live (breathing, the lamp's glow) at most this often; 0: one picture
        this.tweenMs = opts.tweenMs === undefined ? 150 : opts.tweenMs;   // a new pose: on its way there (0: live until settled)
        this.tweens = opts.tweens || 3;          // in that many pictures, a tick each (then it's there)
        this.clockMs = opts.clockMs || 0;        // the host's ticks, about this far apart (0: unknown, frameMs)
        this.count = 0;                          // ticks so far
        this.gaps = [];                          // between the last ticks (ms), for clock()
        this.lastAt = null;
        this.move = { id: '', at: 0 };           // the quick move under way, from tick at
        this.smooth = !!opts.smooth;            // a move whose pictures would show for more than a frame: drawn live
        this.sheets = [];
        for (let i = 0; i < Math.max(2, opts.sheets || 8); i++)
            this.sheets.push({ index: i, key: '~', paint: false, usedAt: 0, d: null, from: null, p: 1 });
        this.front = this.sheets[0];
        this.shown = null;                       // the film's picture shown last (talpy.still()), null while live
        this.tween = null;                       // a pose on its way: { from, at }
        this.liveAt = -1e9;                      // (ticks, as fx.at and tween.at)
        this.fx = { on: false, paint: false, at: -1e9 };
    }

    // New colors, theme or size: every picture is drawn again, what shows at once
    reset() {
        for (const s of this.sheets) {
            if (s === this.front)
                s.paint = true;
            else if (!s.paint)
                s.key = '~';
        }
        this.liveAt = -1e9;
        this.fx.at = -1e9;
        this.shown = null;
        this.tween = null;
    }

    // How far apart the ticks come: clockMs, unless they come at another
    // pace (the median of the last ones), and how many make ms
    clock() {
        const hint = this.clockMs || this.frameMs;
        if (this.gaps.length < 5)
            return hint;
        const sorted = this.gaps.slice().sort((a, b) => a - b);
        const median = sorted[sorted.length >> 1];
        return Math.abs(median - hint) < hint / 4 ? hint : median;
    }

    ticksFor(ms) {
        return Math.max(1, Math.round(ms / this.clock()));
    }

    // (for Talpy.still: ticks since move id began, ms ago, counted from then on)
    ticksSince(id, ms) {
        if (this.move.id !== id)
            this.move = { id: id, at: this.count - Math.round(ms / this.clock()) };
        return this.count - this.move.at;
    }

    // Returns true when what shows changed
    tick(now) {
        const t = this.talpy;
        this.count++;
        if (this.lastAt !== null && now - this.lastAt > 0 && now - this.lastAt < 250) {
            this.gaps.push(now - this.lastAt);
            if (this.gaps.length > 15)
                this.gaps.shift();
        }
        this.lastAt = now;
        let changed = this.tickFx(now);
        const d = this.on ? t.still(this.frameMs, this.maxFrames, this.clock(), (id, ms) => this.ticksSince(id, ms)) : null;
        let every = 0;   // drawn live this often
        if (!d)
            every = this.on ? this.frameMs : Math.max(this.frameMs, t.frameMs >= 200 ? 2 * this.frameMs : this.frameMs);
        else if (this.tweenMs <= 0 && !t.settled)
            every = this.frameMs;
        else if (this.smooth && !d.rest && d.every > 1.5 * this.frameMs)
            every = this.frameMs;
        else if (d.rest && this.restMs > 0 && !this.tween)
            every = t.frameMs >= 200 ? this.restMs : t.frameMs >= 80 ? 2 * this.frameMs : this.frameMs;
        if (every > 0) {
            this.tween = null;
            this.shown = d && this.tweenMs <= 0 ? d : null;
            return this.live(now, every) || changed;
        }
        let pic = { key: d.key, d: d, from: null, p: 1 };
        // a new pose: on its way there through pictures in between
        // (a picture each frameMs, in whole ticks)
        const steps = () => Math.floor((this.count - this.tween.at) / this.ticksFor(this.frameMs));
        let was = this.shown;
        if (this.tween && steps() >= this.tweens) {
            this.tween = null;
            was = this.shown = d;   // (there)
        }
        if (!this.tween && was && was.pose !== d.pose && was.left === d.left && this.tweenMs > 0 && moved(was, d))
            this.tween = { from: was, at: this.count };
        if (this.tween) {
            const k = Math.min(this.tweens, 1 + steps());
            const from = this.tween.from;
            pic = { key: from.key + '>' + d.key + '@' + k, d: d, from: from, p: k / (this.tweens + 1) };
        } else {
            this.shown = d;
        }
        return this.show(pic, now) || changed;
    }

    // Drawn live on the sheet in front (or the one unused longest)
    live(now, every) {
        let s = this.front;
        if (s.key !== '') {
            s = this.free();
            s.key = '';
            s.d = null;
            this.liveAt = -1e9;
        }
        let changed = s !== this.front;
        this.toFront(s, now);
        if (this.count - this.liveAt >= this.ticksFor(every)) {
            this.liveAt = this.count;
            s.paint = true;
            changed = true;
        }
        return changed;
    }

    show(pic, now) {
        if (this.front.key === pic.key)
            return false;
        let s = null;
        for (const sheet of this.sheets) {
            if (sheet.key === pic.key)
                s = sheet;
        }
        if (!s) {
            s = this.free();
            s.key = pic.key;
            s.d = pic.d;
            s.from = pic.from;
            s.p = pic.p;
            s.paint = true;
        }
        this.toFront(s, now);
        return true;
    }

    // The sheet unused longest (never the one in front)
    free() {
        let free = null;
        for (const s of this.sheets) {
            if (s !== this.front && (!free || s.usedAt < free.usedAt))
                free = s;
        }
        return free;
    }

    toFront(s, now) {
        this.front.usedAt = now;
        s.usedAt = now;
        this.front = s;
    }

    // What flies about: drawn as often as it moves (z's float up slowly)
    tickFx(now) {
        const fx = this.fx, flying = this.talpy.particles(now);
        if (!flying) {
            if (!fx.on)
                return false;
            fx.on = false;
            return true;
        }
        const every = flying === 1 ? Math.max(this.frameMs, 200) : this.frameMs;
        const changed = !fx.on || this.count - fx.at >= this.ticksFor(every);
        fx.on = true;
        if (changed) {
            fx.at = this.count;
            fx.paint = true;
        }
        return changed;
    }

    // Draws sheet s: its picture, or Talpy now (live)
    paint(s, ctx) {
        if (s.key === '')
            this.talpy.draw(ctx, true);
        else if (s.d)
            this.talpy.drawFilm(ctx, s.d, s.from, s.p);
        s.paint = false;
    }

    paintFx(ctx) {
        this.fx.paint = false;
        if (this.fx.on)
            this.talpy.drawParticles(ctx, this.talpy.now);
    }
}
