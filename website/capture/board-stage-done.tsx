/**
 * Capture page: a Conductor's Board with three stories whose first stage is done (the
 * Conductor decided on each done report and kept it open; each stage's chat closed),
 * one truly stuck story, one done report the Conductor has not judged yet and one
 * working story. `?board=all` draws the all-projects page, otherwise the right bar's
 * panel (460 wide beside an empty chat pane from 900px up). The components are the real ones;
 * only the reads are fixtures, and every name is made up.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../src/api/client'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import { initI18n } from '../src/i18n/all'
import ConductorModeCrewPanel from '../src/pages/chat/command-center/ConductorModeCrewPanel'
import { AllProjectsBoard } from '../src/pages/AllProjectsBoardPage'
import '../src/index.css'

document.documentElement.setAttribute('data-theme', 'dark')

const ROOT = 'root-lantern'
const HERE = 'desk-mac'
const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()
const item = (id: string, title: string, over: Record<string, unknown>) => ({
  schema: 1, item_id: id, title, acceptance: { kind: 'human_approval' }, state: 'open', verdict: null, decision: '', round: 1,
  fails: 0, status: 'progress', summary: '', artifacts: {}, pr: null, last_report_at: ago(60), created_at: ago(300), closed_at: null,
  decided_at: null, orphaned: false, stale: false, acceptance_concrete: true, outstanding: false, terminal: false, alive: 'idle',
  events: [], reason: null, awaits_person: false, has_worker: true, in_backlog: false, runs_on: HERE, plan: { level: 'story', step: 'build' }, ...over,
})
// A stage done: reported done, decided on after, kept open, its chat closed.
const stage = (id: string, title: string, minutes: number, over: Record<string, unknown>) =>
  item(id, title, { status: 'done', alive: 'closed', stale: true, last_report_at: ago(minutes), decided_at: ago(minutes - 3), ...over })
const items = [
  stage('it_kettle', 'Kettle firmware: the parts list', 96, { runs_on: 'Lab crew', summary: 'Stage 1 done: the parts are listed and priced.' }),
  stage('it_teapot', 'Teapot timer: the minute test', 82, { runs_on: 'Lab crew', summary: 'Stage 1 done: the timer reads whole minutes.' }),
  stage('it_dimmer', 'Lantern dimmer: the release notes', 64, { summary: 'Stage 1 done: the notes draft is in the branch.' }),
  item('it_wick', 'Fix the lantern wick sensor', { status: 'progress', alive: 'closed', stale: true, last_report_at: ago(71), summary: 'Half the sensor table is read.' }),
  item('it_price', 'Price list for the shop', { status: 'done', alive: 'idle', last_report_at: ago(14), summary: 'The price list is ready to check.', plan: { level: 'story', step: 'review' } }),
  item('it_sign', 'Shop sign for the door', { status: 'progress', alive: 'running', last_report_at: ago(4), summary: 'Drawing the second sign.' }),
]
const worker = (key: string, title: string, ledger_item: string, status: string, over: Record<string, unknown> = {}) => ({
  key, title, agent: 'kirocrew-worker', status, group: status === 'closed' ? 'done' : 'working', last_activity: ago(5), ledger_item,
  created_by: ROOT, dispatched_by: ROOT, ...over,
})
const workers = [
  // On the linked crew and closed there: the crew list names no chat on it to open.
  worker('r-kettle', 'Kettle firmware: the parts list', 'it_kettle', 'closed', { remote_crew: 'Lab crew' }),
  worker('r-teapot', 'Teapot timer: the minute test', 'it_teapot', 'closed', { remote_crew: 'Lab crew' }),
  worker('w-dimmer', 'Lantern dimmer notes', 'it_dimmer', 'closed'),
  worker('w-wick', 'Wick sensor', 'it_wick', 'closed'),
  worker('w-price', 'Price list', 'it_price', 'idle'),
  worker('w-sign', 'Door sign', 'it_sign', 'working'),
]
const conductor = { schema: 1, slot_key: ROOT, goal: 'Open the lantern shop with its first four products. Done when: the shop page is live.', round: 2, depth: 0, parent_item: null, created_at: ago(400), goal_version: 1, round_started_at: ago(30) }
const board = { conductor, conductor_alive: 'idle', items, take_over_available: false, run: { settled: '', open: items.length, accepted: 0, not_done: 0, at_limit: false } }
Object.assign(api, {
  crewWorkers: async () => ({ root: ROOT, workers }),
  crewBoard: async () => board,
  crewBoardCapacity: async () => ({ name: HERE, available_gb: 11.2, posture: 'ample', tight_gb: 4, critical_gb: 2, pressure_held: false }),
  crewBoardTimeline: async (_root: string, hours: number) => ({ read_at: new Date().toISOString(), since: ago(hours * 60), hours, events: {} }),
  sessionSummary: async () => ({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null }),
})
store.dispatch(sseSlots([{ key: ROOT, title: 'Lantern shop', crew_mode_root: true, messages: 3, running: false, last_turn_ts: ago(6) }] as never))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
initI18n('en')
const all = new URLSearchParams(location.search).get('board') === 'all'
// The right bar's own width beside the chat pane on a wide window; the whole width on a phone.
const wide = window.innerWidth >= 900
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/crew-board']}>
        {all ? (
          <div className="crew-board bg-bg text-text p-4 min-h-screen" data-testid="capture-frame"><AllProjectsBoard /></div>
        ) : (
          <div className="flex bg-bg text-text h-screen" data-testid="capture-frame">
            {wide && <div className="flex-1 border-r border-border" aria-hidden="true" />}
            <div className="h-full" style={{ width: wide ? 460 : '100%' }}><ConductorModeCrewPanel root={ROOT} viewing={ROOT} active /></div>
          </div>
        )}
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
