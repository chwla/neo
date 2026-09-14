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
import { backgroundDpr, createRenderBudget } from "./renderBudget.js";

//: Vite substitutes an object literal for `import.meta.env` when it builds, so
//: in production this folds to `false` and every branch guarded by it is
//: dropped. The ternary rather than a bare read because the tests run this file
//: under Node, where `import.meta.env` does not exist -- and rather than `?.`,
//: which defeats the constant folding and leaves the diagnostics in the bundle.
//:
//: The diagnostic state is reached through `globalThis` rather than by importing
//: the module that defines it. An import would survive the folding as a module
//: reference and ship its strings for nothing; a property read on a global lives
//: entirely inside the branch that disappears.
const DEV = import.meta.env ? import.meta.env.DEV : false;

//: Past this, a frame is not a frame -- it is the tab coming back, a breakpoint
//: resuming or a long GC pause. Effects integrate velocity against dt, so an
//: unclamped 4-second delta teleports every particle off-screen at once and the
//: field has to repopulate from nothing.
const MAX_FRAME_MS = 50;

// Allow a little jitter without accidentally halving a nominal 60Hz display.
const FRAME_SLACK_MS = 1.5;

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

//: How far the buffer is softened before it is amplified, in buffer pixels --
//: so about one cell, twelve pixels of field.
//:
//: The layer was built on the assumption that the compositor's upscale would
//: turn coarse blocks into soft shapes. Bilinear does not do that at twelve
//: times magnification: it interpolates between cell centres and leaves the
//: grid legible as facets. On the static field that passes for texture, but a
//: meteor is a thin bright diagonal, and a thin bright diagonal across a coarse
//: grid lights a staircase of single cells -- which is what it looked like,
//: a row of blocks trailing the streak, and worse once there were four times as
//: many meteors to notice it on.
//:
//: One Gaussian at buffer resolution softens the grid. Before the gain
//: rather than after, so a block is never amplified to clipping and then
//: spread -- the spreading has to happen while there is still a gradient to
//: spread. Total light is unchanged; a blur conserves it. What changes is that
//: it arrives as a glow instead of as a staircase.
const BLOOM_BLUR = 1;

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
  //: Canvas2D filters are not universal; the field must work without them.
  let bloomBlurs = false;

  const motionQuery =
    typeof matchMedia === "function" ? matchMedia("(prefers-reduced-motion: reduce)") : null;

  let palette = readPalette();
  let instance = null;
  let width = 0;
  let height = 0;
  let frameHandle = 0;
  let lastMs = null;
  let lastRefresh = null;
  let nextPaintMs = 0;
  let resizePending = false;
  let dpr = 0;
  const budget = createRenderBudget();
  let elapsed = 0;
  let running = false;
  let destroyed = false;

  const wantsStillness = () => Boolean(motionQuery && motionQuery.matches);

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
    //: Feature-detected rather than assumed: a browser without Canvas2D filters
    //: gets the reduction it always got, which is the faceted version rather
    //: than a broken one.
    // Keep reduction and blur in one draw. A separate reduced scratch canvas
    // added a copy; keeping this direct reduced measured diffusion time 12–28%.
    if (bloomBlurs) bloomCtx.filter = `blur(${BLOOM_BLUR}px)`;
    bloomCtx.drawImage(canvas, 0, 0, bloomWidth, bloomHeight);
    if (bloomBlurs) bloomCtx.filter = "none";

    //: The amplification passes are deliberately unfiltered. Each one is the
    //: buffer drawn onto itself, so a filter here would blur what is already
    //: blurred, once per doubling, and the spread would compound into a wash.
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
    if (bloomGain > 1 && host.style) {
      // The wash shares the canvas clock, including its cap and all pauses.
      // A separate CSS animation would keep invalidating the glass at 120Hz.
      const cycle = (elapsed / 52) % 2;
      const progress = cycle <= 1 ? cycle : 2 - cycle;
      const eased = (1 - Math.cos(Math.PI * progress)) / 2;
      host.style.setProperty("--bg-wash-transform",
        `translate3d(${(-2 + 5 * eased) * 1.5 / 1.08}%, ${(1 - 3 * eased) * 1.5 / 1.08}%, 0) scale(${1.04 + 0.08 * eased})`);
    }
    //: Reset the state an effect is allowed to change, so a module that leaves
    //: the context in "lighter" cannot tint the one that replaces it -- the
    //: canvas outlives the effect when the picker switches.
    ctx.globalCompositeOperation = "source-over";
    ctx.globalAlpha = 1;
    ctx.clearRect(0, 0, width, height);

    //: Taking the frame apart, in development only. Two halves of the paint can
    //: each be switched off and each be timed, because "the background is slow"
    //: is not an actionable statement until it says which half.
    if (DEV) {
      const bag = globalThis.__neoBg;
      const clock = bag && bag.timing ? performance : null;
      const started = clock ? clock.now() : 0;
      if (!bag || bag.layers.marks !== false) instance.frame(ctx, dt, elapsed);
      const marked = clock ? clock.now() : 0;
      if (!bag || bag.layers.diffusion !== false) diffuse();
      if (clock) {
        const done = clock.now();
        bag.record({ frame: done - started, marks: marked - started, diffusion: done - marked });
      }
      return;
    }

    instance.frame(ctx, dt, elapsed);
    diffuse();
  }

  function tick(now) {
    if (!running || destroyed || !instance) return;
    //: Rescheduled before the budget is consulted rather than after the paint,
    //: because a refused refresh is still a running loop. What counts a loop
    //: from the outside is how many callbacks are pending -- that is how the
    //: tests tell one engine from two after a remount -- and a tick that
    //: returned without rescheduling would read as a loop that had stopped.
    frameHandle = requestAnimationFrame(tick);
    //: The first tick anchors the clock, rather than `start` doing it. Both ends
    //: of this subtraction then sit on the timebase rAF actually reports, which
    //: `performance.now()` is only guaranteed to match in a plain browser
    //: document -- an embedder is free to hand the callback a different origin,
    //: and a mismatch here reads as one enormous delta on the first frame.
    if (lastRefresh !== null) budget.refresh(now - lastRefresh);
    lastRefresh = now;
    const since = lastMs === null ? 0 : now - lastMs;
    const period = 1000 / budget.fps;
    if (lastMs !== null && now < nextPaintMs - FRAME_SLACK_MS) return;
    // Carry the deadline remainder so 144Hz does not collapse to 48fps.
    nextPaintMs = lastMs === null ? now + period : nextPaintMs + period;
    if (nextPaintMs < now) nextPaintMs = now + period;
    if (resizePending) {
      sizeCanvas();
      resizePending = false;
    }
    const dt = Math.min(since, MAX_FRAME_MS) / 1000;
    lastMs = now;
    elapsed += dt;
    const started = performance.now();
    paint(dt);
    if (budget.record(now, performance.now() - started)) {
      resizePending = true;
      nextPaintMs = now + 1000 / budget.fps;
    }
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
    if (destroyed || !instance || !width || !height || document.hidden) return;
    paint(0);
  }

  function start() {
    if (running || destroyed || !instance || !width || !height) return;
    if (wantsStillness()) {
      paintStill();
      return;
    }
    //: A hidden tab and nothing else. Anything open in the interface -- a dialog,
    //: a popover, a menu -- leaves this running: the field is the feature, and a
    //: field that stops because somebody opened a menu is a broken one.
    if (typeof document !== "undefined" && document.hidden) return;
    running = true;
    //: Re-anchored on every start, not just the first. Coming back from a
    //: hidden tab is otherwise a delta measured from whenever it was hidden,
    //: and while the clamp above would cap it, restarting the clock is what
    //: makes the resumed motion continuous rather than a jump of one capped
    //: frame. Cleared rather than set: the next tick is what anchors it, on the
    //: clock rAF reports instead of on this one.
    lastMs = lastRefresh = null;
    budget.reset();
    frameHandle = requestAnimationFrame(tick);
  }

  function sizeCanvas() {
    dpr = backgroundDpr(width, height, window.devicePixelRatio, budget.level);
    //: Assigning width/height resets the whole 2D state, transform included,
    //: so the scale has to go on afterwards or every effect draws at 1x in the
    //: corner of a 2x buffer.
    canvas.width = Math.max(1, Math.floor(width * dpr));
    canvas.height = Math.max(1, Math.floor(height * dpr));
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.setTransform(canvas.width / width, 0, 0, canvas.height / height, 0, 0);

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
      //: Re-checked here for the same reason the smoothing is: assigning the
      //: size resets the context, and a stub context in a test has neither.
      bloomBlurs = typeof bloomCtx.filter === "string";

    }
  }

  function measure() {
    if (destroyed) return;
    const rect = host.getBoundingClientRect();
    const nextWidth = Math.max(0, Math.round(rect.width));
    const nextHeight = Math.max(0, Math.round(rect.height));
    if (!nextWidth || !nextHeight) {
      width = height = 0;
      stop();
      return;
    }
    const nextDpr = backgroundDpr(nextWidth, nextHeight, window.devicePixelRatio, budget.level);
    if (nextWidth === width && nextHeight === height && nextDpr === dpr) return;
    const resized = nextWidth !== width || nextHeight !== height;
    width = nextWidth;
    height = nextHeight;
    sizeCanvas();
    resizePending = false;
    budget.reset();

    if (instance && resized) {
      instance.resize(width, height);
    } else if (!instance) {
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
  // Moving between displays can change DPR without changing CSS dimensions.
  window.addEventListener?.("resize", measure);

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
    getStats() {
      return { quality: budget.level, fps: budget.fps, dpr, pixels: canvas.width * canvas.height };
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      stop();
      resizeObserver.disconnect();
      window.removeEventListener?.("resize", measure);
      themeObserver.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
      if (motionQuery) motionQuery.removeEventListener("change", onMotionPreferenceChange);
      instance?.destroy?.();
      instance = null;
      // Release GPU backing stores even if a detached DOM node is retained.
      canvas.width = canvas.height = 0;
      if (bloom) bloom.width = bloom.height = 0;
      host.style?.removeProperty("--bg-wash-transform");
    },
  };
}
