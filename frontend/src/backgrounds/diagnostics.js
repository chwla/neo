/**
 * The development-only diagnostic bag, and where it lives.
 *
 * The engine does not import this file. It reads `globalThis.__neoBg` inside a
 * branch guarded by `import.meta.env.DEV`, which Vite folds to `false` when it
 * builds -- so the branch, and every string in it, is gone from production. An
 * import would have survived that folding as a module reference and shipped its
 * contents for nothing. This file exists to *install* the bag, and only the
 * benchmark page calls it.
 *
 * What the bag is for: a frame of this background is several separable pieces of
 * work -- the effect's own drawing, the diffusion pass that reduces it, a CSS
 * wash beneath, and three `backdrop-filter` surfaces above that re-run because
 * the canvas under them changed. "The background is slow" does not say which,
 * and the only way to find out is to switch them off one at a time.
 *
 * Only one effect is mounted at a time, so isolating jellyfish from rain is a
 * matter of choosing a background rather than of toggling layers.
 */

export function installDiagnostics() {
  if (globalThis.__neoBg) return globalThis.__neoBg;
  globalThis.__neoBg = {
    layers: { marks: true, diffusion: true },
    timing: false,
    samples: [],
    reset() {
      this.samples.length = 0;
    },
    record(sample) {
      //: Bounded: the loop runs for as long as the tab is open, and an
      //: unbounded array would become its own performance problem.
      if (this.samples.length >= 8000) this.samples.shift();
      this.samples.push(sample);
    },
    /**
     * The per-phase costs as a distribution rather than a mean.
     *
     * A mean hides the thing that is actually felt: a loop comfortable on
     * nineteen frames in twenty and over budget on the twentieth reads as
     * stutter while averaging perfectly well.
     */
    report() {
      const stat = (key) => {
        const values = this.samples.map((s) => s[key]).sort((a, b) => a - b);
        if (!values.length) return null;
        const at = (q) => values[Math.min(values.length - 1, Math.floor(values.length * q))];
        return {
          n: values.length,
          mean: +(values.reduce((a, b) => a + b, 0) / values.length).toFixed(3),
          median: +at(0.5).toFixed(3),
          p95: +at(0.95).toFixed(3),
          max: +values[values.length - 1].toFixed(3),
        };
      };
      return { frame: stat("frame"), marks: stat("marks"), diffusion: stat("diffusion") };
    },
  };
  return globalThis.__neoBg;
}
