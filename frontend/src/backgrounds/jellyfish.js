/**
 * Jellyfish.
 *
 * Two things carry this. The motion is pulsed rather than even: a jellyfish
 * squeezes its bell, surges, then coasts while the bell refills, so one sine
 * drives both the shape and the speed -- drawing the pulse without the surge
 * reads as a throbbing balloon rather than as swimming.
 *
 * And each one swims somewhere of its own. They carry a heading that wanders on
 * a slow sine, so the paths curve instead of running in parallel, and the bell
 * turns to face the way it is going -- a bell drawn upright while travelling
 * sideways is the tell that gives away a field of sprites all sharing one
 * direction. Three species, at their own sizes, keep them from reading as one
 * shape at three scales.
 */

const SEGMENTS = 14;
const BASE = 26;

//: The shape differences are what make a field look populated rather than
//: duplicated: a wide shallow dome with a fringe of short tentacles reads as a
//: different animal from a tall narrow one trailing four long arms, even at a
//: glance and even at the same size.
const SPECIES = [
  {
    // Moon jelly: broad and shallow, a dense fringe of short tentacles. Flat,
    // but not as flat as a real one -- much under about 0.7 tall and the dome
    // turns into a blade the moment it tilts, the same failure the bell jelly
    // has in the other direction.
    dome: [1.1, 0.72],
    tentacles: [9, 13],
    reach: [1.3, 2.0],
    thickness: 0.55,
    scale: [0.72, 1.2],
    pulse: [0.4, 0.6],
    speed: [9, 15],
  },
  {
    // Bell jelly: taller than it is wide, a few long trailing arms. Not as
    // narrow as it wants to be -- past about 0.8 the silhouette turns into a
    // blade as soon as it is travelling anywhere but straight up.
    dome: [0.84, 1.14],
    tentacles: [4, 6],
    reach: [3.0, 4.5],
    thickness: 1.05,
    scale: [0.55, 0.95],
    pulse: [0.62, 0.9],
    speed: [15, 23],
  },
  {
    // A small fast drifter, to break up the scale of the other two.
    dome: [0.95, 0.85],
    tentacles: [6, 8],
    reach: [1.9, 2.9],
    thickness: 0.7,
    scale: [0.3, 0.5],
    pulse: [0.8, 1.1],
    speed: [19, 29],
  },
];

const between = ([low, high]) => low + Math.random() * (high - low);

const UP = -Math.PI / 2;

function spawn(width, height, atEdge, slot, of) {
  const species = SPECIES[Math.floor(Math.random() * SPECIES.length)];
  const jelly = {
    species,
    scale: between(species.scale),
    tentacles: Math.round(between(species.tentacles)),
    reach: between(species.reach),
    phase: Math.random() * Math.PI * 2,
    pulseRate: between(species.pulse),
    speed: between(species.speed),
    //: How far off vertical this one swings, and which way it leans while it
    //: does. Together they stay under about 69 degrees from straight up, which
    //: is a spread of nearly 140 degrees across the field and still leaves
    //: every one of them climbing. Past horizontal it would be swimming
    //: bell-first downward with its tentacles streaming ahead of it, which is
    //: the one heading that stops reading as a jellyfish.
    arc: 0.2 + Math.random() * 0.65,
    //: Which way this one leans on average. Without it every heading swings
    //: about straight up, so the horizontal drift cancels over each swing and
    //: whatever clustering the field is born with it keeps forever -- the
    //: jellyfish rise in their own columns and the gaps between them never
    //: close. A standing lean is what makes them cross the panel and spread.
    bias: (Math.random() - 0.5) * 0.7,
    wanderRate: 0.05 + Math.random() * 0.14,
    wanderPhase: Math.random() * Math.PI * 2,
    heading: UP,
  };
  if (atEdge) {
    //: They travel broadly upward, so they leave by the top and the sides and
    //: have to come back from the bottom and the sides. Re-entering from the
    //: top would put one on screen only to walk it straight back off.
    const margin = BASE * jelly.scale * 4;
    const side = Math.random();
    if (side < 0.6) { jelly.x = Math.random() * width; jelly.y = height + margin; }
    else if (side < 0.8) { jelly.x = -margin; jelly.y = Math.random() * height; }
    else { jelly.x = width + margin; jelly.y = Math.random() * height; }
  } else {
    //: The first field is dealt one to a column rather than at random. Eight
    //: uniform draws clump often enough to notice, and a clump on the first
    //: frame is the one a reader actually sees.
    jelly.x = ((slot + Math.random()) / of) * width;
    jelly.y = Math.random() * height;
  }
  return jelly;
}

export default {
  id: "jellyfish",

  //: The one sparse field made of real shapes: bells are filled radial gradients
  //: tens of pixels across, so there is a shape to diffuse rather than a line to
  //: lose, and the light arrives concentrated enough that this ends up the
  //: strongest diffusion of the four in absolute terms -- alpha 0.67 at the
  //: brightest cell against Rain's 0.13. The gain is nonetheless the smallest,
  //: because a lift multiplies what is already there: at three, Vivid saturates
  //: an eighth of the cells it lights and the bells start reading as one flat
  //: colour instead of tracking the swim. Two leaves a bell's core white-hot and
  //: the rest of it modulated, which is what a luminous body looks like.
  bloom: 2,

  create({ width, height, palette, intensity }) {
    let w = width;
    let h = height;
    let colours = palette;
    //: Four at Subtle, six at Medium, eight at Vivid. More than before, because
    //: three species need enough bodies on screen to read as a mix.
    const count = Math.max(4, Math.min(8, Math.round(6 * intensity.density)));
    const alpha = intensity.alpha;
    const items = Array.from({ length: count }, (_, i) => spawn(w, h, false, i, count));

    function draw(ctx, jelly, t) {
      const { accent, isLight, rgba, glowMode } = colours;
      //: Low alphas thin out over a light ground, where there is no additive
      //: light to help them; Paper needs a little more to read at all.
      const lift = (isLight ? 1.35 : 1) * alpha;
      const [domeW, domeH] = jelly.species.dome;

      const squeeze = Math.sin(jelly.phase);
      const halfWidth = BASE * jelly.scale * domeW * (1 - 0.2 * squeeze);
      const bell = BASE * 0.85 * jelly.scale * domeH * (1 + 0.16 * squeeze);

      ctx.save();
      ctx.translate(jelly.x, jelly.y);
      //: The bell is drawn with its dome toward -y, and a heading of -pi/2 is
      //: up, so this is zero for one swimming straight up and turns it to face
      //: every other direction.
      ctx.rotate(jelly.heading + Math.PI / 2);

      //: Tentacles first, so the bell's glow sits over the top of them where
      //: they meet it and the join does not show as a seam.
      const skirt = bell * 0.12;
      for (let i = 0; i < jelly.tentacles; i += 1) {
        const across = jelly.tentacles === 1 ? 0.5 : i / (jelly.tentacles - 1);
        const originX = (across - 0.5) * halfWidth * 1.45;
        const length = bell * jelly.reach * (0.82 + ((i * 7) % 5) * 0.09);

        ctx.beginPath();
        ctx.moveTo(originX, skirt);
        for (let s = 1; s <= SEGMENTS; s += 1) {
          const along = s / SEGMENTS;
          //: Amplitude grows along the length and the phase lags behind it, so
          //: the wave travels down the tentacle rather than the whole strand
          //: swinging as one rigid piece.
          const sway =
            Math.sin(t * 1.5 + jelly.phase + i * 0.8 - along * 3.4) * halfWidth * 0.34 * along;
          ctx.lineTo(originX + sway + originX * 0.3 * along, skirt + length * along);
        }
        const fade = ctx.createLinearGradient(0, skirt, 0, skirt + length);
        fade.addColorStop(0, rgba(accent, 0.2 * lift));
        fade.addColorStop(1, rgba(accent, 0));
        ctx.strokeStyle = fade;
        ctx.lineWidth = Math.max(0.5, jelly.species.thickness * jelly.scale);
        ctx.stroke();
      }

      ctx.beginPath();
      ctx.moveTo(-halfWidth, 0);
      ctx.bezierCurveTo(-halfWidth, -bell * 1.6, halfWidth, -bell * 1.6, halfWidth, 0);
      ctx.quadraticCurveTo(halfWidth * 0.55, bell * 0.3, 0, skirt);
      ctx.quadraticCurveTo(-halfWidth * 0.55, bell * 0.3, -halfWidth, 0);
      ctx.closePath();

      const body = ctx.createRadialGradient(0, -bell * 0.45, 0, 0, -bell * 0.45, bell * 1.7);
      body.addColorStop(0, rgba(accent, 0.26 * lift));
      body.addColorStop(0.55, rgba(accent, 0.12 * lift));
      body.addColorStop(1, rgba(accent, 0));
      //: Additive on a dark ground makes the overlap of two bells brighten the
      //: way real translucency does. On a light one it would erase them.
      ctx.globalCompositeOperation = glowMode;
      ctx.fillStyle = body;
      ctx.fill();

      ctx.globalCompositeOperation = "source-over";
      ctx.strokeStyle = rgba(accent, 0.3 * lift);
      ctx.lineWidth = Math.max(0.7, 1.2 * jelly.scale);
      ctx.stroke();

      ctx.restore();
    }

    return {
      resize(nextWidth, nextHeight) {
        //: Keep the field where it was in proportion rather than re-seeding, so
        //: collapsing the sidebar slides them across instead of replacing them
        //: with a new set mid-glance.
        const scaleX = nextWidth / (w || nextWidth);
        const scaleY = nextHeight / (h || nextHeight);
        for (const jelly of items) {
          jelly.x *= scaleX;
          jelly.y *= scaleY;
        }
        w = nextWidth;
        h = nextHeight;
      },

      retint(next) {
        colours = next;
      },

      frame(ctx, dt, t) {
        for (const jelly of items) {
          jelly.phase += dt * jelly.pulseRate * Math.PI * 2;
          //: The heading is stated outright each frame rather than integrated
          //: from a turn rate. Integrating drifts: a small bias accumulates
          //: until everything is swimming the same way, or wanders into the
          //: downward headings this is meant to keep out of. Reading it off a
          //: sine keeps every one of them inside its own arc for good, while
          //: still curving smoothly, and gives each a different swing and
          //: period so no two trace the same path.
          jelly.heading =
            UP + jelly.bias + Math.sin(t * jelly.wanderRate + jelly.wanderPhase) * jelly.arc;

          //: Thrust only on the contracting half of the pulse: the bell pushes
          //: water on the squeeze and coasts on the refill.
          const thrust = Math.max(0, Math.sin(jelly.phase));
          const drive = dt * jelly.speed * (0.35 + 1.2 * thrust);
          jelly.x += Math.cos(jelly.heading) * drive;
          jelly.y += Math.sin(jelly.heading) * drive;

          const margin = BASE * jelly.scale * 4;
          if (
            jelly.x < -margin || jelly.x > w + margin ||
            jelly.y < -margin || jelly.y > h + margin
          ) {
            Object.assign(jelly, spawn(w, h, true));
          }

          draw(ctx, jelly, t);
        }
      },
    };
  },
};
