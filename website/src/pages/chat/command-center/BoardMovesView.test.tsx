/**
 * The Board's Moves and Steps views, through the conductor mode panel: sections by
 * who must act in a fixed order, an empty one kept as one quiet line, the
 * Conductor's move red from 10 minutes on a pinned clock, one row shape with the
 * machine read from the board, Up next ranked with its fold, and Steps regrouping
 * the same rows under a count strip.
 */
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({ crewWorkers: vi.fn(), resumeChatSlot: vi.fn(), crewBoard: vi.fn(), crewBoardAction: vi.fn(), sessionSummary: vi.fn() }))
vi.mock('../../../api/client', () => ({ api }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))

import ConductorModeCrewPanel from './ConductorModeCrewPanel'
import type { WorkBoardItem } from '../../../api/crewBoard'

const NOW = Date.parse('2026-11-25T12:00:00Z')
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString()

const ITEM = (over: Partial<WorkBoardItem>): WorkBoardItem => ({
  schema: 1, item_id: 'it_a', title: 'Story', acceptance: { kind: 'human_approval' }, state: 'open', verdict: null,
  decision: '', round: 1, fails: 0, status: 'progress', summary: '', artifacts: {}, pr: null,
  last_report_at: ago(5), created_at: ago(120), closed_at: null, decided_at: null,
  orphaned: false, stale: false, acceptance_concrete: true, outstanding: false, terminal: false, alive: 'idle', events: [],
  reason: null, awaits_person: false, has_worker: true, in_backlog: false, ...over,
} as WorkBoardItem)

const BOARD = { conductor: { schema: 1, slot_key: 'root-1', goal: 'Plan the launch.', round: 1, depth: 0, parent_item: null, created_at: '' }, conductor_alive: 'idle', take_over_available: false }

const ITEMS = [
  ITEM({ item_id: 'it_ask', title: 'Pick a tea', status: 'question', reason: 'needs_human', awaits_person: true, outstanding: true, summary: 'Oolong or green?', last_report_at: ago(18) }),
  ITEM({ item_id: 'it_stuck', title: 'Port the gate', stale: true, summary: 'Waits for a test slot.', last_report_at: ago(72) }),
  ITEM({ item_id: 'it_done', title: 'Ship the fix', status: 'done', summary: 'Worker finished.', last_report_at: ago(50) }),
  ITEM({ item_id: 'it_pr', title: 'Fix the rows', pr: 116, summary: 'Rows fixed, PR is up.', last_report_at: ago(31), runs_on: 'host-b' }),
  ITEM({ item_id: 'it_run', title: 'Draft the menu', alive: 'running', summary: 'Fixing review rows', last_report_at: ago(38), runs_on: 'host-a', plan: { tags: ['est: 3 h'], step: 'review' } }),
  ITEM({ item_id: 'it_n1', title: 'Join the lists', status: null, last_report_at: null, has_worker: false, in_backlog: true, plan: { rank: 1, tags: ['est: 2 h'] } }),
  ITEM({ item_id: 'it_n2', title: 'Check the files', status: null, last_report_at: null, has_worker: false, in_backlog: true, plan: { rank: 2, tags: ['est: 45 m'] } }),
  ITEM({ item_id: 'it_n3', title: 'Trim the copy', status: null, last_report_at: null, has_worker: false, in_backlog: true, plan: { rank: 3, tags: ['est: 3 h'] } }),
  ITEM({ item_id: 'it_n4', title: 'Fourth', status: null, last_report_at: null, has_worker: false, in_backlog: true, plan: { rank: 4, tags: ['est: 1 h'] } }),
  ITEM({ item_id: 'it_m', title: 'Landed one', state: 'accepted', terminal: true, plan: { step: 'merged' } }),
]

function pinClock(at = NOW) {
  // Only Date is faked: the ages read the pinned clock, the query timers stay real.
  vi.useFakeTimers({ toFake: ['Date'] })
  onTestFinished(() => vi.useRealTimers())
  vi.setSystemTime(at)
}

function renderPanel(items: WorkBoardItem[] = ITEMS, board: typeof BOARD = BOARD) {
  api.crewBoard.mockResolvedValue({ ...board, items })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots: [] } } as never)
  render(<QueryClientProvider client={qc}><Provider store={store}><ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} /></Provider></QueryClientProvider>)
}

const section = (id: string) => document.querySelector(`[data-section="${id}"]`) as HTMLElement
const rowOf = (id: string) => document.querySelector(`[data-item="${id}"][data-move]`) as HTMLElement

describe('Board Moves', () => {
  beforeEach(() => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [{ key: 'w-run', title: 'menu-worker', agent: 'kirocrew-worker', status: 'working', group: 'working', last_activity: '', ledger_item: 'it_run', created_by: 'root-1' }] })
    api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
  })
  afterEach(() => vi.clearAllMocks())

  it('cuts the stories into sections by who must act, in the order that hurts most', async () => {
    pinClock()
    renderPanel()
    const moves = await screen.findByTestId('board-moves')
    expect([...moves.querySelectorAll('[data-section]')].map(s => [s.getAttribute('data-section'), s.getAttribute('data-count')])).toEqual([
      ['needs_you', '1'], ['stuck', '1'], ['conductor', '1'], ['github', '1'], ['working', '1'], ['up_next', '4'],
    ])
    expect(rowOf('it_ask').closest('[data-section]')).toBe(section('needs_you'))
    expect(rowOf('it_stuck').closest('[data-section]')).toBe(section('stuck'))
    expect(rowOf('it_done').closest('[data-section]')).toBe(section('conductor'))
    expect(rowOf('it_pr').closest('[data-section]')).toBe(section('github'))
    expect(rowOf('it_run').closest('[data-section]')).toBe(section('working'))
    // Landed work is no row here.
    expect(screen.queryByText('Landed one')).toBeNull()
    // The section heads say their rule.
    expect(within(section('stuck')).getByTestId('board-section-hint')).toHaveTextContent('nobody is moving it')
    expect(within(section('conductor')).getByTestId('board-section-hint')).toHaveTextContent('red after 10m')
  })

  it('keeps an empty section as one quiet line', async () => {
    pinClock()
    renderPanel(ITEMS.filter(i => i.item_id !== 'it_stuck'))
    await screen.findByTestId('board-moves')
    const stuck = section('stuck')
    expect(stuck).toHaveAttribute('data-empty', 'true')
    expect(within(stuck).getByTestId('board-section-none')).toHaveTextContent('none')
    expect(within(stuck).queryAllByTestId('board-story')).toHaveLength(0)
  })

  it('turns the Conductor\'s move red at 10 minutes, and not one minute before', async () => {
    const done = ITEM({ item_id: 'it_done', title: 'Ship the fix', status: 'done', summary: 'Worker finished.', last_report_at: ago(0) })
    // The panel's own clock steps on a half-minute interval: fake it with Date, so
    // the line turns red with no new board read.
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
    onTestFinished(() => vi.useRealTimers())
    // A second short of ten minutes: 9m, not red.
    vi.setSystemTime(NOW + 10 * 60_000 - 1_000)
    renderPanel([done])
    let row = await screen.findByTestId('board-story')
    expect(within(row).getByTestId('board-row-age')).toHaveTextContent('9m')
    expect(row).not.toHaveAttribute('data-tone')
    expect(within(row).getByTestId('board-move-mark').className).not.toContain('danger')
    // The panel's next half-minute step lands past ten minutes.
    act(() => { vi.advanceTimersByTime(30_000) })
    row = screen.getByTestId('board-story')
    expect(within(row).getByTestId('board-row-age')).toHaveTextContent('10m')
    expect(row).toHaveAttribute('data-tone', 'danger')
    expect(within(row).getByTestId('board-row-age').className).toContain('crew-tone-danger')
    expect(within(row).getByTestId('board-move-mark').className).toContain('crew-tone-danger')
  })

  it('draws one row shape: mark, title, age at the right in even-width digits, why, and quiet facts', async () => {
    pinClock()
    renderPanel()
    await screen.findByTestId('board-moves')
    const run = rowOf('it_run')
    expect(within(run).getByTestId('board-row-title')).toHaveTextContent('Draft the menu')
    const age = within(run).getByTestId('board-row-age')
    expect(age).toHaveTextContent('38m of 3h')
    expect(age.className).toContain('tabular-nums')
    expect(within(run).getByTestId('board-row-why')).toHaveTextContent('In progress')
    // The worker from the crew list, the machine from the board read, never a literal.
    expect(within(run).getByTestId('board-fact-worker')).toHaveTextContent('menu-worker · host-a')
    expect(within(rowOf('it_pr')).getByTestId('board-fact-pr')).toHaveTextContent('PR #116')
    // Neutral rows carry no colour; only the person's and the late ones do.
    expect(run).not.toHaveAttribute('data-tone')
    expect(rowOf('it_ask')).toHaveAttribute('data-tone', 'warn')
    expect(rowOf('it_stuck')).toHaveAttribute('data-tone', 'danger')
    expect(document.body.textContent).not.toMatch(/%/)
  })

  it('reads a done report its Conductor handed back as Stuck on an unread board, and as a stage done once its chat closed', async () => {
    pinClock()
    // The Conductor's decision came after the done report: the worker's move.
    const back = (id: string, over: Partial<WorkBoardItem> = {}) => ITEM({ item_id: id, title: id, status: 'done', summary: 'Rows redone.', last_report_at: ago(40), decided_at: ago(30), ...over })
    renderPanel([back('it_gone', { alive: 'closed' }), back('it_unread', { orphaned: true }), back('it_here'), back('it_back_running', { alive: 'running', orphaned: true })])
    await screen.findByTestId('board-moves')
    // No main chat reads the board: nobody is on it, red as any stuck row.
    expect(rowOf('it_unread').closest('[data-section]')).toBe(section('stuck'))
    expect(rowOf('it_unread')).toHaveAttribute('data-tone', 'danger')
    // Its chat closed on a decided done, the item kept open: a stage done, waiting for its
    // Conductor to seat the next stage, never red.
    expect(rowOf('it_gone').closest('[data-section]')).toBe(section('conductor'))
    expect(rowOf('it_gone')).not.toHaveAttribute('data-tone')
    expect(within(rowOf('it_gone')).getByTestId('board-row-why')).toHaveTextContent('Stage done · waits for its next seat')
    // Its worker still there, or its turn running again: the worker's, Working.
    expect(rowOf('it_here').closest('[data-section]')).toBe(section('working'))
    expect(rowOf('it_back_running').closest('[data-section]')).toBe(section('working'))
  })

  it('keeps each separator with the part before it, so a wrapped line never starts with one', async () => {
    pinClock()
    renderPanel([
      ITEM({ item_id: 'it_d1', title: 'Done with all', status: 'done', summary: 'Gate ported, PR is up.', pr: 118, artifacts: { notes: 'https://example.test/n' }, last_report_at: ago(2) }),
      ITEM({ item_id: 'it_d2', title: 'Done, a link', status: 'done', summary: 'Copy trimmed.', artifacts: { notes: 'https://example.test/n' }, last_report_at: ago(2) }),
      ITEM({ item_id: 'it_d3', title: 'Done, bare link', status: 'done', artifacts: { notes: 'https://example.test/n' }, last_report_at: ago(2) }),
      ITEM({ item_id: 'it_wait', title: 'Draft the menu', summary: 'All twelve drafted, checking the counts.', last_report_at: ago(3) }),
      ITEM({ item_id: 'it_run', title: 'Fix the rows', alive: 'running', summary: 'Fixing the last one.', runs_on: 'host-a' }),
      ...ITEMS.filter(i => i.in_backlog),
    // A seat cap, so the run line has two counts to separate.
    ], { ...BOARD, conductor: { ...BOARD.conductor, concurrency_cap: 4 } as typeof BOARD.conductor })
    await screen.findByTestId('board-moves')
    // Every part of a story line is its own wrap unit: none may start with a dot.
    const lines = [...document.querySelectorAll('[data-testid="conductor-mode-milestone"]')]
    expect(lines.length).toBe(5)
    for (const line of lines) {
      for (const part of line.children) expect(part.textContent?.trim().startsWith('·'), `${line.textContent}`).toBe(false)
    }
    const parts = (id: string) => [...within(rowOf(id)).getByTestId('conductor-mode-milestone').children].map(c => c.textContent)
    // The dot ends the part before it, behind a no-break space.
    const S = String.fromCharCode(0xa0) + '·'
    expect(parts('it_d1')).toEqual([`Done${S}`, `Gate ported, PR is up.${S}`, `PR #118${S}`, 'notes'])
    expect(parts('it_d2')).toEqual([`Done${S}`, `Copy trimmed.${S}`, 'notes'])
    expect(parts('it_d3')).toEqual([`Done${S}`, 'notes'])
    expect(parts('it_wait')).toEqual([`Waiting${S}`, 'All twelve drafted, checking the counts.'])
    // Inside one text, a no-break space before each dot: the break can fall only after it.
    for (const text of [
      within(rowOf('it_run')).getByTestId('board-row-why').textContent,
      within(rowOf('it_run')).getByTestId('board-fact-worker').textContent,
      screen.getByTestId('plan-sprint-counts').textContent,
      screen.getByTestId('board-up-next-more').textContent,
    ]) {
      expect(text, text ?? '').toMatch(/·/)
      expect(text, text ?? '').not.toMatch(/[^\xa0]·/)
    }
  })

  it('ranks Up next by number, folds past three with the count and the total, and opens on a click', async () => {
    pinClock()
    renderPanel()
    await screen.findByTestId('board-moves')
    const upNext = screen.getByTestId('plan-backlog')
    const ranks = () => within(upNext).getAllByTestId('board-up-next-row').map(r => [within(r).getByTestId('board-up-next-rank').textContent, within(r).getByTestId('board-row-title').textContent, within(r).queryByTestId('board-row-age')?.textContent])
    expect(ranks()).toEqual([['1', 'Join the lists', '2h'], ['2', 'Check the files', '45m'], ['3', 'Trim the copy', '3h']])
    expect(within(upNext).getByTestId('board-section-hint')).toHaveTextContent('ranked · drag to reorder')
    const more = within(upNext).getByTestId('board-up-next-more')
    expect(more).toHaveTextContent('1 more · 6.8h in all')
    expect(more).toHaveAttribute('aria-expanded', 'false')
    more.focus()
    fireEvent.click(more)
    expect(ranks().map(r => r[1])).toEqual(['Join the lists', 'Check the files', 'Trim the copy', 'Fourth'])
    // The same button stays, keeps the keyboard's focus and folds the list back.
    expect(within(upNext).getByTestId('board-up-next-more')).toBe(more)
    expect(more).toHaveFocus()
    expect(more).toHaveAttribute('aria-expanded', 'true')
    expect(more).toHaveTextContent('Show fewer')
    fireEvent.click(more)
    expect(ranks().map(r => r[1])).toEqual(['Join the lists', 'Check the files', 'Trim the copy'])
    expect(more).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(more)
    // Each row keeps its drag grip.
    expect(within(upNext).getAllByTestId('plan-backlog-grip')).toHaveLength(4)
    expect(api.crewBoardAction).not.toHaveBeenCalled()
  })
})

describe('Board Steps', () => {
  beforeEach(() => {
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [] })
    api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
  })
  afterEach(() => vi.clearAllMocks())

  it('regroups the same rows by step under a count strip, furthest along first, with landed as counts', async () => {
    pinClock()
    const items = ITEMS.map(i => (i.plan?.step || i.in_backlog || i.state !== 'open' ? i : { ...i, plan: { step: i.pr != null ? 'review' : 'build' } } as WorkBoardItem))
    renderPanel(items)
    fireEvent.click(await screen.findByTestId('conductor-mode-view-steps'))
    const steps = await screen.findByTestId('board-steps')
    const cells = within(steps).getAllByTestId('board-step-cell').map(c => [c.getAttribute('data-step'), c.querySelector('dd')?.textContent])
    expect(cells).toEqual([['todo', '0'], ['mocks', '0'], ['build', '3'], ['review', '2'], ['merged', '1'], ['released', '0']])
    // A reader hears each term before its count, and the strip by its name.
    const cell = within(steps).getAllByTestId('board-step-cell')[0]
    expect([...cell.children].map(c => c.tagName)).toEqual(['DT', 'DD'])
    expect(screen.getByRole('group', { name: 'Stories per step' })).toContainElement(within(steps).getByTestId('board-step-strip'))
    const groups = within(steps).getAllByTestId('board-step-section')
    expect(groups.map(g => [g.getAttribute('data-section'), g.getAttribute('data-count')])).toEqual([['review', '2'], ['build', '3'], ['mocks', '0'], ['todo', '0']])
    // Inside a step, who must act first: the person's ask leads Build.
    expect(within(groups[1]).getAllByTestId(/^(board-story|conductor-mode-person-ask)$/).map(r => r.getAttribute('data-item'))).toEqual(['it_ask', 'it_stuck', 'it_done'])
    // The mark still says who must act, by name.
    expect(within(groups[1]).getAllByTestId('board-move-mark').map(m => m.getAttribute('aria-label'))).toEqual(['Needs you', 'Stuck', "Conductor's move"])
    expect(within(steps).getByTestId('board-landed-merged')).toHaveTextContent(/^Merged1$/)
    expect(within(steps).getByTestId('board-landed-released')).toHaveTextContent(/^Released0 of 1$/)
    expect(within(steps).getByTestId('plan-backlog')).toHaveAttribute('data-count', '4')
  })
})
