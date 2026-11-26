/**
 * What the Timeline draws from the ledger's lines: which stretch each kind of line
 * opens, how long each ran, where it sits on the axis, and the Conductor's own ticks
 * and longest quiet stretch. Every time here is explicit -- no clock is read.
 */
import { describe, expect, it } from 'vitest'
import type { TimelineKind, TimelineLine } from '../api/boardTimeline'
import type { WorkReportStatus } from '../api/crewBoard'
import {
  axisHours, conductorLane, LATE_MINUTES, mark, place, timelineLane,
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
  it('opens a stretch per line and runs it to the next one, named by who then had it', () => {
    const lane = timelineLane([
      line('create', 11),
      line('bind', 10),
      line('report', 9, 'progress'),
      line('report', 8, 'done'),
      line('decision', 7),
      line('verdict', 6),
    ], WINDOW)
    expect(lane.spans.map(s => s.kind)).toEqual(['idle', 'working', 'working', 'conductor', 'working', 'idle'])
    // Each stretch is exactly the gap to the next line; the last runs to the read time.
    expect(lane.spans.map(minutes)).toEqual([60, 60, 60, 60, 60, 360])
    // A diamond per worker report, and only those.
    expect(lane.reports).toEqual([NOW - 9 * HOUR, NOW - 8 * HOUR])
  })

  it('marks a wait on its Conductor late only once it reaches the rule', () => {
    const late = timelineLane([line('report', 9, 'blocked'), line('decision', 8)], WINDOW)
    expect(late.spans[0]).toMatchObject({ kind: 'conductor', late: true })
    // Answered inside the rule: the same stretch, not drawn late.
    const quick = timelineLane([
      line('report', 9, 'question'),
      { ...line('decision', 9), ts: new Date(NOW - 9 * HOUR + (LATE_MINUTES - 1) * 60_000).toISOString() },
    ], WINDOW)
    expect(quick.spans[0]).toMatchObject({ kind: 'conductor', late: false })
    expect(minutes(quick.spans[0])).toBe(LATE_MINUTES - 1)
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
    ], { ...WINDOW, awaitsPerson: true })
    expect(waiting.spans.map(s => s.kind)).toEqual(['conductor', 'working', 'person'])
    expect(waiting.spans.map(s => s.late)).toEqual([true, false, false])
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
    expect(lane.longestGap).toEqual({ from: NOW - 10 * HOUR, to: NOW - 2 * HOUR, minutes: 480 })
    // A conductor that wrote once, early: the gap is from that write to now.
    expect(conductorLane({ a: [line('bind', 11)] }, WINDOW).longestGap)
      .toEqual({ from: NOW - 11 * HOUR, to: NOW, minutes: 660 })
    // Nothing in the window at all: no gap to name, so the line is not drawn.
    expect(conductorLane({}, WINDOW).longestGap).toBeNull()
    expect(conductorLane({ a: [line('report', 3, 'progress')] }, WINDOW).longestGap).toBeNull()
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
})
