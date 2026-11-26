/**
 * The Board's line for the other projects: what every other Conductor's row
 * carries, summed. Late is a report its Conductor has not picked up for 10 minutes,
 * read against a pinned clock, and a running worker turn is never late.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'
import OtherProjectsLine, { otherProjectsCounts, type OtherProjectRow } from './OtherProjectsLine'
import { crewWorkLate } from '../../../lib/crewWork'

const NOW = Date.parse('2026-11-25T12:00:00Z')
const MIN = 60_000

const facts = (over: Partial<NonNullable<OtherProjectRow['crew_work']>> = {}) => ({ open: 3, person: 0, items: [], ...over })

const ROWS: OtherProjectRow[] = [
  // This chat's own project is never counted.
  { key: 'here', crew_mode_root: true, crew_work: facts({ person: 4 }) },
  { key: 'proj-a', crew_mode_root: true, crew_work: facts({
    person: 1,
    items: [
      { worker: 'w-late', stale_at_ms: 0, conductor_since_ms: NOW - 12 * MIN },
      { worker: 'w-soon', stale_at_ms: 0, conductor_since_ms: NOW - 9 * MIN },
      { worker: 'w-quiet', stale_at_ms: NOW - MIN },
    ],
  }) },
  { key: 'proj-b', crew_mode_root: true, crew_work: facts({
    items: [{ worker: 'w-run', stale_at_ms: 0, conductor_since_ms: NOW - 40 * MIN }, { worker: '', stale_at_ms: 0, conductor_since_ms: NOW - 10 * MIN }],
  }) },
  { key: 'w-run', running: true },
  // A plain chat with a ledger fold is no project here.
  { key: 'plain', crew_work: facts({ person: 2 }) },
]

afterEach(() => vi.useRealTimers())

describe('otherProjectsCounts', () => {
  it('sums needs you, stuck and late over the other Conductor roots', () => {
    expect(otherProjectsCounts(ROWS, 'here', NOW)).toEqual({ needsYou: 1, stuck: 1, late: 2 })
  })

  it('turns a wait late at 10 minutes, not before', () => {
    const one = facts({ items: [{ worker: 'w', stale_at_ms: 0, conductor_since_ms: NOW - 10 * MIN }] })
    expect(crewWorkLate(one, [], NOW)).toBe(1)
    expect(crewWorkLate(one, [], NOW - 1)).toBe(0)
    // Its worker's turn running again: the worker moved on.
    expect(crewWorkLate(one, [{ key: 'w', running: true }], NOW)).toBe(0)
    // A remote worker has no slot key (`worker: ''`): its machine's own record says
    // whether its turn runs, by the same rule.
    const remote = (worker_running?: boolean) => facts({ items: [{ worker: '', stale_at_ms: 0, conductor_since_ms: NOW - 30 * MIN, worker_running }] })
    expect(crewWorkLate(remote(true), [], NOW)).toBe(0)
    expect(crewWorkLate(remote(false), [], NOW)).toBe(1)
    expect(crewWorkLate(remote(), [], NOW)).toBe(1)
    // An older gateway sends no clock.
    expect(crewWorkLate(facts({ items: [{ worker: 'w', stale_at_ms: 0 }] }), [], NOW)).toBe(0)
  })

  it('leaves a running Conductor\'s stalls out, as its Sessions row does', () => {
    const rows = ROWS.map(r => (r.key === 'proj-a' ? { ...r, running: true } : r))
    expect(otherProjectsCounts(rows, 'here', NOW).stuck).toBe(0)
  })
})

describe('OtherProjectsLine', () => {
  it('says only the counts above zero', () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
    render(<Provider store={createTestStore({ dashboard: { slots: ROWS } } as never)}><OtherProjectsLine root="here" now={NOW} /></Provider>)
    expect(screen.getByTestId('board-other-projects')).toHaveTextContent('Other projects: 1 needs you, 1 stuck, and 2 late')
    // One sentence from one key: the list is its placeholder, drawn bold inside it.
    expect(screen.getByText('1 needs you, 1 stuck, and 2 late')).toHaveClass('font-medium')
  })

  it('draws nothing when the other projects hold nothing', () => {
    const { container } = render(<Provider store={createTestStore({ dashboard: { slots: [ROWS[0]] } } as never)}><OtherProjectsLine root="here" now={NOW} /></Provider>)
    expect(container).toBeEmptyDOMElement()
  })
})
