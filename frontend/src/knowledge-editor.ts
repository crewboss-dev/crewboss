// The knowledge editor: WYSIWYG-lite rich text (TipTap) over a plain Markdown <textarea>.
//
// Progressive enhancement: the page ships a `body` textarea holding the Markdown and a <template> holding
// the SAME body as the server rendered it (python-markdown + nh3 — so the editor only ever starts from
// sanitised HTML). This script hides the textarea, mounts the editor on that HTML, and writes Markdown
// back into the textarea (knowledge-markdown.ts) on every change and on submit. No JS = Markdown textarea.
//
// Images are uploaded to the dashboard (POST <data-upload-url>, same origin, re-encoded server-side) and
// inserted by their /media URL — by the toolbar button, by paste or by drop. Nothing else is fetched.
import { Editor } from '@tiptap/core'
import StarterKit from '@tiptap/starter-kit'
import Image from '@tiptap/extension-image'
import { TableKit } from '@tiptap/extension-table'
import { Placeholder } from '@tiptap/extensions'
import { htmlToMarkdown, MEDIA_SRC } from './knowledge-markdown'

type Btn = { key: string; label: string; title: string; icon?: string; run: (e: Editor) => void;
  active?: (e: Editor) => boolean; group: string }

const ICON: Record<string, string> = {
  ul: '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1.2" fill="currentColor"/><circle cx="4.5" cy="12" r="1.2" fill="currentColor"/><circle cx="4.5" cy="18" r="1.2" fill="currentColor"/>',
  ol: '<path d="M10 6h10M10 12h10M10 18h10"/><path d="M4 5l1.5-1v5M3.5 14.5a1.5 1.5 0 1 1 2.5 1.2L3.5 19H6.5" stroke-width="1.6"/>',
  quote: '<path d="M7 7h4v4c0 3-1.5 5-4 6M15 7h4v4c0 3-1.5 5-4 6"/>',
  code: '<path d="M9 8l-4 4 4 4M15 8l4 4-4 4"/>',
  block: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M8 10l-2 2 2 2M16 10l2 2-2 2"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  table: '<rect x="3.5" y="4.5" width="17" height="15" rx="1.5"/><path d="M3.5 9.5h17M3.5 14.5h17M9.5 4.5v15M15 4.5v15"/>',
  image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M20.5 16l-5-5-8 8.5"/>',
  hr: '<path d="M4 12h16"/>',
}

function svg(name: string): string {
  return `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name]}</svg>`
}

function toast(message: string) {
  const w = window as any
  if (typeof w.mddToast === 'function') w.mddToast(message, 'error')
  else window.alert(message)
}

const BUTTONS: Btn[] = [
  { key: 'bold', group: 'text', label: 'B', title: 'Bold (Ctrl+B)', run: (e) => e.chain().focus().toggleBold().run(), active: (e) => e.isActive('bold') },
  { key: 'italic', group: 'text', label: 'I', title: 'Italic (Ctrl+I)', run: (e) => e.chain().focus().toggleItalic().run(), active: (e) => e.isActive('italic') },
  { key: 'code', group: 'text', label: '', icon: 'code', title: 'Inline code', run: (e) => e.chain().focus().toggleCode().run(), active: (e) => e.isActive('code') },
  { key: 'h2', group: 'head', label: 'H2', title: 'Heading', run: (e) => e.chain().focus().toggleHeading({ level: 2 }).run(), active: (e) => e.isActive('heading', { level: 2 }) },
  { key: 'h3', group: 'head', label: 'H3', title: 'Subheading', run: (e) => e.chain().focus().toggleHeading({ level: 3 }).run(), active: (e) => e.isActive('heading', { level: 3 }) },
  { key: 'h4', group: 'head', label: 'H4', title: 'Small heading', run: (e) => e.chain().focus().toggleHeading({ level: 4 }).run(), active: (e) => e.isActive('heading', { level: 4 }) },
  { key: 'ul', group: 'block', label: '', icon: 'ul', title: 'Bullet list', run: (e) => e.chain().focus().toggleBulletList().run(), active: (e) => e.isActive('bulletList') },
  { key: 'ol', group: 'block', label: '', icon: 'ol', title: 'Numbered list', run: (e) => e.chain().focus().toggleOrderedList().run(), active: (e) => e.isActive('orderedList') },
  { key: 'quote', group: 'block', label: '', icon: 'quote', title: 'Quote', run: (e) => e.chain().focus().toggleBlockquote().run(), active: (e) => e.isActive('blockquote') },
  { key: 'codeblock', group: 'block', label: '', icon: 'block', title: 'Code block', run: (e) => e.chain().focus().toggleCodeBlock().run(), active: (e) => e.isActive('codeBlock') },
  { key: 'link', group: 'insert', label: '', icon: 'link', title: 'Link', run: () => {}, active: (e) => e.isActive('link') },
  { key: 'table', group: 'insert', label: '', icon: 'table', title: 'Table', run: (e) => e.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run(), active: (e) => e.isActive('table') },
  { key: 'image', group: 'insert', label: '', icon: 'image', title: 'Image (or paste / drop one)', run: () => {} },
  { key: 'hr', group: 'insert', label: '', icon: 'hr', title: 'Divider', run: (e) => e.chain().focus().setHorizontalRule().run() },
]

const TABLE_ACTIONS: { label: string; run: (e: Editor) => void }[] = [
  { label: '+ Row', run: (e) => e.chain().focus().addRowAfter().run() },
  { label: '+ Column', run: (e) => e.chain().focus().addColumnAfter().run() },
  { label: '− Row', run: (e) => e.chain().focus().deleteRow().run() },
  { label: '− Column', run: (e) => e.chain().focus().deleteColumn().run() },
  { label: 'Delete table', run: (e) => e.chain().focus().deleteTable().run() },
]

function button(cls: string, html: string, title: string): HTMLButtonElement {
  const b = document.createElement('button')
  b.type = 'button'
  b.className = cls
  b.innerHTML = html
  b.title = title
  b.setAttribute('aria-label', title)
  return b
}

function mount(root: HTMLElement) {
  const textarea = root.querySelector<HTMLTextAreaElement>('textarea[name="body"]')
  const tpl = root.querySelector<HTMLTemplateElement>('template[data-kb-html]')
  if (!textarea || !tpl || root.dataset.mounted) return
  root.dataset.mounted = '1'
  const uploadUrl = root.dataset.uploadUrl || '/knowledge/images'
  const form = root.closest('form')

  // --- chrome: toolbar, contextual bar (link / table / image), the editing surface, a hidden file input ---
  const bar = document.createElement('div')
  bar.className = 'kb-rt-bar'
  bar.setAttribute('role', 'toolbar')
  bar.setAttribute('aria-label', 'Formatting')
  const ctx = document.createElement('div')
  ctx.className = 'kb-rt-ctx'
  ctx.hidden = true
  const surface = document.createElement('div')
  surface.className = 'kb-rt-surface'
  const file = document.createElement('input')
  file.type = 'file'
  file.accept = 'image/png,image/jpeg,image/webp,image/gif'
  file.hidden = true
  root.insertBefore(bar, textarea)
  root.insertBefore(ctx, textarea)
  root.insertBefore(surface, textarea)
  root.appendChild(file)
  textarea.hidden = true
  root.classList.add('kb-rt-ready')

  const KbImage = Image.configure({
    inline: false,
    allowBase64: false,
    resize: { enabled: true, directions: ['bottom-right', 'bottom-left'], minWidth: 48, minHeight: 32, alwaysPreserveAspectRatio: true },
  })

  let dirty = false
  let uploading = 0
  const editor: Editor = new Editor({
    element: surface,
    extensions: [
      StarterKit.configure({
        heading: { levels: [1, 2, 3, 4] },
        underline: false,
        link: {
          openOnClick: false,
          autolink: true,
          defaultProtocol: 'https',
          HTMLAttributes: { rel: 'noopener noreferrer nofollow', target: null },
          isAllowedUri: (url: string) => /^(https?:|mailto:)/i.test(url),
        },
      }),
      KbImage,
      TableKit.configure({ table: { resizable: false } }),
      Placeholder.configure({ placeholder: 'Write the detail. Paste or drop screenshots straight in.' }),
    ],
    content: tpl.innerHTML,
    editorProps: {
      attributes: { class: 'kb-prose kb-rt-content', 'aria-label': 'Detail', 'aria-multiline': 'true', role: 'textbox' },
      handlePaste: (_view, event) => {
        const files = Array.from(event.clipboardData?.files || []).filter((f) => f.type.startsWith('image/'))
        if (!files.length) return false
        event.preventDefault()
        files.forEach((f) => upload(f))
        return true
      },
      handleDrop: (view, event) => {
        const files = Array.from((event as DragEvent).dataTransfer?.files || []).filter((f) => f.type.startsWith('image/'))
        if (!files.length) return false
        event.preventDefault()
        const at = view.posAtCoords({ left: event.clientX, top: event.clientY })
        files.forEach((f) => upload(f, at?.pos))
        return true
      },
    },
    onUpdate: () => { dirty = true; sync() },
    onSelectionUpdate: () => refresh(),
    onTransaction: () => refresh(),
  })

  function sync() {
    textarea!.value = htmlToMarkdown(editor.getHTML())
  }

  async function upload(f: File, pos?: number) {
    if (!/^image\/(png|jpeg|webp|gif)$/.test(f.type)) { toast('Images must be PNG, JPEG, WebP or GIF.'); return }
    uploading++
    root.classList.add('kb-rt-uploading')
    try {
      const body = new FormData()
      body.append('file', f)
      const res = await fetch(uploadUrl, { method: 'POST', body, credentials: 'same-origin' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok || !MEDIA_SRC.test(String(data.url || ''))) { toast(data.detail || 'That image didn’t upload — try again.'); return }
      const alt = f.name.replace(/\.[a-z0-9]+$/i, '').replace(/[-_]+/g, ' ').trim().slice(0, 120)
      const attrs = { src: data.url, alt, width: data.width, height: data.height }
      const chain = editor.chain().focus()
      if (typeof pos === 'number') chain.insertContentAt(pos, { type: 'image', attrs }).run()
      else chain.setImage(attrs as any).run()
    } catch {
      toast('That image didn’t upload — check the connection and try again.')
    } finally {
      uploading--
      if (!uploading) root.classList.remove('kb-rt-uploading')
    }
  }

  // --- toolbar ------------------------------------------------------------------------------------------
  const btns = new Map<string, HTMLButtonElement>()
  let group = ''
  for (const b of BUTTONS) {
    if (group && b.group !== group) {
      const sep = document.createElement('span')
      sep.className = 'kb-rt-sep'
      sep.setAttribute('aria-hidden', 'true')
      bar.appendChild(sep)
    }
    group = b.group
    const el = button(`kb-rt-btn kb-rt-${b.key}`, b.icon ? svg(b.icon) : `<span>${b.label}</span>`, b.title)
    el.addEventListener('mousedown', (ev) => ev.preventDefault()) // keep the editor's selection
    el.addEventListener('click', () => {
      if (b.key === 'link') return openLink()
      if (b.key === 'image') return file.click()
      b.run(editor)
    })
    bar.appendChild(el)
    btns.set(b.key, el)
  }
  file.addEventListener('change', () => {
    Array.from(file.files || []).forEach((f) => upload(f))
    file.value = ''
  })

  // --- contextual bar: link editing, table actions, image alt text --------------------------------------
  let mode: '' | 'link' | 'table' | 'image' = ''
  function openLink() {
    mode = 'link'
    renderCtx()
    const input = ctx.querySelector<HTMLInputElement>('input')
    input?.focus()
    input?.select()
  }
  function renderCtx() {
    ctx.innerHTML = ''
    ctx.hidden = !mode
    if (mode === 'link') {
      const input = document.createElement('input')
      input.type = 'url'
      input.className = 'field text-small kb-rt-input'
      input.placeholder = 'https://…'
      input.setAttribute('aria-label', 'Link address')
      input.value = editor.getAttributes('link').href || ''
      const apply = button('btn-primary text-small', 'Apply', 'Apply link')
      const remove = button('btn-ghost text-small', 'Remove', 'Remove link')
      const close = () => { mode = ''; renderCtx(); editor.commands.focus() }
      const doApply = () => {
        const href = input.value.trim()
        if (!href) { editor.chain().focus().extendMarkRange('link').unsetLink().run(); return close() }
        const url = /^[a-z]+:/i.test(href) ? href : `https://${href}`
        if (!/^(https?:|mailto:)/i.test(url)) { toast('Links must start with https://, http:// or mailto:'); return }
        if (editor.state.selection.empty && !editor.isActive('link')) {
          editor.chain().focus().insertContent({ type: 'text', text: href, marks: [{ type: 'link', attrs: { href: url } }] }).run()
        } else {
          editor.chain().focus().extendMarkRange('link').setLink({ href: url }).run()
        }
        close()
      }
      input.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') { ev.preventDefault(); doApply() }
        if (ev.key === 'Escape') { ev.preventDefault(); close() }
      })
      apply.addEventListener('click', doApply)
      remove.addEventListener('click', () => { editor.chain().focus().extendMarkRange('link').unsetLink().run(); close() })
      ctx.append(input, apply, remove)
    } else if (mode === 'table') {
      const label = document.createElement('span')
      label.className = 'kb-rt-ctx-label'
      label.textContent = 'Table'
      ctx.appendChild(label)
      for (const a of TABLE_ACTIONS) {
        const b = button('kb-rt-chip', a.label, a.label)
        b.addEventListener('mousedown', (ev) => ev.preventDefault())
        b.addEventListener('click', () => a.run(editor))
        ctx.appendChild(b)
      }
    } else if (mode === 'image') {
      const input = document.createElement('input')
      input.className = 'field text-small kb-rt-input'
      input.placeholder = 'Describe the image (alt text)'
      input.setAttribute('aria-label', 'Image description')
      input.maxLength = 200
      input.value = editor.getAttributes('image').alt || ''
      input.addEventListener('input', () => editor.chain().updateAttributes('image', { alt: input.value }).run())
      const del = button('kb-rt-chip', 'Remove image', 'Remove image')
      del.addEventListener('click', () => editor.chain().focus().deleteSelection().run())
      ctx.append(input, del)
    }
  }

  function refresh() {
    for (const b of BUTTONS) {
      const el = btns.get(b.key)!
      const on = b.active ? b.active(editor) : false
      el.classList.toggle('kb-rt-on', on)
      if (b.active) el.setAttribute('aria-pressed', on ? 'true' : 'false')
    }
    if (mode === 'link') return
    const next = editor.isActive('image') ? 'image' : editor.isActive('table') ? 'table' : ''
    if (next !== mode) { mode = next; renderCtx() }
  }
  refresh()
  sync()
  dirty = false

  // --- save -----------------------------------------------------------------------------------------------
  form?.addEventListener('submit', (ev) => {
    if (uploading) { ev.preventDefault(); toast('Wait for the image to finish uploading.'); return }
    sync()
    dirty = false
  })
  window.addEventListener('beforeunload', (ev) => {
    if (dirty) { ev.preventDefault(); ev.returnValue = '' }
  })
}

function init() {
  document.querySelectorAll<HTMLElement>('[data-kb-editor]').forEach(mount)
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init)
else init()
