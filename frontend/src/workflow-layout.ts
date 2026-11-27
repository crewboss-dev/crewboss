// FALLBACK layered layout (dagre, bundled at build time — no CDN) for the few graphs that don't nest into
// Power Automate-style blocks (several roots, cross-branch merges, cycles — see workflow-structure.ts).
// Top-to-bottom, same card width as the structured view, so the two read as one design.
import dagre from "@dagrejs/dagre";

import { CARD_W } from "./workflow-flow-layout.ts";
import type { GraphEdge, GraphNode } from "./workflow-graph.ts";

export { CARD_W };
const RANK_SEP = 48; // vertical gap between cards
const NODE_SEP = 36; // horizontal gap between siblings

export type Placed = { id: string; x: number; y: number; h: number }; // top-left, React Flow convention

export function layoutGraph(nodes: GraphNode[], edges: GraphEdge[], cardH: (id: string) => number = () => 44): {
  positions: Map<string, Placed>;
  width: number;
  height: number;
} {
  const g = new dagre.graphlib.Graph({ multigraph: false });
  g.setGraph({ rankdir: "TB", ranksep: RANK_SEP, nodesep: NODE_SEP, edgesep: 24, marginx: 0, marginy: 0 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) g.setNode(n.id, { width: CARD_W, height: cardH(n.id) });
  for (const e of edges) {
    // A labelled branch gets room for its label pill between the ranks.
    g.setEdge(e.from, e.to, e.label ? { label: e.label, width: Math.min(160, e.label.length * 6.5 + 16), height: 20, labelpos: "c" } : {});
  }
  dagre.layout(g);

  const positions = new Map<string, Placed>();
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of nodes) {
    const p = g.node(n.id);
    const h = cardH(n.id);
    const x = p.x - CARD_W / 2, y = p.y - h / 2;
    positions.set(n.id, { id: n.id, x, y, h });
    minX = Math.min(minX, x); minY = Math.min(minY, y);
    maxX = Math.max(maxX, x + CARD_W); maxY = Math.max(maxY, y + h);
  }
  if (!nodes.length) return { positions, width: 0, height: 0 };
  for (const p of positions.values()) { p.x -= minX; p.y -= minY; }
  return { positions, width: maxX - minX, height: maxY - minY };
}
