import type { Config } from "tailwindcss";

/**
 * GRX10 Automated Market Research Tool — design tokens.
 *
 * Semantic colour scales are exposed both as Tailwind utilities and as CSS
 * variables (see app/globals.css) so React components, Recharts and visx can
 * all read the same palette. The three domain colour systems are:
 *   - confidence  (high / medium / low)        — Q5 validation confidence
 *   - health      (7-state connector taxonomy)  — Q7
 *   - segment     (trade direction dimension)   — DOMESTIC/IMPORT/EXPORT/...
 */
const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // --- Brand / surfaces (GRX10 identity: magenta / purple / navy) ----
        brand: {
          DEFAULT: "#E1198B", // GRX10 magenta
          fg: "#ffffff",
          50: "#fdeff7",  // faint pink wash
          100: "#F5E6F0", // GRX10 light pink tint
          200: "#F0A0CC", // GRX10 soft magenta
          500: "#E1198B", // GRX10 magenta
          600: "#c4137a", // magenta hover/active
          700: "#6B2D8B", // GRX10 purple
          900: "#1A0A2E", // GRX10 dark navy
        },
        surface: {
          DEFAULT: "var(--surface)",
          subtle: "var(--surface-subtle)",
          raised: "var(--surface-raised)",
          inverse: "var(--surface-inverse)",
        },
        ink: {
          DEFAULT: "var(--ink)",
          muted: "var(--ink-muted)",
          subtle: "var(--ink-subtle)",
          inverse: "var(--ink-inverse)",
        },
        line: "var(--line)",

        // --- Confidence (Q5) ----------------------------------------------
        confidence: {
          high: "#059669", // emerald-600
          "high-bg": "#ecfdf5",
          medium: "#d97706", // amber-600
          "medium-bg": "#fffbeb",
          low: "#64748b", // slate-500 — weak signal, intentionally muted
          "low-bg": "#f1f5f9",
        },

        // --- Connector health 7-state (Q7) --------------------------------
        health: {
          ok: "#059669",
          "ok-bg": "#ecfdf5",
          auth: "#dc2626", // AUTH_FAILED — red-600
          "auth-bg": "#fef2f2",
          quota: "#ea580c", // QUOTA_EXHAUSTED — orange-600
          "quota-bg": "#fff7ed",
          rate: "#ca8a04", // RATE_LIMITED — yellow-600
          "rate-bg": "#fefce8",
          unreachable: "#475569", // UNREACHABLE — slate-600
          "unreachable-bg": "#f1f5f9",
          schema: "#7c3aed", // SCHEMA_MISMATCH — violet-600
          "schema-bg": "#f5f3ff",
          empty: "#94a3b8", // EMPTY — slate-400
          "empty-bg": "#f8fafc",
          budget: "#ea580c", // 🟠 budget pre-warning
          "budget-bg": "#fff7ed",
        },

        // --- Segment / trade direction ------------------------------------
        segment: {
          domestic: "#2563eb", // blue-600
          "domestic-bg": "#eff6ff",
          import: "#0891b2", // cyan-600
          "import-bg": "#ecfeff",
          export: "#7c3aed", // violet-600
          "export-bg": "#f5f3ff",
          self: "#0d9488", // teal-600 (SELF_CONSUME)
          "self-bg": "#f0fdfa",
          other: "#64748b",
          "other-bg": "#f1f5f9",
        },
      },
      borderRadius: {
        card: "0.75rem",
      },
      boxShadow: {
        card: "0 1px 2px 0 rgb(16 24 40 / 0.04), 0 1px 3px 0 rgb(16 24 40 / 0.06)",
        raised: "0 4px 12px -2px rgb(16 24 40 / 0.10)",
      },
      fontFamily: {
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        heading: ["var(--font-heading)", "var(--font-sans)", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
      },
    },
  },
  plugins: [],
};

export default config;
