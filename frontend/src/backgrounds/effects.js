/**
 * Which module draws which background.
 *
 * Separate from `index.js` so that the catalogue stays importable without
 * pulling in any drawing code -- the picker and both test suites read the
 * catalogue, and none of them wants four canvas modules loaded to do it.
 *
 * "none" has no entry here on purpose. It is not an effect that draws nothing;
 * it is the absence of an effect, and `ChatBackground` returns null for it so
 * no canvas and no engine are ever created.
 */

import jellyfish from "./jellyfish.js";
import rain from "./rain.js";
import stars from "./stars.js";
import waves from "./waves.js";

const MODULES = [jellyfish, stars, rain, waves];

const BY_ID = new Map(MODULES.map((module) => [module.id, module]));

/** Undefined for "none" and for any id whose module has been removed. */
export function effectById(id) {
  return BY_ID.get(id);
}

export const EFFECT_IDS = MODULES.map((module) => module.id);
