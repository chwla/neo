/**
 * A star field, and now and then something falling across it.
 *
 * The field is nearly static on purpose: it is the still thing that makes the
 * meteor read as fast. Both populations are drawn in the accent, so the whole
 * effect is one hue and follows the theme -- there is no white here, which
 * matters because Mono's accent already is near-white and Paper's is a dark
 * green on paper, and a hard-coded white star would vanish on one and look
 * wrong on the other.
 */

const STARS = 70;
//: The pool, before intensity has its say. It was three, with a gap of two to
//: seven seconds each, which left the sky empty more than half the time -- the
//: field is meant to be the still thing that makes a meteor read as fast, and
//: it was doing that to an audience of nobody. Six, and a shorter gap, keeps
//: the stillness between passes while making the passes the thing you stay for.
//: They are pooled rather than allocated so a long session does not churn
//: objects once a second.
const METEORS = 6;

//: Seconds between one meteor finishing and the same one starting again. The
//: floor matters more than the spread: it is what stops two of the six going
//: off together often enough to read as a shower.
const WAIT_FLOOR = 1.4;
const WAIT_SPREAD = 2.6;

function spawnStar(width, height) {
  return {
    x: Math.random() * width,
    y: Math.random() * height,
    radius: 0.5 + Math.random() * 1.2,
    //: Depth drives size, brightness and drift together, so the near stars are
    //: the big bright fast ones and the parallax reads as distance.
    depth: 0.3 + Math.random() * 0.7,
    phase: Math.random() * Math.PI * 2,
    rate: 0.25 + Math.random() * 0.75,
  };
}

function arm(meteor, width, height, immediate, pace = 1) {
  meteor.wait = immediate
    ? Math.random() * 1.2 * pace
    : (WAIT_FLOOR + Math.random() * WAIT_SPREAD) * pace;
  meteor.life = 0;
  //: Down and to the right, from somewhere above the left two-thirds. Starting
  //: off the top edge means the streak is already at full length when it
  //: arrives rather than growing out of a point at the boundary.
  const angle = (20 + Math.random() * 14) * (Math.PI / 180);
  meteor.dirX = Math.cos(angle);
  meteor.dirY = Math.sin(angle);
  meteor.speed = 380 + Math.random() * 260;
  meteor.length = 90 + Math.random() * 120;
  meteor.x = Math.random() * width * 0.75 - width * 0.1;
  meteor.y = -Math.random() * height * 0.25 - 20;
  meteor.span = 0.55 + Math.random() * 0.5;
}

export default {
  id: "stars",

  //: 0.0058% covered, the sparsest field of the four: seventy dots of radius
  //: one or two. The distribution is what matters rather than the peak -- at ten
  //: the median cell lands near alpha 0.013, so an ordinary star gains no halo
  //: at all, while the top of the range carries the meteor and its head still
  //: reads as light passing behind the glass. Sixteen haloes the dots too.
  bloom: 10,

  create({ width, height, palette, intensity }) {
    let w = width;
    let h = height;
    let colours = palette;
    const alpha = intensity.alpha;
    const count = Math.max(12, Math.round(STARS * intensity.density));
    const field = Array.from({ length: count }, () => spawnStar(w, h));

    //: Meteors follow the intensity the star count always has, which they never
    //: used to: all three settings ran the same three meteors on the same gap,
    //: so the control moved the dots and said nothing about the part of this
    //: effect anyone watches for.
    //:
    //: Both halves move, because count alone is coarse at these numbers and gap
    //: alone leaves the sky able to hold only so many at once. The pace is the
    //: reciprocal, so denser means a shorter wait, and it is clamped: pushing
    //: Subtle out to a 1/0.6 gap would have made it quieter than it was before
    //: this change, which is not what "more shooting stars" asked for. Every
    //: setting ends up above where it started -- roughly half again at Subtle,
    //: three times at Medium, four and a half at Vivid.
    const meteorCount = Math.max(3, Math.round(METEORS * intensity.density));
    const pace = Math.min(1.25, Math.max(0.85, 1 / intensity.density));
    const meteors = Array.from({ length: meteorCount }, () => {
      const meteor = {};
      arm(meteor, w, h, true, pace);
      return meteor;
    });

    return {
      resize(nextWidth, nextHeight) {
        const scaleX = nextWidth / (w || nextWidth);
        const scaleY = nextHeight / (h || nextHeight);
        for (const star of field) {
          star.x *= scaleX;
          star.y *= scaleY;
        }
        w = nextWidth;
        h = nextHeight;
      },

      retint(next) {
        colours = next;
      },

      frame(ctx, dt, t) {
        const { accent, isLight, rgba, glowMode } = colours;
        const lift = (isLight ? 1.4 : 1) * alpha;

        for (const star of field) {
          //: Barely moving -- a few pixels a second at the very front. Enough
          //: that the sky is not a photograph, not enough to track.
          star.x -= dt * 2.2 * star.depth;
          if (star.x < -2) {
            star.x = w + 2;
            star.y = Math.random() * h;
          }

          const twinkle = 0.5 + 0.5 * Math.sin(t * star.rate * Math.PI * 2 + star.phase);
          const shine = (0.18 + 0.46 * twinkle) * star.depth * lift;
          ctx.beginPath();
          ctx.arc(star.x, star.y, star.radius * star.depth, 0, Math.PI * 2);
          ctx.fillStyle = rgba(accent, shine);
          ctx.fill();
        }

        for (const meteor of meteors) {
          if (meteor.wait > 0) {
            meteor.wait -= dt;
            continue;
          }

          meteor.life += dt;
          meteor.x += dt * meteor.speed * meteor.dirX;
          meteor.y += dt * meteor.speed * meteor.dirY;

          //: Faded in and out over the pass rather than clipped at the edges,
          //: so one does not blink into existence in the middle of the panel.
          const progress = meteor.life / meteor.span;
          if (progress >= 1 || meteor.y - meteor.length > h || meteor.x - meteor.length > w) {
            arm(meteor, w, h, false, pace);
            continue;
          }
          const strength = Math.sin(Math.PI * progress) * lift;

          const tailX = meteor.x - meteor.dirX * meteor.length;
          const tailY = meteor.y - meteor.dirY * meteor.length;
          const trail = ctx.createLinearGradient(meteor.x, meteor.y, tailX, tailY);
          trail.addColorStop(0, rgba(accent, 0.75 * strength));
          trail.addColorStop(1, rgba(accent, 0));

          ctx.globalCompositeOperation = glowMode;
          ctx.beginPath();
          ctx.moveTo(meteor.x, meteor.y);
          ctx.lineTo(tailX, tailY);
          ctx.strokeStyle = trail;
          ctx.lineWidth = 1.7;
          ctx.lineCap = "round";
          ctx.stroke();

          const head = ctx.createRadialGradient(meteor.x, meteor.y, 0, meteor.x, meteor.y, 5);
          head.addColorStop(0, rgba(accent, 0.9 * strength));
          head.addColorStop(1, rgba(accent, 0));
          ctx.beginPath();
          ctx.arc(meteor.x, meteor.y, 5, 0, Math.PI * 2);
          ctx.fillStyle = head;
          ctx.fill();
          ctx.globalCompositeOperation = "source-over";
        }
      },
    };
  },
};
