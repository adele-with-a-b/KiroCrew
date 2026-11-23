import { describe, expect, it } from 'vitest'
import type { WorkBoardItem, WorkItemPlan } from '../api/crewBoard'
import { backlogCompare, boardPlan, childProgress, epicOf, moveRow, projectedIndex, ranksForDrop, releaseViews, seatsBusy } from './crewPlan'

function item(id: string, plan?: WorkItemPlan, extra: Partial<WorkBoardItem> = {}): WorkBoardItem {
  return { item_id: id, title: id, state: 'open', created_at: `2026-11-22T08:00:0${id.slice(-1)}Z`, ...(plan ? { plan } : {}), ...extra } as WorkBoardItem
}

describe('boardPlan', () => {
  it('splits epics, the sprint and the ranked backlog, tasks under their story', () => {
    const items = [
      item('e1', { level: 'epic' }),
      item('s1', { parent: 'e1' }),
      item('t1', { level: 'task', parent: 's1' }),
      item('b2', { backlog: true, rank: 5 }),
      item('b3', { backlog: true, rank: 2 }),
      item('b4', { backlog: true }),
      item('d5', { backlog: true }, { state: 'accepted' }),
    ]
    const plan = boardPlan(items)
    expect(plan.epics.map(i => i.item_id)).toEqual(['e1'])
    expect(plan.sprint.map(i => i.item_id)).toEqual(['s1'])
    expect(plan.backlog.map(i => i.item_id)).toEqual(['b3', 'b2', 'b4'])
    expect(plan.children.get('s1')?.map(i => i.item_id)).toEqual(['t1'])
    expect(plan.planned).toBe(true)
    expect(epicOf(items[2], plan.byId)?.item_id).toBe('e1')
    expect(epicOf(items[0], plan.byId)).toBeNull()
  })

  it('puts the stories the server reads as waiting in the backlog, oldest first, on a board never planned', () => {
    // Blick's shape: open rows with no plan, two of them with a worker.
    const rows = ['a1', 'a2', 'a3', 'a4', 'a5'].map(id => item(id, undefined, { in_backlog: true, has_worker: false }))
    rows[1] = { ...rows[1], in_backlog: false, has_worker: true }
    rows[3] = { ...rows[3], in_backlog: false, has_worker: true }
    const plan = boardPlan(rows)
    expect(plan.sprint.map(i => i.item_id)).toEqual(['a2', 'a4'])
    expect(plan.backlog.map(i => i.item_id)).toEqual(['a1', 'a3', 'a5'])
    expect(seatsBusy(rows)).toBe(2)
  })

  it('keeps a story held in the sprint with no worker yet out of the backlog and the seats', () => {
    const rows = [item('h1', { backlog: false }, { in_backlog: false, has_worker: false }), item('w2', undefined, { in_backlog: false, has_worker: true })]
    const plan = boardPlan(rows)
    expect(plan.sprint.map(i => i.item_id)).toEqual(['h1', 'w2'])
    expect(plan.backlog).toHaveLength(0)
    expect(seatsBusy(rows)).toBe(1)
  })

  it('reads a board that was never planned as one sprint from an older gateway', () => {
    const plan = boardPlan([item('a1'), item('a2')])
    expect(plan.planned).toBe(false)
    expect(plan.sprint).toHaveLength(2)
    expect(plan.backlog).toHaveLength(0)
  })
})

describe('backlogCompare', () => {
  it('ranks first, then the unranked oldest first, as next_pick does', () => {
    const rows = [item('u2', { backlog: true }), item('r1', { backlog: true, rank: 9 }), item('u1', { backlog: true })]
    expect([...rows].sort(backlogCompare).map(i => i.item_id)).toEqual(['r1', 'u1', 'u2'])
  })
})

describe('ranksForDrop', () => {
  const order = [item('a1', { rank: 1 }), item('a2', { rank: 2 }), item('a3', { rank: 3 })]
  const moved = (from: number, to: number, rows = order) => ranksForDrop(rows, from, to).map(w => [w.item_id, w.rank])
  it('writes only the dragged row, at the midpoint of its new neighbours', () => {
    expect(moved(2, 1)).toEqual([['a3', 1.5]])
  })
  it('goes one past the edge at the top and the bottom', () => {
    expect(moved(2, 0)).toEqual([['a3', 0]])
    expect(moved(0, 2)).toEqual([['a1', 4]])
  })
  it('ranks the unranked rows a drop lands below, so the server keeps the shown order', () => {
    const plain = [item('p1'), item('p2'), item('p3')]
    expect(moved(2, 0, plain)).toEqual([['p3', 0]])
    expect(moved(0, 1, plain)).toEqual([['p2', 2], ['p1', 2.5]])
    const mixed = [item('r1', { rank: 1 }), item('u2'), item('u3')]
    expect(moved(0, 2, mixed)).toEqual([['u2', 2], ['u3', 3], ['r1', 4]])
  })
  it('renumbers the list when the new neighbours share a rank, so the drop still moves the row', () => {
    const tied = [item('t1', { rank: 1 }), item('t2', { rank: 1 }), item('t3', { rank: 2 })]
    const writes = new Map(ranksForDrop(tied, 2, 1).map(w => [w.item_id, w.rank]))
    expect([...writes]).toEqual([['t2', 3]])
    const after = tied.map(i => ({ ...i, plan: { ...i.plan, rank: writes.get(i.item_id) ?? i.plan?.rank } }))
    expect([...after].sort(backlogCompare).map(i => i.item_id)).toEqual(['t1', 't3', 't2'])
  })
  it('moveRow puts the row where it dropped', () => {
    expect(moveRow(['a', 'b', 'c'], 2, 0)).toEqual(['c', 'a', 'b'])
  })
  it('projectedIndex numbers each row as moveRow would place it', () => {
    const rows = ['a', 'b', 'c', 'd']
    for (const [from, to] of [[3, 0], [0, 3], [1, 2], [2, 2]]) {
      const after = moveRow(rows, from, to)
      expect(rows.map((_, n) => projectedIndex(n, from, to))).toEqual(rows.map(r => after.indexOf(r)))
    }
    expect(projectedIndex(2, -1, -1)).toBe(2)
  })
})

describe('progress and seats', () => {
  it('counts merged children and seats outside the backlog', () => {
    const items = [
      item('s1'),
      item('t1', { level: 'task', parent: 's1', step: 'merged' }),
      item('t2', { level: 'task', parent: 's1', step: 'build' }),
      item('e1', { level: 'epic' }),
      item('b1', { backlog: true }),
    ]
    const plan = boardPlan(items)
    expect(childProgress(items[0], plan.children)).toEqual({ done: 1, total: 2 })
    expect(seatsBusy(items)).toBe(1)
  })

  it('lists releases newest first with what ships in each', () => {
    const items = [item('a1', { release: '0.5', step: 'merged' }), item('a2', { release: '0.5', step: 'review' }), item('a3', { release: '0.4', step: 'released' })]
    const views = releaseViews([{ name: '0.4', state: 'shipped' }, { name: '0.5', state: 'planned' }], items)
    expect(views.map(v => [v.release.name, v.items.length, v.merged])).toEqual([['0.5', 2, 1], ['0.4', 1, 1]])
  })
})
