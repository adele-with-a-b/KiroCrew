/** Timeline, the Board's third view: what actually happened to this project over the
 *  last 6, 12 or 24 hours, one lane per story on one shared time axis, with the
 *  Conductor's own lane above them.
 *
 *  Every bar is a line the work ledger wrote (`lib/boardTimeline.ts` turns the lines
 *  into stretches): a worker had it, its Conductor owed it a move, it waited on the
 *  person, or nobody was on it. Nothing is estimated and nothing is a percentage of
 *  "done" -- Crew cannot know that -- so a lane says WHEN, never how far along. The
 *  Conductor's lane ticks where it wrote on the board and names the longest stretch
 *  of the window it wrote nothing in; the ledger records no READS, so a tick is a
 *  move it made, which is what the lane's own line says.
 *
 *  Each lane's row is the Board's one row shape (`BoardRow`), the same mark, title,
 *  facts and age as Moves and Steps; only the bar under it is new. The bars are
 *  `aria-hidden`: the mark says who must act and the age says how long, both as
 *  text, and the Conductor's longest gap is a sentence, so nothing a bar draws is
 *  only in the bar. The head says the rule a red stretch follows, in words, so that
 *  number is never something a reader has to infer from a colour either.
 *
 *  The window is read on its own (`GET /api/crew-board/timeline`), not from the
 *  board read the panel already holds: that read sends each item's newest 20 events
 *  WITH their text, too few lines for a 24-hour picture and more than a browser
 *  should see. This one asks for times and kinds only, and joins them to the rows
 *  the panel worked out. */
import { useMemo, useState } from 'react'
import { Zap } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../../api/client'
import {
  boardTimelineQueryKey, TIMELINE_DEFAULT_WINDOW, TIMELINE_POLL_MS, TIMELINE_WINDOWS,
  type TimelineLine, type TimelineResponse,
} from '../../../api/boardTimeline'
import {
  axisHours, conductorLane, LATE_MINUTES, mark as markAt, place, timelineLane,
  type SpanKind, type TimelineLane,
} from '../../../lib/boardTimeline'
import { BOARD_MOVES, longestWaitingFirst } from '../../../lib/boardMoves'
import { timedPoll, usePollRule } from '../../../lib/pollRule'
import { fmtMinutes } from '../../../lib/crewWork'
import { i18nT } from '../../../i18n/t'
import { fmtDateFields, fmtUnit } from '../../../i18n/format'
import { isNotFoundError } from '../../../api/apiError'
import { errMessage } from '../../../utils/thunkError'
import ErrorNotice from '../../../components/ErrorNotice'
import { BoardRow, MoveMark, moveTone, type BoardStory } from '../../../components/board/BoardRow'
import { BoardSections } from '../../../components/board/BoardSection'
import { RadioChipGroup, radioChip } from '../../../components/RadioChipGroup'

/** The tone each stretch is drawn in, as the Board's tones: the project's own colour
 *  while a worker has it, the person's yellow where it waits on them, red once a
 *  Conductor's move is late, quiet grey otherwise. `bg-current` fills from these. */
const SPAN_TONE: Record<SpanKind, string> = {
  working: 'crew-tone-aim',
  conductor: 'text-muted',
  person: 'crew-tone-warn',
  idle: 'text-muted opacity-50',
}

/** The legend, in the order the lanes are most likely to show them. */
const LEGEND: ReadonlyArray<{ kind: SpanKind; late?: boolean; key: string }> = [
  { kind: 'working', key: 'components.crewMode.board_timeline_legend_working' },
  { kind: 'conductor', key: 'components.crewMode.board_timeline_legend_conductor' },
  { kind: 'person', key: 'components.crewMode.board_timeline_legend_person' },
  { kind: 'conductor', late: true, key: 'components.crewMode.board_timeline_late_rule' },
  { kind: 'idle', key: 'components.crewMode.board_timeline_legend_idle' },
]

/** Who must act first, then the one waiting longest: the order Steps uses, so the
 *  same stories read down the page in the same order in both views. */
function byMoveThenWait(a: BoardStory, b: BoardStory): number {
  return BOARD_MOVES.indexOf(a.move) - BOARD_MOVES.indexOf(b.move) || longestWaitingFirst(a.item, b.item)
}

/** A percentage for a CSS length: plain digits, never locale-formatted ones. */
function pct(value: number): string {
  return `${value}%`
}

function spanClass(kind: SpanKind, late: boolean): string {
  return late ? 'crew-tone-danger' : SPAN_TONE[kind]
}

/** One lane: its stretches on the window, and a diamond where its worker reported.
 *  The track is the groove the bars sit in, so a story idle for most of the window
 *  still draws a full-width line rather than nothing. */
function Lane({ lane, since, until, testId = 'board-timeline-lane' }: {
  lane: TimelineLane
  since: number
  until: number
  testId?: string
}) {
  return (
    <div className="relative h-3" aria-hidden="true" data-testid={testId}>
      <div className="absolute inset-x-0 top-1/2 -translate-y-1/2 h-1.5 rounded-full bg-bg-hover" />
      {lane.spans.map(span => {
        const { left, width } = place(span.from, span.to, since, until)
        return (
          <span key={`${span.from}-${span.kind}`} data-span={span.kind} data-late={span.late || undefined}
            className={`absolute top-1/2 -translate-y-1/2 h-1.5 rounded-full bg-current ${spanClass(span.kind, span.late)}`}
            style={{ left: pct(left), width: pct(width), minWidth: 2 }} />
        )
      })}
      {lane.reports.map(ms => (
        <span key={ms} data-testid="board-timeline-report"
          className="absolute top-1/2 w-2 h-2 -ml-1 -translate-y-1/2 rotate-45 border border-text bg-bg-elevated"
          style={{ left: pct(markAt(ms, since, until)) }} />
      ))}
    </div>
  )
}

/** The hours under the axis and, at its right edge, now. */
function Axis({ since, until }: { since: number; until: number }) {
  return (
    <div className="relative h-4 text-[11px] tabular-nums text-muted" data-testid="board-timeline-axis">
      {axisHours(since, until).map(ms => {
        const left = markAt(ms, since, until)
        // Nothing within a hand's width of the right edge: "now" sits there.
        return left > 88 ? null : (
          <span key={ms} className="absolute top-0" style={{ left: pct(left) }} data-testid="board-timeline-hour">
            {/* An axis needs even-width marks and no AM/PM, so the hour is asked for
                flat; the digits still follow the app's language. */}
            {fmtDateFields(ms, { hour: '2-digit', hour12: false })}
          </span>
        )
      })}
      <span className="absolute top-0 right-0 crew-tone-aim" data-testid="board-timeline-now">{i18nT('components.crewMode.board_timeline_now')}</span>
    </div>
  )
}

/** The Conductor's lane: a tick per move it made, and the longest stretch of the
 *  window with no move in it, red once it passes the Board's own rule. */
function ConductorLane({ events, since, until }: {
  events: Readonly<Record<string, readonly TimelineLine[]>>
  since: number
  until: number
}) {
  const { ticks, longestGap } = useMemo(() => conductorLane(events, { since, until }), [events, since, until])
  const gap = longestGap && longestGap.minutes >= 1 ? longestGap : null
  return (
    <div className="px-3 py-2 space-y-1" data-testid="board-timeline-conductor">
      <div className="flex items-baseline gap-2">
        <span className="shrink-0 text-muted inline-flex mt-[3px]"><Zap size={14} aria-hidden="true" /></span>
        <span className="text-[13px] font-medium text-text">{i18nT('components.crewMode.board_timeline_conductor')}</span>
        <span className="min-w-0 text-[11px] text-muted [overflow-wrap:anywhere]">{i18nT('components.crewMode.board_timeline_conductor_hint')}</span>
        {gap && (
          <span className={`ml-auto shrink-0 text-[12px] tabular-nums ${gap.minutes >= LATE_MINUTES ? 'crew-tone-danger' : 'text-muted'}`}
            data-testid="board-timeline-gap">
            {i18nT('components.crewMode.board_timeline_gap', { age: fmtMinutes(gap.minutes) })}
          </span>
        )}
      </div>
      <div className="relative h-3" aria-hidden="true">
        {ticks.map(ms => (
          <span key={ms} data-testid="board-timeline-tick" className="absolute inset-y-0 w-px bg-current text-muted"
            style={{ left: pct(markAt(ms, since, until)) }} />
        ))}
        {gap && (
          <span className="absolute top-1/2 h-px -translate-y-1/2 bg-current crew-tone-danger" data-testid="board-timeline-gap-line"
            style={{ left: pct(place(gap.from, gap.to, since, until).left), width: pct(place(gap.from, gap.to, since, until).width) }} />
        )}
      </div>
    </div>
  )
}

function Legend() {
  return (
    <ul className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted" data-testid="board-timeline-legend">
      {LEGEND.map(({ kind, late, key }) => (
        <li key={key} className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" data-span={kind} data-late={late || undefined}
            className={`w-5 h-1.5 rounded-full bg-current ${spanClass(kind, !!late)}`} />
          {i18nT(key, { age: fmtMinutes(LATE_MINUTES) })}
        </li>
      ))}
      <li className="inline-flex items-center gap-1.5">
        <span aria-hidden="true" className="w-2 h-2 rotate-45 border border-text bg-bg-elevated" />
        {i18nT('components.crewMode.board_timeline_legend_report')}
      </li>
    </ul>
  )
}

export default function BoardTimelineView({ root, stories, active = true }: {
  root: string
  /** The same rows Moves and Steps draw, from the panel's own board read. */
  stories: readonly BoardStory[]
  /** Polled only while the Board tab is the one on screen. */
  active?: boolean
}) {
  const [hours, setHours] = useState<number>(TIMELINE_DEFAULT_WINDOW)
  const id = `crew-board-timeline:${root}:${hours}`
  const key = boardTimelineQueryKey(root, hours)
  const interval = usePollRule(id, TIMELINE_POLL_MS, active, key)
  const read = useQuery({
    queryKey: key,
    queryFn: () => timedPoll(id, () => api.crewBoardTimeline(root, hours)) as Promise<TimelineResponse>,
    enabled: !!root && active,
    refetchInterval: interval,
    retry: false,
  })
  // The server's own read time and window edge, so the bars and the event stamps
  // are measured against the same clock. Before the first answer the browser's
  // clock stands in, and the lanes are empty anyway.
  const until = read.data ? Date.parse(read.data.read_at) : Date.now()
  const since = read.data ? Date.parse(read.data.since) : until - hours * 3_600_000
  const events = read.data?.events ?? {}
  const lanes = useMemo(() => [...stories].sort(byMoveThenWait), [stories])
  return (
    <div className="space-y-2" data-testid="board-timeline" data-hours={hours} data-loading={read.isPending || undefined}>
      {/* A ledger the conductor never opened is the empty board, which the panel
          already says; any other failure says so here and offers the hand-off. */}
      {read.isError && !isNotFoundError(read.error) && (
        <ErrorNotice variant="inline" message={errMessage(read.error) || i18nT('pages.crewBoard.error_title')} askAgent />
      )}
      <div className="flex items-baseline gap-2">
        <h3 className="text-[11px] font-semibold uppercase tracking-[1px] text-muted" data-testid="board-timeline-head">
          {i18nT('components.crewMode.board_timeline_window', { age: fmtUnit(hours, 'hour', { unitDisplay: 'long' }) })}
        </h3>
        <RadioChipGroup ariaLabel={i18nT('components.crewMode.board_timeline_window_pick')}
          className="ml-auto shrink-0 flex gap-0.5 rounded-lg border border-border bg-bg-hover/40 p-0.5" data-testid="board-timeline-windows">
          {TIMELINE_WINDOWS.map(h => (
            <button key={h} type="button" {...radioChip(h === hours)} data-testid={`board-timeline-window-${h}`} onClick={() => setHours(h)}
              className={`rounded-md px-1.5 py-0.5 text-[11px] font-medium tabular-nums transition-colors ${h === hours ? 'bg-bg-elevated text-text shadow-sm' : 'text-muted hover:text-text'}`}>
              {fmtUnit(h, 'hour', { unitDisplay: 'narrow' })}
            </button>
          ))}
        </RadioChipGroup>
      </div>
      {/* The rule the red stretches follow, in the head: a reader learns it here
          rather than inferring it from a colour. */}
      <p className="flex items-center gap-1.5 text-[11px] text-muted" data-testid="board-timeline-rule">
        <span aria-hidden="true" className="w-5 h-1.5 shrink-0 rounded-full bg-current crew-tone-danger" />
        {i18nT('components.crewMode.board_timeline_late_rule', { age: fmtMinutes(LATE_MINUTES) })}
      </p>
      <BoardSections testId="board-timeline-lanes">
        <div className="px-3 pt-2"><Axis since={since} until={until} /></div>
        <ConductorLane events={events} since={since} until={until} />
        {lanes.length === 0
          ? <p className="px-3 py-2 text-[12px] text-muted" data-testid="board-timeline-empty">{i18nT('components.crewMode.board_timeline_empty')}</p>
          : (
            <ul className="divide-y divide-border">
              {lanes.map(story => (
                <li key={story.item.item_id}>
                  <BoardRow mark={<MoveMark move={story.move} late={story.late} />} title={story.item.title} onOpen={story.onOpen}
                    age={story.age} tone={moveTone(story.move, story.late)} facts={story.facts}
                    testId="board-timeline-story" item={story.item.item_id} move={story.move} />
                  <div className="px-3 pb-2">
                    <Lane since={since} until={until}
                      lane={timelineLane(events[story.item.item_id] ?? [], {
                        since, until,
                        awaitsPerson: story.item.awaits_person === true,
                        closedAt: story.item.closed_at ? Date.parse(story.item.closed_at) : null,
                      })} />
                  </div>
                </li>
              ))}
            </ul>
          )}
      </BoardSections>
      <Legend />
    </div>
  )
}
