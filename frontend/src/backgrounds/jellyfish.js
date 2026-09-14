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

// The travelling wave has fixed offsets along an arm and between arms.
// Cache those offsets so drawing a jelly needs two trig calls for the whole
// fringe, rather than one for every vertex of every tentacle.
const ALONG = Float64Array.from({ length: SEGMENTS }, (_, s) => (s + 1) / SEGMENTS);
const SEGMENT_SIN = ALONG.map((along) => Math.sin(-along * 3.4));
const SEGMENT_COS = ALONG.map((along) => Math.cos(-along * 3.4));
const ARM_SIN = Float64Array.from({ length: 13 }, (_, i) => Math.sin(i * 0.8));
const ARM_COS = Float64Array.from({ length: 13 }, (_, i) => Math.cos(i * 0.8));

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

    /**
     * The bells and the tentacle fades, kept rather than rebuilt.
     *
     * Eight jellyfish with up to thirteen arms each is around ninety-six
     * gradient objects a frame -- one `createRadialGradient` per bell and one
     * `createLinearGradient` per tentacle -- every one of them built, filled
     * with colour stops parsed from freshly assembled strings, used once and
     * dropped. At sixty frames a second that is most of six thousand short-lived
     * objects a second for a field of eight animals.
     *
     * None of them is as varied as that makes it sound. A tentacle's fade is
     * fixed by where it starts and how far it runs, and a bell's by its radius;
     * all three follow the pulse, which is a sine, so across a whole field over
     * a whole second they cover a narrow band of values many times over. Rounded
     * to half a pixel at the skirt and two along the length -- a shift far below
     * what the eye resolves on a fade that spans tens of pixels -- the same few
     * hundred gradients serve every frame.
     *
     * Dropped on retint, which is the only thing that can change the colour in
     * them.
     */
    let fades = new Map();
    let bells = new Map();
    let outline = null;
    //: Four at Subtle, six at Medium, eight at Vivid. More than before, because
    //: three species need enough bodies on screen to read as a mix.
    const count = Math.max(4, Math.min(8, Math.round(6 * intensity.density)));
    const alpha = intensity.alpha;
    const items = Array.from({ length: count }, (_, i) => spawn(w, h, false, i, count));

    //: Quantised so the cache can hit. The tentacle's fade is keyed on both
    //: ends of it; the two are packed into one integer so the lookup costs no
    //: string.
    function fadeFor(ctx, skirt, length, lift) {
      const { accent, rgba } = colours;
      const qSkirt = Math.round(skirt * 2);
      const qLength = Math.max(1, Math.round(length / 2));
      const key = qSkirt * 4096 + qLength;
      let fade = fades.get(key);
      if (fade === undefined) {
        const from = qSkirt / 2;
        const to = from + qLength * 2;
        fade = ctx.createLinearGradient(0, from, 0, to);
        fade.addColorStop(0, rgba(accent, 0.2 * lift));
        fade.addColorStop(1, rgba(accent, 0));
        fades.set(key, fade);
      }
      return fade;
    }

    function bellFor(ctx, bell, lift) {
      const { accent, rgba } = colours;
      const key = Math.max(1, Math.round(bell * 2));
      let body = bells.get(key);
      if (body === undefined) {
        const size = key / 2;
        body = ctx.createRadialGradient(0, -size * 0.45, 0, 0, -size * 0.45, size * 1.7);
        body.addColorStop(0, rgba(accent, 0.26 * lift));
        body.addColorStop(0.55, rgba(accent, 0.12 * lift));
        body.addColorStop(1, rgba(accent, 0));
        bells.set(key, body);
      }
      return body;
    }

    function draw(ctx, jelly, t, squeeze) {
      const { accent, isLight, rgba, glowMode } = colours;
      //: Low alphas thin out over a light ground, where there is no additive
      //: light to help them; Paper needs a little more to read at all.
      const lift = (isLight ? 1.35 : 1) * alpha;
      const [domeW, domeH] = jelly.species.dome;

      const halfWidth = BASE * jelly.scale * domeW * (1 - 0.2 * squeeze);
      const bell = BASE * 0.85 * jelly.scale * domeH * (1 + 0.16 * squeeze);
      // Continue simulating offscreen animals, but do not submit their paths.
      // This radius includes every heading, the longest arm and its full sway.
      const radius = halfWidth * 1.3 + bell * Math.max(1.6, 0.12 + jelly.reach * 1.18);
      if (jelly.x + radius < 0 || jelly.x - radius > w || jelly.y + radius < 0 || jelly.y - radius > h) return;

      ctx.save();
      ctx.translate(jelly.x, jelly.y);
      //: The bell is drawn with its dome toward -y, and a heading of -pi/2 is
      //: up, so this is zero for one swimming straight up and turns it to face
      //: every other direction.
      ctx.rotate(jelly.heading + Math.PI / 2);

      //: Tentacles first, so the bell's glow sits over the top of them where
      //: they meet it and the join does not show as a seam.
      const skirt = bell * 0.12;
      const wavePhase = t * 1.5 + jelly.phase;
      const waveSin = Math.sin(wavePhase);
      const waveCos = Math.cos(wavePhase);
      ctx.lineWidth = Math.max(0.5, jelly.species.thickness * jelly.scale);
      for (let i = 0; i < jelly.tentacles; i += 1) {
        const across = jelly.tentacles === 1 ? 0.5 : i / (jelly.tentacles - 1);
        const originX = (across - 0.5) * halfWidth * 1.45;
        const length = bell * jelly.reach * (0.82 + ((i * 7) % 5) * 0.09);
        const phaseSin = waveSin * ARM_COS[i] + waveCos * ARM_SIN[i];
        const phaseCos = waveCos * ARM_COS[i] - waveSin * ARM_SIN[i];
        const swayWidth = halfWidth * 0.34;
        const drift = originX * 0.3;

        ctx.beginPath();
        ctx.moveTo(originX, skirt);
        for (let s = 0; s < SEGMENTS; s += 1) {
          const along = ALONG[s];
          //: Amplitude grows along the length and the phase lags behind it, so
          //: the wave travels down the tentacle rather than the whole strand
          //: swinging as one rigid piece.
          const sway = (phaseSin * SEGMENT_COS[s] + phaseCos * SEGMENT_SIN[s]) * swayWidth * along;
          ctx.lineTo(originX + sway + drift * along, skirt + length * along);
        }
        ctx.strokeStyle = fadeFor(ctx, skirt, length, lift);
        ctx.stroke();
      }

      ctx.beginPath();
      ctx.moveTo(-halfWidth, 0);
      ctx.bezierCurveTo(-halfWidth, -bell * 1.6, halfWidth, -bell * 1.6, halfWidth, 0);
      ctx.quadraticCurveTo(halfWidth * 0.55, bell * 0.3, 0, skirt);
      ctx.quadraticCurveTo(-halfWidth * 0.55, bell * 0.3, -halfWidth, 0);
      ctx.closePath();

      const body = bellFor(ctx, bell, lift);
      //: Additive on a dark ground makes the overlap of two bells brighten the
      //: way real translucency does. On a light one it would erase them.
      ctx.globalCompositeOperation = glowMode;
      ctx.fillStyle = body;
      ctx.fill();

      ctx.globalCompositeOperation = "source-over";
      if (!outline) outline = rgba(accent, 0.3 * lift);
      ctx.strokeStyle = outline;
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
        fades = new Map();
        bells = new Map();
        outline = null;
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
          let squeeze = Math.sin(jelly.phase);
          const thrust = Math.max(0, squeeze);
          const drive = dt * jelly.speed * (0.35 + 1.2 * thrust);
          jelly.x += Math.cos(jelly.heading) * drive;
          jelly.y += Math.sin(jelly.heading) * drive;

          const margin = BASE * jelly.scale * 4;
          if (
            jelly.x < -margin || jelly.x > w + margin ||
            jelly.y < -margin || jelly.y > h + margin
          ) {
            Object.assign(jelly, spawn(w, h, true));
            squeeze = Math.sin(jelly.phase);
          }

          draw(ctx, jelly, t, squeeze);
        }
      },
    };
  },
};
