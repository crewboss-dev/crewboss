// node --test tests/workflow_layout.test.mjs — needs frontend/node_modules (dagre); the pytest driver skips otherwise.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { buildGraph } from '../frontend/src/workflow-graph.ts'
import { CARD_W, layoutGraph } from '../frontend/src/workflow-layout.ts'
const CARD_H = 44

const S = (id, kind = 'action') => ({ id, kind, label: id })

test('a straight run is one centred column, top to bottom, evenly spaced', () => {
  const g = buildGraph({ trigger: { type: 'timer' }, steps: ['t', 'a', 'b', 'c', 'd'].map((id, i) => S(id, i ? 'action' : 'trigger')) })
  const { positions, width, height } = layoutGraph(g.nodes, g.edges, () => CARD_H)
  const ps = ['t', 'a', 'b', 'c', 'd'].map((id) => positions.get(id))
  assert.ok(ps.every((p) => p.x === ps[0].x))
  const gaps = ps.slice(1).map((p, i) => p.y - ps[i].y)
  assert.ok(gaps.every((d) => d === gaps[0] && d > CARD_H))
  assert.equal(width, CARD_W)
  assert.equal(height, ps[4].y + CARD_H)
})

test('labelled branches fan out side by side under their parent', () => {
  const g = buildGraph({ trigger: { type: 'webhook' }, steps: [S('t', 'trigger'), S('check', 'logic'), S('yes'), S('no')],
    edges: [{ from: 'check', to: 'yes', label: 'ok' }, { from: 'check', to: 'no', label: 'on error' }] })
  const { positions } = layoutGraph(g.nodes, g.edges, () => CARD_H)
  const [c, y, n] = ['check', 'yes', 'no'].map((id) => positions.get(id))
  assert.equal(y.y, n.y)
  assert.ok(y.y > c.y)
  assert.ok(Math.abs(y.x - n.x) >= CARD_W)
  const mid = (y.x + n.x) / 2
  assert.ok(Math.abs(mid - c.x) < 1, 'parent centred over its branches')
})
