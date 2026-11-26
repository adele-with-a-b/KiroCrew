/**
 * Types for the Board Timeline's read (`GET /api/crew-board/timeline`).
 *
 * These mirror the server's projection in
 * `kiro_crew/dashboard/handlers/crew_board_timeline.py`. A line carries WHEN
 * something happened and WHAT kind it was, never its text: a `bind` line's text is
 * a worker session key, and a report's is the summary the board read already sends
 * (redacted) on the row. So the Timeline joins these lines to the board read it
 * already holds, and asks for nothing the row does not carry.
 */

import type { WorkBoardEvent, WorkReportStatus, WorkVerdict } from './crewBoard'

/** The windows the route serves, in hours. Anything else answers `invalid_hours`. */
export const TIMELINE_WINDOWS: readonly number[] = [6, 12, 24]
export const TIMELINE_DEFAULT_WINDOW = 12

/**
 * Every kind the ledger writes (`work_ledger_records.EVENT_KINDS`).
 *
 * Wider than the board read's `WorkBoardEvent['kind']`, which predates `ask`,
 * `retire` and `destroy`: the Timeline dispatches on the kind to decide what a
 * stretch is, so a kind it has no case for would be drawn as the wrong thing. Named
 * in full here rather than widened on `WorkBoardEvent`, which the board read owns.
 */
export type TimelineKind = WorkBoardEvent['kind'] | 'ask' | 'retire' | 'destroy'

/** One line of an item's log, placed on the time axis. */
export interface TimelineLine {
  ts: string
  kind: TimelineKind
  status: WorkReportStatus
  /** On a `verdict` line only: the verdict the conductor wrote. */
  verdict?: WorkVerdict
}

export interface TimelineResponse {
  /** When the server read it: the axis's right edge ("now"). */
  read_at: string
  /** The axis's left edge. */
  since: string
  hours: number
  /** Lines per item id, oldest first. An item with nothing in the window is absent;
   *  one that was already running carries the newest line from before `since`. */
  events: Record<string, TimelineLine[]>
}

/** How often the browser re-reads it. Slower than the board's own poll: a bar a
 *  minute wide does not move between reads, and the row's age comes from the board. */
export const TIMELINE_POLL_MS = 60_000

/** React Query key. One timeline per conductor AND window: a switch to 24 h is a
 *  different read, not a refetch of the same one. */
export function boardTimelineQueryKey(conductor: string, hours: number): readonly unknown[] {
  return ['crew-board-timeline', conductor, hours]
}
