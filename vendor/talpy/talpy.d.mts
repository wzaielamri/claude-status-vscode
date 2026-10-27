// Types for talpy.mjs, for the VS Code extension's webview (TypeScript)

export interface TalpyColors {
    skin: string;
    shade: string;
}

export interface TalpyTheme {
    fx: string;    // particles, z's, notes
    text: string;  // the words it says
}

export const BOX: number;
export const FLOOR: number;
export const TOP: number;
export const LILAC: TalpyColors;
export function calmColors(tint: string): TalpyColors;
export function drawHole(ctx: unknown, hole: number): void;   // an empty hole in the 64 × 64 box (hole: 0 to 1.1, as it opens or closes)
// the open hole's back or front, turned left or not, in the 64 × 64 box (for Talpy.holeImage)
export function drawHoleSide(ctx: unknown, part: "back" | "front", left: boolean): void;

// A film's picture of it (Talpy.still), see films.mjs
export interface Still {
    readonly key: string;
    readonly pose: string;
    readonly phase: number;
    readonly rest: boolean;
    readonly left: boolean;
    readonly every: number;   // ms from one picture of its move to the next (Infinity at rest)
}

export class Talpy {
    constructor(opts?: { colors?: TalpyColors; theme?: TalpyTheme; words?: (s: string) => string; travels?: boolean; brain?: boolean });
    onTravel: ((ms: number) => void) | null;
    facingLeft: boolean;
    travels: boolean;
    readonly busy: boolean;
    readonly frameMs: number;   // how often a picture is worth drawing now: 40 (quick moves), 80 (slow) or 200 (at rest)
    readonly run: unknown;
    readonly now: number;
    readonly settled: boolean;   // what's eased between poses is where it's going
    // the open hole's back or front drawn by drawHoleSide on a sheet the size of the canvas, at the
    // canvas's transform: copied in (half of a picture's cost); null: drawn
    holeImage: ((part: "back" | "front", left: boolean) => CanvasImageSource) | null;
    setColors(colors: TalpyColors): void;
    setTheme(theme: TalpyTheme): void;
    setMood(mood: string): void;
    react(name: string, done?: ((ok: boolean) => void) | null): void;
    setHeld(held: boolean): number;   // how much of its hole it leaves behind (0 to 1): draw that where it was, drawHole
    land(height: number, done?: ((ok: boolean) => void) | null): "sick" | "dizzy" | "fine";   // height in box units
    pose(step?: Record<string, unknown>): void;
    tick(now: number): void;
    draw(ctx: unknown, withoutParticles?: boolean): void;
    drawParticles(ctx: unknown, now: number): void;
    particles(now: number): 0 | 1 | 2;   // nothing flies about, only z's, quicker things
    still(frameMs: number, maxFrames: number, clockMs?: number): Still | null;
    drawFilm(ctx: unknown, d: Still, from: Still | null, p: number): void;
    // a picture instead of every frame: what it shows ("pose#details": fade over to a new pose), and the picture
    stillKey(): string;
    drawStill(ctx: unknown): void;
}
