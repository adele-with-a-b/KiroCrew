/**
 * Capture page for "Needs you": the REAL `ChatSidebar` over twenty made-up chats,
 * four of which wait on the person, one of each kind (a labelled question, a tool
 * approval, a question card, an `[OPTIONS:]` ending), as the design's frames draw
 * them. Above it, the tab title the shell would write, from the same selector.
 *
 * Query string: ?theme=dark|light&filter=0|1
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

const nowSec = Date.now() / 1000
const waits = (min: number, words: string, kinds: NeedsYou['kinds'], count = 1): NeedsYou =>
  ({ count, oldest_ts: nowSec - min * 60, first_words: words, row_id: null, kinds })
type Row = LocalSidebarRow & { pinned?: boolean; interrupted?: boolean; needs_you?: NeedsYou | null }
const row = (key: string, title: string, minAgo: number, last: string, over: Partial<Row> = {}): Row =>
  ({ ...localRow({ key, title, last_ts: at(minAgo * MIN), last_message: last }), needs_you: null, ...over })

const rows: Row[] = [
  row('digest', 'Morning digest', 40, 'Digest ready: 14 items, 3 flagged.', { pinned: true }),
  row('train', 'Release train', 1, '3 running', { pinned: true, needs_you: waits(34, 'Q4a Ship 0.13.2 tonight? I would: the fix is in and tested.', ['labelled'], 2) }),
  row('weekly', 'Weekly report', 1, 'Drafting the summary.', { running: true }),
  row('search', 'Search index rebuild', 2, 'Rebuilding shard 3.', { running: true }),
  row('ci', 'CI cache tuning', 3, 'Measuring cold builds.', { running: true }),
  row('trip', 'Trip planner', 4, 'Saved the plan to trip.md.'),
  row('flaky', 'Flaky test sweep', 12, 'Running shard 4.', { needs_you: waits(12, 'Approve Bash · run e2e shard 4 in a test home', ['approval']) }),
  row('schema', 'Schema migration', 5, 'Checking the down step.', { running: true }),
  row('price', 'Price watch', 6, 'No change since 09:00.'),
  row('inbox', 'Inbox triage', 7, 'Sorting 40 threads.', { running: true }),
  row('billing', 'Billing CSV export', 6, 'Export is ready to write.', { needs_you: waits(6, 'Which date format should the export use?', ['question']) }),
  row('notes', 'Release notes draft', 9, 'Draft is in notes/0.13.2.md.'),
  row('deps', 'Dependency audit', 10, 'Waiting · 2 open'),
  row('docs', 'Docs site refresh', 2, 'Drop the old install page, or keep it with a redirect?', { needs_you: waits(2, 'Drop the old install page, or keep it with a redirect?', ['options']) }),
  row('macros', 'Support macros', 12, 'Added 4 macros to the shared list.'),
  row('oldapi', 'Old API cleanup', 14, 'The provider throttled the request.', { interrupted: true }),
  row('fonts', 'Font picker', 15, 'Both fonts load in under 50 ms.'),
  row('logo', 'Logo ideas', 18, 'Here are three more takes on the mark.'),
  row('meeting', 'Meeting notes', 20, 'Notes filed under 2026-11-25.'),
  row('sandbox', 'Sandbox cleanup', 25, 'Removed 3 old test folders.'),
]

const unread = ['digest', 'weekly', 'search', 'trip', 'schema', 'price', 'notes', 'macros', 'fonts']

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

mountLocalSidebar(rows, ['kirocrew'], unread)
