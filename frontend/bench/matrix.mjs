/** Write a reproducible browser matrix to stdout; pass its JSON file to drive.mjs. */
const profile = process.argv[2] || "cpu4";
if (!["native", "cpu4", "4k"].includes(profile)) throw new Error("profile must be native, cpu4, or 4k");
const viewport = profile === "4k"
  ? { width: 3840, height: 2160, dpr: 2 }
  : { width: 1512, height: 982, dpr: 2 };
const cpu = profile === "cpu4" ? 4 : 1;
const cases = [];
for (const bg of ["jellyfish", "stars", "rain", "gradient"]) {
  for (const intensity of ["subtle", "medium", "vivid"]) {
    cases.push({ label: `${bg}-${intensity}-${profile}`, cpu, viewport, interact: true,
      params: { bg, intensity, warmup: 2000, seed: 1 } });
  }
}
cases.push({ label: `control-${profile}`, cpu, viewport, interact: true,
  params: { bg: "jellyfish", warmup: 2000, marks: "0", diffusion: "0", wash: "0",
    sidebarGlass: "0", stripGlass: "0", cardGlass: "0" } });
console.log(JSON.stringify(cases, null, 2));
