import { pets } from "../pets";
import type { HeroInput } from "../pets/api";
import { stageCallbacks, usePetStage } from "../util/petStage";

// The big Talpy: the whole dashboard at a glance
export function HeroStage({ input }: { input: HeroInput }) {
  const ref = usePetStage((el) => pets.createHeroStage(el, stageCallbacks), input, true);
  return <div class="hero" ref={ref} />;
}
