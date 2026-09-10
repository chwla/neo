/**
 * The animated layer behind the transcript.
 *
 * Mounted as the first child of `.neo-main`, which is the chat view's own
 * element -- Projects, Tasks, Calendar and the rest are siblings of it rather
 * than children, so scoping the background to the chat costs nothing and
 * cannot leak into them by accident.
 *
 * Not mounted inside `.neo-shell`: that rule set clamps every direct child to
 * 860px and centres it, which would silently squeeze the canvas into a column
 * down the middle of the panel.
 *
 * "none" returns null rather than rendering an idle canvas, so a profile that
 * has not asked for motion pays nothing at all -- no element, no context, no
 * observers, no loop.
 */

import { useEffect, useRef } from "react";

import { effectById } from "./backgrounds/effects.js";
import { intensityById } from "./backgrounds/index.js";
import { createEngine } from "./backgrounds/engine.js";

export default function ChatBackground({ background, intensity }) {
  const hostRef = useRef(null);
  const canvasRef = useRef(null);
  const effect = effectById(background);

  useEffect(() => {
    if (!effect || !hostRef.current || !canvasRef.current) return undefined;
    const engine = createEngine(canvasRef.current, hostRef.current, {
      effect,
      intensity: intensityById(intensity),
    });
    //: The cleanup is the whole contract with StrictMode, which mounts this
    //: twice in development. It also runs on every change of effect or
    //: intensity, because both are in the dependency list -- switching
    //: backgrounds tears the old engine down before building the new one
    //: rather than leaving its loop running against the same canvas.
    return () => engine.destroy();
  }, [effect, intensity]);

  if (!effect) return null;

  return (
    <div className="chat-bg-layer" ref={hostRef} aria-hidden="true">
      <canvas ref={canvasRef} />
    </div>
  );
}
