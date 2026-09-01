// One icon set for the workspace pages. Paths follow the sidebar's NavIcon
// conventions: 24x24 box, no fill, currentColor stroke.
const ICON_PATHS = {
  back: ["m15 5-7 7 7 7"],
  plus: ["M12 5v14", "M5 12h14"],
  search: ["M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z", "m20 20-4-4"],
  pin: ["M9 3h6l-1 6 4 3v2h-5l-1 7-1-7H6v-2l4-3z"],
  archive: ["M3 5h18v4H3z", "M5 9v10h14V9", "M9 13h6"],
  trash: ["M4 6h16", "M9 6V4h6v2", "m6 6 1 14h10l1-14"],
  upload: ["M12 19V5", "m6 11 6-6 6 6"],
  download: ["M12 5v14", "m6 13 6 6 6-6"],
  file: ["M6 2h8l4 4v16H6z", "M14 2v5h5"],
  folder: ["M3 8a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2", "M3 8h18v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"],
  repo: ["M4 4h6l2 3h8v13H4z", "M8 12h8", "M8 16h5"],
  tasks: ["M5 4h14v16H5z", "m8 12 2 2 5-5"],
  note: ["M5 3h14v18H5z", "M8 8h8", "M8 12h8", "M8 16h5"],
  memory: ["M4 6c0-1.66 3.58-3 8-3s8 1.34 8 3", "M4 6v12c0 1.66 3.58 3 8 3s8-1.34 8-3V6"],
  play: ["m7 4 12 8-12 8z"],
  sparkle: ["M12 3v6", "M12 15v6", "M3 12h6", "M15 12h6"],
  link: ["M9 15 15 9", "M11 5h5a4 4 0 0 1 0 8h-2", "M13 19H8a4 4 0 0 1 0-8h2"],
  external: ["M14 4h6v6", "M20 4 11 13", "M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"],
  refresh: ["M20 12a8 8 0 1 1-2.34-5.66", "M20 4v5h-5"],
  clock: ["M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16z", "M12 8v4l3 2"],
  layers: ["m12 3 9 5-9 5-9-5z", "m3 13 9 5 9-5"],
  shield: ["M12 3l8 3v6c0 4.5-3.2 7.8-8 9-4.8-1.2-8-4.5-8-9V6z"],
  terminal: ["m5 7 4 4-4 4", "M12 15h7"],
  gauge: ["M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16z", "m12 12 4-3"],
  close: ["M6 6l12 12", "M18 6 6 18"],
  next: ["m9 5 7 7-7 7"],
  up: ["m5 15 7-7 7 7"],
  down: ["m5 9 7 7 7-7"],
  grid: ["M4 4h7v7H4z", "M13 4h7v7h-7z", "M4 13h7v7H4z", "M13 13h7v7h-7z"],
  rows: ["M4 6h16", "M4 12h16", "M4 18h16"],
  expand: ["M4 9V4h5", "M20 15v5h-5", "m4 4 6 6", "m20 20-6-6"],
  check: ["m5 12 5 5 9-11"],
  copy: ["M9 9h11v11H9z", "M15 5H4v11"],
  more: ["M6 12h.01", "M12 12h.01", "M18 12h.01"],
  stop: ["M7 7h10v10H7z"],
  globe: ["M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16z", "M4 12h16", "M12 4c2.6 2.6 2.6 13.4 0 16", "M12 4c-2.6 2.6-2.6 13.4 0 16"],
  tag: ["M4 4h7l9 9-7 7-9-9z", "M8 8h.01"],
  filter: ["M4 5h16l-6 7v6l-4 2v-8z"],
  image: ["M4 5h16v14H4z", "m5 16 4-5 3 3 3-4 4 6", "M9 9h.01"],
  preview: ["M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z", "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"],
  write: ["M4 20h4l10-10-4-4L4 16z", "m14 6 4 4"],
  split: ["M4 4h16v16H4z", "M12 4v16"],
  calendar: ["M4 6h16v14H4z", "M4 10h16", "M8 3v4", "M16 3v4"],
  bolt: ["m13 3-8 10h6l-1 8 8-10h-6z"],
  sort: ["M7 4v14", "m4 15 3 3 3-3", "M17 20V6", "m14 9 3-3 3 3"],
  book: ["M5 4h9a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z", "M17 7h2v13h-2"],
  warning: ["M12 4 3 20h18z", "M12 10v4", "M12 17h.01"],
  quote: ["M4 8h6v6a4 4 0 0 1-4 4", "M14 8h6v6a4 4 0 0 1-4 4"],
};

export default function Icon({ name, className = "" }) {
  return (
    <svg
      className={`ws-icon ${className}`.trim()}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {(ICON_PATHS[name] || []).map((path) => (
        <path d={path} key={path} />
      ))}
    </svg>
  );
}
