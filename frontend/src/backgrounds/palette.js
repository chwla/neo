/**
 * The theme, in a form a canvas can use.
 *
 * The stylesheet's palette is two tiers: fifteen raw anchors, and about ninety
 * tokens `color-mix()`ed from them. Only the first tier is readable from here.
 * Custom properties resolve lazily, so `getPropertyValue("--neo-accent-a20")`
 * hands back the literal string "color-mix(in srgb, var(--neo-accent) 20%,
 * transparent)" rather than a colour -- useless to `fillStyle`. So this reads
 * the three anchors it needs as hex and does its own alpha arithmetic.
 *
 * Nothing here hard-codes white or black. Two themes make that a correctness
 * issue rather than a preference: Mono's accent is a near-white #e8e8e8, and
 * Paper is a light theme on #faf8f4, where an effect drawn to glow additively
 * over a dark ground disappears entirely.
 */

//: Everything the effects are allowed to draw with. Kept to three because each
//: one is an anchor every theme is already test-asserted to declare -- a
//: derived token would read back unresolved.
const ACCENT = "--neo-accent";
const BG = "--neo-bg";
const INK = "--neo-ink";

//: What the picker falls back to if the document has no computed style yet,
//: which is the case under `renderToStaticMarkup` in the tests.
const FALLBACK = { accent: [57, 255, 20], bg: [10, 10, 10], ink: [255, 255, 255] };

/**
 * `#abc` and `#aabbcc` both appear in the stylesheet -- the default theme's ink
 * is written `#fff`. Anything else reads as null so the caller can fall back
 * rather than draw in NaN, which paints nothing and gives no clue why.
 */
export function parseHex(value) {
  const hex = String(value || "").trim().replace(/^#/, "");
  if (hex.length === 3) {
    const [r, g, b] = hex;
    const parsed = Number.parseInt(`${r}${r}${g}${g}${b}${b}`, 16);
    return Number.isNaN(parsed) ? null : [(parsed >> 16) & 255, (parsed >> 8) & 255, parsed & 255];
  }
  if (hex.length === 6) {
    const parsed = Number.parseInt(hex, 16);
    return Number.isNaN(parsed) ? null : [(parsed >> 16) & 255, (parsed >> 8) & 255, parsed & 255];
  }
  return null;
}

/** Relative luminance, sRGB. Used only to answer "is the ground light?". */
export function luminance([r, g, b]) {
  const channel = (raw) => {
    const c = raw / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

export function rgba([r, g, b], alpha) {
  //: Clamped rather than trusted: intensity is a multiplier, so Vivid can push
  //: an effect's own alpha past 1, and a canvas given 1.4 silently draws
  //: nothing in some engines instead of drawing fully opaque.
  const a = Math.max(0, Math.min(1, alpha));
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}

/**
 * Read the anchors off the document.
 *
 * Called once when an effect starts and again whenever `data-theme` changes,
 * which the engine watches for. Cheap enough at that rate -- it is three
 * property reads, not a per-frame cost.
 */
export function readPalette(root) {
  const element = root || (typeof document === "undefined" ? null : document.documentElement);
  let accent = null;
  let bg = null;
  let ink = null;

  if (element && typeof getComputedStyle === "function") {
    const style = getComputedStyle(element);
    accent = parseHex(style.getPropertyValue(ACCENT));
    bg = parseHex(style.getPropertyValue(BG));
    ink = parseHex(style.getPropertyValue(INK));
  }

  accent = accent || FALLBACK.accent;
  bg = bg || FALLBACK.bg;
  ink = ink || FALLBACK.ink;
  const isLight = luminance(bg) > 0.5;

  return {
    accent,
    bg,
    ink,
    isLight,
    rgba,
    //: Additive light is what makes a glow read on a dark ground, and it is
    //: also what makes one invisible on a light one, since screening toward
    //: white on near-white moves nothing. Effects that glow ask for this;
    //: effects that are better as flat shapes ignore it and draw source-over.
    glowMode: isLight ? "source-over" : "lighter",
  };
}
