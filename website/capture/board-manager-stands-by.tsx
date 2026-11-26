/**
 * Capture page: the conductor mode Board's Moves view with a Conductor's standing
 * manager chat idle between jobs (its sidebar title starts with "Manager:"), beside
 * an ordinary worker gone quiet, a running one and a manager whose chat closed. The
 * panel is the real component; only the reads are fixtures, and every text is made up.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../src/api/client'
import { store } from '../src/store'
import { initI18n } from '../src/i18n/all'
import ConductorModeCrewPanel from '../src/pages/chat/command-center/ConductorModeCrewPanel'
import '../src/index.css'

document.documentElement.setAttribute('data-theme', 'dark')

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()
const item = (id: string, title: string, over: Record<string, unknown>) => ({
  schema: 1, item_id: id, title, acceptance: { kind: 'human_approval' }, state: 'open', verdict: null, decision: '', round: 1,
  fails: 0, status: 'progress', summary: '', artifacts: {}, pr: null, last_report_at: ago(60), created_at: ago(300), closed_at: null,
  decided_at: null, orphaned: false, stale: false, acceptance_concrete: true, outstanding: false, terminal: false, alive: 'idle',
  events: [], reason: null, awaits_person: false, has_worker: true, in_backlog: false, ...over,
})
// `?only=manager` keeps only the idle manager, so the run line's pulse reads that seat alone.
const only = new URLSearchParams(location.search).get('only')
const all = [
  item('it_mgr', 'Lantern releases: build, final run and merge for each change, in order', { stale: true, last_report_at: ago(95), summary: 'Step 3 done: the 2.4 notes are posted.' }),
  item('it_quiet', 'Fix the lantern dimmer curve', { stale: true, last_report_at: ago(70), summary: 'Pushed the new curve to the branch.' }),
  item('it_run', 'Teapot timer reads the right minute', { alive: 'running', last_report_at: ago(4), summary: 'Writing the minute test.' }),
  item('it_mgr_closed', 'Kettle builds: one build at a time, in order', { stale: true, alive: 'closed', last_report_at: ago(130), summary: 'Build 7 is green.' }),
]
const items = only === 'manager' ? all.filter(i => i.item_id === 'it_mgr') : all
const worker = (key: string, title: string, ledger_item: string, status: string) => ({
  key, title, agent: 'kirocrew-worker', status, group: 'working', last_activity: ago(5), ledger_item, created_by: 'root', dispatched_by: 'root',
})
const workers = [
  worker('w-mgr', 'Manager: Lantern releases', 'it_mgr', 'idle'),
  worker('w-quiet', 'Lantern dimmer', 'it_quiet', 'idle'),
  worker('w-run', 'Teapot timer', 'it_run', 'working'),
  worker('w-mgr-closed', 'Manager: Kettle builds', 'it_mgr_closed', 'closed'),
]
const conductor = { schema: 1, slot_key: 'root', goal: 'Ship the lantern 2.4 release with the dimmer fix. Done when: 2.4 is tagged.', round: 2, depth: 0, parent_item: null, created_at: ago(400), goal_version: 1, round_started_at: ago(30) }
Object.assign(api, {
  crewWorkers: async () => ({ root: 'root', workers }),
  crewBoard: async () => ({ conductor, conductor_alive: 'idle', items, take_over_available: false, run: { settled: '', open: items.length, accepted: 0, not_done: 0, at_limit: false } }),
  sessionSummary: async () => ({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null }),
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <div style={{ width: 460, height: 900 }} className="bg-bg text-text border border-border" data-testid="capture-frame">
        <ConductorModeCrewPanel root="root" viewing="root" active />
      </div>
    </QueryClientProvider>
  </Provider>,
)
