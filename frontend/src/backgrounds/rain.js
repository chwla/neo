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

    return {
      resize(nextWidth, nextHeight) {
        const scaleX = nextWidth / (w || nextWidth);
        for (const drop of drops) drop.x *= scaleX;
        w = nextWidth;
        h = nextHeight;
      },

      retint(next) {
        colours = next;
      },

      frame(ctx, dt) {
        const { accent, isLight, rgba } = colours;
        //: Deliberately quiet. Rain covers the whole panel at once, so the
        //: per-drop alpha that reads as weather is far below what a handful of
        //: jellyfish can carry, and Vivid still has to leave the transcript
        //: the brightest thing on screen.
        const lift = (isLight ? 1.5 : 1) * alpha;

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
          const streak = ctx.createLinearGradient(tailX, tailY, drop.x, drop.y);
          streak.addColorStop(0, rgba(accent, 0));
          streak.addColorStop(1, rgba(accent, (0.05 + drop.depth * 0.13) * lift));

          ctx.beginPath();
          ctx.moveTo(tailX, tailY);
          ctx.lineTo(drop.x, drop.y);
          ctx.strokeStyle = streak;
          ctx.lineWidth = 0.5 + drop.depth * 0.7;
          ctx.stroke();
        }
      },
    };
  },
};
