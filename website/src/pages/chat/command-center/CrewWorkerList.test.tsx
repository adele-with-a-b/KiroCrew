import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), resumeChatSlot: vi.fn(), crewBoard: vi.fn(), sessionSummary: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
const panel = vi.hoisted(() => ({ openPanelView: vi.fn() }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: panel.openPanelView }))

import { setQuestionCard, switchSlot } from '../../../store/chatSlice'
import { sseSlotPatch } from '../../../store/dashboardSlice'
import { ApiError } from '../../../api/apiError'
import { CREW_WORKERS_PUSH_COALESCE_MS, CrewWorkersTab, reopenCrewSession } from './CrewWorkerList'
import ConductorModeCrewPanel, { CREW_BOARD_EXPANDED_KEY } from './ConductorModeCrewPanel'
import { setCrewModeCatalog } from '../../../lib/crewMode'
import { closeCrewWindow, currentCrewWindow } from '../crew-window/crewWindowStore'

const W = (key: string, title: string, status: string, group: string) =>
  ({ key, title, agent: 'kirocrew-worker', status, group, last_activity: '', ledger_item: '', created_by: 'root-1' })

const WORKERS = {
  root: 'root-1',
  workers: [
    W('w-done', 'Worker D', 'idle', 'done'),
    { ...W('w-work', 'Worker A', 'working', 'working'), summary: 'Writing a 3-line welcome note' },
    W('w-need', 'Worker B', 'needs_approval', 'approval'),
    W('w-ask', 'Worker F', 'idle', 'with_conductor'),
    W('w-wait', 'Worker C', 'idle', 'waiting'),
    { ...W('w-gone', 'Worker E', 'closed', 'done'), ledger_item: 'it_9' },
  ],
}

const BOARD = { conductor: { schema: 1, slot_key: 'root-1', goal: 'Plan the launch. Then more.', round: 1, depth: 0, parent_item: null, created_at: '' }, conductor_alive: 'idle', items: [], take_over_available: false }

/** The Crew tab opens on the Board; most tests here read the Workers list. */
async function toList() {
  fireEvent.click(await screen.findByTestId('conductor-mode-view-workers'))
}

/** A member dispatched by `by` (a sub-conductor's key, or the root). */
const D = (key: string, title: string, by: string, group = 'working') => ({ ...W(key, title, 'idle', group), created_by: by, dispatched_by: by })
const titleOf = (row: HTMLElement) => row.querySelector('[data-testid="conductor-mode-member-title"]')?.textContent
const rowOf = (title: string) => screen.getByText(title).closest('[data-testid="conductor-mode-member"]') as HTMLElement
const findRowOf = async (title: string) => (await screen.findByText(title)).closest('[data-testid="conductor-mode-member"]') as HTMLElement

function renderWith(ui: React.ReactElement, slots: unknown[] = []) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots } } as never)
  const view = render(<QueryClientProvider client={qc}><Provider store={store}>{ui}</Provider></QueryClientProvider>)
  return Object.assign(store, { unmount: view.unmount })
}

describe('Conductor mode Crew tab', () => {
  beforeEach(() => {
    api.crewBoard.mockResolvedValue(BOARD)
    api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
  })
  afterEach(() => vi.clearAllMocks())

  it('opens on Moves: the goal line from the ledger and the sections by who must act, with no Recap', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    expect(screen.getByTestId('conductor-mode-view-moves')).toHaveAttribute('aria-checked', 'true')
    expect(await screen.findByText('Plan the launch.')).toHaveAttribute('data-testid', 'conductor-mode-goal')
    expect(await screen.findByTestId('conductor-mode-needs-you')).toBeInTheDocument()
    expect(screen.getByTestId('board-moves')).toBeInTheDocument()
    expect(screen.queryByTestId('conductor-mode-recap')).toBeNull()
    expect(screen.queryByTestId('conductor-mode-milestones')).toBeNull()
    // Session counts live in Workers, not on the Board.
    expect(screen.getByTestId('conductor-mode-board')).not.toHaveTextContent(/sessions ·/)
  })

  it('List: sub-teams sit under their lead, finished members fold away', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [D('g1', 'Grandchild', 'sub'), D('top', 'Top worker', 'root-1'), D('sub', 'Sub team', 'root-1'), D('old', 'Old one', 'root-1', 'done')] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    await screen.findByText('Grandchild')
    const rows = within(screen.getByTestId('conductor-mode-list')).getAllByTestId('conductor-mode-member')
    expect(rows.slice(0, 3).map(titleOf)).toEqual(['Top worker', 'Sub team', 'Grandchild'])
    expect(rows[2]).toHaveAttribute('data-depth', '1')
    const finished = screen.getByTestId('conductor-mode-finished')
    expect(finished).not.toHaveAttribute('open')
    expect(within(finished).getByText('Old one')).toBeInTheDocument()
  })

  it('List: each row says where its worker runs, and a remote row opens its chat in the crew window', async () => {
    onTestFinished(() => closeCrewWindow())
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [
      D('local', 'Local one', 'root-1'),
      { ...D('remote:it_r', 'Remote one', 'root-1'), ledger_item: 'it_r', remote_crew: 'Build box', remote_instance: 'inst-1', remote_slot: 'chat-7' },
      { ...D('remote:inst-1:chat-9', 'No task one', 'root-1'), remote_crew: 'Build box', remote_instance: 'inst-1', remote_slot: 'chat-9' },
      { ...D('remote:it_old', 'Old remote', 'root-1'), ledger_item: 'it_old', remote_crew: 'Build box' },
      { ...W('remote:it_done', 'Done remote', 'closed', 'done'), created_by: 'root-1', dispatched_by: 'root-1', ledger_item: 'it_done', remote_crew: 'Build box', remote_instance: 'inst-1', remote_slot: 'chat-5' },
    ] })
    const store = renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    const local = await findRowOf('Local one')
    const remote = rowOf('Remote one')
    expect(within(local).getByTestId('conductor-mode-member-location')).toHaveTextContent('This machine')
    expect(within(remote).getByTestId('conductor-mode-member-location')).toHaveTextContent('Build box')
    // One click lands in that chat on its crew; the main pane keeps its own chat.
    fireEvent.click(within(remote).getByText('Remote one').closest('button') as HTMLButtonElement)
    expect(currentCrewWindow()).toEqual({ instanceId: 'inst-1', key: 'chat-7' })
    expect(store.getState().chat.activeSlot).not.toBe('remote:it_r')
    // A chat with no task opens the same way and says it has none.
    const untied = rowOf('No task one')
    expect(within(untied).getByTestId('conductor-mode-member-no-task')).toHaveTextContent('No task linked')
    expect(within(remote).queryByTestId('conductor-mode-member-no-task')).toBeNull()
    fireEvent.click(within(untied).getByText('No task one').closest('button') as HTMLButtonElement)
    expect(currentCrewWindow()).toEqual({ instanceId: 'inst-1', key: 'chat-9' })
    // A finished task whose chat its crew still holds opens that chat from Finished.
    fireEvent.click(within(screen.getByTestId('conductor-mode-finished')).getByText('Done remote').closest('button') as HTMLButtonElement)
    expect(currentCrewWindow()).toEqual({ instanceId: 'inst-1', key: 'chat-5' })
    // A remote row that names no chat on its crew still opens nothing.
    expect(within(rowOf('Old remote')).getByText('Old remote').closest('button')).toBeDisabled()
    expect(within(local).getByText('Local one').closest('button')).not.toBeDisabled()
  })

  it('List: ONE tree, a finished member stays under its open lead and an open one under its finished lead', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [
      D('kid', 'Kid', 'sub', 'done'), D('sub', 'Sub team', 'root-1', 'working'),
      D('rel', 'Release notes', 'root-1', 'done'), D('draft', 'Draft notes', 'rel', 'working'),
      D('old', 'Old one', 'root-1', 'done'),
    ] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    await screen.findByText('Kid')
    const list = within(screen.getByTestId('conductor-mode-list'))
    const main = list.getAllByTestId('conductor-mode-member').filter(r => !r.closest('[data-testid="conductor-mode-finished"]'))
    expect(main.map(r => `${titleOf(r)}@${r.getAttribute('data-depth')}`)).toEqual(['Sub team@0', 'Kid@1', 'Release notes@0', 'Draft notes@1'])
    // Finished only styles the row.
    expect(main[1]).toHaveAttribute('data-done', 'true')
    expect(main[1].querySelector('[data-testid="conductor-mode-member-title"]')?.className).toContain('text-muted')
    // The Finished fold still holds a tree finished all through.
    const finished = screen.getByTestId('conductor-mode-finished')
    expect(finished).toHaveTextContent('Finished · 1')
    expect(within(finished).getByText('Old one')).toBeInTheDocument()
  })

  it('List: a lead folds and unfolds its crew, and a folded lead shows its counts', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [
      D('lead', 'Lead', 'root-1', 'waiting'), D('sub', 'Sub team', 'lead', 'working'),
      D('ask', 'Asker', 'sub', 'approval'), D('run', 'Runner', 'sub', 'working'), D('done1', 'Done one', 'sub', 'done'),
    ] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    await screen.findByText('Sub team')
    // Top-level open by default, deeper folded: the sub team's crew is out of sight.
    expect(screen.queryByText('Asker')).toBeNull()
    const sub = rowOf('Sub team')
    const chev = within(sub).getByTestId('conductor-mode-member-chevron')
    expect(chev).toHaveAttribute('aria-expanded', 'false')
    expect(within(sub).getByTestId('conductor-mode-member-kids')).toHaveTextContent('3')
    expect(within(sub).getByTestId('conductor-mode-member-needs-you')).toHaveTextContent('1')
    expect(within(sub).getByTestId('conductor-mode-member-running')).toHaveTextContent('1')
    fireEvent.click(chev)
    expect(screen.getByText('Asker')).toBeInTheDocument()
    expect(screen.getByText('Done one')).toBeInTheDocument()
    const opened = rowOf('Sub team')
    expect(within(opened).getByTestId('conductor-mode-member-chevron')).toHaveAttribute('aria-expanded', 'true')
    // Open, the counts make way for the rows themselves; the child count stays.
    expect(within(opened).queryByTestId('conductor-mode-member-needs-you')).toBeNull()
    expect(within(opened).queryByTestId('conductor-mode-member-running')).toBeNull()
    expect(within(opened).getByTestId('conductor-mode-member-kids')).toHaveTextContent('3')
    // The chevron only folds: the session stays where it was.
    expect(panel.openPanelView).not.toHaveBeenCalled()
    // Folding the lead hides its whole tree.
    fireEvent.click(within(rowOf('Lead')).getByTestId('conductor-mode-member-chevron'))
    expect(screen.queryByText('Sub team')).toBeNull()
    expect(within(rowOf('Lead')).getByTestId('conductor-mode-member-running')).toHaveTextContent('2')
  })

  it('List: the fold choices survive a remount under their own key', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [D('lead', 'Lead', 'root-1', 'working'), D('sub', 'Sub team', 'lead', 'working'), D('deep', 'Deep one', 'sub', 'working')] })
    const first = renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    fireEvent.click(within(await findRowOf('Sub team')).getByTestId('conductor-mode-member-chevron'))
    fireEvent.click(within(rowOf('Lead')).getByTestId('conductor-mode-member-chevron'))
    expect(JSON.parse(localStorage.getItem(CREW_BOARD_EXPANDED_KEY) || '{}')).toEqual({ sub: true, lead: false })
    expect(localStorage.getItem('mc-sidebar-conductor-expanded')).toBeNull()
    first.unmount()
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    await findRowOf('Lead')
    expect(screen.queryByText('Sub team')).toBeNull()
    fireEvent.click(within(rowOf('Lead')).getByTestId('conductor-mode-member-chevron'))
    expect(screen.getByText('Deep one')).toBeInTheDocument()
  })

  it('List: a search unfolds every match and the folds come back after', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [
      D('lead', 'Lead', 'root-1', 'working'), D('sub', 'Sub team', 'lead', 'working'), D('deep', 'Deep one', 'sub', 'working'),
      D('x1', 'Other 1', 'root-1', 'working'), D('x2', 'Other 2', 'root-1', 'working'), D('x3', 'Other 3', 'root-1', 'working'),
    ] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    await findRowOf('Lead')
    expect(screen.queryByText('Deep one')).toBeNull()
    const box = screen.getByTestId('crew-search')
    fireEvent.change(box, { target: { value: 'e' } })
    expect(await screen.findByText('Deep one')).toBeInTheDocument()
    // While searching every row is open, so there is nothing to fold.
    expect(screen.queryByTestId('conductor-mode-member-chevron')).toBeNull()
    fireEvent.change(box, { target: { value: '' } })
    await waitFor(() => expect(screen.queryByText('Deep one')).toBeNull())
    expect(within(rowOf('Sub team')).getByTestId('conductor-mode-member-chevron')).toHaveAttribute('aria-expanded', 'false')
  })

  it('List: the member on screen is not folded out of sight, and stays highlighted', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [D('lead', 'Lead', 'root-1', 'working'), D('sub', 'Sub team', 'lead', 'working'), D('deep', 'Deep one', 'sub', 'working')] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="deep" active />)
    await toList()
    const deep = await findRowOf('Deep one')
    expect(within(deep).getByRole('button', { name: /Deep one/ })).toHaveAttribute('aria-current', 'true')
  })

  it('List: past six levels the rows stop stepping right and carry the level', async () => {
    const chain = Array.from({ length: 9 }, (_, i) => D(`n${i}`, `Level ${i}`, i === 0 ? 'root-1' : `n${i - 1}`, 'working'))
    // Every lead opened, so the whole chain is on screen.
    localStorage.setItem(CREW_BOARD_EXPANDED_KEY, JSON.stringify(Object.fromEntries(chain.map(w => [w.key, true]))))
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: chain })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    const l6 = await findRowOf('Level 6')
    const l8 = rowOf('Level 8')
    expect(l8).toHaveAttribute('data-depth', '8')
    expect(l8.style.paddingLeft).toBe(l6.style.paddingLeft)
    expect(within(l8).getByTestId('conductor-mode-member-depth')).toHaveTextContent('·8')
    expect(within(l6).queryByTestId('conductor-mode-member-depth')).toBeNull()
  })

  it('List: with no crew under anyone, rows keep their old layout: no chevron and no gutter', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    const row = await findRowOf('Worker A')
    expect(screen.queryByTestId('conductor-mode-member-chevron')).toBeNull()
    expect(row.children).toHaveLength(1)
  })

  it('List: clicking a closed member reopens it from history', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.resumeChatSlot.mockResolvedValue({ ok: true, key: 'w-gone', messages: [] })
    const store = renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    fireEvent.click(await screen.findByRole('button', { name: /Worker E/ }))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('w-gone'))
    expect(api.resumeChatSlot).toHaveBeenCalledWith('dashboard_w-gone', 'Worker E')
    // The reopen settles its last update before the test ends.
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  })

  it('List: clicking a member opens it in the main pane, with no drawer step', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    const store = renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    fireEvent.click(await screen.findByRole('button', { name: /Worker A/ }))
    expect(screen.queryByTestId('conductor-mode-member-drawer')).toBeNull()
    expect(panel.openPanelView).toHaveBeenCalledWith('w-work', 'workers', { hold: true })
    expect(store.getState().chat.activeSlot).toBe('w-work')
  })

  it('List: shows the open rows from the slot store when the gateway does not serve the list', async () => {
    api.crewWorkers.mockRejectedValue(new ApiError(404, 'not found', ''))
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />, [
      { key: 'w-live', title: 'Live worker', agent: 'kirocrew-worker', messages: 2, running: true, crew_root: 'root-1', created_by: 'root-1' },
    ])
    await toList()
    expect(await screen.findByRole('button', { name: /Live worker/ })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('List: from six members up, a search filters live and finished members and unfolds finished matches', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    const box = await screen.findByTestId('crew-search')
    fireEvent.change(box, { target: { value: 'Worker E' } })
    const list = screen.getByTestId('conductor-mode-list')
    await waitFor(() => expect(within(list).getAllByTestId('conductor-mode-member')).toHaveLength(1))
    expect(screen.getByTestId('conductor-mode-finished')).toHaveAttribute('open')
    fireEvent.change(box, { target: { value: 'nobody' } })
    expect(await screen.findByTestId('crew-search-empty')).toBeInTheDocument()
  })

  it('the Crew tab finds the root from a crew member row', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<CrewWorkersTab slot="w-need" active />, [{ key: 'w-need', crew_root: 'root-1', messages: 1 }, { key: 'plain' }])
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalledWith('root-1'))
  })

  it('reopenCrewSession says whether the reopened session took the view', async () => {
    // Took the view: the person was still where they asked from.
    api.resumeChatSlot.mockResolvedValueOnce({ ok: true, key: 'w-gone', messages: [] })
    const store = createTestStore({ dashboard: { slots: [] } } as never)
    await expect(reopenCrewSession(store.dispatch, store.getState, 'w-gone', 'Worker E')).resolves.toEqual({ ok: true, activated: true, key: 'w-gone' })
    expect(store.getState().chat.activeSlot).toBe('w-gone')
    // Reopened, but the person picked another chat while it was pending.
    let answer: (v: unknown) => void = () => {}
    api.resumeChatSlot.mockReturnValueOnce(new Promise(resolve => { answer = resolve }))
    const pending = reopenCrewSession(store.dispatch, store.getState, 'w-two', 'Worker F')
    await vi.waitFor(() => expect(api.resumeChatSlot).toHaveBeenCalledTimes(2))
    store.dispatch(switchSlot({ key: 'elsewhere' }))
    answer({ ok: true, key: 'w-two', messages: [] })
    await expect(pending).resolves.toEqual({ ok: true, activated: false, key: 'w-two' })
    expect(store.getState().chat.activeSlot).toBe('elsewhere')
    // Could not reopen.
    api.resumeChatSlot.mockRejectedValueOnce(new Error('gone'))
    await expect(reopenCrewSession(store.dispatch, store.getState, 'w-three', 'Worker G')).resolves.toEqual({ ok: false, activated: false, key: 'w-three' })
  })

  it('shows the conductor mode panel for a chat with messages that was just switched on, before its root stamp lands', async () => {
    setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor'])
    try {
      api.crewWorkers.mockResolvedValue({ root: 'mid', workers: [] })
      renderWith(<CrewWorkersTab slot="mid" active />, [{ key: 'mid', messages: 3, agent: 'kirocrew-conductor', agent_kind: 'template' }])
      expect(await screen.findByTestId('conductor-mode-crew-panel')).toBeInTheDocument()
      expect(screen.queryByTestId('crew-workers-none')).toBeNull()
    } finally {
      setCrewModeCatalog(undefined, undefined)
    }
  })

  it('names the four positions of the one switch, Moves first and picked', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />, [{ key: 'root-1', title: 'Sample goal', crew_mode_root: true }])
    const radios = within(await screen.findByRole('radiogroup', { name: 'Board view' })).getAllByRole('radio')
    expect(radios.map(r => r.textContent)).toEqual(['Moves', 'Steps', 'Timeline', 'Workers'])
    // A chip a narrow bar cuts short still names itself in full.
    expect(radios.map(r => r.getAttribute('title'))).toEqual(['Moves', 'Steps', 'Timeline', 'Workers'])
    expect(radios.map(r => r.getAttribute('aria-checked'))).toEqual(['true', 'false', 'false', 'false'])
  })

  it('switches the body under the same head: Steps groups by step, Timeline says it is coming', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    expect(await screen.findByTestId('board-moves')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('conductor-mode-view-steps'))
    expect(await screen.findByTestId('board-steps')).toBeInTheDocument()
    expect(screen.getByTestId('conductor-mode-goal')).toHaveTextContent('Plan the launch.')
    expect(screen.queryByTestId('board-moves')).toBeNull()
    fireEvent.click(screen.getByTestId('conductor-mode-view-timeline'))
    expect(screen.getByTestId('board-timeline-coming')).toHaveTextContent('The Timeline comes in a later update.')
    expect(screen.getByTestId('conductor-mode-goal')).toBeInTheDocument()
  })

  it('the Workers tab on a plain chat lists nothing', () => {
    // A plain chat with messages: a brand-new one shows its (empty) panel while
    // its conductor mode switch lands.
    renderWith(<CrewWorkersTab slot="plain" active />, [{ key: 'plain', messages: 2 }])
    expect(screen.getByTestId('crew-workers-none')).toBeInTheDocument()
    expect(api.crewWorkers).not.toHaveBeenCalled()
  })

  it('viewing a worker: a Back row returns to the root and keeps the panel open', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    const store = renderWith(<ConductorModeCrewPanel root="root-1" viewing="w-need" active />, [
      { key: 'root-1', title: 'Sample goal', crew_mode_root: true },
      { key: 'w-need', title: 'Worker B', crew_root: 'root-1' },
    ])
    await screen.findByTestId('conductor-mode-crew-panel')
    const back = screen.getByTestId('crew-panel-back')
    // The visible word is short; its accessible name still says where it goes.
    expect(back).toHaveTextContent(/^Back$/)
    // One label, no title chip beside it.
    expect(back).not.toHaveTextContent('Sample goal')
    // Label in name: the visible word is the accessible name; the long form is the tooltip.
    expect(back).toHaveAccessibleName('Back')
    expect(back).toHaveAttribute('title', 'Back to main chat')
    // A real button, so Tab and Enter reach it.
    expect(back.tagName).toBe('BUTTON')
    fireEvent.click(back)
    expect(store.getState().chat.activeSlot).toBe('root-1')
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(panel.openPanelView).toHaveBeenCalledWith('root-1', 'workers', { hold: true })
  })

  it('viewing the root itself: no Back row', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />, [{ key: 'root-1', title: 'Sample goal', crew_mode_root: true }])
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalled())
    expect(screen.queryByTestId('crew-panel-back')).toBeNull()
  })

  it('a worker whose root is closed: the Back row reopens the main chat, never a dead switch', async () => {
    api.crewWorkers.mockResolvedValue({ ...WORKERS, root_closed: true, root_title: 'Saved goal' })
    api.resumeChatSlot.mockResolvedValue({ ok: true, key: 'root-1', messages: [] })
    // No 'root-1' row: the root's tab is closed.
    const store = renderWith(<ConductorModeCrewPanel root="root-1" viewing="w-need" active />, [{ key: 'w-need', title: 'Worker B', crew_root: 'root-1' }])
    await screen.findByTestId('conductor-mode-crew-panel')
    const back = screen.getByTestId('crew-panel-back')
    expect(back).toHaveAttribute('data-root-closed', 'true')
    expect(back).toHaveTextContent('Open main chat')
    // The saved title arrives with the crew list; Back reopens under it.
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalled())
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    fireEvent.click(back)
    await vi.waitFor(() => expect(store.getState().chat.activeSlot).toBe('root-1'))
    expect(api.resumeChatSlot).toHaveBeenCalledWith('dashboard_root-1', 'Saved goal')
  })

  it('a failed reopen of a closed root says so under the Back row', async () => {
    api.crewWorkers.mockResolvedValue({ ...WORKERS, root_closed: true, root_title: 'Saved goal' })
    api.resumeChatSlot.mockResolvedValue({ ok: false })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="w-need" active />, [{ key: 'w-need', title: 'Worker B', crew_root: 'root-1' }])
    await screen.findByTestId('conductor-mode-crew-panel')
    fireEvent.click(screen.getByTestId('crew-panel-back'))
    expect(await screen.findByText('Could not reopen that session. Try again.')).toBeInTheDocument()
  })

  const ITEM = (over: Record<string, unknown>) => ({
    schema: 1, item_id: 'it_1', title: 'Menu', acceptance: { kind: 'human_approval' }, state: 'open', verdict: null,
    decision: '', round: 1, fails: 0, status: 'progress', summary: '', artifacts: {}, pr: null, last_report_at: '2026-10-30T00:00:00Z',
    created_at: '2026-10-30T00:00:00Z', closed_at: null, decided_at: null, orphaned: false, stale: false,
    acceptance_concrete: true, outstanding: false, terminal: false, alive: 'idle', events: [], ...over,
  })

  it('Board: a failed board read says so instead of an empty sprint', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockRejectedValue(new ApiError(500, 'board down', ''))
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    expect(await screen.findByText('board down')).toBeInTheDocument()
    // No section claims "none" on a board it never read.
    expect(screen.queryByTestId('board-moves')).toBeNull()
  })

  it('List: a failed crew read says so', async () => {
    api.crewWorkers.mockRejectedValue(new ApiError(500, 'boom', ''))
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    await toList()
    expect(await screen.findByText('Could not load the crew.')).toBeInTheDocument()
  })

  it('Board: the pulse says stuck only when a milestone has no chat left', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'a', alive: 'closed' })] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    expect(await screen.findByTestId('conductor-mode-pulse')).toHaveTextContent(/stuck/i)
    // Muted, not red: only "needs you" asks the person to act.
    expect(screen.getByTestId('conductor-mode-pulse').className).toContain('text-muted')
    expect(screen.getByTestId('conductor-mode-pulse').className).not.toContain('danger')
  })

  it('Board: the quiet pulse shows even when nobody has reported yet', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'n1', status: null, last_report_at: null, stale: true })] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    expect(await screen.findByTestId('conductor-mode-pulse')).toHaveTextContent(/quiet/i)
    expect(screen.getByTestId('conductor-mode-pulse').className).toContain('text-muted')
    expect(screen.getByTestId('conductor-mode-pulse').className).not.toContain('warn')
  })

  it('Board: a stale milestone whose chat is open reads Quiet with its PR, an idle one names its PR or says no reason', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [
      ITEM({ item_id: 'q1', title: 'Supplies list', stale: true, pr: 42, summary: 'Holding for CI.' }),
      ITEM({ item_id: 'w1', title: 'Drinks menu', pr: 7, summary: '' }),
      ITEM({ item_id: 'w2', title: 'Guest list', summary: '' }),
    ] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    const rows = await screen.findAllByTestId('conductor-mode-milestone')
    expect(rows.map(r => r.getAttribute('data-state'))).toEqual(['quiet', 'waiting', 'waiting'])
    // Each part ends in its separator, so a wrapped line never starts with one.
    expect(rows[0]).toHaveTextContent('Quiet · nothing is scheduled to wake it ·PR #42Holding for CI.')
    // The worker's words get a line of their own, so a narrow panel never hides them.
    expect(screen.getByTestId('conductor-mode-quiet-said')).toHaveTextContent('Holding for CI.')
    expect(screen.getByTestId('conductor-mode-quiet-said').parentElement?.className).toContain('flex-col')
    // On a narrow panel the reason wraps instead of being cut, and the PR number keeps its width.
    expect(screen.getByTestId('conductor-mode-quiet-reason').className).toMatch(/\bmin-w-0\b.*overflow-wrap:anywhere/)
    expect(within(rows[0]).getByText('PR #42').className).toContain('shrink-0')
    expect(rows[1]).toHaveTextContent('Waiting on PR #7')
    expect(rows[2]).toHaveTextContent('Paused, no reason given')
    expect(screen.getByTestId('conductor-mode-pulse')).toHaveTextContent(/quiet/i)
    // Quiet is nobody moving it: Stuck, red. A PR it idles on waits on GitHub.
    expect(screen.getByTestId('conductor-mode-needs-you')).toHaveAttribute('data-empty', 'true')
    expect(rows[0].closest('[data-section]')).toHaveAttribute('data-section', 'stuck')
    expect(rows[0].closest('[data-testid="board-story"]')).toHaveAttribute('data-tone', 'danger')
    expect(rows[1].closest('[data-section]')).toHaveAttribute('data-section', 'github')
    expect(rows[2].closest('[data-section]')).toHaveAttribute('data-section', 'working')
  })

  it('Board: an idle milestone whose worker loops on CI reads running and names its PR', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [
      ITEM({ item_id: 'l1', title: 'Drinks menu', pr: 7, looping: true, summary: 'Monitor armed, ending turn.' }),
    ] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    const [row] = await screen.findAllByTestId('conductor-mode-milestone')
    expect(row.getAttribute('data-state')).toBe('running')
    // One wording, typed, never the worker's own words; no turn is in flight, so no "now".
    expect(row).toHaveTextContent('In progress · PR #7')
    expect(row).not.toHaveTextContent('Waiting')
    expect(row).not.toHaveTextContent('Monitor armed')
    expect(row).not.toHaveTextContent(/\bnow\b/)
  })

  it('Board: a blocked milestone opens its crew member, and shows no "moving" pulse', async () => {
    api.crewWorkers.mockResolvedValue({ ...WORKERS, workers: [{ ...W('w-sup', 'Supplies', 'idle', 'waiting'), ledger_item: 'b1' }] })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'b1', title: 'Supplies list', status: 'blocked' })] })
    const store = renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    const open = within(await screen.findByTestId('board-story')).getByRole('button', { name: 'Supplies list' })
    expect(screen.queryByTestId('conductor-mode-pulse')).toBeNull()
    fireEvent.click(open)
    expect(store.getState().chat.activeSlot).toBe('w-sup')
  })

  it('Board: a worker blocked on the conductor says the main chat is answering, and is not counted as needing you', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'b2', title: 'Supplies list', status: 'blocked', summary: 'The file save was declined. Retry next.' })] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    const row = await screen.findByTestId('conductor-mode-milestone')
    expect(row.getAttribute('data-state')).toBe('waiting')
    expect(row).toHaveTextContent('Main chat is answering')
    expect(row).not.toHaveTextContent('The file save was declined.')
    // The Conductor's move, not the person's.
    expect(row.closest('[data-section]')).toHaveAttribute('data-section', 'conductor')
    expect(screen.getByTestId('conductor-mode-needs-you')).toHaveAttribute('data-empty', 'true')
  })

  it('Board: a report whose reason names a person needs you, and Needs you counts it', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'h1', title: 'Supplies list', status: 'blocked', reason: 'needs_human', summary: 'Sign in to the shop. Then retry.' })] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    // Its row sits in Needs you, yellow, and says what it needs in place of the worker's line.
    const row = await screen.findByTestId('board-story')
    expect(row).toHaveAttribute('data-move', 'needs_you')
    expect(row).toHaveAttribute('data-tone', 'warn')
    expect(row).toHaveTextContent('Sign in to the shop.')
    // The section counts the row, so the number and the rows agree.
    expect(screen.getByTestId('conductor-mode-needs-you')).toHaveAttribute('data-count', '1')
    expect(within(screen.getByTestId('conductor-mode-needs-you')).getByTestId('board-story')).toBe(row)
  })

  it('Board: a worker whose session closed on an open report reads Stuck, not needs you', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 's1', title: 'Supplies list', status: 'progress', alive: 'closed', summary: 'Half the list. More soon.' })] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    const row = await screen.findByTestId('conductor-mode-milestone')
    expect(row.getAttribute('data-state')).toBe('stuck')
    expect(row).toHaveTextContent('Stuck · its chat closed, ask the main chat to restart it')
    expect(row.closest('[data-section]')).toHaveAttribute('data-section', 'stuck')
    expect(screen.getByTestId('conductor-mode-needs-you')).toHaveAttribute('data-empty', 'true')
  })

  it('Board: an unstarted milestone says Not started, never a made-up order', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'r1', title: 'Menu', alive: 'running' }), ITEM({ item_id: 'n1', title: 'Decor', status: null, last_report_at: null, created_at: '2026-10-30T00:01:00Z' })] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    const rows = await screen.findAllByTestId('conductor-mode-milestone')
    const next = rows.find(r => r.getAttribute('data-state') === 'next') as HTMLElement
    expect(next).toHaveTextContent('Not started')
    expect(next).not.toHaveTextContent('Starts after')
  })

  it('Board: an idle worker whose last report was progress reads Waiting, with no spinner or "moving" pulse', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'p1', title: 'Menu', summary: 'Drafted the menu. More soon.' })] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    const row = await screen.findByTestId('conductor-mode-milestone')
    expect(row.getAttribute('data-state')).toBe('waiting')
    expect(row).toHaveTextContent('Waiting')
    expect(row).toHaveTextContent('Drafted the menu.')
    expect(row).not.toHaveTextContent('now')
    expect(screen.queryByTestId('conductor-mode-pulse')).toBeNull()
  })

  it('Board: a milestone turns running on the same slot push the List re-reads on, not on the next poll', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'p1', title: 'Menu' })] })
    // A root of its own: the coalesced re-read is keyed by root across the file,
    // and an earlier test's torn-down timer must not swallow this push.
    const store = renderWith(<ConductorModeCrewPanel root="root-push" viewing="root-push" active />,
      [{ key: 'root-push', title: 'Lead' }, { key: 'w-menu', title: 'Menu worker', crew_root: 'root-push', created_by: 'root-push', running: false }])
    await waitFor(() => expect(screen.getByTestId('conductor-mode-milestone').getAttribute('data-state')).toBe('waiting'))
    await waitFor(() => expect(api.crewWorkers).toHaveBeenCalled())
    // The worker's turn starts: the slot push the List already re-reads on.
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'p1', title: 'Menu', alive: 'running' })] })
    act(() => { store.dispatch(sseSlotPatch({ slots: [{ key: 'w-menu', running: true }] })) })
    // Well inside the 10 s board poll (CREW_BOARD_POLL_MS): only the push can move it.
    await waitFor(() => expect(screen.getByTestId('conductor-mode-milestone').getAttribute('data-state')).toBe('running'), { timeout: CREW_WORKERS_PUSH_COALESCE_MS + 2000 })
  })

  it('Board: a story\'s sub-tasks are one count on its facts line, never rows of their own', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [
      ITEM({ item_id: 'it_0000000a', title: 'Menu', plan: { level: 'story' } }),
      ITEM({ item_id: 'it_0000000b', title: 'Tea list', status: null, last_report_at: null, plan: { level: 'task', parent: 'it_0000000a', step: 'merged' } }),
      ITEM({ item_id: 'it_0000000c', title: 'Prices', status: 'progress', summary: 'Half priced.', plan: { level: 'task', parent: 'it_0000000a', step: 'build' } }),
    ] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    const [menu, ...rest] = await screen.findAllByTestId('board-story')
    expect(rest).toHaveLength(0)
    expect(within(menu).getByTestId('board-fact-subtasks')).toHaveTextContent('Sub-tasks merged: 1 of 2')
    expect(screen.queryByText('Tea list')).toBeNull()
  })

  it('Board: a story reported done but not accepted is the Conductor\'s move, aged from its report', async () => {
    // Only Date is faked: the age reads the pinned clock, and the query timers stay real.
    vi.useFakeTimers({ toFake: ['Date'] })
    onTestFinished(() => vi.useRealTimers())
    vi.setSystemTime(Date.parse('2026-11-01T12:00:00Z'))
    api.crewWorkers.mockResolvedValue(WORKERS)
    const reported = '2026-11-01T11:55:00.000Z'
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'd2', title: 'Menu', status: 'done', summary: 'Menu drafted. All of it.', pr: 7, created_at: '2026-10-30T00:00:00Z', last_report_at: reported })] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    const row = await screen.findByTestId('board-story')
    expect(row.closest('[data-section]')).toHaveAttribute('data-section', 'conductor')
    expect(within(row).getByTestId('conductor-mode-milestone')).toHaveTextContent(/^Done ·Menu drafted\. ·PR #7$/)
    // Five minutes: under the 10-minute rule, so not red yet.
    expect(within(row).getByTestId('board-row-age')).toHaveTextContent('5m')
    expect(row).not.toHaveAttribute('data-tone')
  })

  it('Board: a Conductor with no ledger yet keeps every section as one quiet line', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockRejectedValue(new ApiError(404, 'no ledger', ''))
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    const moves = await screen.findByTestId('board-moves')
    expect([...moves.querySelectorAll('[data-section]')].map(s => [s.getAttribute('data-section'), s.getAttribute('data-empty')])).toEqual([
      ['needs_you', 'true'], ['stuck', 'true'], ['conductor', 'true'], ['github', 'true'], ['working', 'true'], ['up_next', 'true'],
    ])
    expect(screen.queryByText('no ledger')).toBeNull()
  })

  it('Board: a finished crew shows no pulse', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'b', state: 'accepted', terminal: true })] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    await screen.findByTestId('board-moves')
    expect(screen.queryByTestId('conductor-mode-pulse')).toBeNull()
  })

  it('Needs your input: the main chat question is one row that points at its card in the chat', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    const store = renderWith(<ConductorModeCrewPanel root="root-1" viewing="w-work" active />)
    act(() => { store.dispatch(switchSlot('w-work')) })
    act(() => { store.dispatch(setQuestionCard({ slot: 'root-1', card_id: 'card-1', questions: [{ question: 'Which tea?', options: [{ label: 'Oolong' }] }] } as never)) })
    const row = await screen.findByTestId('conductor-mode-main-question')
    expect(row).toHaveTextContent('Which tea?')
    expect(screen.queryByText('Oolong')).toBeNull()
    expect(screen.getByTestId('conductor-mode-needs-you')).toHaveAttribute('data-count', '1')
    fireEvent.click(screen.getByTestId('conductor-mode-answer-in-chat'))
    expect(store.getState().chat.activeSlot).toBe('root-1')
  })

  it('Board: landed work is a count in Steps, never a list of titles', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'd1', title: 'Drinks menu', state: 'accepted', terminal: true })] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    fireEvent.click(await screen.findByTestId('conductor-mode-view-steps'))
    expect(await screen.findByTestId('board-landed-merged')).toHaveTextContent(/^Merged1$/)
    expect(screen.queryByText('Drinks menu')).toBeNull()
  })

  it('Board: a failed crew read says so', async () => {
    api.crewWorkers.mockRejectedValue(new ApiError(500, 'boom', ''))
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    expect(await screen.findByText('Could not load the crew.')).toBeInTheDocument()
  })

  it('Board: a worker question waiting on the main chat does not ask the person', async () => {
    api.crewWorkers.mockResolvedValue(WORKERS)
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [ITEM({ item_id: 'q', title: 'Menu', status: 'question', outstanding: true })] })
    renderWith(<ConductorModeCrewPanel root="root-1" viewing="root-1" active />)
    const row = await screen.findByTestId('conductor-mode-milestone')
    expect(row).toHaveTextContent('Main chat is answering')
  })
})
