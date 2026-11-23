/** The board plan, read on the client: epics, the sprint, the ranked backlog and
 *  the releases a conductor's work ledger carries (`work_plan_vocab.py`). Pure
 *  functions only, so the Backlog view and its tests share one reading. */
import { PLAN_STEPS, type PlanLevel, type PlanStep, type WorkBoardItem, type WorkBoardRelease, type WorkItemPlan } from '../api/crewBoard'

export function planOf(item: WorkBoardItem): WorkItemPlan {
  return item.plan ?? {}
}

/** An item with no level is a story, as the store reads it. */
export function levelOf(item: WorkBoardItem): PlanLevel {
  return planOf(item).level ?? 'story'
}

/** Still on the board: not accepted, rejected or abandoned. */
export function isOpen(item: WorkBoardItem): boolean {
  return item.state === 'open'
}

/** Stored, not started: an open story marked `backlog`. An epic or a sub-task is
 *  never bound, so neither is ever picked from the backlog (`next_pick`). */
export function inBacklog(item: WorkBoardItem): boolean {
  return isOpen(item) && !!planOf(item).backlog && levelOf(item) === 'story'
}

/** Merged or shipped, by its step or by the conductor's acceptance. */
export function isMerged(item: WorkBoardItem): boolean {
  const step = planOf(item).step
  return item.state === 'accepted' || step === 'merged' || step === 'released'
}

/** 0 for To do through 5 for Released; -1 when no step is set. */
export function stepIndex(step: PlanStep | undefined): number {
  return step ? PLAN_STEPS.indexOf(step) : -1
}

/** `next_pick`'s order on the server: ranked rows first by rank, then the
 *  unranked oldest first, the id breaking a tie. */
export function backlogCompare(a: WorkBoardItem, b: WorkBoardItem): number {
  const ra = planOf(a).rank, rb = planOf(b).rank
  if ((ra === undefined) !== (rb === undefined)) return ra === undefined ? 1 : -1
  if (ra !== undefined && rb !== undefined && ra !== rb) return ra - rb
  const ca = a.created_at || '', cb = b.created_at || ''
  if (ca !== cb) return ca < cb ? -1 : 1
  return a.item_id < b.item_id ? -1 : a.item_id > b.item_id ? 1 : 0
}

export interface BoardPlan {
  epics: WorkBoardItem[]
  /** Open stories (and unparented tasks) the conductor is working: not in the backlog. */
  sprint: WorkBoardItem[]
  /** Open backlog stories in pick order. */
  backlog: WorkBoardItem[]
  /** Every item by the `it_` id of its parent. */
  children: Map<string, WorkBoardItem[]>
  byId: Map<string, WorkBoardItem>
  /** Whether any item carries a plan: a board that never planned keeps its old view. */
  planned: boolean
}

export function boardPlan(items: readonly WorkBoardItem[]): BoardPlan {
  const byId = new Map(items.map(i => [i.item_id, i]))
  const children = new Map<string, WorkBoardItem[]>()
  const epics: WorkBoardItem[] = [], sprint: WorkBoardItem[] = [], backlog: WorkBoardItem[] = []
  let planned = false
  for (const item of items) {
    if (item.plan) planned = true
    const parent = planOf(item).parent
    if (parent && byId.has(parent)) children.set(parent, [...(children.get(parent) ?? []), item])
    const level = levelOf(item)
    if (level === 'epic') { epics.push(item); continue }
    // A task under a story shows inside that story, not as a row of its own.
    if (level === 'task' && parent && byId.has(parent)) continue
    if (inBacklog(item)) backlog.push(item)
    else if (isOpen(item)) sprint.push(item)
  }
  backlog.sort(backlogCompare)
  return { epics, sprint, backlog, children, byId, planned }
}

/** The epic a story belongs to (a task's through its story), or null. */
export function epicOf(item: WorkBoardItem, byId: ReadonlyMap<string, WorkBoardItem>): WorkBoardItem | null {
  let at: WorkBoardItem | undefined = item
  for (let hops = 0; at && hops < 3; hops++) {
    if (levelOf(at) === 'epic') return at === item ? null : at
    const parent: string | undefined = planOf(at).parent
    at = parent ? byId.get(parent) : undefined
  }
  return null
}

/** Done and total among an item's children: an epic's stories, a story's tasks. */
export function childProgress(item: WorkBoardItem, children: ReadonlyMap<string, readonly WorkBoardItem[]>): { done: number; total: number } {
  const kids = children.get(item.item_id) ?? []
  return { done: kids.filter(isMerged).length, total: kids.length }
}

/** Seats in use against the goal's cap: open items outside the backlog that are
 *  neither epics nor sub-tasks (a sub-task rides its story's worker), as
 *  `counts_toward_concurrency` reads it. */
export function seatsBusy(items: readonly WorkBoardItem[]): number {
  return items.filter(i => isOpen(i) && !planOf(i).backlog && levelOf(i) === 'story').length
}

/** The rank writes a drop of the row at `from` onto `to` (an index in the list
 *  without it) needs, so the server's order then matches the list as shown. The
 *  dragged row takes the midpoint of its new neighbours, one past the end at
 *  either edge. An unranked row sorts after every ranked one, so the unranked
 *  rows a drop lands below are ranked too, one step apart after the last ranked
 *  row: a board that never ranked keeps the order it showed. When the new
 *  neighbours share a rank (or their midpoint rounds onto one of them), no rank
 *  fits between them, so every row is renumbered 1..n in the new order and only
 *  the rows whose rank changes are written. */
export function ranksForDrop(order: readonly WorkBoardItem[], from: number, to: number): Array<{ item_id: string; rank: number }> {
  const effective: number[] = []
  let last = 0
  for (const item of order) {
    const r = planOf(item).rank
    last = r !== undefined ? r : last + 1
    effective.push(last)
  }
  const rest = order.filter((_, n) => n !== from)
  const restRank = effective.filter((_, n) => n !== from)
  const writes = rest.slice(0, Math.max(0, to))
    .map((item, n) => ({ item, rank: restRank[n] }))
    .filter(({ item }) => planOf(item).rank === undefined)
    .map(({ item, rank }) => ({ item_id: item.item_id, rank }))
  const rank = rest.length === 0 ? 1
    : to <= 0 ? restRank[0] - 1
      : to >= rest.length ? restRank[rest.length - 1] + 1
        : (restRank[to - 1] + restRank[to]) / 2
  const fits = (to <= 0 || rank > restRank[to - 1]) && (to >= rest.length || rank < restRank[to])
  if (!fits) {
    return moveRow(order, from, to)
      .map((item, n) => ({ item_id: item.item_id, rank: n + 1, was: planOf(item).rank }))
      .filter(w => w.was !== w.rank)
      .map(({ item_id, rank }) => ({ item_id, rank }))
  }
  return [...writes, { item_id: order[from].item_id, rank }]
}

/** Where the row at `index` sits once the row at `active` drops onto `over`
 *  (-1 for either: no drag), the same move `moveRow` makes. */
export function projectedIndex(index: number, active: number, over: number): number {
  if (active < 0 || over < 0) return index
  if (index === active) return over
  if (active < index && index <= over) return index - 1
  if (over <= index && index < active) return index + 1
  return index
}

/** `order` with the row at `from` moved to `to`. */
export function moveRow<T>(order: readonly T[], from: number, to: number): T[] {
  const next = [...order]
  const [row] = next.splice(from, 1)
  next.splice(Math.max(0, Math.min(to, next.length)), 0, row)
  return next
}

export interface ReleaseView {
  release: WorkBoardRelease
  items: WorkBoardItem[]
  merged: number
}

/** The board's releases newest first, each with the stories that ship in it. */
export function releaseViews(releases: readonly WorkBoardRelease[] | undefined, items: readonly WorkBoardItem[]): ReleaseView[] {
  return [...(releases ?? [])].reverse().map(release => {
    const mine = items.filter(i => planOf(i).release === release.name && levelOf(i) !== 'epic')
    return { release, items: mine, merged: mine.filter(isMerged).length }
  })
}

/** Theme tones an epic's chip takes, by its place among the epics. */
export const EPIC_TONES = ['var(--danger)', 'var(--info)', 'var(--aim)', 'var(--warn)', 'var(--ok)', 'var(--accent)'] as const

export function epicTone(epic: WorkBoardItem | null, epics: readonly WorkBoardItem[]): string {
  if (!epic) return 'var(--muted)'
  const n = epics.findIndex(e => e.item_id === epic.item_id)
  return EPIC_TONES[(n < 0 ? 0 : n) % EPIC_TONES.length]
}

/** A conductor's estimate tag, `est: 2 h`, `est: 1.5 h` or `est: 45 m`: the number
 *  and its unit, hours or minutes. The estimate rides `plan.tags` and nothing else. */
const ESTIMATE_TAG = /^est:\s*(\d+(?:\.\d+)?)\s*(h|m)$/i

export function isEstimateTag(tag: string): boolean {
  return ESTIMATE_TAG.test(tag.trim())
}

/** An item's estimate in minutes, read from its first estimate tag; undefined when it has none. */
export function estimateMinutes(item: WorkBoardItem): number | undefined {
  for (const tag of planOf(item).tags ?? []) {
    const m = ESTIMATE_TAG.exec(tag.trim())
    if (m) return Number(m[1]) * (m[2].toLowerCase() === 'h' ? 60 : 1)
  }
  return undefined
}

/** `rows` by estimate, smallest first, the rows with none last; a tie keeps the
 *  order given. A copy: the ranked order it came from is not touched. */
export function byEstimate(rows: readonly WorkBoardItem[]): WorkBoardItem[] {
  return rows
    .map((item, n) => ({ item, n, est: estimateMinutes(item) }))
    .sort((a, b) => (a.est ?? Infinity) - (b.est ?? Infinity) || a.n - b.n)
    .map(r => r.item)
}
