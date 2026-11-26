/**
 * The Board's Timeline, through the conductor mode panel: the third position of the
 * switch draws one lane per story from the ledger's own lines, the Conductor's lane
 * ticks on its writes and names its longest quiet stretch, the window chips re-read
 * 6, 12 or 24 hours, and the legend says the rule a red stretch follows. The clock
 * is pinned and every stamp is explicit, so no assertion depends on when it runs.
 */
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'

const api = vi.hoisted(() => ({
  crewWorkers: vi.fn(), resumeChatSlot: vi.fn(), crewBoard: vi.fn(), crewBoardAction: vi.fn(),
  sessionSummary: vi.fn(), crewBoardTimeline: vi.fn(),
}))
vi.mock('../../../api/client', () => ({ api }))
vi.mock('../../../hooks/usePanelTabs', () => ({ openPanelView: vi.fn() }))

import ConductorModeCrewPanel from './ConductorModeCrewPanel'
import type { WorkBoardItem } from '../../../api/crewBoard'
import type { TimelineLine, TimelineResponse } from '../../../api/boardTimeline'
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
  ITEM({ item_id: 'it_done', title: 'Ship the fix', status: 'done', summary: 'Worker finished.', last_report_at: ago(50) }),
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
    // Reported done, nobody judged it: its Conductor's stretch, drawn late.
    expect(spansOf('it_done')).toEqual([
      { kind: 'working', late: false }, { kind: 'conductor', late: true },
    ])
    // A row the board says waits on the person: its last stretch is theirs.
    expect(spansOf('it_ask')).toEqual([
      { kind: 'working', late: false }, { kind: 'person', late: false },
    ])
  })

  it('places each stretch by time, so a bar says when and not how far along', async () => {
    await renderTimeline()
    // The late wait began 5 of the 12 hours ago: 7/12 along the axis, to its end.
    const conductorSpan = laneOf('it_done').querySelector('[data-span="conductor"]') as HTMLElement
    expect(parseFloat(conductorSpan.style.left)).toBeCloseTo((7 / 12) * 100, 6)
    expect(parseFloat(conductorSpan.style.width)).toBeCloseTo((5 / 12) * 100, 6)
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
    expect(gap.className).toContain('crew-tone-danger')
    expect(lane.querySelector('[data-testid="board-timeline-gap-line"]')).not.toBeNull()
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
    expect(legend.querySelectorAll('li')).toHaveLength(6)
    expect(legend).toHaveTextContent(String(LATE_MINUTES))
    expect(legend.querySelector('[data-span="conductor"][data-late]')).not.toBeNull()
  })

  it('keeps the axis and the Conductor lane when no story is open', async () => {
    await renderTimeline([], { ...TIMELINE, events: {} })
    expect(screen.getByTestId('board-timeline-empty')).toBeInTheDocument()
    expect(screen.getByTestId('board-timeline-now')).toBeInTheDocument()
    // Nothing written in the window: no gap to name.
    expect(document.querySelector('[data-testid="board-timeline-gap"]')).toBeNull()
  })
})
