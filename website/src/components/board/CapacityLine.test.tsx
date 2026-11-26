/**
 * The right bar's seats strip: the seats each computer holds, under the goal, read
 * from every Conductor-mode project's board and the capacity read, so it says what
 * the all-projects page's tiles say. A count alone, never "N of M"; this computer's
 * room in red beside its count only when memory hurts; nothing read while the tab is
 * not on screen. The api client is mocked at the module boundary; the clock is pinned.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../test/helpers'
import type { WorkBoardItem, WorkBoardResponse } from '../../api/crewBoard'
import type { CrewBoardCapacity } from '../../api/crewBoardCapacity'
import { resetPollRule } from '../../lib/pollRule'

const crewBoard = vi.fn()
const crewBoardCapacity = vi.fn()

vi.mock('../../api/client', () => ({
  api: {
    crewBoard: (...args: unknown[]) => crewBoard(...args),
    crewBoardCapacity: (...args: unknown[]) => crewBoardCapacity(...args),
  },
}))

const NOW = Date.parse('2026-11-25T12:00:00Z')
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString()

function item(id: string, over: Partial<WorkBoardItem> = {}): WorkBoardItem {
  return {
    schema: 1, item_id: id, title: id, acceptance: {}, state: 'open', verdict: null, decision: '', round: 0, fails: 0,
    status: 'progress', summary: '', artifacts: {}, pr: null, last_report_at: ago(5), created_at: ago(60),
    closed_at: null, decided_at: null, orphaned: false, stale: false, acceptance_concrete: true, outstanding: false,
    terminal: false, alive: 'idle', events: [], has_worker: true, in_backlog: false, runs_on: 'box-here',
    ...over,
  }
}

function board(items: WorkBoardItem[]): WorkBoardResponse {
  return {
    conductor: { schema: 1, slot_key: 'k', goal: 'ship it', round: 1, depth: 0, parent_item: null, created_at: ago(120) },
    conductor_alive: 'idle', items, take_over_available: false,
  }
}

/** Two projects. Seats: two here in Alpha, one here in Beta, one on the linked Crew.
 *  Not seats: a closed chat, a backlog row with no worker, a closed story. */
const BOARDS: Record<string, WorkBoardResponse> = {
  'root-a': board([
    item('Here one'),
    item('Here two', { alive: 'running' }),
    item('On the crew', { runs_on: 'linked-crew', alive: 'running' }),
    item('Chat closed', { alive: 'closed' }),
    item('Backlog', { has_worker: false, in_backlog: true, alive: 'closed', status: null }),
  ]),
  'root-b': board([item('Beta here'), item('Landed', { state: 'closed', terminal: true })]),
}

const SLOTS = [
  { key: 'root-a', title: 'Alpha', crew_mode_root: true, messages: 1, running: false },
  { key: 'root-b', title: 'Beta', crew_mode_root: true, messages: 1, running: false },
  { key: 'plain', title: 'Not a project', messages: 1, running: false },
]

const CAPACITY: CrewBoardCapacity = { name: 'box-here', available_gb: 10.3, posture: 'ample', tight_gb: 4, critical_gb: 2, pressure_held: false }

async function mount(active = true) {
  const { SeatsStrip } = await import('./CapacityLine')
  const store = createTestStore({ dashboard: { slots: SLOTS } } as never)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <SeatsStrip active={active} />
      </QueryClientProvider>
    </Provider>,
  )
}

describe('SeatsStrip', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
    crewBoard.mockImplementation((root: string) => Promise.resolve(BOARDS[root]))
    crewBoardCapacity.mockResolvedValue(CAPACITY)
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.clearAllMocks()
    resetPollRule()
  })

  it("counts every project's seats per computer, this computer first, as a count alone", async () => {
    await mount()
    const computers = await screen.findAllByTestId('board-seats-computer')
    expect(computers.map(c => c.getAttribute('data-here'))).toEqual(['true', null])
    await waitFor(() => expect(within(computers[0]).getByTestId('board-seats-count').textContent).toBe('3'))
    expect(computers[0]).toHaveTextContent(/^This computer 3$/)
    expect(computers[1]).toHaveTextContent(/^linked-crew 1$/)
    expect(screen.getByTestId('board-seats-strip')).toHaveTextContent(/^Seats/)
    expect(screen.getByTestId('board-seats-strip')).not.toHaveTextContent(/ of /)
    expect(screen.queryByTestId('board-seats-room')).toBeNull()
    expect(crewBoard.mock.calls.map(c => c[0]).sort()).toEqual(['root-a', 'root-b'])
  })

  it("says this computer's room in red beside its count only when memory hurts", async () => {
    crewBoardCapacity.mockResolvedValue({ ...CAPACITY, available_gb: 3.1, posture: 'tight' })
    await mount()
    const room = await screen.findByTestId('board-seats-room')
    // The word alone, after a separator: "5 3.1 GB" would read as one number.
    expect(room.textContent).toBe('\xa0· memory tight')
    expect(room).toHaveClass('crew-tone-danger')
    const [here, linked] = screen.getAllByTestId('board-seats-count')
    expect(here).toHaveClass('crew-tone-danger')
    expect(linked).not.toHaveClass('crew-tone-danger')
  })

  it('draws nothing until the capacity read names this computer', async () => {
    crewBoardCapacity.mockImplementation(() => new Promise(() => undefined))
    await mount()
    await waitFor(() => expect(crewBoard).toHaveBeenCalledTimes(2))
    expect(screen.queryByTestId('board-seats-strip')).toBeNull()
  })

  it('reads nothing while the Board is not on screen', async () => {
    await mount(false)
    expect(screen.queryByTestId('board-seats-strip')).toBeNull()
    expect(crewBoard).not.toHaveBeenCalled()
    expect(crewBoardCapacity).not.toHaveBeenCalled()
  })
})
