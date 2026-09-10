/**
 * The one animation loop behind the transcript.
 *
 * Every lifetime concern lives here rather than in the effects: sizing, device
 * pixel ratio, resize, the frame loop, tab visibility, reduced motion, theme
 * changes and teardown. An effect module is handed a context and a delta and
 * draws -- it owns no observers, no timers and no loop of its own. That split is
 * what keeps four effects from being four subtly different lifecycles, and it
 * is what makes teardown checkable in one place.
 *
 * Teardown is not a nicety. `main.jsx` renders under React.StrictMode, which
 * mounts, unmounts and remounts every effect in development, so an engine that
 * leaked its loop would run two of them after the first remount and four after
 * the second, each drawing over the last.
 */

import { readPalette } from "./palette.js";

//: Past this, a frame is not a frame -- it is the tab coming back, a breakpoint
//: resuming or a long GC pause. Effects integrate velocity against dt, so an
//: unclamped 4-second delta teleports every particle off-screen at once and the
//: field has to repopulate from nothing.
const MAX_FRAME_MS = 50;

//: Two device pixels per CSS pixel is the point where more stops being visible
//: on this kind of art and starts being four times the fill rate. A 3x phone
//: screen would otherwise quadruple the cost of a full-panel gradient.
const MAX_DPR = 2;

export function createEngine(canvas, host, options) {
  const effect = options.effect;
  const intensity = options.intensity;
  const ctx = canvas && typeof canvas.getContext === "function" ? canvas.getContext("2d") : null;
  if (!ctx || !host || !effect) {
    return { destroy() {} };
  }

  const motionQuery =
    typeof matchMedia === "function" ? matchMedia("(prefers-reduced-motion: reduce)") : null;

  let palette = readPalette();
  let instance = null;
  let width = 0;
  let height = 0;
  let frameHandle = 0;
  let lastMs = 0;
  let elapsed = 0;
  let running = false;
  let destroyed = false;

  const wantsStillness = () => Boolean(motionQuery && motionQuery.matches);

  function paint(dt) {
    //: Reset the state an effect is allowed to change, so a module that leaves
    //: the context in "lighter" cannot tint the one that replaces it -- the
    //: canvas outlives the effect when the picker switches.
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.clearRect(0, 0, width, height);
    instance.frame(ctx, dt, elapsed);
  }

  function tick(now) {
    if (!running || destroyed || !instance) return;
    const dt = Math.min(now - lastMs, MAX_FRAME_MS) / 1000;
    lastMs = now;
    elapsed += dt;
    paint(dt);
    frameHandle = requestAnimationFrame(tick);
  }

  function stop() {
    running = false;
    if (frameHandle) cancelAnimationFrame(frameHandle);
    frameHandle = 0;
  }

  /**
   * One frame and no loop. What reduced motion gets: the field is still worth
   * looking at standing still, and a blank panel would read as the setting
   * having silently turned the feature off rather than having stilled it.
   */
  function paintStill() {
    stop();
    if (destroyed || !instance || !width || !height) return;
    paint(0);
  }

  function start() {
    if (running || destroyed || !instance || !width || !height) return;
    if (wantsStillness()) {
      paintStill();
      return;
    }
    if (typeof document !== "undefined" && document.hidden) return;
    running = true;
    //: Re-anchored on every start, not just the first. Coming back from a
    //: hidden tab is otherwise a delta measured from whenever it was hidden,
    //: and while the clamp above would cap it, restarting the clock is what
    //: makes the resumed motion continuous rather than a jump of one capped
    //: frame.
    lastMs = performance.now();
    frameHandle = requestAnimationFrame(tick);
  }

  function measure() {
    if (destroyed) return;
    const rect = host.getBoundingClientRect();
    const nextWidth = Math.max(1, Math.round(rect.width));
    const nextHeight = Math.max(1, Math.round(rect.height));
    if (nextWidth === width && nextHeight === height) return;

    width = nextWidth;
    height = nextHeight;
    const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
    //: Assigning width/height resets the whole 2D state, transform included,
    //: so the scale has to go on afterwards or every effect draws at 1x in the
    //: corner of a 2x buffer.
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    if (instance) {
      instance.resize(width, height);
    } else {
      instance = effect.create({ width, height, palette, intensity });
    }

    if (wantsStillness()) paintStill();
    else start();
  }

  //: The panel resizes without the window doing so -- collapsing the sidebar
  //: swaps --neo-sidebar-width from 256px to 56px, and a window resize listener
  //: would never hear about it and would leave the canvas stretched.
  const resizeObserver = new ResizeObserver(measure);
  resizeObserver.observe(host);

  //: Canvas cannot inherit a custom property, so a theme change has to be
  //: pushed in. Watching the attribute rather than taking a React prop keeps
  //: this correct no matter who calls applyTheme -- the picker, the first-paint
  //: gate, or a profile switch.
  const themeObserver = new MutationObserver(() => {
    palette = readPalette();
    if (instance) instance.retint(palette);
    if (wantsStillness()) paintStill();
  });
  themeObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });

  function onVisibility() {
    if (document.hidden) stop();
    else start();
  }
  document.addEventListener("visibilitychange", onVisibility);

  function onMotionPreferenceChange() {
    if (wantsStillness()) paintStill();
    else start();
  }
  if (motionQuery) motionQuery.addEventListener("change", onMotionPreferenceChange);

  measure();

  return {
    destroy() {
      destroyed = true;
      stop();
      resizeObserver.disconnect();
      themeObserver.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      if (motionQuery) motionQuery.removeEventListener("change", onMotionPreferenceChange);
      instance = null;
    },
  };
}
