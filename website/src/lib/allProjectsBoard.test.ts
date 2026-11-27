/**
 * The all-projects Board's reading: every Conductor's project, its stories under
 * the right bar's own moves, the 10-minute rule on a pinned clock, and the worker
 * chats each computer holds.
 */
import { describe, expect, it } from 'vitest'
import type { WorkBoardItem, WorkBoardResponse } from '../api/crewBoard'
import { allProjectsBoard, computerLoads, minutesSince, type ProjectBoard, type ProjectLive, type ProjectSlotRow } from './allProjectsBoard'

const NOW = Date.parse('2026-11-25T12:00:00Z')
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString()

function item(id: string, over: Partial<WorkBoardItem> = {}): WorkBoardItem {
  return {
    schema: 1, item_id: id, title: id, acceptance: {}, state: 'open', verdict: null, decision: '', round: 0, fails: 0,
    status: 'progress', summary: 'moving along', artifacts: {}, pr: null, last_report_at: ago(5), created_at: ago(60),
    closed_at: null, decided_at: null, orphaned: false, stale: false, acceptance_concrete: true, outstanding: false,
    terminal: false, alive: 'running', events: [], has_worker: true, in_backlog: false,
    ...over,
  }
}

function board(items: WorkBoardItem[], goal = 'ship it'): WorkBoardResponse {
  return {
    conductor: { schema: 1, slot_key: 'k', goal, round: 1, depth: 0, parent_item: null, created_at: ago(120) },
    conductor_alive: 'idle', items, take_over_available: false,
  }
}

const ROWS: ProjectSlotRow[] = [
  { key: 'root-a', title: 'Alpha', crew_mode_root: true, last_turn_ts: ago(2) },
  { key: 'root-b', title: 'Beta', crew_mode_root: true },
  { key: 'root-c', title: 'Gamma', crew_mode_root: true },
  // A plain chat is no project.
  { key: 'plain', title: 'Plain' },
]

const live = (root: string, over: Partial<ProjectLive>) => new Map<string, ProjectLive>([[root, { paused: false, questions: 0, approvals: [], workers: [], running: 0, ...over }]])

describe('allProjectsBoard', () => {
  it('puts a Conductor wait under its move and turns it late at 10 minutes, not before', () => {
    const waiting = (minutes: number) => new Map<string, ProjectBoard>([
      ['root-a', board([item('it_done', { status: 'done', alive: 'idle', last_report_at: ago(minutes) })])],
    ])
    const early = allProjectsBoard(ROWS, waiting(9), NOW)
    expect(early.stories.map(s => [s.item.item_id, s.move, s.late, s.minutes])).toEqual([['it_done', 'conductor', false, 9]])
    expect(early.projects.find(p => p.root === 'root-a')?.late).toBe(0)
    const late = allProjectsBoard(ROWS, waiting(10), NOW)
    expect(late.stories[0].late).toBe(true)
    expect(late.projects.find(p => p.root === 'root-a')?.late).toBe(1)
  })

  it('reads each story the way the right bar does', () => {
    const boards = new Map<string, ProjectBoard>([
      ['root-a', board([
        item('it_ask', { status: 'question', reason: 'needs_human', awaits_person: true, outstanding: true, alive: 'idle' }),
        item('it_gone', { status: 'progress', alive: 'closed' }),
        item('it_pr', { status: 'done', alive: 'idle', pr: 7, events: [{ id: 'e1', ts: ago(4), item_id: 'it_pr', kind: 'report', status: 'done', text: '' }, { id: 'e2', ts: ago(3), item_id: 'it_pr', kind: 'verdict', status: null, text: '' }] }),
        item('it_run'),
        item('it_next', { has_worker: false, in_backlog: true, alive: 'closed', status: null }),
        item('it_old', { state: 'accepted', terminal: true, alive: 'closed', status: 'done' }),
      ])],
    ])
    const page = allProjectsBoard(ROWS, boards, NOW)
    const moves = Object.fromEntries(page.stories.filter(s => s.root === 'root-a').map(s => [s.item.item_id, s.move]))
    expect(moves).toEqual({ it_ask: 'needs_you', it_gone: 'stuck', it_pr: 'github', it_run: 'working' })
    expect(page.upNext.map(u => [u.item.item_id, u.rank])).toEqual([['it_next', 1]])
    const alpha = page.projects.find(p => p.root === 'root-a')!
    expect(alpha).toMatchObject({ stories: 6, merged: 1, needsYou: 1, stuck: 1, late: 0, working: 1, lastLook: 2, run: 'waiting', loaded: true })
  })

  it('counts a stage done, decided on with its chat closed, under the Conductor\'s move and never as stuck or late', () => {
    const stage = (id: string) => item(id, { status: 'done', alive: 'closed', stale: true, last_report_at: ago(90), decided_at: ago(80) })
    const boards = new Map<string, ProjectBoard>([['root-a', board([stage('it_s1'), stage('it_s2'), stage('it_s3'), item('it_gone', { status: 'progress', alive: 'closed' })])]])
    const page = allProjectsBoard(ROWS, boards, NOW)
    expect(page.stories.map(s => [s.item.item_id, s.state, s.move, s.late])).toEqual([
      ['it_s1', 'stage_done', 'conductor', false], ['it_s2', 'stage_done', 'conductor', false], ['it_s3', 'stage_done', 'conductor', false],
      ['it_gone', 'stuck', 'stuck', false],
    ])
    expect(page.projects[0]).toMatchObject({ stuck: 1, late: 0 })
  })

  it('counts what waits on the person outside the stories, and lists the most to act on first', () => {
    const boards = new Map<string, ProjectBoard>([['root-a', board([item('it_run')])], ['root-b', null]])
    const page = allProjectsBoard(ROWS, boards, NOW, live('root-b', { questions: 1, approvals: [{ worker: 'w-b' }] }))
    expect(page.asks).toEqual([{ root: 'root-b', project: 'Beta', conductor: true, approvals: 1 }])
    expect(page.projects.map(p => [p.project, p.needsYou, p.loaded])).toEqual([['Beta', 2, true], ['Alpha', 0, true], ['Gamma', 0, false]])
    expect(page.projects.find(p => p.root === 'root-b')?.lastLook).toBeNull()
  })

  it("counts Needs you the right bar's way: the Conductor chat's own flags are not cards", () => {
    const rows: ProjectSlotRow[] = [{ key: 'root-a', title: 'Alpha', crew_mode_root: true, pending_approval: true, needs_input: true } as ProjectSlotRow]
    const page = allProjectsBoard(rows, new Map([['root-a', board([item('it_run')])]]), NOW)
    expect(page.projects[0].needsYou).toBe(0)
    expect(page.asks).toEqual([])
    // Its own tool approval still makes the run wait on the person, as the right bar's word does.
    expect(page.projects[0].run).toBe('waiting')
  })

  it("puts a story whose worker waits on a carded approval under Needs you, as its card, once", () => {
    const boards = new Map<string, ProjectBoard>([['root-a', board([item('it_tool'), item('it_run')])]])
    const workers = [{ key: 'w-tool', ledger_item: 'it_tool' }, { key: 'w-run', ledger_item: 'it_run' }]
    const page = allProjectsBoard(ROWS, boards, NOW, live('root-a', { approvals: [{ worker: 'w-tool' }], workers }))
    // Steps still draws it, under the person; Moves draws its card instead.
    expect(page.stories.map(s => [s.item.item_id, s.move])).toEqual([['it_tool', 'needs_you'], ['it_run', 'working']])
    expect(page.moves.map(s => s.item.item_id)).toEqual(['it_run'])
    expect(page.projects[0]).toMatchObject({ needsYou: 1, working: 1, run: 'waiting' })
  })

  it('lets only a live card joined through the crew list card a story; a stray approval flag on a board row decides nothing', () => {
    // No board read carries an approval flag; one that did must not move a story.
    const flagged = (it: WorkBoardItem) => ({ ...it, approval_waiting: true }) as WorkBoardItem
    const boards = new Map<string, ProjectBoard>([['root-a', board([flagged(item('it_tool')), flagged(item('it_old')), item('it_run')])]])
    const workers = [{ key: 'w-tool', ledger_item: 'it_tool' }, { key: 'w-run', ledger_item: 'it_run' }]
    const moves = (page: ReturnType<typeof allProjectsBoard>) => page.moves.map(s => [s.item.item_id, s.move])
    // The card waits in the worker bound to it_tool: that story is the card; it_old, flagged and bound to nobody, stays.
    const asked = allProjectsBoard(ROWS, boards, NOW, live('root-a', { approvals: [{ worker: 'w-tool' }], workers }))
    expect(moves(asked)).toEqual([['it_old', 'working'], ['it_run', 'working']])
    expect(asked.projects[0]).toMatchObject({ needsYou: 1, working: 2 })
    // The decision has landed (no card): every flagged story is back in Working at once.
    const decided = allProjectsBoard(ROWS, boards, NOW, live('root-a', { workers }))
    expect(moves(decided)).toEqual([['it_tool', 'working'], ['it_old', 'working'], ['it_run', 'working']])
    expect(decided.projects[0]).toMatchObject({ needsYou: 0, working: 3 })
    // The crew list binds no worker yet (loading, failed, read before the bind): the card is
    // counted and the story keeps its own move beside it.
    const unbound = allProjectsBoard(ROWS, boards, NOW, live('root-a', { approvals: [{ worker: 'w-tool' }] }))
    expect(moves(unbound)).toEqual([['it_tool', 'working'], ['it_old', 'working'], ['it_run', 'working']])
    expect(unbound.projects[0]).toMatchObject({ needsYou: 1, working: 3 })
  })

  it('says Paused for a paused project, as its right bar does', () => {
    const boards = new Map<string, ProjectBoard>([['root-a', board([item('it_run')])]])
    expect(allProjectsBoard(ROWS, boards, NOW, live('root-a', { paused: true })).projects[0].run).toBe('paused')
    expect(allProjectsBoard(ROWS, boards, NOW).projects[0].run).toBe('working')
  })
})

describe('computerLoads', () => {
  it('counts open worker chats per computer, this one first, closed chats left out', () => {
    const boards: ProjectBoard[] = [
      board([
        item('a', { runs_on: 'here-box', alive: 'running' }),
        item('b', { runs_on: 'here-box', alive: 'idle' }),
        item('c', { runs_on: 'here-box', alive: 'closed' }),
        item('d', { runs_on: 'linked-crew', alive: 'running' }),
        item('e', { runs_on: 'here-box', state: 'accepted', terminal: true }),
        item('f', { has_worker: false, runs_on: undefined }),
      ]),
      null,
      undefined,
    ]
    expect(computerLoads(boards, 'here-box')).toEqual([
      { name: 'here-box', here: true, chats: 2, running: 1 },
      { name: 'linked-crew', here: false, chats: 1, running: 1 },
    ])
  })

  it('still shows this computer with nothing on it', () => {
    expect(computerLoads([], 'here-box')).toEqual([{ name: 'here-box', here: true, chats: 0, running: 0 }])
  })
})

describe('minutesSince', () => {
  it('rounds down and refuses a stamp that does not parse', () => {
    expect(minutesSince(ago(9.9), NOW)).toBe(9)
    expect(Number.isNaN(minutesSince('soon', NOW))).toBe(true)
  })
})
