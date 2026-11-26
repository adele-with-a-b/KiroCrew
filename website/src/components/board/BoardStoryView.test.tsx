import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), resumeChatSlot: vi.fn(), crewBoard: vi.fn(), crewBoardTimeline: vi.fn(), sessionSummary: vi.fn() }))
vi.mock('../../api/client', () => ({ api }))
vi.mock('../../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))

import ConductorModeCrewPanel from '../../pages/chat/command-center/ConductorModeCrewPanel'
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

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
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
    expect(screen.queryByTestId('conductor-mode-board')).toBeNull()
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
    expect(steps[1]).toHaveTextContent('Worker G · 4h of work')
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
    expect(within(view).queryByTestId('board-story-open-chat')).toBeNull()
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
    fireEvent.click(await screen.findByRole('button', { name: 'Open the chat' }))
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
})
