import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), resumeChatSlot: vi.fn(), crewBoard: vi.fn(), crewBoardTimeline: vi.fn(), sessionSummary: vi.fn(), crewBoardCapacity: vi.fn() }))
vi.mock('../../api/client', () => ({ api }))
vi.mock('../../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))

import ConductorModeCrewPanel from '../../pages/chat/command-center/ConductorModeCrewPanel'
import { crewBoardCapacityQueryKey } from '../../api/crewBoardCapacity'
import { closeCrewWindow, currentCrewWindow } from '../../pages/chat/crew-window/crewWindowStore'
import { landSteps, storyHeld, storyMinutes } from './storyView'
import type { WorkBoardItem } from '../../api/crewBoard'
import type { TimelineLane } from '../../lib/boardTimeline'

const NOW = Date.parse('2026-11-20T12:00:00Z')
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString()

const ITEM = (over: Partial<WorkBoardItem> & Record<string, unknown>) => ({
  schema: 1, item_id: 'it_1', title: 'Menu', acceptance: { kind: 'human_approval' }, state: 'open', verdict: null,
  decision: '', round: 1, fails: 0, status: 'progress', summary: '', artifacts: {}, pr: null, last_report_at: ago(30),
  created_at: ago(120), closed_at: null, decided_at: null, orphaned: false, stale: false,
  acceptance_concrete: true, outstanding: false, terminal: false, alive: 'idle', has_worker: true, events: [], ...over,
}) as WorkBoardItem

const W = (key: string, title: string, status: string, ledger: string) =>
  ({ key, title, agent: 'kirocrew-worker', status, group: 'waiting', last_activity: '', ledger_item: ledger, created_by: 'root-s' })

const EPIC = ITEM({ item_id: 'it_epic', title: 'Edits alone', status: null, last_report_at: null, plan: { level: 'epic' } })
/** Waits on GitHub: worked four hours, reported done, judged pending, idle on PR 12. */
const GITHUB = ITEM({
  item_id: 'it_g', title: 'Vignette rows', status: 'done', pr: 12, summary: 'Rows fixed, PR is up.', created_at: ago(360), last_report_at: ago(60),
  plan: { level: 'story', parent: 'it_epic', step: 'review', tags: ['est: 8 h'] },
  events: [{ id: 'e1', ts: ago(60), item_id: 'it_g', kind: 'report', status: 'done', text: '' }, { id: 'e2', ts: ago(52), item_id: 'it_g', kind: 'verdict', status: null, text: '' }],
})
/** Working, with no estimate, no report yet and no worker chat to open. */
const FRESH = ITEM({ item_id: 'it_f', title: 'Decor list', status: null, last_report_at: null, summary: '', has_worker: false, created_at: ago(45), plan: { level: 'story', step: 'todo' } })

const BOARD = {
  conductor: { schema: 1, slot_key: 'root-s', goal: 'Ship the edits.', round: 1, depth: 0, parent_item: null, created_at: ago(600) },
  conductor_alive: 'idle', items: [EPIC, GITHUB, FRESH], take_over_available: false,
}

const TIMELINE = {
  read_at: new Date(NOW).toISOString(), since: ago(720), hours: 12,
  events: {
    it_g: [
      { ts: ago(360), kind: 'create', status: null }, { ts: ago(300), kind: 'bind', status: null },
      { ts: ago(60), kind: 'report', status: 'done' }, { ts: ago(52), kind: 'verdict', status: null, verdict: 'pending' },
    ],
    it_f: [{ ts: ago(45), kind: 'create', status: null }],
  },
}

function renderPanel(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  const store = createTestStore({ dashboard: { slots: [{ key: 'root-s', title: 'Lantern' }] } } as never)
  render(<QueryClientProvider client={qc}><Provider store={store}><ConductorModeCrewPanel root="root-s" viewing="root-s" active /></Provider></QueryClientProvider>)
  return store
}

const lane = (spans: TimelineLane['spans']): TimelineLane => ({ spans, reports: [], owed: [] })
const span = (kind: TimelineLane['spans'][number]['kind'], from: number, to: number, late = false) => ({ kind, from: NOW - from * 60_000, to: NOW - to * 60_000, late, opened: NOW - from * 60_000 })

describe('a story view reads only what the ledger wrote', () => {
  it('splits the steps at the step the Board read gives the story', () => {
    expect(landSteps(GITHUB)).toEqual([
      { step: 'mocks', state: 'passed' }, { step: 'build', state: 'passed' }, { step: 'review', state: 'current' },
      { step: 'merged', state: 'ahead' }, { step: 'released', state: 'ahead' },
    ])
    // A story still at To do has every step ahead of it.
    expect(landSteps(FRESH).map(s => s.state)).toEqual(['ahead', 'ahead', 'ahead', 'ahead', 'ahead'])
  })

  it('counts GitHub only for the wait running now, and only while the row waits on GitHub', () => {
    const l = lane([span('working', 300, 60), span('conductor', 60, 55), span('conductor', 55, 52, true), span('idle', 52, 0)])
    expect(storyHeld(l, NOW, 'github')).toEqual({ worker: 240, github: 52, conductor: 8, you: 0, nobody: 0 })
    // The same lane on a row that does not wait on GitHub: no record of GitHub, and the idle stretch is nobody's.
    expect(storyHeld(l, NOW, 'working')).toEqual({ worker: 240, github: null, conductor: 8, you: 0, nobody: 52 })
    // An idle stretch in the past is never GitHub's, even on a GitHub row.
    expect(storyHeld(lane([span('idle', 90, 80), span('person', 80, 0)]), NOW, 'github')).toEqual({ worker: 0, github: null, conductor: 0, you: 80, nobody: 10 })
  })

  it('times a story from its creation to its close, NaN on a stamp that does not parse', () => {
    expect(storyMinutes(GITHUB, NOW)).toBe(360)
    expect(storyMinutes(ITEM({ created_at: ago(90), closed_at: ago(30) }), NOW)).toBe(60)
    expect(storyMinutes(ITEM({ created_at: 'not a time' }), NOW)).toBeNaN()
  })
})

describe('a click on a story opens its story view', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
    api.crewBoard.mockResolvedValue(BOARD)
    api.crewBoardTimeline.mockResolvedValue(TIMELINE)
    api.crewWorkers.mockResolvedValue({ root: 'root-s', workers: [W('w-g', 'Worker G', 'idle', 'it_g')] })
    api.crewBoardCapacity.mockResolvedValue({ name: 'this-mac', available_gb: 12, posture: 'ample', tight_gb: 4, critical_gb: 2, pressure_held: false })
    api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  it('draws the steps, who held it, its lane and its facts, each from the ledger', async () => {
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Vignette rows' }))
    const view = await screen.findByTestId('board-story-view')
    expect(screen.getByTestId('conductor-mode-board')).not.toBeVisible()
    expect(within(view).getByTestId('board-story-back')).toHaveTextContent('Lantern · Review')
    expect(within(view).getByTestId('board-story-state')).toHaveTextContent('Waits on GitHub')
    expect(within(view).getByTestId('board-story-worker')).toHaveTextContent('Worker G')
    const steps = within(view).getAllByTestId('board-story-step')
    expect(steps.map(s => [s.getAttribute('data-step'), s.getAttribute('data-state')])).toEqual([
      ['mocks', 'passed'], ['build', 'passed'], ['review', 'current'], ['merged', 'ahead'], ['released', 'ahead'],
    ])
    expect(steps[2]).toHaveAttribute('aria-current', 'step')
    expect(steps[2]).toHaveTextContent('PR #12')
    // Who held it, from the Timeline read's lines: the story was made inside the window, so the work time is whole.
    const held = await within(view).findByTestId('board-story-held')
    // Nobody had it between its creation and its worker's bind, so that is said too.
    expect([...held.querySelectorAll('li')].map(li => li.textContent)).toEqual(['worker 4h', 'GitHub 52m', 'Conductor 8m', 'you 0', 'nobody 1h'])
    expect(steps[1]).toHaveTextContent('Worker G · This machine · 4h of work')
    expect(within(view).queryByTestId('board-story-held-window')).toBeNull()
    expect([...within(view).getByTestId('board-story-lane').querySelectorAll('[data-span]')].map(s => s.getAttribute('data-span'))).toEqual(['idle', 'working', 'conductor', 'idle'])
    expect(within(view).getByTestId('board-story-time')).toHaveTextContent('6h used of 8h')
    expect(within(view).getByTestId('board-story-subtasks')).toHaveTextContent('none')
    expect(within(view).getByTestId('board-story-epic')).toHaveTextContent('Edits alone')
    expect(within(view).getByTestId('board-story-last-report').querySelector('q')).toHaveTextContent('Rows fixed, PR is up.')
    expect(api.crewBoardTimeline).toHaveBeenCalledWith('root-s', 12)
  })

  it('says "no record" where the ledger keeps nothing, and offers no chat a story does not have', async () => {
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Decor list' }))
    const view = await screen.findByTestId('board-story-view')
    expect(within(view).queryByTestId('board-worker-door')).toBeNull()
    expect(within(view).getByTestId('board-story-time')).toHaveTextContent('45m used · estimate: no record')
    expect(within(view).getByTestId('board-story-last-report')).toHaveTextContent('no record')
    expect(within(view).getByTestId('board-story-epic')).toHaveTextContent('none')
    const github = (await within(view).findByTestId('board-story-held')).querySelector('[data-held="github"]')
    expect(github).toHaveTextContent('GitHub no record')
    expect(github).toHaveAttribute('data-none', 'true')
  })

  it('keeps the chat one press away: Open the chat opens the worker the click opened before', async () => {
    const store = renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Vignette rows' }))
    // The click opened the story view, not the chat.
    expect(store.getState().chat.activeSlot).not.toBe('w-g')
    fireEvent.click(await screen.findByRole('button', { name: 'Open the chat' }))
    expect(store.getState().chat.activeSlot).toBe('w-g')
  })

  it('a closed worker chat is reopened from its transcript first, as before', async () => {
    api.crewWorkers.mockResolvedValue({ root: 'root-s', workers: [W('w-g', 'Worker G', 'closed', 'it_g')] })
    api.resumeChatSlot.mockResolvedValueOnce({ ok: true, key: 'w-g', messages: [] })
    const store = renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Vignette rows' }))
    // The button says it reopens it.
    fireEvent.click(within(await screen.findByTestId('board-story-view')).getByRole('button', { name: 'Reopen the chat' }))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('w-g'))
    expect(api.resumeChatSlot).toHaveBeenCalled()
  })

  it('works from the keyboard and names every control for a screen reader', async () => {
    const user = userEvent.setup()
    renderPanel()
    const title = await screen.findByRole('button', { name: 'Vignette rows' })
    title.focus()
    await user.keyboard('{Enter}')
    // The reader lands on the story's own heading.
    const heading = await screen.findByRole('heading', { level: 2, name: 'Vignette rows' })
    expect(heading).toHaveFocus()
    expect(screen.getByRole('list', { name: 'Left to land' })).toBeInTheDocument()
    expect(screen.getAllByRole('img', { name: 'Step reached' })).toHaveLength(2)
    expect(screen.getAllByRole('img', { name: 'Step ahead' })).toHaveLength(2)
    // The step it is at says who must act, as its row's mark does.
    expect(within(screen.getByRole('list', { name: 'Left to land' })).getByRole('img', { name: 'Waits on GitHub' })).toBeInTheDocument()
    // From the heading, Tab reaches Open the chat; back up past the heading is the crumb.
    await user.tab()
    expect(screen.getByRole('button', { name: 'Open the chat' })).toHaveFocus()
    await user.tab({ shift: true })
    // The Board's separator keeps a no-break space before its dot (`PART_SEP`).
    expect(screen.getByRole('button', { name: 'Lantern\xa0· Review, back to the Board' })).toHaveFocus()
    await user.keyboard('{Enter}')
    // Back on the Board, on the row it left from.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Vignette rows' })).toHaveFocus())
    expect(screen.queryByTestId('board-story-view')).toBeNull()
  })

  it('back finds the Board as it was left: its view, its Timeline window and its scroll', async () => {
    api.crewBoardTimeline.mockImplementation((_root: string, hours: number) => Promise.resolve({ ...TIMELINE, since: ago(hours * 60), hours }))
    renderPanel()
    fireEvent.click(await screen.findByTestId('conductor-mode-view-timeline'))
    fireEvent.click(await screen.findByTestId('board-timeline-window-24'))
    await waitFor(() => expect(screen.getByTestId('board-timeline')).toHaveAttribute('data-hours', '24'))
    const box = screen.getByTestId('conductor-mode-board')
    box.scrollTop = 400
    fireEvent.click(await screen.findByRole('button', { name: 'Vignette rows' }))
    await screen.findByTestId('board-story-view')
    fireEvent.click(screen.getByTestId('board-story-back'))
    expect(screen.queryByTestId('board-story-view')).toBeNull()
    expect.soft(screen.getByTestId('conductor-mode-crew-panel')).toHaveAttribute('data-view', 'timeline')
    expect.soft(screen.getByTestId('board-timeline')).toHaveAttribute('data-hours', '24')
    expect.soft(screen.getByTestId('conductor-mode-board')).toBe(box)
    expect.soft(screen.getByTestId('conductor-mode-board').scrollTop).toBe(400)
  })

  it('keeps the Board hidden under an open story: out of the tab order and named to no reader', async () => {
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Vignette rows' }))
    await screen.findByTestId('board-story-view')
    expect(screen.getByTestId('conductor-mode-board-pane')).toHaveAttribute('hidden')
    expect(screen.getByTestId('conductor-mode-board-pane')).not.toBeVisible()
    expect(screen.queryByRole('radio', { name: 'Timeline' })).toBeNull()
    // The Board row behind the view is named to no reader: its title button is not found.
    expect(screen.queryAllByRole('button', { name: 'Vignette rows' })).toHaveLength(0)
  })

  it('Escape goes back, the focus on the story title it was opened from, and reaches nothing behind it', async () => {
    const user = userEvent.setup()
    const behind = vi.fn()
    document.addEventListener('keydown', behind)
    try {
      renderPanel()
      await user.click(await screen.findByRole('button', { name: 'Vignette rows' }))
      expect(await screen.findByRole('heading', { level: 2, name: 'Vignette rows' })).toHaveFocus()
      await user.keyboard('{Escape}')
      expect(screen.queryByTestId('board-story-view')).toBeNull()
      await waitFor(() => expect(screen.getByRole('button', { name: 'Vignette rows' })).toHaveFocus())
      expect(behind).not.toHaveBeenCalledWith(expect.objectContaining({ key: 'Escape' }))
    } finally {
      document.removeEventListener('keydown', behind)
    }
  })

  it('a story made under a minute ago has used 0, never "1m"', async () => {
    const NEW = ITEM({ item_id: 'it_n', title: 'New rows', status: null, last_report_at: null, has_worker: false, created_at: new Date(NOW - 20_000).toISOString(), plan: { level: 'story', step: 'todo' } })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: [...BOARD.items, NEW] })
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'New rows' }))
    expect(await screen.findByTestId('board-story-time')).toHaveTextContent(/^0 used · estimate: no record$/)
  })

  describe('names its worker, where it ran, and the way to its chat', () => {
    afterEach(() => closeCrewWindow())
    const LAB = { remote_crew: 'Lab crew' }
    const openStory = async (title = 'Vignette rows') => {
      fireEvent.click(await screen.findByRole('button', { name: title }))
      return screen.findByTestId('board-story-view')
    }

    it('a live chat on this machine: who and where, and Open the chat opens it', async () => {
      const store = renderPanel()
      const view = await openStory()
      expect(within(view).getByTestId('board-story-worker')).toHaveTextContent('Worker G · This machine')
      fireEvent.click(within(view).getByRole('button', { name: 'Open the chat' }))
      expect(store.getState().chat.activeSlot).toBe('w-g')
      expect(api.resumeChatSlot).not.toHaveBeenCalled()
    })

    it('a closed chat on this machine: Reopen the chat reopens it from its saved transcript', async () => {
      api.crewWorkers.mockResolvedValue({ root: 'root-s', workers: [W('w-g', 'Worker G', 'closed', 'it_g')] })
      api.resumeChatSlot.mockResolvedValueOnce({ ok: true, key: 'w-g', messages: [] })
      const store = renderPanel()
      const view = await openStory()
      expect(within(view).getByTestId('board-story-worker')).toHaveTextContent('Worker G · This machine')
      expect(within(view).queryByRole('button', { name: 'Open the chat' })).toBeNull()
      fireEvent.click(within(view).getByRole('button', { name: 'Reopen the chat' }))
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('w-g'))
      expect(api.resumeChatSlot).toHaveBeenCalledWith('dashboard_w-g', 'Worker G')
    })

    it('a live chat on a linked crew: Open the chat opens it in the crew window', async () => {
      api.crewWorkers.mockResolvedValue({ root: 'root-s', workers: [{ ...W('w-g', 'Worker G', 'idle', 'it_g'), ...LAB, remote_instance: 'inst-1', remote_slot: 'peer-g' }] })
      const store = renderPanel()
      const view = await openStory()
      expect(within(view).getByTestId('board-story-worker')).toHaveTextContent('Worker G · Lab crew')
      fireEvent.click(within(view).getByRole('button', { name: 'Open the chat' }))
      expect(currentCrewWindow()).toEqual({ instanceId: 'inst-1', key: 'peer-g' })
      expect(store.getState().chat.activeSlot).not.toBe('w-g')
    })

    it('a chat closed on a linked crew: no button, a line that says it cannot be opened from here', async () => {
      // Closed on its crew, so the crew list names no chat there to open.
      api.crewWorkers.mockResolvedValue({ root: 'root-s', workers: [{ ...W('w-g', 'Worker G', 'closed', 'it_g'), ...LAB }] })
      renderPanel()
      const view = await openStory()
      expect(within(view).getByTestId('board-story-worker')).toHaveTextContent('Worker G · Lab crew')
      expect(within(view).queryByTestId('board-worker-door')).toBeNull()
      expect(within(view).getByTestId('board-worker-door-none')).toHaveTextContent('Its chat is closed on Lab crew and cannot be opened from here.')
    })

    it('a bound worker the crew list has no row for: a plain "worker chat", where the board says it ran, and why', async () => {
      api.crewWorkers.mockResolvedValue({ root: 'root-s', workers: [] })
      api.crewBoard.mockResolvedValue({ ...BOARD, items: [...BOARD.items, ITEM({ item_id: 'it_x', title: 'Kettle rows', status: 'done', alive: 'closed', runs_on: 'Lab crew', decided_at: ago(20), in_backlog: false, plan: { level: 'story', step: 'build' } })] })
      renderPanel()
      const view = await openStory('Kettle rows')
      expect(within(view).getByTestId('board-story-worker')).toHaveTextContent('worker chat · Lab crew')
      expect(within(view).getByTestId('board-worker-door-none')).toHaveTextContent('Its chat is closed on Lab crew and cannot be opened from here.')
    })

    it('no worker ever bound: no button, and the line says no worker yet', async () => {
      renderPanel()
      const view = await openStory('Decor list')
      expect(within(view).getByTestId('board-story-worker')).toHaveTextContent('No worker yet')
      expect(within(view).getByTestId('board-story-worker')).toHaveAttribute('data-bound', 'false')
      expect(within(view).queryByTestId('board-worker-door')).toBeNull()
      expect(within(view).queryByTestId('board-worker-door-none')).toBeNull()
    })

    it("each move's row carries the same door, and it opens the chat without the story view", async () => {
      api.crewWorkers.mockResolvedValue({ root: 'root-s', workers: [W('w-g', 'Worker G', 'closed', 'it_g')] })
      api.resumeChatSlot.mockResolvedValueOnce({ ok: true, key: 'w-g', messages: [] })
      const store = renderPanel()
      const row = (await screen.findAllByTestId('board-story')).find(r => r.getAttribute('data-item') === 'it_g') as HTMLElement
      fireEvent.click(within(row).getByRole('button', { name: 'Reopen the chat' }))
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('w-g'))
      expect(screen.queryByTestId('board-story-view')).toBeNull()
      // A story nobody was ever bound to has no door on its row.
      const fresh = (await screen.findAllByTestId('board-story')).find(r => r.getAttribute('data-item') === 'it_f') as HTMLElement
      expect(within(fresh).queryByTestId('board-worker-door')).toBeNull()
    })
  })

  it("the hidden run line's computer counts read nothing under an open story, and read again on back", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    renderPanel(qc)
    const seats = () => qc.getQueryCache().find({ queryKey: crewBoardCapacityQueryKey })
    await waitFor(() => expect(seats()?.isActive()).toBe(true))
    fireEvent.click(await screen.findByRole('button', { name: 'Vignette rows' }))
    await screen.findByTestId('board-story-view')
    expect(seats()?.isActive()).toBe(false)
    fireEvent.click(screen.getByTestId('board-story-back'))
    expect(seats()?.isActive()).toBe(true)
  })
})
