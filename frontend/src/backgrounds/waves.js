/**
 * Slow water.
 *
 * What makes a wave read as water rather than as a hill is the crest: a thin
 * bright line where the surface catches light, with the body falling away
 * underneath it. An earlier version of this filled each band solidly to the
 * bottom of the panel and got four green hills, so the fill here is deliberately
 * shallow -- it fades to almost nothing within a quarter of the panel and the
 * stroked crest carries the shape.
 *
 * Three sine waves are summed per band, not one. One sine is recognisably a
 * sine and gives itself away within seconds; three at different wavelengths and
 * opposing drift beat against each other for long enough that the surface never
 * visibly repeats. The largest is the swell, the smallest is the chop.
 *
 * Everything is a fraction of the canvas, never a pixel constant. The bands sit
 * across the lower half, so the front ones pass behind the composer's scrim on
 * their own -- composed to let that happen rather than tuned around where the
 * scrim currently ends, which is a number this file should not have to know.
 */

const BANDS = 5;

export default {
  id: "waves",

  create({ width, height, palette, intensity }) {
    let w = width;
    let h = height;
    let colours = palette;
    const alpha = intensity.alpha;
    const count = Math.max(3, Math.min(BANDS, Math.round(4 * intensity.density)));

    //: Fixed per band so the water keeps its character across a resize; only
    //: the amplitudes and wavelengths below are measured in canvas units.
    const bands = Array.from({ length: count }, (_, i) => ({
      depth: i / Math.max(1, count - 1),
      //: Three octaves. The ratios are deliberately not whole multiples, so the
      //: combined period is long rather than the three lining up every cycle.
      cycles: [1.0 + i * 0.35, 2.7 + i * 0.6, 5.3 + i * 0.9],
      speeds: [0.17 + i * 0.04, -(0.11 + i * 0.03), 0.23 + i * 0.05],
      offsets: [i * 1.7, i * 0.9 + 2.1, i * 2.3 + 0.6],
      swellRate: 0.07 + i * 0.017,
      swellPhase: i * 1.3,
    }));

    return {
      resize(nextWidth, nextHeight) {
        w = nextWidth;
        h = nextHeight;
      },

      retint(next) {
        colours = next;
      },

      frame(ctx, dt, t) {
        const { accent, isLight, rgba, glowMode } = colours;
        const lift = (isLight ? 1.45 : 1) * alpha;
        //: Finer than the old sampling, because a stroked crest shows facets
        //: that a filled body hides.
        const step = Math.max(4, w / 200);

        for (const band of bands) {
          //: Across the lower half, back band highest. The conversation sits at
          //: the top of the panel, so the water stays out from under it.
          const base = h * (0.56 + band.depth * 0.36);
          //: Farther swells are smaller as well as slower, which is most of
          //: what separates the layers into distance.
          const amp = h * 0.055 * (1 - band.depth * 0.45);
          //: Sets rolling through, so the water is not the same height forever.
          const swell = 1 + 0.28 * Math.sin(t * band.swellRate + band.swellPhase);
          const heights = [amp, amp * 0.42, amp * 0.17];
          const k = band.cycles.map((c) => (Math.PI * 2 * c) / Math.max(1, w));

          const surfaceAt = (x) =>
            base +
            swell *
              (Math.sin(x * k[0] + t * band.speeds[0] + band.offsets[0]) * heights[0] +
                Math.sin(x * k[1] + t * band.speeds[1] + band.offsets[1]) * heights[1] +
                Math.sin(x * k[2] + t * band.speeds[2] + band.offsets[2]) * heights[2]);

          const trace = () => {
            ctx.moveTo(0, surfaceAt(0));
            for (let x = step; x <= w + step; x += step) ctx.lineTo(x, surfaceAt(x));
          };

          //: The body: shallow and fading fast. The last stop is what every
          //: pixel below the gradient clamps to, so keeping it near zero is
          //: what stops the bands stacking into a solid block of colour.
          const reachDown = h * 0.24;
          const body = ctx.createLinearGradient(0, base - amp * 1.6, 0, base + reachDown);
          const density = (0.03 + band.depth * 0.035) * lift;
          body.addColorStop(0, rgba(accent, density * 1.9));
          body.addColorStop(0.16, rgba(accent, density));
          body.addColorStop(1, rgba(accent, density * 0.12));

          //: Closed just past where the gradient has faded out, not at the
          //: bottom of the canvas. Every band used to fill the whole depth
          //: beneath it, so five of them rasterised five overlapping
          //: full-height gradients per frame to show a few pixels of colour --
          //: it cost about half the frame budget and none of it was visible.
          //: The bands overlap enough that bounding them leaves no seam.
          const foot = Math.min(h, base + reachDown * 1.05);
          ctx.beginPath();
          trace();
          ctx.lineTo(w, foot);
          ctx.lineTo(0, foot);
          ctx.closePath();
          ctx.fillStyle = body;
          ctx.fill();

          //: The crest. Brighter than anything in the fill and only a pixel or
          //: so wide -- this is the line the eye actually reads as water, and
          //: it is why the body underneath can afford to be so faint.
          ctx.beginPath();
          trace();
          ctx.globalCompositeOperation = glowMode;
          ctx.strokeStyle = rgba(accent, (0.16 + band.depth * 0.16) * lift);
          ctx.lineWidth = 0.8 + band.depth * 0.5;
          ctx.stroke();
          ctx.globalCompositeOperation = "source-over";
        }
      },
    };
  },
};
