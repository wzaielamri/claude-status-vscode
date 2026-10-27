// SVG path data → a few canvas calls (moveTo, lineTo, bezierCurveTo,
// closePath), so Talpy's shapes draw the same on an HTML canvas, a QML Canvas
// and Cairo. Arcs become cubic curves. Parsed paths are cached.
//
// Plain ES2017: this file also runs in Qt's and GNOME Shell's JavaScript
// engines (no optional chaining, no class fields).

const cache = new Map();

function tokens(d) {
    const out = [];
    const re = /([MmLlHhVvCcSsQqTtAaZz])|(-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)/g;
    let m;
    while ((m = re.exec(d)) !== null)
        out.push(m[1] ? m[1] : parseFloat(m[2]));
    return out;
}

// One elliptical arc as cubic curves (SVG implementation notes, F.6)
function arcToCubics(x1, y1, rx, ry, phiDeg, largeArc, sweep, x2, y2, out) {
    if (rx === 0 || ry === 0) {
        out.push(['L', x2, y2]);
        return;
    }
    const phi = phiDeg * Math.PI / 180, cos = Math.cos(phi), sin = Math.sin(phi);
    const dx = (x1 - x2) / 2, dy = (y1 - y2) / 2;
    const x1p = cos * dx + sin * dy, y1p = -sin * dx + cos * dy;
    rx = Math.abs(rx);
    ry = Math.abs(ry);
    const lambda = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry);
    if (lambda > 1) {
        rx *= Math.sqrt(lambda);
        ry *= Math.sqrt(lambda);
    }
    const num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p;
    const den = rx * rx * y1p * y1p + ry * ry * x1p * x1p;
    let coef = Math.sqrt(Math.max(0, num / den));
    if (largeArc === sweep)
        coef = -coef;
    const cxp = coef * (rx * y1p / ry), cyp = coef * -(ry * x1p / rx);
    const cx = cos * cxp - sin * cyp + (x1 + x2) / 2, cy = sin * cxp + cos * cyp + (y1 + y2) / 2;
    const angle = (ux, uy, vx, vy) => {
        const a = Math.atan2(ux * vy - uy * vx, ux * vx + uy * vy);
        return a;
    };
    const t1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry);
    let dt = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry);
    if (!sweep && dt > 0)
        dt -= 2 * Math.PI;
    else if (sweep && dt < 0)
        dt += 2 * Math.PI;
    const segments = Math.max(1, Math.ceil(Math.abs(dt) / (Math.PI / 2)));
    const step = dt / segments, k = 4 / 3 * Math.tan(step / 4);
    let a = t1;
    for (let i = 0; i < segments; i++) {
        const b = a + step;
        const ca = Math.cos(a), sa = Math.sin(a), cb = Math.cos(b), sb = Math.sin(b);
        const p = (ex, ey) => [cos * rx * ex - sin * ry * ey + cx, sin * rx * ex + cos * ry * ey + cy];
        const c1 = p(ca - k * sa, sa + k * ca), c2 = p(cb + k * sb, sb - k * cb), e = p(cb, sb);
        out.push(['C', c1[0], c1[1], c2[0], c2[1], e[0], e[1]]);
        a = b;
    }
}

// Absolute commands: ['M', x, y] ['L', x, y] ['C', x1, y1, x2, y2, x, y] ['Z']
export function parsePath(d) {
    const hit = cache.get(d);
    if (hit)
        return hit;
    const t = tokens(d), out = [];
    let i = 0, cmd = '', x = 0, y = 0, sx = 0, sy = 0, lcx = 0, lcy = 0, lqx = 0, lqy = 0, prev = '';
    const num = () => t[i++];
    while (i < t.length) {
        if (typeof t[i] === 'string')
            cmd = t[i++];
        else if (cmd === 'M')
            cmd = 'L';
        else if (cmd === 'm')
            cmd = 'l';
        const rel = cmd === cmd.toLowerCase(), C = cmd.toUpperCase();
        const ox = rel ? x : 0, oy = rel ? y : 0;
        if (C === 'M') {
            x = ox + num();
            y = oy + num();
            sx = x;
            sy = y;
            out.push(['M', x, y]);
        } else if (C === 'L') {
            x = ox + num();
            y = oy + num();
            out.push(['L', x, y]);
        } else if (C === 'H') {
            x = ox + num();
            out.push(['L', x, y]);
        } else if (C === 'V') {
            y = oy + num();
            out.push(['L', x, y]);
        } else if (C === 'C') {
            const x1 = ox + num(), y1 = oy + num(), x2 = ox + num(), y2 = oy + num();
            x = ox + num();
            y = oy + num();
            out.push(['C', x1, y1, x2, y2, x, y]);
            lcx = x2;
            lcy = y2;
        } else if (C === 'S') {
            const r = prev === 'C' || prev === 'S';
            const x1 = r ? 2 * x - lcx : x, y1 = r ? 2 * y - lcy : y;
            const x2 = ox + num(), y2 = oy + num();
            x = ox + num();
            y = oy + num();
            out.push(['C', x1, y1, x2, y2, x, y]);
            lcx = x2;
            lcy = y2;
        } else if (C === 'Q' || C === 'T') {
            let qx, qy;
            if (C === 'Q') {
                qx = ox + num();
                qy = oy + num();
            } else {
                const r = prev === 'Q' || prev === 'T';
                qx = r ? 2 * x - lqx : x;
                qy = r ? 2 * y - lqy : y;
            }
            const ex = ox + num(), ey = oy + num();
            out.push(['C', x + 2 / 3 * (qx - x), y + 2 / 3 * (qy - y), ex + 2 / 3 * (qx - ex), ey + 2 / 3 * (qy - ey), ex, ey]);
            lqx = qx;
            lqy = qy;
            x = ex;
            y = ey;
        } else if (C === 'A') {
            const rx = num(), ry = num(), phi = num(), large = num(), sweep = num();
            const ex = ox + num(), ey = oy + num();
            arcToCubics(x, y, rx, ry, phi, !!large, !!sweep, ex, ey, out);
            x = ex;
            y = ey;
        } else if (C === 'Z') {
            out.push(['Z']);
            x = sx;
            y = sy;
        } else {
            i++;   // unknown: skip a token rather than loop forever
        }
        prev = C;
    }
    cache.set(d, out);
    return out;
}

// Adds a path to the context's current path
// map(x, y) → [x, y]: moves every point (an affine map keeps the curves
// exact); join: the path goes on from the current point (its M as a line)
export function tracePath(ctx, d, map, join) {
    const at = map || ((x, y) => [x, y]);
    let first = true;
    for (const c of parsePath(d)) {
        if (c[0] === 'M' || c[0] === 'L') {
            const p = at(c[1], c[2]);
            if (c[0] === 'M' && !(join && first))
                ctx.moveTo(p[0], p[1]);
            else
                ctx.lineTo(p[0], p[1]);
        } else if (c[0] === 'C') {
            const a = at(c[1], c[2]), b = at(c[3], c[4]), e = at(c[5], c[6]);
            ctx.bezierCurveTo(a[0], a[1], b[0], b[1], e[0], e[1]);
        } else {
            ctx.closePath();
        }
        first = false;
    }
}

// An ellipse as four cubic curves (QML's Canvas ellipse() differs from HTML's)
export function traceEllipse(ctx, cx, cy, rx, ry) {
    const k = 0.5523;
    ctx.moveTo(cx + rx, cy);
    ctx.bezierCurveTo(cx + rx, cy + ry * k, cx + rx * k, cy + ry, cx, cy + ry);
    ctx.bezierCurveTo(cx - rx * k, cy + ry, cx - rx, cy + ry * k, cx - rx, cy);
    ctx.bezierCurveTo(cx - rx, cy - ry * k, cx - rx * k, cy - ry, cx, cy - ry);
    ctx.bezierCurveTo(cx + rx * k, cy - ry, cx + rx, cy - ry * k, cx + rx, cy);
    ctx.closePath();
}

export function traceRoundRect(ctx, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    if (r <= 0) {
        ctx.moveTo(x, y);
        ctx.lineTo(x + w, y);
        ctx.lineTo(x + w, y + h);
        ctx.lineTo(x, y + h);
        ctx.closePath();
        return;
    }
    const k = 0.5523 * r;
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.bezierCurveTo(x + w - r + k, y, x + w, y + r - k, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.bezierCurveTo(x + w, y + h - r + k, x + w - r + k, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.bezierCurveTo(x + r - k, y + h, x, y + h - r + k, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.bezierCurveTo(x, y + r - k, x + r - k, y, x + r, y);
    ctx.closePath();
}
