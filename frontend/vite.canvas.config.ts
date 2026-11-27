import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// THE SHARED CANVAS (docs/canvas-contract.md): one self-contained IIFE (React + React Flow + dagre bundled in) and
// one stylesheet, for workflows and integration diagrams on Crewboss AND the ERP admin site, which copies
// app/static/canvas/ verbatim. Committed (not built in the image only) so the ERP copies exactly what was reviewed.
// CSP-safe for script-src 'self' / style-src 'self': CSS is extracted to mallen-canvas.css, never injected.
export default defineConfig({
  plugins: [react()],
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  build: {
    outDir: "../app/static/canvas",
    emptyOutDir: false,
    cssCodeSplit: false,
    sourcemap: false,
    rollupOptions: {
      input: "src/canvas/entry.tsx",
      output: {
        entryFileNames: "mallen-canvas.js",
        assetFileNames: (info) => (info.name && info.name.endsWith(".css") ? "mallen-canvas.css" : "[name][extname]"),
        format: "iife",
        name: "MallenCanvasBundle",
      },
    },
  },
});
