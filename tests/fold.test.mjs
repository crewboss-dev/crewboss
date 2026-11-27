// node --test tests/fold.test.mjs — the pure logic of the fold-away helper (app/static/js/fold.js, task #582):
// which keys leave / arrive / stay when a view is re-rendered, and where focus lands when an item leaves.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
const require = createRequire(import.meta.url)
const fold = require('../app/static/js/fold.js')

test('Done on a Now row: it leaves Now and arrives in Done today, the rest stay', () => {
  const d = fold.diffKeys(['now:1', 'now:2', 'up:3', 'done:9'], ['now:2', 'up:3', 'done:1', 'done:9'])
  assert.deepEqual([...d.leaving], ['now:1'])
  assert.deepEqual([...d.entering], ['done:1'])
  assert.deepEqual([...d.staying].sort(), ['done:9', 'now:2', 'up:3'])
})

test('a re-render with the same items changes nothing (a reorder or a count tick folds nothing)', () => {
  const d = fold.diffKeys(['a', 'b', 'c'], ['c', 'b', 'a'])
  assert.equal(d.leaving.size, 0)
  assert.equal(d.entering.size, 0)
})

test('an emptied view: everything leaves; a first render: everything enters', () => {
  assert.deepEqual([...fold.diffKeys(['a', 'b'], []).leaving], ['a', 'b'])
  assert.deepEqual([...fold.diffKeys([], ['a', 'b']).entering], ['a', 'b'])
})

test('focus goes to the next item that stays', () => {
  assert.equal(fold.pickFocusKey(['a', 'b', 'c'], 0, new Set(['a'])), 'b')
  assert.equal(fold.pickFocusKey(['a', 'b', 'c'], 0, new Set(['a', 'b'])), 'c')
})

test('the last item leaving lands on the previous one', () => {
  assert.equal(fold.pickFocusKey(['a', 'b', 'c'], 2, new Set(['c'])), 'b')
  assert.equal(fold.pickFocusKey(['a', 'b', 'c'], 2, new Set(['b', 'c'])), 'a')
})

test('nothing left to focus: null (the caller lands on the section heading, not the top of the page)', () => {
  assert.equal(fold.pickFocusKey(['a'], 0, new Set(['a'])), null)
  assert.equal(fold.pickFocusKey([], 0, new Set()), null)
})

// Two rows, each holding its own buttons (a nested item). Acknowledge one: its buttons fold away.
const fakeSection = { contains: () => true }
const row = (id) => ({ id, live: true })
const btn = (parent) => ({ live: true, parent })
const opts = { live: (n) => n.live, parentItem: (n) => n.parent || null }

test('the count is rows, not the buttons nested inside them', () => {
  const a = row('dec:1'), b = row('dec:2')
  const ca = btn(a), cb = btn(b)
  assert.equal(fold.countTopLevel([a, ca, b, cb], fakeSection, opts), 2)   // not 4
  ca.live = false                                                          // acknowledge row 1: its buttons fold away
  assert.equal(fold.countTopLevel([a, ca, b, cb], fakeSection, opts), 2)   // 2, not 3
  b.live = false
  assert.equal(fold.countTopLevel([a, ca, b, cb], fakeSection, opts), 1)
})

test('an item whose parent item lies outside the section still counts', () => {
  const outer = { live: true }, inner = { live: true, parent: outer }
  const section = { contains: (n) => n !== outer }
  assert.equal(fold.countTopLevel([inner], section, opts), 1)
})
