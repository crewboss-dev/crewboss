import typography from "@tailwindcss/typography";

// Knowledge bodies (`.kb-prose`) — Tailwind Typography tuned to the theme tokens and the dashboard's type
// scale (DESIGN.md §2–3). The plugin can't resolve Tailwind's <alpha-value> colours, so these are plain CSS that read
// the theme-token channels (app/static/css/theme-tokens.css, the ERP's file): they swap under html.dark by themselves,
// so there is no separate dark (prose-invert) set any more (#479).
const tok = (name, a) => (a === undefined ? `rgb(var(--c-${name}))` : `rgb(var(--c-${name}) / ${a})`);
const proseColours = {
  "--tw-prose-body": tok("charcoal", .86), "--tw-prose-headings": tok("charcoal"), "--tw-prose-lead": tok("charcoal", .75),
  "--tw-prose-links": tok("indigo"), "--tw-prose-bold": tok("charcoal"), "--tw-prose-counters": tok("charcoal", .55),
  "--tw-prose-bullets": tok("charcoal", .35), "--tw-prose-hr": tok("line"), "--tw-prose-quotes": tok("charcoal", .8),
  "--tw-prose-quote-borders": tok("indigo", .35), "--tw-prose-captions": tok("charcoal", .55),
  "--tw-prose-code": tok("charcoal"), "--tw-prose-pre-code": tok("charcoal", .92), "--tw-prose-pre-bg": tok("creamwarm"),
  "--tw-prose-th-borders": tok("charcoal", .22), "--tw-prose-td-borders": tok("line"),
};

/** Design tokens. Dashboard register = the ERP's:
 * operational instrument, dense, dark-mode capable, system sans (no display serif). */
export default {
  darkMode: "class",
  content: [
    "../app/templates/**/*.html",
    "../app/**/*.py",
    "../app/static/js/*.js",
    "./src/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      /* THE ERP'S COLOUR TOKENS (#479): the same names and the same mapping as the ERP's tailwind.config.js, reading the
         RGB channel variables in app/static/css/theme-tokens.css (a verbatim copy of the ERP's). Dark mode swaps the
         channels, so `text-charcoal` is the ink and `bg-cream` the page in BOTH themes, and the palette needs no `dark:`
         class. indigo = the accent (terracotta; coral in dark), indigodeep = the solid fill behind white text, flame =
         urgent / needs you (crimson), focus = keyboard focus (blue). */
      colors: {
        charcoal: "rgb(var(--c-charcoal) / <alpha-value>)",
        cream: "rgb(var(--c-cream) / <alpha-value>)",
        creamwarm: "rgb(var(--c-creamwarm) / <alpha-value>)",
        line: "rgb(var(--c-line) / <alpha-value>)",
        surface: "rgb(var(--c-surface) / <alpha-value>)",
        indigo: "rgb(var(--c-indigo) / <alpha-value>)",
        indigodeep: "rgb(var(--c-indigodeep) / <alpha-value>)",
        nearblack: "#141413",
        flame: "rgb(var(--c-flame) / <alpha-value>)",
        focus: "rgb(var(--c-focus) / <alpha-value>)",
        // The status ramp, fixed in both modes (DESIGN.md §2). The ERP names them ok/warn/…; here they are status-*.
        status: {
          ok: "rgb(46 125 82 / <alpha-value>)",
          warn: "rgb(201 162 39 / <alpha-value>)",
          avg: "rgb(232 130 30 / <alpha-value>)",
          high: "rgb(226 86 43 / <alpha-value>)",
          disaster: "rgb(198 40 40 / <alpha-value>)",
        },
        // Workflow step KIND accents (canvas cards). Categorical, not semantic: always paired with the kind's word + a
        // system icon on the card (mallen-design §5: colour is never the only encoding). trigger = the accent; ai = the
        // ERP's AI series blue (--stream-ai), no longer flame, which now says only "needs you" (#479).
        kind: {
          trigger: "rgb(var(--c-indigo) / <alpha-value>)",
          precode: "rgb(96 108 128 / <alpha-value>)",
          ai: "rgb(42 120 214 / <alpha-value>)",
          logic: "rgb(201 162 39 / <alpha-value>)",
          action: "rgb(31 122 140 / <alpha-value>)",
        },
      },
      /* The type scale. micro/small/base/lead/title are the dashboard's names; 2xs…3xl are the ERP's (the same steps,
         leading and tracking), so markup copied from the ERP's chrome renders here at exactly the ERP's size. */
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1.35", letterSpacing: "0.04em" }],
        xs: ["0.75rem", { lineHeight: "1.4", letterSpacing: "0.01em" }],
        sm: ["0.8125rem", { lineHeight: "1.45", letterSpacing: "0" }],
        lg: ["1.0625rem", { lineHeight: "1.35", letterSpacing: "-0.01em" }],
        xl: ["1.25rem", { lineHeight: "1.25", letterSpacing: "-0.015em" }],
        "2xl": ["1.5rem", { lineHeight: "1.15", letterSpacing: "-0.022em" }],
        "3xl": ["1.875rem", { lineHeight: "1.1", letterSpacing: "-0.028em" }],
        micro: ["0.6875rem", { lineHeight: "1.35", letterSpacing: "0.04em" }],
        small: ["0.8125rem", { lineHeight: "1.45", letterSpacing: "0" }],
        base: ["0.9375rem", { lineHeight: "1.55", letterSpacing: "0" }],
        lead: ["1.0625rem", { lineHeight: "1.35", letterSpacing: "-0.01em" }],
        title: ["1.375rem", { lineHeight: "1.15", letterSpacing: "-0.022em" }],
      },
      fontFamily: {
        sans: ["-apple-system", "BlinkMacSystemFont", "Segoe UI", "system-ui", "Roboto", "Helvetica",
               "Arial", "sans-serif"],
      },
      transitionTimingFunction: {
        physics: "cubic-bezier(.25,.1,.25,1)",
        "physics-out": "cubic-bezier(.75,0,.75,.9)",
      },
      typography: {
        DEFAULT: {
          css: {
            ...proseColours,
            // the dashboard's scale: base 0.9375rem/1.6; headings build hierarchy from weight + size + leading
            fontSize: "0.9375rem",
            lineHeight: "1.6",
            maxWidth: "72ch",
            p: { marginTop: "0.75em", marginBottom: "0.75em" },
            h1: { fontSize: "1.375rem", lineHeight: "1.15", letterSpacing: "-0.022em", fontWeight: "700", marginTop: "1.4em", marginBottom: "0.6em" },
            h2: { fontSize: "1.1875rem", lineHeight: "1.25", letterSpacing: "-0.016em", fontWeight: "650", marginTop: "1.6em", marginBottom: "0.55em" },
            h3: { fontSize: "1.0625rem", lineHeight: "1.35", letterSpacing: "-0.01em", fontWeight: "650", marginTop: "1.4em", marginBottom: "0.45em" },
            h4: { fontSize: "0.9375rem", lineHeight: "1.4", fontWeight: "650", marginTop: "1.25em", marginBottom: "0.4em" },
            "h1:first-child, h2:first-child, h3:first-child, h4:first-child": { marginTop: "0" },
            a: { fontWeight: "500", textUnderlineOffset: "2px", textDecorationThickness: "1px" },
            strong: { fontWeight: "650" },
            "ul, ol": { marginTop: "0.6em", marginBottom: "0.6em", paddingInlineStart: "1.35em" },
            li: { marginTop: "0.2em", marginBottom: "0.2em" },
            "li > p": { marginTop: "0.25em", marginBottom: "0.25em" },
            blockquote: { fontStyle: "normal", fontWeight: "400", borderInlineStartWidth: "3px", paddingInlineStart: "0.9em" },
            // (one key per pseudo-element: the plugin wraps keys in :where(), which can't hold ::before)
            "blockquote p:first-of-type::before": { content: "none" },
            "blockquote p:last-of-type::after": { content: "none" },
            hr: { marginTop: "1.75em", marginBottom: "1.75em" },
            code: {
              fontSize: "0.85em", fontWeight: "500", padding: "0.12em 0.36em", borderRadius: "0.3rem",
              backgroundColor: tok("charcoal", .06),
            },
            "code::before": { content: "none" },
            "code::after": { content: "none" },
            pre: { fontSize: "0.8125rem", lineHeight: "1.55", borderRadius: "0.6rem", padding: "0.85em 1em",
                   border: `1px solid ${tok("line")}` },
            "pre code": { backgroundColor: "transparent", padding: "0", fontWeight: "400" },
            // tables are dense data UI: the small step, tabular digits, and they scroll sideways on a phone
            table: { fontSize: "0.8125rem", lineHeight: "1.45", display: "block", overflowX: "auto", maxWidth: "100%",
                     marginTop: "1em", marginBottom: "1.25em", fontVariantNumeric: "tabular-nums" },
            "thead th": { fontWeight: "650", verticalAlign: "bottom", paddingTop: "0.5em", paddingBottom: "0.5em" },
            "th, td": { paddingInlineStart: "0.75em", paddingInlineEnd: "0.75em" },
            "tbody td": { paddingTop: "0.5em", paddingBottom: "0.5em", verticalAlign: "top" },
            "thead th:first-child, tbody td:first-child": { paddingInlineStart: "0.75em" },
            "tbody tr:nth-child(even)": { backgroundColor: tok("charcoal", .025) },
            img: { borderRadius: "0.5rem", marginTop: "1em", marginBottom: "1em", height: "auto",
                   border: `1px solid ${tok("line")}` },
          },
        },
      },
      // Flat, like the ERP (DESIGN.md §2: "no heavy shadows"). Cards carry none; `card` is only the lift under a
      // selected segment; `float` = Tailwind's shadow-xl, which is what the ERP's menus use.
      boxShadow: {
        card: "0 1px 2px 0 rgb(0 0 0 / 0.06)",
        float: "0 20px 25px -5px rgb(0 0 0 / 0.1), 0 8px 10px -6px rgb(0 0 0 / 0.1)",
      },
    },
  },
  plugins: [typography],
};
