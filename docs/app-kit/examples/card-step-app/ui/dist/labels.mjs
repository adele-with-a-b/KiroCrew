// A label card that steps from label to label with `card.step` (App Kit seam
// level 20). Plain ESM with no build step, and no backend: the card knows only
// the labels it has been shown in this frame (hover them once), in chat order.
// A real app would list the chat's labels from its own backend instead.

const PARENT = /^\[([A-Z][0-9]+)[a-z]\]$/

export function activate(ctx) {
  /** Labels this frame was shown, by text: `{ text, message }`. */
  const seen = new Map()
  let link = null
  let pinned = false
  let note = ''

  const order = () => [...seen.values()].sort((a, b) =>
    a.message.ts < b.message.ts ? -1 : a.message.ts > b.message.ts ? 1 : a.at - b.at)

  const style = document.createElement('style')
  style.textContent = `
    body { margin: 0; font: 13px/1.4 system-ui, sans-serif; }
    .card { box-sizing: border-box; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--line); background: var(--bg); color: var(--fg); }
    .head { display: flex; align-items: baseline; gap: 8px; }
    .label { font: 600 15px ui-monospace, monospace; }
    .where { opacity: .7; }
    .row { display: flex; gap: 6px; margin-top: 8px; }
    button { font: inherit; padding: 3px 8px; border-radius: 6px; border: 1px solid var(--line); background: transparent; color: inherit; cursor: pointer; }
    button[aria-pressed="true"] { background: var(--line); }
    .note { margin-top: 6px; opacity: .75; min-height: 1.4em; }`
  document.head.append(style)
  const root = document.createElement('div')
  root.className = 'card'
  document.body.append(root)

  function render() {
    if (!link) return
    const dark = link.theme === 'dark'
    root.style.setProperty('--bg', dark ? '#1d1f23' : '#ffffff')
    root.style.setProperty('--fg', dark ? '#e8e8ea' : '#1d1f23')
    root.style.setProperty('--line', dark ? '#3a3d44' : '#d5d7dc')
    const list = order()
    const at = list.findIndex((l) => l.text === link.text)
    const parent = PARENT.exec(link.text)
    root.replaceChildren()
    const head = el('div', 'head', el('span', 'label', link.text), el('span', 'where', `${at + 1} of ${list.length} seen`))
    const row = el('div', 'row',
      button('↑', 'Previous label', () => stepBy(-1)),
      button('↓', 'Next label', () => stepBy(1)),
      parent ? button('Go there', `Go to [${parent[1]}]`, () => goTo(`[${parent[1]}]`)) : null,
      button(pinned ? 'Pinned' : 'Pin', 'Keep this card', togglePin, pinned))
    root.append(head, row, el('div', 'note', note))
    link.card.resize({ width: 260, height: root.getBoundingClientRect().height + 2 })
  }

  function el(tag, cls, ...kids) {
    const n = document.createElement(tag)
    n.className = cls
    n.append(...kids.filter(Boolean))
    return n
  }
  function button(text, title, onClick, pressed) {
    const b = document.createElement('button')
    b.type = 'button'
    b.textContent = text
    b.title = title
    if (pressed !== undefined) b.setAttribute('aria-pressed', String(pressed))
    b.addEventListener('click', onClick)
    return b
  }

  function togglePin() {
    pinned = !pinned
    link.card.pin(pinned)
    render()
  }

  // Called from a click or a key in the card: that is what lets the host step.
  async function stepTo(target, list) {
    if (!('step' in link.card)) { note = 'This Crew cannot step a card.'; render(); return }
    if (!pinned) { pinned = true; link.card.pin(true) }
    const n = list.findIndex((l) => l.text === target.text)
    const result = await link.card.step({ message: target.message, text: target.text, line: `${target.text} · ${n + 1} of ${list.length}` })
    note = result.ok ? '' : `Not moved: ${result.reason}`
    render()
  }
  function stepBy(dir) {
    const list = order()
    const at = list.findIndex((l) => l.text === link.text)
    const target = list[at + dir]
    if (!target) { note = dir < 0 ? 'First label seen.' : 'Last label seen.'; render(); return }
    void stepTo(target, list)
  }
  function goTo(text) {
    const target = seen.get(text)
    if (!target) { note = `Hover ${text} once first.`; render(); return }
    void stepTo(target, order())
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      e.preventDefault()
      stepBy(e.key === 'ArrowUp' ? -1 : 1)
    } else if (e.key === 'Home' || e.key === 'End') {
      e.preventDefault()
      const list = order()
      const target = e.key === 'Home' ? list[0] : list[list.length - 1]
      if (target && target.text !== link.text) void stepTo(target, list)
    }
  })

  return {
    show(next) {
      if (!next.text || !next.message) return
      if (!seen.has(next.text)) seen.set(next.text, { text: next.text, message: next.message, at: seen.size })
      // The host keeps a stepped card pinned; so does the card.
      if (next.trigger !== 'step') { pinned = false; note = '' }
      link = next
      render()
      next.card.open()
    },
    leave() {},
    hide() {},
    theme(t) { if (link) { link = { ...link, theme: t }; render() } },
  }
}
