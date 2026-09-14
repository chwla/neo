/**
 * Rain.
 *
 * Depth is the whole trick, and it is one number: a drop's distance sets its
 * speed, its length, its thickness and its alpha together. Vary those
 * independently and it reads as noise; vary them from one value and the eye
 * sorts the drops into near and far by itself.
 *
 * No splashes. The composer sits over the bottom of the panel behind a near
 * opaque scrim, so a landing line would be drawn where nobody can see it.
 */

const DROPS = 90;

//: One lean for every drop, because rain is wind and wind is not per-drop. The
//: moment they disagree it stops looking like weather.
const LEAN = 0.2;

function spawn(width, height, aboveOnly) {
  const depth = 0.35 + Math.random() * 0.65;
  return {
    x: Math.random() * (width * 1.25) - width * 0.2,
    y: aboveOnly ? -Math.random() * height * 0.4 - 10 : Math.random() * height,
    depth,
    length: 9 + depth * 21,
    speed: 360 + depth * 520,
  };
}

export default {
  id: "rain",

  //: Ninety half-pixel hairlines cover 0.0066% of the field, so the diffusion
  //: layer has to lift them by an order of magnitude before a blur above can
  //: show anything at all. Ten, not more: it puts the median cell at alpha 0.03
  //: and the brightest at 0.15, which is a wet glow behind the strokes -- push
  //: it to sixteen and the drops start trailing halos and rain stops reading as
  //: rain, which is the failure this number exists to avoid.
  bloom: 10,

  create({ width, height, palette, intensity }) {
    let w = width;
    let h = height;
    let colours = palette;
    const alpha = intensity.alpha;
    const count = Math.max(16, Math.round(DROPS * intensity.density));
    const drops = Array.from({ length: count }, () => spawn(w, h, false));

    /**
     * One gradient for the whole storm, in the shape of a single unit drop.
     *
     * Every drop used to build its own: a `createLinearGradient` and two
     * `addColorStop`s, each parsing a colour string this module had just
     * assembled, once per drop per frame. At Vivid that is a hundred and
     * twenty-six gradient objects a frame and better than seven thousand a
     * second, all of them identical in everything but where they sit and how
     * bright they end -- which is a lot of garbage to collect for no difference
     * in what is drawn.
     *
     * Both of those differences can be taken out of the gradient. Position comes
     * off it by building the thing at the origin and translating to the drop.
     * Brightness comes off it because alpha multiplies: a stop at full strength
     * under `globalAlpha` is the same pixel as a stop at that strength, so the
     * gradient can run to opaque and the drop can carry its own alpha.
     *
     * What is left is one shape shared by every drop, because `LEAN` is one
     * value for the whole field -- rain is wind, and wind is not per-drop -- so
     * a drop's displacement is always `length` times the same vector. That makes
     * the difference between two drops a uniform scale, which is why this is
     * built for a drop of length one: scaling it by the drop's length lands both
     * ends exactly where the old per-drop gradient put them, with no rounding
     * and no bucketing.
     *
     * Rebuilt on retint and never in the loop, since the accent is the only
     * thing in it that can change.
     */
    let streak = null;

    function buildStreak(ctx) {
      const { accent, rgba } = colours;
      streak = ctx.createLinearGradient(0, 0, LEAN, 1);
      streak.addColorStop(0, rgba(accent, 0));
      streak.addColorStop(1, rgba(accent, 1));
    }

    return {
      resize(nextWidth, nextHeight) {
        const scaleX = nextWidth / (w || nextWidth);
        for (const drop of drops) drop.x *= scaleX;
        w = nextWidth;
        h = nextHeight;
      },

      retint(next) {
        colours = next;
        //: Dropped rather than rebuilt: there is no context here, and the next
        //: frame has one.
        streak = null;
      },

      frame(ctx, dt) {
        const { isLight } = colours;
        //: Deliberately quiet. Rain covers the whole panel at once, so the
        //: per-drop alpha that reads as weather is far below what a handful of
        //: jellyfish can carry, and Vivid still has to leave the transcript
        //: the brightest thing on screen.
        const lift = (isLight ? 1.5 : 1) * alpha;

        if (!streak) buildStreak(ctx);
        ctx.strokeStyle = streak;

        //: Source-over throughout, never additive: ninety overlapping additive
        //: strokes accumulate into a visible haze across the middle of the
        //: panel, which is exactly where the conversation is.
        for (const drop of drops) {
          drop.y += dt * drop.speed;
          drop.x += dt * drop.speed * LEAN;

          if (drop.y - drop.length > h || drop.x - drop.length > w) {
            Object.assign(drop, spawn(w, h, true));
            continue;
          }

          const tailX = drop.x - LEAN * drop.length;
          const tailY = drop.y - drop.length;
          const lineWidth = 0.5 + drop.depth * 0.7;
          // Drops spend part of their lifetime above or left of the viewport.
          // Keep their motion, but avoid all canvas state/path work until any
          // part of the stroke can be visible (including its antialias fringe).
          const fringe = lineWidth / 2 + 1;
          if (drop.y < -fringe || drop.x < -fringe || tailY > h + fringe || tailX > w + fringe) continue;

          //: The unit drop, put where this one is and grown to its length. The
          //: scale is uniform, so it takes the line width with it -- hence the
          //: division, which leaves the stroke exactly as wide as it was drawn
          //: before, in field pixels rather than in unit ones.
          ctx.save();
          ctx.translate(tailX, tailY);
          ctx.scale(drop.length, drop.length);
          ctx.globalAlpha = (0.05 + drop.depth * 0.13) * lift;
          ctx.lineWidth = lineWidth / drop.length;
          ctx.beginPath();
          ctx.moveTo(0, 0);
          ctx.lineTo(LEAN, 1);
          ctx.stroke();
          ctx.restore();
        }

        //: Put back rather than left for the engine to reset. The engine does
        //: reset it, but an effect that quietly depends on that is one refactor
        //: away from tinting whatever draws after it.
        ctx.globalAlpha = 1;
      },
    };
  },
};
