// Talpy's parts: body, face, lamp, arms, burrow, props and the bits that fly
// off (particles). All in one 64 × 64 box with the floor at y ≈ 57; the
// shapes come straight from the design (build "Bean").
//
// Plain ES2017 (see paths.mjs).

import { tracePath, traceEllipse, traceRoundRect } from './paths.mjs';

export const INK = '#1b2143';
export const HEART = 'M0 4.5C-6 .5-3-4.5 0-1.5 3-4.5 6 .5 0 4.5Z';
export const STAR4 = 'M0-3.4L1-1 3.4 0 1 1 0 3.4-1 1-3.4 0-1-1Z';
export const BODY = 'M19 54C13.5 54 12.6 47 13 40C13.8 27 21.5 20 32 20S50.2 27 51 40C51.4 47 50.5 54 45 54Z';
// What can be seen of the creature (and what it holds) while it's in its
// burrow or its hole is there: whatever is above the mound, and what comes up
// out of the opening (straight up from it, down to its front edge, which the
// lip covers). So nothing shows beside the hole, on the mound, or below its
// edge. The burrow's edge, from the floor on the right round to the left:
export const EDGE = 'M59 56.6A27 6.2 0 0 0 53.5 52.85V55.6A21.5 2.2 0 0 1 10.5 55.6V52.85A27 6.2 0 0 0 5 56.6';

// The cut, with the burrow's edge scaled like the hole is while it opens or
// closes (sx, sy about 32, 56: see Talpy.draw)
export function traceCut(ctx, sx, sy) {
    ctx.moveTo(-40, -60);
    ctx.lineTo(104, -60);
    ctx.lineTo(104, 56.6);
    tracePath(ctx, EDGE, (x, y) => [32 + (x - 32) * sx, 56 + (y - 56) * sy], true);
    ctx.lineTo(-40, 56.6);
    ctx.closePath();
}
export const FACE_Y = -2;    // the face sits a little higher on the Bean
export const PLUME_Y = -5;   // and so does what's on its head

// ---------------------------------------------------------------- drawing helpers

export function fill(ctx, color, alpha) {
    if (alpha !== undefined && alpha < 1) {
        ctx.save();
        ctx.globalAlpha = ctx.globalAlpha * Math.max(0, alpha);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.restore();
    } else {
        ctx.fillStyle = color;
        ctx.fill();
    }
}

export function stroke(ctx, color, width, alpha) {
    ctx.save();
    if (alpha !== undefined && alpha < 1)
        ctx.globalAlpha = ctx.globalAlpha * Math.max(0, alpha);
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.stroke();
    ctx.restore();
}

export function path(ctx, d, color, alpha) {
    ctx.beginPath();
    tracePath(ctx, d);
    fill(ctx, color, alpha);
}

export function line(ctx, d, color, width, alpha) {
    ctx.beginPath();
    tracePath(ctx, d);
    stroke(ctx, color, width, alpha);
}

export function ellipse(ctx, cx, cy, rx, ry, color, alpha) {
    ctx.beginPath();
    traceEllipse(ctx, cx, cy, rx, ry);
    fill(ctx, color, alpha);
}

export function circle(ctx, cx, cy, r, color, alpha) {
    ellipse(ctx, cx, cy, r, r, color, alpha);
}

export function rrect(ctx, x, y, w, h, r, color, line_, width) {
    ctx.beginPath();
    traceRoundRect(ctx, x, y, w, h, r);
    if (color)
        fill(ctx, color);
    if (line_)
        stroke(ctx, line_, width || 1);
}

// Rotate (degrees) / scale about a point, inside a save()/restore() pair
export function about(ctx, x, y, deg, sx, sy) {
    ctx.translate(x, y);
    if (deg)
        ctx.rotate(deg * Math.PI / 180);
    if (sx !== undefined && (sx !== 1 || sy !== 1))
        ctx.scale(sx, sy === undefined ? sx : sy);
    ctx.translate(-x, -y);
}

// ---------------------------------------------------------------- colors

function hex(c) {
    const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})/i.exec(c);
    return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [153, 143, 190];
}
function toHex(rgb) {
    return '#' + rgb.map(v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('');
}

// a color mixed with another (k of it)
export function mix(a, b, k) {
    const x = hex(a), y = hex(b);
    return toHex(x.map((v, i) => v * (1 - k) + y[i] * k));
}

// Talpy's own colors, and a calm version of a chat's color for a chat's pet
export const LILAC = { skin: '#998fbe', shade: '#6b6190' };
export function calmColors(tint) {
    if (!tint)
        return LILAC;
    const c = hex(tint), grey = [160, 160, 176];
    const skin = c.map((v, i) => v * 0.6 + grey[i] * 0.4);
    return { skin: toHex(skin), shade: toHex(skin.map(v => v * 0.7)) };
}

export const LAMP = {
    idle: '#ffd86b', working: '#3daee9', agents: '#3daee9', needs: '#f67400',
    done: '#27ae60', failed: '#da4453', limit: '#9a927c', error: '#da4453'
};

// ---------------------------------------------------------------- the burrow

const BACK_LUMPS = [[12, 52.4, 2.6, 1.4], [21.5, 50.9, 2.8, 1.4], [42.5, 50.9, 2.8, 1.4], [52, 52.4, 2.6, 1.4]];
const FRONT_LUMPS = [[13.5, 56.3, 2.4, 1.5], [19.5, 57, 2.8, 1.7], [26, 57.3, 2.6, 1.5], [33, 57.4, 3, 1.7],
                     [40, 57.2, 2.6, 1.6], [46.5, 56.8, 2.8, 1.6], [51.5, 56.1, 2.2, 1.4]];
const DIRT = '#8d6e57', LUMP = '#977760';

export function holeBack(ctx) {
    ellipse(ctx, 32, 56.6, 27, 6.2, DIRT);
    for (const l of BACK_LUMPS)
        ellipse(ctx, l[0], l[1], l[2], l[3], LUMP);
    ellipse(ctx, 32, 55.6, 21.5, 3.8, '#33271f');
    ellipse(ctx, 32, 56.4, 17, 2.6, '#1f1712');
}

export function holeFront(ctx) {
    // a soft shadow where the body meets the burrow
    line(ctx, 'M11 55.2A21 1.6 0 0 0 53 55.2', '#1c120c', 4.6, 0.16);
    line(ctx, 'M11 55.2A21 1.6 0 0 0 53 55.2', '#1c120c', 2.4, 0.3);
    // the lip, up to the edge the creature is cut along (CUT)
    path(ctx, 'M5 56.6A27 6.2 0 0 0 59 56.6L53.5 55.6A21.5 2.2 0 0 1 10.5 55.6Z', DIRT);
    for (const l of FRONT_LUMPS)
        ellipse(ctx, l[0], l[1], l[2], l[3], LUMP);
    line(ctx, 'M12.4 57.6A19.8 2.9 0 0 0 51.6 57.6', '#b29276', 0.8, 0.8);
    for (const s of [[16, 60.4], [24, 61.6], [30, 62.1], [38, 61.8], [47, 60.6]])
        circle(ctx, s[0], s[1], 0.55, '#6c5241');
    ellipse(ctx, 7.6, 58, 1.3, 0.9, '#8a8174');
    ellipse(ctx, 56.6, 58.6, 1.5, 1, '#8a8174');
}

export function tunnel(ctx) {
    ellipse(ctx, 32, 56.6, 12.5, 4.8, DIRT);
    for (const l of [[25.5, 53.4, 2.6, 1.5], [31.5, 52.2, 3, 1.7], [38, 53, 2.4, 1.4]])
        ellipse(ctx, l[0], l[1], l[2], l[3], LUMP);
    line(ctx, 'M24.6 56l2 .8 1.6-.9M36 55.2l1.8.7', '#6c5241', 1);
}

// ---------------------------------------------------------------- body and face

export function body(ctx, pal) {
    path(ctx, BODY, pal.skin);
    // light from above, darker towards the burrow
    const g = ctx.createLinearGradient(0, 20, 0, 54);
    g.addColorStop(0, 'rgba(255,255,255,0.16)');
    g.addColorStop(0.45, 'rgba(255,255,255,0)');
    g.addColorStop(1, 'rgba(26,16,40,0.28)');
    ctx.beginPath();
    tracePath(ctx, BODY);
    ctx.fillStyle = g;
    ctx.fill();
}

export function feet(ctx, pal, angles) {
    for (let i = 0; i < 2; i++) {
        const x = i ? 40 : 24;
        ctx.save();
        about(ctx, x, 53.3, angles ? angles[i] : 0);
        if (angles && angles[2 + i])
            ctx.translate(0, angles[2 + i]);
        ellipse(ctx, x, 55.6, 4.4, 2.3, pal.shade);
        line(ctx, `M${x - 1.4} 55.9v1.4M${x + 1.4} 55.9v1.4`, pal.skin, 0.8, 0.85);
        ctx.restore();
    }
}

export function tail(ctx, pal) {
    line(ctx, 'M48.5 50.5Q57.5 51 57.6 43.6', pal.shade, 1.8);
    ctx.beginPath();
    traceEllipse(ctx, 57.6, 42, 2.7, 2.7);
    fill(ctx, pal.skin);
    stroke(ctx, pal.shade, 1.2);
}

export function plume(ctx, pal, lamp, glow, flicker) {
    line(ctx, 'M31.4 25.6Q29.6 18.8 34.4 14.6', pal.shade, 1.8);
    if (glow > 0) {
        ctx.save();
        about(ctx, 35.4, 12.4, 0, 0.85 + 0.3 * glow);
        circle(ctx, 35.4, 12.4, 5.8, lamp, 0.22 + 0.18 * glow);
        ctx.restore();
    }
    ctx.save();
    if (flicker < 1)
        ctx.globalAlpha = ctx.globalAlpha * flicker;
    ctx.beginPath();
    traceEllipse(ctx, 35.4, 12.4, 3.3, 3.3);
    fill(ctx, lamp);
    stroke(ctx, pal.shade, 0.8);
    ctx.restore();
    circle(ctx, 34.3, 11.3, 1, '#ffffff', 0.75);
}

const EYE_X = [25, 39], EYE_Y = 39;

function openEye(ctx, x, rx, ry, blink) {
    ctx.save();
    about(ctx, x, EYE_Y, 0, 1, blink);
    ellipse(ctx, x, EYE_Y, rx, ry, INK);
    circle(ctx, x + rx * 0.34, EYE_Y - ry * 0.4, rx * 0.38, '#ffffff');
    circle(ctx, x - rx * 0.36, EYE_Y + ry * 0.36, rx * 0.17, '#ffffff');
    ctx.restore();
}

// eyes: open wide tiny happy closed squint spiral star heart wink sad tired
export function eyes(ctx, pal, kind, look, blink, t) {
    const lx = look[0], ly = look[1];
    if (kind === 'open' || kind === 'sad' || kind === 'tired' || kind === 'wide') {
        const big = kind === 'wide';
        ctx.save();
        ctx.translate(lx, ly);
        for (const x of EYE_X)
            openEye(ctx, x, big ? 4.3 : 3.6, big ? 5.2 : 4.4, blink);
        ctx.restore();
        if (kind === 'sad') {
            path(ctx, 'M20.2 33.8H29.8V36.2L20.2 39.2ZM34.2 33.8H43.8V39.2L34.2 36.2Z', pal.skin);
            line(ctx, 'M20.2 39.2L29.8 36.2M34.2 36.2L43.8 39.2', pal.shade, 1);
        } else if (kind === 'tired') {
            path(ctx, 'M20.6 33.8h9v5.4h-9zM34.4 33.8h9v5.4h-9z', pal.skin);
            line(ctx, 'M20.8 39.2h8.6M34.6 39.2h8.6', pal.shade, 1);
        }
    } else if (kind === 'tiny') {
        for (const x of EYE_X)
            circle(ctx, x + lx, EYE_Y + ly, 1.5, INK);
    } else if (kind === 'happy') {
        line(ctx, 'M21.4 40.4q3.6-4.8 7.2 0M35.4 40.4q3.6-4.8 7.2 0', INK, 1.6);
    } else if (kind === 'closed') {
        line(ctx, 'M21.4 39q3.6 3.2 7.2 0M35.4 39q3.6 3.2 7.2 0', INK, 1.6);
    } else if (kind === 'squint') {
        line(ctx, 'M22.2 36.4l4.8 2.6-4.8 2.6M41.8 36.4l-4.8 2.6 4.8 2.6', INK, 1.6);
    } else if (kind === 'spiral') {
        for (const x of EYE_X) {
            ctx.save();
            about(ctx, x, EYE_Y, (t / 1000) * 360 % 360);
            line(ctx, `M${x - 2.8} ${EYE_Y}a2.8 2.8 0 1 0 5.6 0a1.9 1.9 0 1 0-3.8 0a1 1 0 1 0 2 0`, INK, 1.1);
            ctx.restore();
        }
    } else if (kind === 'star' || kind === 'heart') {
        for (const x of EYE_X) {
            ctx.save();
            ctx.translate(x, kind === 'star' ? EYE_Y : 38.4);
            ctx.scale(kind === 'star' ? 1.4 : 0.9, kind === 'star' ? 1.4 : 0.9);
            path(ctx, kind === 'star' ? STAR4 : HEART, kind === 'star' ? '#f5c542' : '#ff5c8a');
            ctx.restore();
        }
    } else if (kind === 'wink') {
        line(ctx, 'M21.4 40.4q3.6-4.8 7.2 0', INK, 1.6);
        ctx.save();
        ctx.translate(lx, ly);
        openEye(ctx, 39, 3.6, 4.4, blink);
        ctx.restore();
    }
}

// mouth: smile open o small frown wavy flat grin kiss yawn none
export function mouth(ctx, kind) {
    switch (kind) {
    case 'smile': line(ctx, 'M29.6 45.4q2.4 2.2 4.8 0', INK, 1.6); break;
    case 'open':
        path(ctx, 'M29 45q3 5 6 0z', INK);
        ellipse(ctx, 32, 48.1, 1.5, 0.8, '#ff8fa3');
        break;
    case 'o': ellipse(ctx, 32, 46.6, 1.7, 2.1, INK); break;
    case 'small': ellipse(ctx, 32, 46, 0.95, 1.05, INK); break;
    case 'frown': line(ctx, 'M29.6 47.4q2.4-2.2 4.8 0', INK, 1.6); break;
    case 'wavy': line(ctx, 'M28.4 46.6q1.2-1.3 2.4 0t2.4 0t2.4 0', INK, 1.1); break;
    case 'flat': line(ctx, 'M30.2 46.2h3.6', INK, 1.6); break;
    case 'grin':
        path(ctx, 'M27.6 44.6q4.4 6.4 8.8 0z', INK);
        ellipse(ctx, 32, 48.3, 2, 1, '#ff8fa3');
        break;
    case 'kiss': line(ctx, 'M31.2 44.6q1.8.8 0 1.6q1.8.8 0 1.6', INK, 1.1); break;
    case 'yawn':
        ellipse(ctx, 32, 47.2, 2.3, 3.2, INK);
        ellipse(ctx, 32, 49.2, 1.4, 0.8, '#ff8fa3');
        break;
    default: break;
    }
}

export function blush(ctx) {
    ellipse(ctx, 20.6, 45.2, 3, 1.7, '#ff7aa8', 0.42);
    ellipse(ctx, 43.4, 45.2, 3, 1.7, '#ff7aa8', 0.42);
}

// ---------------------------------------------------------------- arms and what they hold

export function arm(ctx, pal) {
    ctx.beginPath();
    traceEllipse(ctx, 0, 4.4, 3.3, 4.9);
    fill(ctx, pal.skin);
    stroke(ctx, pal.shade, 1.3);
}

// In the left arm's frame (pointing up when the arm is raised)
export function balloon(ctx, sway) {
    line(ctx, 'M0 8.6Q2.5 14 3.3 20.9', '#8a93ab', 0.8);
    ctx.save();
    about(ctx, 0, 26.6, -150);
    about(ctx, 0, 32.8, sway);
    ellipse(ctx, 0, 26.6, 5.2, 6.2, '#ff6b8b');
    path(ctx, 'M-1.2 33.4H1.2L0 32.6Z', '#e2506f');
    ellipse(ctx, -1.8, 24.4, 1.3, 2, '#ffffff', 0.55);
    ctx.restore();
}

// In the right arm's frame
export function sign(ctx) {
    line(ctx, 'M0 8.6L-2.6 22.5', '#8a6d3b', 1.5);
    ctx.save();
    about(ctx, 0, 27, 150);
    rrect(ctx, -7.5, 21.8, 15, 10.4, 2, '#f67400');
    line(ctx, 'M0 23.8v3.8', '#ffffff', 2);
    circle(ctx, 0, 30, 1, '#ffffff');
    ctx.restore();
}

// In the right arm's frame: a phone held up, its camera on you
export function selfie(ctx) {
    ctx.save();
    about(ctx, 0, 12.6, 155);
    rrect(ctx, -3, 8.4, 6, 9.6, 1.4, '#2b2f3a');
    rrect(ctx, -2.2, 9.6, 4.4, 6.6, 0.6, '#7fd1ff');
    circle(ctx, 0, 17, 0.55, '#d0d6e6');
    ctx.restore();
}

export function headphones(ctx) {
    line(ctx, 'M13.2 40C12.4 28 21.5 23.2 32 23.2S51.6 28 50.8 40', '#343a4a', 2.4);
    rrect(ctx, 9.8, 36, 5.4, 9, 2.4, '#e05a7a');
    rrect(ctx, 48.8, 36, 5.4, 9, 2.4, '#e05a7a');
}

// Props held behind the hands (drawn before the arms)
export function propBack(ctx, name, t, fx, sip, faceY) {
    if (name === 'mug') {
        ctx.save();
        if (sip > 0) {
            ctx.translate(-5 * sip, -3 * sip);
            about(ctx, 40.6, 47, -18 * sip);
        }
        for (let i = 0; i < 2; i++) {
            const p = ((t / 2200) + i * 0.5) % 1;
            ctx.save();
            ctx.translate(0, 2 - 7 * p);
            line(ctx, i ? 'M42 39.6q-1.4-1.8 0-3.6t0-3.6' : 'M38.6 39.6q-1.4-1.8 0-3.6t0-3.6', fx, 1.1, p < 0.3 ? p / 0.3 * 0.9 : (1 - p) / 0.7 * 0.9);
            ctx.restore();
        }
        rrect(ctx, 36.3, 42.2, 8.6, 9.6, 2, '#f6f1e7', '#b9ad98', 1);
        rrect(ctx, 37.4, 43.3, 6.4, 1.9, 0.9, '#7a4e2d');
        line(ctx, 'M44.9 44.4h1.3a2.3 2.3 0 0 1 0 4.6h-1.3', '#b9ad98', 1.4);
        ctx.restore();
    } else if (name === 'phone') {
        ctx.save();
        about(ctx, 45.2, 31.9, Math.sin(t / 25) * 9);
        rrect(ctx, 42.4, 27.2, 5.6, 9.4, 1.3, '#2b2f3a');
        rrect(ctx, 43.2, 28.4, 4, 6.4, 0.6, '#7fd1ff');
        ctx.restore();
    } else if (name === 'magnifier') {
        const p = (t % 2400) / 2400;
        const sx = p < 0.3 ? -1.6 * p / 0.3 : p < 0.65 ? -1.6 + 2.8 * (p - 0.3) / 0.35 : 1.2 * (1 - (p - 0.65) / 0.35);
        ctx.save();
        ctx.translate(sx, faceY + sx * -0.5);
        ellipse(ctx, 39, 38.6, 5.2, 6, INK);
        circle(ctx, 40.8, 36.2, 2.1, '#ffffff');
        circle(ctx, 37.4, 41, 1, '#ffffff');
        ctx.beginPath();
        traceEllipse(ctx, 39, 38.6, 6.6, 6.6);
        fill(ctx, 'rgba(190,225,255,0.28)');
        stroke(ctx, '#6b5b3e', 1.7);
        line(ctx, 'M43.6 43.4L45.6 47', '#6b5b3e', 2.2);
        ctx.restore();
    } else if (name === 'telescope') {
        // along one axis from the right eye: the narrow eyepiece at the eye,
        // wider tubes, the lens at the far end
        ctx.save();
        ctx.translate(41.2, 38.9 + faceY);
        ctx.rotate(-32.5 * Math.PI / 180);
        rrect(ctx, -0.6, -1.4, 4.4, 2.8, 0.5, '#8a6d3b');
        rrect(ctx, 3.2, -1.9, 8, 3.8, 0.6, '#c8a26b', '#8a6d3b', 0.6);
        rrect(ctx, 10.6, -2.5, 7.6, 5, 0.7, '#d4b07a', '#8a6d3b', 0.6);
        rrect(ctx, 17.6, -3.1, 2.8, 6.2, 0.8, '#8a6d3b');
        ellipse(ctx, 20.4, 0, 0.7, 2.6, '#bfe6ff');
        ctx.restore();
    }
}

// Props held in front (the hands are behind them)
export function propFront(ctx, name, t, since, opts) {
    opts = opts || {};
    if (name === 'laptop') {
        ellipse(ctx, 32, 40, 13, 6, '#8fd3ff', [0.22, 0.28, 0.34][Math.floor(t / 433) % 3]);
        rrect(ctx, 19.6, 42.4, 24.8, 12.2, 2, '#dfe3ec', '#a3abbd', 1);
        path(ctx, 'M31.2 50.2q-.5-2.3 1.4-3.4q.5 2.1-1.4 3.4z', '#b9c0cf');
        rrect(ctx, 16.4, 53.8, 31.2, 2.6, 1.3, '#a3abbd');
    } else if (name === 'book') {
        ctx.save();
        about(ctx, 32, 48.5, Math.sin(t / 3000 * 2 * Math.PI) * 2);
        rrect(ctx, 20.4, 42.4, 23.2, 12.2, 1.6, '#3f8f8a');
        rrect(ctx, 31.4, 42.4, 1.2, 12.2, 0, '#2f6f6b');
        line(ctx, 'M23 45.6h6M23 47.8h5M35 45.6h6M35 47.8h4.4', '#bfe3df', 0.9);
        ctx.restore();
    } else if (name === 'clipboard') {
        rrect(ctx, 22.2, 42, 19.6, 13.6, 1.6, '#c8a26b');
        rrect(ctx, 23.6, 43.6, 16.8, 11.2, 0.8, '#fffdf6');
        rrect(ctx, 28.6, 41.2, 6.8, 2.6, 1, '#8a8f9c');
        const rows = [45.2, 48.4, 51.6];
        rows.forEach((y, i) => {
            rrect(ctx, 25, y, 2.2, 2.2, 0.3, null, '#9aa3ba', 0.5);
            line(ctx, `M28.8 ${y + 1.1}h8.4`, '#c9cfdc', 0.8);
            if (since > 350 + i * 900) {
                if (i === 2 && opts.fail)
                    line(ctx, 'M25.2 51.8l2 2m0-2-2 2', '#da4453', 0.9);
                else
                    line(ctx, `M25.3 ${y + 1.1}l.8.8 1.6-1.8`, '#27ae60', 0.9);
            }
        });
    } else if (name === 'letter') {
        rrect(ctx, 21.6, 42.2, 20.8, 12.8, 1.2, '#fffaf0', '#c9b98f', 0.9);
        line(ctx, 'M21.8 42.6L32 49.6 42.2 42.6', '#c9b98f', 0.9);
        ctx.save();
        ctx.translate(32, 49.4);
        ctx.scale(0.42, 0.42);
        path(ctx, HEART, '#e05a7a');
        ctx.restore();
    } else if (name === 'cookie') {
        // bitten (opts.bite times) where the skin behind it shows, a little shaded
        circle(ctx, 32, 47.4, 5.6, '#d9a066');
        ctx.beginPath();
        traceEllipse(ctx, 32, 47.4, 5.6, 5.6);
        stroke(ctx, '#a8733e', 0.7);
        for (const c of [[29.6, 46.2], [33.6, 45.6], [31.4, 49.4], [34.8, 48.6]])
            circle(ctx, c[0], c[1], 0.75, '#5b3a22');
        const behind = mix(opts.skin || LILAC.skin, '#1a1028', 0.14);
        [[36.4, 43.6], [33, 41.9], [29.2, 42.6]].slice(0, Math.min(3, opts.bite || 0)).forEach(b => circle(ctx, b[0], b[1], 2.1, behind));
    } else if (name === 'gum') {
        const grow = Math.min(1, since / Math.max(1, opts.gum || 2000));
        ctx.save();
        about(ctx, 32, 47.6, 0, 0.12 + 0.88 * grow);
        circle(ctx, 32, 47.6, 7, '#ff8fbf', 0.92);
        ellipse(ctx, 29.6, 45.2, 1.6, 1, '#ffffff', 0.6);
        ctx.restore();
    }
}

export function think(ctx, t, fx) {
    [[47, 22, 1.5, 0], [52, 16.5, 2, 350], [58, 10.5, 2.8, 700]].forEach(d => {
        const p = ((t - d[3]) % 2600 + 2600) % 2600 / 2600;
        const a = p < 0.08 ? 0 : p < 0.22 ? (p - 0.08) / 0.14 : p < 0.78 ? 1 : p < 0.92 ? 1 - (p - 0.78) / 0.14 : 0;
        circle(ctx, d[0], d[1], d[2], fx, a);
    });
}

// Three balls looping from hand to hand
export function juggle(ctx, t) {
    const colors = ['#3daee9', '#27ae60', '#f28dbb'];
    colors.forEach((c, i) => {
        const p = ((t / 1500) + i / 3) % 1;
        const x = 17.2 + 29.6 * (p < 0.5 ? p * 2 : 2 - p * 2);
        let y;
        if (p < 0.25) {
            const q = p / 0.25;
            y = -30 * (1 - (1 - q) * (1 - q));
        } else if (p < 0.5) {
            const q = (p - 0.25) / 0.25;
            y = -30 * (1 - q * q);
        } else {
            y = 3 * Math.sin((p - 0.5) / 0.5 * Math.PI);
        }
        circle(ctx, x, 44 + y, 2.5, c);
    });
}

export function battery(ctx, t, fx) {
    rrect(ctx, 12.5, 9, 12.5, 7, 1.6, null, fx, 1.2);
    rrect(ctx, 25.6, 11.2, 1.6, 2.6, 0.5, fx);
    if (Math.floor(t / 550) % 2 === 0)
        rrect(ctx, 14, 10.5, 2.8, 4, 0.6, '#da4453');
}

export function orbit(ctx, t) {
    ctx.save();
    ctx.translate(32, 21);
    ctx.scale(1, 0.38);
    ctx.rotate((t / 1400) * 2 * Math.PI);
    for (const s of [[14, 0], [-7, 12.1], [-7, -12.1]]) {
        ctx.save();
        ctx.translate(s[0], s[1]);
        ctx.scale(1, 2.6);
        path(ctx, STAR4, '#f5c542');
        ctx.restore();
    }
    ctx.restore();
}

// ---------------------------------------------------------------- particles

// Drawn at the particle's spot, unmirrored
export function particle(ctx, kind, o, t, theme) {
    switch (kind) {
    case 'heart': path(ctx, HEART, o.color || '#ff6b9a'); break;
    case 'note':
        line(ctx, 'M2.2 0V-6.2l4.3-1.3V-1', theme.fx, 1.2);
        ellipse(ctx, 1.1, 0.1, 1.4, 1.05, theme.fx);
        ellipse(ctx, 5.4, -1, 1.4, 1.05, theme.fx);
        break;
    case 'z': text(ctx, 'z', 7, theme.fx); break;
    case 'text': text(ctx, o.text, o.size || 6, theme.text); break;
    case 'sparkle': path(ctx, STAR4, o.color || '#ffd54a'); break;
    case 'confetti': rrect(ctx, -0.9, -1.5, 1.8, 3, 0.4, o.color); break;
    case 'drop': path(ctx, 'M0-2q1.7 2.6 0 3.7q-1.7-1.1 0-3.7z', '#7cc4f2'); break;
    case 'cloud':
        for (const c of [[0, 0, 3], [3.4, -0.8, 2.4], [-3.2, -0.4, 2.2]]) {
            ctx.beginPath();
            traceEllipse(ctx, c[0], c[1], c[2], c[2]);
            fill(ctx, '#eef1f8');
            stroke(ctx, '#c4cbdc', 0.6);
        }
        break;
    case 'bubble': {
        const r = o.r || 1.6;
        ctx.beginPath();
        traceEllipse(ctx, 0, 0, r, r);
        fill(ctx, 'rgba(175,220,250,0.4)');
        stroke(ctx, '#9fd0f0', 0.6);
        circle(ctx, -r * 0.38, -r * 0.38, r * 0.28, '#ffffff', 0.85);
        break;
    }
    case 'bulb':
        circle(ctx, 0, 0, 3.6, '#ffe27a');
        rrect(ctx, -1.6, 3, 3.2, 2.2, 0.5, '#9aa3ba');
        line(ctx, 'M0-5.2v-1.8M4.4-3.6l1.3-1.2M-4.4-3.6l-1.3-1.2M5.4 0h1.8M-5.4 0h-1.8', '#f5b82e', 0.9);
        break;
    case 'butterfly': {
        const flap = 0.25 + 0.75 * Math.abs(Math.sin(t / 120 * Math.PI / 2));
        for (const side of [-1, 1]) {
            ctx.save();
            about(ctx, 0, 0, 0, flap, 1);
            ellipse(ctx, side * 2, 0, 2.2, 1.7, o.color || '#ffb347');
            ctx.restore();
        }
        rrect(ctx, -0.4, -1.6, 0.8, 3.2, 0.4, '#5a4a3a');
        break;
    }
    case 'plane':
        ctx.beginPath();
        tracePath(ctx, 'M-4 0L4-2.2-1 2.4ZM-1 2.4L0 .2 4-2.2');
        fill(ctx, '#fffaf0');
        stroke(ctx, '#9aa3ba', 0.5);
        break;
    case 'bug':
        ellipse(ctx, 0, 0, 1.8, 1.2, '#d94a4a');
        circle(ctx, -1.8, 0, 0.8, '#2b2f3a');
        line(ctx, 'M-1 1v1M0 1.1v1M1 1v1', '#2b2f3a', 0.4);
        break;
    case 'splat':
        for (const c of [[0, 0, 2.2], [3, -1, 1.2], [-3, 1, 1], [1, 3, 1]])
            circle(ctx, c[0], c[1], c[2], '#ff8fbf');
        break;
    case 'ring': line(ctx, 'M0-3a3 3 0 0 1 0 6M2-5a5.4 5.4 0 0 1 0 10', theme.fx, 1.2); break;
    case 'snow':
        ctx.beginPath();
        traceEllipse(ctx, 0, 0, 1, 1);
        fill(ctx, '#ffffff');
        stroke(ctx, '#cdd6e8', 0.3);
        break;
    case 'exclaim':
        circle(ctx, 0, 0, 4.2, '#f67400');
        line(ctx, 'M0-2.4v2.6', '#ffffff', 1.6);
        circle(ctx, 0, 2, 0.8, '#ffffff');
        break;
    case 'clod': ellipse(ctx, 0, 0, o.r || 1.1, (o.r || 1.1) * 0.75, o.color || '#8a6a4f'); break;
    case 'crumb': ellipse(ctx, 0, 0, 0.7, 0.55, '#b07a45'); break;
    case 'flower':   // from its foot, at the spot, up
        line(ctx, 'M0 0Q1.2-4 0-8.6', '#5f9e4a', 0.9);
        ellipse(ctx, 1.6, -3.6, 1.6, 0.7, '#6fb35a');
        ellipse(ctx, -1.5, -5.6, 1.4, 0.6, '#6fb35a');
        for (let i = 0; i < 5; i++) {
            const a = i * 2 * Math.PI / 5 - Math.PI / 2;
            circle(ctx, Math.cos(a) * 1.9, -10 + Math.sin(a) * 1.9, 1.45, o.color || '#ff8fbf');
        }
        circle(ctx, 0, -10, 1.1, '#ffd84a');
        break;
    case 'firefly': {
        const glow = 0.5 + 0.5 * Math.sin(t / 160);
        circle(ctx, 0, 0, 3.2, '#fff3a0', 0.12 + 0.22 * glow);
        circle(ctx, 0, 0, 1.6, '#fff3a0', 0.35 + 0.3 * glow);
        circle(ctx, 0, 0, 0.8, '#ffe766');
        break;
    }
    case 'shoot':
        line(ctx, 'M-9-1.4L0 0', '#ffe9a8', 1.2, 0.5);
        line(ctx, 'M-5-0.8L0 0', '#fff6d6', 1.4, 0.8);
        ctx.save();
        ctx.scale(0.8, 0.8);
        path(ctx, STAR4, '#ffe066');
        ctx.restore();
        break;
    case 'flash':
        for (let i = 0; i < 8; i++) {
            const a = i * Math.PI / 4;
            line(ctx, `M${Math.cos(a) * 2.2} ${Math.sin(a) * 2.2}L${Math.cos(a) * 5} ${Math.sin(a) * 5}`, o.color || '#ffffff', 1.1);
        }
        circle(ctx, 0, 0, 1.6, o.color || '#ffffff', 0.9);
        break;
    default: break;
    }
}

export function text(ctx, s, size, color) {
    ctx.save();
    ctx.font = `bold ${size}px sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = color;
    ctx.fillText(s, 0, 0);
    ctx.restore();
}
