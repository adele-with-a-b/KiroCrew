// A notes box docked above the message box that stays open while the person
// reads and clicks the chat (`dock` and `stayOpen`, App Kit seam level 16), and
// that opens itself in another chat on a click (`useSessionControlGo`, level 17).
// Plain ESM with no build step: React and the SDK come from the dashboard's import map.
import { createElement as h, useState } from 'react'
import * as sdk from '@kirocrew/app-sdk'

// Read off the namespace import: an older host has none of these.
const DOCKS = typeof sdk.APP_SDK_LEVEL === 'number' && sdk.APP_SDK_LEVEL >= 16
const useSessionControlGo = sdk.useSessionControlGo
const useSessionControlRequest = sdk.useSessionControlRequest
const useStatusGoBack = sdk.useStatusGoBack

// Each chat's note, kept while the page is open: the box is unmounted when it closes.
const NOTES = new Map()

export default function NotesControl({ session }) {
  const key = session.sessionKey || ''
  const [text, setText] = useState(() => NOTES.get(key) || '')
  const [note, setNote] = useState('')
  // Each hook is there or not for the whole page (the host's exports do not
  // change), so these calls keep the same order on every render.
  const go = useSessionControlGo ? useSessionControlGo() : null
  const req = useSessionControlRequest ? useSessionControlRequest() : null
  const back = useStatusGoBack ? useStatusGoBack() : null
  const others = [...NOTES].filter(([k, v]) => k !== key && v.trim())

  // Crew draws the header (this app's name, the label, a close button) above
  // this module, so the box draws no title of its own.
  return h('div', { style: { padding: 12, display: 'flex', flexDirection: 'column', gap: 8, fontSize: 13, color: 'var(--text)' } },
    req?.via === 'control' && h('div', { role: 'status', style: { display: 'flex', gap: 8, alignItems: 'center' } },
      'Opened from your notes in another chat.',
      back && h('button', { type: 'button', onClick: back }, 'Back'),
    ),
    h('textarea', {
      value: text,
      rows: 6,
      'aria-label': 'Notes for this chat',
      placeholder: 'Notes for this chat',
      onChange: (e) => { setText(e.target.value); NOTES.set(key, e.target.value) },
      style: { width: '100%', resize: 'vertical', font: 'inherit', color: 'inherit', background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 6, padding: 8 },
    }),
    go && others.length > 0 && h('ul', { 'aria-label': 'Notes in other chats', style: { margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 4 } },
      ...others.map(([k, v]) => h('li', { key: k },
        // The click is what lets the box open itself in that chat.
        h('button', {
          type: 'button',
          onClick: () => { const r = go(k, { from: key }); if (!r.ok) setNote(r.reason) },
          style: { textAlign: 'left', width: '100%', background: 'none', border: 'none', padding: 0, color: 'inherit', cursor: 'pointer', textDecoration: 'underline' },
        }, v.split('\n')[0].slice(0, 60)),
      )),
    ),
    note && h('div', { role: 'status', style: { color: 'var(--muted)', fontSize: 12 } }, note),
    h('div', { style: { color: 'var(--muted)', fontSize: 12 } },
      // One text for both forms: a phone keeps the popover, which closes on an outside click.
      DOCKS ? 'On a wide screen this box stays open while you use the chat.' : 'Closes when you click outside it on this version of Kiro Crew.'),
  )
}
