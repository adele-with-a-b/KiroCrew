/**
 * The Board's Timeline, through the conductor mode panel: the third position of the
 * switch draws one lane per story from the ledger's own lines, the Conductor's lane
 * ticks on its writes and names its longest quiet stretch, the window chips re-read
 * 6, 12 or 24 hours, and the legend says the rule a red stretch follows. The clock
 * is pinned and every stamp is explicit, so no assertion depends on when it runs.
 */
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'
import { fmtMinutes } from '../../../lib/crewWork'
import { fmtUnit } from '../../../i18n/format'

const api = vi.hoisted(() => ({
  crewWorkers: vi.fn(), resumeChatSlot: vi.fn(), crewBoard: vi.fn(), crewBoardAction: vi.fn(),
  sessionSummary: vi.fn(), crewBoardTimeline: vi.fn(),
}))
vi.mock('../../../api/client', () => ({ api }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))

import ConductorModeCrewPanel from './ConductorModeCrewPanel'
import { crewBoardQueryKey, type WorkBoardItem } from '../../../api/crewBoard'
import { i18nT } from '../../../i18n/t'
import { boardTimelineQueryKey, type TimelineLine, type TimelineResponse } from '../../../api/boardTimeline'
import { LATE_MINUTES } from '../../../lib/boardTimeline'

const NOW = Date.parse('2026-11-25T12:00:00Z')
const HOUR = 3_600_000
const stamp = (h: number) => new Date(NOW - h * HOUR).toISOString()
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString()

const ITEM = (over: Partial<WorkBoardItem>): WorkBoardItem => ({
  schema: 1, item_id: 'it_a', title: 'Story', acceptance: { kind: 'human_approval' }, state: 'open', verdict: null,
  decision: '', round: 1, fails: 0, status: 'progress', summary: '', artifacts: {}, pr: null,
  last_report_at: ago(5), created_at: stamp(20), closed_at: null, decided_at: null,
  orphaned: false, stale: false, acceptance_concrete: true, outstanding: false, terminal: false, alive: 'idle', events: [],
  reason: null, awaits_person: false, has_worker: true, in_backlog: false, ...over,
} as WorkBoardItem)

const BOARD = { conductor: { schema: 1, slot_key: 'root-1', goal: 'Plan the launch.', round: 1, depth: 0, parent_item: null, created_at: '' }, conductor_alive: 'idle', take_over_available: false }

const ITEMS = [
  ITEM({ item_id: 'it_run', title: 'Draft the menu', alive: 'running', summary: 'Fixing review rows', last_report_at: ago(38), runs_on: 'host-a' }),
  ITEM({ item_id: 'it_done', title: 'Ship the fix', status: 'done', summary: 'Worker finished.', last_report_at: stamp(5) }),
  ITEM({ item_id: 'it_ask', title: 'Pick a tea', status: 'question', reason: 'needs_human', awaits_person: true, outstanding: true, summary: 'Oolong or green?', last_report_at: ago(18) }),
]

const line = (kind: TimelineLine['kind'], h: number, status: TimelineLine['status'] = null): TimelineLine =>
  ({ ts: stamp(h), kind, status })

/**
 * One ledger line per story, as the route sends them: times and kinds, no text.
 *
 * Every sequence is one the ledger can actually hold. Two CONSECUTIVE `progress`
 * reports never appear in a log -- the store coalesces them and keeps the newest
 * (`work_ledger_store._coalesce_progress`) -- so a long run shows one diamond here,
 * as it does on a real Board.
 */
const EVENTS: Record<string, TimelineLine[]> = {
  // Bound early, steered mid-run, reported progress since: its worker all along.
  it_run: [line('bind', 11), line('steer', 6), line('report', 2, 'progress')],
  // Reported done five hours ago and nobody has judged it: its Conductor's move, late.
  it_done: [line('bind', 10), line('report', 5, 'done')],
  // Asked the person two hours ago.
  it_ask: [line('bind', 9), line('report', 2, 'question')],
}

const TIMELINE: TimelineResponse = {
  read_at: new Date(NOW).toISOString(),
  since: stamp(12),
  hours: 12,
  events: EVENTS,
}

function pinClock(at = NOW) {
  vi.useFakeTimers({ toFake: ['Date'] })
  onTestFinished(() => vi.useRealTimers())
  vi.setSystemTime(at)
}

async function renderTimeline(items: WorkBoardItem[] = ITEMS, timeline: TimelineResponse = TIMELINE) {
  api.crewBoard.mockResolvedValue({ ...BOARD, items })
  api.crewBoardTimeline.mockImplementation(async (_root: string, hours: number) => ({ ...timeline, hours }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore({ dashboard: { slots: [] } } as never)
  render(<QueryClientProvider client={qc}><Provider store={store}><ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} /></Provider></QueryClientProvider>)
  await screen.findByTestId('conductor-mode-view-timeline')
  await act(async () => { fireEvent.click(screen.getByTestId('conductor-mode-view-timeline')) })
  return screen.findByTestId('board-timeline')
}

/** A bar's place as a number of percent. The view writes `calc(N * 1%)`, which the
 *  DOM keeps as `calc(N%)` with N to four places. */
const pctOf = (css: string) => Number(/^calc\(([-\d.e]+)(?: \* 1)?%\)$/.exec(css)?.[1] ?? NaN)

const laneOf = (item: string) =>
  (document.querySelector(`[data-item="${item}"][data-move]`) as HTMLElement).parentElement as HTMLElement
const spansOf = (item: string) =>
  Array.from(laneOf(item).querySelectorAll('[data-span]')).map(el => ({
    kind: el.getAttribute('data-span'), late: el.hasAttribute('data-late'),
  }))

describe('Board Timeline', () => {
  beforeEach(() => {
    pinClock()
    api.crewWorkers.mockResolvedValue({ root: 'root-1', workers: [{ key: 'w-run', title: 'menu-worker', agent: 'kirocrew-worker', status: 'working', group: 'working', last_activity: '', ledger_item: 'it_run', created_by: 'root-1' }] })
    api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
  })
  afterEach(() => vi.clearAllMocks())

  it('draws one lane per story, each stretch from the ledger lines of the window', async () => {
    const view = await renderTimeline()
    expect(view).toHaveAttribute('data-hours', '12')
    // The same rows as the other views, in the same order: who must act, then the
    // one waiting longest.
    expect(Array.from(document.querySelectorAll('[data-testid="board-timeline-story"]')).map(r => r.getAttribute('data-item')))
      .toEqual(['it_ask', 'it_done', 'it_run'])
    // Its worker has it, from the bind through the steer and the report, to the
    // read time: a steer and a progress report both leave it with its worker.
    expect(spansOf('it_run')).toEqual([
      { kind: 'working', late: false }, { kind: 'working', late: false }, { kind: 'working', late: false },
    ])
    // A diamond where a WORKER reported, and nowhere else: the steer is not one.
    expect(laneOf('it_run').querySelectorAll('[data-testid="board-timeline-report"]')).toHaveLength(1)
    // One report each, so one diamond each, across the three lanes.
    expect(document.querySelectorAll('[data-testid="board-timeline-report"]')).toHaveLength(3)
    // Reported done, nobody judged it: its Conductor's stretch, late from the rule on.
    expect(spansOf('it_done')).toEqual([
      { kind: 'working', late: false }, { kind: 'conductor', late: false }, { kind: 'conductor', late: true },
    ])
    // A row the board says waits on the person: its last stretch is theirs.
    expect(spansOf('it_ask')).toEqual([
      { kind: 'working', late: false }, { kind: 'person', late: false },
    ])
  })

  it('places each stretch by time, so a bar says when and not how far along', async () => {
    await renderTimeline()
    // The wait began 5 of the 12 hours ago: 7/12 along the axis, and it is late from
    // ten minutes after, to its end.
    const [waited, late] = Array.from(laneOf('it_done').querySelectorAll('[data-span="conductor"]')) as HTMLElement[]
    expect(pctOf(waited.style.left)).toBeCloseTo((7 / 12) * 100, 3)
    expect(pctOf(waited.style.width)).toBeCloseTo((LATE_MINUTES / 720) * 100, 3)
    expect(pctOf(late.style.left)).toBeCloseTo(((7 * 60 + LATE_MINUTES) / 720) * 100, 3)
    expect(pctOf(late.style.width)).toBeCloseTo(((5 * 60 - LATE_MINUTES) / 720) * 100, 3)
    // No percentage of "done" anywhere: the lanes carry no text at all.
    expect(laneOf('it_done').querySelector('[data-testid="board-timeline-lane"]')?.textContent).toBe('')
  })

  it('ticks the Conductor lane on its own writes and names its longest quiet stretch', async () => {
    await renderTimeline()
    const lane = screen.getByTestId('board-timeline-conductor')
    // Three binds. The three reports are the workers' and the steer is the
    // person's, so neither ticks: a tick is a move the Conductor itself made.
    expect(lane.querySelectorAll('[data-testid="board-timeline-tick"]')).toHaveLength(3)
    // Its last write was the 9 h bind, so it has been quiet for 9 hours — past the
    // rule, so the stretch is named in the tone that says so.
    const gap = within(lane).getByTestId('board-timeline-gap')
    expect(gap).toHaveTextContent('9')
    // Ship the fix has waited on it 5 of those hours: late, so the text and the line
    // are both red.
    expect(gap.className).toContain('crew-tone-danger')
    expect((lane.querySelector('[data-testid="board-timeline-gap-line"]') as HTMLElement).className).toContain('crew-tone-danger')
  })

  it('draws a long quiet stretch with nothing owed in it quiet, the text and the line alike', async () => {
    // A healthy long run: only Draft the menu, its worker on it all along.
    await renderTimeline([ITEMS[0]], { ...TIMELINE, events: { it_run: EVENTS.it_run } })
    const lane = screen.getByTestId('board-timeline-conductor')
    const gap = within(lane).getByTestId('board-timeline-gap')
    expect(gap).toHaveTextContent(fmtMinutes(660))
    expect(gap).not.toHaveAttribute('data-late')
    for (const el of [gap, lane.querySelector('[data-testid="board-timeline-gap-line"]') as HTMLElement]) {
      expect(el.className).not.toContain('crew-tone-danger')
      expect(el.className).toContain('text-muted')
    }
  })

  it('says the quiet stretch in whole minutes, as every age on the Board is said', async () => {
    // Read 26 s after the hour: the stretch since the 9 h bind is 540.43 minutes,
    // and the head must say the 540 that have passed, never a fraction of one.
    const at = NOW + 26_000
    pinClock(at)
    await renderTimeline(ITEMS, { ...TIMELINE, read_at: new Date(at).toISOString() })
    const gap = within(screen.getByTestId('board-timeline-conductor')).getByTestId('board-timeline-gap')
    expect(gap).toHaveTextContent(fmtMinutes(540))
    expect(gap.textContent).not.toMatch(/\d\.\d/)
  })

  it('re-reads the window the chips pick, and nothing else', async () => {
    const view = await renderTimeline()
    expect(api.crewBoardTimeline).toHaveBeenCalledWith('root-1', 12)
    await act(async () => { fireEvent.click(screen.getByTestId('board-timeline-window-24')) })
    expect(api.crewBoardTimeline).toHaveBeenCalledWith('root-1', 24)
    expect(view).toHaveAttribute('data-hours', '24')
    // The board read is not re-read for a window change: the rows are unchanged.
    expect(api.crewBoard).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('board-timeline-window-24')).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByTestId('board-timeline-window-12')).toHaveAttribute('aria-checked', 'false')
  })

  it('says the late rule in the head, and what every colour means', async () => {
    await renderTimeline()
    // The rule the red stretches follow, as text in the section head: ten minutes
    // with no move on it. A reader never has to infer it from a colour.
    expect(screen.getByTestId('board-timeline-rule')).toHaveTextContent(String(LATE_MINUTES))
    const legend = screen.getByTestId('board-timeline-legend')
    expect(legend.querySelectorAll('li')).toHaveLength(7)
    expect(legend.querySelector('[data-span="stuck"]')).not.toBeNull()
    expect(legend).toHaveTextContent(String(LATE_MINUTES))
    expect(legend.querySelector('[data-span="conductor"][data-late]')).not.toBeNull()
    // One red, one meaning: late. A stuck stretch is an empty outline, never red.
    const reds = Array.from(legend.querySelectorAll('[data-span]')).filter(s => s.className.includes('crew-tone-danger'))
    expect(reds).toEqual([legend.querySelector('[data-span="conductor"][data-late]')])
    expect(legend.querySelector('[data-span="stuck"]')?.className).toMatch(/\bborder\b/)
    expect(legend.querySelector('[data-span="stuck"]')?.className).not.toMatch(/\bbg-current\b/)
  })

  it('names a late gap over a longer quiet one, red, and draws it where it was', async () => {
    // 24 hours: the Conductor's longest gap, 5 h 48 m, owed nothing; a later, shorter
    // one held a done report it left 1 h 18 m, judged at 2 h.
    const at = (h: number, m = 0) => new Date(NOW - h * HOUR - m * 60_000).toISOString()
    const items = [
      ITEM({ item_id: 'it_quiet', title: 'Draft the menu', alive: 'running', last_report_at: at(1) }),
      // Judged pending at 2 h and waiting on its pull request since: nobody owes it now.
      ITEM({
        item_id: 'it_owed', title: 'Ship the fix', status: 'done', last_report_at: at(3, 18), verdict: 'pending', pr: 9,
        events: [
          { id: 'e1', ts: at(3, 18), item_id: 'it_owed', kind: 'report', status: 'done', text: '' },
          { id: 'e2', ts: at(2), item_id: 'it_owed', kind: 'verdict', status: null, text: '' },
        ],
      }),
    ]
    const events: Record<string, TimelineLine[]> = {
      it_quiet: [{ ts: at(20), kind: 'bind', status: null }, { ts: at(14, 12), kind: 'decision', status: null }, { ts: at(9), kind: 'decision', status: null }, { ts: at(1), kind: 'report', status: 'progress' }],
      it_owed: [{ ts: at(4), kind: 'bind', status: null }, { ts: at(3, 18), kind: 'report', status: 'done' }, { ts: at(2), kind: 'verdict', status: null, verdict: 'pending' }],
    }
    await renderTimeline(items, { ...TIMELINE, since: stamp(24), hours: 24, events })
    expect(document.querySelector('[data-item="it_owed"][data-move]')).toHaveAttribute('data-move', 'github')
    const lane = screen.getByTestId('board-timeline-conductor')
    const gap = within(lane).getByTestId('board-timeline-gap')
    expect(gap).toHaveAttribute('data-late', 'true')
    expect(gap).toHaveTextContent(i18nT('components.crewMode.board_timeline_gap_late', { age: fmtMinutes(120) }).replace(/\s+/g, ' '))
    expect(gap.className).toContain('crew-tone-danger')
    const drawn = lane.querySelector('[data-testid="board-timeline-gap-line"]') as HTMLElement
    expect(drawn.className).toContain('crew-tone-danger')
    expect(pctOf(drawn.style.left)).toBeCloseTo((20 / 24) * 100, 3)
    expect(pctOf(drawn.style.width)).toBeCloseTo((2 / 24) * 100, 3)
  })

  it('never makes a past wait on the person the Conductor\'s lateness', async () => {
    // Asked the person three hours ago, answered by their steer an hour later: the
    // item still carries the reason, so that hour was theirs, not the Conductor's.
    const items = [ITEM({ item_id: 'it_ask', title: 'Pick a tea', status: 'question', reason: 'needs_human', last_report_at: stamp(3), alive: 'running' })]
    await renderTimeline(items, { ...TIMELINE, events: { it_ask: [line('bind', 9), line('report', 3, 'question'), line('steer', 2)] } })
    expect(spansOf('it_ask')).toEqual([{ kind: 'working', late: false }, { kind: 'person', late: false }, { kind: 'working', late: false }])
    // The Conductor's 9 hours since its bind owed nobody anything: quiet.
    const gap = within(screen.getByTestId('board-timeline-conductor')).getByTestId('board-timeline-gap')
    expect(gap).not.toHaveAttribute('data-late')
    expect(gap.className).not.toContain('crew-tone-danger')
  })

  it('draws a Working row whose turn runs after a verdict as working, not as nobody\'s', async () => {
    const items = [ITEM({ item_id: 'it_done', title: 'Ship the fix', status: 'done', alive: 'running', verdict: 'fail', last_report_at: stamp(5) })]
    await renderTimeline(items, { ...TIMELINE, events: { it_done: [line('bind', 10), line('report', 5, 'done'), line('verdict', 3)] } })
    expect(document.querySelector('[data-item="it_done"][data-move]')).toHaveAttribute('data-move', 'working')
    expect(spansOf('it_done').at(-1)).toEqual({ kind: 'working', late: false })
  })

  it('moves only the open end of a bar on the next read, never redrawing it whole', async () => {
    // Bound before the window opened: its stretch starts at the window's edge, which
    // the next read moves. The bar stays the same element.
    const items = [ITEM({ item_id: 'it_run', title: 'Draft the menu', alive: 'running', last_report_at: stamp(13) })]
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    api.crewBoard.mockResolvedValue({ ...BOARD, items })
    api.crewBoardTimeline.mockResolvedValue({ ...TIMELINE, events: { it_run: [line('bind', 13)] } })
    const store = createTestStore({ dashboard: { slots: [] } } as never)
    render(<QueryClientProvider client={qc}><Provider store={store}><ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} /></Provider></QueryClientProvider>)
    await act(async () => { fireEvent.click(await screen.findByTestId('conductor-mode-view-timeline')) })
    await screen.findByTestId('board-timeline-lanes')
    const bar = laneOf('it_run').querySelector('[data-span]') as HTMLElement
    expect(pctOf(bar.style.left)).toBe(0)
    const hourAt = () => pctOf((screen.getAllByTestId('board-timeline-hour')[0]).style.left)
    const hourBefore = hourAt()
    const next = NOW + 60_000
    api.crewBoardTimeline.mockResolvedValue({ ...TIMELINE, read_at: new Date(next).toISOString(), since: new Date(next - 12 * HOUR).toISOString(), events: { it_run: [line('bind', 13)] } })
    await act(async () => { await qc.invalidateQueries({ queryKey: boardTimelineQueryKey('root-1', 12) }) })
    // The next read has landed once the hour marks have moved with the window.
    await waitFor(() => expect(hourAt()).not.toBe(hourBefore))
    expect(laneOf('it_run').querySelector('[data-span]')).toBe(bar)
    expect(pctOf(bar.style.left)).toBe(0)
  })

  it('draws a reported row whose worker runs again as working, as its Moves row says', async () => {
    // Reported done, then its worker's turn started again: Moves owes the Conductor
    // nothing (`conductorWaitMinutes` is null), so the bar is not red either.
    const items = [ITEM({ item_id: 'it_done', title: 'Ship the fix', status: 'done', alive: 'running', last_report_at: ago(50) })]
    await renderTimeline(items)
    expect(spansOf('it_done')).toEqual([
      { kind: 'working', late: false }, { kind: 'working', late: false },
    ])
  })

  it('draws a stuck row stuck, though its worker last reported progress', async () => {
    // Its worker's chat closed after a progress report: the ledger's last line says
    // working, the row says stuck, and the bar follows the row.
    const items = [ITEM({ item_id: 'it_run', title: 'Draft the menu', alive: 'closed', last_report_at: ago(38) })]
    await renderTimeline(items)
    expect(spansOf('it_run').at(-1)).toEqual({ kind: 'stuck', late: false })
  })

  it('draws a row that owes its Conductor as owed, though a re-bind came last', async () => {
    // Blocked, then bound again, nobody answered: the row owes the Conductor.
    const items = [ITEM({ item_id: 'it_done', title: 'Ship the fix', status: 'blocked', reason: null, last_report_at: stamp(3) })]
    await renderTimeline(items, { ...TIMELINE, events: { it_done: [line('report', 3, 'blocked'), line('bind', 1)] } })
    expect(document.querySelector('[data-item="it_done"][data-move]')).toHaveAttribute('data-move', 'conductor')
    // Late from ten minutes after its report, on through the re-bind to now.
    expect(spansOf('it_done')).toEqual([{ kind: 'conductor', late: false }, { kind: 'conductor', late: true }, { kind: 'conductor', late: true }])
  })

  it('marks a story closed in the window by how it ended, in words a screen reader says', async () => {
    const items = [
      ITEM({ item_id: 'it_kept', title: 'Wrote the notes', state: 'accepted', closed_at: stamp(3) }),
      ITEM({ item_id: 'it_dropped', title: 'Dropped the idea', state: 'abandoned', closed_at: stamp(2) }),
    ]
    await renderTimeline(items, { ...TIMELINE, events: {} })
    const marks = Array.from(document.querySelectorAll('[data-testid="board-timeline-closed-mark"]'))
    expect(marks.map(m => [m.getAttribute('data-ended'), m.getAttribute('aria-label')])).toEqual([
      ['not_done', i18nT('components.crewMode.board_landed_not_done')],
      ['accepted', i18nT('components.crewMode.board_landed')],
    ])
  })

  it('keeps the lane of a story closed in the window, ended at its close, and no older one', async () => {
    const items = [
      ...ITEMS,
      ITEM({ item_id: 'it_closed', title: 'Wrote the notes', state: 'accepted', closed_at: stamp(3) }),
      ITEM({ item_id: 'it_old', title: 'Old work', state: 'accepted', closed_at: stamp(14) }),
    ]
    await renderTimeline(items, { ...TIMELINE, events: { ...EVENTS, it_closed: [line('bind', 8), line('close', 3)] } })
    const rows = Array.from(document.querySelectorAll('[data-testid="board-timeline-closed"]'))
    expect(rows.map(r => r.getAttribute('data-item'))).toEqual(['it_closed'])
    const span = (rows[0].parentElement as HTMLElement).querySelector('[data-span]') as HTMLElement
    // Bound 8 h ago, closed 3 h ago: from 4/12 to 9/12 of the axis, nothing after.
    expect(span.getAttribute('data-span')).toBe('working')
    expect(pctOf(span.style.left)).toBeCloseTo((4 / 12) * 100, 3)
    expect(pctOf(span.style.width)).toBeCloseTo((5 / 12) * 100, 3)
    expect((rows[0].parentElement as HTMLElement).querySelectorAll('[data-span]')).toHaveLength(1)
  })

  it('says it is reading before the first window lands, and draws no empty lanes', async () => {
    let land: (value: TimelineResponse) => void = () => {}
    api.crewBoardTimeline.mockImplementationOnce(() => new Promise<TimelineResponse>(resolve => { land = resolve }))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: ITEMS })
    const store = createTestStore({ dashboard: { slots: [] } } as never)
    render(<QueryClientProvider client={qc}><Provider store={store}><ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} /></Provider></QueryClientProvider>)
    await act(async () => { fireEvent.click(await screen.findByTestId('conductor-mode-view-timeline')) })
    expect(await screen.findByTestId('board-timeline-loading')).toBeInTheDocument()
    expect(screen.queryByTestId('board-timeline-lanes')).toBeNull()
    await act(async () => { land(TIMELINE) })
    expect(await screen.findByTestId('board-timeline-lanes')).toBeInTheDocument()
    expect(screen.queryByTestId('board-timeline-loading')).toBeNull()
  })

  it('keeps the last window on screen while a new one is read', async () => {
    const view = await renderTimeline()
    await screen.findByTestId('board-timeline-lanes')
    let land: (value: TimelineResponse) => void = () => {}
    api.crewBoardTimeline.mockImplementationOnce(() => new Promise<TimelineResponse>(resolve => { land = resolve }))
    await act(async () => { fireEvent.click(screen.getByTestId('board-timeline-window-24')) })
    // The 12 h lanes stay, dimmed and marked busy, instead of blanking, and the head
    // still names the 12 hours they show, never the 24 not read yet.
    expect(spansOf('it_done')).toEqual([{ kind: 'working', late: false }, { kind: 'conductor', late: false }, { kind: 'conductor', late: true }])
    expect(view).toHaveAttribute('data-loading', 'true')
    const head = () => screen.getByTestId('board-timeline-head')
    // The matcher folds the received text's spaces, so the expected text is folded too.
    const windowOf = (hours: number) => i18nT('components.crewMode.board_timeline_window', { age: fmtUnit(hours, 'hour', { unitDisplay: 'long' }) }).replace(/\s+/g, ' ')
    expect(head()).toHaveTextContent(windowOf(12))
    const dimmed = screen.getByTestId('board-timeline-lanes').parentElement as HTMLElement
    expect(dimmed).toHaveAttribute('aria-busy', 'true')
    expect(dimmed.className).toContain('opacity-60')
    await act(async () => { land({ ...TIMELINE, hours: 24, since: stamp(24) }) })
    await waitFor(() => expect(view).not.toHaveAttribute('data-loading'))
    expect(head()).toHaveTextContent(windowOf(24))
    expect(dimmed).not.toHaveAttribute('aria-busy')
  })

  it('reads the window under its own key, so a crew push re-reading the board leaves it be', async () => {
    expect(boardTimelineQueryKey('root-1', 12).slice(0, crewBoardQueryKey('root-1').length))
      .not.toEqual(crewBoardQueryKey('root-1'))
    // What a push does: invalidate the board's key. The board re-reads; the window not.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    api.crewBoard.mockResolvedValue({ ...BOARD, items: ITEMS })
    api.crewBoardTimeline.mockResolvedValue(TIMELINE)
    const store = createTestStore({ dashboard: { slots: [] } } as never)
    render(<QueryClientProvider client={qc}><Provider store={store}><ConductorModeCrewPanel root="root-1" viewing="root-1" active onAct={vi.fn()} /></Provider></QueryClientProvider>)
    await act(async () => { fireEvent.click(await screen.findByTestId('conductor-mode-view-timeline')) })
    await screen.findByTestId('board-timeline-lanes')
    const [boards, windows] = [api.crewBoard.mock.calls.length, api.crewBoardTimeline.mock.calls.length]
    await act(async () => { await qc.invalidateQueries({ queryKey: crewBoardQueryKey('root-1') }) })
    expect(api.crewBoard.mock.calls.length).toBeGreaterThan(boards)
    expect(api.crewBoardTimeline.mock.calls.length).toBe(windows)
  })

  it('keeps the axis and the Conductor lane when no story is open', async () => {
    await renderTimeline([], { ...TIMELINE, events: {} })
    expect(screen.getByTestId('board-timeline-empty')).toBeInTheDocument()
    expect(screen.getByTestId('board-timeline-now')).toBeInTheDocument()
    // Nothing written in the window: no gap to name.
    expect(document.querySelector('[data-testid="board-timeline-gap"]')).toBeNull()
  })
})
