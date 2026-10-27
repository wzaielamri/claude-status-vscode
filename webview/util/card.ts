// The Recap as a card to share (WeekView.tsx: Share…), as the widget's
// RecapCard.qml draws it: the period shown with Talpy, Claude's and your
// time, prompts, chats, Claude's work day by day and the tokens, the
// projects named if you want. 1200 × 630 pixels (twice the widget's size),
// fixed colors: it's a picture, not part of the theme.

import { BOX, LILAC, Talpy } from "../../vendor/talpy/talpy.mjs";
import type { WeekRecap } from "../../src/shared/types";

export const CARD_W = 1200;
export const CARD_H = 630;

function hours(ms: number): string {
  const h = (ms || 0) / 3600000;
  return h >= 10 ? `${Math.round(h)} h` : h >= 1 ? `${h.toFixed(1)} h` : `${Math.round((ms || 0) / 60000)} min`;
}

function count(n: number): string {
  return n >= 1e9 ? `${(n / 1e9).toFixed(1)}B` : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k` : String(n || 0);
}

function dates(r: WeekRecap): string {
  const f = (d: string) => new Date(`${d}T12:00:00`).toLocaleDateString([], { day: "numeric", month: "short" });
  return r.from === r.to ? f(r.from) : `${f(r.from)} – ${f(r.to)}`;
}

export function drawCard(canvas: HTMLCanvasElement, r: WeekRecap, names: boolean): void {
  canvas.width = CARD_W;
  canvas.height = CARD_H;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const k = 2; // (the widget's card at twice its size)
  ctx.clearRect(0, 0, CARD_W, CARD_H);
  const g = ctx.createLinearGradient(0, 0, 0, CARD_H);
  g.addColorStop(0, "#1d2042");
  g.addColorStop(1, "#3b2e6c");
  ctx.fillStyle = g;
  ctx.beginPath();
  ctx.roundRect(0, 0, CARD_W, CARD_H, 18 * k);
  ctx.fill();
  ctx.fillStyle = "rgba(255,246,200,0.7)";
  for (const [x, y, rr] of [[30, 26, 2], [120, 60, 1.5], [200, 22, 2.2], [262, 92, 1.4], [70, 120, 1.6], [560, 30, 1.8], [470, 18, 1.2]]) {
    ctx.beginPath();
    ctx.arc((x + rr) * k, (y + rr) * k, rr * k, 0, Math.PI * 2);
    ctx.fill();
  }
  // Talpy, out of its burrow, happy
  const t = new Talpy({ brain: false, colors: LILAC, theme: { fx: "rgba(255,255,255,0.6)", text: "#ffffff" } });
  t.tick(1e6);
  t.pose({ depth: "high", arms: "up", eyes: "star", mouth: "grin", plume: "up", lamp: "done", blush: 1 });
  ctx.save();
  ctx.translate(25 * k, 80 * k);
  ctx.scale((200 * k) / BOX, (200 * k) / BOX);
  t.draw(ctx);
  ctx.restore();

  const x = 250 * k;
  const s = r.sum;
  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = "#ffffff";
  ctx.font = `bold ${25 * k}px system-ui, sans-serif`;
  ctx.fillText(r.span === "day" ? "My day with Claude" : r.span === "all" ? "Me and Claude, so far" : "My week with Claude", x, 52 * k);
  ctx.fillStyle = "#c9c4ee";
  ctx.font = `${13 * k}px system-ui, sans-serif`;
  ctx.fillText(dates(r), x, 74 * k);
  const tiles: [string, string][] = [[hours(s.workedMs), "Claude worked"], [hours(s.youMs), "with me"], [count(s.prompts), "prompts"],
                                     [count(s.chats ?? 0), "chats"]];
  tiles.forEach(([value, label], i) => {
    const tx = x + (i % 2) * 200 * k, ty = (122 + Math.floor(i / 2) * 58) * k;
    ctx.fillStyle = "#ffffff";
    ctx.font = `bold ${27 * k}px system-ui, sans-serif`;
    ctx.fillText(value, tx, ty);
    ctx.fillStyle = "#b9b4e0";
    ctx.font = `${11 * k}px system-ui, sans-serif`;
    ctx.fillText(label, tx, ty + 16 * k);
  });
  // Claude's work, day by day
  const days = r.days;
  if (days.length > 1) {
    const top = Math.max(1, ...days.map((d) => d.workedMs || 0));
    const w = Math.max(6, Math.min(22, 250 / days.length - 5));
    days.forEach((d, i) => {
      const h = 4 + (34 * (d.workedMs || 0)) / top;
      ctx.globalAlpha = 0.55 + (0.45 * (d.workedMs || 0)) / top;
      ctx.fillStyle = "#d97757";
      ctx.beginPath();
      ctx.roundRect(x + i * (w + 5) * k, (262 - h) * k, w * k, h * k, 3 * k);
      ctx.fill();
    });
    ctx.globalAlpha = 1;
  }
  const projects = r.projects.slice(0, 3);
  ctx.fillStyle = "#c9c4ee";
  ctx.font = `${12 * k}px system-ui, sans-serif`;
  const tail = projects.length ? ` · ${names ? projects.map((p) => p.name).join(", ") : `${r.projects.length} ${r.projects.length === 1 ? "project" : "projects"}`}` : "";
  let line = `${count(s.tokenTotal ?? 0)} tokens${tail}`;
  while (line.length > 4 && ctx.measureText(line).width > CARD_W - x - 30 * k) line = line.slice(0, -2) + "…";
  ctx.fillText(line, x, 286 * k);
  ctx.fillStyle = "#8f89c4";
  ctx.font = `${11 * k}px system-ui, sans-serif`;
  ctx.textAlign = "right";
  ctx.fillText("Talpy Den", CARD_W - 14 * k, CARD_H - 14 * k);
  ctx.textAlign = "left";
}
