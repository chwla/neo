/**
 * Sketch strokes: how a drawing is kept in a note, and how it is drawn.
 *
 * Coordinates are in the sketch's own units: its width is always 10000 and its
 * height 10000 x aspect, so a drawing keeps its shape at any size and through
 * any rotation of the screen. A stroke is
 *   { tool: "pen" | "marker" | "pencil", color, size: 1 | 2 | 3, points }
 * where `points` is an encoded string: each point (x, y, pressure 0-100) as the
 * difference from the one before, zigzag varints, base64url. A long stroke of
 * a few hundred points is a few hundred bytes.
 *
 * Decoding is strict: anything malformed is refused, not drawn half.
 */
import { getStroke } from "perfect-freehand";

export const SKETCH_WIDTH = 10000;
export const MAX_STROKES = 2000;
export const MAX_POINTS = 5000;
export const TOOLS = ["pen", "marker", "pencil"];
export const SKETCH_COLORS = ["ink", "purple", "pink", "orange", "mint", "blue"];
export const SIZES = [1, 2, 3];

/** Each tool's look: base width (sketch units, times the size), how pressure thins it, opacity. */
export const TOOL_STYLE = {
  pen: { width: 70, thinning: 0.6, smoothing: 0.5, streamline: 0.5, opacity: 1 },
  marker: { width: 260, thinning: 0, smoothing: 0.6, streamline: 0.6, opacity: 0.4 },
  pencil: { width: 45, thinning: 0.3, smoothing: 0.3, streamline: 0.3, opacity: 0.8 },
};

export function colorValue(color) {
  return color === "ink" || !SKETCH_COLORS.includes(color) ? "var(--neo-text)" : `var(--nw-ink-${color})`;
}

const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
const LOOKUP = Object.fromEntries([...ALPHABET].map((char, index) => [char, index]));

function toBase64Url(bytes) {
  let out = "";
  for (let i = 0; i < bytes.length; i += 3) {
    const [a, b = 0, c = 0] = [bytes[i], bytes[i + 1], bytes[i + 2]];
    const n = (a << 16) | (b << 8) | c;
    out += ALPHABET[(n >> 18) & 63] + ALPHABET[(n >> 12) & 63];
    if (i + 1 < bytes.length) out += ALPHABET[(n >> 6) & 63];
    if (i + 2 < bytes.length) out += ALPHABET[n & 63];
  }
  return out;
}

function fromBase64Url(text) {
  if (!/^[A-Za-z0-9_-]*$/.test(text) || text.length % 4 === 1) throw new Error("not a stroke encoding");
  const bytes = [];
  for (let i = 0; i < text.length; i += 4) {
    const chunk = text.slice(i, i + 4);
    const values = [...chunk].map((char) => LOOKUP[char]);
    const n = (values[0] << 18) | (values[1] << 12) | ((values[2] ?? 0) << 6) | (values[3] ?? 0);
    bytes.push((n >> 16) & 255);
    if (chunk.length > 2) bytes.push((n >> 8) & 255);
    if (chunk.length > 3) bytes.push(n & 255);
  }
  return bytes;
}

const zigzag = (n) => (n >= 0 ? n * 2 : -n * 2 - 1);
const unzigzag = (n) => (n % 2 === 0 ? n / 2 : -(n + 1) / 2);

/** Points [[x, y, pressure], ...] (sketch units; pressure 0-1) as a string. */
export function encodePoints(points) {
  const bytes = [];
  let previous = [0, 0, 0];
  for (const [x, y, pressure = 0.5] of points.slice(0, MAX_POINTS)) {
    const current = [Math.round(x), Math.round(y), Math.round(Math.min(1, Math.max(0, pressure)) * 100)];
    for (let axis = 0; axis < 3; axis += 1) {
      let value = zigzag(current[axis] - previous[axis]);
      do {
        let byte = value & 127;
        value = Math.floor(value / 128);
        if (value) byte |= 128;
        bytes.push(byte);
      } while (value);
    }
    previous = current;
  }
  return toBase64Url(bytes);
}

/** The points of an encoded string, or an error for anything malformed. */
export function decodePoints(text, aspect = 1) {
  const bytes = fromBase64Url(String(text));
  const values = [];
  let value = 0;
  let shift = 1;
  for (const byte of bytes) {
    value += (byte & 127) * shift;
    if (byte & 128) {
      shift *= 128;
      if (shift > 2 ** 35) throw new Error("not a stroke encoding");
    } else {
      values.push(unzigzag(value));
      value = 0;
      shift = 1;
    }
  }
  if (shift !== 1 || values.length % 3 !== 0 || values.length / 3 > MAX_POINTS) throw new Error("not a stroke encoding");
  const points = [];
  const limit = [SKETCH_WIDTH, SKETCH_WIDTH * aspect, 100];
  let current = [0, 0, 0];
  for (let i = 0; i < values.length; i += 3) {
    current = [current[0] + values[i], current[1] + values[i + 1], current[2] + values[i + 2]];
    if (current.some((coordinate, axis) => coordinate < -limit[axis] * 0.05 || coordinate > limit[axis] * 1.05)) {
      throw new Error("a point lies outside the sketch");
    }
    points.push([current[0], current[1], current[2] / 100]);
  }
  return points;
}

/** A stroke checked and normalised, or null if it can't be one. */
export function validStroke(stroke, aspect = 1) {
  if (!stroke || typeof stroke !== "object") return null;
  const tool = TOOLS.includes(stroke.tool) ? stroke.tool : null;
  const size = SIZES.includes(stroke.size) ? stroke.size : null;
  if (!tool || !size || typeof stroke.points !== "string") return null;
  try {
    if (!decodePoints(stroke.points, aspect).length) return null;
  } catch {
    return null;
  }
  return { tool, color: SKETCH_COLORS.includes(stroke.color) ? stroke.color : "ink", size, points: stroke.points };
}

/** The outline of a stroke as SVG path data, in sketch units. */
export function strokePath(stroke, aspect = 1, points = null) {
  const style = TOOL_STYLE[stroke.tool] ?? TOOL_STYLE.pen;
  const input = points ?? decodePoints(stroke.points, aspect);
  const pressured = input.some(([, , pressure]) => pressure !== 0.5);
  const outline = getStroke(input, {
    size: style.width * stroke.size,
    thinning: style.thinning,
    smoothing: style.smoothing,
    streamline: style.streamline,
    simulatePressure: !pressured,
    last: true,
  });
  if (!outline.length) return "";
  const [first, ...rest] = outline;
  let d = `M${first[0].toFixed(0)} ${first[1].toFixed(0)}`;
  for (let i = 0; i < rest.length; i += 1) {
    const [x, y] = rest[i];
    const [nx, ny] = rest[(i + 1) % rest.length];
    d += ` Q${x.toFixed(0)} ${y.toFixed(0)} ${((x + nx) / 2).toFixed(0)} ${((y + ny) / 2).toFixed(0)}`;
  }
  return `${d} Z`;
}

/** Squared distance from point (px, py) to the segment a-b. */
function toSegment2(px, py, [ax, ay], [bx, by]) {
  const dx = bx - ax;
  const dy = by - ay;
  const length = dx * dx + dy * dy;
  const t = length ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / length)) : 0;
  return (ax + t * dx - px) ** 2 + (ay + t * dy - py) ** 2;
}

const side = ([ax, ay], [bx, by], [cx, cy]) => Math.sign((bx - ax) * (cy - ay) - (by - ay) * (cx - ax));

/** Squared distance between the segments a-b and c-d (0 where they cross). */
function segments2(a, b, c, d) {
  if (side(a, b, c) * side(a, b, d) < 0 && side(c, d, a) * side(c, d, b) < 0) return 0;
  return Math.min(toSegment2(a[0], a[1], c, d), toSegment2(b[0], b[1], c, d), toSegment2(c[0], c[1], a, b), toSegment2(d[0], d[1], a, b));
}

/**
 * The indexes of strokes the eraser touches moving from `from` to `point`
 * (sketch units). The whole way between the two is tested, not just the
 * points: a quick rub's samples are far apart and would step over a line.
 */
export function strokesUnder(strokes, point, radius, aspect = 1, cache = new Map(), from = null) {
  const hit = [];
  const start = from ?? point;
  strokes.forEach((stroke, index) => {
    let points = cache.get(stroke);
    if (!points) {
      points = decodePoints(stroke.points, aspect);
      cache.set(stroke, points);
    }
    const reach = radius + (TOOL_STYLE[stroke.tool]?.width ?? 70) * stroke.size * 0.5;
    // Each segment of the stroke, not just each point: a quick stroke's points are far apart too.
    const near = points.some((p, i) => segments2(start, point, i ? points[i - 1] : p, p) <= reach * reach);
    if (near) hit.push(index);
  });
  return hit;
}

/** A sketch's stored attributes, checked: unknown or broken strokes are dropped, not drawn. */
export function sketchFromAttrs(attrs) {
  const aspect = Math.min(3, Math.max(0.3, Number(attrs?.aspect) || 0.75));
  const strokes = (Array.isArray(attrs?.strokes) ? attrs.strokes : []).slice(0, MAX_STROKES).map((stroke) => validStroke(stroke, aspect)).filter(Boolean);
  return { aspect, strokes };
}
