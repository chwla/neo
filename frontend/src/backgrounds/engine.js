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

/**
 * The diffusion pass, and why the glass needs one.
 *
 * Measured over a 2000x1200 field, the mean alpha these effects paint across
 * the whole canvas is 0.0066% for Rain, 0.0058% for Stars and 0.024% for
 * Jellyfish. The canvas is essentially empty: Rain is ninety gradient hairlines
 * half a pixel wide, Stars is seventy dots of radius one or two.
 *
 * A blur conserves energy, it does not create it. Put `backdrop-filter:
 * blur(18px)` over a 0.85x20px stroke at alpha 0.18 and its peak lands near
 * alpha 0.007 -- under two parts in 255 of accent over the ground, which is
 * below what eight bits can even represent once a surface tint sits on top.
 * That is why lowering a panel's alpha can never make these backgrounds show
 * through it: there is nothing behind the panel to diffuse. Only Waves, whose
 * five banded fills cover 2.8%, has real content.
 *
 * So the field grows a second layer: the same frame, reduced to a low-resolution
 * buffer and displayed back at full size. Reduction is an energy-conserving
 * average, so a hairline's light is redistributed over the whole block it fell
 * in, and the browser's smooth upscale turns those blocks into soft shapes at a
 * scale a blur cannot erase. Amplified by a per-effect gain, it is what the
 * glass above actually diffuses.
 *
 * This is what a diffuser physically does to a thin bright source -- spreads its
 * light into a glow rather than showing the line -- so the layer is the optics
 * of the material, not decoration. It is generated from the effect's own frame
 * and painted beneath the crisp marks, so Rain still reads as hairlines in the
 * open field and as moving light behind the glass.
 *
 * Only for the effects that need it. Each one declares its own gain and Waves
 * declares none, which turns the whole pipeline off for it: no second canvas, no
 * reduction, no amplification. The gains are set from measured coverage and from
 * where the marks stop being recognisable -- see the notes in the effect modules.
 */
const BLOOM_SCALE = 12;

//: The ceiling on what an effect may ask for. Past 16x even the sparsest field
//: clips to flat colour and stops tracking the animation, which is the one
//: thing the layer is for. Clamped here rather than in the loop below, so what
//: reaches the buffer is always exactly the gain that was asked for -- capping
//: the number of doublings instead would quietly turn a request for 64 into 32.
const MAX_BLOOM_GAIN = 16;

export function createEngine(canvas, host, options) {
  const effect = options.effect;
  const intensity = options.intensity;
  const ctx = canvas && typeof canvas.getContext === "function" ? canvas.getContext("2d") : null;
  if (!ctx || !host || !effect) {
    return { destroy() {} };
  }

  //: Declared by the effect, because how much diffusion a field needs is a
  //: property of what it paints. An effect that names no gain wants none: Waves
  //: covers 2.8% of the field on its own, which is real content for a
  //: backdrop-filter, and running the pipeline for it would cost a canvas and a
  //: downscale a frame to soften something already soft. A gain of 1 means the
  //: same thing -- a diffused copy at unit strength is the field again.
  const bloomGain = Math.min(MAX_BLOOM_GAIN, Number(effect.bloom) || 0);
  //: Optional on both sides. `ChatBackground` only mounts the second canvas for
  //: an effect that asked for one, and every test that builds an engine by hand
  //: passes a single canvas -- either way the field runs and only the glass goes
  //: without its material.
  const bloom = bloomGain > 1 ? options.bloom || null : null;
  const bloomCtx = bloom && typeof bloom.getContext === "function" ? bloom.getContext("2d") : null;
  //: The amplification schedule, worked out once. Both fall out of the gain and
  //: the gain cannot change for the life of an engine -- switching effects
  //: builds a new one -- so a `log2` and a `2 **` per frame would be arithmetic
  //: the loop repeats sixty times a second to reach the same two numbers.
  const bloomDoublings = bloomGain > 1 ? Math.floor(Math.log2(bloomGain)) : 0;
  const bloomRemainder = bloomGain > 1 ? bloomGain / 2 ** bloomDoublings - 1 : 0;
  let bloomWidth = 0;
  let bloomHeight = 0;

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

  //: Two ways to be invisible, and the loop owes nothing to either. A hidden tab
  //: is the obvious one. The other is a dialog: `modalStack` flags the document
  //: while anything is open, and an open dialog is both an occluder and -- since
  //: the settings panel became glass -- the one thing that makes this loop
  //: expensive, because every frame it paints is a frame the pane above has to
  //: blur again. Stopping is what makes that pane free rather than cheap.
  const isCovered = () => {
    if (typeof document === "undefined") return false;
    return document.hidden || document.documentElement?.dataset.modalOpen !== undefined;
  };

  /**
   * Reduce the frame just painted into the diffusion buffer.
   *
   * One downscale of the full-resolution canvas, then the buffer is amplified
   * by drawing it onto itself in `lighter`, which adds premultiplied colour and
   * alpha -- so each pass doubles. The fractional remainder rides on
   * `globalAlpha`, which lets an effect ask for a gain of 10 rather than being
   * rounded to 8 or 16.
   *
   * Nothing here reads the previous buffer, so the amplification cannot compound
   * frame over frame: the buffer is cleared and rebuilt from the canvas every
   * time, which also means reduced motion's single still frame gets its
   * diffusion for free.
   */
  function diffuse() {
    if (!bloomCtx || !bloomWidth || !bloomHeight || !canvas.width || !canvas.height) return;
    //: Entered with the context in its default state and left that way, which
    //: is what the restore at the end is for -- nothing else touches this
    //: context, so it does not need resetting on the way in as well.
    bloomCtx.clearRect(0, 0, bloomWidth, bloomHeight);
    bloomCtx.drawImage(canvas, 0, 0, bloomWidth, bloomHeight);

    bloomCtx.globalCompositeOperation = "lighter";
    for (let pass = 0; pass < bloomDoublings; pass += 1) {
      bloomCtx.drawImage(bloom, 0, 0);
    }
    if (bloomRemainder > 0.01) {
      bloomCtx.globalAlpha = bloomRemainder;
      bloomCtx.drawImage(bloom, 0, 0);
      bloomCtx.globalAlpha = 1;
    }
    bloomCtx.globalCompositeOperation = "source-over";
  }

  function paint(dt) {
    //: Reset the state an effect is allowed to change, so a module that leaves
    //: the context in "lighter" cannot tint the one that replaces it -- the
    //: canvas outlives the effect when the picker switches.
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.clearRect(0, 0, width, height);
    instance.frame(ctx, dt, elapsed);
    diffuse();
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
    if (isCovered()) return;
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

    //: Deliberately not scaled by the device pixel ratio: this buffer is meant
    //: to be coarse, and it is stretched back over the field by the compositor.
    //: Sizing it in CSS pixels also keeps the block a mark's light is spread
    //: over the same physical size on every display.
    if (bloomCtx) {
      bloomWidth = Math.max(1, Math.round(width / BLOOM_SCALE));
      bloomHeight = Math.max(1, Math.round(height / BLOOM_SCALE));
      bloom.width = bloomWidth;
      bloom.height = bloomHeight;
      //: Assigning the size reset the context, so the smoothing that does the
      //: averaging has to be re-asked for here rather than once at startup.
      bloomCtx.imageSmoothingEnabled = true;
      bloomCtx.imageSmoothingQuality = "high";
    }

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
  //: `data-modal-open` rides along because it is on the same element and the
  //: engine already had an observer there -- a dialog opening is a stop, and a
  //: dialog closing is the same resume a tab regaining focus gets, clock and
  //: all.
  const rootObserver = new MutationObserver((records) => {
    for (const record of records) {
      if (record.attributeName === "data-theme") {
        palette = readPalette();
        if (instance) instance.retint(palette);
        if (wantsStillness()) paintStill();
      } else {
        syncActivity();
      }
    }
  });
  rootObserver.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme", "data-modal-open"],
  });

  function syncActivity() {
    if (isCovered()) stop();
    else start();
  }
  document.addEventListener("visibilitychange", syncActivity);

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
      rootObserver.disconnect();
      document.removeEventListener("visibilitychange", syncActivity);
      if (motionQuery) motionQuery.removeEventListener("change", onMotionPreferenceChange);
      instance = null;
    },
  };
}
