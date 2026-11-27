/** One story open in the right bar's Board: the steps left before it lands, who held
 *  it, its own lane over the last 12 hours, then its time, sub-tasks, epic and last
 *  report. A click on a story's title opens it; the crumb or Escape goes back to the Board, and
 *  Open the chat opens the story's worker the way the click did before, a closed chat
 *  reopened from its saved transcript first (`useOpenCrewWorker`).
 *
 *  Every value is a field or an event the work ledger wrote: the step is the Board
 *  read's (`storyStep`), the lane and who held it come from the Timeline read's lines
 *  (`timelineLane`, under the Timeline's own key for its 12-hour window, so the two
 *  share one cache entry), and the rest from the board read the panel holds. Where
 *  the ledger keeps nothing for a part, the part says "no record", never a number.
 *  The lane and the held bar are `aria-hidden`: the words beside each say what it
 *  draws. */
import { useEffect, useId, useMemo, useRef, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Bot, ChevronLeft, Circle, CircleCheck, CircleX } from 'lucide-react'
import { api } from '../../api/client'
import type { CrewPrFacts, WorkBoardItem } from '../../api/crewBoard'
import { boardTimelineQueryKey, TIMELINE_DEFAULT_WINDOW, TIMELINE_POLL_MS, type TimelineResponse } from '../../api/boardTimeline'
import { isNotFoundError } from '../../api/apiError'
import { axisHours, mark as markAt, place, timelineLane, type SpanKind, type TimelineLane, type TimelineSpan } from '../../lib/boardTimeline'
import { storyStep } from '../../lib/boardMoves'
import { childProgress, estimateMinutes, levelOf, planOf, type BoardPlan } from '../../lib/crewPlan'
import { fmtMinutes } from '../../lib/crewWork'
import { timedPoll, usePollRule } from '../../lib/pollRule'
import { i18nT } from '../../i18n/t'
import { fmtDateFields, fmtNumber, fmtRelative, fmtUnit } from '../../i18n/format'
import { errMessage } from '../../utils/thunkError'
import { STEP_KEY } from '../../pages/chat/command-center/BoardStepsView'
import ErrorNotice from '../ErrorNotice'
import { Btn } from '../ui'
import { prFactTags } from '../crew/prFactTags'
import { MOVE_LABEL, MoveMark, joinParts, moveTone, type BoardStory } from './BoardRow'
import WorkerDoorAction from './WorkerDoor'
import type { WorkerDoor } from '../../pages/chat/command-center/openCrewWorker'
import { landSteps, storyHeld, storyMinutes, type LandStepState, type StoryHeld } from './storyView'

/** A share of the lane as a CSS length, never through the locale's percent format
 *  (a French "25 %" is no CSS length). */
const pct = (value: number) => `calc(${value} * 1%)`

/** Each stretch in the Timeline's own tones: the project's colour while a worker has
 *  it, the person's yellow, quiet grey for the rest, and red only for late. */
const SPAN_TONE: Record<SpanKind, string> = {
  working: 'bg-current crew-tone-aim',
  conductor: 'bg-current text-muted',
  person: 'bg-current crew-tone-warn',
  stuck: 'border border-current crew-tone-muted',
  idle: 'bg-current text-muted opacity-50',
}

const spanTone = (span: Pick<TimelineSpan, 'kind' | 'late'>) => (span.late ? 'bg-current crew-tone-danger' : SPAN_TONE[span.kind])

const HEAD = 'text-[11px] font-semibold uppercase tracking-[1px] text-muted'

/** The age in its row's tone: yellow when it waits on the person, red when stuck or late. */
const AGE_TONE = { warn: 'crew-tone-warn', danger: 'crew-tone-danger', none: 'text-text' } as const

/** A held or used time as the facts say it: none is a plain 0, never "1m". */
function heldTime(minutes: number | null): string {
  if (minutes == null) return i18nT('components.crewMode.board_story_no_record')
  return minutes < 1 ? fmtNumber(0) : fmtMinutes(minutes)
}

function StepMark({ state, story, item }: { state: LandStepState; story?: BoardStory; item: WorkBoardItem }) {
  // The step it is at says who must act there, as its row's mark does.
  if (state === 'current' && story) return <MoveMark move={story.move} late={story.late} />
  const done = state === 'passed' || state === 'current'
  const Icon = done ? CircleCheck : Circle
  const label = i18nT(done ? 'components.crewMode.board_story_step_passed' : 'components.crewMode.board_story_step_ahead')
  return (
    <span role="img" aria-label={label} title={label} className={`inline-flex ${done && item.state === 'accepted' ? 'crew-tone-ok' : 'text-muted'}`}>
      <Icon size={14} aria-hidden="true" />
    </span>
  )
}

function Lane({ lane, since, until }: { lane: TimelineLane; since: number; until: number }) {
  return (
    <div className="relative h-2 rounded-full bg-bg-hover" aria-hidden="true" data-testid="board-story-lane">
      {lane.spans.map(span => {
        const at = place(span.from, span.to, since, until)
        return <span key={`${span.opened}-${span.kind}-${span.late ? 'late' : 'on'}`} data-span={span.kind} data-late={span.late || undefined}
          className={`absolute inset-y-0 rounded-full ${spanTone(span)}`} style={{ left: pct(at.left), width: pct(at.width), minWidth: 2 }} />
      })}
      {lane.reports.map((ms, n) => (
        <span key={`${ms}-${n}`} className="absolute top-1/2 w-2 h-2 -ml-1 -translate-y-1/2 rotate-45 border border-text bg-bg-elevated" style={{ left: pct(markAt(ms, since, until)) }} />
      ))}
    </div>
  )
}

/** Who held it, in words, and the bar that draws the same minutes. */
function Held({ held, late }: { held: StoryHeld; late: boolean }) {
  const parts: Array<{ who: string; minutes: number | null; key: string; tone: string }> = [
    { who: 'worker', minutes: held.worker, key: 'components.crewMode.board_story_held_worker', tone: SPAN_TONE.working },
    { who: 'github', minutes: held.github, key: 'components.crewMode.board_story_held_github', tone: SPAN_TONE.idle },
    { who: 'conductor', minutes: held.conductor, key: 'components.crewMode.board_story_held_conductor', tone: late ? 'bg-current crew-tone-danger' : SPAN_TONE.conductor },
    { who: 'you', minutes: held.you, key: 'components.crewMode.board_story_held_you', tone: SPAN_TONE.person },
  ]
  // Nobody on it is said only when it happened: the mock's four are always said.
  if (held.nobody >= 1) parts.push({ who: 'nobody', minutes: held.nobody, key: 'components.crewMode.board_story_held_nobody', tone: SPAN_TONE.stuck })
  return (
    <>
      <div className="flex h-1.5 gap-0.5" aria-hidden="true" data-testid="board-story-held-bar">
        {parts.filter(p => (p.minutes ?? 0) > 0).map(p => (
          <span key={p.who} data-held={p.who} className={`h-full rounded-sm ${p.tone}`} style={{ flexGrow: p.minutes ?? 0, flexBasis: 0, minWidth: 2 }} />
        ))}
      </div>
      <ul className="flex flex-wrap gap-x-3 gap-y-0.5 text-[12px] text-muted tabular-nums" data-testid="board-story-held">
        {parts.map(p => <li key={p.who} data-held={p.who} data-none={p.minutes == null || undefined}>{i18nT(p.key, { time: heldTime(p.minutes) })}</li>)}
      </ul>
    </>
  )
}

function Fact({ label, children, testId }: { label: string; children: ReactNode; testId: string }) {
  return (
    <>
      <dt className="text-muted">{label}</dt>
      <dd className="m-0 min-w-0 text-text [overflow-wrap:anywhere]" data-testid={testId}>{children}</dd>
    </>
  )
}

export default function BoardStoryView({ root, item, story, project, door, facts, plan, onBack, reopenFailed, active = true, now }: {
  root: string
  item: WorkBoardItem
  /** Its Board row while it is open; absent once it closed, when it has no move left. */
  story?: BoardStory
  /** The project's name: its Conductor chat's title. */
  project?: string
  /** Who its worker is, where it ran and the way to its chat (`workerDoor`); null
   *  when no worker was ever bound to it. */
  door?: WorkerDoor | null
  facts?: CrewPrFacts
  plan: BoardPlan
  onBack: () => void
  /** The last reopen of its closed chat failed. */
  reopenFailed?: boolean
  /** Polled only while the Board tab is the one on screen. */
  active?: boolean
  now: number
}) {
  const heading = useRef<HTMLHeadingElement>(null)
  const stepsHead = useId()
  // Opened from a row: the reader lands on the story, not back at the panel's top.
  useEffect(() => { heading.current?.focus() }, [item.item_id])
  const hours = TIMELINE_DEFAULT_WINDOW
  const pollId = `crew-board-timeline:${root}:${hours}`
  const key = boardTimelineQueryKey(root, hours)
  const interval = usePollRule(pollId, TIMELINE_POLL_MS, active, key)
  const read = useQuery({
    queryKey: key,
    queryFn: () => timedPoll(pollId, () => api.crewBoardTimeline(root, hours)) as Promise<TimelineResponse>,
    enabled: !!root && active,
    refetchInterval: interval,
    retry: false,
  })
  const data = read.data
  const until = data ? Date.parse(data.read_at) : 0
  const since = data ? Date.parse(data.since) : 0
  const lane = useMemo(() => data ? timelineLane(data.events[item.item_id] ?? [], story
    ? {
        since, until, reason: item.reason,
        row: { move: story.move, late: story.late, hasWorker: item.has_worker, waitSince: story.move === 'conductor' ? Date.parse(item.last_report_at ?? '') : undefined },
      }
    : { since, until, reason: item.reason, closedAt: Date.parse(item.closed_at ?? '') }) : null, [data, item, story, since, until])
  const held = lane ? storyHeld(lane, until, story?.move) : null
  // The window holds the story's whole life only when it was made inside it.
  const whole = data != null && Date.parse(item.created_at) >= since
  const step = storyStep(item)
  const who = door ? joinParts([door.who, door.where]) : ''
  // An open story nobody was ever bound to says so; a closed one says nothing.
  const whoLine = who || (item.state === 'open' ? i18nT('components.crewMode.board_no_worker_yet') : '')
  const said = prFactTags(facts).filter(t => !t.read).map(t => t.text)
  const crumb = joinParts([project ?? '', i18nT(STEP_KEY[step])])
  const subtasks = childProgress(item, plan.children)
  const parent = plan.byId.get(planOf(item).parent ?? '')
  const epic = parent && levelOf(parent) === 'epic' ? parent.title : ''
  const used = storyMinutes(item, now)
  const estimate = estimateMinutes(item)
  const release = planOf(item).release
  const accepted = item.state === 'accepted'
  const windowLabel = i18nT('components.crewMode.board_timeline_window', { age: fmtUnit(hours, 'hour', { unitDisplay: 'long' }) })

  const stepLines = (s: typeof step, state: LandStepState): string[] => {
    // The release it ships in is the plan's, said before it is reached too.
    if (s === 'released') return release ? [release] : []
    if (state === 'ahead') return []
    if (s === 'build') return [joinParts([who, whole && held && held.worker >= 1 ? i18nT('components.crewMode.board_story_work', { time: fmtMinutes(held.worker) }) : ''])]
    if (s === 'review' && item.pr != null) return [joinParts([i18nT('components.crewMode.plan_pr', { n: item.pr }), ...said])]
    return []
  }

  return (
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions -- Escape here is a shortcut for the crumb, a real button inside; the listener catches it from whatever inside has the focus and adds no control a role could announce
    <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-3.5" data-testid="board-story-view" data-item={item.item_id} data-step={step}
      onKeyDown={e => {
        // Escape goes back as the crumb does; the panel behind it never sees this one.
        if (e.key !== 'Escape' || e.defaultPrevented) return
        e.preventDefault()
        e.stopPropagation()
        onBack()
      }}>
      <Btn type="button" onClick={onBack} aria-label={i18nT('components.crewMode.board_story_back', { where: crumb })} data-testid="board-story-back"
        className="p-0 border-0 bg-transparent hover:bg-transparent active:scale-100 text-[12px] text-muted hover:text-text hover:underline">
        <ChevronLeft size={12} aria-hidden="true" />{crumb}
      </Btn>
      <div className="space-y-1">
        <h2 ref={heading} tabIndex={-1} className="text-[15px] font-semibold text-text [overflow-wrap:anywhere]" data-testid="board-story-title">{item.title}</h2>
        <p className="flex flex-wrap items-center gap-x-3.5 gap-y-1 text-[12px] text-muted" data-testid="board-story-state">
          {story ? (
            <span className="inline-flex items-center gap-1" data-move={story.move}>
              <MoveMark move={story.move} late={story.late} />
              {i18nT(MOVE_LABEL[story.move])}
              <b className={`font-medium tabular-nums ${AGE_TONE[moveTone(story.move, story.late) ?? 'none']}`} data-testid="board-story-age">{story.age}</b>
            </span>
          ) : (
            <span className="inline-flex items-center gap-1" data-ended={accepted ? 'accepted' : 'not_done'}>
              {accepted ? <CircleCheck size={14} aria-hidden="true" /> : <CircleX size={14} aria-hidden="true" />}
              {i18nT(accepted ? 'components.crewMode.board_landed' : 'components.crewMode.board_landed_not_done')}
            </span>
          )}
          {whoLine && (
            <span className="inline-flex items-center gap-1 min-w-0" data-testid="board-story-worker" data-bound={door ? 'true' : 'false'}>
              <Bot size={12} aria-hidden="true" className="shrink-0" /><span className="[overflow-wrap:anywhere]">{whoLine}</span>
            </span>
          )}
        </p>
        {door && <WorkerDoorAction door={door} />}
        {reopenFailed && <ErrorNotice variant="inline" message={i18nT('commandCenter.worker_reopen_failed')} askAgent />}
      </div>

      <section className="space-y-1.5">
        <h3 id={stepsHead} className={HEAD}>{i18nT('components.crewMode.board_story_left_to_land')}</h3>
        <ol aria-labelledby={stepsHead} className="rounded-lg border border-border bg-card text-card-fg overflow-hidden divide-y divide-border" data-testid="board-story-steps">
          {landSteps(item).map(({ step: s, state }) => {
            const lines = stepLines(s, state).filter(Boolean)
            const current = state === 'current'
            return (
              <li key={s} data-step={s} data-state={state} aria-current={current ? 'step' : undefined}
                className={`flex items-baseline gap-2 px-3 py-2 ${current ? 'bg-bg-hover/50 text-text' : ''}`} data-testid="board-story-step">
                <span className="shrink-0 relative top-[2px]"><StepMark state={state} story={story} item={item} /></span>
                <div className="flex-1 min-w-0 space-y-0.5">
                  <div className={`text-[13px] ${current ? 'font-medium text-text' : state === 'ahead' ? 'text-muted' : 'text-text'}`}>{i18nT(STEP_KEY[s])}</div>
                  {lines.map(line => <div key={line} className="text-[12px] text-muted [overflow-wrap:anywhere]">{line}</div>)}
                  {current && story?.why && <div className="text-[12px] text-muted [overflow-wrap:anywhere]" data-testid="board-story-step-why">{story.why}</div>}
                </div>
              </li>
            )
          })}
        </ol>
      </section>

      {read.isError && !isNotFoundError(read.error) && <ErrorNotice variant="inline" message={errMessage(read.error) || i18nT('pages.crewBoard.error_title')} askAgent />}
      <section className="space-y-1.5" data-testid="board-story-who-held">
        <h3 className={`flex items-baseline gap-2 ${HEAD}`}>
          {i18nT('components.crewMode.board_story_who_held')}
          {data && !whole && <span className="normal-case tracking-normal font-normal text-muted" data-testid="board-story-held-window">{windowLabel}</span>}
        </h3>
        {held
          ? <Held held={held} late={!!lane?.spans.some(s => s.late)} />
          : !read.isError && <p className="text-[12px] text-muted">{i18nT('components.crewMode.board_timeline_loading')}</p>}
      </section>

      <section className="space-y-1.5" data-testid="board-story-lane-section">
        <h3 className={HEAD}>{windowLabel}</h3>
        {lane && (
          <div className="rounded-lg border border-border bg-card text-card-fg px-3 py-2.5 space-y-1.5">
            <div className="relative h-4 text-[11px] tabular-nums text-muted" aria-hidden="true">
              {axisHours(since, until).map(ms => markAt(ms, since, until) > 88 ? null : (
                <span key={ms} className="absolute top-0 -translate-x-1/2" style={{ left: pct(markAt(ms, since, until)) }}>{fmtDateFields(ms, { hour: '2-digit', hour12: false })}</span>
              ))}
              <span className="absolute top-0 right-0 font-semibold crew-tone-aim">{i18nT('components.crewMode.board_timeline_now')}</span>
            </div>
            <Lane lane={lane} since={since} until={until} />
          </div>
        )}
      </section>

      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1.5 text-[12.5px]" data-testid="board-story-facts">
        <Fact label={i18nT('components.crewMode.board_story_time')} testId="board-story-time">
          {estimate !== undefined
            ? i18nT('components.crewMode.board_story_time_of', { used: heldTime(used), estimate: fmtMinutes(estimate) })
            : i18nT('components.crewMode.board_story_time_no_estimate', { used: heldTime(used) })}
        </Fact>
        <Fact label={i18nT('components.crewMode.board_story_subtasks')} testId="board-story-subtasks">
          {subtasks.total > 0
            ? i18nT('components.crewMode.board_story_subtasks_of', { done: subtasks.done, total: subtasks.total })
            : i18nT('components.crewMode.board_story_none')}
        </Fact>
        <Fact label={i18nT('components.crewMode.board_story_epic')} testId="board-story-epic">{epic || i18nT('components.crewMode.board_story_none')}</Fact>
        <Fact label={i18nT('components.crewMode.board_story_last_report')} testId="board-story-last-report">
          {item.last_report_at
            ? <>{item.summary && <q>{item.summary}</q>} <span className="text-muted">{fmtRelative(Math.min(Date.parse(item.last_report_at), now), { now })}</span></>
            : i18nT('components.crewMode.board_story_no_record')}
        </Fact>
      </dl>
    </div>
  )
}
