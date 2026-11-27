// node --test tests/canvas_model.test.mjs  (driven from tests/test_canvas_graph.py)
// The shared canvas's integration rules (frontend/src/canvas/integrations-model.ts; Node strips the types).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  groupSystems, layoutIntegrations, modulesOf, resolveStep, stepIntegrationIds, summaryOf, vocabLabel, HUB_W, LINE_W, NODE_W,
} from '../frontend/src/canvas/integrations-model.ts'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const EXP = JSON.parse(readFileSync(join(ROOT, 'fixtures/integrations-fixture.json'), 'utf8'))
const core = EXP.integrations.filter((i) => i.module === 'core')

test('a step resolves through its system, weakest grade first', () => {
  assert.deepEqual(resolveStep({ system: 'graph' }, EXP).map((i) => i.id), ['ms-mail-send', 'ms-sharepoint'])
  assert.deepEqual(resolveStep({ system: 'db' }, EXP).map((i) => i.grade), ['C'])
  assert.deepEqual(resolveStep({ system: 'internal' }, EXP), [])
  assert.deepEqual(resolveStep({ system: 'nope' }, EXP), [])
  assert.deepEqual(resolveStep({ system: 'graph' }, null), [])
})

test("a step's own `integrations` narrows its system; unknown ids are dropped", () => {
  assert.deepEqual(stepIntegrationIds({ system: 'graph', integrations: ['ms-sharepoint'] }, EXP), ['ms-sharepoint'])
  assert.deepEqual(resolveStep({ system: 'graph', integrations: ['ms-sharepoint', 'gone'] }, EXP).map((i) => i.id), ['ms-sharepoint'])
  // an empty list is not a narrowing: the system's ids apply
  assert.equal(resolveStep({ system: 'graph', integrations: [] }, EXP).length, 2)
})

test('vocab labels, falling back to the code', () => {
  assert.equal(vocabLabel(EXP, 'auths', 'entra_oauth'), 'Microsoft Entra (OAuth)')
  assert.equal(vocabLabel(EXP, 'auths', 'brand_new'), 'brand_new')
  assert.equal(vocabLabel(null, 'auths', 'x'), 'x')
})

test('modules biggest first; summary counts every grade, zero included', () => {
  assert.deepEqual(modulesOf(EXP).map((m) => m.module), ['core', 'pbx'])
  assert.deepEqual(summaryOf(core), { A: 3, B: 2, C: 1, D: 0 })
})

test('one box per system: weakest grade, flow both when its links disagree', () => {
  const gs = groupSystems(core, EXP)
  const ms = gs.find((g) => g.peer === 'Microsoft 365')
  assert.equal(ms.links.length, 2)
  assert.equal(ms.flow, 'both')            // out + both
  assert.equal(ms.sub, '2 links')
  const pg = gs.find((g) => g.peer === 'Postgres')
  assert.equal(pg.grade, 'C')
  assert.equal(groupSystems([{ ...core[0], grade: 'A' }, { ...core[0], id: 'x2', grade: 'D' }]).at(0).grade, 'D')
})

test('layout: data in on the left, out on the right, two-way balances; hub in the middle spans the height', () => {
  const L = layoutIntegrations(core, EXP)
  const side = Object.fromEntries(L.systems.map((s) => [s.group.peer, s.side]))
  assert.equal(side.Zabbix, 'left')        // in
  assert.equal(side.Anthropic, 'right')    // out
  const lefts = L.systems.filter((s) => s.side === 'left').length
  const rights = L.systems.filter((s) => s.side === 'right').length
  assert.ok(Math.abs(lefts - rights) <= 1, `${lefts} vs ${rights}`)
  assert.equal(L.hub.x, NODE_W + LINE_W)
  assert.equal(L.width, NODE_W + LINE_W + HUB_W + LINE_W + NODE_W)
  for (const s of L.systems) assert.ok(s.y >= L.hub.y && s.y + 40 <= L.hub.y + L.hub.h + 8)
  // zone headings precede their systems, and every zone is labelled from the vocab
  assert.ok(L.heads.every((h) => !h.text.includes('_')))
  assert.ok(L.heads.some((h) => h.text === 'Cloud services'))
})

test('an empty module still lays out', () => {
  const L = layoutIntegrations([], EXP)
  assert.equal(L.systems.length, 0)
  assert.ok(L.height > 0 && L.hub.h > 0)
})
