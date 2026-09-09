/**
 * The themes Neo ships with.
 *
 * Data and nothing else, on the same reasoning as `keys/commands.js`: adding a
 * theme is one object literal here plus one `[data-theme]` block in index.css,
 * with no component to touch. The stylesheet owns the actual palette -- these
 * swatches are only what the picker draws, so a theme can never be half-added
 * (a swatch here with no block there shows up as a failing test rather than as
 * a card that does nothing).
 *
 * `id` is what goes in `document.documentElement.dataset.theme` and what the
 * profile database stores, so the ids are part of the API and should not be
 * renamed once a profile might hold them.
 */

//: No `data-theme` attribute at all means this one, so that a profile which has
//: never chosen a theme, and every screen shown before the profile is known,
//: render in the palette Neo has always had.
export const DEFAULT_THEME_ID = "default";

export const THEMES = [
  {
    id: "default",
    name: "Default",
    description: "Phosphor green on ink black. Neo's original palette.",
    swatch: ["#39ff14", "#0a0a0a", "#111111", "#252525", "#e8e8e8"],
  },
  {
    id: "cyberpunk",
    name: "Cyberpunk",
    description: "Magenta and cyan neon over navy, edged in purple.",
    swatch: ["#d42ac6", "#55b3c4", "#6d2a8c", "#21386b", "#101a2d"],
  },
  {
    id: "amber",
    name: "Amber CRT",
    description: "The warm phosphor of a vintage terminal.",
    swatch: ["#ffb000", "#0d0904", "#17100a", "#3d2e18", "#ffeecc"],
  },
  {
    id: "ice",
    name: "Ice",
    description: "Cold blue on near-black. The quiet one.",
    swatch: ["#38bdf8", "#060a0f", "#0c131c", "#263444", "#eaf4ff"],
  },
  {
    id: "mono",
    name: "Mono",
    description: "No accent hue at all. Status colours stay, everything else is grey.",
    swatch: ["#e8e8e8", "#0a0a0a", "#111111", "#2b2b2b", "#7a9a70"],
  },
  {
    id: "indigo",
    name: "Indigo",
    description: "Periwinkle on neutral graphite — the one dark theme that isn't near-black.",
    swatch: ["#7c83ff", "#16161c", "#202029", "#464657", "#e8e9f7"],
  },
  {
    id: "crimson",
    name: "Crimson",
    description: "Deep red on black. Errors shift to amber so they still read as errors.",
    swatch: ["#ff2d55", "#0d0505", "#170a0a", "#3d1f1f", "#ffe9ec"],
  },
  {
    id: "paper",
    name: "Paper",
    description: "The light one: ink on warm off-white, for working in daylight.",
    swatch: ["#1f7a3d", "#faf8f4", "#ffffff", "#cfc8b8", "#14140f"],
  },
];

const BY_ID = new Map(THEMES.map((theme) => [theme.id, theme]));

/** Undefined for an id we no longer ship, which callers treat as the default. */
export function themeById(id) {
  return BY_ID.get(id);
}

export function isThemeId(id) {
  return BY_ID.has(id);
}

/**
 * Put a theme on the document. Called before the first paint of the real UI
 * (App.jsx resolves it inside the same gate that already waits on the profile
 * session) and again on every change from the picker.
 *
 * The default is expressed by removing the attribute rather than by setting
 * `data-theme="default"`, so `:root` alone styles it and there is exactly one
 * place the default palette lives.
 */
export function applyTheme(id, root = document.documentElement) {
  if (id && id !== DEFAULT_THEME_ID && BY_ID.has(id)) {
    root.dataset.theme = id;
  } else {
    delete root.dataset.theme;
  }
}
