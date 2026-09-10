/**
 * The animated layer behind the whole window.
 *
 * Mounted as the first child of `.neo-main`, which is the chat view's own
 * element -- Projects, Tasks, Calendar and the rest are siblings of it rather
 * than children, so scoping the background to the chat costs nothing and
 * cannot leak into them by accident.
 *
 * Where it is mounted and how far it reaches are two different questions: the
 * layer is fixed to the viewport (`.chat-bg-layer`), so it paints the full
 * window from here -- behind the sidebar and the composer, which are glass over
 * it -- while still mounting and unmounting with the chat view alone.
 *
 * Two canvases where the effect needs two. The marks canvas is the effect; the
 * diffusion canvas beneath it is the same frame reduced and spread, and it is
 * what makes the glass above a material rather than a tinted box for the sparse
 * fields. Gradient paints a broad field on its own and gets one canvas -- and
 * no `has-wash`, so the stylesheet's continuous wash stays off for it too. The
 * engine owns whichever canvases are there.
 *
 * Not mounted inside `.neo-shell`: that rule set clamps every direct child to
 * 860px and centres it, and a child of a scroller is the wrong place to hang a
 * layer that has to stay still while the transcript moves.
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
  const bloomRef = useRef(null);
  const effect = effectById(background);

  useEffect(() => {
    if (!effect || !hostRef.current || !canvasRef.current) return undefined;
    const engine = createEngine(canvasRef.current, hostRef.current, {
      effect,
      intensity: intensityById(intensity),
      bloom: bloomRef.current,
    });
    //: The cleanup is the whole contract with StrictMode, which mounts this
    //: twice in development. It also runs on every change of effect or
    //: intensity, because both are in the dependency list -- switching
    //: backgrounds tears the old engine down before building the new one
    //: rather than leaving its loop running against the same canvas.
    return () => engine.destroy();
  }, [effect, intensity]);

  if (!effect) return null;

  //: One condition governs both halves of the field. An effect that declares a
  //: gain is one whose marks are too sparse to be a backdrop on their own, so it
  //: needs the diffusion buffer *and* the continuous wash underneath. Gradient
  //: declares none because it is already a broad moving field -- washing it
  //: again would lay a second gradient under the one the user chose.
  const sparse = effect.bloom > 1;

  return (
    <div
      className={`chat-bg-layer ${sparse ? "has-wash" : ""}`.trim()}
      ref={hostRef}
      aria-hidden="true"
    >
      {/* Under the marks, and first in the DOM for that reason: the low
          resolution buffer the engine reduces each frame into, stretched back
          over the field by the compositor. It is the layer the glass above has
          something to diffuse -- see the note on the diffusion pass in
          `engine.js` for the measurements that make it necessary.

          Mounted only for an effect that asked for a gain. Waves covers enough
          of the field to be a backdrop on its own, so it gets no second canvas
          and no reduction rather than an element the engine would skip. */}
      {sparse && <canvas className="chat-bg-diffusion" ref={bloomRef} />}
      <canvas className="chat-bg-marks" ref={canvasRef} />
    </div>
  );
}
