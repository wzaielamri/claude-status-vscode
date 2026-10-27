// Talpy, the pet (the widget's talpy.mjs, vendored), on the stages ported
// from the widget's PetStrip.qml and Pet.qml. The UI talks to the pets only
// through this module (see api.ts).
//
// A stage starts live: it pauses by itself while it's off screen or the page
// is hidden, and whenever the UI says so (setLive(false)).

import type { PetsModule } from "./api";
import { onColorsChanged, petColor } from "./colors";
import { Stage, allStages, setDarkAll } from "./stage";

export const pets: PetsModule = {
  createHeroStage: (el, cb) => new Stage(el, true, "hero", cb),
  createAccountStage: (el, stageKey, cb) => new Stage(el, false, `account:${stageKey}`, cb),
  dispatchPetEvent(kind, text, key) {
    for (const stage of [...allStages]) stage.petEvent(kind, text, key ?? "");
  },
  petColor,
  onColorsChanged,
  setDark: setDarkAll,
};
