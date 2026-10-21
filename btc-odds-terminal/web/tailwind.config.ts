import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: "class",
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0A0D12",
        surface: "#0E1218",
        "surface-2": "#151B24",
        inset: "#0A0D12",
        border: { DEFAULT: "#1E2530", strong: "#2A3442" },
        text: { DEFAULT: "#E6E9EF", 2: "#C9CED8" },
        muted: "#A3ABB9",
        label: "#8A93A3",
        up: { DEFAULT: "#34D399", tint: "#123528" },
        down: { DEFAULT: "#F87171", tint: "#3A1818" },
        odds: { DEFAULT: "#5B9CFF", text: "#7FB2FF", tint: "#13213A" },
        fair: "#C29BFF",
        ptb: { DEFAULT: "#F5B942", tint: "#2A2111" },
        "neg-edge": "#F59E0B",
      },
      borderRadius: { ctl: "8px", card: "10px" },
      fontFamily: {
        sans: ["var(--font-inter)", "Inter", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "JetBrains Mono", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      fontSize: { base: ["13px", "18px"], label: ["11px", "14px"] },
      screens: { lg2: "1100px", xl2: "1280px" },
    },
  },
  plugins: [],
};
export default config;
