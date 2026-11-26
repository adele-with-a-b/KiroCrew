/**
 * The run line's computer parts: the worker chats each computer holds across every
 * Conductor-mode project, read from every project's board and the capacity read, so
 * they say what the all-projects page's tiles say. A count, never "N of M"; this
 * computer's room always said in words, red only when memory hurts; a linked Crew's
 * room as the patrol last read it, else its count alone; nothing read while the tab
 * is not on screen. The api client is mocked at the module boundary; the clock is pinned.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../test/helpers'
import type { WorkBoardItem, WorkBoardResponse } from '../../api/crewBoard'
import type { CrewBoardCapacity } from '../../api/crewBoardCapacity'
import type { ComputerLoad } from '../../lib/allProjectsBoard'
import { resetPollRule } from '../../lib/pollRule'
import { seatParts, useSeatParts } from './CapacityLine'

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

/** Two projects. Worker chats: two here in Alpha, one here in Beta, one on the linked
 *  Crew. Not counted: a closed chat, a backlog row with no worker, a closed story. */
const BOARDS: Record<string, WorkBoardResponse> = {
  'root-a': board([
    item('Here one'),
    item('Here two', { alive: 'running' }),
    item('On the crew', { runs_on: 'demo-crew', alive: 'running' }),
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

const CAPACITY: CrewBoardCapacity = { name: 'box-here', available_gb: 10.3, posture: 'ample', tight_gb: 4, critical_gb: 2, pressure_held: false, crews: [{ name: 'demo-crew', room: '' }] }

const HERE: ComputerLoad = { name: 'box-here', here: true, chats: 14, running: 3 }
const CREW: ComputerLoad = { name: 'demo-crew', here: false, chats: 3, running: 1 }
const texts = (loads: ComputerLoad[], capacity: CrewBoardCapacity | undefined) => seatParts(loads, capacity).map(p => p.text)

describe('seatParts', () => {
  it('says both computers, this one first, each with its room in words and no "of N"', () => {
    expect(texts([HERE, CREW], CAPACITY)).toEqual(['this computer 14, has room', 'demo-crew 3, has room'])
    expect(texts([HERE, CREW], CAPACITY).join(' ')).not.toMatch(/ of /)
  })

  it('says this computer alone when no linked Crew holds a chat', () => {
    const parts = seatParts([HERE], CAPACITY)
    expect(parts.map(p => p.text)).toEqual(['this computer 14, has room'])
    expect(parts[0].hurts).toBe(false)
  })

  it("says this computer's room in red when memory hurts, a crew's shortage in words", () => {
    const parts = seatParts([HERE, CREW], { ...CAPACITY, posture: 'tight', crews: [{ name: 'demo-crew', room: 'memory' }] })
    expect(parts.map(p => p.text)).toEqual(['this computer 14, memory tight', 'demo-crew 3, short on memory'])
    expect(parts.map(p => p.hurts)).toEqual([true, false])
    expect(texts([CREW], { ...CAPACITY, crews: [{ name: 'demo-crew', room: 'unreachable' }] })).toEqual(['demo-crew 3, not reachable'])
  })

  it('shows a count alone where the room is unknown, and nothing before the shared read lands', () => {
    // Unmeasured memory, and a crew the patrol has not read lately (or read as unknown).
    expect(texts([HERE, CREW], { ...CAPACITY, posture: 'unknown', available_gb: null, crews: [] })).toEqual(['this computer 14', 'demo-crew 3'])
    expect(texts([CREW], { ...CAPACITY, crews: [{ name: 'demo-crew', room: 'unknown' }] })).toEqual(['demo-crew 3'])
    // An older gateway sends no crews list.
    const { crews: _dropped, ...older } = CAPACITY
    expect(texts([CREW], older)).toEqual(['demo-crew 3'])
    // The shared count is unknown until the capacity read names this computer: no part at all.
    expect(seatParts([HERE, CREW], undefined)).toEqual([])
  })
})

function Probe({ active }: { active: boolean }) {
  const parts = useSeatParts(active)
  return <ul data-testid="probe">{parts.map(p => <li key={p.name} data-here={p.here || undefined}>{p.text}</li>)}</ul>
}

async function mount(active = true) {
  const store = createTestStore({ dashboard: { slots: SLOTS } } as never)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <Probe active={active} />
      </QueryClientProvider>
    </Provider>,
  )
}

describe('useSeatParts', () => {
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

  it("counts every project's worker chats per computer, this computer first", async () => {
    await mount()
    await waitFor(() => expect(screen.getByTestId('probe').textContent).toBe('this computer 3, has roomdemo-crew 1, has room'))
    expect([...screen.getByTestId('probe').children].map(c => c.getAttribute('data-here'))).toEqual(['true', null])
    expect(crewBoard.mock.calls.map(c => c[0]).sort()).toEqual(['root-a', 'root-b'])
  })

  it('says nothing until the capacity read names this computer', async () => {
    crewBoardCapacity.mockImplementation(() => new Promise(() => undefined))
    await mount()
    await waitFor(() => expect(crewBoard).toHaveBeenCalledTimes(2))
    expect(screen.getByTestId('probe').children).toHaveLength(0)
  })

  it('reads nothing while the Board is not on screen', async () => {
    await mount(false)
    expect(screen.getByTestId('probe').children).toHaveLength(0)
    expect(crewBoard).not.toHaveBeenCalled()
    expect(crewBoardCapacity).not.toHaveBeenCalled()
  })
})
