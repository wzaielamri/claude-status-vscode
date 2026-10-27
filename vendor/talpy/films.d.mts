// Types for films.mjs, for the VS Code extension's webview (TypeScript)

import type { Still, Talpy } from "./talpy.mjs";

export interface FilmSheet {
    readonly index: number;
    readonly key: string;   // what's on it: a still's key, a picture in between, '' drawn live, '~' nothing yet
    paint: boolean;         // to draw (the host calls paint before the next frame shows)
    usedAt: number;
    readonly d: Still | null;
}

export class Films {
    constructor(talpy: Talpy, opts?: { sheets?: number; frameMs?: number; maxFrames?: number; restMs?: number; tweenMs?: number;
                                        tweens?: number; smooth?: boolean; clockMs?: number });
    on: boolean;        // false: drawn live (a pet you hold)
    frameMs: number;
    clockMs: number;    // about how far apart the host's ticks come (measured from them once there are a few)
    readonly sheets: FilmSheet[];
    readonly front: FilmSheet;
    readonly fx: { readonly on: boolean; paint: boolean };   // what flies about: shown while on, drawn again when paint
    reset(): void;
    tick(now: number): boolean;   // true when what shows changed
    paint(sheet: FilmSheet, ctx: unknown): void;
    paintFx(ctx: unknown): void;
}
