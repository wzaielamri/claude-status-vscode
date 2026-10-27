// Talpy (talpy.mjs) as pictures, for a host that can't afford to draw it
// every frame (the widget's chat pets): a picture is drawn once, shown while
// it's what Talpy looks like (talpy.stillKey) and kept for when it's that
// again, so a quick move goes round its pictures without drawing any.
//
//   const pictures = new Pictures(talpy, 5);   // five sheets: canvases, offscreen or not
//   every tick: talpy.tick(now); const changed = pictures.tick(now); then
//   - each sheet s with s.paint set: clear that and draw the sheet (now, or
//     when its canvas paints, before the next tick) with pictures.paint(s,
//     ctx), ctx at the box's transform as for talpy.draw;
//   - show pictures.front; while pictures.coming is set, the two mixed:
//     (1 - mix) × front + mix × coming, added up ('lighter' on a canvas,
//     Cairo's ADD), not one over the other. changed: that changed.
//
// Mixed that way a pose fades into the next and the background never shows
// through: two half see-through pictures one over the other let a quarter of
// it through (Talpy went darker, then lighter again), and an old picture kept
// whole under the new one shows what moved twice. A new detail (a blink, a
// glance, a beat) just shows. A big move (talpy.stillKey() is '') is drawn
// live on the front sheet (its key ''), and so is everything with pictures
// off (a pet you hold; the big Talpy has one sheet).
//
// Plain ES2017 (see paths.mjs).

const pose = key => key.split('#')[0];

export class Pictures {
    constructor(talpy, count) {
        this.talpy = talpy;
        this.pictures = count > 1;   // false: drawn live
        this.fadeMs = 200;           // 0: a new pose just shows
        this.bigMs = 80;             // a big move is drawn at most this often (with pictures on)
        this.slackMs = 5;            // how much early a tick may come and still draw (the widget's come every 40 ms)
        this.sheets = [];
        for (let i = 0; i < Math.max(1, count); i++)
            this.sheets.push({ index: i, key: '~', ready: false, paint: false, usedAt: 0 });
        this.front = this.sheets[0];
        this.coming = null;          // fading in over the front since comingAt: mix of it shows
        this.comingAt = 0;
        this.mix = 0;
        this.waiting = null;         // its picture is being drawn, then it shows
        this.liveAt = -1e9;          // drawn live: the last time
    }

    // New colors, theme or size: every picture is drawn again
    reset() {
        for (const s of this.sheets) {
            s.key = '~';
            s.ready = false;
        }
        this.liveAt = -1e9;
    }

    // Returns true when what shows changed
    tick(now) {
        const key = this.pictures && this.sheets.length > 1 ? this.talpy.stillKey() : '';
        if (key === '') {
            let changed = this.coming !== null;
            this.coming = this.waiting = null;
            const s = this.front;
            if (s.key !== '') {
                s.key = '';
                this.liveAt = -1e9;
            }
            const every = this.pictures ? Math.max(this.talpy.frameMs, this.bigMs) : this.talpy.frameMs;
            if (now - this.liveAt >= every - this.slackMs) {
                this.liveAt = now;
                s.paint = true;
                changed = true;
            }
            return changed;
        }
        let changed = false;
        if (this.coming) {
            this.mix = this.fadeMs > 0 ? (now - this.comingAt) / this.fadeMs : 1;
            if (this.mix < 1)
                return true;
            this.toFront(this.coming, now);
            changed = true;
        }
        if (this.waiting) {
            if (!this.waiting.ready)
                return changed;   // (drawn by the next tick)
            const s = this.waiting;
            this.waiting = null;
            if (s.key === key)
                return this.show(s, now);
            // (it changed meanwhile: that one's kept for later)
        }
        if (this.front.key === key)
            return changed;
        for (const s of this.sheets) {
            if (s.ready && s.key === key)
                return this.show(s, now);
        }
        // drawn on the sheet unused longest
        let free = null;
        for (const s of this.sheets) {
            if (s !== this.front && (!free || s.usedAt < free.usedAt))
                free = s;
        }
        free.key = key;
        free.ready = false;
        free.paint = true;
        this.waiting = free;
        return changed;
    }

    // (a new pose fades in, unless there's nothing to fade from yet)
    show(s, now) {
        const front = this.front;
        if (this.fadeMs <= 0 || !front.ready || (front.key !== '' && pose(front.key) === pose(s.key))) {
            this.toFront(s, now);
        } else {
            this.coming = s;
            this.comingAt = now;
            this.mix = 0;
        }
        return true;
    }

    toFront(s, now) {
        this.front.usedAt = now;
        s.usedAt = now;
        this.front = s;
        this.coming = null;
        this.mix = 0;
    }

    // Draws sheet s: Talpy now, live or its picture
    paint(s, ctx) {
        const t = this.talpy;
        if (s.key === '') {
            t.draw(ctx);
        } else {
            t.drawStill(ctx);
            const key = t.stillKey();
            s.key = key === '' ? '~' : key;   // (what's on it)
        }
        s.ready = true;
    }
}
