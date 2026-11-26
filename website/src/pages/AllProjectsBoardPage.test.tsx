/**
 * The all-projects Board page: every Conductor's project in the table, its stories
 * in the move columns with the 10-minute rule said in the head, the capacity line
 * from real counts, the Timeline view pointing at each project's own Board, and
 * `?conductor=` still serving that one board. Each project reads as its right bar
 * does: a carded approval in Needs you, the same Needs you count, Paused, a row that
 * opens its worker, and the one poll rule. The api client is mocked at the module
 * boundary; the clock is pinned.
 */
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { createTestStore } from '../test/helpers'
import { newQueryClient } from '../api/queryClient'
import { sseSlots } from '../store/dashboardSlice'
import { CREW_BOARD_POLL_MS, type WorkBoardItem, type WorkBoardResponse } from '../api/crewBoard'
import type { CrewBoardCapacity } from '../api/crewBoardCapacity'
import { pollGapMs, resetPollRule } from '../lib/pollRule'
import { closeCrewWindow, currentCrewWindow } from './chat/crew-window/crewWindowStore'

const crewBoard = vi.fn()
const crewBoardCapacity = vi.fn()
const crewWorkers = vi.fn()
const openCrewSession = vi.hoisted(() => vi.fn())
const crewMode = vi.hoisted(() => ({ on: false }))

vi.mock('../api/client', () => ({
  api: {
    crewBoard: (...args: unknown[]) => crewBoard(...args),
    crewBoardCapacity: (...args: unknown[]) => crewBoardCapacity(...args),
    crewWorkers: (...args: unknown[]) => crewWorkers(...args),
  },
}))
vi.mock('./chat/command-center/CrewWorkerList', async importOriginal => ({
  ...(await importOriginal<typeof import('./chat/command-center/CrewWorkerList')>()),
  openCrewSession,
}))
vi.mock('../hooks/usePreviewFlag', async importOriginal => ({
  ...(await importOriginal<typeof import('../hooks/usePreviewFlag')>()),
  useCrewMode: () => crewMode.on,
}))

const NOW = Date.parse('2026-11-25T12:00:00Z')
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString()

function item(id: string, over: Partial<WorkBoardItem> = {}): WorkBoardItem {
  return {
    schema: 1, item_id: id, title: id, acceptance: {}, state: 'open', verdict: null, decision: '', round: 0, fails: 0,
    status: 'progress', summary: 'moving along', artifacts: {}, pr: null, last_report_at: ago(5), created_at: ago(60),
    closed_at: null, decided_at: null, orphaned: false, stale: false, acceptance_concrete: true, outstanding: false,
    terminal: false, alive: 'running', events: [], has_worker: true, in_backlog: false, runs_on: 'box-here',
    ...over,
  }
}

function board(items: WorkBoardItem[]): WorkBoardResponse {
  return {
    conductor: { schema: 1, slot_key: 'k', goal: 'ship it', round: 1, depth: 0, parent_item: null, created_at: ago(120) },
    conductor_alive: 'idle', items, take_over_available: false,
  }
}

const BOARDS: Record<string, WorkBoardResponse> = {
  'root-a': board([
    item('Late review', { status: 'done', alive: 'idle', last_report_at: ago(12) }),
    item('Fresh review', { status: 'done', alive: 'idle', last_report_at: ago(3) }),
    item('Building it', { runs_on: 'linked-crew' }),
    item('Next pick', { has_worker: false, in_backlog: true, alive: 'closed', status: null }),
  ]),
  'root-b': board([item('Asks a thing', { status: 'question', reason: 'needs_human', awaits_person: true, outstanding: true, alive: 'idle', summary: 'Keep the old range?' })]),
}

const SLOTS = [
  { key: 'root-a', title: 'Alpha', crew_mode_root: true, last_turn_ts: ago(4), messages: 1, running: false },
  { key: 'root-b', title: 'Beta', crew_mode_root: true, messages: 1, running: false },
  { key: 'root-c', title: 'Gamma', crew_mode_root: true, messages: 1, running: false },
]

const CAPACITY: CrewBoardCapacity = { name: 'box-here', available_gb: 10.3, posture: 'ample', tight_gb: 4, critical_gb: 2, pressure_held: false }

/** Where the router is, for a click that leaves the page. */
function Where() {
  const at = useLocation()
  return <p data-testid="where">{`${at.pathname}${at.search}`}</p>
}

async function mount(path = '/crew-board', slots: unknown[] = SLOTS, qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const { default: AllProjectsBoardPage } = await import('./AllProjectsBoardPage')
  const store = createTestStore({ dashboard: { slots } } as never)
  const view = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/crew-board" element={<AllProjectsBoardPage />} />
            <Route path="*" element={<Where />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
  return { ...view, store, qc }
}

const project =(root: string) => screen.getAllByTestId('board-project').find(r => r.getAttribute('data-root') === root)!

describe('AllProjectsBoardPage', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
    crewBoard.mockImplementation((root: string) => (BOARDS[root]
      ? Promise.resolve(BOARDS[root])
      : Promise.reject(Object.assign(new Error('no ledger'), { status: 404 }))))
    crewBoardCapacity.mockResolvedValue(CAPACITY)
    crewWorkers.mockImplementation((root: string) => Promise.resolve({ root, workers: [] }))
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    crewBoard.mockReset()
    crewBoardCapacity.mockReset()
    crewWorkers.mockReset()
    openCrewSession.mockReset()
    crewMode.on = false
    closeCrewWindow()
    resetPollRule()
  })

  it('lists every Conductor project with its counts, the most to act on first', async () => {
    await mount()
    // Both boards answered: Alpha has a late story, Beta asks the person.
    await waitFor(() => expect(screen.getAllByTestId('board-project-name').map(n => n.textContent)).toEqual(['Alpha', 'Beta', 'Gamma']))
    await waitFor(() => expect(screen.getAllByTestId('board-project-needs-you').map(n => n.textContent)).toEqual(['0', '1', '0']))
    const alpha = project('root-a')
    expect(within(alpha).getByTestId('board-project-late')).toHaveTextContent('1')
    expect(within(alpha).getByTestId('board-project-merged')).toHaveTextContent('0 of 4')
    const gamma = project('root-c')
    expect(within(gamma).getByTestId('board-project-conductor')).toHaveTextContent('No stories yet')
    expect(gamma).toHaveAttribute('data-quiet', 'true')
  })

  it('draws each story under its move and says the 10-minute rule in the head', async () => {
    await mount()
    const conductor = await screen.findByTestId('all-projects-section-conductor')
    expect(within(conductor).getByTestId('board-section-hint')).toHaveTextContent(/10/)
    const lateRow = await within(conductor).findByText('Late review')
    expect(lateRow.closest('[data-testid="board-story"]')).toHaveAttribute('data-tone', 'danger')
    const fresh = within(conductor).getByText('Fresh review').closest('[data-testid="board-story"]')
    expect(fresh).not.toHaveAttribute('data-tone')
    const needs = screen.getByTestId('all-projects-section-needs_you')
    expect(await within(needs).findByText('Asks a thing')).toBeTruthy()
    expect(within(needs).getByText('Asks: Keep the old range?')).toBeTruthy()
    expect(within(screen.getByTestId('all-projects-section-up-next')).getByText('Next pick')).toBeTruthy()
  })

  it("puts a story whose worker waits on a carded approval under Needs you, as its card, as the right bar does", async () => {
    crewBoard.mockImplementation((root: string) => Promise.resolve(root === 'root-a'
      ? board([item('Needs a tool'), item('Building it')])
      : board([])))
    crewWorkers.mockImplementation((root: string) => Promise.resolve({
      root,
      workers: root === 'root-a' ? [{ key: 'w-tool', title: 'Tool worker', agent: 'kirocrew-worker', status: 'needs_approval', group: 'working', last_activity: '', ledger_item: 'Needs a tool', created_by: root }] : [],
    }))
    const worker = { key: 'w-tool', crew_root: 'root-a', pending_approval: true, pending_approval_info: { origin: 'native', tool: 'shell', request_id: 'req-1' }, messages: 1 }
    await mount('/crew-board', [SLOTS[0], worker])
    const needs = await screen.findByTestId('all-projects-section-needs_you')
    // The card stands for the story: one row, the approval's, counted once.
    await waitFor(() => expect(within(needs).getByTestId('all-projects-ask-row')).toHaveTextContent('1 tool approval waits'))
    expect(needs).toHaveAttribute('data-count', '1')
    expect(within(screen.getByTestId('all-projects-section-working')).queryByText('Needs a tool')).toBeNull()
    expect(within(needs).queryByText('Needs a tool')).toBeNull()
    await waitFor(() => expect(within(project('root-a')).getByTestId('board-project-needs-you')).toHaveTextContent('1'))
    expect(within(project('root-a')).getByTestId('board-project-working')).toHaveTextContent('1')
    expect(within(project('root-a')).getByTestId('board-project-conductor')).toHaveTextContent('Waiting for you')
  })

  it("counts Needs you as the right bar does: not the Conductor's own flags, not an approval with no card", async () => {
    const slots = [
      { ...SLOTS[1], needs_input: true, pending_approval: true },
      { key: 'w-b', crew_root: 'root-b', pending_approval: true, messages: 1 },
    ]
    await mount('/crew-board', slots)
    // Beta's one question story, and nothing else.
    await waitFor(() => expect(within(project('root-b')).getByTestId('board-project-needs-you')).toHaveTextContent('1'))
    expect(screen.queryByTestId('all-projects-ask')).toBeNull()
  })

  it('says Paused for a paused project, as its right bar does', async () => {
    crewMode.on = true
    crewWorkers.mockImplementation((root: string) => Promise.resolve(root === 'root-a'
      ? { root, workers: [], paused: { stopped: [], paused_at: ago(2) } }
      : { root, workers: [] }))
    await mount()
    await waitFor(() => expect(within(project('root-a')).getByTestId('board-project-conductor')).toHaveTextContent('Paused'))
    expect(within(project('root-b')).getByTestId('board-project-conductor')).not.toHaveTextContent('Paused')
  })

  it("opens a story's worker, never the Conductor's chat", async () => {
    crewWorkers.mockImplementation((root: string) => Promise.resolve(root === 'root-a' ? {
      root,
      workers: [
        { key: 'w-build', title: 'Builder', agent: 'kirocrew-worker', status: 'working', group: 'working', last_activity: '', ledger_item: 'Building it', created_by: root },
        { key: 'r-1', title: 'Reviewer', agent: '', status: 'idle', group: 'done', last_activity: '', ledger_item: 'Fresh review', created_by: root, remote_crew: 'linked-crew', remote_instance: 'inst-1', remote_slot: 'peer-slot' },
      ],
    } : { root, workers: [] }))
    await mount()
    const working = await screen.findByTestId('all-projects-section-working')
    const user = userEvent.setup({ advanceTimers: () => undefined })
    await user.click(await within(working).findByRole('button', { name: 'Building it' }))
    expect(openCrewSession).toHaveBeenCalledWith(expect.any(Function), 'w-build', expect.any(Boolean))
    expect(screen.getByTestId('where')).toHaveTextContent(/^\/chat$/)
  })

  /** One project, mounted with the board read and the slot row given; then every board
   *  re-read is held back, so a slot push moves only the live slot row. Its crew list
   *  binds `w-tool` to "Needs a tool" (`bound`), binds only the other worker because it
   *  was read before `w-tool` was bound (`late`), never answers (`loading`) or fails
   *  (`failed`). `flagged` stories carry a stray `approval_waiting`, which no board read
   *  sends and no Board may read. Each test takes its own root: a push re-read another
   *  test left pending for its root would swallow this one's. */
  async function mountHeld(root: string, { asks, list = 'bound', flagged = [] }: { asks: boolean; list?: 'bound' | 'late' | 'loading' | 'failed'; flagged?: string[] }) {
    const story = (title: string) => (flagged.includes(title) ? { ...item(title), approval_waiting: true } as WorkBoardItem : item(title))
    crewBoard.mockResolvedValue(board([story('Needs a tool'), ...(flagged.includes('Old ask') ? [story('Old ask')] : []), story('Building it')]))
    const bound = (key: string, ledger: string) => ({ key, title: key, agent: 'kirocrew-worker', status: 'working', group: 'working', last_activity: '', ledger_item: ledger, created_by: root })
    if (list === 'bound') crewWorkers.mockResolvedValue({ root, workers: [bound('w-tool', 'Needs a tool'), bound('w-build', 'Building it')] })
    else if (list === 'late') crewWorkers.mockResolvedValue({ root, workers: [bound('w-build', 'Building it')] })
    else if (list === 'loading') crewWorkers.mockImplementation(() => new Promise(() => {}))
    else crewWorkers.mockRejectedValue(Object.assign(new Error('crew list down'), { status: 500 }))
    const conductor = { key: root, title: 'Pusher', crew_mode_root: true, messages: 1, running: false }
    const idle = { key: 'w-tool', title: 'Tool worker', crew_root: root, running: true, pending_approval: false, messages: 1 }
    const asking = { ...idle, pending_approval: true, pending_approval_info: { origin: 'native', tool: 'shell', request_id: 'req-1' } }
    const { store, qc } = await mount('/crew-board', [conductor, asks ? asking : idle])
    if (list === 'bound' || list === 'late') {
      // The board and the crew list are in once the other story's row opens its worker.
      expect(await within(screen.getByTestId('all-projects-section-working')).findByRole('button', { name: 'Building it' })).toBeTruthy()
    } else {
      await waitFor(() => expect(within(project(root)).getByTestId('board-project-conductor')).not.toBeEmptyDOMElement())
      await waitFor(() => expect(crewWorkers).toHaveBeenCalled())
      // Imported here, not at the top: a static import would load the mocked module
      // before the page and leave the page's opener on the unmocked `openCrewSession`.
      const { crewWorkersQueryKey } = await import('./chat/command-center/CrewWorkerList')
      if (list === 'failed') await waitFor(() => expect(qc.getQueryState(crewWorkersQueryKey(root))?.status).toBe('error'))
    }
    const held: unknown[] = []
    crewBoard.mockImplementation(() => new Promise(resolve => { held.push(resolve) }))
    return { push: (slot: typeof idle) => act(() => { store.dispatch(sseSlots([conductor, slot] as never)) }), idle, asking, held }
  }
  const working = () => screen.getByTestId('all-projects-section-working')
  const needs = () => screen.getByTestId('all-projects-section-needs_you')
  const inWorking = (title: string) => within(working()).queryByText(title)

  it('an ask moves the story into its card at once, before the board re-read, as the right bar does', async () => {
    const { push, asking, held } = await mountHeld('root-ask', { asks: false })
    expect(within(working()).getByText('Needs a tool')).toBeTruthy()
    push(asking)
    await waitFor(() => expect(within(working()).queryByText('Needs a tool')).toBeNull())
    expect(needs()).toHaveAttribute('data-count', '1')
    expect(within(needs()).getByTestId('all-projects-ask-row')).toHaveTextContent('1 tool approval waits')
    expect(within(needs()).queryByText('Needs a tool')).toBeNull()
    expect(within(project('root-ask')).getByTestId('board-project-needs-you')).toHaveTextContent('1')
    // The board has not answered since the push.
    await waitFor(() => expect(held).toHaveLength(1))
  })

  it('a decision puts the story back in Working at once, before the board re-read, as the right bar does', async () => {
    const { push, idle, held } = await mountHeld('root-decide', { asks: true, flagged: ['Needs a tool'] })
    await waitFor(() => expect(needs()).toHaveAttribute('data-count', '1'))
    expect(within(working()).queryByText('Needs a tool')).toBeNull()
    push(idle)
    await waitFor(() => expect(within(working()).getByText('Needs a tool')).toBeTruthy())
    expect(needs()).toHaveAttribute('data-count', '0')
    expect(within(project('root-decide')).getByTestId('board-project-needs-you')).toHaveTextContent('0')
    await waitFor(() => expect(held).toHaveLength(1))
  })

  it('with the crew list still loading, a story stays in Working beside its card, and stays there at the decision, as the right bar does', async () => {
    const { push, idle } = await mountHeld('root-loading', { asks: true, list: 'loading', flagged: ['Needs a tool'] })
    await waitFor(() => expect(needs()).toHaveAttribute('data-count', '1'))
    expect(within(needs()).getByTestId('all-projects-ask-row')).toHaveTextContent('1 tool approval waits')
    expect(inWorking('Needs a tool')).not.toBeNull()
    expect(working()).toHaveAttribute('data-count', '2')
    push(idle)
    await waitFor(() => expect(needs()).toHaveAttribute('data-count', '0'))
    expect(inWorking('Needs a tool')).not.toBeNull()
    expect(working()).toHaveAttribute('data-count', '2')
    expect(within(project('root-loading')).getByTestId('board-project-working')).toHaveTextContent('2')
  })

  it('with the crew list failed and no card on screen, a story stays in Working, as the right bar does', async () => {
    await mountHeld('root-failed', { asks: false, list: 'failed', flagged: ['Needs a tool'] })
    expect(inWorking('Needs a tool')).not.toBeNull()
    expect(needs()).toHaveAttribute('data-count', '0')
    expect(working()).toHaveAttribute('data-count', '2')
  })

  it('one card stands for one story: a second flagged story nobody is bound to stays in Working, as the right bar does', async () => {
    await mountHeld('root-two', { asks: true, flagged: ['Needs a tool', 'Old ask'] })
    await waitFor(() => expect(needs()).toHaveAttribute('data-count', '1'))
    expect(inWorking('Needs a tool')).toBeNull()
    expect(inWorking('Old ask')).not.toBeNull()
    expect(working()).toHaveAttribute('data-count', '2')
  })

  it('with the crew list read before the worker was bound, a story stays in Working beside its card, as the right bar does', async () => {
    await mountHeld('root-late', { asks: true, list: 'late', flagged: ['Needs a tool'] })
    await waitFor(() => expect(needs()).toHaveAttribute('data-count', '1'))
    expect(inWorking('Needs a tool')).not.toBeNull()
    expect(working()).toHaveAttribute('data-count', '2')
  })

  it('reads each project once on a return to the tab: one board read and one crew list read each', async () => {
    let shown: DocumentVisibilityState = 'visible'
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => shown })
    onTestFinished(() => { delete (document as { visibilityState?: unknown }).visibilityState })
    // The app's own client: no read goes stale on its own, so only the page asks.
    const qc = newQueryClient()
    await mount('/crew-board', SLOTS, qc)
    await waitFor(() => expect(crewBoard).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(crewWorkers).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(qc.isFetching()).toBe(0))
    act(() => { shown = 'hidden'; document.dispatchEvent(new Event('visibilitychange')) })
    act(() => { shown = 'visible'; document.dispatchEvent(new Event('visibilitychange')) })
    await waitFor(() => expect(crewBoard).toHaveBeenCalledTimes(6))
    await waitFor(() => expect(qc.isFetching()).toBe(0))
    expect(crewBoard).toHaveBeenCalledTimes(6)
    expect(crewWorkers).toHaveBeenCalledTimes(6)
    expect(crewBoard.mock.calls.slice(3).map(c => c[0]).sort()).toEqual(['root-a', 'root-b', 'root-c'])
  })

  it("opens a linked Crew's worker in the crew window", async () => {
    crewWorkers.mockImplementation((root: string) => Promise.resolve(root === 'root-a' ? {
      root,
      workers: [{ key: 'r-1', title: 'Reviewer', agent: '', status: 'idle', group: 'done', last_activity: '', ledger_item: 'Fresh review', created_by: root, remote_crew: 'linked-crew', remote_instance: 'inst-1', remote_slot: 'peer-slot' }],
    } : { root, workers: [] }))
    await mount()
    const conductor = await screen.findByTestId('all-projects-section-conductor')
    await userEvent.setup({ advanceTimers: () => undefined }).click(await within(conductor).findByRole('button', { name: 'Fresh review' }))
    expect(currentCrewWindow()).toEqual({ instanceId: 'inst-1', key: 'peer-slot' })
    expect(openCrewSession).not.toHaveBeenCalled()
    expect(screen.getByTestId('where')).toHaveTextContent(/^\/chat$/)
  })

  it('stretches a board whose read fails under the one poll rule, the right bar\'s own id', async () => {
    crewBoard.mockImplementation((root: string) => (root === 'root-a'
      ? Promise.reject(Object.assign(new Error('gateway restarting'), { status: 503 }))
      : Promise.reject(Object.assign(new Error('no ledger'), { status: 404 }))))
    await mount()
    await screen.findByText('gateway restarting')
    expect(pollGapMs('crew-board:root-a', CREW_BOARD_POLL_MS)).toBe(2 * CREW_BOARD_POLL_MS)
  })

  it('carries the capacity line from real counts, this computer and a linked Crew', async () => {
    await mount()
    const computers = await screen.findAllByTestId('board-capacity-computer')
    expect(computers.map(c => within(c).getByTestId('board-capacity-name').textContent)).toEqual(['Seats on this computer', 'Seats on linked-crew'])
    // Two idle reviews and one idle ask here; the running build is on the linked Crew.
    // A count alone: no computer refuses a worker chat past a number, so no "of N".
    await waitFor(() => expect(within(computers[0]).getByTestId('board-capacity-chats').textContent).toBe('3'))
    expect(within(computers[0]).getByTestId('board-capacity-room')).toHaveTextContent('10.3 GB free · has room')
    expect(within(computers[1]).getByTestId('board-capacity-chats').textContent).toBe('1')
    expect(within(computers[1]).getByTestId('board-capacity-room')).toHaveTextContent('room not reported')
    expect(screen.getByTestId('board-capacity')).not.toHaveTextContent(/ of |%/)
  })

  it('reds the room only when memory hurts', async () => {
    crewBoardCapacity.mockResolvedValue({ ...CAPACITY, available_gb: 3.1, posture: 'tight' })
    await mount()
    const room = (await screen.findAllByTestId('board-capacity-room'))[0]
    expect(room).toHaveTextContent('3.1 GB free · memory tight')
    expect(room).toHaveAttribute('data-hurts', 'true')
  })

  it("points the Timeline at each project's own Board and switches to Steps bands", async () => {
    await mount()
    await screen.findAllByTestId('board-project')
    await userEvent.setup({ advanceTimers: () => undefined }).click(screen.getByTestId('conductor-mode-view-timeline'))
    expect(screen.getByTestId('all-projects-timeline-slot')).toHaveTextContent("Each project's Timeline is in its own Board, in that project's chat.")
    expect(screen.queryByTestId('conductor-mode-view-workers')).toBeNull()
    await userEvent.setup().click(screen.getByTestId('conductor-mode-view-steps'))
    const bands = screen.getAllByTestId('all-projects-band')
    expect(bands.map(b => b.getAttribute('data-root'))).toEqual(['root-a', 'root-b', 'root-c'])
  })

  it("draws a band's step rows and landed counts the right bar's Steps way", async () => {
    crewBoard.mockImplementation((root: string) => Promise.resolve(root === 'root-a' ? board([
      // Plan order puts the working story first; Steps puts who must act first.
      item('Still building', { plan: { step: 'build' } }),
      item('Gone quiet', { plan: { step: 'build' }, alive: 'closed' }),
      item('Shipped', { state: 'accepted', terminal: true, alive: 'closed', status: 'done' }),
    ]) : board([])))
    await mount('/crew-board', [SLOTS[0]])
    await screen.findAllByTestId('board-project')
    await userEvent.setup({ advanceTimers: () => undefined }).click(screen.getByTestId('conductor-mode-view-steps'))
    const build = (await screen.findAllByTestId('all-projects-band-step')).find(c => c.getAttribute('data-step') === 'build')!
    await waitFor(() => expect(within(build).getAllByTestId('board-row-title').map(t => t.textContent)).toEqual(['Gone quiet', 'Still building']))
    const landed = screen.getByTestId('all-projects-band-landed')
    expect(within(landed).getByTestId('board-landed-merged')).toHaveTextContent('1')
  })

  it('says so when no chat is in Conductor mode', async () => {
    await mount('/crew-board', [{ key: 'plain', title: 'Plain', messages: 1, running: false }])
    expect(await screen.findByTestId('all-projects-empty')).toBeTruthy()
    expect(crewBoard).not.toHaveBeenCalled()
  })

  it('still serves one Conductor board at ?conductor=', async () => {
    await mount('/crew-board?conductor=root-a')
    expect(await screen.findByText('Late review')).toBeTruthy()
    expect(screen.queryByTestId('all-projects-board')).toBeNull()
  })
})
