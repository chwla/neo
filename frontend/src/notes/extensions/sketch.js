/**
 * A sketch in a note: drawn strokes kept in the note itself (no file), shown
 * as an SVG at the note's width and opened full screen to draw (SketchEditor).
 * The strokes are in the sketch's own units (sketch/strokes.js), so the
 * drawing keeps its shape at any width and through any rotation.
 *
 * The Markdown mirror says "[Sketch]": strokes have no Markdown form, a loss
 * the mirror accepts (as with colours).
 */
import { Node, mergeAttributes } from "@tiptap/core";

import { SKETCH_WIDTH, TOOL_STYLE, colorValue, sketchFromAttrs, strokePath } from "../sketch/strokes.js";

const SVG = "http://www.w3.org/2000/svg";
// Outlines by what they are drawn from (the strokes are rebuilt on every update).
const pathCache = new Map();
const PATH_CACHE_LIMIT = 600;

/** Draw strokes into an SVG element (sketch units), reusing the outlines already made. */
export function paintSketch(svg, { aspect, strokes }) {
  svg.setAttribute("viewBox", `0 0 ${SKETCH_WIDTH} ${Math.round(SKETCH_WIDTH * aspect)}`);
  svg.replaceChildren(
    ...strokes.map((stroke) => {
      const key = `${stroke.tool}|${stroke.size}|${aspect}|${stroke.points}`;
      let d = pathCache.get(key);
      if (d === undefined) {
        try {
          d = strokePath(stroke, aspect);
        } catch {
          d = "";
        }
        if (pathCache.size >= PATH_CACHE_LIMIT) pathCache.delete(pathCache.keys().next().value);
        pathCache.set(key, d);
      }
      const path = document.createElementNS(SVG, "path");
      path.setAttribute("d", d);
      path.style.fill = colorValue(stroke.color);
      path.style.opacity = String(TOOL_STYLE[stroke.tool]?.opacity ?? 1);
      return path;
    }),
  );
}

export const Sketch = Node.create({
  name: "sketch",
  group: "block",
  atom: true,
  draggable: true,
  selectable: true,

  addOptions() {
    return {
      /** Called with the sketch's position when it is tapped to draw. */
      onEdit: null,
    };
  },

  addAttributes() {
    return {
      aspect: {
        default: 0.75,
        parseHTML: (element) => Number(element.getAttribute("data-aspect")) || 0.75,
        renderHTML: (attributes) => ({ "data-aspect": attributes.aspect }),
      },
      strokes: {
        default: [],
        parseHTML: (element) => {
          try {
            return JSON.parse(element.getAttribute("data-strokes") || "[]");
          } catch {
            return [];
          }
        },
        renderHTML: (attributes) => ({ "data-strokes": JSON.stringify(attributes.strokes ?? []) }),
      },
    };
  },

  parseHTML() {
    return [{ tag: 'div[data-type="sketch"]' }];
  },

  renderHTML({ HTMLAttributes }) {
    return ["div", mergeAttributes(HTMLAttributes, { "data-type": "sketch", class: "nw-sketch" }), "Sketch"];
  },

  addNodeView() {
    return ({ node, getPos }) => {
      const dom = document.createElement("figure");
      dom.className = "nw-sketch";
      dom.dataset.type = "sketch";
      dom.contentEditable = "false";
      const svg = document.createElementNS(SVG, "svg");
      svg.setAttribute("role", "img");
      svg.setAttribute("aria-label", "Sketch");
      const empty = document.createElement("span");
      empty.className = "nw-sketch-empty";
      empty.textContent = "Tap to draw";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.className = "nw-sketch-edit";
      edit.textContent = "Edit Sketch";
      edit.setAttribute("aria-label", "Edit sketch");
      dom.append(svg, empty, edit);
      let current = node;
      const paint = () => {
        const sketch = sketchFromAttrs(current.attrs);
        dom.style.aspectRatio = `${1} / ${sketch.aspect}`;
        paintSketch(svg, sketch);
        empty.hidden = sketch.strokes.length > 0;
      };
      paint();
      const open = (event) => {
        event.preventDefault();
        const pos = typeof getPos === "function" ? getPos() : null;
        if (typeof pos === "number") this.options.onEdit?.(pos);
      };
      // A tap anywhere on it opens it; the button is there for the keyboard and screen readers.
      dom.addEventListener("click", open);
      return {
        dom,
        update(next) {
          if (next.type.name !== "sketch") return false;
          current = next;
          paint();
          return true;
        },
        stopEvent: (event) => event.type === "click" || event.type === "mousedown",
        ignoreMutation: () => true,
      };
    };
  },

  renderText() {
    return "[Sketch]";
  },

  renderMarkdown() {
    return "[Sketch]";
  },
});
