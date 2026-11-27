/**
 * Capture page for "Needs you": the REAL `ChatSidebar` over fifteen made-up chats
 * in five folders, four of them waiting, as the design's C frames draw them:
 * Harbor open with two waits, Lumen open with none, Quill collapsed holding one,
 * Pebble whose only chat waits, Sundial collapsed with none. The folders arrive
 * over the `/api/chat/folders` read the harness answers. Above the sidebar, the
 * tab title the shell would write, from the same selector.
 *
 * Query string: ?theme=dark|light&filter=0|1&answered=0|1&unfiled=0|1
 *   answered  Harbor Conductor's ask is answered (it waits on nobody now)
 *   unfiled   one more waiting chat, in no folder
 */
import { createRoot } from 'react-dom/client'
import { Provider, useSelector } from 'react-redux'
import { MIN, at, localRow, mountLocalSidebar, prepareLocalSidebar, type LocalSidebarRow } from './localSidebarFixture'
import { store, type RootState } from '../src/store'
import { selectChatsWaitingOnYou } from '../src/lib/needsYouCount'
import type { NeedsYou } from '../src/types'

const params = new URLSearchParams(location.search)
prepareLocalSidebar(params)
localStorage.setItem('mc-sidebar-lane', 'tree')
localStorage.setItem('mc-sidebar-width', '320')
localStorage.setItem('mc-session-needs-you-only', params.get('filter') === '1' ? '1' : '0')
const answered = params.get('answered') === '1'

const nowSec = Date.now() / 1000
const waits = (min: number, words: string, kinds: NeedsYou['kinds'], count = 1): NeedsYou =>
  ({ count, oldest_ts: nowSec - min * 60, first_words: words, row_id: null, kinds })
type Row = LocalSidebarRow & { folder_id?: string; needs_you?: NeedsYou | null }
const row = (key: string, title: string, folder: string | undefined, agent: string, minAgo: number, last: string, over: Partial<Row> = {}): Row =>
  ({ ...localRow({ key, title, agent, last_ts: at(minAgo * MIN), last_message: last }), ...(folder ? { folder_id: folder } : {}), needs_you: null, ...over })

const C = 'kirocrew-conductor'
const W = 'kirocrew-worker'
const rows: Row[] = [
  row('h-cond', 'Harbor Conductor', 'f-harbor', C, 1, 'Which tide source wins?', answered ? {} : { needs_you: waits(125, 'Which tide source wins when the two disagree?', ['question']) }),
  row('h-tide', 'Fix tide sync', 'f-harbor', W, 2, 'Running tests…', { running: true }),
  row('h-docs', 'Docs pass', 'f-harbor', W, 3, 'git push origin docs-pass', { needs_you: waits(21, 'git push origin docs-pass', ['approval']) }),
  row('h-retry', 'Retry budget', 'f-harbor', W, 30, 'Done: PR 41 opened, checks green.'),
  row('l-cond', 'Lumen Conductor', 'f-lumen', C, 1, 'Dispatching 2 workers…', { running: true }),
  row('l-shader', 'Shader cache', 'f-lumen', W, 8, 'Cache hit rate up to 92%.'),
  row('l-palette', 'Palette fix', 'f-lumen', W, 40, 'Merged.'),
  row('q-import', 'Import parser', 'f-quill', W, 4, 'Keep the old date format?', { needs_you: waits(48, 'Keep the old date format, or switch to ISO?', ['options'], 2) }),
  row('q-lexer', 'Lexer tests', 'f-quill', W, 50, 'All 212 pass.'),
  row('q-grammar', 'Grammar notes', 'f-quill', W, 70, 'Notes saved.'),
  row('q-bench', 'Bench run', 'f-quill', W, 90, 'Parse time down 14%.'),
  row('p-setup', 'Pebble setup', 'f-pebble', 'kirocrew', 5, 'Q2a Use the shared bucket?', { needs_you: waits(6, 'Q2a Use the shared bucket for uploads?', ['labelled']) }),
  row('s-clock', 'Clock face', 'f-sundial', W, 60, 'Done.'),
  row('s-alarm', 'Alarm sounds', 'f-sundial', W, 80, 'Picked three.'),
  row('s-tz', 'Time zones', 'f-sundial', W, 100, 'Table updated.'),
]
if (params.get('unfiled') === '1') {
  rows.push(row('loose', 'Rename the repo', undefined, 'kirocrew', 3, 'Q7a Rename it to tide-tools?', { needs_you: waits(15, 'Q7a Rename it to tide-tools?', ['labelled']) }))
}

const unread = ['l-shader']

/** The browser tab's title as the shell writes it (`appShell`, the same selector). */
function TabTitle() {
  const n = useSelector((s: RootState) => selectChatsWaitingOnYou(s))
  return (
    <div data-capture-tab="" className="px-3 py-2 text-[12px] text-text-strong bg-bg">
      <span className="inline-block px-3 py-1 rounded-md border border-border bg-bg-elevated">{n > 0 ? `(${n}) ` : ''}Kiro Crew</span>
    </div>
  )
}
createRoot(document.getElementById('tab')!).render(<Provider store={store}><TabTitle /></Provider>)

mountLocalSidebar(rows, ['kirocrew', C, W], unread)
