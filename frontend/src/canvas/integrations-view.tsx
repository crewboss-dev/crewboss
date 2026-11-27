// kind=integrations — one module's links on the same infinite desktop (C4 "system context" style): the module
// in the middle, what sends data TO it on the left, what it sends to on the right, two-way systems balancing
// the shorter side; systems grouped by zone under a heading; one line per system, coloured by its WEAKEST
// grade, arrowheads showing which way the data goes. Click a system (or its line) → the side panel lists every
// integration with it, weakest first, with the grade reasons; the other systems dim; Escape closes;
// ?open=<integration id> reopens the panel holding that id. Systems can be dragged (kept per browser).
import {
  applyNodeChanges,
  Background,
  BackgroundVariant,
  ControlButton,
  Controls,
  EdgeLabelRenderer,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  useInternalNode,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeChange,
  type NodeProps,
  type ReactFlowInstance,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Icon } from "./icons.tsx";
import {
  layoutIntegrations, modulesOf, NODE_H, NODE_W, summaryOf,
  type Integration, type IntegrationsExport, type SystemGroup,
} from "./integrations-model.ts";
import { SystemPanel } from "./panel.tsx";
import { GradeBadge, plural, Shell, useOffsets, type CanvasOptions } from "./shared.tsx";

export type IntegrationsCanvasData = { integrations?: IntegrationsExport | null; module?: string | null };

const HUB = "hub";
const sysId = (peer: string) => `sys:${peer}`;

function urlOpen(): string | null {
  try { return new URLSearchParams(window.location.search).get("open"); } catch { return null; }
}
function setUrlOpen(id: string | null) {
  try {
    const u = new URL(window.location.href);
    if (id) u.searchParams.set("open", id); else u.searchParams.delete("open");
    window.history.replaceState(window.history.state, "", u.toString());
  } catch { /* no history API: fine */ }
}

// --- nodes ----------------------------------------------------------------------------------------------

type SystemData = { group: SystemGroup; dim: boolean; side: "left" | "right"; onOpen: (peer: string) => void };

function SystemNode({ data, selected }: NodeProps) {
  const { group, dim, onOpen } = data as unknown as SystemData;
  const g = /^[ABCD]$/.test(group.grade) ? group.grade : "x";
  return (
    <div className={`mc-sys mc-sys-${g} ${group.live ? "" : "mc-sys-leftover"} ${dim ? "mc-dim" : ""} ${selected ? "mc-selected" : ""}`}
         role="button" tabIndex={0} title={`${group.peer} — ${group.label}`}
         aria-label={`${group.peer}: ${plural(group.links.length, "integration")}, weakest grade ${group.grade}. Enter shows them.`}
         onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(group.peer); } }}>
      <Handle id="l" type="target" position={Position.Left} className="mc-handle" isConnectable={false} />
      <Handle id="r" type="source" position={Position.Right} className="mc-handle" isConnectable={false} />
      <span className="mc-sys-bar" aria-hidden />
      <span className="mc-sys-body">
        <span className="mc-sys-name">{group.peer}</span>
        <span className="mc-sys-sub">{group.sub}{group.live ? "" : " · leftover"}</span>
      </span>
      <GradeBadge grade={group.grade} />
    </div>
  );
}

function HubNode({ data }: NodeProps) {
  const { module, label, rows } = data as { module: string; label?: string; rows: Integration[] };
  const name = label || module;
  const s = summaryOf(rows);
  return (
    <div className="mc-hub" aria-label={`${name}: ${plural(rows.length, "integration")}`}>
      <Handle id="l" type="target" position={Position.Left} className="mc-handle" isConnectable={false} />
      <Handle id="r" type="source" position={Position.Right} className="mc-handle" isConnectable={false} />
      <span className="mc-hub-name">{name}</span>
      <span className="mc-hub-sub">{plural(rows.length, "integration")}</span>
      <span className="mc-hub-grades">
        {(["D", "C", "B", "A"] as const).filter((g) => s[g]).map((g) => <span key={g} className="mc-hub-grade"><GradeBadge grade={g} /> {s[g]}</span>)}
      </span>
    </div>
  );
}

function HeadNode({ data }: NodeProps) {
  return <div className="mc-zone-head">{(data as { text: string }).text}</div>;
}

const nodeTypes = { system: SystemNode, hub: HubNode, head: HeadNode };

// --- the line: horizontal, weakest grade's colour, arrowheads the way the data goes -----------------------

type LinkData = { side: "left" | "right"; flow: string; grade: string; label: string; dim: boolean; peer: string };

function LinkEdge({ id, sourceX, sourceY, targetX, targetY, data }: EdgeProps) {
  const d = data as unknown as LinkData;
  const hub = useInternalNode(HUB);
  const sysY = d.side === "left" ? sourceY : targetY;
  let hubY = sysY;
  if (hub) {
    const top = hub.internals.positionAbsolute.y, h = hub.measured.height || hub.height || 0;
    hubY = Math.max(top + 8, Math.min(top + h - 8, sysY));
  }
  const [sy, ty] = d.side === "left" ? [sysY, hubY] : [hubY, sysY];
  const mid = (sourceX + targetX) / 2;
  const path = Math.abs(sy - ty) < 0.5 ? `M ${sourceX} ${sy} L ${targetX} ${ty}`
    : `M ${sourceX} ${sy} L ${mid} ${sy} L ${mid} ${ty} L ${targetX} ${ty}`;
  const toRight = (d.side === "left" && (d.flow === "in" || d.flow === "both")) || (d.side === "right" && (d.flow === "out" || d.flow === "both"));
  const toLeft = d.flow === "both";
  const g = /^[ABCD]$/.test(d.grade) ? d.grade : "x";
  const lx = d.side === "left" ? sourceX + (targetX - sourceX) * 0.42 : sourceX + (targetX - sourceX) * 0.58;
  const ly = d.side === "left" ? sy : ty;
  return (
    <>
      <g className={`mc-link-line mc-line-${g} ${d.dim ? "mc-edge-dim" : ""}`}>
        <path d={path} className="mc-line-hit" />
        <path id={id} d={path} className="mc-line" fill="none" />
        {toRight && <path d={`M ${targetX - 9} ${ty - 4.5} L ${targetX} ${ty} L ${targetX - 9} ${ty + 4.5} Z`} className="mc-arrow" />}
        {toLeft && <path d={`M ${sourceX + 9} ${sy - 4.5} L ${sourceX} ${sy} L ${sourceX + 9} ${sy + 4.5} Z`} className="mc-arrow" />}
      </g>
      {d.label && (
        <EdgeLabelRenderer>
          <div className={`mc-link-label ${d.dim ? "mc-dim" : ""}`} title={d.label}
               ref={(el) => { if (el) el.style.transform = `translate(-50%, -100%) translate(${lx}px, ${ly - 3}px)`; }}>{d.label}</div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

const edgeTypes = { link: LinkEdge };

// --- the canvas -----------------------------------------------------------------------------------------

const PAD = 24;
const coarse = () => window.matchMedia("(pointer: coarse)").matches;

export function IntegrationsCanvas({ data, opts }: { data: IntegrationsCanvasData; opts: CanvasOptions }) {
  const exp = data.integrations || null;
  const all = useMemo(() => (exp && exp.integrations) || [], [exp]);
  const modules = useMemo(() => modulesOf(exp), [exp]);
  const fixed = opts.module || data.module || null;
  const wanted = opts.open || urlOpen();
  const [module, setModule] = useState<string>(() => {
    if (fixed) return fixed;
    const hit = wanted ? all.find((i) => i.id === wanted) : undefined;
    return hit ? hit.module : (modules[0] ? modules[0].module : "");
  });
  const rows = useMemo(() => all.filter((i) => i.module === module), [all, module]);
  const layout = useMemo(() => layoutIntegrations(rows, exp), [rows, exp]);
  const groups = useMemo(() => new Map(layout.systems.map((s) => [s.group.peer, s.group])), [layout]);

  const wrapRef = useRef<HTMLDivElement>(null);
  const [openPeer, setOpenPeer] = useState<string | null>(() => {
    const hit = wanted ? rows.find((i) => i.id === wanted) : undefined;
    return hit ? hit.peer : null;
  });
  const [focus, setFocus] = useState<string | null>(() => (wanted && rows.some((i) => i.id === wanted) ? wanted : null));
  const open = useCallback((peer: string) => {
    const g = groups.get(peer);
    if (!g) return;
    setOpenPeer(peer);
    setFocus(null);
    setUrlOpen(g.links[0].id);
    wrapRef.current?.closest(".mallen-canvas")?.dispatchEvent(new CustomEvent("mallen-canvas:open",
      { bubbles: true, detail: { kind: "integrations", peer, ids: g.links.map((i) => i.id) } }));
  }, [groups]);
  const close = useCallback(() => {
    if (openPeer) setUrlOpen(null);
    setOpenPeer(null);
    setFocus(null);
  }, [openPeer]);

  const rfRef = useRef<ReactFlowInstance | null>(null);
  const [height, setHeight] = useState(420);
  const { offsets, nudge, reset, moved } = useOffsets(`mallen-canvas:int:${module}`);

  const basePos = useMemo(() => {
    const m = new Map<string, { x: number; y: number }>();
    m.set(HUB, { x: layout.hub.x, y: layout.hub.y });
    for (const s of layout.systems) m.set(sysId(s.group.peer), { x: s.x, y: s.y });
    return m;
  }, [layout]);

  const computed: Node[] = useMemo(() => {
    const at = (id: string, x: number, y: number) => { const o = offsets[id]; return { x: x + (o ? o.dx : 0), y: y + (o ? o.dy : 0) }; };
    const out: Node[] = [{
      id: HUB, type: "hub", position: at(HUB, layout.hub.x, layout.hub.y), width: layout.hub.w, height: layout.hub.h,
      draggable: true, selectable: false, connectable: false,
      data: { module, label: (exp && exp.modules && exp.modules[module]) || undefined, rows },
    }];
    for (const h of layout.heads) {
      out.push({ id: h.id, type: "head", position: { x: h.x, y: h.y }, width: NODE_W, height: 20, draggable: false,
                 selectable: false, connectable: false, data: { text: h.text } });
    }
    for (const s of layout.systems) {
      const id = sysId(s.group.peer);
      out.push({ id, type: "system", position: at(id, s.x, s.y), width: NODE_W, height: NODE_H, draggable: true,
                 connectable: false, selected: openPeer === s.group.peer,
                 data: { group: s.group, side: s.side, dim: !!openPeer && openPeer !== s.group.peer, onOpen: open } });
    }
    return out;
  }, [layout, offsets, module, rows, openPeer, open, exp]);

  const [nodes, setNodes] = useState<Node[]>(computed);
  useEffect(() => { setNodes(computed); }, [computed]);
  const onNodesChange = useCallback((changes: NodeChange[]) => {
    setNodes((ns) => applyNodeChanges(changes.filter((c) => c.type === "position" || c.type === "dimensions"), ns));
  }, []);

  const edges: Edge[] = useMemo(() => layout.systems.map((s) => {
    const id = sysId(s.group.peer);
    const left = s.side === "left";
    return {
      id: `link:${s.group.peer}`, type: "link", source: left ? id : HUB, target: left ? HUB : id,
      sourceHandle: "r", targetHandle: "l", focusable: false, zIndex: 1,
      data: { side: s.side, flow: s.group.flow, grade: s.group.grade, label: s.group.label, peer: s.group.peer,
              dim: !!openPeer && openPeer !== s.group.peer },
    };
  }), [layout, openPeer]);

  const fit = useCallback(() => {
    const el = wrapRef.current, rf = rfRef.current;
    if (!el || !rf) return;
    const W = el.clientWidth || 900;
    const zoom = Math.max(0.28, Math.min(1, (W - 2 * PAD) / Math.max(layout.width, 1)));
    const contentH = layout.height * zoom + 2 * PAD;
    const h = Math.round(Math.max(W < 640 ? 240 : 300, Math.min(contentH, window.innerHeight * 0.8)));
    setHeight(h);
    rf.setViewport({ x: (W - layout.width * zoom) / 2, y: contentH <= h ? (h - layout.height * zoom) / 2 : PAD, zoom }, { duration: 0 });
  }, [layout]);

  useEffect(() => { fit(); }, [fit]);
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

  const g = openPeer ? groups.get(openPeer) : undefined;
  const panel = g
    ? <SystemPanel key={g.peer} peer={g.peer} zone={g.zone} links={g.links} exp={exp} opts={opts} focus={focus} onClose={close} />
    : (
      <div className="mc-card mc-hint">
        <p>Tap a system (or its line) for every integration with it, weakest first, and why each has its grade.</p>
        <p className="mc-muted mc-small">Left: data comes to {module || "us"}. Right: data goes out. Arrowheads at both ends: both ways.
          The line's colour is the weakest grade.</p>
      </div>
    );

  const toolbar = !fixed && modules.length > 1 ? (
    <div className="mc-modules" role="group" aria-label="Module">
      {modules.map((m) => (
        <button key={m.module} type="button" className={`mc-chip mc-chip-btn ${m.module === module ? "mc-chip-on" : ""}`}
                aria-pressed={m.module === module} onClick={() => { setModule(m.module); close(); }}>
          {m.module} <span className="mc-tabular mc-muted">{m.count}</span>
        </button>
      ))}
    </div>
  ) : null;

  if (!all.length) {
    return <div className="mc-card mc-hint mc-empty-state">No integrations to draw yet.</div>;
  }

  return (
    <Shell height={height} panel={panel} panelOpen={!!g} onClose={close} label={`Integrations of ${module}`} toolbar={toolbar}>
      <div ref={wrapRef} className="mc-fill">
        <ReactFlow
          className="mc-flow mc-flow-int"
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
          onNodeClick={(_, node) => { if (node.type === "system") open((node.data as unknown as SystemData).group.peer); }}
          onEdgeClick={(_, edge) => open((edge.data as unknown as LinkData).peer)}
          onPaneClick={() => { if (openPeer) close(); }}
        >
          <Background variant={BackgroundVariant.Dots} gap={16} size={1.2} className="mc-dots" />
          <Controls showInteractive={false} position="bottom-left" fitViewOptions={{ padding: 0.1, duration: 200 }}>
            {moved && <ControlButton onClick={reset} title="Put moved systems back" aria-label="Put moved systems back"><Icon name="reset" className="mc-ico" /></ControlButton>}
          </Controls>
          <MiniMap position="bottom-right" pannable zoomable ariaLabel="Diagram overview" className="mc-minimap"
                   nodeBorderRadius={3} nodeStrokeWidth={0}
                   nodeColor={(n) => (n.type === "head" ? "transparent" : "currentColor")} />
        </ReactFlow>
      </div>
    </Shell>
  );
}

