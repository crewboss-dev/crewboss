// Structured (Power Automate-style) view of a workflow graph — pure, node-tested
// (tests/workflow_structure.test.mjs). Turns the step graph into nested blocks:
//
//   Item = a step card
//        | a SCOPE: a condition (kind=decision, drawn as a header pill with the question) — or a plain
//          fan-out after an ordinary step — with one BRANCH container per outgoing label, side by side,
//          each holding its own vertical stack; the paths rejoin below the scope
//        | a LOOP (kind=loop, "For each"): a header pill + a container holding its body
//
// Two ways in:
//   1. NESTED: steps carry `parent` (the decision/loop they sit in) and, under a decision, `branch`
//      (its label). Branch labels come from the decision's labelled edges first (so an empty branch
//      still shows as "0 Actions"), then from the children.
//   2. FROM EDGES: no `parent` anywhere — walk the (buildGraph) edges from the single root; a step with
//      several successors opens a scope whose branches run until the point where they all rejoin (the
//      first step every branch reaches), or to their ends.
// Anything that won't nest cleanly (several roots, a merge that isn't a rejoin, a cycle) returns null
// and the canvas falls back to the plain layered layout.
//
// `flow` = the edges between step ids implied by the structure, used for the run view's ancestry.

import type { GraphEdge, GraphNode } from "./workflow-graph.ts";

export type Branch = { label: string; items: Item[] };
export type Item =
  | { type: "step"; id: string }
  | { type: "scope"; id: string; decision: boolean; branches: Branch[] }
  | { type: "loop"; id: string; body: Item[] };
export type FlowEdge = { from: string; to: string };
export type Structure = { items: Item[]; flow: FlowEdge[] };

class NotStructured extends Error {}

export function buildStructure(nodes: GraphNode[], edges: GraphEdge[]): Structure | null {
  if (!nodes.length) return { items: [], flow: [] };
  try {
    const items = nodes.some((n) => n.parent) ? fromParents(nodes, edges) : fromEdges(nodes, edges);
    return { items, flow: flowOf(items) };
  } catch (e) {
    if (e instanceof NotStructured) return null;
    throw e;
  }
}

// --- 1. nested (parent / branch) ------------------------------------------------------------------------

function fromParents(nodes: GraphNode[], edges: GraphEdge[]): Item[] {
  const ids = new Set(nodes.map((n) => n.id));
  const kids = (pid: string | undefined) =>
    nodes.filter((n) => (pid === undefined ? !n.parent || !ids.has(n.parent) : n.parent === pid));
  const seen = new Set<string>();
  const seq = (list: GraphNode[]): Item[] => list.map((n) => {
    if (seen.has(n.id)) throw new NotStructured();
    seen.add(n.id);
    if (n.kind === "loop") return { type: "loop", id: n.id, body: seq(kids(n.id)) };
    if (n.kind === "decision") {
      const children = kids(n.id);
      const labels: string[] = [];
      for (const e of edges) if (e.from === n.id && e.label && !labels.includes(e.label)) labels.push(e.label);
      for (const c of children) {
        const l = c.branch || "";
        if (!labels.includes(l)) labels.push(l);
      }
      return {
        type: "scope", id: n.id, decision: true,
        branches: labels.map((label) => ({ label, items: seq(children.filter((c) => (c.branch || "") === label)) })),
      };
    }
    return { type: "step", id: n.id };
  });
  return seq(kids(undefined));
}

// --- 2. from edges --------------------------------------------------------------------------------------

function fromEdges(nodes: GraphNode[], edges: GraphEdge[]): Item[] {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const succ = new Map<string, GraphEdge[]>();
  const indeg = new Map<string, number>(nodes.map((n) => [n.id, 0]));
  for (const e of edges) {
    succ.set(e.from, [...(succ.get(e.from) || []), e]);
    indeg.set(e.to, (indeg.get(e.to) || 0) + 1);
  }
  const roots = nodes.filter((n) => !indeg.get(n.id));
  if (roots.length !== 1) throw new NotStructured();

  // topological order (a cycle can't nest)
  const topo: string[] = [];
  const deg = new Map(indeg);
  const queue = [roots[0].id];
  while (queue.length) {
    const id = queue.shift() as string;
    topo.push(id);
    for (const e of succ.get(id) || []) {
      deg.set(e.to, (deg.get(e.to) || 0) - 1);
      if (!deg.get(e.to)) queue.push(e.to);
    }
  }
  if (topo.length !== nodes.length) throw new NotStructured();
  const rank = new Map(topo.map((id, i) => [id, i]));

  const reach = new Map<string, Set<string>>();
  const reachable = (id: string): Set<string> => {
    const hit = reach.get(id);
    if (hit) return hit;
    const out = new Set<string>([id]);
    for (const e of succ.get(id) || []) for (const r of reachable(e.to)) out.add(r);
    reach.set(id, out);
    return out;
  };
  const rejoin = (outs: GraphEdge[]): string | null => {
    let common: string[] | null = null;
    for (const e of outs) {
      const r = reachable(e.to);
      common = common === null ? [...r] : common.filter((x: string) => r.has(x));
    }
    const list = (common || []).slice().sort((a, b) => (rank.get(a) as number) - (rank.get(b) as number));
    return list[0] ?? null;
  };

  const seen = new Set<string>();
  const seq = (start: string | null, stop: string | null): Item[] => {
    const items: Item[] = [];
    let cur = start;
    while (cur && cur !== stop) {
      if (seen.has(cur)) throw new NotStructured(); // a merge that isn't this scope's rejoin
      seen.add(cur);
      const node = byId.get(cur) as GraphNode;
      const outs = succ.get(cur) || [];
      if (node.kind === "loop") {
        items.push({ type: "loop", id: cur, body: [] });
        if (outs.length > 1) throw new NotStructured();
        cur = outs[0]?.to ?? null;
        continue;
      }
      if (outs.length <= 1 && node.kind !== "decision") {
        items.push({ type: "step", id: cur });
        cur = outs[0]?.to ?? null;
        continue;
      }
      const join = outs.length > 1 ? rejoin(outs) : (outs[0]?.to ?? null);
      if (join !== null && stop !== null && !reachable(join).has(stop) && join !== stop) throw new NotStructured();
      // a decision with a single way out is just its header pill, then the flow carries on
      const branches: Branch[] = outs.length > 1
        ? outs.map((e, i) => ({ label: e.label || (node.kind === "decision" ? `Branch ${i + 1}` : ""), items: seq(e.to, join) }))
        : [];
      if (node.kind === "decision") {
        items.push({ type: "scope", id: cur, decision: true, branches });
      } else {
        items.push({ type: "step", id: cur });
        items.push({ type: "scope", id: `${cur}__fanout`, decision: false, branches });
      }
      cur = join;
    }
    return items;
  };
  const items = seq(roots[0].id, null);
  if (seen.size !== nodes.length) throw new NotStructured();
  return items;
}

// --- the flow implied by the structure ----------------------------------------------------------------

const entries = (item: Item): string[] =>
  item.type === "scope" && !item.decision
    ? item.branches.flatMap((b) => (b.items.length ? entries(b.items[0]) : []))
    : [item.id];

function exitsOf(items: Item[]): string[] {
  if (!items.length) return [];
  const last = items[items.length - 1];
  if (last.type === "scope") {
    const out = last.branches.flatMap((b) => (b.items.length ? exitsOf(b.items) : []));
    const anyEmpty = last.branches.some((b) => !b.items.length) || !last.branches.length;
    if (anyEmpty) out.push(...(last.decision ? [last.id] : exitsOf(items.slice(0, -1))));
    return out;
  }
  return [last.id];
}

function flowOf(items: Item[]): FlowEdge[] {
  const flow: FlowEdge[] = [];
  const walk = (list: Item[]) => {
    list.forEach((item, i) => {
      if (i > 0) {
        const from = exitsOf(list.slice(0, i));
        for (const f of from) for (const t of entries(item)) flow.push({ from: f, to: t });
      }
      if (item.type === "scope") {
        for (const b of item.branches) {
          if (item.decision && b.items.length) for (const t of entries(b.items[0])) flow.push({ from: item.id, to: t });
          walk(b.items);
        }
      } else if (item.type === "loop") {
        if (item.body.length) for (const t of entries(item.body[0])) flow.push({ from: item.id, to: t });
        walk(item.body);
      }
    });
  };
  walk(items);
  return flow.filter((e, i) => flow.findIndex((x) => x.from === e.from && x.to === e.to) === i);
}

/** Every step id inside an item (for "N Actions" and branch dimming). */
export function stepIds(items: Item[]): string[] {
  return items.flatMap((it) =>
    it.type === "step" ? [it.id]
      : it.type === "loop" ? [it.id, ...stepIds(it.body)]
        : [...(it.decision ? [it.id] : []), ...it.branches.flatMap((b) => stepIds(b.items))]);
}
