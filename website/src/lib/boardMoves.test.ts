/**
 * Who must act on a Board story, and the Steps reading: each move from its
 * milestone state and its Conductor's wait, each story counted once at its step,
 * Merged counting what reached it, and the landed counts.
 */
import { describe, expect, it } from 'vitest'
import type { WorkBoardItem, WorkItemPlan } from '../api/crewBoard'
import { boardMove, boardSteps, longestWaitingFirst, storyStep } from './boardMoves'
import { boardPlan } from './crewPlan'

function item(id: string, plan?: WorkItemPlan, extra: Partial<WorkBoardItem> = {}): WorkBoardItem {
  return { item_id: id, title: id, state: 'open', created_at: '2026-11-25T10:00:00Z', pr: null, alive: 'idle', ...(plan ? { plan } : {}), ...extra } as WorkBoardItem
}

describe('boardMove', () => {
  it('names who must act, in the order that hurts most', () => {
    const it0 = item('a')
    expect(boardMove(it0, 'needs_you', 30)).toBe('needs_you')
    expect(boardMove(it0, 'stuck', null)).toBe('stuck')
    // A worker gone quiet with its own move unmade: nobody is moving it.
    expect(boardMove(it0, 'quiet', null)).toBe('stuck')
    // Any report its Conductor has not picked up, late or not.
    expect(boardMove(it0, 'waiting', 0)).toBe('conductor')
    expect(boardMove(it0, 'done', 50)).toBe('conductor')
    expect(boardMove(it0, 'running', null)).toBe('working')
    expect(boardMove(it0, 'next', null)).toBe('working')
  })

  it('reads a wait on a pull request or on CI as GitHub, never while a turn runs', () => {
    expect(boardMove(item('p', undefined, { pr: 116 }), 'waiting', null)).toBe('github')
    expect(boardMove(item('l', undefined, { looping: true, pr: 12 }), 'running', null)).toBe('github')
    expect(boardMove(item('r', undefined, { looping: true, alive: 'running', pr: 12 }), 'running', null)).toBe('working')
    // A loop with no pull request waits on its own next check, not on GitHub.
    expect(boardMove(item('n', undefined, { looping: true }), 'running', null)).toBe('working')
    // Judged pending with a PR to wait on; with none, still the Conductor's to close.
    expect(boardMove(item('d', undefined, { pr: 9 }), 'done', null)).toBe('github')
    expect(boardMove(item('d2'), 'done', null)).toBe('conductor')
    // An idle story after progress with no PR is the worker's, between turns.
    expect(boardMove(item('w'), 'waiting', null)).toBe('working')
  })

  it('follows the newest of report and decision once the Conductor hands a done report back', () => {
    const report = { status: 'done', last_report_at: '2026-11-25T11:00:00Z' } as const
    // A decision after the report (the ledger's decide stamps `decided_at` and leaves
    // the status `done`): the worker's move, with a PR or without.
    const handed = item('h', undefined, { ...report, decided_at: '2026-11-25T11:05:00Z' })
    expect(boardMove(handed, 'done', null)).toBe('working')
    expect(boardMove({ ...handed, pr: 7 }, 'done', null)).toBe('working')
    // The same handback on the event tail only, as an older gateway sends it.
    const tail = item('t', undefined, { ...report, events: [
      { kind: 'report', ts: '2026-11-25T11:00:00Z' }, { kind: 'decision', ts: '2026-11-25T11:05:00Z' },
    ] } as Partial<WorkBoardItem>)
    expect(boardMove(tail, 'done', null)).toBe('working')
    // A new done report after that decision is the Conductor's again.
    const again = item('a', undefined, { ...report, last_report_at: '2026-11-25T11:10:00Z', decided_at: '2026-11-25T11:05:00Z' })
    expect(boardMove(again, 'done', 0)).toBe('conductor')
    expect(boardMove(again, 'done', null)).toBe('conductor')
  })
})

describe('boardSteps', () => {
  const items = [
    item('r1', { step: 'review' }),
    item('b1', { step: 'build' }),
    item('b2', { step: 'build' }),
    item('n1', { backlog: true, step: 'todo' }, { in_backlog: true }),
    item('m1', { step: 'merged' }, { state: 'accepted' }),
    item('m2', { step: 'released' }, { state: 'accepted' }),
    item('m3', undefined, { state: 'accepted' }),
    item('x1', { step: 'build' }, { state: 'abandoned' }),
    item('e1', { level: 'epic' }),
    item('t1', { level: 'task', parent: 'b1', step: 'merged' }),
  ]
  const steps = boardSteps(items, boardPlan(items))

  it('counts each story at its step, Up next and sub-tasks left out', () => {
    expect(steps.strip).toEqual({ todo: 0, mocks: 0, build: 2, review: 1, merged: 3, released: 1 })
  })

  it('groups the open stories furthest along first, an empty step kept as one line', () => {
    expect(steps.groups.map(g => [g.step, g.items.map(i => i.item_id)])).toEqual([
      ['review', ['r1']], ['build', ['b1', 'b2']], ['mocks', []], ['todo', []],
    ])
  })

  it('says what landed as counts: merged, released of merged, not done', () => {
    expect(steps.landed).toEqual({ merged: 3, released: 1, notDone: 1 })
  })

  it('shows Merged as a group only while an open story sits there', () => {
    const open = [item('o1', { step: 'merged' })]
    expect(boardSteps(open, boardPlan(open)).groups.map(g => g.step)).toEqual(['merged', 'review', 'build', 'mocks', 'todo'])
  })

  it('reads a story with no step as merged once accepted, else to do', () => {
    expect(storyStep(item('a', undefined, { state: 'accepted' }))).toBe('merged')
    expect(storyStep(item('b'))).toBe('todo')
  })
})

describe('longestWaitingFirst', () => {
  it('orders by the last report, then by creation', () => {
    const rows = [
      item('new', undefined, { last_report_at: '2026-11-25T10:30:00Z' }),
      item('old', undefined, { last_report_at: '2026-11-25T10:05:00Z' }),
      item('none', undefined, { created_at: '2026-11-25T09:00:00Z' }),
    ]
    expect([...rows].sort(longestWaitingFirst).map(r => r.item_id)).toEqual(['none', 'old', 'new'])
  })
})
