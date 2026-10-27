// Types for pictures.mjs, for the VS Code extension's webview (TypeScript)

import type { Talpy } from "./talpy.mjs";

export interface Sheet {
    readonly index: number;
    key: string;      // what's on it: a stillKey, '' drawn live, '~' nothing yet
    ready: boolean;   // drawn
    paint: boolean;   // to draw (the host clears it and calls paint)
    usedAt: number;
}

export class Pictures {
    constructor(talpy: Talpy, count: number);
    pictures: boolean;   // false: drawn live
    fadeMs: number;      // 0: a new pose just shows
    bigMs: number;       // a big move is drawn at most this often (with pictures on)
    slackMs: number;     // how much early a tick may come and still draw
    readonly sheets: Sheet[];
    readonly front: Sheet;
    readonly coming: Sheet | null;   // fading in over the front: (1 - mix) × front + mix × coming, added up
    readonly mix: number;
    reset(): void;
    tick(now: number): boolean;      // true when what shows changed
    paint(sheet: Sheet, ctx: unknown): void;
}
