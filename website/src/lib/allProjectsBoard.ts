/** The all-projects Board, read on the client: every Conductor's project, its
 *  stories by who must act, and the worker chats each computer holds. Built from
 *  what the page already reads -- each Conductor's own board (`GET /api/crew-board`),
 *  its crew list and the live session rows -- through the same rules the right bar
 *  uses (`boardMove`, `milestoneState`, `conductorWaitMinutes`, `approvalCarded`,
 *  `needsYouCount`), so a story sits under the same move, and a project shows the
 *  same word and count, on both. Pure: the page and its tests share one reading, and
 *  the clock is passed in. Nothing here is stored. */
import type { WorkBoardItem, WorkBoardResponse } from '../api/crewBoard'
import { CONDUCTOR_WAIT_LOUD_MINUTES, approvalCarded, boardMove, boardSteps, movesStories, needsYouCount, type BoardMove, type BoardSteps, type BoundWorker, type MilestoneState } from './boardMoves'
import { boardPlan, childProgress, isMerged, levelOf } from './crewPlan'
import { conductorModeRunState, type ConductorModeRunState } from './conductorModeRun'
import { conductorWaitMinutes, milestoneState, personAsks } from '../pages/chat/command-center/ConductorModeCrewPanel'

/** The session-row fields this reads, for a Conductor's own chat. */
export interface ProjectSlotRow {
  key: string
  title?: string
  running?: boolean
  pending_approval?: boolean | unknown
  last_turn_ts?: string
  crew_mode_root?: boolean
}

/** What the right bar reads live for one project beside its board, so the page
 *  reads the same: Pause all (the crew list's `paused`), the Conductor chat's own
 *  question card (0 or 1), the crew's approval cards (`crewApprovalRequests`), its
 *  crew list (which worker each story is bound to), and the crew members running a
 *  turn (`conductorModeCounts`). */
export interface ProjectLive { paused: boolean; questions: number; approvals: readonly { worker: string }[]; workers: readonly BoundWorker[]; running: number }

const NOTHING_LIVE: ProjectLive = { paused: false, questions: 0, approvals: [], workers: [], running: 0 }

/** One Conductor's board read: the answer, `null` for a Conductor with no work
 *  ledger yet (the read's 404), `undefined` while it loads. */
export type ProjectBoard = WorkBoardResponse | null | undefined

export interface AllProjectsStory {
  root: string
  project: string
  item: WorkBoardItem
  /** Its state as the right bar's `milestoneState` reads it. */
  state: MilestoneState
  move: BoardMove
  /** A Conductor's move past its 10 minutes. */
  late: boolean
  /** Minutes in its state: how long it waited on its Conductor, else since its last report. */
  minutes: number
  /** Its sub-tasks merged and in all. */
  subtasks: { done: number; total: number }
}

/** What waits on the person in a project and is not a story: its Conductor chat's
 *  own question card, and its crew's tool approval cards. */
export interface ProjectAsk { root: string; project: string; conductor: boolean; approvals: number }

export interface ProjectRow {
  root: string
  project: string
  /** The run's word, as the right bar's sprint line reads it; null with nothing to call a run. */
  run: ConductorModeRunState | null
  stories: number
  merged: number
  needsYou: number
  stuck: number
  late: number
  working: number
  /** Minutes since the Conductor's last turn, null when it has none. */
  lastLook: number | null
  /** The board read has answered (a board, or no ledger). */
  loaded: boolean
}

export interface UpNextRow { root: string; project: string; item: WorkBoardItem; rank: number }

export interface ProjectSteps { root: string; project: string; steps: BoardSteps; upNext: WorkBoardItem[] }

export interface AllProjects {
  projects: ProjectRow[]
  stories: AllProjectsStory[]
  /** The stories Moves draws: a story whose approval has its own card is that card. */
  moves: AllProjectsStory[]
  asks: ProjectAsk[]
  upNext: UpNextRow[]
  steps: ProjectSteps[]
}

/** Whole minutes from an ISO stamp to `now`, rounded down; NaN when it does not parse. */
export function minutesSince(stamp: string | null | undefined, now: number): number {
  const at = stamp ? Date.parse(stamp) : NaN
  return Number.isFinite(at) ? Math.max(0, Math.floor((now - at) / 60000)) : NaN
}

/** The name a project goes by: its Conductor chat's title, else its key. */
export function projectName(row: ProjectSlotRow): string {
  return (row.title || '').trim() || row.key
}

/** The Conductor-mode roots among the live rows: one per project. */
export function projectRoots(rows: readonly ProjectSlotRow[]): ProjectSlotRow[] {
  return rows.filter(r => r.crew_mode_root)
}

/** One project's open stories as the Board draws them (not the backlog). A story
 *  whose worker waits on a carded approval (`carded`, from `approvalCarded`) waits on
 *  the person, as in the right bar. */
export function projectStories(root: string, project: string, items: readonly WorkBoardItem[], now: number, carded: ReadonlySet<string>): AllProjectsStory[] {
  const plan = boardPlan(items)
  const asks = new Set(personAsks(items, carded).map(i => i.item_id))
  return plan.sprint.map(item => {
    const state = milestoneState(item, carded.has(item.item_id) || asks.has(item.item_id))
    const wait = state === 'needs_you' ? null : conductorWaitMinutes(item, now)
    const move = boardMove(item, state, wait)
    const since = minutesSince(item.last_report_at || item.created_at, now)
    return {
      root, project, item, state, move,
      late: wait != null && wait >= CONDUCTOR_WAIT_LOUD_MINUTES,
      minutes: wait ?? since,
      subtasks: childProgress(item, plan.children),
    }
  })
}

/** Most to act on first, then most stories, then the name in byte order. */
function projectOrder(a: ProjectRow, b: ProjectRow): number {
  const act = (p: ProjectRow) => p.needsYou + p.stuck + p.late
  return act(b) - act(a) || b.stories - a.stories || (a.project < b.project ? -1 : a.project > b.project ? 1 : 0)
}

/** The whole page from the live rows, each root's board read, what the right bar
 *  reads live per root (`live`, by root key) and the clock. */
export function allProjectsBoard(rows: readonly ProjectSlotRow[], boards: ReadonlyMap<string, ProjectBoard>, now: number, live: ReadonlyMap<string, ProjectLive> = new Map()): AllProjects {
  const projects: ProjectRow[] = []
  const stories: AllProjectsStory[] = []
  const moves: AllProjectsStory[] = []
  const asks: ProjectAsk[] = []
  const upNext: UpNextRow[] = []
  const steps: ProjectSteps[] = []
  for (const root of projectRoots(rows)) {
    const project = projectName(root)
    const board = boards.get(root.key)
    const items = board?.items ?? []
    const lv = live.get(root.key) ?? NOTHING_LIVE
    const carded = approvalCarded(lv.approvals, lv.workers)
    const all = projectStories(root.key, project, items, now, carded)
    // The stories Moves draws, which its counts read: one whose approval has its own
    // card is that card. Steps draws them all.
    const own = movesStories(all, carded)
    const plan = boardPlan(items)
    const approvals = lv.approvals.length
    if (lv.questions > 0 || approvals > 0) asks.push({ root: root.key, project, conductor: lv.questions > 0, approvals })
    const count = (move: BoardMove) => own.filter(s => s.move === move).length
    const needsYou = needsYouCount(lv.questions, approvals, own)
    const storyItems = items.filter(i => levelOf(i) === 'story')
    const look = minutesSince(root.last_turn_ts, now)
    projects.push({
      root: root.key,
      project,
      // The right bar's run word: the Conductor's own tool approval waits on the
      // person too, though its turn still runs.
      run: board ? conductorModeRunState({
        paused: lv.paused, needsYou: needsYou > 0 || !!root.pending_approval, run: board.run, hasGoal: !!(board.conductor?.goal || '').trim(), items: items.length,
        working: all.some(s => s.state === 'running') || lv.running > 0 || !!root.running,
      }) : null,
      stories: storyItems.length,
      merged: storyItems.filter(isMerged).length,
      needsYou,
      stuck: count('stuck'),
      late: own.filter(s => s.move === 'conductor' && s.late).length,
      working: count('working'),
      lastLook: Number.isFinite(look) ? look : null,
      loaded: board !== undefined,
    })
    stories.push(...all)
    moves.push(...own)
    plan.backlog.forEach((item, n) => upNext.push({ root: root.key, project, item, rank: n + 1 }))
    steps.push({ root: root.key, project, steps: boardSteps(items, plan), upNext: plan.backlog })
  }
  projects.sort(projectOrder)
  const place = new Map(projects.map((p, n) => [p.root, n]))
  const byProject = (a: { root: string }, b: { root: string }) => (place.get(a.root) ?? 0) - (place.get(b.root) ?? 0)
  // The stories keep their board order: each view sorts them its right-bar way
  // (`storiesByMove`, `byMoveThenWait`). Up next: each project's own pick order, the projects in the table's order.
  upNext.sort((a, b) => a.rank - b.rank || byProject(a, b))
  asks.sort(byProject)
  steps.sort(byProject)
  return { projects, stories, moves, asks, upNext, steps }
}

/** Worker chats one computer holds: open stories with a bound worker whose chat is
 *  not closed, and how many of those are running a turn now. */
export interface ComputerLoad { name: string; here: boolean; chats: number; running: number }

/** Worker chats per computer, from the stories' own `runs_on` and `alive`. `here`
 *  is the name the gateway gives this computer (`GET /api/crew-board/capacity`);
 *  this computer comes first, then linked Crews by name. */
export function computerLoads(boards: Iterable<ProjectBoard>, here: string): ComputerLoad[] {
  const by = new Map<string, ComputerLoad>()
  if (here) by.set(here, { name: here, here: true, chats: 0, running: 0 })
  for (const board of boards) {
    for (const item of board?.items ?? []) {
      if (item.state !== 'open' || !item.has_worker || item.alive === 'closed') continue
      const name = item.runs_on || here
      if (!name) continue
      const load = by.get(name) ?? { name, here: name === here, chats: 0, running: 0 }
      load.chats += 1
      if (item.alive === 'running') load.running += 1
      by.set(name, load)
    }
  }
  return [...by.values()].sort((a, b) => Number(b.here) - Number(a.here) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
}
