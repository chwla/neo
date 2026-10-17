import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import Icon from "../WorkspaceIcon.jsx";
import { registerModal } from "../modalStack.js";
import {
  MAX_STROKES,
  SIZES,
  SKETCH_COLORS,
  SKETCH_WIDTH,
  TOOLS,
  TOOL_STYLE,
  colorValue,
  encodePoints,
  strokePath,
  strokesUnder,
} from "./sketch/strokes.js";
import { ConfirmDialog } from "./ui.jsx";

const TOOL_LABELS = { pen: "Pen", marker: "Marker", pencil: "Pencil", eraser: "Eraser" };
const COLOR_LABELS = { ink: "Ink", purple: "Purple", pink: "Pink", orange: "Orange", mint: "Mint", blue: "Blue" };
const ERASER_RADIUS = 140;

const touches = new WeakMap();

/**
 * A control's handlers that act on a finger's lift as well as on a click.
 * Chrome on Android drops the click of a tap that comes within a few hundred
 * milliseconds of a stroke that lifted while still moving over the drawing
 * (a `touch-action: none` surface): it takes the tap for one that stops a
 * fling. Undo right after a stroke would do nothing. So a tap that lifts
 * inside the control it began on acts at the lift (touchend), as a click
 * would, and cancels that touchend: the browser then sends no click at all,
 * neither a second one here nor one on whatever is under the finger once
 * Done has closed the drawing. Keyboards and mice still click.
 */
function pressable(action) {
  return {
    onPointerDown: (event) => {
      if (event.pointerType === "touch") touches.set(event.currentTarget, { pointerId: event.pointerId, lifted: false, tappedAt: -Infinity });
    },
    onPointerUp: (event) => {
      const control = event.currentTarget;
      const touch = touches.get(control);
      if (!touch || touch.pointerId !== event.pointerId) return;
      touch.pointerId = null;
      const r = control.getBoundingClientRect();
      touch.lifted = !control.disabled && event.clientX >= r.left && event.clientX <= r.right && event.clientY >= r.top && event.clientY <= r.bottom;
    },
    onPointerCancel: (event) => {
      const touch = touches.get(event.currentTarget);
      if (touch) Object.assign(touch, { pointerId: null, lifted: false });
    },
    onTouchEnd: (event) => {
      const touch = touches.get(event.currentTarget);
      if (!touch?.lifted) return;
      touch.lifted = false;
      touch.tappedAt = event.timeStamp;
      event.preventDefault();
      action();
    },
    onClick: (event) => {
      const touch = touches.get(event.currentTarget);
      if (touch && event.timeStamp - touch.tappedAt < 1000) return;
      action();
    },
  };
}

/** CSS colours (theme tokens) resolved for the canvas, which can't read custom properties. */
function resolveColors(probe) {
  const out = {};
  for (const color of SKETCH_COLORS) {
    probe.style.color = colorValue(color);
    out[color] = getComputedStyle(probe).color;
  }
  return out;
}

function drawStroke(context, stroke, scale, colors, aspect, points = null) {
  const d = strokePath(stroke, aspect, points);
  if (!d) return;
  context.save();
  context.scale(scale, scale);
  context.globalAlpha = TOOL_STYLE[stroke.tool]?.opacity ?? 1;
  context.fillStyle = colors[stroke.color] ?? colors.ink;
  context.fill(new Path2D(d));
  context.restore();
}

/**
 * Drawing, full screen. Pen, marker and pencil follow pressure where the
 * device reports it (a stylus); a finger or mouse gets simulated pressure.
 * Once a stylus has been used, touches are ignored (a palm resting on the
 * screen), and a second finger cancels the stroke (two fingers are not
 * drawing). The eraser takes whole strokes, as Apple's object eraser does.
 */
export default function SketchEditor({ initial, isNew, onDone, onCancel }) {
  const [strokes, setStrokes] = useState(initial.strokes);
  const [aspect, setAspect] = useState(initial.aspect);
  const [tool, setTool] = useState("pen");
  const [color, setColor] = useState("ink");
  const [size, setSize] = useState(2);
  const [history, setHistory] = useState({ undo: [], redo: [] });
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const dialog = useRef(null);
  const area = useRef(null);
  const base = useRef(null);
  const live = useRef(null);
  const probe = useRef(null);
  const colors = useRef({});
  const surface = useRef({ width: 0, height: 0 });
  const active = useRef(null);
  const penSeen = useRef(false);
  const shaped = useRef(!isNew || initial.strokes.length > 0);
  const [box, setBox] = useState({ width: 0, height: 0 });
  const strokesRef = useRef(strokes);
  strokesRef.current = strokes;

  const changed = history.undo.length > 0;

  const cancel = useCallback(() => {
    if (changed) setConfirmDiscard(true);
    else onCancel();
  }, [changed, onCancel]);

  useEffect(() => registerModal(cancel), [cancel]);

  // Focus comes into the drawing: the note's text loses it, so an on-screen
  // keyboard goes away instead of covering the drawing (where a stroke would type).
  useEffect(() => {
    dialog.current?.focus({ preventScroll: true });
  }, []);

  // Fit the drawing to the space it has; a new, empty sketch takes that space's shape.
  useLayoutEffect(() => {
    const element = area.current;
    if (!element) return undefined;
    const measure = () => {
      // The space inside the padding: the drawing must fit in it, never push it wider.
      const style = getComputedStyle(element);
      const room = {
        width: element.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
        height: element.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom),
      };
      if (room.width <= 0 || room.height <= 0) return;
      let shape = aspect;
      if (!shaped.current) {
        shape = Math.min(1.6, Math.max(0.5, room.height / room.width));
        shaped.current = true;
        setAspect(shape);
      }
      const width = Math.min(room.width, room.height / shape);
      setBox({ width: Math.floor(width), height: Math.floor(width * shape) });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [aspect]);

  // Paint every finished stroke whenever they or the size change (rotation included).
  useEffect(() => {
    const canvas = base.current;
    if (!canvas || !box.width) return;
    const dpr = window.devicePixelRatio || 1;
    for (const layer of [base.current, live.current]) {
      layer.width = Math.round(box.width * dpr);
      layer.height = Math.round(box.height * dpr);
    }
    surface.current = box;
    colors.current = resolveColors(probe.current);
    const context = canvas.getContext("2d");
    context.clearRect(0, 0, canvas.width, canvas.height);
    const scale = (box.width * dpr) / SKETCH_WIDTH;
    for (const stroke of strokes) drawStroke(context, stroke, scale, colors.current, aspect);
  }, [strokes, box, aspect]);

  const record = useCallback((entry) => {
    setHistory((current) => ({ undo: [...current.undo, entry], redo: [] }));
  }, []);

  // Each step reads the history as it is now and sets both, so a step is applied once.
  const undo = useCallback(() => {
    const entry = history.undo.at(-1);
    if (!entry || active.current) return;
    const list = strokesRef.current;
    const next = entry.type === "add" ? list.filter((stroke) => stroke !== entry.stroke) : restore(list, entry.removed);
    strokesRef.current = next;
    setStrokes(next);
    setHistory({ undo: history.undo.slice(0, -1), redo: [...history.redo, entry] });
  }, [history]);

  const redo = useCallback(() => {
    const entry = history.redo.at(-1);
    if (!entry || active.current) return;
    const list = strokesRef.current;
    const next = entry.type === "add" ? [...list, entry.stroke] : list.filter((stroke) => !entry.removed.some((item) => item.stroke === stroke));
    strokesRef.current = next;
    setStrokes(next);
    setHistory({ undo: [...history.undo, entry], redo: history.redo.slice(0, -1) });
  }, [history]);

  useEffect(() => {
    const onKey = (event) => {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "z") return;
      event.preventDefault();
      event.stopPropagation();
      if (event.shiftKey) redo();
      else undo();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [undo, redo]);

  function unitsOf(event) {
    const rect = live.current.getBoundingClientRect();
    const scale = SKETCH_WIDTH / rect.width;
    return [(event.clientX - rect.left) * scale, (event.clientY - rect.top) * scale, event.pointerType === "pen" ? event.pressure || 0.5 : 0.5];
  }

  function paintLive() {
    const canvas = live.current;
    const context = canvas.getContext("2d");
    context.clearRect(0, 0, canvas.width, canvas.height);
    const current = active.current;
    if (!current || current.erasing || current.points.length < 1) return;
    const scale = canvas.width / SKETCH_WIDTH;
    drawStroke(context, { tool: current.tool, color: current.color, size: current.size }, scale, colors.current, aspect, current.points);
  }

  function erase(point) {
    const current = active.current;
    const list = strokesRef.current;
    const hit = strokesUnder(list, point, ERASER_RADIUS, aspect, current.cache, current.lastErase);
    current.lastErase = point;
    if (!hit.length) return;
    const removed = hit.map((index) => ({ index, stroke: list[index] }));
    current.removed.push(...removed);
    const gone = new Set(removed.map((item) => item.stroke));
    // The next hit in the same burst of events must see this one's result.
    const next = list.filter((stroke) => !gone.has(stroke));
    strokesRef.current = next;
    setStrokes(next);
  }

  function onPointerDown(event) {
    if (event.pointerType === "pen") penSeen.current = true;
    else if (penSeen.current && event.pointerType === "touch") return; // a palm, while a stylus draws
    if (active.current) {
      // A second finger: not drawing (a pinch or a scroll). The stroke is dropped.
      if (active.current.pointerType === "touch" && event.pointerType === "touch") {
        active.current = null;
        paintLive();
      }
      return;
    }
    event.preventDefault();
    try {
      // Moves outside the drawing still belong to the stroke; without capture it still draws.
      event.currentTarget.setPointerCapture?.(event.pointerId);
    } catch {
      /* a pointer the browser no longer counts as down */
    }
    active.current = {
      pointerId: event.pointerId,
      pointerType: event.pointerType,
      tool,
      color,
      size,
      erasing: tool === "eraser",
      points: [],
      removed: [],
      cache: new Map(),
    };
    onPointerMove(event);
  }

  function onPointerMove(event) {
    const current = active.current;
    if (!current || event.pointerId !== current.pointerId) return;
    // Every sample since the last frame (React's event has no getCoalescedEvents; the browser's does).
    const native = event.nativeEvent ?? event;
    const coalesced = native.getCoalescedEvents?.() ?? [];
    const events = coalesced.length ? coalesced : [native];
    for (const each of events) {
      const point = unitsOf(each);
      if (current.erasing) erase(point);
      else current.points.push(point);
    }
    if (!current.erasing) {
      if (!current.frame) {
        current.frame = requestAnimationFrame(() => {
          current.frame = 0;
          paintLive();
        });
      }
    }
  }

  function onPointerUp(event) {
    const current = active.current;
    if (!current || event.pointerId !== current.pointerId) return;
    active.current = null;
    cancelAnimationFrame(current.frame);
    if (current.erasing) {
      if (current.removed.length) record({ type: "erase", removed: current.removed });
    } else if (current.points.length && strokesRef.current.length < MAX_STROKES) {
      const stroke = { tool: current.tool, color: current.color, size: current.size, points: encodePoints(current.points) };
      const next = [...strokesRef.current, stroke];
      strokesRef.current = next;
      setStrokes(next);
      record({ type: "add", stroke });
    }
    paintLive();
  }

  function onPointerCancel(event) {
    if (active.current?.pointerId !== event.pointerId) return;
    active.current = null;
    paintLive();
  }

  return createPortal(
    <div ref={dialog} className="nw-sketch-editor" role="dialog" aria-modal="true" aria-label="Sketch" tabIndex={-1}>
      <span ref={probe} className="nw-sketch-probe" aria-hidden="true" />
      <header className="nw-sketch-head">
        <button type="button" className="nw-sketch-text" {...pressable(cancel)}>Cancel</button>
        <div className="nw-sketch-history">
          <button type="button" className="nw-tool" aria-label="Undo" disabled={!history.undo.length} {...pressable(undo)}>
            <Icon name="undo" />
          </button>
          <button type="button" className="nw-tool" aria-label="Redo" disabled={!history.redo.length} {...pressable(redo)}>
            <Icon name="redo" />
          </button>
        </div>
        <button type="button" className="nw-sketch-text nw-sketch-done" {...pressable(() => onDone({ aspect, strokes }))}>Done</button>
      </header>
      <div ref={area} className="nw-sketch-area">
        <div className="nw-sketch-surface" style={{ width: box.width, height: box.height }}>
          <canvas ref={base} className="nw-sketch-canvas" aria-hidden="true" />
          <canvas
            ref={live}
            className="nw-sketch-canvas nw-sketch-input"
            role="img"
            aria-label="Drawing area"
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerCancel}
            onContextMenu={(event) => event.preventDefault()}
          />
        </div>
      </div>
      <footer className="nw-sketch-tools" role="toolbar" aria-label="Drawing tools">
        <div className="nw-sketch-group">
          {[...TOOLS, "eraser"].map((name) => (
            <button
              key={name}
              type="button"
              className={`nw-sketch-tool ${tool === name ? "on" : ""}`.trim()}
              aria-pressed={tool === name}
              {...pressable(() => setTool(name))}
            >
              {TOOL_LABELS[name]}
            </button>
          ))}
        </div>
        <div className="nw-sketch-group">
          {SKETCH_COLORS.map((name) => (
            <button
              key={name}
              type="button"
              className={`nw-sketch-color ${color === name ? "on" : ""}`.trim()}
              style={{ "--swatch": colorValue(name) }}
              aria-label={COLOR_LABELS[name]}
              aria-pressed={color === name}
              disabled={tool === "eraser"}
              {...pressable(() => setColor(name))}
            />
          ))}
        </div>
        <div className="nw-sketch-group">
          {SIZES.map((value) => (
            <button
              key={value}
              type="button"
              className={`nw-sketch-size ${size === value ? "on" : ""}`.trim()}
              style={{ "--dot": `${4 + value * 4}px` }}
              aria-label={["Thin", "Medium", "Thick"][value - 1]}
              aria-pressed={size === value}
              disabled={tool === "eraser"}
              {...pressable(() => setSize(value))}
            />
          ))}
        </div>
      </footer>
      {confirmDiscard && (
        <ConfirmDialog
          title="Discard changes?"
          message="What you drew since opening this sketch will be lost."
          confirmLabel="Discard"
          danger
          onCancel={() => setConfirmDiscard(false)}
          onConfirm={() => {
            setConfirmDiscard(false);
            onCancel();
          }}
        />
      )}
    </div>,
    document.body,
  );
}

function restore(list, removed) {
  const next = [...list];
  for (const { index, stroke } of [...removed].sort((a, b) => a.index - b.index)) next.splice(Math.min(index, next.length), 0, stroke);
  return next;
}
