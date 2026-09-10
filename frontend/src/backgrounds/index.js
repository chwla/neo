/**
 * The chat backgrounds Neo ships with.
 *
 * Data and nothing else, on the same reasoning as `themes.js`: adding a
 * background is one object literal here plus one effect module in this folder,
 * with no component to touch. The module owns the drawing -- these sentences
 * are only what the picker reads out, so a background can never be half-added
 * (an entry here with no module there shows up as a failing test rather than as
 * a card that turns the panel blank).
 *
 * `id` is what the profile database stores and what selects the effect module,
 * so the ids are part of the API and should not be renamed once a profile might
 * hold them. They stay lowercase letters only, because the test that holds this
 * catalogue and `app/services/appearance.py` together reads them out of this
 * file with a regular expression.
 */

//: Motion is off unless it is asked for, and "off" has to be a real entry in
//: the catalogue rather than the absence of one, so the picker has something to
//: select and a profile has something to go back to. Nothing is mounted for it:
//: `ChatBackground` renders null, so no canvas and no engine exist.
export const DEFAULT_BACKGROUND_ID = "none";

export const BACKGROUNDS = [
  {
    id: "none",
    name: "None",
    description: "No motion. The panel stays the flat colour of the theme.",
  },
  {
    id: "jellyfish",
    name: "Jellyfish",
    description: "Translucent bells drifting upward, trailing tentacles behind them.",
  },
  {
    id: "stars",
    name: "Shooting stars",
    description: "A slow field of stars, and every so often one falls across it.",
  },
  {
    id: "rain",
    name: "Rain",
    description: "Fine rain leaning with the wind, near drops falling faster than far ones.",
  },
  {
    id: "gradient",
    name: "Gradient",
    description: "Fine lines flowing in ribbons, gathering into a bright core where they cross.",
  },
];

//: Multipliers the effects apply to their own tuned values, not absolute
//: numbers. What reads as a whisper on Ice is a smear on Paper, so the strength
//: has to be the reader's choice rather than one constant per effect.
export const DEFAULT_INTENSITY_ID = "medium";

export const INTENSITIES = [
  { id: "subtle", name: "Subtle", alpha: 0.55, density: 0.6 },
  { id: "medium", name: "Medium", alpha: 1, density: 1 },
  { id: "vivid", name: "Vivid", alpha: 1.6, density: 1.4 },
];

const BY_ID = new Map(BACKGROUNDS.map((background) => [background.id, background]));
const INTENSITY_BY_ID = new Map(INTENSITIES.map((entry) => [entry.id, entry]));

/** Undefined for an id we no longer ship, which callers treat as the default. */
export function backgroundById(id) {
  return BY_ID.get(id);
}

export function isBackgroundId(id) {
  return BY_ID.has(id);
}

/** Always a usable pair of multipliers, so an unknown id cannot stop the draw. */
export function intensityById(id) {
  return INTENSITY_BY_ID.get(id) || INTENSITY_BY_ID.get(DEFAULT_INTENSITY_ID);
}

export function isIntensityId(id) {
  return INTENSITY_BY_ID.has(id);
}

/**
 * Put the background on the document.
 *
 * The canvas is driven by a prop rather than by this attribute -- it exists so
 * the stylesheet can tell that something is moving behind the transcript and
 * give the empty state a ground to sit on. As with `applyTheme`, "none" is
 * expressed by removing the attribute rather than by naming itself, so there is
 * exactly one selector for "no background" and it is the absence of one.
 */
export function applyBackground(id, root = document.documentElement) {
  if (id && id !== DEFAULT_BACKGROUND_ID && BY_ID.has(id)) {
    root.dataset.chatBg = id;
  } else {
    delete root.dataset.chatBg;
  }
}
