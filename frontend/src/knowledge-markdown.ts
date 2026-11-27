// Rich-text editor HTML -> the Markdown a knowledge body is stored as (app/knowledge_text.py: bodies are
// MARKDOWN, turned into HTML only by the server's python-markdown + nh3 sanitiser). Pure and DOM-free
// apart from turndown's own parser, so it runs in Node for tests/knowledge_markdown.test.mjs.
//
// What python-markdown needs from us:
//  - ATX headings, fenced code, "-" bullets, blank lines between blocks (turndown's defaults + options);
//  - GFM pipe tables with a header row (the gfm plugin; a cell's paragraphs become one line);
//  - images: only the dashboard's own uploads (/media/knowledge/<id>.<ext>). A sized image is kept as a
//    raw <img> (Markdown has no size syntax; the sanitiser allows src/alt/width/height on img); anything
//    else — a pasted remote or data: image — is dropped here rather than stored and then stripped.
import TurndownService from 'turndown'
import turndownPluginGfm from '@joplin/turndown-plugin-gfm'

export const MEDIA_SRC = /^\/media\/knowledge\/[1-9][0-9]{0,9}\.(png|jpg|webp)$/
const SAFE_HREF = /^(https?:|mailto:)/i

function escapeAttr(value: string): string {
  return value.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function dimension(value: string | null): string | null {
  return value && /^[1-9][0-9]{0,3}$/.test(value.trim()) ? value.trim() : null
}

export function createTurndown(): TurndownService {
  const td = new TurndownService({
    headingStyle: 'atx',
    codeBlockStyle: 'fenced',
    bulletListMarker: '-',
    emDelimiter: '*',
    strongDelimiter: '**',
    hr: '---',
  })
  const gfm = (turndownPluginGfm as any).gfm || (turndownPluginGfm as any).default?.gfm
  td.use(gfm)

  td.addRule('kbImage', {
    filter: 'img',
    replacement(_content, node) {
      const el = node as HTMLElement
      const src = (el.getAttribute('src') || '').trim()
      if (!MEDIA_SRC.test(src)) return ''
      const alt = (el.getAttribute('alt') || '').replace(/\s+/g, ' ').trim()
      const width = dimension(el.getAttribute('width'))
      const height = dimension(el.getAttribute('height'))
      if (width || height) {
        return `<img src="${src}" alt="${escapeAttr(alt)}"${width ? ` width="${width}"` : ''}${height ? ` height="${height}"` : ''}>`
      }
      return `![${alt.replace(/([\[\]\\])/g, '\\$1')}](${src})`
    },
  })

  // A link the sanitiser would strip anyway (javascript:, data:, relative) keeps only its text.
  td.addRule('kbLink', {
    filter: (node) => node.nodeName === 'A' && !!node.getAttribute('href'),
    replacement(content, node) {
      const href = ((node as HTMLElement).getAttribute('href') || '').trim()
      if (!SAFE_HREF.test(href)) return content
      const text = content.trim() || href
      return `[${text}](${href.replace(/[()\s]/g, (c) => encodeURIComponent(c))})`
    },
  })

  // Lists: "- item" / "1. item", TIGHT (TipTap wraps every item in <p>; a lone paragraph must not make
  // python-markdown's loose-list <p>s), nested content indented 4 spaces as python-markdown requires.
  td.addRule('kbListParagraph', {
    filter: (node) => node.nodeName === 'P' && !!node.parentNode && node.parentNode.nodeName === 'LI',
    replacement(content, node) {
      const next = (node as HTMLElement).nextElementSibling
      return content + (next ? (next.nodeName === 'P' ? '\n\n' : '\n') : '')
    },
  })
  td.addRule('kbListItem', {
    filter: 'li',
    replacement(content, node, options) {
      const parent = node.parentNode as HTMLElement
      let prefix = `${options.bulletListMarker} `
      if (parent && parent.nodeName === 'OL') {
        const start = Number(parent.getAttribute('start') || '1') || 1
        prefix = `${start + Array.prototype.indexOf.call(parent.children, node)}. `
      }
      const body = content.replace(/^\n+/, '').replace(/\n+$/, '').replace(/\n(?=[^\n])/g, '\n    ')
      return prefix + body + ((node as HTMLElement).nextElementSibling ? '\n' : '')
    },
  })

  // Table cells: TipTap wraps cell text in <p>. Keep one line per cell (a pipe table can't hold more).
  td.addRule('kbCellParagraph', {
    filter: (node) => node.nodeName === 'P' && !!node.parentNode && ['TD', 'TH'].includes(node.parentNode.nodeName),
    replacement(content, node) {
      const next = (node as HTMLElement).nextElementSibling
      return content.replace(/\n+/g, ' ') + (next ? ' <br> ' : '')
    },
  })

  return td
}

let shared: TurndownService | null = null

/** Editor HTML -> stored Markdown. An empty document is ''. */
export function htmlToMarkdown(html: string): string {
  if (!shared) shared = createTurndown()
  // TipTap puts a <colgroup> before <tbody>; the gfm plugin then misses the header row. Widths aren't
  // Markdown anyway.
  const clean = (html || '').replace(/<colgroup>[\s\S]*?<\/colgroup>/gi, '')
  const md = shared.turndown(clean)
  return md.replace(/\n{3,}/g, '\n\n').trim()
}
