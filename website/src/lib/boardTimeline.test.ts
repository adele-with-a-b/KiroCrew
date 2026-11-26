/**
 * What the Timeline draws from the ledger's lines: which stretch each kind of line
 * opens, how long each ran, where it sits on the axis, and the Conductor's own ticks
 * and longest quiet stretch. Every time here is explicit -- no clock is read.
 */
import { describe, expect, it } from 'vitest'
import type { TimelineKind, TimelineLine } from '../api/boardTimeline'
import type { WorkReportStatus } from '../api/crewBoard'
import {
  axisHours, conductorLane, LATE_MINUTES, mark, place, rowStretch, timelineLane, type RowNow,
} from './boardTimeline'

const NOW = Date.parse('2026-11-25T12:00:00Z')
const HOUR = 3_600_000
const SINCE = NOW - 12 * HOUR
const WINDOW = { since: SINCE, until: NOW }

/** A line `h` hours before the read time. */
function line(kind: TimelineKind, h: number, status: WorkReportStatus = null): TimelineLine {
  return { ts: new Date(NOW - h * HOUR).toISOString(), kind, status }
}

const minutes = (span: { from: number; to: number }) => (span.to - span.from) / 60_000

describe('timelineLane', () => {
  // What a row says now. The trailing stretch follows it both ways: what the
  // ledger's last line left it as never outvotes the row's move.
  const row = (move: RowNow['move'], late = false): RowNow => ({ move, late })

  it('opens a stretch per line and runs it to the next one, named by who then had it', () => {
    const lane = timelineLane([
      line('create', 11),
      line('bind', 10),
      line('report', 9, 'progress'),
      line('report', 8, 'done'),
      line('decision', 7),
      line('verdict', 6),
    ], WINDOW)
    // The hour its Conductor owed it is two spans: the wait's first ten minutes, then
    // the late rest of it.
    expect(lane.spans.map(s => s.kind)).toEqual(['idle', 'working', 'working', 'conductor', 'conductor', 'working', 'idle'])
    // Each stretch is exactly the gap to the next line; the last runs to the read time.
    expect(lane.spans.map(minutes)).toEqual([60, 60, 60, LATE_MINUTES, 60 - LATE_MINUTES, 60, 360])
    expect(lane.owed).toEqual([{ from: NOW - 8 * HOUR, to: NOW - 7 * HOUR }])
    // A diamond per worker report, and only those.
    expect(lane.reports).toEqual([NOW - 9 * HOUR, NOW - 8 * HOUR])
  })

  it('marks a wait on its Conductor late only from the rule on', () => {
    const late = timelineLane([line('report', 9, 'blocked'), line('decision', 8)], WINDOW)
    expect(late.spans.slice(0, 2)).toMatchObject([
      { kind: 'conductor', late: false, from: NOW - 9 * HOUR, opened: NOW - 9 * HOUR },
      { kind: 'conductor', late: true, from: NOW - 9 * HOUR + LATE_MINUTES * 60_000, opened: NOW - 9 * HOUR },
    ])
    // Answered inside the rule: one stretch, not drawn late.
    const quick = timelineLane([
      line('report', 9, 'question'),
      { ...line('decision', 9), ts: new Date(NOW - 9 * HOUR + (LATE_MINUTES - 1) * 60_000).toISOString() },
    ], WINDOW)
    expect(quick.spans[0]).toMatchObject({ kind: 'conductor', late: false })
    expect(minutes(quick.spans[0])).toBe(LATE_MINUTES - 1)
    expect(quick.spans[1].kind).toBe('working')
  })

  it('measures a wait from the line that opened it, not from the window edge', () => {
    // Asked 5 minutes before a 6 h window opened, answered 6 minutes into it: 11
    // minutes waited, past the rule, though only 6 of them show; the last one is late.
    const six = { since: NOW - 6 * HOUR, until: NOW }
    const straddle = timelineLane([
      { ...line('report', 6), status: 'question', ts: new Date(NOW - 6 * HOUR - 5 * 60_000).toISOString() },
      { ...line('decision', 6), ts: new Date(NOW - 6 * HOUR + 6 * 60_000).toISOString() },
    ], six)
    expect(straddle.spans.slice(0, 2)).toMatchObject([
      { kind: 'conductor', from: NOW - 6 * HOUR, late: false },
      { kind: 'conductor', from: NOW - 6 * HOUR + 5 * 60_000, late: true },
    ])
    expect(straddle.spans.slice(0, 2).map(minutes)).toEqual([5, 1])
  })

  it('changes only the open end of a bar from one read to the next, and at the tick it turns late', () => {
    // A story bound before the window, then reported done: read now, then one poll
    // later with the window moved on. Each stretch keeps the line it opened from.
    const lines = [line('bind', 14), { ...line('report', 0), status: 'done' as const, ts: new Date(NOW - 9 * 60_000).toISOString() }]
    const read = (at: number) => timelineLane(lines, { since: at - 12 * HOUR, until: at, row: row('conductor', at - (NOW - 9 * 60_000) >= LATE_MINUTES * 60_000) })
    const before = read(NOW)
    const after = read(NOW + 2 * 60_000)
    const keep = (s: { kind: string; late: boolean; opened: number }) => [s.opened, s.kind, s.late]
    // The bound stretch is cut by the window's edge; its line, kind and lateness stay.
    expect(keep(after.spans[0])).toEqual(keep(before.spans[0]))
    expect(after.spans[0].to).toBe(before.spans[0].to)
    // Nine minutes after the report it is one quiet wait; eleven minutes after, the
    // same wait up to the rule, and a late open end added after it.
    expect(before.spans.slice(1).map(keep)).toEqual([[NOW - 9 * 60_000, 'conductor', false]])
    expect(after.spans.slice(1).map(keep)).toEqual([[NOW - 9 * 60_000, 'conductor', false], [NOW - 9 * 60_000, 'conductor', true]])
    expect(after.spans[1].from).toBe(before.spans[1].from)
    expect(after.spans[2]).toMatchObject({ from: NOW + 60_000, to: NOW + 2 * 60_000 })
  })

  it('starts a story that was already running at the window edge, never before it', () => {
    // The newest line before the window: the route sends it so the lane is not blank.
    const lane = timelineLane([line('bind', 30), line('report', 4, 'progress')], WINDOW)
    expect(lane.spans[0].from).toBe(SINCE)
    expect(minutes(lane.spans[0])).toBe(8 * 60)
    // A report older than the window is no diamond: it is not in the picture.
    expect(lane.reports).toEqual([NOW - 4 * HOUR])
  })

  it('stops the lane where the story ended, and draws nothing after', () => {
    const closed = timelineLane([line('bind', 10), line('close', 7), line('plan', 6)], WINDOW)
    expect(closed.spans).toHaveLength(1)
    expect(closed.spans[0].to).toBe(NOW - 7 * HOUR)
    // The board read's own stamp ends it too, for an item whose close line is older
    // than the window.
    const stamped = timelineLane([line('bind', 10)], { ...WINDOW, closedAt: NOW - 3 * HOUR })
    expect(stamped.spans[0].to).toBe(NOW - 3 * HOUR)
    expect(timelineLane([line('retire', 5)], WINDOW).spans).toEqual([])
  })

  it('reads a question the conductor wrote down, and the row that waits on you now, as yours', () => {
    const asked = timelineLane([line('bind', 10), line('ask', 9)], WINDOW)
    expect(asked.spans.map(s => s.kind)).toEqual(['working', 'person'])
    // A row the board says waits on the person: its LAST stretch is theirs, and the
    // earlier wait on its Conductor stays its Conductor's.
    const waiting = timelineLane([
      line('report', 10, 'done'),
      line('decision', 9),
      line('report', 8, 'question'),
    ], { ...WINDOW, row: row('needs_you') })
    expect(waiting.spans.map(s => s.kind)).toEqual(['conductor', 'conductor', 'working', 'person'])
    expect(waiting.spans.map(s => s.late)).toEqual([false, true, false, false])
    expect(waiting.owed).toEqual([{ from: NOW - 10 * HOUR, to: NOW - 9 * HOUR }])
  })

  it('reads a past wait on the person as theirs, never as the Conductor\'s lateness', () => {
    // Asked the person two hours ago, answered by their steer an hour later, its
    // worker moving again since: the item still carries the report's reason.
    const answered = [line('bind', 10), line('report', 3, 'question'), line('steer', 2)]
    const lane = timelineLane(answered, { ...WINDOW, reason: 'needs_human', row: row('working') })
    expect(lane.spans.map(s => [s.kind, s.late])).toEqual([['working', false], ['person', false], ['working', false]])
    expect(lane.owed).toEqual([])
    // Its hour is in a Conductor gap, and that gap is not late for it.
    expect(conductorLane({ a: answered }, { ...WINDOW, owed: lane.owed }).gap?.late).toBe(false)
    // An approval the worker waits on is the person's too; a question with no reason
    // is the Conductor's, late past the rule.
    expect(timelineLane(answered, { ...WINDOW, reason: 'approval' }).spans[1].kind).toBe('person')
    expect(timelineLane(answered, { ...WINDOW, reason: null }).spans.slice(1, 3).map(s => [s.kind, s.late]))
      .toEqual([['conductor', false], ['conductor', true]])
    // Only the newest report's reason is known: an older question stays the Conductor's.
    const twice = [line('report', 6, 'question'), line('decision', 5), line('report', 3, 'blocked'), line('steer', 2)]
    expect(timelineLane(twice, { ...WINDOW, reason: 'needs_human' }).spans.map(s => s.kind))
      .toEqual(['conductor', 'conductor', 'working', 'person', 'working'])
  })

  it('draws a stuck row stuck, though its last line was a progress report', () => {
    // Its worker's chat closed after it reported progress: the ledger says working.
    const lane = timelineLane([line('bind', 10), line('report', 3, 'progress')], { ...WINDOW, row: row('stuck') })
    expect(lane.spans.map(s => s.kind)).toEqual(['working', 'stuck'])
    expect(lane.spans.every(s => !s.late)).toBe(true)
  })

  it('draws a working row working, though its last line was a question and an ask', () => {
    // Its turn runs again after it asked and its Conductor wrote the question down.
    const lane = timelineLane([line('bind', 10), line('report', 4, 'question'), line('ask', 3)],
      { ...WINDOW, row: row('working') })
    expect(lane.spans.map(s => s.kind)).toEqual(['working', 'conductor', 'conductor', 'working'])
    expect(lane.spans[3].late).toBe(false)
  })

  it('draws a working row working, though a verdict came last and its turn runs after it', () => {
    // Judged, then its worker's turn ran again, which writes no ledger line: the
    // ledger's last line says nobody is on it, the row says Working, with a worker.
    const judged = [line('bind', 10), line('report', 5, 'done'), line('verdict', 3)]
    const lane = timelineLane(judged, { ...WINDOW, row: { move: 'working', late: false, hasWorker: true } })
    expect(lane.spans.at(-1)).toMatchObject({ kind: 'working', late: false })
    // A Working row with no worker bound is a story nobody started: nobody on it.
    expect(timelineLane(judged, { ...WINDOW, row: { move: 'working', late: false, hasWorker: false } }).spans.at(-1)?.kind).toBe('idle')
  })

  it('draws a row that owes its Conductor as owed and late, though a re-bind came last', () => {
    // Blocked, then bound again: the ledger's last line says working, the row says
    // the Conductor owes it since the report and is past the rule.
    const reported = [line('report', 10, 'blocked'), line('bind', 2)]
    const owed = timelineLane(reported, { ...WINDOW, row: { ...row('conductor', true), waitSince: NOW - 10 * HOUR } })
    expect(owed.spans.map(s => [s.kind, s.late])).toEqual([['conductor', false], ['conductor', true], ['conductor', true]])
    expect(owed.owed).toEqual([{ from: NOW - 10 * HOUR, to: NOW - 2 * HOUR }, { from: NOW - 2 * HOUR, to: NOW }])
    // Not past the rule on its row: not late on its bar.
    expect(timelineLane(reported, { ...WINDOW, row: row('conductor', false) }).spans.at(-1)?.late).toBe(false)
  })

  it('never draws a working row red, and leaves earlier stretches as the ledger wrote them', () => {
    const reported = [line('report', 10, 'blocked'), line('decision', 9), line('report', 2, 'done')]
    const running = timelineLane(reported, { ...WINDOW, row: row('working') })
    expect(running.spans.map(s => [s.kind, s.late])).toEqual([['conductor', false], ['conductor', true], ['working', false], ['working', false]])
    // A row on GitHub has nobody on it; a working row the ledger says nobody started
    // (created, never bound) stays idle rather than claiming a worker.
    expect(timelineLane(reported, { ...WINDOW, row: row('github') }).spans[3].kind).toBe('idle')
    expect(timelineLane([line('create', 3)], { ...WINDOW, row: row('working') }).spans[0].kind).toBe('idle')
  })

  it('draws a closed story as its lines say, whatever row it last had', () => {
    const lane = timelineLane([line('bind', 10), line('close', 7)], { ...WINDOW, row: row('stuck') })
    expect(lane.spans.map(s => s.kind)).toEqual(['working'])
  })

  it('lets a plan write pass: it says nothing about who has the work', () => {
    const lane = timelineLane([line('bind', 10), line('plan', 9), line('report', 8, 'progress')], WINDOW)
    expect(lane.spans.map(s => s.kind)).toEqual(['working', 'working'])
    // One stretch from the bind to the report, not two cut at the plan write.
    expect(lane.spans.map(minutes)).toEqual([120, 480])
  })

  it('ignores a line with an unreadable time rather than drawing from NaN', () => {
    const lane = timelineLane([{ ts: 'not a time', kind: 'bind', status: null }, line('bind', 6)], WINDOW)
    expect(lane.spans.map(minutes)).toEqual([360])
  })
})

describe('conductorLane', () => {
  const byItem = {
    a: [line('create', 11), line('bind', 10), line('report', 9, 'progress')],
    b: [line('decision', 2), line('verdict', 1)],
  }

  it('ticks on the conductor\'s own writes, across every story, never on a worker\'s', () => {
    const lane = conductorLane(byItem, WINDOW)
    expect(lane.ticks).toEqual([NOW - 11 * HOUR, NOW - 10 * HOUR, NOW - 2 * HOUR, NOW - HOUR])
    // The person's own steer is not the Conductor's move either.
    expect(conductorLane({ a: [line('steer', 5)] }, WINDOW).ticks).toEqual([])
  })

  it('names the longest stretch it wrote nothing in, both window edges counted', () => {
    const lane = conductorLane(byItem, WINDOW)
    // Between the 10 h bind and the 2 h decision: 8 hours, longer than the hour since.
    expect(lane.gap).toEqual({ from: NOW - 10 * HOUR, to: NOW - 2 * HOUR, minutes: 480, late: false })
    // A conductor that wrote once, early: the gap is from that write to now.
    expect(conductorLane({ a: [line('bind', 11)] }, WINDOW).gap)
      .toEqual({ from: NOW - 11 * HOUR, to: NOW, minutes: 660, late: false })
    // No line at all: no board, so no Conductor to name a gap for.
    expect(conductorLane({}, WINDOW).gap).toBeNull()
  })

  it('names a silence that fills the whole window, with no tick in it', () => {
    // Only a worker wrote in the window: the whole window is the Conductor's gap.
    expect(conductorLane({ a: [line('report', 3, 'progress')] }, WINDOW).gap)
      .toEqual({ from: SINCE, to: NOW, minutes: 720, late: false })
  })

  it('starts a gap at the window edge, whoever wrote an item\'s lead line', () => {
    // The same 6 h of Conductor silence, with a worker's report or the Conductor's
    // own bind as the item's lead line from before the window: one gap, one length.
    const six = { since: NOW - 6 * HOUR, until: NOW }
    const worker = conductorLane({ a: [line('report', 8, 'progress')] }, six).gap
    const own = conductorLane({ a: [line('bind', 8)] }, six).gap
    expect(worker).toEqual({ from: NOW - 6 * HOUR, to: NOW, minutes: 360, late: false })
    expect(own).toEqual(worker)
  })

  it('calls a gap late only when a story waited on the Conductor through the rule in it', () => {
    // A long, healthy run: nothing owed, so a long gap is no alarm.
    expect(conductorLane(byItem, WINDOW).gap?.late).toBe(false)
    // A done report left unjudged for 40 minutes inside the longest gap: late.
    const owed = [{ from: NOW - 5 * HOUR, to: NOW - 5 * HOUR + 40 * 60_000 }]
    expect(conductorLane(byItem, { ...WINDOW, owed }).gap).toMatchObject({ from: NOW - 10 * HOUR, late: true })
    // Owed only 5 minutes inside the longest gap, but an hour inside the next one: that
    // shorter gap is the late one, and the lane names it.
    const later = [{ from: NOW - 2 * HOUR - 5 * 60_000, to: NOW - HOUR }]
    expect(conductorLane(byItem, { ...WINDOW, owed: later }).gap).toEqual({ from: NOW - 2 * HOUR, to: NOW - HOUR, minutes: 60, late: true })
    // Owed under the rule in every gap: nothing late, the longest gap named, quiet.
    const brief = [{ from: NOW - 2 * HOUR - 5 * 60_000, to: NOW - 2 * HOUR + 5 * 60_000 }]
    expect(conductorLane(byItem, { ...WINDOW, owed: brief }).gap).toMatchObject({ minutes: 480, late: false })
  })

  it('names a late gap over a longer quiet one, as a 24 h window shows them', () => {
    // A 24 h window: the longest gap, 5 h 48 m, owes nothing; a shorter one holds a
    // done report its Conductor left 1 h 18 m, so 1 h 8 m past the rule.
    const day = { since: NOW - 24 * HOUR, until: NOW }
    const at = (h: number, m = 0) => new Date(NOW - h * HOUR - m * 60_000).toISOString()
    const events: Record<string, TimelineLine[]> = {
      quiet: [{ ts: at(20), kind: 'bind', status: null }, { ts: at(14, 12), kind: 'decision', status: null }, { ts: at(9), kind: 'decision', status: null }],
      owed: [{ ts: at(4), kind: 'bind', status: null }, { ts: at(3, 18), kind: 'report', status: 'done' }, { ts: at(2), kind: 'verdict', status: null }],
    }
    const owed = timelineLane(events.owed, day).owed
    expect(owed).toEqual([{ from: NOW - 3 * HOUR - 18 * 60_000, to: NOW - 2 * HOUR }])
    const lane = conductorLane(events, { ...day, owed })
    expect(lane.gap).toEqual({ from: NOW - 4 * HOUR, to: NOW - 2 * HOUR, minutes: 120, late: true })
    // With nothing owed the same window names its longest gap, 5 h 48 m, quiet.
    expect(conductorLane(events, day).gap).toEqual({ from: NOW - 20 * HOUR, to: NOW - 14 * HOUR - 12 * 60_000, minutes: 348, late: false })
  })

  it('never ticks on a person\'s rank drag, and still ticks on the Conductor\'s own plan write', () => {
    const lane = conductorLane({ a: [{ ...line('plan', 4), by: 'person' }, line('plan', 3)] }, WINDOW)
    expect(lane.ticks).toEqual([NOW - 3 * HOUR])
  })

  it('counts a write once, however many lines share the moment', () => {
    const same = { a: [line('bind', 5)], b: [line('bind', 5)] }
    expect(conductorLane(same, WINDOW).ticks).toEqual([NOW - 5 * HOUR])
  })
})

describe('place', () => {
  it('measures a stretch as a share of the window', () => {
    expect(place(SINCE, SINCE + 3 * HOUR, SINCE, NOW)).toEqual({ left: 0, width: 25 })
    expect(place(SINCE + 6 * HOUR, NOW, SINCE, NOW)).toEqual({ left: 50, width: 50 })
    expect(mark(SINCE + 9 * HOUR, SINCE, NOW)).toBe(75)
  })

  it('clamps to the window, so a line just outside it still draws at the edge', () => {
    const over = place(SINCE - HOUR, SINCE + HOUR, SINCE, NOW)
    expect(over.left).toBe(0)
    expect(over.width).toBeCloseTo(100 / 12, 10)
    expect(place(NOW + HOUR, NOW + 2 * HOUR, SINCE, NOW)).toEqual({ left: 100, width: 0 })
  })
})

describe('axisHours', () => {
  it('marks every whole hour of a short window', () => {
    const marks = axisHours(NOW - 6 * HOUR, NOW)
    expect(marks).toHaveLength(6)
    expect(marks[0]).toBe(NOW - 6 * HOUR)
  })

  it('thins a long window to at most six marks, evenly spaced', () => {
    const marks = axisHours(NOW - 24 * HOUR, NOW)
    expect(marks).toHaveLength(6)
    expect(marks[1] - marks[0]).toBe(4 * HOUR)
  })

  it('marks the reader\'s own whole hours in a half-hour time zone', () => {
    // vitest pins TZ=UTC; India is UTC+5:30, so its whole hours fall on UTC half-hours.
    const prev = process.env.TZ
    process.env.TZ = 'Asia/Kolkata'
    try {
      const marks = axisHours(NOW - 6 * HOUR, NOW)
      expect(marks.map(ms => new Date(ms).getMinutes())).toEqual(marks.map(() => 0))
      expect(marks[0]).toBe(NOW - 6 * HOUR + 30 * 60_000)
    } finally {
      if (prev === undefined) delete process.env.TZ
      else process.env.TZ = prev
    }
  })
})

describe('rowStretch', () => {
  it('reads each move a row can have, and lateness only on a Conductor\'s move', () => {
    expect(rowStretch({ move: 'needs_you', late: true }, 'working')).toEqual({ kind: 'person', late: false })
    expect(rowStretch({ move: 'stuck', late: false }, 'working')).toEqual({ kind: 'stuck', late: false })
    expect(rowStretch({ move: 'conductor', late: true }, 'working')).toEqual({ kind: 'conductor', late: true })
    expect(rowStretch({ move: 'github', late: false }, 'conductor')).toEqual({ kind: 'idle', late: false })
    expect(rowStretch({ move: 'working', late: false }, 'person')).toEqual({ kind: 'working', late: false })
    // A Working row reads its own worker, not the ledger's last line, when it has one.
    expect(rowStretch({ move: 'working', late: false, hasWorker: true }, 'idle')).toEqual({ kind: 'working', late: false })
    expect(rowStretch({ move: 'working', late: false, hasWorker: false }, 'working')).toEqual({ kind: 'idle', late: false })
    expect(rowStretch({ move: 'working', late: false }, 'idle')).toEqual({ kind: 'idle', late: false })
  })
})
