/**
 * Conductor mode Board > Backlog: the plan as a scrum board. The sprint line
 * counts only the seats stories hold, the backlog reads in rank order with the
 * conductor's next pick above it, a row that waits on the person says on what,
 * and an epic chip opens its drawer of stories and their sub-tasks. The drag
 * itself is `crewPlan.ranksForDrop` (crewPlan.test.ts) plus the headless drive.
 */
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../../../api/client', () => ({ api: { crewBoardAction: vi.fn() } }))

import ConductorBacklogView from './ConductorBacklogView'
import type { WorkBoardConductor, WorkBoardItem, WorkItemPlan } from '../../../api/crewBoard'

function item(id: string, title: string, plan?: WorkItemPlan, extra: Partial<WorkBoardItem> = {}): WorkBoardItem {
  return { item_id: id, title, state: 'open', created_at: `2026-11-22T08:00:0${id.slice(-1)}Z`, pr: null, ...(plan ? { plan } : {}), ...extra } as WorkBoardItem
}

const ITEMS = [
  item('it_e1', 'Questions panel', { level: 'epic' }),
  item('it_s2', 'Answers at top', { parent: 'it_e1', step: 'build' }),
  item('it_t3', 'Move answers up', { level: 'task', parent: 'it_s2', step: 'merged' }),
  item('it_t4', 'Check at phone width', { level: 'task', parent: 'it_s2', step: 'todo' }),
  item('it_s5', 'Phone layout', { parent: 'it_e1', step: 'mocks' }),
  item('it_b6', 'Fuzz the parser', { backlog: true, rank: 2, parent: 'it_e1', tags: ['est: 45 m'] }),
  item('it_b7', 'Bound the fetch', { backlog: true, rank: 1, tags: ['bug', 'est: 2 h'] }),
  item('it_b8', 'Colour legend', { backlog: true, tags: ['est: 59.6 m', 'est: 1 h'] }),
  item('it_b9', 'Sort the epics', { backlog: true }),
]

const CONDUCTOR = {
  schema: 1, slot_key: 'root-1', goal: 'Answers feel instant\nDone when: ...', round: 3, depth: 0, parent_item: null, created_at: '',
  concurrency_cap: 4, next_pick: 'it_b7', releases: [{ name: '0.5', state: 'planned' }],
} as WorkBoardConductor

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const rowState = (id: string) => (id === 'it_s5' ? { needsYou: true, waitingOn: 'Pick a design' } : undefined)
  render(
    <QueryClientProvider client={qc}>
      <ConductorBacklogView root="root-1" items={ITEMS} conductor={CONDUCTOR} rowState={rowState} openItem={() => undefined} needsYou={null} needCount={1} />
    </QueryClientProvider>,
  )
}

describe('Conductor mode Backlog view', () => {
  it('counts story seats, lists the backlog by rank and shows the next pick', () => {
    renderView()
    // Two stories hold seats; the epic and the two sub-tasks hold none.
    expect(screen.getByTestId('plan-sprint-line').textContent).toContain('slots busy: 2 of 4')
    const sprint = within(screen.getByTestId('plan-sprint'))
    expect(sprint.getAllByTestId('plan-story').map(r => r.getAttribute('data-item'))).toEqual(['it_s2', 'it_s5'])
    expect(sprint.getByText('Needs you: Pick a design')).toBeTruthy()
    const rows = screen.getAllByTestId('plan-backlog-row').map(r => within(r).getByTestId('plan-story').getAttribute('data-item'))
    expect(rows).toEqual(['it_b7', 'it_b6', 'it_b8', 'it_b9'])
    expect(screen.getByTestId('plan-next-pick').getAttribute('data-item')).toBe('it_b7')
    expect(within(screen.getByTestId('plan-releases')).getByText('0.5')).toBeTruthy()
  })

  it('opens an epic onto its stories and their sub-tasks', () => {
    renderView()
    expect(screen.queryByTestId('plan-epic-drawer')).toBeNull()
    fireEvent.click(screen.getByTestId('plan-epic-chip'))
    const drawer = within(screen.getByTestId('plan-epic-drawer'))
    expect(drawer.getAllByTestId('plan-epic-story').map(s => s.textContent)).toEqual([
      expect.stringContaining('Answers at top'), expect.stringContaining('Phone layout'), expect.stringContaining('Fuzz the parser'),
    ])
    expect(drawer.getAllByTestId('plan-epic-task').map(t => t.textContent)).toEqual([
      expect.stringContaining('Move answers up'), expect.stringContaining('Check at phone width'),
    ])
  })

  it('shows each backlog row\'s estimate in its own column, totals them, and sorts by them without moving a rank', async () => {
    const { api } = await import('../../../api/client')
    renderView()
    const order = () => screen.getAllByTestId('plan-backlog-row').map(r => within(r).getByTestId('plan-story').getAttribute('data-item'))
    const cells = () => screen.getAllByTestId('plan-estimate').map(c => c.textContent)
    expect(order()).toEqual(['it_b7', 'it_b6', 'it_b8', 'it_b9'])
    // 59.6 minutes rounds to an hour, and reads as one.
    expect(cells()).toEqual(['2h', '45m', '1h', '—'])
    expect(screen.getByTestId('plan-estimate-total').textContent).toBe('Total 3.7h')
    // A second estimate tag is not the column's, so it stays a chip.
    expect(within(screen.getAllByTestId('plan-backlog-row')[2]).getByText('est: 1 h')).toBeTruthy()
    // The estimate tag is the column, not a chip; the other tags stay chips.
    const top = within(screen.getAllByTestId('plan-backlog-row')[0])
    expect(top.getByText('bug')).toBeTruthy()
    expect(top.queryByText('est: 2 h')).toBeNull()

    const sort = screen.getByTestId('plan-estimate-sort')
    fireEvent.click(sort)
    expect(sort.getAttribute('aria-pressed')).toBe('true')
    expect(order()).toEqual(['it_b6', 'it_b8', 'it_b7', 'it_b9'])
    expect(cells()).toEqual(['45m', '1h', '2h', '—'])
    fireEvent.click(sort)
    expect(order()).toEqual(['it_b7', 'it_b6', 'it_b8', 'it_b9'])
    expect(vi.mocked(api.crewBoardAction)).not.toHaveBeenCalled()
  })

  it('draws a board nobody planned from its facts: step, machine, PR facts and the wait on its conductor', () => {
    // The board read's own derived step rides `plan.step`; nothing else is planned.
    const rows = [
      item('it_n1', 'Unbound', { step: 'todo' }, { has_worker: false, in_backlog: true }),
      item('it_n2', 'Working', { step: 'build' }, { has_worker: true, in_backlog: false, runs_on: 'M5' }),
      item('it_n3', 'Has a PR', { step: 'review' }, { has_worker: true, in_backlog: false, runs_on: 'wlap', pr: 118 }),
      item('it_n4', 'Done, unanswered', { step: 'build' }, { has_worker: true, in_backlog: false, runs_on: 'M5' }),
    ]
    const prFacts = { it_n3: { label: 'o/r#118', url: 'u', reads: { warm_brief: { facts: { checks: { passed: 11, failed: 3 } }, observed_at: 1 } } } }
    const waits: Record<string, { age: string; loud: boolean }> = { it_n2: { age: '4m', loud: false }, it_n4: { age: '50m', loud: true } }
    const rowState = (id: string) => ({ needsYou: false, waitingOn: '', conductorWait: waits[id] })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ConductorBacklogView root="root-1" items={rows} conductor={{ ...CONDUCTOR, next_pick: 'it_n1' }} prFacts={prFacts} rowState={rowState} openItem={() => undefined} needsYou={null} needCount={0} />
      </QueryClientProvider>,
    )
    const row = (id: string) => within(document.querySelector(`[data-testid="plan-story"][data-item="${id}"]`) as HTMLElement)
    expect(row('it_n1').getByTestId('plan-step').getAttribute('data-step')).toBe('todo')
    expect(row('it_n1').queryByTestId('plan-runs-on')).toBeNull()
    expect(row('it_n2').getByTestId('plan-runs-on').textContent).toBe('M5')
    expect(row('it_n3').getByTestId('plan-runs-on').getAttribute('title')).toBe('Runs on wlap')
    expect(row('it_n3').getAllByTestId('plan-pr-fact').length).toBeGreaterThan(1)
    expect(row('it_n3').getAllByTestId('plan-pr-fact')[0].getAttribute('data-bad')).toBe('true')
    expect(row('it_n2').getByTestId('plan-conductor-wait').getAttribute('data-loud')).toBeNull()
    const late = row('it_n4').getByTestId('plan-conductor-wait')
    expect(late.textContent).toBe('Waits on its Conductor 50m')
    expect(late.getAttribute('data-loud')).toBe('true')
  })
})
