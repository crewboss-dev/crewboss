// Coordinates for the structured (Power Automate-style) canvas — pure, node-tested
// (tests/workflow_structure.test.mjs). One centred vertical column; a condition is a SCOPE container with
// its header pill on top and one branch container per label side by side underneath, each with its own
// stack (or "0 Actions"); a loop is a header pill + a container around its body. Every block measures
// itself first, then is placed centred on its parent's axis — so nesting "just works" and the paths
// rejoin at the bottom-centre of each container.

import { stepIds, type Item } from "./workflow-structure.ts";

export const CARD_W = 220;
const GAP = 44; // between consecutive items — the connector carries the (+)
const PILL_H = 34;
const BR_PILL_H = 24;
const PAD = 14; // container padding
const HEAD_GAP = 30; // header pill → branch containers
const IN_GAP = 26; // branch pill → first card
const BRANCH_GAP = 18;
const EMPTY_W = 150, EMPTY_H = 30;
const COLLAPSED_H = 18;

export type LaidNode =
  | { kind: "card"; id: string; x: number; y: number; w: number; h: number }
  | { kind: "pill"; id: string; x: number; y: number; w: number; h: number; variant: "decision" | "loop";
      count: number; collapsed: boolean }
  | { kind: "branch"; id: string; x: number; y: number; w: number; h: number; label: string; steps: string[] }
  | { kind: "box"; id: string; x: number; y: number; w: number; h: number; variant: "scope" | "branch" | "loop";
      steps: string[] }
  | { kind: "empty"; id: string; x: number; y: number; w: number; h: number; steps: string[] }
  | { kind: "join"; id: string; x: number; y: number; w: number; h: number };

// seq = down a column (straight, arrow, may carry a (+)); fan = out of a header into its branches (elbow just
// under the source); merge = back into a rejoin point (elbow just above the target)
export type LaidEdge = { id: string; from: string; to: string; kind: "seq" | "fan" | "merge"; plusFor?: string };

export type FlowLayout = { nodes: LaidNode[]; edges: LaidEdge[]; width: number; height: number };

export type Sizing = {
  cardH: (id: string) => number;
  pillText: (id: string) => string;
  collapsed: Set<string>;
};

const textW = (s: string, perChar: number, min: number, max: number) =>
  Math.max(min, Math.min(max, Math.round(s.length * perChar)));
const pillW = (text: string) => textW(text, 8, 120, 340) + 80; // icon + chevron + padding
const branchPillW = (label: string) => textW(label || " ", 6.8, 36, 200) + 24;

type Measured = { w: number; h: number };

export function layoutFlow(items: Item[], sz: Sizing): FlowLayout {
  const nodes: LaidNode[] = [];
  const edges: LaidEdge[] = [];
  const edge = (from: string, to: string, kind: LaidEdge["kind"], plusFor?: string) =>
    edges.push({ id: `f${edges.length}:${from}>${to}`, from, to, kind, ...(plusFor ? { plusFor } : {}) });

  // --- measuring -----------------------------------------------------------------------------------
  const mSeq = (list: Item[]): Measured => {
    if (!list.length) return { w: 0, h: 0 };
    const ms = list.map(mItem);
    return { w: Math.max(...ms.map((m) => m.w)), h: ms.reduce((a, m) => a + m.h, 0) + GAP * (ms.length - 1) };
  };
  const mBranch = (label: string, list: Item[]): Measured => {
    const inner = list.length ? mSeq(list) : { w: EMPTY_W, h: EMPTY_H };
    return { w: Math.max(inner.w, branchPillW(label), 170) + 2 * PAD,
             h: PAD + (label ? BR_PILL_H + IN_GAP : 0) + inner.h + PAD };
  };
  const mItem = (it: Item): Measured => {
    if (it.type === "step") return { w: CARD_W, h: sz.cardH(it.id) };
    const collapsed = sz.collapsed.has(it.id);
    if (it.type === "loop") {
      const pw = pillW(sz.pillText(it.id));
      if (collapsed) return { w: pw + 2 * PAD, h: PAD + PILL_H + COLLAPSED_H + PAD };
      const body = it.body.length ? mSeq(it.body) : { w: EMPTY_W, h: EMPTY_H };
      return { w: Math.max(pw, body.w) + 2 * PAD, h: PAD + PILL_H + IN_GAP + body.h + PAD + 6 };
    }
    // scope
    const bs = it.branches.map((b) => mBranch(b.label, b.items));
    const row = { w: bs.reduce((a, b) => a + b.w, 0) + BRANCH_GAP * Math.max(0, bs.length - 1),
                  h: bs.length ? Math.max(...bs.map((b) => b.h)) : 0 };
    if (!it.decision) return row; // a plain fan-out: just the branch containers
    const pw = pillW(sz.pillText(it.id));
    if (collapsed || !bs.length) {
      return { w: pw + 2 * PAD, h: PAD + PILL_H + (collapsed ? COLLAPSED_H : 0) + PAD };
    }
    return { w: Math.max(pw, row.w) + 2 * PAD, h: PAD + PILL_H + HEAD_GAP + row.h + PAD + 14 };
  };

  // --- placing (returns where to connect in / out) ---------------------------------------------------
  type Ports = { entry: string[]; exits: string[]; card?: string };

  const pSeq = (list: Item[], cx: number, y: number): Ports => {
    let cursor = y;
    let prev: Ports | null = null;
    let first: Ports | null = null;
    for (const it of list) {
      const m = mItem(it);
      const ports = pItem(it, cx, cursor, m);
      if (prev) for (const f of prev.exits) for (const t of ports.entry) edge(f, t, "seq", prev.card);
      first = first || ports;
      prev = ports;
      cursor += m.h + GAP;
    }
    return { entry: first ? first.entry : [], exits: prev ? prev.exits : [] };
  };

  const pBranches = (scopeId: string, branches: { label: string; items: Item[] }[], cx: number, y: number,
                     rowH: number, from: string[], join: string): string[] => {
    const ms = branches.map((b) => mBranch(b.label, b.items));
    const total = ms.reduce((a, m) => a + m.w, 0) + BRANCH_GAP * Math.max(0, ms.length - 1);
    let x = cx - total / 2;
    const entries: string[] = [];
    branches.forEach((b, i) => {
      const m = ms[i];
      const bcx = x + m.w / 2;
      const steps = stepIds(b.items);
      nodes.push({ kind: "box", id: `brbox:${scopeId}:${i}`, x, y, w: m.w, h: rowH, variant: "branch", steps });
      let top = y + PAD;
      let inlet: string[];
      if (b.label) {
        const bw = branchPillW(b.label);
        const pid = `br:${scopeId}:${i}`;
        nodes.push({ kind: "branch", id: pid, x: bcx - bw / 2, y: top, w: bw, h: BR_PILL_H, label: b.label, steps });
        for (const f of from) edge(f, pid, "fan");
        entries.push(pid);
        inlet = [pid];
        top += BR_PILL_H + IN_GAP;
      } else {
        inlet = from;
      }
      // each branch drops straight to the bottom of its container, then all of them turn in to the rejoin
      // at the same height — PA's tidy "comb" rather than a staircase of elbows
      const out = `bout:${scopeId}:${i}`;
      nodes.push({ kind: "join", id: out, x: bcx, y: y + rowH, w: 1, h: 1 });
      if (b.items.length) {
        const ports = pSeq(b.items, bcx, top);
        for (const f of inlet) for (const t of ports.entry) edge(f, t, b.label ? "seq" : "fan");
        if (!b.label) entries.push(...ports.entry);
        for (const e of ports.exits) edge(e, out, "merge");
      } else {
        const eid = `empty:${scopeId}:${i}`;
        nodes.push({ kind: "empty", id: eid, x: bcx - EMPTY_W / 2, y: top, w: EMPTY_W, h: EMPTY_H, steps });
        for (const f of inlet) edge(f, eid, b.label ? "seq" : "fan");
        if (!b.label) entries.push(eid);
        edge(eid, out, "merge");
      }
      edge(out, join, "merge");
      x += m.w + BRANCH_GAP;
    });
    return entries;
  };

  const pItem = (it: Item, cx: number, y: number, m: Measured): Ports => {
    if (it.type === "step") {
      nodes.push({ kind: "card", id: it.id, x: cx - CARD_W / 2, y, w: CARD_W, h: m.h });
      return { entry: [it.id], exits: [it.id], card: it.id };
    }
    const collapsed = sz.collapsed.has(it.id);
    const join = `join:${it.id}`;
    if (it.type === "scope" && !it.decision) {
      // plain fan-out: branch containers hang straight off the previous card (connected by pSeq)
      const from = [`fanin:${it.id}`];
      nodes.push({ kind: "join", id: from[0], x: cx, y: y - GAP / 2, w: 1, h: 1 });
      nodes.push({ kind: "join", id: join, x: cx, y: y + m.h + 6, w: 1, h: 1 });
      pBranches(it.id, it.branches, cx, y, m.h, from, join);
      return { entry: from, exits: [join] };
    }
    const pw = pillW(sz.pillText(it.id));
    const variant = it.type === "loop" ? "loop" : "decision";
    const count = it.type === "loop" ? stepIds(it.body).length
      : it.branches.reduce((a, b) => a + stepIds(b.items).length, 0);
    nodes.push({ kind: "box", id: `box:${it.id}`, x: cx - m.w / 2, y, w: m.w, h: m.h,
                 variant: it.type === "loop" ? "loop" : "scope",
                 steps: it.type === "loop" ? [it.id, ...stepIds(it.body)] : stepIds([it]) });
    nodes.push({ kind: "pill", id: it.id, x: cx - pw / 2, y: y + PAD, w: pw, h: PILL_H, variant, count, collapsed });
    nodes.push({ kind: "join", id: join, x: cx, y: y + m.h, w: 1, h: 1 });
    if (collapsed || (it.type === "scope" && !it.branches.length)) {
      edge(it.id, join, "seq");
      return { entry: [it.id], exits: [join] };
    }
    const top = y + PAD + PILL_H + (it.type === "loop" ? IN_GAP : HEAD_GAP);
    if (it.type === "loop") {
      if (it.body.length) {
        const ports = pSeq(it.body, cx, top);
        for (const t of ports.entry) edge(it.id, t, "seq");
        for (const e of ports.exits) edge(e, join, "merge");
      } else {
        const eid = `empty:${it.id}`;
        nodes.push({ kind: "empty", id: eid, x: cx - EMPTY_W / 2, y: top, w: EMPTY_W, h: EMPTY_H, steps: [] });
        edge(it.id, eid, "seq");
        edge(eid, join, "merge");
      }
      return { entry: [it.id], exits: [join] };
    }
    const rowH = Math.max(...it.branches.map((b) => mBranch(b.label, b.items).h));
    pBranches(it.id, it.branches, cx, top, rowH, [it.id], join);
    return { entry: [it.id], exits: [join] };
  };

  const total = mSeq(items);
  pSeq(items, total.w / 2, 0);
  return { nodes, edges, width: total.w, height: total.h };
}
