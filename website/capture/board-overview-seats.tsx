/**
 * Capture page: the conductor mode Board's Overview line on a project with worker
 * chats here and on a linked Crew, beside a second project that holds more of this
 * computer's chats. The panel is the real component; only the reads are fixtures,
 * and every name is made up. `?w=390` sets the frame's width (default: the window's).
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../src/api/client'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import { initI18n } from '../src/i18n/all'
import ConductorModeCrewPanel from '../src/pages/chat/command-center/ConductorModeCrewPanel'
import type { ChatSlot } from '../src/types'
import '../src/index.css'

document.documentElement.setAttribute('data-theme', 'dark')

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()
const item = (id: string, title: string, over: Record<string, unknown>) => ({
  schema: 1, item_id: id, title, acceptance: { kind: 'human_approval' }, state: 'open', verdict: null, decision: '', round: 1,
  fails: 0, status: 'progress', summary: '', artifacts: {}, pr: null, last_report_at: ago(20), created_at: ago(300), closed_at: null,
  decided_at: null, orphaned: false, stale: false, acceptance_concrete: true, outstanding: false, terminal: false, alive: 'idle',
  events: [], reason: null, awaits_person: false, has_worker: true, in_backlog: false, runs_on: 'box-here', ...over,
})
const merged = (id: string, title: string) => item(id, title, { state: 'accepted', verdict: 'pass', status: 'done', terminal: true, alive: 'closed', closed_at: ago(90) })
// Lantern: four stories merged of ten; six worker chats, four here and two on demo-crew.
const lantern = [
  merged('it_m1', 'Dimmer curve reads the new table'), merged('it_m2', 'Wick timer survives a restart'),
  merged('it_m3', 'Glass shade picker'), merged('it_m4', 'Battery level on the lid'),
  item('it_l1', 'Night mode fades in over a minute', { alive: 'running', summary: 'Writing the fade test.' }),
  item('it_l2', 'Lantern 2.4 release notes', { summary: 'Draft is up for review.' }),
  item('it_l3', 'Colour presets sync', { alive: 'running', summary: 'Sync works on two lanterns.' }),
  item('it_l4', 'Low-battery warning wording', { summary: 'Three wordings to choose from.' }),
  item('it_l5', 'Firmware build for the 2.4 board', { runs_on: 'demo-crew', alive: 'running', summary: 'Build 3 of 4 is green.' }),
  item('it_l6', 'Long-run soak test', { runs_on: 'demo-crew', summary: 'Soak at 9 hours.' }),
]
// Kettle: ten more chats here and one on demo-crew, so this computer holds 14 across both projects.
const kettle = Array.from({ length: 11 }, (_, n) => item(`it_k${n}`, `Kettle task ${n + 1}`, n === 10 ? { runs_on: 'demo-crew' } : {}))
const conductor = (slot_key: string, goal: string) => ({ schema: 1, slot_key, goal, round: 2, depth: 0, parent_item: null, created_at: ago(400), goal_version: 1, round_started_at: ago(30), concurrency_cap: 32 })
const boards: Record<string, { conductor: unknown; items: unknown[] }> = {
  root: { conductor: conductor('root', 'Ship the lantern 2.4 release. Done when: 2.4 is tagged.'), items: lantern },
  kettle: { conductor: conductor('kettle', 'Kettle builds, one at a time. Done when: build 9 is green.'), items: kettle },
}
const room = new URLSearchParams(location.search).get('room')
Object.assign(api, {
  crewWorkers: async () => ({ root: 'root', workers: [] }),
  crewBoard: async (root: string) => ({ ...boards[root], conductor_alive: 'idle', take_over_available: false, run: { settled: '', open: 6, accepted: 4, not_done: 0, at_limit: false } }),
  sessionSummary: async () => ({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null }),
  crewBoardCapacity: async () => ({
    name: 'box-here', available_gb: room === 'tight' ? 3.1 : 10.3, posture: room === 'tight' ? 'tight' : 'ample', tight_gb: 4, critical_gb: 2, pressure_held: false,
    crews: [{ name: 'demo-crew', room: room === 'tight' ? 'memory' : '' }],
  }),
})
const slot = (key: string, title: string) => ({ key, title, crew_mode_root: true, messages: 1, running: false }) as unknown as ChatSlot
store.dispatch(sseSlots([slot('root', 'Lantern'), slot('kettle', 'Kettle')]))

const width = Number(new URLSearchParams(location.search).get('w')) || window.innerWidth
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <div style={{ width, height: 520 }} className="bg-bg text-text border border-border" data-testid="capture-frame">
        <ConductorModeCrewPanel root="root" viewing="root" active />
      </div>
    </QueryClientProvider>
  </Provider>,
)
