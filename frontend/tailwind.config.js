/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        surface: {
          base: "#F5F0EB",
          raised: "#FAF7F4",
          inset: "#EDE8E2",
          pressed: "#E5DFD8",
        },
        copper: {
          DEFAULT: "#B87333",
          light: "#D4956A",
          dark: "#8B5A2B",
          muted: "#C9A47C",
        },
        warm: {
          50: "#FDFCFB",
          100: "#FAF7F4",
          200: "#F0EBE4",
          300: "#E0D6CA",
          400: "#C4B5A3",
          500: "#A89682",
          600: "#8B7A68",
          700: "#6B5D4F",
          800: "#4A4039",
          900: "#2E2722",
        },
      },
      fontFamily: {
        display: ['"Playfair Display"', "Georgia", "serif"],
        body: ['"DM Sans"', "system-ui", "sans-serif"],
      },
      borderRadius: {
        tactile: "12px",
      },
      boxShadow: {
        "soft": "0 2px 8px rgba(139,122,104,0.08), 0 1px 3px rgba(139,122,104,0.06)",
        "mid": "0 4px 16px rgba(139,122,104,0.10), 0 2px 6px rgba(139,122,104,0.08)",
        "deep": "0 8px 32px rgba(139,122,104,0.14), 0 4px 12px rgba(139,122,104,0.10)",
        "inset": "inset 0 2px 4px rgba(139,122,104,0.10)",
      },
    },
  },
  plugins: [],
};
