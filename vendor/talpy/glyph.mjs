// The panel icon: Talpy peeking up through the panel's edge, in one color
// (the state's), its eyes cut out so the panel shows through. A 24 × 24 box.
//   waiting: pops up, wide-eyed    working: glances around, lamp bobbing
//   idle: smiles (your turn)       none: asleep, sunk low, lamp drooping
//
// Plain ES2017 (see paths.mjs).

import { tracePath, traceEllipse } from './paths.mjs';

const BODY = 'M7 21C5.1 21 4.7 18.6 5 16.8C5.6 12.4 8.4 9.6 12 9.6S18.4 12.4 19 16.8C19.3 18.6 18.9 21 17 21Z';
const sine = p => 0.5 - 0.5 * Math.cos(Math.PI * p);
function kf(p, frames) {
    for (let i = 1; i < frames.length; i++)
        if (p <= frames[i][0]) {
            const a = frames[i - 1], b = frames[i];
            return a[1] + (b[1] - a[1]) * sine((p - a[0]) / Math.max(1e-6, b[0] - a[0]));
        }
    return frames[frames.length - 1][1];
}

// Whether the icon moves in this state (the host can stop its clock otherwise)
export function glyphMoves(state) {
    return state === 'waiting' || state === 'working';
}

export function drawGlyph(ctx, state, color, now) {
    now = now || 0;
    let dig = 0, lamp = 0, lookX = 0;
    if (state === 'none') {
        dig = 2.4;
        lamp = 50;
    } else if (state === 'idle') {
        dig = 1;
    } else if (state === 'working') {
        lookX = kf((now % 4500) / 4500, [[0, 0], [0.18, 0], [0.28, -0.9], [0.46, -0.9], [0.56, 0.9], [0.74, 0.9], [0.84, 0], [1, 0]]);
        lamp = kf((now % 1100) / 1100, [[0, -10], [0.5, 12], [1, -10]]);
    } else if (state === 'waiting') {
        dig = kf((now % 1100) / 1100, [[0, 0], [0.2, -2.4], [0.4, -0.4], [0.5, -1.2], [0.6, 0], [1, 0]]);
        lamp = -20;
    }
    ctx.save();
    ctx.fillStyle = color;
    ctx.strokeStyle = color;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    // everything above the edge
    ctx.save();
    ctx.beginPath();
    ctx.rect(-4, -8, 32, 27.4);
    ctx.clip();
    ctx.translate(0, dig);
    ctx.save();
    ctx.translate(11.7, 9.6);
    ctx.rotate(lamp * Math.PI / 180);
    ctx.translate(-11.7, -9.6);
    ctx.lineWidth = 1.1;
    ctx.beginPath();
    tracePath(ctx, 'M11.7 9.6Q11 6.9 12.9 5.4');
    ctx.stroke();
    ctx.beginPath();
    traceEllipse(ctx, 13.6, 4.4, 1.7, 1.7);
    ctx.fill();
    ctx.restore();
    ctx.beginPath();
    tracePath(ctx, BODY);
    ctx.fill();
    // eyes: holes through the body
    ctx.globalCompositeOperation = 'destination-out';
    if (state === 'none' || state === 'idle') {
        ctx.lineWidth = 1;
        ctx.beginPath();
        tracePath(ctx, state === 'none' ? 'M8 15.4q1.5 1.3 3 0M13 15.4q1.5 1.3 3 0' : 'M8 16q1.5-1.8 3 0M13 16q1.5-1.8 3 0');
        ctx.stroke();
    } else {
        const wide = state === 'waiting';
        ctx.beginPath();
        traceEllipse(ctx, 9.5 + lookX, wide ? 15 : 15.2, wide ? 1.8 : 1.5, wide ? 2.2 : 1.8);
        traceEllipse(ctx, 14.5 + lookX, wide ? 15 : 15.2, wide ? 1.8 : 1.5, wide ? 2.2 : 1.8);
        ctx.fill();
        ctx.globalCompositeOperation = 'source-over';
        ctx.beginPath();
        traceEllipse(ctx, 10 + lookX, 14.4, wide ? 0.6 : 0.5, wide ? 0.6 : 0.5);
        traceEllipse(ctx, 15 + lookX, 14.4, wide ? 0.6 : 0.5, wide ? 0.6 : 0.5);
        ctx.fill();
    }
    ctx.restore();
    // the panel's edge it peeks over
    ctx.globalCompositeOperation = 'source-over';
    ctx.lineWidth = 1.8;
    ctx.beginPath();
    ctx.moveTo(2, 19.9);
    ctx.lineTo(22, 19.9);
    ctx.stroke();
    ctx.restore();
}
