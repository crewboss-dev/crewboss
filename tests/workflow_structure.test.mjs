// node --test tests/workflow_structure.test.mjs — the Power Automate-style nesting (frontend/src/workflow-structure.ts)
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { applyRunStatus, buildGraph } from '../frontend/src/workflow-graph.ts'
import { buildStructure, stepIds } from '../frontend/src/workflow-structure.ts'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const S = (id, kind = 'action', extra = {}) => ({ id, kind, label: id, ...extra })
const shape = (items) => items.map((it) => it.type === 'step' ? it.id
  : it.type === 'loop' ? { loop: it.id, body: shape(it.body) }
    : { [it.decision ? 'if' : 'fanout']: it.id, branches: it.branches.map((b) => [b.label, shape(b.items)]) })
const structure = (wf) => { const g = buildGraph(wf); return buildStructure(g.nodes, g.edges) }

test('a straight run is one column', () => {
  const s = structure({ trigger: { type: 'timer' }, steps: [S('t', 'trigger'), S('a'), S('b')] })
  assert.deepEqual(shape(s.items), ['t', 'a', 'b'])
  assert.deepEqual(s.flow, [{ from: 't', to: 'a' }, { from: 'a', to: 'b' }])
})

test('4-way decision from labelled edges, with an empty branch and a rejoin', () => {
  // AiOS email triage: classify → lead / case / fyi / ignore → advance the cursor
  const s = structure({ trigger: { type: 'timer' },
    steps: [S('timer', 'trigger'), S('fetch'), S('classify', 'ai'), S('what', 'decision', { question: 'What is it?' }),
      S('lead'), S('notify'), S('case'), S('fyi'), S('advance')],
    edges: [{ from: 'what', to: 'lead', label: 'lead' }, { from: 'lead', to: 'notify' }, { from: 'notify', to: 'advance' },
      { from: 'what', to: 'case', label: 'case' }, { from: 'case', to: 'advance' },
      { from: 'what', to: 'fyi', label: 'fyi' }, { from: 'fyi', to: 'advance' },
      { from: 'what', to: 'advance', label: 'ignore' }] })
  assert.deepEqual(shape(s.items), ['timer', 'fetch', 'classify',
    { if: 'what', branches: [['lead', ['lead', 'notify']], ['case', ['case']], ['fyi', ['fyi']], ['ignore', []]] }, 'advance'])
  const into = (id) => s.flow.filter((e) => e.to === id).map((e) => e.from).sort()
  assert.deepEqual(into('advance'), ['case', 'fyi', 'notify', 'what'])  // the empty "ignore" branch joins from the decision
  assert.deepEqual(into('lead'), ['what'])
})

test('a fan-out after an ordinary step with no rejoin (stripe payout shape)', () => {
  const s = structure({ trigger: { type: 'webhook' }, steps: [S('t', 'trigger'), S('paid'), S('notify'), S('xero')],
    edges: [{ from: 'paid', to: 'notify' }, { from: 'paid', to: 'xero' }] })
  assert.deepEqual(shape(s.items), ['t', 'paid', { fanout: 'paid__fanout', branches: [['', ['notify']], ['', ['xero']]] }])
  assert.deepEqual(s.flow.filter((e) => e.from === 'paid').map((e) => e.to), ['notify', 'xero'])
})

test('nested: a loop holding a True/False condition (parent/branch), then the flow carries on', () => {
  const s = structure({ trigger: { type: 'email', detail: 'When a new email arrives' },
    steps: [S('get-attachments'), S('each', 'loop', { label: 'For each attachment' }),
      S('is-pdf', 'decision', { parent: 'each', question: 'Is it a PDF?' }),
      S('extract', 'ai', { parent: 'is-pdf', branch: 'True' }), S('bill', 'action', { parent: 'is-pdf', branch: 'True' }),
      S('summary')],
    edges: [{ from: 'is-pdf', to: 'extract', label: 'True' }, { from: 'is-pdf', to: 'summary', label: 'False' }] })
  assert.deepEqual(shape(s.items), ['__trigger__', 'get-attachments',
    { loop: 'each', body: [{ if: 'is-pdf', branches: [['True', ['extract', 'bill']], ['False', []]] }] }, 'summary'])
  assert.deepEqual(stepIds(s.items), ['__trigger__', 'get-attachments', 'each', 'is-pdf', 'extract', 'bill', 'summary'])
  assert.ok(s.flow.some((e) => e.from === 'each' && e.to === 'is-pdf'))
  assert.ok(s.flow.some((e) => e.from === 'is-pdf' && e.to === 'extract'))
})

test('the run view walks the structure: a failure inside a branch greys the other branch', () => {
  const wf = { trigger: { type: 'webhook' }, steps: [S('t', 'trigger'), S('check', 'decision'), S('yes'), S('no'), S('end')],
    edges: [{ from: 'check', to: 'yes', label: 'Yes' }, { from: 'check', to: 'no', label: 'No' }, { from: 'yes', to: 'end' }, { from: 'no', to: 'end' }] }
  const g = buildGraph(wf)
  const s = buildStructure(g.nodes, g.edges)
  const flowEdges = s.flow.map((e, i) => ({ id: String(i), ...e, synthetic: true }))
  const st = Object.fromEntries(applyRunStatus(g.nodes, flowEdges, 'yes').map((n) => [n.id, n.status]))
  assert.deepEqual(st, { t: 'done', check: 'done', yes: 'failed', no: 'not-run', end: 'not-run' })
  const viaPath = Object.fromEntries(applyRunStatus(g.nodes, flowEdges, null, ['t', 'check', 'no', 'end']).map((n) => [n.id, n.status]))
  assert.deepEqual(viaPath, { t: 'done', check: 'done', yes: 'not-run', no: 'done', end: 'done' })
})

test('what will not nest falls back (null): two roots, a cross merge, a cycle', () => {
  assert.equal(structure({ trigger: { type: 'event' }, steps: [S('a'), S('b'), S('approve', 'trigger'), S('run')] }), null)
  const cross = buildGraph({ trigger: { type: 'x' }, steps: [S('t', 'trigger'), S('d', 'decision'), S('a'), S('b'), S('c'), S('e')],
    edges: [{ from: 'd', to: 'a' }, { from: 'd', to: 'b' }, { from: 'a', to: 'c' }, { from: 'b', to: 'c' }, { from: 'a', to: 'e' }, { from: 'c', to: 'e' }] })
  const cs = buildStructure(cross.nodes, cross.edges)
  assert.ok(cs === null || stepIds(cs.items).length === 6)
  const cyc = buildGraph({ trigger: { type: 'x' }, steps: [S('t', 'trigger'), S('a'), S('b')], edges: [{ from: 'b', to: 'a' }] })
  assert.equal(buildStructure(cyc.nodes, cyc.edges), null)
})

const CATALOG = join(ROOT, 'scanner/out/latest.json')
test('real catalog: every workflow either nests (all steps placed once) or falls back', { skip: !existsSync(CATALOG) }, () => {
  const { workflows } = JSON.parse(readFileSync(CATALOG, 'utf8'))
  let nested = 0
  for (const wf of workflows) {
    const g = buildGraph(wf)
    const s = buildStructure(g.nodes, g.edges)
    if (!s) continue
    nested++
    assert.deepEqual(stepIds(s.items).sort(), g.nodes.map((n) => n.id).sort(), wf.id)
  }
  assert.ok(nested >= workflows.length - 3, `${nested}/${workflows.length} nest`)
})

// --- coordinates (frontend/src/workflow-flow-layout.ts) ------------------------------------------------
import { CARD_W, layoutFlow } from '../frontend/src/workflow-flow-layout.ts'

const sizing = (collapsed = []) => ({ cardH: () => 44, pillText: (id) => id, collapsed: new Set(collapsed) })
const overlap = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h
const LOOP_WF = { trigger: { type: 'email' }, steps: [S('get'), S('each', 'loop'), S('is-pdf', 'decision', { parent: 'each' }),
  S('extract', 'ai', { parent: 'is-pdf', branch: 'True' }), S('bill', 'action', { parent: 'is-pdf', branch: 'True' }), S('summary')],
  edges: [{ from: 'is-pdf', to: 'extract', label: 'True' }, { from: 'is-pdf', to: 'summary', label: 'False' }] }

test('layout: a straight run is one centred column with even gaps and a (+) on every link', () => {
  const s = structure({ trigger: { type: 'timer' }, steps: [S('t', 'trigger'), S('a'), S('b'), S('c')] })
  const L = layoutFlow(s.items, sizing())
  const cards = L.nodes.filter((n) => n.kind === 'card')
  assert.ok(cards.every((c) => c.x === cards[0].x && c.w === CARD_W))
  const gaps = cards.slice(1).map((c, i) => c.y - (cards[i].y + cards[i].h))
  assert.ok(gaps.every((g) => g === gaps[0] && g > 20))
  assert.deepEqual(L.edges.map((e) => e.plusFor), ['t', 'a', 'b'])
  assert.equal(L.width, CARD_W)
})

test('layout: nothing overlaps, branches sit side by side inside their scope, edges resolve', () => {
  const L = layoutFlow(structure(LOOP_WF).items, sizing())
  const solid = L.nodes.filter((n) => ['card', 'pill', 'branch', 'empty'].includes(n.kind))
  for (let i = 0; i < solid.length; i++) for (let j = i + 1; j < solid.length; j++) {
    assert.ok(!overlap(solid[i], solid[j]), `${solid[i].id} overlaps ${solid[j].id}`)
  }
  const ids = new Set(L.nodes.map((n) => n.id))
  for (const e of L.edges) assert.ok(ids.has(e.from) && ids.has(e.to), e.id)
  const [t, f] = ['brbox:is-pdf:0', 'brbox:is-pdf:1'].map((id) => L.nodes.find((n) => n.id === id))
  assert.equal(t.y, f.y)
  assert.ok(t.x + t.w <= f.x, 'True sits left of False')
  const inside = (inner, outer) => inner.x >= outer.x && inner.y >= outer.y && inner.x + inner.w <= outer.x + outer.w && inner.y + inner.h <= outer.y + outer.h
  const loopBox = L.nodes.find((n) => n.id === 'box:each')
  const scopeBox = L.nodes.find((n) => n.id === 'box:is-pdf')
  assert.ok(inside(scopeBox, loopBox) && inside(t, scopeBox) && inside(f, scopeBox))
  for (const id of ['extract', 'bill']) assert.ok(inside(L.nodes.find((n) => n.id === id), t))
  assert.ok(L.nodes.some((n) => n.kind === 'empty' && n.id === 'empty:is-pdf:1'), 'False shows 0 Actions')
  const summary = L.nodes.find((n) => n.id === 'summary')
  assert.ok(summary.y > loopBox.y + loopBox.h, 'the flow carries on below the loop')
})

test('layout: a collapsed loop hides its body and says how many actions', () => {
  const L = layoutFlow(structure(LOOP_WF).items, sizing(['each']))
  assert.ok(!L.nodes.some((n) => ['extract', 'bill', 'is-pdf'].includes(n.id)))
  assert.equal(L.nodes.find((n) => n.id === 'each').count, 3)  // the condition + its two steps
})

test('the decisions fixture nests: 4-way + rejoin, loop with True/False, 2-way', () => {
  const { workflows } = JSON.parse(readFileSync(join(ROOT, 'fixtures/catalog-decisions-fixture.json'), 'utf8'))
  const got = Object.fromEntries(workflows.map((wf) => [wf.id, shape(structure(wf).items)]))
  assert.deepEqual(got['email.triage.route'], ['timer', 'fetch', 'classify',
    { if: 'what', branches: [['lead', ['lead', 'lead-card']], ['case', ['case']], ['fyi', ['fyi']], ['ignore', []]] }, 'advance'])
  assert.deepEqual(got['billing.supplier-invoice.intake'], ['__trigger__', 'get-attachments',
    { loop: 'each', body: [{ if: 'is-pdf', branches: [['True', ['extract', 'draft-bill']], ['False', []]] }] }, 'summary'])
  assert.deepEqual(got['monitoring.alert-gate.check'], ['hook', 'match',
    { if: 'gate', branches: [['Yes', ['raise', 'page']], ['No', []]] }, 'log'])
})
