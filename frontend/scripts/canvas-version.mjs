// Writes app/static/canvas/VERSION after `vite build --config vite.canvas.config.ts`: the contract version the
// bundle implements (docs/canvas-contract.md) and the git commit it was built from (+ "-dirty" when the canvas
// sources had uncommitted changes). Inside the Docker build there is no git: the committed VERSION is kept.
import { execSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const CONTRACT = "1";
const here = dirname(fileURLToPath(import.meta.url));
const out = join(here, "..", "..", "app", "static", "canvas", "VERSION");
let source = "";
try {
  const sha = execSync("git rev-parse --short=12 HEAD", { cwd: here, stdio: ["ignore", "pipe", "ignore"] }).toString().trim();
  const dirty = execSync("git status --porcelain -- src/canvas src/workflow-graph.ts src/workflow-structure.ts src/workflow-flow-layout.ts src/workflow-layout.ts vite.canvas.config.ts package-lock.json",
    { cwd: join(here, ".."), stdio: ["ignore", "pipe", "ignore"] }).toString().trim();
  source = sha + (dirty ? "-dirty" : "");
} catch {
  const prev = existsSync(out) ? readFileSync(out, "utf8").match(/^source=(.+)$/m) : null;
  source = prev ? prev[1] : "unknown";
}
writeFileSync(out, `contract=${CONTRACT}\nsource=${source}\n`);
console.log(`wrote ${out}: contract=${CONTRACT} source=${source}`);
