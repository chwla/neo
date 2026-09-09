/** @type {import('tailwindcss').Config} */
// Tailwind is here for its preflight reset and nothing else -- the interface is
// written in hand-authored classes in index.css, and the one utility in the
// whole codebase is a `w-full`. There used to be a `theme.extend` with a `neo`
// palette in it; it was removed with the themes, because it was unused by every
// file and its values had drifted from the real ones (`#030603` against the
// actual background of `#0a0a0a`), so the only thing it could do was mislead
// whoever read it next. Colours live in index.css as custom properties, which
// is what the themes switch.
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: { extend: {} },
  plugins: [],
};
