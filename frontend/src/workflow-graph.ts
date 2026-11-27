// Pure graph logic for the workflow canvas — no React, no dagre, no DOM — so it can be unit-tested
// with plain `node --test` (tests/workflow_graph.test.mjs; Node strips the type annotations).
//
// The catalog (docs/catalog-schema.md) OMITS `edges` for a straight run, and the hand-annotated
// workflows that do carry edges usually only describe the part that branches. So:
//
//   1. Explicit edges are drawn exactly as given (labels kept).
//   2. A trigger node is synthesised from `workflow.trigger` ONLY when no step has kind "trigger".
//      It links to the first step.
//   3. Every step that still has no incoming edge (and isn't a trigger, and isn't the first step) is
//      linked from the step before it in array order. For a straight run that is the whole chain; for
//      an annotated workflow it joins the unannotated lead-in / tail (e.g. Timer → Load token, Save →
//      Heartbeat) onto the explicit part instead of leaving those cards floating.
//
// Only erasable TypeScript here (type annotations/aliases, no enums/namespaces/parameter properties).

export type StepKind = "trigger" | "precode" | "ai" | "logic" | "action" | "decision" | "loop";
export type RunStatus = "done" | "failed" | "not-run";

export type CatalogStep = {
  id: string;
  kind: StepKind | string;
  label: string;
  detail?: string;
  system?: string;
  question?: string; // kind=decision: the condition, e.g. "Is it a PDF?"
  parent?: string; // scope nesting: the decision/loop step this step sits inside
  branch?: string; // …and, under a decision, which labelled branch
};

export type CatalogEdge = { from: string; to: string; label?: string };

export type CatalogWorkflow = {
  trigger?: { type?: string; detail?: string; system?: string } | null;
  steps?: CatalogStep[] | null;
  edges?: CatalogEdge[] | null;
};

export type GraphNode = {
  id: string;
  kind: string;
  label: string;
  detail: string;
  system: string;
  synthetic: boolean;
  question?: string;
  parent?: string;
  branch?: string;
  status?: RunStatus;
};

export type GraphEdge = {
  id: string;
  from: string;
  to: string;
  label?: string;
  synthetic: boolean; // true = inferred from array order / the synthesised trigger
};

export const TRIGGER_ID = "__trigger__";

const TRIGGER_LABEL: Record<string, string> = {
  timer: "Timer",
  webhook: "Webhook",
  email: "Email arrives",
  button: "Click",
  event: "Event",
  api: "API call",
};

export function buildGraph(wf: CatalogWorkflow): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const steps = (wf.steps || []).filter((s) => s && typeof s.id === "string");
  const ids = new Set(steps.map((s) => s.id));
  const triggerDetail = (wf.trigger && wf.trigger.detail) || "";

  const nodes: GraphNode[] = steps.map((s) => ({
    id: s.id,
    kind: s.kind || "action",
    label: s.label || s.id,
    // A trigger step usually has no detail of its own; the workflow's trigger line says when it fires.
    detail: s.detail || (s.kind === "trigger" ? triggerDetail : ""),
    system: s.system || (s.kind === "trigger" && wf.trigger && wf.trigger.type) || "internal",
    synthetic: false,
    ...(s.question ? { question: s.question } : {}),
    ...(s.parent ? { parent: s.parent } : {}),
    ...(s.branch ? { branch: s.branch } : {}),
  }));

  const edges: GraphEdge[] = [];
  const seen = new Set<string>();
  const push = (from: string, to: string, synthetic: boolean, label?: string) => {
    const key = `${from}->${to}`;
    if (from === to || seen.has(key)) return;
    seen.add(key);
    edges.push({ id: `e${edges.length}-${from}-${to}`, from, to, label: label || undefined, synthetic });
  };

  // 1. explicit edges, as given — dropping only ones that name a step that doesn't exist
  for (const e of wf.edges || []) {
    if (e && ids.has(e.from) && ids.has(e.to)) push(e.from, e.to, false, e.label);
  }

  // 2. a synthetic trigger only when the steps don't already carry one
  const hasTriggerStep = steps.some((s) => s.kind === "trigger");
  if (!hasTriggerStep && wf.trigger && (wf.trigger.type || wf.trigger.detail)) {
    const type = wf.trigger.type || "event";
    nodes.unshift({
      id: TRIGGER_ID,
      kind: "trigger",
      label: TRIGGER_LABEL[type] || type.charAt(0).toUpperCase() + type.slice(1),
      detail: triggerDetail,
      system: wf.trigger.system || type,
      synthetic: true,
    });
    if (steps.length) push(TRIGGER_ID, steps[0].id, true);
  }

  // 3. sequential fill for every step nothing leads into yet
  const hasIncoming = new Set(edges.map((e) => e.to));
  for (let i = 1; i < steps.length; i++) {
    const s = steps[i];
    if (s.kind === "trigger" || hasIncoming.has(s.id)) continue;
    push(steps[i - 1].id, s.id, true);
    hasIncoming.add(s.id);
  }

  return { nodes, edges };
}

/** Run/failure view. With a `path` (the step ids the run actually went through, e.g. from
 * ?path=a,b,c) those are done — the failed step, if on it, failed — and everything else didn't run.
 * Without one, the path is derived: everything upstream of the failed step (its ancestors — what must
 * have run for the run to reach it) is done. Graph-aware either way, so an untaken branch greys out
 * even when it sits earlier in array order. Unknown ids are ignored; nothing known = no-op. */
export function applyRunStatus(nodes: GraphNode[], edges: GraphEdge[], failedStep?: string | null,
                               path?: string[] | null): GraphNode[] {
  const known = new Set(nodes.map((n) => n.id));
  const taken = (path || []).filter((id) => known.has(id));
  if (taken.length) {
    const on = new Set(taken);
    // the trigger always ran if anything did
    for (const n of nodes) if (n.kind === "trigger" && !edges.some((e) => e.to === n.id)) on.add(n.id);
    return nodes.map((n) => ({
      ...n,
      status: n.id === failedStep ? "failed" : on.has(n.id) ? "done" : "not-run",
    }));
  }
  if (!failedStep || !known.has(failedStep)) return nodes;
  const parents = new Map<string, string[]>();
  for (const e of edges) parents.set(e.to, [...(parents.get(e.to) || []), e.from]);
  const done = new Set<string>();
  const stack = [...(parents.get(failedStep) || [])];
  while (stack.length) {
    const id = stack.pop() as string;
    if (done.has(id) || id === failedStep) continue;
    done.add(id);
    stack.push(...(parents.get(id) || []));
  }
  return nodes.map((n) => ({
    ...n,
    status: n.id === failedStep ? "failed" : done.has(n.id) ? "done" : "not-run",
  }));
}
