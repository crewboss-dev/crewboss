// Pure integration logic for the shared canvas — no React, no DOM — node-tested (tests/canvas_model.test.mjs).
// The input is the ERP's integrations export (app/integrations_registry.py export(), committed by the ERP as
// docs/generated/integrations.json; docs/canvas-contract.md). Three jobs:
//
//   1. resolveStep: which integrations a catalog step uses — its own `integrations` ids when the annotation
//      narrows it, else every id its `system` maps to (export.systems). Weakest grade first.
//   2. groupSystems: one box per other system (C4: one relationship per pair of systems). The line takes the
//      WEAKEST grade among that system's links; its data flow is in, out, or both.
//   3. layoutIntegrations: one module's diagram, the module (hub) in the middle — what sends data TO us on the
//      left, what we send to on the right, two-way systems balancing the shorter side zone by zone; each side
//      grouped by zone under a heading. The same geometry the ERP's admin page draws (integrations_registry
//      diagram()), so both sites read alike.
//
// Only erasable TypeScript here (type annotations/aliases, no enums/namespaces/parameter properties).

export type Grade = "A" | "B" | "C" | "D";

export type Integration = {
  id: string;
  name: string;
  module: string;
  purpose: string;
  us?: string;
  peer: string;
  zone: string;
  flow: "in" | "out" | "both" | string;
  starts?: string;
  kind: string;
  label?: string;
  endpoint?: string;
  resolves?: string;
  path?: string;
  auth: string;
  transport: string;
  keys?: string[];
  notes?: string;
  status?: string;
  accepted?: string;
  grade: Grade | string;
  why?: string[];
};

export type Vocab = Partial<Record<"kinds" | "flows" | "starts" | "zones" | "resolves" | "paths" | "auths" | "transports",
  Record<string, string>>>;

export type IntegrationsExport = {
  version?: number;
  vocab?: Vocab;
  integrations?: Integration[];
  systems?: Record<string, string[]>;
  modules?: Record<string, string>;   // module key → display name (optional; the hub falls back to the key)
  summary?: Record<string, number>;
};

export const GRADES: Grade[] = ["A", "B", "C", "D"];
const ZONE_ORDER = ["server", "estate", "cloud", "people"];

/** "D" sorts first: the weakest link leads every list. Unknown grades sort last. */
export function gradeRank(g: string): number {
  const i = GRADES.indexOf(g as Grade);
  return i < 0 ? -1 : i;
}

export function weakest(grades: string[]): string {
  return grades.reduce((w, g) => (gradeRank(g) > gradeRank(w) ? g : w), grades[0] || "");
}

export const byWeakest = (a: Integration, b: Integration) =>
  gradeRank(b.grade) - gradeRank(a.grade) || a.name.toLowerCase().localeCompare(b.name.toLowerCase());

/** The vocab's plain label for a code (e.g. auths.entra_oauth → "Microsoft Entra (OAuth)"), else the code. */
export function vocabLabel(exp: IntegrationsExport | null | undefined, group: keyof Vocab, code?: string): string {
  if (!code) return "";
  return (exp && exp.vocab && exp.vocab[group] && exp.vocab[group]![code]) || code;
}

export function indexById(exp: IntegrationsExport | null | undefined): Map<string, Integration> {
  return new Map(((exp && exp.integrations) || []).filter((i) => i && i.id).map((i) => [i.id, i]));
}

/** The integration ids a step names or its system maps to (deduplicated, in the order given). */
export function stepIntegrationIds(step: { system?: string; integrations?: unknown },
                                   exp: IntegrationsExport | null | undefined): string[] {
  const own = Array.isArray(step.integrations) ? step.integrations.filter((x): x is string => typeof x === "string") : null;
  const ids = own && own.length ? own : ((exp && exp.systems && step.system && exp.systems[step.system]) || []);
  return [...new Set(ids)];
}

/** The integrations a step uses, weakest first; ids the export doesn't know are dropped (the scanner's check
 * reports those). */
export function resolveStep(step: { system?: string; integrations?: unknown },
                            exp: IntegrationsExport | null | undefined): Integration[] {
  const idx = indexById(exp);
  return stepIntegrationIds(step, exp).map((id) => idx.get(id)).filter((i): i is Integration => !!i).sort(byWeakest);
}

export function modulesOf(exp: IntegrationsExport | null | undefined): { module: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const i of (exp && exp.integrations) || []) counts.set(i.module, (counts.get(i.module) || 0) + 1);
  return [...counts].map(([module, count]) => ({ module, count })).sort((a, b) => b.count - a.count || a.module.localeCompare(b.module));
}

export function summaryOf(rows: Integration[]): Record<Grade, number> {
  const out = { A: 0, B: 0, C: 0, D: 0 } as Record<Grade, number>;
  for (const r of rows) if (r.grade in out) out[r.grade as Grade] += 1;
  return out;
}

export type SystemGroup = {
  peer: string;
  zone: string;
  flow: "in" | "out" | "both";
  grade: string;
  links: Integration[]; // weakest first
  live: boolean;
  label: string;
  sub: string;
};

export function groupSystems(rows: Integration[], exp?: IntegrationsExport | null): SystemGroup[] {
  const by = new Map<string, Integration[]>();
  for (const r of rows) by.set(r.peer, [...(by.get(r.peer) || []), r]);
  const out: SystemGroup[] = [];
  for (const [peer, links] of by) {
    const flows = new Set(links.map((r) => r.flow));
    const flow = (flows.size === 1 ? [...flows][0] : "both") as SystemGroup["flow"];
    const labels = [...new Set(links.map((r) => r.label || r.name))];
    const one = links.length === 1;
    out.push({
      peer, zone: links[0].zone, flow: flow === "in" || flow === "out" ? flow : "both",
      grade: weakest(links.map((r) => r.grade)), links: [...links].sort(byWeakest),
      live: links.some((r) => (r.status || "live") === "live"),
      label: labels.length === 1 ? labels[0] : `${links.length} links: ${labels.join(", ")}`,
      sub: one ? [links[0].us, vocabLabel(exp, "kinds", links[0].kind)].filter(Boolean).join(" · ") : `${links.length} links`,
    });
  }
  return out;
}

// geometry (px) — matches integrations_registry.py so the two sites draw the same picture
export const NODE_W = 210, NODE_H = 40, ROW_GAP = 10, HEAD_H = 24, LINE_W = 250, HUB_W = 160, PAD = 8;

export type IntegrationLayout = {
  hub: { x: number; y: number; w: number; h: number };
  systems: { group: SystemGroup; side: "left" | "right"; x: number; y: number }[];
  heads: { id: string; text: string; side: "left" | "right"; x: number; y: number }[];
  width: number;
  height: number;
};

function column(gs: SystemGroup[], side: "left" | "right", x: number, exp?: IntegrationsExport | null) {
  const systems: IntegrationLayout["systems"] = [];
  const heads: IntegrationLayout["heads"] = [];
  let y = PAD;
  const zones = [...ZONE_ORDER, ...new Set(gs.map((g) => g.zone).filter((z) => !ZONE_ORDER.includes(z)))];
  for (const zone of zones) {
    const group = gs.filter((g) => g.zone === zone).sort((a, b) => a.peer.toLowerCase().localeCompare(b.peer.toLowerCase()));
    if (!group.length) continue;
    heads.push({ id: `head:${side}:${zone}`, text: vocabLabel(exp, "zones", zone), side, x, y });
    y += HEAD_H;
    for (const g of group) {
      systems.push({ group: g, side, x, y });
      y += NODE_H + ROW_GAP;
    }
  }
  return { systems, heads, height: y };
}

export function layoutIntegrations(rows: Integration[], exp?: IntegrationsExport | null): IntegrationLayout {
  const gs = groupSystems(rows, exp);
  const lefts = gs.filter((g) => g.flow === "in");
  const rights = gs.filter((g) => g.flow === "out");
  const zones = [...ZONE_ORDER, ...new Set(gs.map((g) => g.zone).filter((z) => !ZONE_ORDER.includes(z)))];
  for (const zone of zones) {
    const both = gs.filter((g) => g.flow === "both" && g.zone === zone)
      .sort((a, b) => a.peer.toLowerCase().localeCompare(b.peer.toLowerCase()));
    for (const g of both) (lefts.length < rights.length ? lefts : rights).push(g);
  }
  const hubX = NODE_W + LINE_W;
  const rightX = hubX + HUB_W + LINE_W;
  const left = column(lefts, "left", 0, exp);
  const right = column(rights, "right", rightX, exp);
  const height = Math.max(left.height, right.height, NODE_H + 2 * PAD) + PAD;
  return {
    hub: { x: hubX, y: PAD, w: HUB_W, h: height - 2 * PAD },
    systems: [...left.systems, ...right.systems],
    heads: [...left.heads, ...right.heads],
    width: rightX + NODE_W,
    height,
  };
}
