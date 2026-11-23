// A session control that shows a file path and opens it on a click, through
// `useOpenFile` (App Kit seam level 12). Plain ESM with no build step: React and
// the SDK come from the dashboard's import map.
import { createElement as h, useState } from 'react'
import * as sdk from '@kirocrew/app-sdk'

// Read off the namespace import: an older host has no `useOpenFile`.
const useOpenFile = sdk.useOpenFile

export default function OpenPathControl({ session }) {
  const openFile = useOpenFile ? useOpenFile() : null
  const [path, setPath] = useState(session.cwd ? `${session.cwd.replace(/\/+$/, '')}/README.md` : '')
  const [note, setNote] = useState('')

  async function open() {
    // Called from the click handler: the click is what lets the host open one file.
    const result = await openFile(path)
    setNote(result.ok ? 'Opened.' : (result.message || result.reason))
  }

  return h('div', { style: { padding: 12, display: 'flex', flexDirection: 'column', gap: 8, fontSize: 12 } },
    h('label', { style: { display: 'flex', flexDirection: 'column', gap: 4 } },
      'Path',
      h('input', {
        value: path,
        onChange: (e) => { setPath(e.target.value); setNote('') },
        'aria-label': 'Path',
        style: { fontFamily: 'monospace', fontSize: 12 },
      }),
    ),
    openFile
      ? h('button', {
          type: 'button',
          onClick: open,
          'data-testid': 'open-path',
          title: 'Open in its default app',
          style: { textAlign: 'left', fontFamily: 'monospace', fontSize: 12, textDecoration: 'underline', cursor: 'pointer', background: 'none', border: 'none', padding: 0, color: 'inherit', wordBreak: 'break-all' },
        }, path)
      : h('code', null, path),
    note && h('div', { 'data-testid': 'open-note', role: 'status' }, note),
  )
}
