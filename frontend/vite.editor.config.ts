import { defineConfig } from "vite";

// The knowledge rich-text editor (TipTap) as its own static IIFE, loaded only on the editor page. A second
// config because Rollup can't emit two IIFE entries from one build. Styles live in src/input.css.
export default defineConfig({
  build: {
    outDir: "../app/static",
    emptyOutDir: false,
    rollupOptions: {
      input: "src/knowledge-editor.ts",
      output: {
        entryFileNames: "js/knowledge-editor.js",
        format: "iife",
        name: "MallenKnowledgeEditor",
      },
    },
  },
});
