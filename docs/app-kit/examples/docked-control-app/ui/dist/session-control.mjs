// A notes box docked above the message box that stays open while the person
// reads and clicks the chat (`dock` and `stayOpen`, App Kit seam level 16). Plain
// ESM with no build step: React and the SDK come from the dashboard's import map.
import { createElement as h, useState } from 'react'
import * as sdk from '@kirocrew/app-sdk'

// An older host has no dock: it opens a popover that closes on an outside press.
const DOCKS = typeof sdk.APP_SDK_LEVEL === 'number' && sdk.APP_SDK_LEVEL >= 16

// Each chat's note, kept while the page is open: the box is unmounted when it closes.
const NOTES = new Map()

export default function NotesControl({ session }) {
  const key = session.sessionKey || ''
  const [text, setText] = useState(() => NOTES.get(key) || '')

  // Crew draws the header (this app's name, the label, a close button) above
  // this module, so the box draws no title of its own.
  return h('div', { style: { padding: 12, display: 'flex', flexDirection: 'column', gap: 8, fontSize: 13, color: 'var(--text)' } },
    h('textarea', {
      value: text,
      rows: 6,
      'aria-label': 'Notes for this chat',
      placeholder: 'Notes for this chat',
      onChange: (e) => { setText(e.target.value); NOTES.set(key, e.target.value) },
      style: { width: '100%', resize: 'vertical', font: 'inherit', color: 'inherit', background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 6, padding: 8 },
    }),
    h('div', { style: { color: 'var(--muted)', fontSize: 12 } },
      // One text for both forms: a phone keeps the popover, which closes on an outside click.
      DOCKS ? 'On a wide screen this box stays open while you use the chat.' : 'Closes when you click outside it on this version of Kiro Crew.'),
  )
}
