/**
 * Flowing gradient ribbons.
 *
 * Each ribbon is a family of forty-odd hairlines, and none of them is drawn
 * independently: line `i` is the linear blend of two animated guide curves at
 * `i / (n - 1)`. That one decision is what produces the whole look. Where the
 * two guides cross, every line in the family passes through nearly the same
 * point and the ribbon pinches to a bright waist; where they diverge it fans
 * open into a wide, almost empty sweep. The guides move at different rates, so
 * those crossings travel along the ribbon and the waist slides with them.
 *
 * The brightness is not painted. Lines are drawn one at a time in additive
 * mode at an alpha low enough to be nearly invisible alone, so what lights up
 * is exactly where they bunch -- the caustic is the sum of the overlap rather
 * than a highlight someone placed. That is also why each line needs its own
 * `stroke`: a single path containing all of them composites once, and the
 * family would flatten into one even ribbon with no core at all.
 *
 * Three sine terms per guide, at wavelengths that are not whole multiples of
 * each other. One sine gives itself away in a few seconds; three beat against
 * each other for long enough that the surface never visibly repeats.
 *
 * Everything is a fraction of the canvas, so a resize re-lays the same ribbons
 * rather than sliding them off an edge.
 */

//: Lines per ribbon at Medium. The moire that reads as a surface needs the
//: spacing to be fine relative to how far the family spreads -- much below
//: thirty and it stops being a ribbon and becomes a handful of curves.
const LINES = 48;

//: Three at Medium, because a ribbon is only pinching part of the time and one
//: on its own leaves stretches with no bright core anywhere. Three crossing
//: each other is also most of what gives the field depth.
//:
//: And three is the ceiling, which is where Vivid's extra goes into lines and
//: alpha instead. Every line is its own `stroke` -- it has to be, or the family
//: composites once and loses its core -- so the ribbon count is the one dial
//: here that multiplies draw calls rather than the work inside them. A fourth
//: ribbon put this past two hundred strokes a frame, more than any other
//: effect, to add a band most of which sits behind the transcript.
const RIBBONS = 3;

//: How much each octave of a guide contributes: the largest is the shape, the
//: smallest is the detail on it. Hoisted because the loop that reads them runs
//: a few thousand times a frame and this array never changes.
const WEIGHTS = [1, 0.44, 0.19];

/** Toward the ink by `k`, which is how one accent becomes a related family. */
function toward([r, g, b], [ir, ig, ib], k) {
  return [r + (ir - r) * k, g + (ig - g) * k, b + (ib - b) * k];
}

export default {
  id: "gradient",

  //: No `bloom`, and it took a measurement to be sure of that. While the lines
  //: were hairlines this field was sparse the way Rain is and needed the
  //: diffusion buffer to reach the glass at all. Widening them to close the gaps
  //: between neighbours took the coverage past half a percent -- concentrated in
  //: bands rather than scattered, and three or four pixels wide, which is
  //: content an eighteen-pixel blur softens instead of erasing. The ribbons are
  //: their own backdrop now.
  //:
  //: Dropping it is also what took the last of the grain out. The buffer is
  //: coarse by design, one cell to twelve pixels, which reads as glow around a
  //: drop or a star but as mottling along a long smooth curve, because the cell
  //: edges follow the line. And it saves a canvas, a downscale and an upscale
  //: every frame for a field that no longer needs any of it.

  create({ width, height, palette, intensity }) {
    let w = width;
    let h = height;
    let colours = palette;

    /**
     * Where the guides are sampled, and the room to hold what they answer.
     *
     * The two guides do not depend on which line is being drawn -- that is the
     * whole point of blending between them -- so they are evaluated once per
     * ribbon per frame and every line reads the same two arrays. Written the
     * obvious way, with the guide called from inside the per-line loop, this
     * effect asked for six sines per sample per line: about eighty-four thousand
     * a frame across three ribbons, to arrive at seventeen hundred distinct
     * answers. Sized here and on resize rather than per frame, so the drawing
     * loop allocates nothing at all.
     */
    let step = 0;
    let samples = 0;
    let xs = new Float64Array(0);
    let guideA = new Float64Array(0);
    let guideB = new Float64Array(0);

    function layout() {
      //: Fine enough that a pinch stays smooth and the polyline corners of
      //: neighbouring lines do not line up into a chevron across the family,
      //: coarse enough that fifty lines across three ribbons is one frame.
      step = Math.max(6, w / 96);
      samples = Math.floor((w + step) / step) + 1;
      if (xs.length !== samples) {
        xs = new Float64Array(samples);
        guideA = new Float64Array(samples);
        guideB = new Float64Array(samples);
      }
      for (let s = 0; s < samples; s += 1) xs[s] = Math.min(s * step, w);
    }

    layout();
    const alpha = intensity.alpha;
    const lines = Math.max(24, Math.round(LINES * intensity.density));
    const count = Math.max(2, Math.min(RIBBONS, Math.round(3 * intensity.density)));

    //: Fixed per ribbon, so a resize and a retint leave the composition alone.
    //: The two guides differ in where they sit, how far they swing and how fast
    //: they run; if they matched, the family would collapse to a single line.
    const ribbons = Array.from({ length: count }, (_, i) => {
      const side = i % 2 === 0 ? 1 : -1;
      //: The band this ribbon works in. Bands overlap on purpose -- the
      //: crossings between two ribbons are half of what makes the field deep.
      const centre = 0.3 + i * 0.19;
      //: The two guides rest close together and swing far, which is what makes
      //: them cross often. Measured with them a third of the panel apart, the
      //: sines could only close the gap near their own extremes, so the family
      //: pinched in bursts and spent the time between them as a plain sweep.
      //: This close, against a swing several times the gap, a crossing is the
      //: normal state and the waist is nearly always somewhere on screen.
      //:
      //: How far the guides swing is also what sets the spacing between lines,
      //: which is the difference between a surface and a set of strands. At
      //: twice these numbers the family fanned to half the panel's height, so
      //: forty-odd lines sat fifteen pixels apart with darkness between them and
      //: the ribbon read as corduroy. Kept to a fifth of the height, the same
      //: lines are a few pixels apart and merge into one sheet -- and the waist
      //: still pinches to two, because the pinch comes from the guides crossing
      //: rather than from how far they travel to do it.
      const split = 0.045;
      return {
        guides: [
          { home: centre - split, swing: 0.1 + (i % 2) * 0.03 },
          { home: centre + split, swing: 0.085 + (i % 3) * 0.022 },
        ],
        //: Three octaves per guide. Ratios deliberately not integers, and the
        //: middle term runs backwards, so the beat period is long.
        cycles: [
          [0.65 + i * 0.15, 1.7 + i * 0.25, 3.1 + i * 0.4],
          [0.8 - i * 0.1, 2.1 + i * 0.3, 3.7 + i * 0.35],
        ],
        //: Faster than the field effects that came before this one, because the
        //: shape is the point here rather than the texture: at a tenth of a
        //: radian a second the waist barely travels within a glance.
        speeds: [
          [0.34 * side, -0.23, 0.41 * side],
          [-0.29 * side, 0.37, -0.19 * side],
        ],
        phases: [
          [i * 1.3, i * 2.7 + 0.6, i * 0.9 + 1.8],
          [i * 2.1 + 1.1, i * 1.6 + 2.4, i * 3.1],
        ],
        //: The whole family leans and breathes, so the ribbon is not the same
        //: width forever and the two edges trade places over minutes.
        leanRate: 0.061 + i * 0.013,
        leanPhase: i * 2.2,
        //: Its own place in the accent-to-ink family. The near ribbon runs
        //: coolest, which is most of what separates it from the ones behind.
        hue: (i % 3) * 0.22,
      };
    });

    return {
      resize(nextWidth, nextHeight) {
        w = nextWidth;
        h = nextHeight;
        layout();
      },

      retint(next) {
        colours = next;
      },

      frame(ctx, dt, t) {
        const { accent, ink, isLight, rgba, glowMode } = colours;
        //: Paper needs more to show at all, the correction every effect makes.
        const lift = (isLight ? 1.8 : 1) * alpha;
        //: Additive, so overlapping lines sum into the bright core. On Paper
        //: that would screen toward white and erase the field, so there the
        //: lines lay down normally and darken the page instead.
        ctx.globalCompositeOperation = glowMode;
        ctx.lineCap = "round";

        for (const ribbon of ribbons) {
          //: One lean for the whole family. It shifts both guides together, so
          //: the ribbon tilts rather than shearing.
          const lean = Math.sin(t * ribbon.leanRate + ribbon.leanPhase);
          const colour = toward(accent, ink, ribbon.hue);

          //: Both guides across the whole width, once. Summed in canvas units
          //: so the shape is the same at every window size.
          for (let g = 0; g < 2; g += 1) {
            const { home, swing } = ribbon.guides[g];
            const cycles = ribbon.cycles[g];
            const speeds = ribbon.speeds[g];
            const phases = ribbon.phases[g];
            const into = g === 0 ? guideA : guideB;
            const base = (home + lean * 0.06) * h;
            for (let s = 0; s < samples; s += 1) {
              const x = xs[s];
              let offset = 0;
              for (let term = 0; term < 3; term += 1) {
                const k = (Math.PI * 2 * cycles[term]) / Math.max(1, w);
                offset += Math.sin(x * k + t * speeds[term] + phases[term]) * WEIGHTS[term];
              }
              into[s] = base + offset * swing * h;
            }
          }

          //: Low, and it has to be. Forty lines crossing at a waist add up, so
          //: an alpha that reads on its own would blow the core out to a white
          //: slab -- the pinch is supposed to be the brightest thing here, not
          //: the only thing.
          //: How wide a line has to be to touch its neighbours, which is the
          //: whole difference between a sheet and a set of strands. It changes
          //: from moment to moment, because the family's spread does: a fixed
          //: width is either too thin when the ribbon fans -- gaps, and the
          //: strands show -- or too fat when it pinches, which softens the core
          //: the pinch exists to produce. So it is measured, from the gap
          //: between the guides, which the loop above already has in hand.
          let spread = 0;
          for (let s = 0; s < samples; s += 1) spread += Math.abs(guideB[s] - guideA[s]);
          const spacing = spread / samples / lines;
          //: A little over the spacing, so neighbours overlap rather than abut
          //: -- abutting anti-aliased strokes leave a seam. Floored at a pixel,
          //: below which a stroke dithers instead of filling.
          //:
          //: The cap is what keeps a wide-open ribbon from becoming a handful of
          //: fat bands, and it is deliberately generous: the spread varies a
          //: hundredfold along a single line, so one width per line is always a
          //: compromise, and the side to err on is closing the gaps. Where the
          //: family pinches, the lines overlap anyway, so the extra width costs
          //: the core a couple of pixels of softness rather than its sharpness.
          const width = Math.max(1, Math.min(4.6, spacing * 1.35));
          ctx.lineWidth = width;
          //: Inverse to the width, so widening a line to close a gap does not
          //: also brighten the ribbon: what is held constant is the light per
          //: unit of area, not per line.
          const perLine = 0.05 * lift * (1.1 / width);

          for (let i = 0; i < lines; i += 1) {
            const blend = i / (lines - 1);
            //: Eased toward the family's two edges, which is what makes it read
            //: as a surface seen edge-on rather than as a printed gradient. Half
            //: strength, though: at full strength the crowding it puts at the
            //: edges comes out of the middle, and the middle is where the gaps
            //: between lines were showing as strands in the first place.
            const cubic = blend * blend * (3 - 2 * blend);
            const eased = blend + (cubic - blend) * 0.5;

            ctx.beginPath();
            ctx.moveTo(xs[0], guideA[0] + (guideB[0] - guideA[0]) * eased);
            for (let s = 1; s < samples; s += 1) {
              ctx.lineTo(xs[s], guideA[s] + (guideB[s] - guideA[s]) * eased);
            }
            //: Faintest in the middle of the family, so the two edges of the
            //: ribbon stay legible as edges even where it fans wide open.
            const edge = 0.55 + 0.45 * Math.abs(blend * 2 - 1);
            ctx.strokeStyle = rgba(colour, perLine * edge);
            ctx.stroke();
          }
        }

        //: Left as the engine expects to find it, so the next effect to use this
        //: canvas inherits neither the composite mode nor the line cap.
        ctx.globalCompositeOperation = "source-over";
        ctx.lineCap = "butt";
      },
    };
  },
};
