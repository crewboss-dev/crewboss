// kind=workflow — the modern Power Automate designer look on an infinite dotted desktop: pan, zoom, minimap,
// fit, drag cards (nudges kept per browser), conditions as SCOPE containers (dark header pill + one branch
// container per label, side by side), loops as containers ("For each"); both collapse to a summary pill with
// an action count. A run view (data.run) shows the taken path and dims the rest. Clicking a step opens the
// side panel: its details, the AI node, the integrations it uses (graded), and any host content.
//
// Pure parts are node-tested: workflow-graph.ts (links + trigger), workflow-structure.ts (nesting),
// workflow-flow-layout.ts (coordinates), canvas/integrations-model.ts (step → integrations). workflow-layout.ts
// (dagre) is the fallback for graphs that don't nest.
import {
  applyNodeChanges,
  Background,
  BackgroundVariant,
  BaseEdge,
  ControlButton,
  Controls,
  EdgeLabelRenderer,
  getSmoothStepPath,
  getStraightPath,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeChange,
  type NodeProps,
  type ReactFlowInstance,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { applyRunStatus, buildGraph, type CatalogWorkflow, type GraphNode } from "../workflow-graph.ts";
import { CARD_W, layoutFlow, type LaidEdge, type LaidNode } from "../workflow-flow-layout.ts";
import { layoutGraph } from "../workflow-layout.ts";
import { buildStructure } from "../workflow-structure.ts";
import { Icon } from "./icons.tsx";
import { resolveStep, type IntegrationsExport } from "./integrations-model.ts";
import { kindLabel, StepPanel, type StepInfo } from "./panel.tsx";
import { plural, Shell, useOffsets, type CanvasOptions } from "./shared.tsx";

export type WorkflowCanvasData = {
  workflow: CatalogWorkflow & { id?: string; name?: string; side_effects?: string[]; steps?: (StepInfo & Record<string, unknown>)[] | null };
  integrations?: IntegrationsExport | null;
  /** a run to show: the failed step, its error, and/or the path the run took */
  run?: { failedStep?: string | null; error?: string | null; path?: string[] | null } | null;
  /** Crewboss extras (optional; the admin site passes none) */
  extras?: {
    base?: string;
    tasks?: boolean; // show the open-task badges and the (+) "new task on this step" connectors
    openTasks?: Record<string, number>;
    ruleCounts?: Record<string, { rules: number; instructions: number }>;
    skills?: Record<string, { id: string; name: string }>;
  } | null;
};

function fire(name: string, id: string | null) {
  window.dispatchEvent(new CustomEvent(name, { detail: { id } }));
}
const stop = (e: { stopPropagation: () => void }) => e.stopPropagation();

const HIDDEN_HANDLES = (
  <>
    <Handle type="target" position={Position.Top} className="mc-handle" isConnectable={false} />
    <Handle type="source" position={Position.Bottom} className="mc-handle" isConnectable={false} />
  </>
);

// --- nodes ----------------------------------------------------------------------------------------------

type CardData = {
  node: GraphNode; open: number; meta: string; metaHref?: string; error?: string; dim: boolean; triggerType?: string;
  tasks: boolean; grade?: string;
};
const KINDS = new Set(["trigger", "precode", "ai", "logic", "action", "decision", "loop"]);

function CardNode({ data, selected }: NodeProps) {
  const { node, open, meta, metaHref, error, dim, triggerType, tasks, grade } = data as unknown as CardData;
  const kind = KINDS.has(node.kind) ? node.kind : "action";
  const status = node.status || "";
  return (
    <div
      className={`mc-wcard mc-kind-${kind} ${status ? `mc-status-${status}` : ""} ${dim ? "mc-dim" : ""} ${selected ? "mc-selected" : ""}`}
      role="group"
      tabIndex={0}
      aria-label={`${kindLabel(kind)}: ${node.label}${status ? ` (${status})` : ""}. Enter shows its detail.`}
      title={node.label}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fire("wf:node-click", node.id); } }}
    >
      {HIDDEN_HANDLES}
      <span className="mc-wcard-bar" aria-hidden />
      <span className="mc-wcard-icon" title={`${kindLabel(kind)} · ${node.system}`}>
        <Icon name={kind === "ai" ? "claude" : kind === "trigger" && triggerType && node.system === "internal" ? triggerType : node.system} />
      </span>
      <div className="mc-wcard-body">
        <div className="mc-wcard-title">{node.label}</div>
        {meta && (metaHref
          ? <a className="nodrag nopan mc-wcard-meta mc-link" href={metaHref} onClick={stop}>{meta}</a>
          : <div className="mc-wcard-meta">{meta}</div>)}
        {status === "failed" && error && (
          <div className="mc-wcard-error" role="alert">
            <svg viewBox="0 0 24 24" className="mc-ico" fill="currentColor" aria-hidden><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm-1 5h2v7h-2Zm0 9h2v2h-2Z" /></svg>
            <span className="mc-clamp2">{error}</span>
          </div>
        )}
      </div>
      {grade && <span className={`mc-wcard-grade mc-grade mc-grade-${grade}`} title={`Integration grade ${grade} — details in the panel`}>{grade}</span>}
      {status === "done" && <span className="mc-wcard-ok" title="Ran" aria-label="ran">✓</span>}
      {tasks && open > 0 && (
        <button type="button" className="nodrag nopan mc-task-badge"
                title={`${plural(open, "open task")} on this step`} aria-label={`${plural(open, "open task")} on this step — show them`}
                onClick={(e) => { e.stopPropagation(); fire("wf:step-tasks", node.id); }}>
          <Icon name="tasks" className="mc-ico-12" />
          <span className="mc-tabular">{open}</span>
        </button>
      )}
    </div>
  );
}

type PillData = { node: GraphNode; variant: "decision" | "loop"; count: number; collapsed: boolean; dim: boolean;
                  onToggle: (id: string) => void; open: number; tasks: boolean };

function PillNode({ data, selected }: NodeProps) {
  const { node, variant, count, collapsed, dim, onToggle, open, tasks } = data as unknown as PillData;
  const text = variant === "decision" ? (node.question || node.label) : node.label;
  return (
    <div className={`mc-pill mc-pill-${variant} ${node.status ? `mc-status-${node.status}` : ""} ${dim ? "mc-dim" : ""} ${selected ? "mc-selected" : ""}`}
         role="group" tabIndex={0} title={text}
         aria-label={`${variant === "decision" ? "Condition" : "Loop"}: ${text}${collapsed ? `, collapsed, ${plural(count, "action")}` : ""}`}
         onKeyDown={(e) => {
           if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fire("wf:node-click", node.id); }
           if (e.key === "ArrowLeft" && !collapsed) onToggle(node.id);
           if (e.key === "ArrowRight" && collapsed) onToggle(node.id);
         }}>
      {HIDDEN_HANDLES}
      <Icon name={variant === "decision" ? "condition" : "loop"} className="mc-ico-16" />
      <span className="mc-pill-text">{text}</span>
      {collapsed && <span className="mc-pill-count mc-tabular">{plural(count, "Action")}</span>}
      {tasks && open > 0 && (
        <button type="button" className="nodrag nopan mc-task-badge mc-task-badge-pill" aria-label={`${plural(open, "open task")} — show them`}
                onClick={(e) => { e.stopPropagation(); fire("wf:step-tasks", node.id); }}><span className="mc-tabular">{open}</span></button>
      )}
      <button type="button" className="nodrag nopan mc-pill-chevron" aria-expanded={!collapsed}
              aria-label={collapsed ? "Expand" : "Collapse"} onClick={(e) => { e.stopPropagation(); onToggle(node.id); }}>
        <svg viewBox="0 0 24 24" className={`mc-ico mc-chev ${collapsed ? "mc-chev-shut" : ""}`} fill="none"
             stroke="currentColor" strokeWidth={2.4} strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M6 9l6 6 6-6" /></svg>
      </button>
    </div>
  );
}

const TRUE_RE = /^(true|yes|y|ok|pass|qualified|approve[d]?)$/i;
const FALSE_RE = /^(false|no|n|fail|reject(ed)?|on error)$/i;

function BranchNode({ data }: NodeProps) {
  const { label, dim } = data as { label: string; dim: boolean };
  const tone = TRUE_RE.test(label) ? "true" : FALSE_RE.test(label) ? "false" : "other";
  return <div className={`mc-branch mc-branch-${tone} ${dim ? "mc-dim" : ""}`} title={`Branch: ${label}`}>{HIDDEN_HANDLES}{label}</div>;
}

function BoxNode({ data }: NodeProps) {
  const { variant, dim } = data as { variant: string; dim: boolean };
  return <div className={`mc-box mc-box-${variant} ${dim ? "mc-dim-box" : ""}`} aria-hidden />;
}

function EmptyNode({ data }: NodeProps) {
  const { dim } = data as { dim: boolean };
  return <div className={`mc-empty ${dim ? "mc-dim" : ""}`}>{HIDDEN_HANDLES}0 Actions</div>;
}

function JoinNode() {
  return <div className="mc-join">{HIDDEN_HANDLES}</div>;
}

const nodeTypes = { card: CardNode, pill: PillNode, branch: BranchNode, box: BoxNode, empty: EmptyNode, join: JoinNode };

// --- the edge: animated line, (+) connector between cards, label pill on fallback branches -------------

/** Down, across at `cy`, down — with rounded corners. Used for fan-outs and merges so every branch turns at
 * the same height (React Flow's smoothstep enforces a minimum offset that breaks short drops). */
function elbowPath(sx: number, sy: number, tx: number, ty: number, cy: number): [string, number, number] {
  const dir = Math.sign(tx - sx);
  const r = Math.max(0, Math.min(8, Math.abs(tx - sx) / 2, Math.abs(cy - sy), Math.abs(ty - cy)));
  return [`M ${sx} ${sy} L ${sx} ${cy - r} Q ${sx} ${cy} ${sx + dir * r} ${cy} L ${tx - dir * r} ${cy} `
    + `Q ${tx} ${cy} ${tx} ${cy + r} L ${tx} ${ty}`, (sx + tx) / 2, cy];
}

function FlowEdge({ id, sourceX, sourceY, targetX, targetY, data, markerEnd }: EdgeProps) {
  const d = (data || {}) as { plusFor?: string; label?: string; dim?: boolean; taken?: boolean; kind?: string };
  const straight = Math.abs(sourceX - targetX) < 1;
  // fan-outs turn just under their header, merges just above the rejoin: a clean comb, not a staircase
  const [path, lx, ly] = straight
    ? getStraightPath({ sourceX, sourceY, targetX, targetY })
    : d.kind === "merge" && targetY > sourceY ? elbowPath(sourceX, sourceY, targetX, targetY, targetY - Math.min(9, (targetY - sourceY) / 2))
      : d.kind === "fan" && targetY > sourceY ? elbowPath(sourceX, sourceY, targetX, targetY, sourceY + Math.min(12, (targetY - sourceY) / 2))
        : getSmoothStepPath({ sourceX, sourceY, targetX, targetY, sourcePosition: Position.Bottom,
                              targetPosition: Position.Top, borderRadius: 8 });
  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd}
                className={`mc-edge ${d.dim ? "mc-edge-dim" : ""} ${d.taken ? "mc-edge-taken" : ""} ${d.label === "on error" ? "mc-edge-error" : ""}`} />
      {(d.plusFor || d.label) && (
        <EdgeLabelRenderer>
          <div className="nodrag nopan mc-edge-label" ref={(el) => { if (el) el.style.transform = `translate(-50%, -50%) translate(${lx}px, ${ly}px)`; }}>
            {d.label && <span className={`mc-edge-pill ${d.dim ? "mc-dim" : ""}`}>{d.label}</span>}
            {d.plusFor && (
              <button type="button" className="mc-plus" title="New task on the step above"
                      aria-label="New task on the step above" onClick={() => fire("wf:step-new-task", d.plusFor as string)}>
                <Icon name="plus" className="mc-ico-12" />
              </button>
            )}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

const edgeTypes = { flow: FlowEdge };

// --- the canvas -----------------------------------------------------------------------------------------

const PAD = 32;
const NONE = Object.freeze({}) as Record<string, never>;
const coarse = () => window.matchMedia("(pointer: coarse)").matches;

export function WorkflowCanvas({ data, opts }: { data: WorkflowCanvasData; opts: CanvasOptions }) {
  // stable fallbacks: a fresh {} per render would re-run the whole layout (and re-measure every node) each frame
  const wf = data.workflow || NONE;
  const run = data.run || NONE;
  const extras = data.extras || NONE;
  const exp = data.integrations || null;
  const tasks = !!extras.tasks;
  const [selectedId, setSelectedId] = useState<string | null>(opts.open || null);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const wrapRef = useRef<HTMLDivElement>(null);
  const rfRef = useRef<ReactFlowInstance | null>(null);
  const [height, setHeight] = useState<number>(480);
  const { offsets, nudge, reset, moved } = useOffsets(`mallen-canvas:wf:${wf.id || "?"}`);
  const toggle = useCallback((id: string) => setCollapsed((c) => {
    const n = new Set(c);
    if (n.has(id)) n.delete(id); else n.add(id);
    return n;
  }), []);

  // graph -> structure (or the dagre fallback) -> run statuses
  const model = useMemo(() => {
    const g = buildGraph(wf);
    const structure = buildStructure(g.nodes, g.edges);
    const flow = structure ? structure.flow.map((e, i) => ({ id: `s${i}`, ...e, synthetic: true })) : g.edges;
    const nodes = applyRunStatus(g.nodes, flow, run.failedStep, run.path);
    return { graph: g, structure, byId: new Map(nodes.map((n) => [n.id, n])), runView: nodes.some((n) => n.status) };
  }, [wf, run.failedStep, run.path]);

  const steps = useMemo(() => new Map((wf.steps || []).filter((s) => s && s.id).map((s) => [s.id, s])), [wf]);
  const integrationsOf = useCallback((id: string) => {
    const s = steps.get(id);
    return s ? resolveStep(s as { system?: string; integrations?: unknown }, exp) : [];
  }, [steps, exp]);

  const metaOf = useCallback((n: GraphNode): { text: string; href?: string } => {
    const skill = (extras.skills || {})[n.id];
    const c = (extras.ruleCounts || {})[n.id];
    const counts = c ? [c.rules ? plural(c.rules, "rule") : "", c.instructions ? plural(c.instructions, "instruction") : ""]
      .filter(Boolean).join(" · ") : "";
    const base = extras.base || "";
    const rulesHref = `${base}/rules?workflow=${encodeURIComponent(wf.id || "")}&step=${encodeURIComponent(n.id)}`;
    if (skill) return { text: `uses skill: ${skill.name}${counts ? ` · ${counts}` : ""}`, href: `${base}/skills?open=${encodeURIComponent(skill.id)}` };
    if (counts) return { text: counts, href: rulesHref };
    if (n.kind === "trigger" && n.detail) return { text: n.detail };
    return { text: "" };
  }, [extras, wf.id]);

  const cardH = useCallback((id: string) => {
    const n = model.byId.get(id);
    if (!n) return 44;
    let h = 44;
    if (metaOf(n).text) h += 14;
    if (n.status === "failed" && run.error) h += 34;
    return h;
  }, [model, metaOf, run.error]);

  const layout = useMemo(() => {
    const { structure, graph, byId } = model;
    if (structure) {
      return layoutFlow(structure.items, {
        cardH, collapsed,
        pillText: (id) => { const n = byId.get(id); return n ? (n.kind === "decision" ? (n.question || n.label) : n.label) : id; },
      });
    }
    const lg = layoutGraph(graph.nodes, graph.edges, cardH);
    const nodes: LaidNode[] = graph.nodes.map((n) => {
      const p = lg.positions.get(n.id) as { x: number; y: number; h: number };
      return { kind: "card", id: n.id, x: p.x, y: p.y, w: CARD_W, h: p.h };
    });
    const edges: (LaidEdge & { label?: string })[] = graph.edges.map((e) => ({ id: e.id, from: e.from, to: e.to, kind: "seq", label: e.label }));
    return { nodes, width: lg.width, height: lg.height, edges };
  }, [model, cardH, collapsed]);

  // run view: what to dim (not-run steps; untaken branch containers)
  const dimmed = useMemo(() => {
    const out = new Set<string>();
    if (!model.runView) return out;
    const ran = (id: string) => { const s = model.byId.get(id)?.status; return s === "done" || s === "failed"; };
    const takenScopes = new Set<string>();
    for (const n of layout.nodes) {
      if (n.kind === "box" && n.variant === "branch" && n.steps.some(ran)) takenScopes.add(n.id.split(":")[1]);
    }
    for (const n of layout.nodes) {
      if (n.kind === "card" || n.kind === "pill") {
        if (model.byId.get(n.id)?.status === "not-run") out.add(n.id);
      } else if (n.kind === "box" && n.variant === "branch") {
        if (n.steps.length ? !n.steps.some(ran) : takenScopes.has(n.id.split(":")[1])) out.add(n.id);
      }
    }
    for (const n of layout.nodes) {  // a branch's pill and "0 Actions" follow its container
      if ((n.kind === "branch" || n.kind === "empty" || n.kind === "join") && /^(br|empty|bout):/.test(n.id)
          && out.has(n.id.replace(/^(br|empty|bout):/, "brbox:"))) out.add(n.id);
    }
    return out;
  }, [layout, model]);

  const basePos = useMemo(() => new Map(layout.nodes.map((n) => [n.id, { x: n.x, y: n.y }])), [layout]);

  const computed: Node[] = useMemo(() => layout.nodes.map((n) => {
    const off = (n.kind === "card" || n.kind === "pill") ? offsets[n.id] : undefined;
    const base = { id: n.id, position: { x: n.x + (off ? off.dx : 0), y: n.y + (off ? off.dy : 0) }, width: n.w, height: n.h,
                   draggable: false, connectable: false };
    const dim = dimmed.has(n.id);
    switch (n.kind) {
      case "card": {
        const gn = model.byId.get(n.id) as GraphNode;
        const meta = metaOf(gn);
        const ints = integrationsOf(n.id);
        return { ...base, type: "card", draggable: true, selected: n.id === selectedId, zIndex: 2,
                 data: { node: gn, open: (extras.openTasks || {})[n.id] || 0, meta: meta.text, metaHref: meta.href,
                         error: run.error || undefined, dim, triggerType: wf.trigger?.type, tasks,
                         grade: ints.length ? ints[0].grade : undefined } };
      }
      case "pill":
        return { ...base, type: "pill", draggable: true, selected: n.id === selectedId, zIndex: 2,
                 data: { node: model.byId.get(n.id), variant: n.variant, count: n.count, collapsed: n.collapsed, dim,
                         onToggle: toggle, open: (extras.openTasks || {})[n.id] || 0, tasks } };
      case "branch": return { ...base, type: "branch", selectable: false, zIndex: 2, data: { label: n.label, dim } };
      case "box": return { ...base, type: "box", selectable: false, zIndex: n.variant === "branch" ? -1 : -2,
                           data: { variant: n.variant, dim } };
      case "empty": return { ...base, type: "empty", selectable: false, zIndex: 2, data: { dim } };
      default: return { ...base, type: "join", selectable: false, zIndex: 1, data: {} };
    }
  }), [layout, model, dimmed, selectedId, extras, run.error, wf.trigger, metaOf, toggle, offsets, integrationsOf, tasks]);

  // controlled nodes: the computed layout, plus whatever a drag in progress has moved
  const [nodes, setNodes] = useState<Node[]>(computed);
  useEffect(() => { setNodes(computed); }, [computed]);
  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setNodes((ns) => applyNodeChanges(changes.filter((c) => c.type === "position" || c.type === "dimensions"), ns));
  }, []);

  // run view: which links the run actually travelled — step to step, through the branch it took, into the
  // rejoin only if something after it ran; nothing leaves the failed step
  const travelled = useMemo(() => {
    const ran = new Set<string>();
    if (!model.runView) return ran;
    for (const n of layout.nodes) {
      const st = model.byId.get(n.id)?.status;
      if ((n.kind === "card" || n.kind === "pill") && (st === "done" || st === "failed")) ran.add(n.id);
      if ((n.kind === "branch" || n.kind === "box") && n.steps.some((id) => { const s = model.byId.get(id)?.status; return s === "done" || s === "failed"; })) ran.add(n.id);
    }
    const joins = layout.nodes.filter((n) => n.kind === "join").map((n) => n.id);
    for (let pass = 0; pass < joins.length + 1; pass++) {
      for (const j of joins) {
        if (ran.has(j)) continue;
        const branchOk = !j.startsWith("bout:") || ran.has(j.replace(/^bout:/, "brbox:"));
        if (branchOk && layout.edges.some((e) => e.from === j && ran.has(e.to))) ran.add(j);
      }
    }
    return ran;
  }, [layout, model]);

  const edges: Edge[] = useMemo(() => layout.edges.map((e) => {
    const label = (e as { label?: string }).label;
    const failedSource = model.byId.get(e.from)?.status === "failed";
    const taken = model.runView && travelled.has(e.from) && travelled.has(e.to) && !failedSource;
    const dim = model.runView ? !taken : dimmed.has(e.from) || dimmed.has(e.to);
    return {
      // interactionWidth 0: edges aren't clickable here, and their invisible hit-stroke would sit on top of the (+)
      id: e.id, source: e.from, target: e.to, type: "flow", animated: !dim, focusable: false, zIndex: 1, interactionWidth: 0,
      data: { plusFor: tasks ? e.plusFor : undefined, label, dim, taken, kind: e.kind },
      markerEnd: e.kind === "seq" ? { type: MarkerType.ArrowClosed, width: 12, height: 12 } : undefined,
    };
  }), [layout, dimmed, model, travelled, tasks]);

  // fit-to-view on load: fit the WIDTH (never zoom in past 1), top-aligned so a long flow reads down
  const fit = useCallback(() => {
    const el = wrapRef.current, rf = rfRef.current;
    if (!el || !rf) return;
    const W = el.clientWidth || 800;
    const small = W < 640;
    const zoom = Math.max(small ? 0.35 : 0.5, Math.min(1, (W - 2 * PAD) / Math.max(layout.width, 1)));
    const contentH = layout.height * zoom + 2 * PAD;
    const h = Math.round(Math.max(small ? 320 : 440, Math.min(contentH, window.innerHeight * (small ? 0.72 : 0.78))));
    setHeight(h);
    const x = (W - layout.width * zoom) / 2;
    const y = contentH <= h ? (h - layout.height * zoom) / 2 : PAD;
    rf.setViewport({ x, y, zoom }, { duration: 0 });
  }, [layout]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    let lastW = el.clientWidth;
    let raf = 0;
    // deferred a frame: refitting changes the stage's height, which must not land inside this same RO delivery
    const ro = new ResizeObserver(() => {
      if (el.clientWidth === lastW) return;
      lastW = el.clientWidth;
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(fit);
    });
    ro.observe(el);
    return () => { ro.disconnect(); cancelAnimationFrame(raf); };
  }, [fit]);

  // the host can select a step too (window events, as Crewboss's task badges / "+" do)
  useEffect(() => {
    const onSelect = (e: Event) => {
      const id = (e as CustomEvent).detail?.id ?? null;
      if (id === null || model.byId.has(id)) setSelectedId(id);
    };
    const names = ["wf:node-click", "wf:step-tasks", "wf:step-new-task"];
    names.forEach((n) => window.addEventListener(n, onSelect));
    return () => names.forEach((n) => window.removeEventListener(n, onSelect));
  }, [model]);

  // opened on load (failed step, ?step=): tell the host too, once it has had a chance to listen
  useEffect(() => {
    if (opts.open && model.byId.has(opts.open)) requestAnimationFrame(() => fire("wf:node-click", opts.open as string));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const close = useCallback(() => { setSelectedId(null); fire("wf:close", null); }, []);

  const selected = selectedId ? model.byId.get(selectedId) : undefined;
  let panel;
  if (selected) {
    const step = steps.get(selected.id);
    const info: StepInfo = step ? { ...step, kind: step.kind || selected.kind, label: step.label || selected.label }
      : { id: selected.id, kind: selected.kind, label: selected.label, detail: selected.detail, system: selected.system };
    const chips: { text: string; href: string; tone: string }[] = [];
    const base = extras.base || "";
    const sk = (extras.skills || {})[selected.id];
    const rc = (extras.ruleCounts || {})[selected.id];
    const rulesQ = `workflow=${encodeURIComponent(wf.id || "")}&step=${encodeURIComponent(selected.id)}`;
    if (sk) chips.push({ text: `Uses skill: ${sk.name}`, href: `${base}/skills?open=${encodeURIComponent(sk.id)}`, tone: "ai" });
    if (rc && rc.rules) chips.push({ text: plural(rc.rules, "rule"), href: `${base}/rules?${rulesQ}`, tone: "logic" });
    if (rc && rc.instructions) chips.push({ text: plural(rc.instructions, "instruction"), href: `${base}/rules?${rulesQ}&kind=instruction`, tone: "ai" });
    panel = <StepPanel key={selected.id} step={info} error={selected.id === run.failedStep ? run.error : null} chips={chips}
                       integrations={step ? integrationsOf(selected.id) : []} exp={exp} opts={opts} onClose={close} />;
  } else {
    panel = (
      <div className="mc-card mc-hint">
        <p>Tap a step for its detail.{tasks && <> The <b>☑ count</b> on a card opens its tasks; <b>+</b> asks for something on that step.</>}</p>
        {wf.side_effects && wf.side_effects.length > 0 && (
          <section className="mc-section">
            <div className="mc-section-k">Side effects</div>
            <div className="mc-chips">{wf.side_effects.map((e) => <span key={e} className="mc-chip">{e}</span>)}</div>
          </section>
        )}
      </div>
    );
  }

  return (
    <Shell height={height} panel={panel} panelOpen={!!selected} onClose={close} label={`Workflow canvas${wf.name ? `: ${wf.name}` : ""}`}>
      <div ref={wrapRef} className="mc-fill">
        <ReactFlow
          className="mc-flow"
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodesChange={onNodesChange}
          onNodeDragStop={(_, node) => {
            const b = basePos.get(node.id);
            if (b) nudge(node.id, node.position.x - b.x, node.position.y - b.y);
          }}
          onInit={(inst) => { rfRef.current = inst; fit(); }}
          minZoom={0.15}
          maxZoom={2}
          nodesConnectable={false}
          nodesFocusable={false}
          edgesFocusable={false}
          elementsSelectable
          panOnDrag
          panOnScroll={!coarse()}
          zoomOnScroll={false}
          zoomOnPinch
          zoomActivationKeyCode={["Meta", "Control"]}
          zoomOnDoubleClick={false}
          preventScrolling
          proOptions={{ hideAttribution: true }}
          onNodeClick={(_, node) => { if (node.type === "card" || node.type === "pill") fire("wf:node-click", node.id); }}
          onPaneClick={() => { if (selectedId) close(); }}
        >
          <Background variant={BackgroundVariant.Dots} gap={16} size={1.2} className="mc-dots" />
          <Controls showInteractive={false} position="bottom-left" fitViewOptions={{ padding: 0.15, duration: 200 }}>
            {moved && <ControlButton onClick={reset} title="Put moved cards back" aria-label="Put moved cards back"><Icon name="reset" className="mc-ico" /></ControlButton>}
          </Controls>
          <MiniMap position="bottom-right" pannable zoomable ariaLabel="Workflow overview" className="mc-minimap"
                   nodeBorderRadius={3} nodeStrokeWidth={0}
                   nodeColor={(n) => (n.type === "box" || n.type === "join" ? "transparent" : "currentColor")} />
        </ReactFlow>
      </div>
    </Shell>
  );
}

