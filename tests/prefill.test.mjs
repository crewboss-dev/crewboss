// node --test tests/prefill.test.mjs — the ERP's #prefill=<base64url JSON> is UNTRUSTED input.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
const require = createRequire(import.meta.url)
const { decode, sanitize } = require('../app/static/js/prefill.js')

const b64url = (obj) => Buffer.from(JSON.stringify(obj), 'utf8').toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')

test('round-trips a well-formed prefill (UTF-8 included)', () => {
  const raw = { title: 'Ignore out-of-office — replies', description: 'From the ERP', stream: 'tweak', assignee: 'owner',
    subtasks: ['One', 'Two'], links: { workflows: [{ id: 'email.triage.classify', step: 'heuristic-gate' }] },
    suggestion: { conditions: { when: 'auto-reply', then: 'skip' } } }
  const p = sanitize(decode('#prefill=' + b64url(raw)))
  assert.deepEqual(p, { title: 'Ignore out-of-office — replies', description: 'From the ERP', stream: 'tweak', assignee: 'owner',
    subtasks: ['One', 'Two'], workflows: [{ id: 'email.triage.classify', step: 'heuristic-gate' }],
    conditions: '{"when":"auto-reply","then":"skip"}' })
})

test('unknown keys are dropped and bad values ignored', () => {
  const p = sanitize({ title: '  a\n\tb  ', stream: 'rm -rf', assignee: 'claude:bugs', priority: 'must', created_by: 'owner',
    onload: 'x', links: { workflows: [{ id: 'BAD ID' }, { id: 'ok.id', step: '<img>' }, 'x', { id: 'ok.id' }], rules: ['x'] },
    subtasks: ['ok', 5, '', { a: 1 }], suggestion: { conditions: 'x'.repeat(5000) } })
  assert.deepEqual(p, { title: 'a b', workflows: [{ id: 'ok.id' }], subtasks: ['ok'] })
})

test('malformed fragments decode to null, never throw', () => {
  for (const f of ['', '#', '#prefill=', '#prefill=%%%', '#prefill=' + 'A'.repeat(20000), '#prefill=' + b64url([1, 2]),
    '#prefill=' + Buffer.from('not json').toString('base64url'), '#prefill=' + Buffer.from([0xff, 0xfe]).toString('base64url')]) {
    assert.equal(decode(f), null, f.slice(0, 30))
  }
  assert.deepEqual(sanitize(null), {})
})

test('markup survives only as inert text for .value/.textContent', () => {
  const p = sanitize({ title: '<img src=x onerror=alert(1)>', description: '<script>alert(1)</script>' })
  assert.equal(p.title, '<img src=x onerror=alert(1)>')  // set via .value: shown as text, never parsed
  assert.equal(typeof p.description, 'string')
})
