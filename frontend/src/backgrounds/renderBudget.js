// Limit decorative work independently of intensity: Vivid keeps its colours
// and population even when a busy/slow device needs a smaller backing buffer.
const LEVELS = [
  { scale: 1, fps: 60 },
  { scale: 0.8, fps: 60 },
  { scale: 0.6, fps: 60 },
  { scale: 0.6, fps: 30 },
];
export const MAX_BACKGROUND_PIXELS = 2_000_000;

export function backgroundDpr(width, height, deviceDpr, level = 0) {
  return Math.min(deviceDpr || 1, 1.5, Math.sqrt(MAX_BACKGROUND_PIXELS / (width * height)))
    * LEVELS[level].scale;
}

export function createRenderBudget() {
  let level = 0;
  let start = null;
  let samples = 0;
  let costTotal = 0;
  let slow = 0;
  let gaps = 0;
  let missed = 0;
  let fastest = Infinity;
  let badWindows = 0;
  let goodWindows = 0;
  let recoverAfter = 0;

  function resetWindow() {
    start = null;
    samples = costTotal = slow = gaps = missed = 0;
  }

  return {
    get level() { return level; },
    get fps() { return LEVELS[level].fps; },
    // Visibility/resize gaps are not evidence of a slow device.
    reset() {
      resetWindow();
      badWindows = goodWindows = 0;
      fastest = Infinity;
    },
    refresh(interval) {
      if (interval <= 0 || interval > 150) return;
      fastest = Math.min(fastest, interval);
      gaps += 1;
      // Missing an optional 120Hz refresh is harmless at our 60fps ceiling.
      // A display that only offers 30Hz is also not a dropped-frame signal.
      if (interval > Math.max(1000 / this.fps, fastest) * 1.45) missed += 1;
    },
    record(now, cost) {
      if (start === null) start = now;
      samples += 1;
      costTotal += cost;
      if (cost > 8) slow += 1;
      if (now - start < 1000 || samples < 8) return false;

      const mean = costTotal / samples;
      const late = gaps ? missed / gaps : 0;
      const overloaded = mean > 5 || slow / samples > 0.2 || late > 0.2;
      const comfortable = mean < 2.5 && slow / samples < 0.05 && late < 0.05;
      badWindows = overloaded ? badWindows + 1 : 0;
      goodWindows = comfortable ? goodWindows + 1 : 0;
      resetWindow();

      // React quickly to sustained pressure, recover slowly. One GC pause or
      // a menu opening must not toggle quality back and forth.
      if (badWindows >= 2 && level < LEVELS.length - 1) {
        level += 1;
        badWindows = goodWindows = 0;
        recoverAfter = now + 15_000;
        return true;
      }
      if (goodWindows >= 10 && level > 0 && now >= recoverAfter) {
        level -= 1;
        badWindows = goodWindows = 0;
        recoverAfter = now + 15_000;
        return true;
      }
      return false;
    },
  };
}
