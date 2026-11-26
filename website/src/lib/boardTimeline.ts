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
 *    verdict has come since. From `LATE_MINUTES` after the report the rest of it is
 *    drawn late: the wait's first minutes are not late, only what follows.
 *  - `idle`: nobody is named on it -- it was created and never bound, or its
 *    Conductor has just judged it and no worker has taken it up since.
 *  - `person`: the conductor wrote down a question for them (`ask`), the story's
 *    newest report named the person (its `reason`, which the row carries), or it is
 *    the TRAILING stretch of a row Moves says needs them. A wait on the person is
 *    never the Conductor's, so it is never late and never owed in the Conductor's
 *    gap. An OLDER report's line does not record whose move it named (the ledger
 *    keeps only the newest report's reason), so its stretch stays `conductor`.
 *  - `stuck`: only ever the trailing stretch, of a row Moves says is stuck (nobody
 *    is left to move it). The ledger writes no line when a chat closes, so no past
 *    stretch can be called stuck.
 *
 *  The trailing stretch of an open story is the row's (`rowStretch`): the row's own
 *  move (`boardMove`) and lateness decide it in both directions, so a stuck row never
 *  sits over a working bar and a working row never over a yellow one. A stretch is
 *  late from the line that opened it, even when that line is older than the window.
 *  Each stretch is keyed by the time of the line that opened it (`opened`), never
 *  by where the window cuts it, so a read that moves the window, or a tick that
 *  turns a wait late, changes only the open end of a bar.
 *
 *  A worker's report is a diamond. Consecutive `progress` reports are coalesced in
 *  the ledger, which keeps only the newest, so a long run with no status change in
 *  it has ONE progress line and draws ONE diamond -- its stretch is unbroken all the
 *  same, because the `bind` or `decision` before it opened it.
 *
 *  Pure, so the view and its tests share one reading. Times are milliseconds. */

import type { TimelineKind, TimelineLine } from '../api/boardTimeline'
import { CONDUCTOR_WAIT_LOUD_MINUTES, type BoardMove } from './boardMoves'

/** A wait on the Conductor this long is late, here as in Moves: the ONE number,
 *  owned by the Board's move reading and named again only for this view's readers.
 *  The legend says it in words, so the rule never lives only in a colour. */
export const LATE_MINUTES = CONDUCTOR_WAIT_LOUD_MINUTES

export type SpanKind = 'working' | 'conductor' | 'person' | 'stuck' | 'idle'

/** One stretch of a story's lane. `late` only on a `conductor` stretch. `opened` is
 *  the time of the line that opened it, before the window clamps `from`: a late
 *  wait is two spans with one `opened`, the wait before the rule and after it. */
export interface TimelineSpan {
  kind: SpanKind
  from: number
  to: number
  late: boolean
  opened: number
}

export interface TimelineLane {
  spans: TimelineSpan[]
  /** When a worker reported: the diamond on the lane. */
  reports: number[]
  /** Every stretch this story waited on its Conductor, each whole (not cut at the
   *  rule), for the Conductor's gap. A wait on the person is never in it. */
  owed: Array<{ from: number; to: number }>
}

/** The reasons a blocked or question report names the person by, as the ledger's
 *  `work_vocab.WORK_BLOCKED_REASONS` spells them. */
const PERSON_REASONS: ReadonlySet<string> = new Set(['approval', 'needs_human'])

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

/** Who writes a line: the Conductor's lane ticks on its own writes only. A report
 *  is its worker's and a steer the person's, by kind; a `plan` line is the person's
 *  when the server marks it `by: 'person'` (a rank drag), else the Conductor's. */
const NOT_THE_CONDUCTORS: ReadonlySet<TimelineKind> = new Set<TimelineKind>(['report', 'steer'])

function conductorWrote(line: TimelineLine): boolean {
  return !NOT_THE_CONDUCTORS.has(line.kind) && line.by !== 'person'
}

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

/** What an open row says now: its move in Moves, whether that move is late, and
 *  whether a worker is bound to it (`has_worker`; absent from an older gateway). */
export interface RowNow {
  move: BoardMove
  late: boolean
  hasWorker?: boolean
  /** When the wait its Conductor owes began (the row's report time), for where a
   *  late Conductor's move turns red; the stretch's own line when absent. */
  waitSince?: number
}

/**
 * The trailing stretch of an open story, read off its row: the ONE owner of whose
 * move it is now is the row's `boardMove`, so the bar follows it both ways. `trailing`
 * is what the ledger's last line left it as. A Working row is working whatever that
 * line was (a turn that runs after a verdict writes none), unless no worker is bound
 * to it: a story not started is Working in Moves with nobody on it. Only a
 * Conductor's move is ever late, as late as its row.
 */
export function rowStretch(row: RowNow, trailing: SpanKind): { kind: SpanKind; late: boolean } {
  switch (row.move) {
    case 'needs_you': return { kind: 'person', late: false }
    case 'stuck': return { kind: 'stuck', late: false }
    case 'conductor': return { kind: 'conductor', late: row.late }
    case 'github': return { kind: 'idle', late: false }
    case 'working': {
      // An older gateway sends no `has_worker`: the ledger's last line decides then.
      const nobody = row.hasWorker === undefined ? trailing === 'idle' : !row.hasWorker
      return { kind: nobody ? 'idle' : 'working', late: false }
    }
  }
}

/**
 * One story's lane over `[since, until]`.
 *
 * `row` is what the story's row says now (`RowNow`), for an open story only: the
 * ledger's lines cannot know a chat closed or a turn started again, so the trailing
 * stretch is drawn as the row reads (`rowStretch`). `closedAt`, when the item is
 * closed, ends the lane there instead of at `until`. `reason` is the item's own
 * (its newest report's): when it names the person, that report's wait is theirs.
 */
export function timelineLane(
  lines: readonly TimelineLine[],
  { since, until, row, closedAt, reason }: {
    since: number
    until: number
    row?: RowNow
    closedAt?: number | null
    reason?: string | null
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
  const reports: number[] = []
  for (const line of placed) {
    if (line.kind === 'report' && at(line) >= since && at(line) <= end) reports.push(at(line))
  }
  // A line that says nothing about who has the work is not a boundary either: the
  // stretch before it runs through to the next line that does.
  const bounds = placed.filter(line => kindAfter(line) !== 'carries')
  // The newest report is the one whose reason the item still carries.
  const newestReport = bounds.filter(line => line.kind === 'report').pop()
  const stretches: Array<{ kind: SpanKind; from: number; to: number; opened: number; late?: boolean; lateFrom?: number }> = []
  bounds.forEach((line, n) => {
    const starts = Math.max(at(line), since)
    const stops = Math.min(n + 1 < bounds.length ? at(bounds[n + 1]) : end, end)
    let kind = kindAfter(line)
    if (kind === 'ends' || kind === 'carries' || stops <= starts) return
    const asked = line.status === 'blocked' || line.status === 'question'
    if (kind === 'conductor' && line === newestReport && asked && PERSON_REASONS.has(reason ?? '')) kind = 'person'
    stretches.push({ kind, from: starts, to: stops, opened: at(line) })
  })
  // The stretch running up to now is the row's, whichever way the two differ. A
  // story that ended has no stretch running up to now, whatever row it had.
  const last = stretches[stretches.length - 1]
  if (row && end === until && last && last.to >= end) {
    const now = rowStretch(row, last.kind)
    last.kind = now.kind
    last.late = now.late
    if (row.waitSince != null && Number.isFinite(row.waitSince)) last.lateFrom = row.waitSince + LATE_MINUTES * 60_000
  }
  const spans: TimelineSpan[] = []
  const owed: TimelineLane['owed'] = []
  for (const s of stretches) {
    if (s.kind !== 'conductor') {
      spans.push({ kind: s.kind, from: s.from, to: s.to, opened: s.opened, late: false })
      continue
    }
    owed.push({ from: s.from, to: s.to })
    // Late from the line that opened it, not from the window's edge: a wait that
    // began before the window is as old as it is, however little of it shows. The
    // trailing one is as late as its row says, and turns red where its row did.
    const lateFrom = s.lateFrom ?? s.opened + LATE_MINUTES * 60_000
    const late = s.late ?? lateFrom <= s.to
    // A row late a moment before this read's clock reaches its rule (the two
    // clocks differ by up to a poll) is red from the read's own end.
    const cut = !late ? s.to : Math.min(Math.max(lateFrom, s.from), s.to)
    if (cut > s.from) spans.push({ kind: 'conductor', from: s.from, to: cut, opened: s.opened, late: false })
    if (late) spans.push({ kind: 'conductor', from: cut < s.to ? cut : Math.max(s.from, s.to - 1), to: s.to, opened: s.opened, late: true })
  }
  return { spans, reports, owed }
}

export interface ConductorGap { from: number; to: number; minutes: number; late: boolean }

export interface ConductorLane {
  /** When the conductor wrote on the board: one tick each. */
  ticks: number[]
  /** The stretch of the window with no write the lane names; `null` only when the
   *  board sent no line, so there is no Conductor to speak of. `late` when a story
   *  waited on the Conductor through `LATE_MINUTES` of it: red means something is
   *  late, never just a long, healthy run with nothing owed. A late gap is named
   *  whenever the window has one, however long a quiet gap beside it; else the
   *  longest gap. */
  gap: ConductorGap | null
}

/**
 * The Conductor's own lane: every line the conductor wrote, across every item, and
 * the stretch of the window it wrote nothing in that the lane names.
 *
 * The ledger records the conductor's WRITES, never its reads, so a tick is a move
 * it made and a gap is a stretch with no move in it. The window's two edges bound
 * the gaps: an item's lead line from before the window is that ITEM's newest line,
 * not the Conductor's newest write, so it never moves where a gap starts. A Conductor
 * silent for the whole window shows the whole window as its gap.
 *
 * `owed` is every stretch a story waited on its Conductor (the lanes' `owed`); EVERY
 * gap is tested, and one is late when an owed stretch runs through `LATE_MINUTES` of
 * it. The lane names the longest late gap, else the longest gap.
 */
export function conductorLane(
  byItem: Readonly<Record<string, readonly TimelineLine[]>>,
  { since, until, owed = [] }: { since: number; until: number; owed?: readonly { from: number; to: number }[] },
): ConductorLane {
  const all = Object.values(byItem).flat().filter(line => Number.isFinite(at(line)))
  const ticks = all.filter(conductorWrote).map(at).filter(ms => ms >= since && ms <= until).sort((a, b) => a - b)
  if (!all.length) return { ticks: [...new Set(ticks)], gap: null }
  const bounds = [since, ...ticks, until]
  const gaps: ConductorGap[] = []
  for (let n = 1; n < bounds.length; n += 1) {
    const minutes = (bounds[n] - bounds[n - 1]) / 60_000
    if (minutes <= 0) continue
    const [from, to] = [bounds[n - 1], bounds[n]]
    const through = (s: { from: number; to: number }) => (Math.min(s.to, to) - Math.max(s.from, from)) / 60_000
    gaps.push({ from, to, minutes, late: minutes >= LATE_MINUTES && owed.some(s => through(s) >= LATE_MINUTES) })
  }
  // The first of equals: the earliest gap of the longest length leads.
  const longest = (list: ConductorGap[]) => list.reduce<ConductorGap | null>((best, g) => (g.minutes > (best?.minutes ?? 0) ? g : best), null)
  return { ticks: [...new Set(ticks)], gap: longest(gaps.filter(g => g.late)) ?? longest(gaps) }
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

/** The next whole hour on the reader's own clock at or after `ms`. A whole UTC hour
 *  is half past in a half-hour time zone, and the label prints the local hour. */
function localHourAtOrAfter(ms: number): number {
  const d = new Date(ms)
  d.setMinutes(0, 0, 0)
  if (d.getTime() < ms) {
    d.setTime(d.getTime() + 3_600_000)
    d.setMinutes(0, 0, 0)
  }
  return d.getTime()
}

/** The hour marks under the axis: each whole local hour inside the window, at most
 *  six of them, so a 24 h axis is labelled every four hours instead of every hour. */
export function axisHours(since: number, until: number): number[] {
  const whole: number[] = []
  for (let ms = localHourAtOrAfter(since); ms < until; ms = localHourAtOrAfter(ms + 1)) whole.push(ms)
  const step = Math.max(1, Math.ceil(whole.length / 6))
  return whole.filter((_ms, n) => n % step === 0)
}
