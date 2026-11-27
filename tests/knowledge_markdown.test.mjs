// node --test tests/knowledge_markdown.test.mjs  (driven from tests/test_knowledge_v2.py)
// The rich-text editor's HTML -> Markdown step (frontend/src/knowledge-markdown.ts; Node strips the types).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { htmlToMarkdown } from '../frontend/src/knowledge-markdown.ts'

test('headings, emphasis, lists and code become python-markdown friendly Markdown', () => {
  const md = htmlToMarkdown('<h2>Two</h2><p><strong>Bold</strong> and <em>it</em> and <code>x()</code></p>' +
    '<ul><li><p>one</p></li><li><p>two</p><ul><li><p>nested</p></li></ul></li></ul>' +
    '<ol><li><p>first</p></li></ol><pre><code class="language-py">print(1)\n</code></pre><blockquote><p>q</p></blockquote><hr>')
  assert.match(md, /^## Two$/m)
  assert.match(md, /\*\*Bold\*\* and \*it\* and `x\(\)`/)
  assert.match(md, /^- one\n- two\n {4}- nested$/m)  // tight, and python-markdown needs 4-space nesting
  assert.match(md, /^1\. first$/m)
  assert.match(md, /```py\nprint\(1\)\n```/)
  assert.match(md, /^> q$/m)
  assert.match(md, /^---$/m)
})

test('a TipTap table (cells wrap <p>) becomes a GFM pipe table with a header row', () => {
  const md = htmlToMarkdown('<table><tbody><tr><th><p>Page</p></th><th><p>Layout</p></th></tr>' +
    '<tr><td><p>Cases</p></td><td><p>list</p><p>then hub</p></td></tr></tbody></table>')
  const lines = md.split('\n')
  assert.match(lines[0], /^\| Page +\| Layout +\|$/)
  assert.match(lines[1], /^\| -+ +\| -+ +\|$/)
  assert.match(lines[2], /^\| Cases +\| list <br> then hub +\|$/)
})

test("TipTap's real table HTML (colgroup before tbody) keeps its header row", () => {
  const md = htmlToMarkdown('<table style="min-width: 75px"><colgroup><col style="min-width: 25px"><col><col></colgroup>' +
    '<tbody><tr><th colspan="1" rowspan="1"><p>Field</p></th><th><p>Max</p></th><th><p>Req</p></th></tr>' +
    '<tr><td><p>Title</p></td><td><p>90</p></td><td><p>yes</p></td></tr></tbody></table>')
  const lines = md.split('\n')
  assert.match(lines[0], /^\| Field \| Max \| Req \|$/)
  assert.match(lines[1], /^\| -+ \| -+ \| -+ \|$/)
  assert.match(lines[2], /^\| Title \| 90 +\| yes \|$/)
  assert.equal(lines.length, 3)
})

test('a list item holding two paragraphs keeps them apart, still indented', () => {
  const md = htmlToMarkdown('<ul><li><p>one</p><p>more about one</p></li><li><p>two</p></li></ul>')
  assert.equal(md, '- one\n\n    more about one\n- two')
})

test('images: own uploads kept (sized ones as raw <img>), anything else dropped', () => {
  assert.equal(htmlToMarkdown('<img src="/media/knowledge/12.png" alt="Flow">'), '![Flow](/media/knowledge/12.png)')
  assert.equal(htmlToMarkdown('<img src="/media/knowledge/12.png" alt="A &quot;b&quot;" width="640" height="360">'),
    '<img src="/media/knowledge/12.png" alt="A &quot;b&quot;" width="640" height="360">')
  assert.equal(htmlToMarkdown('<p>x</p><img src="https://evil.example/a.png"><img src="data:image/png;base64,AAAA">'), 'x')
  assert.equal(htmlToMarkdown('<img src="/media/knowledge/12.png" width="100%">'), '![](/media/knowledge/12.png)')
})

test('links: http(s)/mailto kept, script and relative links reduced to their text', () => {
  assert.equal(htmlToMarkdown('<p><a href="https://example.com/x">site</a></p>'), '[site](https://example.com/x)')
  assert.equal(htmlToMarkdown('<p><a href="javascript:alert(1)">bad</a> <a href="/tasks">rel</a></p>'), 'bad rel')
})

test('an empty editor stores an empty body', () => {
  assert.equal(htmlToMarkdown('<p></p>'), '')
  assert.equal(htmlToMarkdown(''), '')
})
