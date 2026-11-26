/** What happened to each story over the last hours, as bars on one time axis.
 *
 *  Every bar comes from a line the work ledger wrote: nothing is estimated, and
 *  there is no forecast, no percent and no shading. A line OPENS a stretch that
 *  runs until the next line (or until the read time), and which kind of stretch it
 *  is follows from who the line left the move with:
 *
 *  - `working`: a worker has it (it was dispatched, answered, steered, or reported
 *    progress).
 *  - `conductor`: a worker reported done, blocked or asked, and no answer or
 *    verdict has come since. Past `LATE_MINUTES` it is drawn late.
 *  - `idle`: nobody is named on it -- it was created and never bound, or its
 *    Conductor has just judged it and no worker has taken it up since.
 *  - `person`: the conductor wrote down a question for them (`ask`), or it is the
 *    TRAILING stretch of a row the board read says waits on the person
 *    (`awaits_person`). Only the trailing one: a past report's line does not record
 *    whose move it named, so an older stretch stays `conductor` rather than
 *    claiming the person was waited on then.
 *
 *  A worker's report is a diamond. Consecutive `progress` reports are coalesced in
 *  the ledger, which keeps only the newest, so a long run with no status change in
 *  it has ONE progress line and draws ONE diamond -- its stretch is unbroken all the
 *  same, because the `bind` or `decision` before it opened it.
 *
 *  Pure, so the view and its tests share one reading. Times are milliseconds. */

import type { TimelineKind, TimelineLine } from '../api/boardTimeline'
import { CONDUCTOR_WAIT_LOUD_MINUTES } from './boardMoves'

/** A wait on the Conductor this long is late, here as in Moves: the ONE number,
 *  owned by the Board's move reading and named again only for this view's readers.
 *  The legend says it in words, so the rule never lives only in a colour. */
export const LATE_MINUTES = CONDUCTOR_WAIT_LOUD_MINUTES

export type SpanKind = 'working' | 'conductor' | 'person' | 'idle'

/** One stretch of a story's lane. `late` only on a `conductor` stretch. */
export interface TimelineSpan {
  kind: SpanKind
  from: number
  to: number
  late: boolean
}

export interface TimelineLane {
  spans: TimelineSpan[]
  /** When a worker reported: the diamond on the lane. */
  reports: number[]
}

/**
 * What each kind of line leaves the story in, by kind, so a kind with no reading is
 * a compile error rather than a bar drawn as the wrong thing.
 *
 * - `ends`: the story is over here, and the lane stops.
 * - `carries`: the line says nothing about who has the work (a `plan` write is
 *   bookkeeping), so the stretch before it runs on through.
 * - a verdict leaves it `idle`: the Conductor has made its move, and whether a
 *   worker picked it up again shows only in the next `bind`, `decision` or report.
 */
const AFTER: Record<TimelineKind, SpanKind | 'ends' | 'carries'> = {
  create: 'idle',
  bind: 'working',
  decision: 'working',
  steer: 'working',
  verdict: 'idle',
  plan: 'carries',
  // The conductor wrote down a question for the person: it is theirs until answered.
  ask: 'person',
  // Overridden by the report's own status below.
  report: 'working',
  close: 'ends',
  retire: 'ends',
  destroy: 'ends',
}

/** Who writes a line: the Conductor's lane ticks on its own writes only. */
const NOT_THE_CONDUCTORS: ReadonlySet<TimelineKind> = new Set<TimelineKind>(['report', 'steer'])

/** The stretch a line opens, before the trailing `person` rule. */
function kindAfter(line: TimelineLine): SpanKind | 'ends' | 'carries' {
  if (line.kind === 'report') {
    // Progress and an answer leave it with its worker; done, blocked and a question
    // are its Conductor's to pick up.
    return line.status === 'progress' || line.status === 'reply' ? 'working' : 'conductor'
  }
  return AFTER[line.kind] ?? 'idle'
}

function at(line: TimelineLine): number {
  return Date.parse(line.ts)
}

/**
 * One story's lane over `[since, until]`.
 *
 * `awaitsPerson` is the board row's own flag, so the trailing stretch of a row that
 * waits on the person is drawn as theirs. `closedAt`, when the item is closed, ends
 * the lane there instead of at `until`.
 */
export function timelineLane(
  lines: readonly TimelineLine[],
  { since, until, awaitsPerson = false, closedAt }: {
    since: number
    until: number
    awaitsPerson?: boolean
    closedAt?: number | null
  },
): TimelineLane {
  const placed = lines
    .filter(line => Number.isFinite(at(line)))
    .slice()
    .sort((a, b) => at(a) - at(b))
  // Where the lane stops: the item's own end, whichever comes first -- the line that
  // ended it (closed, retired, destroyed), the closed stamp the board read carries,
  // else the window's right edge.
  const ended = placed.find(line => kindAfter(line) === 'ends')
  const end = Math.min(
    until,
    closedAt != null && Number.isFinite(closedAt) ? closedAt : until,
    ended ? at(ended) : until,
  )
  const spans: TimelineSpan[] = []
  const reports: number[] = []
  for (const line of placed) {
    if (line.kind === 'report' && at(line) >= since && at(line) <= end) reports.push(at(line))
  }
  // A line that says nothing about who has the work is not a boundary either: the
  // stretch before it runs through to the next line that does.
  const bounds = placed.filter(line => kindAfter(line) !== 'carries')
  bounds.forEach((line, n) => {
    const starts = Math.max(at(line), since)
    const stops = Math.min(n + 1 < bounds.length ? at(bounds[n + 1]) : end, end)
    const kind = kindAfter(line)
    if (kind === 'ends' || kind === 'carries' || stops <= starts) return
    const minutes = (stops - starts) / 60_000
    spans.push({
      kind,
      from: starts,
      to: stops,
      late: kind === 'conductor' && minutes >= LATE_MINUTES,
    })
  })
  // The last stretch of a row that waits on the person is theirs, not its
  // conductor's: `awaits_person` is about the move the row holds NOW.
  const last = spans[spans.length - 1]
  if (awaitsPerson && last && last.kind === 'conductor' && last.to >= end) {
    spans[spans.length - 1] = { ...last, kind: 'person', late: false }
  }
  return { spans, reports }
}

export interface ConductorLane {
  /** When the conductor wrote on the board: one tick each. */
  ticks: number[]
  /** The longest stretch inside the window with no write at all, when it is the
   *  longest by itself; `null` when there is nothing to compare (fewer than two
   *  bounds) or the window holds no write. */
  longestGap: { from: number; to: number; minutes: number } | null
}

/**
 * The Conductor's own lane: every line the conductor wrote, across every item, and
 * the longest stretch of the window it wrote nothing in.
 *
 * The ledger records the conductor's WRITES, never its reads, so a tick is a move
 * it made and the gap is a stretch with no move in it. Both edges of the window
 * bound the gap, so a conductor that wrote nothing for the first three hours shows
 * that as its gap.
 */
export function conductorLane(
  byItem: Readonly<Record<string, readonly TimelineLine[]>>,
  { since, until }: { since: number; until: number },
): ConductorLane {
  const ticks = Object.values(byItem)
    .flat()
    .filter(line => !NOT_THE_CONDUCTORS.has(line.kind) && Number.isFinite(at(line)))
    .map(at)
    .filter(ms => ms >= since && ms <= until)
    .sort((a, b) => a - b)
  let longestGap: ConductorLane['longestGap'] = null
  if (ticks.length) {
    const bounds = [since, ...ticks, until]
    for (let n = 1; n < bounds.length; n += 1) {
      const minutes = (bounds[n] - bounds[n - 1]) / 60_000
      if (minutes > (longestGap?.minutes ?? 0)) {
        longestGap = { from: bounds[n - 1], to: bounds[n], minutes }
      }
    }
  }
  return { ticks: [...new Set(ticks)], longestGap }
}

/** Where a stretch sits on the axis, as percentages of the window. Clamped, so a
 *  line a second older than the window still draws from the left edge. */
export function place(from: number, to: number, since: number, until: number): { left: number; width: number } {
  const span = Math.max(1, until - since)
  const left = Math.min(100, Math.max(0, ((from - since) / span) * 100))
  const right = Math.min(100, Math.max(0, ((to - since) / span) * 100))
  return { left, width: Math.max(0, right - left) }
}

/** Where one moment sits on the axis, as a percentage of the window. */
export function mark(ms: number, since: number, until: number): number {
  return place(ms, ms, since, until).left
}

/** The hour marks under the axis: each whole hour inside the window, at most six of
 *  them, so a 24 h axis is labelled every four hours instead of every hour. */
export function axisHours(since: number, until: number): number[] {
  const first = Math.ceil(since / 3_600_000) * 3_600_000
  const whole: number[] = []
  for (let ms = first; ms < until; ms += 3_600_000) whole.push(ms)
  const step = Math.max(1, Math.ceil(whole.length / 6))
  return whole.filter((_ms, n) => n % step === 0)
}
