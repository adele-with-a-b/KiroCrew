/** Who must act on a story, and where it is in the process, for the right bar's
 *  Board views (Moves and Steps). Read from what the conductor mode panel already
 *  works out per story -- its milestone state and how long its last report has
 *  waited on its Conductor -- and from the plan step the Board read derives. Pure,
 *  so the views and their tests share one reading. Nothing here is stored: every
 *  move is derived on each read, and no Conductor writes a field for it. */
import { PLAN_STEPS, type PlanStep, type WorkBoardItem } from '../api/crewBoard'
import { isMerged, levelOf, planOf, type BoardPlan } from './crewPlan'

/** A story's state on the Board, from its reports and its session (the panel's
 *  `milestoneState`). */
export type MilestoneState = 'done' | 'not_done' | 'needs_you' | 'running' | 'waiting' | 'quiet' | 'stuck' | 'next'

/** A wait on the Conductor this long is late, drawn red: a patrol looks far more often. */
export const CONDUCTOR_WAIT_LOUD_MINUTES = 10

/** Who must act, in the order that hurts most. */
export type BoardMove = 'needs_you' | 'stuck' | 'conductor' | 'github' | 'working'
export const BOARD_MOVES: readonly BoardMove[] = ['needs_you', 'stuck', 'conductor', 'github', 'working']

/** The item's last report has its answer: for a question the
 *  board's own `outstanding` (a newer decision or person's steer), for a blocked
 *  report the conductor's `decide`, or a decision / steer after the newest report
 *  on the event tail, as `answered_since_report` reads it on the server. */
export function reportAnswered(item: WorkBoardItem): boolean {
  if (item.status === 'question') return !item.outstanding
  // Stamps may carry different offsets (a remote peer keeps its own), so they
  // are compared as instants; one that does not parse answers nothing.
  const reported = item.last_report_at ? Date.parse(item.last_report_at) : NaN
  const atOrAfterReport = (ts: string | null | undefined) => {
    const t = ts ? Date.parse(ts) : NaN
    return Number.isFinite(t) && (!item.last_report_at || (Number.isFinite(reported) && t >= reported))
  }
  if (atOrAfterReport(item.decided_at)) return true
  const events = item.events || []
  let last = -1
  events.forEach((e, n) => { if (e.kind === 'report') last = n })
  if (last >= 0) return events.slice(last + 1).some(e => e.kind === 'decision' || e.kind === 'steer')
  return events.some(e => (e.kind === 'decision' || e.kind === 'steer') && atOrAfterReport(e.ts))
}

/** Who must act on an open story. `conductorWait` is `conductorWaitMinutes` (null
 *  when its Conductor owes nothing).
 *  - needs you: the person must answer or approve;
 *  - stuck: nothing is left to move it (its chat closed, the board unread, or its
 *    worker went quiet with its own move unmade);
 *  - the Conductor's move: a report it has not answered or judged;
 *  - working, when its Conductor handed a done report back: the move follows the
 *    newest of report and decision (`reportAnswered`), so a decision after the
 *    report is the worker's to act on, never a wait on the Conductor that cannot
 *    turn late; with nobody on it (its chat closed, the board unread) the panel's
 *    `milestoneState` reads it stuck, as an open report's;
 *  - waits on GitHub: idle on a pull request, its own loop waiting on it included;
 *  - working: anything else still open, a story not started included, and a loop
 *    with no pull request to wait on (it waits on its own next check, not GitHub). */
export function boardMove(item: WorkBoardItem, state: MilestoneState, conductorWait: number | null): BoardMove {
  if (state === 'needs_you') return 'needs_you'
  if (state === 'stuck' || state === 'quiet') return 'stuck'
  if (conductorWait != null) return 'conductor'
  if (state === 'done' && reportAnswered(item)) return 'working'
  const idle = item.alive !== 'running'
  if (idle && item.pr != null && ((state === 'running' && item.looping === true) || state === 'waiting' || state === 'done')) return 'github'
  // Reported done and judged pending with no PR to wait on: still the Conductor's to close.
  if (state === 'done') return 'conductor'
  return 'working'
}

/** The steps a story is grouped under in Steps, furthest along first. */
export const STEPS_FURTHEST_FIRST: readonly PlanStep[] = [...PLAN_STEPS].reverse()

/** The step a story is at: the plan's (the Board read derives one for a story nobody
 *  planned), else merged when accepted, else To do (an older gateway). */
export function storyStep(item: WorkBoardItem): PlanStep {
  const step = planOf(item).step
  if (step) return step
  return isMerged(item) ? 'merged' : 'todo'
}

export interface BoardSteps {
  /** Stories at each step: the open ones outside Up next, and the accepted ones.
   *  Merged counts every story that reached it, so Released is a part of Merged
   *  ("4 of 6 released"), as the Landed lines read. */
  strip: Record<PlanStep, number>
  /** The open stories outside Up next by step, furthest along first. Merged and
   *  Released appear only while an open story sits there; the others always do. */
  groups: Array<{ step: PlanStep; items: WorkBoardItem[] }>
  /** Accepted stories: how many reached Merged or further, and of those Released;
   *  plus the stories closed without being accepted. */
  landed: { merged: number; released: number; notDone: number }
}

/** A story as the Board counts it: not an epic, and not a sub-task inside a story. */
function isStory(item: WorkBoardItem, plan: BoardPlan): boolean {
  const level = levelOf(item)
  return level !== 'epic' && !(level === 'task' && plan.byId.has(planOf(item).parent ?? ''))
}

export function boardSteps(items: readonly WorkBoardItem[], plan: BoardPlan): BoardSteps {
  const strip = Object.fromEntries(PLAN_STEPS.map(s => [s, 0])) as Record<PlanStep, number>
  const byStep = new Map<PlanStep, WorkBoardItem[]>()
  for (const item of plan.sprint) {
    const step = storyStep(item)
    strip[step] += 1
    if (step === 'released') strip.merged += 1
    byStep.set(step, [...(byStep.get(step) ?? []), item])
  }
  let merged = 0, released = 0, notDone = 0
  for (const item of items) {
    if (item.state === 'open' || !isStory(item, plan)) continue
    if (item.state !== 'accepted') { notDone += 1; continue }
    merged += 1
    strip.merged += 1
    if (storyStep(item) === 'released') { released += 1; strip.released += 1 }
  }
  const groups = STEPS_FURTHEST_FIRST
    .filter(step => (step !== 'merged' && step !== 'released') || byStep.has(step))
    .map(step => ({ step, items: byStep.get(step) ?? [] }))
  return { strip, groups, landed: { merged, released, notDone } }
}

/** Oldest first: the story waiting longest leads its section. ISO stamps, so a byte
 *  compare orders them, no locale involved. */
export function longestWaitingFirst(a: WorkBoardItem, b: WorkBoardItem): number {
  const x = a.last_report_at || a.created_at || '', y = b.last_report_at || b.created_at || ''
  return x < y ? -1 : x > y ? 1 : 0
}

/** What a Board's rules read of a drawn story. */
export interface MovedStory { item: WorkBoardItem; move: BoardMove }

/** A crew-list row as this reads it: the worker and the ledger item it is bound to. */
export interface BoundWorker { key: string; ledger_item?: string | null }

/** The items whose worker waits on a tool approval with its own card in Needs you:
 *  each live card (`cards`, `crewApprovalRequests` over the slot rows) joined to the
 *  ledger item the crew list binds its worker to (`ledger_item`), and nothing else.
 *  So the story and its card move together the moment an ask or a decision lands, and
 *  a story whose worker the crew list binds to no item yet stays where its own state
 *  puts it, beside the card. Every Board reads this one rule. */
export function approvalCarded(cards: readonly { worker: string }[], workers: readonly BoundWorker[]): Set<string> {
  const carded = new Set<string>()
  for (const card of cards) {
    const item = workers.find(w => w.key === card.worker)?.ledger_item
    if (item) carded.add(item)
  }
  return carded
}

/** The stories Moves draws: one whose approval has its own card (`carded`, from
 *  `approvalCarded`) is that card, not a second row. */
export function movesStories<S extends MovedStory>(stories: readonly S[], carded: ReadonlySet<string>): S[] {
  return stories.filter(s => !(s.move === 'needs_you' && carded.has(s.item.item_id)))
}

/** Needs you's count: the main chat's question card, the crew's approval cards
 *  (`crewApprovalRequests`), and the stories Moves draws there (`movesStories`). */
export function needsYouCount(questions: number, approvals: number, shown: readonly MovedStory[]): number {
  return questions + approvals + shown.filter(s => s.move === 'needs_you').length
}
