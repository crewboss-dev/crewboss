// node --test tests/workflow_graph.test.mjs  (driven from tests/test_canvas_graph.py)
// The canvas's edge/trigger rules as a pure function (frontend/src/workflow-graph.ts; Node strips the types).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { TRIGGER_ID, applyRunStatus, buildGraph } from '../frontend/src/workflow-graph.ts'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const pairs = (g) => g.edges.map((e) => `${e.from}>${e.to}${e.label ? `[${e.label}]` : ''}`)
const S = (id, kind = 'action', extra = {}) => ({ id, kind, label: id, ...extra })

test('straight run with edges OMITTED: steps chain in array order', () => {
  const g = buildGraph({ trigger: { type: 'timer' }, steps: [S('t', 'trigger'), S('a'), S('b', 'logic'), S('c')] })
  assert.deepEqual(pairs(g), ['t>a', 'a>b', 'b>c'])
  assert.ok(g.edges.every((e) => e.synthetic))
})

test('straight run with edges: [] behaves the same as omitted', () => {
  const g = buildGraph({ trigger: { type: 'timer' }, steps: [S('t', 'trigger'), S('a'), S('b')], edges: [] })
  assert.deepEqual(pairs(g), ['t>a', 'a>b'])
})

test('NO duplicate trigger when a step already is the trigger', () => {
  const g = buildGraph({ trigger: { type: 'timer', detail: 'every 600s' }, steps: [S('timer', 'trigger'), S('a')] })
  assert.equal(g.nodes.filter((n) => n.kind === 'trigger').length, 1)
  assert.ok(!g.nodes.some((n) => n.id === TRIGGER_ID))
  // the trigger step borrows the workflow's trigger line as its detail
  assert.equal(g.nodes[0].detail, 'every 600s')
})

test('trigger is synthesised only when no trigger step exists, and linked to the first step', () => {
  const g = buildGraph({ trigger: { type: 'event', detail: 'on analyse' }, steps: [S('extract', 'precode'), S('ai', 'ai'), S('write')],
    edges: [{ from: 'extract', to: 'ai' }, { from: 'ai', to: 'write' }] })
  assert.equal(g.nodes[0].id, TRIGGER_ID)
  assert.equal(g.nodes[0].label, 'Event')
  assert.equal(g.nodes[0].detail, 'on analyse')
  assert.deepEqual(pairs(g), ['extract>ai', 'ai>write', `${TRIGGER_ID}>extract`])
})

test('click/button trigger label', () => {
  const g = buildGraph({ trigger: { type: 'button' }, steps: [S('a')] })
  assert.equal(g.nodes[0].label, 'Click')
})

test('explicit edges are drawn as given (labels kept); unreferenced lead-in and tail are joined on', () => {
  // billing.xero-token.refresh shape: edges only cover the middle
  const g = buildGraph({ trigger: { type: 'timer' },
    steps: [S('timer', 'trigger'), S('load'), S('check', 'logic'), S('call'), S('save'), S('beat', 'logic')],
    edges: [{ from: 'load', to: 'check' }, { from: 'check', to: 'call', label: 'near expiry' }, { from: 'call', to: 'save' }] })
  assert.deepEqual(pairs(g), ['load>check', 'check>call[near expiry]', 'call>save', 'timer>load', 'save>beat'])
  assert.deepEqual(g.edges.filter((e) => !e.synthetic).map((e) => e.from), ['load', 'check', 'call'])
})

test('branches fan out from explicit edges; branch targets are not re-chained', () => {
  // billing.stripe-webhook.reconcile shape
  const g = buildGraph({ trigger: { type: 'webhook' },
    steps: [S('trigger', 'trigger'), S('verify', 'logic'), S('payout'), S('fee'), S('paid'), S('notify'), S('xero')],
    edges: [{ from: 'paid', to: 'notify' }, { from: 'paid', to: 'xero' }] })
  assert.deepEqual(pairs(g), ['paid>notify', 'paid>xero', 'trigger>verify', 'verify>payout', 'payout>fee', 'fee>paid'])
})

test('a trigger step mid-flow is never given a synthetic incoming edge', () => {
  const g = buildGraph({ trigger: { type: 'event' }, steps: [S('a'), S('b'), S('approve', 'trigger'), S('run')] })
  assert.deepEqual(pairs(g), ['a>b', 'approve>run'])
  assert.ok(!g.nodes.some((n) => n.id === TRIGGER_ID))
})

test('edges naming a missing step are dropped; duplicates and self-loops ignored', () => {
  const g = buildGraph({ trigger: { type: 'timer' }, steps: [S('t', 'trigger'), S('a')],
    edges: [{ from: 't', to: 'ghost' }, { from: 't', to: 'a' }, { from: 't', to: 'a' }, { from: 'a', to: 'a' }] })
  assert.deepEqual(pairs(g), ['t>a'])
})

test('empty workflow is safe', () => {
  assert.deepEqual(buildGraph({}), { nodes: [], edges: [] })
  const g = buildGraph({ trigger: { type: 'timer' } })
  assert.equal(g.nodes.length, 1)
  assert.equal(g.edges.length, 0)
})

test('run view: ancestors of the failed step are done, the untaken branch and later steps did not run', () => {
  const { nodes, edges } = buildGraph({ trigger: { type: 'webhook' },
    steps: [S('t', 'trigger'), S('a'), S('b'), S('c'), S('d')],
    edges: [{ from: 'a', to: 'b', label: 'yes' }, { from: 'a', to: 'c', label: 'no' }] })
  const st = Object.fromEntries(applyRunStatus(nodes, edges, 'c').map((n) => [n.id, n.status]))
  assert.deepEqual(st, { t: 'done', a: 'done', b: 'not-run', c: 'failed', d: 'not-run' })
  assert.equal(applyRunStatus(nodes, edges, 'nope'), nodes)
  assert.equal(applyRunStatus(nodes, edges, null), nodes)
})

// Against the real scanned catalog when this checkout has one (scanner/out is not committed).
const CATALOG = join(ROOT, 'scanner/out/latest.json')
test('every real workflow: no floating cards, exactly one trigger unless the steps carry more', { skip: !existsSync(CATALOG) }, () => {
  const { workflows } = JSON.parse(readFileSync(CATALOG, 'utf8'))
  let straight = 0
  for (const wf of workflows) {
    const g = buildGraph(wf)
    if (!(wf.edges || []).length) straight++
    const triggerSteps = (wf.steps || []).filter((s) => s.kind === 'trigger').length
    assert.equal(g.nodes.filter((n) => n.kind === 'trigger').length, Math.max(1, triggerSteps), wf.id)
    const touched = new Set(g.edges.flatMap((e) => [e.from, e.to]))
    if (g.nodes.length > 1) assert.deepEqual(g.nodes.filter((n) => !touched.has(n.id)).map((n) => n.id), [], wf.id)
    // explicit edges survive verbatim
    for (const e of wf.edges || []) assert.ok(g.edges.some((x) => x.from === e.from && x.to === e.to && !x.synthetic), wf.id)
  }
  assert.ok(straight > 0)
})
