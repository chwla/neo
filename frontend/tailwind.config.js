/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        neo: {
          green: "#39ff14",
          black: "#030603",
          panel: "#071007",
          panel2: "#0b180b",
          text: "#eaffea",
          muted: "#89ad89",
        },
      },
      boxShadow: {
        neo: "0 0 22px rgba(57,255,20,.06)",
        "neo-button": "0 0 16px rgba(57,255,20,.25)",
      },
    },
  },
  plugins: [],
};
