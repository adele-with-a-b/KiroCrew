/** The conductor mode chat's Board tab: one switch (Moves, Steps, Timeline,
 *  Workers, `BoardViewSwitch`), opening on Moves (`BoardMovesView`). This file works
 *  out what the Board draws from the ledger: the goal as one line that opens onto
 *  its "Done when", scope and latest change; the run's state word; and for each open
 *  story who must act (`boardMove`), how long it has waited, and its line, what its
 *  worker last said or what it waits on. Needs you also holds the main chat's
 *  question as one row pointing at its card and the approval cards compact. Steps
 *  (`BoardStepsView`) regroups the same rows by step; Timeline
 *  (`BoardTimelineView`) draws them as lanes over the last hours, on its own
 *  read. Workers = the crew as one
 *  indented tree, each lead foldable, trees finished all through folded away; a
 *  click opens the member in the main pane. Every number comes from reads the
 *  dashboard already makes: the crew list route, the work-ledger board, the live
 *  slot rows and the pending question cards. No model call. */
import { cloneElement, useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ArrowDown, Check, ChevronDown, ChevronLeft, ChevronRight, Loader, Loader2, MessageCircleQuestionMark, Pause, CircleStop, X } from 'lucide-react'
import { safeGetItem, safeSetItem } from '../../../utils/safeStorage'
import { useCrewBack, type CrewBack } from '../../../components/crew/useCrewBack'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../../components/ui/dropdown-menu'
import { api } from '../../../api/client'
import type { CrewWorker, ConductorModeRun } from '../../../types'
import { timedPoll, usePollRule } from '../../../lib/pollRule'
import { crewBoardQueryKey, CREW_BOARD_POLL_MS, type WorkBoardItem, type WorkBoardResponse } from '../../../api/crewBoard'
import { artifactEntries } from '../../crewBoardRows'
import { fmtRelative } from '../../../i18n/format'
import { fmtMinutes } from '../../../lib/crewWork'
import ErrorNotice from '../../../components/ErrorNotice'
import { isNotFoundError } from '../../../api/apiError'
import { errMessage } from '../../../utils/thunkError'
import { closeActivityPanel, pendingQuestionFor } from '../../../store/chatSlice'
import { useAppDispatch, useAppSelector, useAppStore } from '../../../store'
import { useIsMobile } from '../../../hooks/useIsMobile'
import { i18nT } from '../../../i18n/t'
import { CrewStatusDot } from '../../../components/crew/CrewModeMarks'
import CrewApprovalCards, { crewApprovalRequests } from '../../../components/crew/CrewApprovalCards'
import { crewRouteNotServed, crewSubDispatcher, crewWorkerGroup, mergeCrewWorkers, searchCrewWorkers } from '../../../lib/crewWorkers'
import { fmtRelativeTime } from '../sessionOrder'
import { CREW_FINISHED_OPEN_KEY, CREW_WORKER_SEARCH_MIN, CrewPanelBack, CrewWorkerSearch, openCrewSession, useCrewWorkerQuery, useCrewWorkers, useLiveCrewWorkers } from './CrewWorkerList'
import { canOpenCrewWorker, itemOpener, useOpenCrewWorker } from './openCrewWorker'
import { usePersistedBool } from '../../../hooks/usePersistedBool'
import { useCrewMode } from '../../../hooks/usePreviewFlag'
import { CrewMemberContinue, CrewPauseRow } from './CrewPauseControl'
import { conductorModeRunState, type ConductorModeRunState } from '../../../lib/conductorModeRun'
import { boardPlan, childProgress, estimateMinutes, isMerged, isOpen as itemOpen, levelOf, planOf, seatsBusy } from '../../../lib/crewPlan'
import { approvalCarded, boardMove, boardSteps, CONDUCTOR_WAIT_LOUD_MINUTES, movesStories, needsYouCount, reportAnswered, type MilestoneState } from '../../../lib/boardMoves'
import BoardViewSwitch, { type BoardView } from '../../../components/board/BoardViewSwitch'
import { PART_SEP, joinParts, type BoardStory } from '../../../components/board/BoardRow'
import StoryFacts, { storyHasFacts } from '../../../components/board/StoryFacts'
import BoardMovesView from './BoardMovesView'
import BoardStepsView from './BoardStepsView'
import BoardTimelineView from './BoardTimelineView'
import BoardUpNext, { fmtEstimate } from './BoardUpNext'
import OtherProjectsLine from './OtherProjectsLine'
import { SeatsStrip } from '../../../components/board/CapacityLine'

export interface ConductorModeCounts { total: number; done: number; running: number; approvals: number; questions: number }

export function conductorModeCounts(workers: readonly CrewWorker[], approvals: number, questions: number): ConductorModeCounts {
  const g = workers.map(crewWorkerGroup)
  return {
    total: workers.length,
    done: g.filter(x => x === 'done').length,
    running: g.filter(x => x === 'working').length,
    approvals,
    questions,
  }
}

/** One member in the Workers tree, with what its subtree holds: the members
 *  waiting on an approval (`needsYou`) and running (`running`) below it, its
 *  size counting itself, and whether all of it is finished. */
export interface CrewTreeNode {
  w: CrewWorker
  depth: number
  kids: CrewTreeNode[]
  needsYou: number
  running: number
  size: number
  allDone: boolean
}

/** The crew as ONE tree, finished and unfinished members together, each lead
 *  followed by its own crew in the given order. A member whose lead is not in
 *  the list is a top-level row, never indented under nothing. Cycle safe: a
 *  member only reachable through a cycle starts a tree of its own. */
export function crewTree(workers: readonly CrewWorker[], root: string): CrewTreeNode[] {
  const keys = new Set(workers.map(w => w.key))
  const kids = new Map<string, CrewWorker[]>()
  const tops: CrewWorker[] = []
  for (const w of workers) {
    const by = crewSubDispatcher(w, root)
    if (by && keys.has(by)) {
      const list = kids.get(by)
      if (list) list.push(w)
      else kids.set(by, [w])
    } else tops.push(w)
  }
  const seen = new Set<string>()
  const build = (w: CrewWorker, depth: number): CrewTreeNode => {
    seen.add(w.key)
    const below: CrewTreeNode[] = []
    for (const k of kids.get(w.key) ?? []) if (!seen.has(k.key)) below.push(build(k, depth + 1))
    let needsYou = 0, running = 0, size = 1, allDone = crewWorkerGroup(w) === 'done'
    for (const c of below) {
      const g = crewWorkerGroup(c.w)
      needsYou += c.needsYou + (g === 'approval' ? 1 : 0)
      running += c.running + (g === 'working' ? 1 : 0)
      size += c.size
      allDone = allDone && c.allDone
    }
    return { w, depth, kids: below, needsYou, running, size, allDone }
  }
  const out: CrewTreeNode[] = []
  for (const w of tops) if (!seen.has(w.key)) out.push(build(w, 0))
  for (const w of workers) if (!seen.has(w.key)) out.push(build(w, 0))
  return out
}

/** The rows on screen: every node whose ancestors are all open, in tree order. */
export function visibleCrewRows(nodes: readonly CrewTreeNode[], isOpen: (n: CrewTreeNode) => boolean): CrewTreeNode[] {
  const out: CrewTreeNode[] = []
  const walk = (n: CrewTreeNode) => { out.push(n); if (n.kids.length && isOpen(n)) n.kids.forEach(walk) }
  nodes.forEach(walk)
  return out
}

/** The keys on the path down to `key` (its ancestors), so the row on screen
 *  is never folded out of sight. Empty when `key` is not in the tree. */
export function crewAncestors(nodes: readonly CrewTreeNode[], key: string | null): Set<string> {
  const path: string[] = []
  const find = (n: CrewTreeNode): boolean => {
    if (n.w.key === key) return true
    path.push(n.w.key)
    if (n.kids.some(find)) return true
    path.pop()
    return false
  }
  return key && nodes.some(find) ? new Set(path) : new Set()
}

/** Where the Workers tree's fold choices are kept: `{ key: open }` for the rows
 *  the person toggled. A row absent from it uses the default (top-level rows
 *  open, deeper ones folded). Its own key, never shared with the sidebar's. */
export const CREW_BOARD_EXPANDED_KEY = 'mc-crew-board-expanded'

/** Past this depth the rows stop stepping right and carry the level as `·N`, as the sidebar does. */
export const CREW_MAX_INDENT_DEPTH = 6

/** The stored fold choices, or none when the value is unusable. */
export function readCrewBoardExpanded(): Record<string, boolean> {
  try {
    const raw = safeGetItem(CREW_BOARD_EXPANDED_KEY)
    if (!raw) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    return Object.fromEntries(Object.entries(parsed).filter(([k, v]) => k !== '' && typeof v === 'boolean')) as Record<string, boolean>
  } catch {
    return {}
  }
}

/** A row's open state: the person's choice, else open at the top level or on
 *  the path to the member on screen (`onPath`). */
export function crewRowOpen(choices: Readonly<Record<string, boolean>>, n: CrewTreeNode, onPath = false): boolean {
  return choices[n.w.key] ?? (n.depth === 0 || onPath)
}

/** First sentence of a long text, for one-line headers. */
export function firstSentence(text: string): string {
  const t = (text || '').replace(/\s+/g, ' ').trim()
  const m = t.match(/^(.+?[.!?。！？])(\s|$)/)
  return (m ? m[1] : t)
}

/** "Done when": ONE checkable sentence from the items' acceptance. A single
 *  machine-checkable kind (PRs merged, PR checks, files) names it, with the
 *  person's approval for the rest; anything else is the approval sentence. */
export function doneWhen(items: readonly WorkBoardItem[]): string {
  const count = new Map<string, number>()
  for (const i of items) {
    const k = String(i.acceptance?.kind || '')
    if (k) count.set(k, (count.get(k) ?? 0) + 1)
  }
  const checkable = [...count.keys()].filter(k => k === 'pr_merged' || k === 'pr_checks' || k === 'file')
  if (checkable.length !== 1) return i18nT('components.crewMode.conductor_mode_done_line_approval')
  const kind = checkable[0]
  const n = count.get(kind) ?? 0
  // "and you approve the rest" only while such an item is still open.
  const rest = items.some(i => String(i.acceptance?.kind || '') !== kind && i.state !== 'accepted' && !i.terminal)
  return i18nT(DONE_LINE_KEY[kind as keyof typeof DONE_LINE_KEY][rest ? 1 : 0], { count: n })
}

/** Full literal keys per acceptance kind: [alone, with "you approve the rest"]. */
const DONE_LINE_KEY = {
  pr_merged: ['components.crewMode.conductor_mode_done_line_merged', 'components.crewMode.conductor_mode_done_line_merged_rest'],
  pr_checks: ['components.crewMode.conductor_mode_done_line_green', 'components.crewMode.conductor_mode_done_line_green_rest'],
  file: ['components.crewMode.conductor_mode_done_line_files', 'components.crewMode.conductor_mode_done_line_files_rest'],
} as const

/** The goal in the user's own words: the ledger goal's first sentence with
 *  parentheticals removed, whole: the Overview wraps it, never cuts it. */
export function goalSentence(text: string): string {
  return firstSentence(text.replace(/\s*\([^)]*\)/g, '')).replace(/\s+([:,.;])/g, '$1').trim()
}

/** The goal's own "Done when ..." sentence and where it starts, or null. It ends at
 *  a sentence end, never inside a number ("0.63 s"), or where the Plan begins. */
export function goalDoneWhen(text: string): { at: number; what: string } | null {
  const m = text.match(/\bdone when:?\s*(.+?)(?:[.。](?=\s|$)|(?=\s*\b(?:plan|changed)\s*:)|$)/i)
  return m ? { at: m.index ?? 0, what: m[1].trim() } : null
}

/** Scope: a limits line, only when the goal states one ("Scope: ..." / "Limits: ..."). */
export function goalScope(text: string): string {
  const m = text.match(/\b(?:scope|limits?|constraints?)\s*:\s*([^.。\n]+)/i)
  return m ? m[1].trim() : ''
}

/** The goal text's latest re-plan: the last entry of its "Changed:" part, or null
 *  when the goal has none. An entry starts with its time ("10:20", "2026-11-14 10:20")
 *  at the start of the part, a line or after a ";"; with no time, the whole part.
 *  `at` is that time when the entry carries a date (optional seconds and fraction;
 *  a "Z", "+08:00" or "+0800" zone is honoured, none reads as local time), null for
 *  a bare time, no time, or a stamp not followed by a space or the end, so a zone
 *  it cannot read gives no age instead of a wrong one.
 *  shortcut: assumes entries are appended, newest last, as the conductor skill writes them. */
export function latestChange(text: string): { text: string; stamp: string; at: number | null } | null {
  const m = text.match(/\bchanged\s*:\s*([\s\S]+)$/i)
  if (!m) return null
  const part = m[1]
  let from = 0
  for (const t of part.matchAll(/(?:^|[;\n]\s*)((?:\d{4}-\d{2}-\d{2}[ T])?\d{1,2}:\d{2})/g)) from = (t.index ?? 0) + t[0].length - t[1].length
  const entry = part.slice(from).replace(/\s+/g, ' ').trim().replace(/[.。]$/, '')
  if (!entry) return null
  const s = entry.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2})(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?(?=\s|$)/)
  const zone = (s?.[6] ?? '').replace(/^([+-]\d{2})(\d{2})$/, '$1:$2')
  const at = s ? Date.parse(`${s[1]}T${s[2].padStart(2, '0')}:${s[3]}:${s[4] ?? '00'}${s[5] ?? ''}${zone}`) : NaN
  return { text: entry, stamp: s ? s[0] : '', at: Number.isFinite(at) ? at : null }
}

/** A report a worker said only a person can move (`reason` approval / needs_human). */
const PERSON_REASONS = new Set(['approval', 'needs_human'])

// The rule lives with the Board's moves (`lib/boardMoves`), which read it too.
export { reportAnswered }

export function milestoneState(item: WorkBoardItem, waiting: boolean): MilestoneState {
  // A closed item is finished whatever its last report said: done when
  // accepted, not done when rejected or abandoned. Then the person; then a
  // session working again reads as running even after an earlier "done"
  // report. A report counts only until the conductor decides on it
  // (`decided_at` at or after the report hands it back).
  if (item.state === 'accepted') return 'done'
  if (item.terminal) return 'not_done'
  const open = item.status === 'progress' || item.status === 'blocked' || item.status === 'question'
  // Needs you only when the person must act: a pending approval of the item's
  // member (`waiting`), or an unanswered blocked / question report whose reason
  // names a person. An answer -- the conductor's or the person's own steer, a
  // dismiss included -- ends it, and so does a turn running again (the worker moved on).
  if (waiting || ((item.status === 'blocked' || item.status === 'question') && PERSON_REASONS.has(item.reason || '') && !reportAnswered(item) && item.alive !== 'running')) return 'needs_you'
  if (item.alive === 'running') return 'running'
  // Stuck only when nothing is left to move it: its session closed on an open
  // report, or the board is orphaned (no main chat reads its reports). A done
  // report its Conductor handed back (`reportAnswered`) is the worker's move as an
  // open report is, so it is stuck the same way. Any other done report, or an
  // unstarted item, keeps its own state even on an orphaned board.
  const handedBack = item.status === 'done' && reportAnswered(item)
  if ((open || handedBack) && (item.alive === 'closed' || item.orphaned)) return 'stuck'
  // Quiet: the board's `stale` flag with the chat still there. Its worker is
  // idle with no loop to wake it and the next move is its own, so it stays
  // visible, but calmer than stuck: the chat can still be resumed. An item
  // with no report yet stays Not started (it may have no worker bound at all),
  // and raises only the quiet pulse below.
  if (open && item.stale) return 'quiet'
  // Idle, but its own loop will wake it (waiting on CI, the server's `looping`):
  // the flow still runs. A report the conductor still owes an answer stays waiting.
  if (open && item.looping === true && item.alive === 'idle' && !onConductor(item)) return 'running'
  // An idle session after progress, or a question / blocked report the
  // conductor still owns, is waiting, as the List shows it.
  if (open) return 'waiting'
  if (item.status === 'done') return 'done'
  return 'next'
}

/** The conductor still owes this item an answer: an open question, or a blocked report it has not decided on. */
function onConductor(item: WorkBoardItem): boolean {
  return (item.status === 'blocked' || item.status === 'question') && !reportAnswered(item)
}

/** A verdict on the event tail after the newest report: the conductor judged it
 *  (pending while CI, a merge or the person is awaited; fail hands it back). */
function judgedSinceReport(item: WorkBoardItem): boolean {
  const events = item.events || []
  let last = -1
  events.forEach((e, n) => { if (e.kind === 'report') last = n })
  if (last >= 0) return events.slice(last + 1).some(e => e.kind === 'verdict')
  const reported = item.last_report_at ? Date.parse(item.last_report_at) : NaN
  return Number.isFinite(reported) && events.some(e => e.kind === 'verdict' && Date.parse(e.ts) >= reported)
}

/** How long an open item has waited on its conductor, in minutes, or null: its
 *  worker's last report is done, blocked or question and no decision, verdict
 *  (or the person's steer) came after it, and its worker's turn is not running
 *  again (a conductor answering by message writes no ledger answer). An ask that
 *  waits on the person is theirs, not the conductor's. The age is the ledger's
 *  own report time. */
export function conductorWaitMinutes(item: WorkBoardItem, now: number): number | null {
  if (item.state !== 'open' || item.terminal || item.awaits_person || item.alive === 'running') return null
  if (item.status !== 'done' && item.status !== 'blocked' && item.status !== 'question') return null
  if (reportAnswered(item) || judgedSinceReport(item)) return null
  const mins = minutesBetween(item.last_report_at, now)
  return Number.isFinite(mins) ? mins : null
}

/** Whole minutes from `a` to `b`, rounded down: a wait reads 10m, and turns late,
 *  only once ten full minutes have passed. */
function minutesBetween(a: string | null | undefined, b: string | number | null | undefined): number {
  const ta = a ? Date.parse(a) : NaN
  const tb = typeof b === 'number' ? b : b ? Date.parse(b) : NaN
  return Number.isFinite(ta) && Number.isFinite(tb) ? Math.max(0, Math.floor((tb - ta) / 60000)) : NaN
}


/** A text that wraps, a long word (a SHA, a path) included, instead of being cut. */
const WRAP = 'min-w-0 [overflow-wrap:anywhere]'

/** The parts of one story line that are there, each but the last ending in the
 *  separator (`PART_SEP`): a part is a wrap unit, so a line that wraps never starts
 *  with a lone dot. */
function lineParts(parts: ReadonlyArray<JSX.Element | false | null | undefined | ''>): JSX.Element[] {
  const shown = parts.filter((p): p is JSX.Element => !!p)
  return shown.map((p, n) => cloneElement(p, { key: n }, (p.props as { children?: ReactNode }).children, n < shown.length - 1 ? PART_SEP : null))
}

/** A story's line on the Board: what its worker last said, or what it waits on,
 *  as far as the board knows. The story's title, chips and button are the Board's. */
function MilestoneLine({ item, state, waitingOn }: { item: WorkBoardItem; state: MilestoneState; waitingOn: string }) {
  let sub: JSX.Element | string = ''
  const pr = item.pr != null ? i18nT('components.crewMode.conductor_mode_ms_pr', { n: item.pr }) : ''
  if (state === 'done') {
    const link = artifactEntries(item).find(([, v]) => /^https?:\/\//.test(v))
    // Reported done but not accepted yet: it stays in the sprint, so it says Done.
    const open = item.state !== 'accepted'
    const said = firstSentence(item.summary)
    sub = <>{lineParts([
      open && <span className="shrink-0">{i18nT('components.crewMode.conductor_mode_run_done')}</span>,
      said && <span className={WRAP}>{said}</span>,
      pr && <span className="shrink-0 conductor-mode-accent">{pr}</span>,
      link && <a className="shrink-0 conductor-mode-accent hover:underline" href={link[1]} target="_blank" rel="noreferrer">{link[0]}</a>,
    ])}</>
  } else if (state === 'not_done') {
    const text = i18nT(item.state === 'rejected' ? 'components.crewMode.conductor_mode_ms_not_done_rejected' : 'components.crewMode.conductor_mode_ms_not_done_abandoned')
    sub = <span className={WRAP} title={firstSentence(item.summary) || text}>{text}</span>
  } else if (state === 'stuck' || (state === 'waiting' && onConductor(item))) {
    // Who acts next, not the worker's own words: a stuck row says why and how to
    // restart it, and a row the conductor owns says the main chat is on it.
    const said = firstSentence(item.summary)
    const text = i18nT(state === 'waiting' ? 'components.crewMode.conductor_mode_ms_main_chat_answering'
      : item.alive === 'closed' ? 'components.crewMode.conductor_mode_ms_stuck_chat_closed' : 'components.crewMode.conductor_mode_ms_stuck_unread')
    sub = <span className={WRAP} title={said || text}>{text}</span>
  } else if (state === 'quiet') {
    // Why it is quiet and the PR it named on one line, the worker's own last
    // words on the next, so a narrow panel keeps both: the PR number never
    // shrinks, the reason and the words wrap.
    const said = firstSentence(item.summary)
    const why = i18nT('components.crewMode.conductor_mode_ms_quiet')
    sub = <span className="flex flex-col min-w-0 w-full">
      <span className="flex flex-wrap items-baseline gap-x-1 min-w-0">
        {lineParts([
          <span className={WRAP} title={why} data-testid="conductor-mode-quiet-reason">{why}</span>,
          pr && <span className="shrink-0">{pr}</span>,
        ])}
      </span>
      {said && <span className={WRAP} title={said} data-testid="conductor-mode-quiet-said">{said}</span>}
    </span>
  } else if (state === 'waiting') {
    // What it waits on, only as far as the board knows: the PR it reported, then
    // the worker's own words; with neither, a calm "no reason given".
    const said = firstSentence(item.summary)
    const head = item.pr != null ? i18nT('components.crewMode.conductor_mode_ms_waiting_pr', { n: item.pr })
      : said ? i18nT('commandCenter.worker_group_waiting') : i18nT('components.crewMode.conductor_mode_ms_waiting_no_reason')
    sub = <>{lineParts([
      <span className="shrink-0">{head}</span>,
      said && <span className={WRAP} title={said}>{said}</span>,
    ])}</>
  } else {
    // A worker idle inside its own loop reads one way, typed: "In progress · PR #N".
    const text = state === 'needs_you' ? waitingOn
      : state === 'running' && item.alive !== 'running' && pr ? joinParts([i18nT('components.crewMode.conductor_mode_ms_running'), pr])
      : state === 'running' ? joinParts([i18nT('components.crewMode.conductor_mode_ms_running'), firstSentence(item.summary)])
        : i18nT('components.crewMode.conductor_mode_ms_not_started')
    sub = <span className={WRAP} title={text}>{text}</span>
  }
  return (
    <span className={`flex flex-wrap items-baseline gap-x-1 min-w-0 text-[12px] ${state === 'needs_you' ? 'crew-tone-warn' : 'text-muted'}`} data-testid="conductor-mode-milestone" data-state={state}>{sub}</span>
  )
}

/** How long a story has been in its state: the time since its last report (a
 *  running turn's too, and an open one reported done), or since it was created when
 *  it has none yet. */
function milestoneAge(item: WorkBoardItem, now: number): string {
  return fmtMinutes(minutesBetween(item.last_report_at || item.created_at, now))
}

const RUN_ICON: Record<ConductorModeRunState, JSX.Element> = {
  paused: <Pause size={12} aria-hidden="true" className="shrink-0 crew-tone-paused" />,
  waiting: <AlertTriangle size={12} aria-hidden="true" className="shrink-0 crew-tone-warn" />,
  working: <Loader size={12} aria-hidden="true" className="shrink-0 conductor-mode-accent motion-safe:animate-spin" />,
  done: <Check size={12} strokeWidth={3} aria-hidden="true" className="shrink-0 crew-tone-ok" />,
  // A stop sign, never a hollow square that reads as a checkbox.
  stopped: <CircleStop size={12} aria-hidden="true" className="shrink-0 text-muted" />,
}

/** The run's word per state, as every Board says it. */
export const RUN_WORD: Record<ConductorModeRunState, string> = {
  paused: 'components.crewMode.conductor_mode_paused',
  waiting: 'components.crewMode.conductor_mode_run_waiting',
  working: 'components.crewMode.conductor_mode_run_working',
  done: 'components.crewMode.conductor_mode_run_done',
  stopped: 'components.crewMode.conductor_mode_run_stopped',
}

/** The run's state word at the head of the Board's sprint line. The way back to
 *  the main chat is the panel's own Back row. */
function RunWord({ state, run }: { state: ConductorModeRunState; run: ConductorModeRun | undefined }) {
  return (
    <span className="inline-flex items-center gap-1 font-semibold text-text" data-testid="conductor-mode-run" data-state={state}>
      {/* Done with stories not done keeps the check but not its green: the run ended, not everything landed. */}
      {state === 'done' && (run?.not_done ?? 0) > 0
        ? <Check size={12} strokeWidth={3} aria-hidden="true" className="shrink-0 text-muted" /> : RUN_ICON[state]}{i18nT(RUN_WORD[state])}
    </span>
  )
}

/** A settled run's one plain sentence, with no numbers, under the sprint line. No
 *  line says a closing note exists: a stop at a limit or by a person sends none,
 *  and the board cannot tell. */
function runDetail(state: ConductorModeRunState | null, run: ConductorModeRun | undefined): string {
  return state === 'done'
    ? i18nT((run?.not_done ?? 0) > 0 ? 'components.crewMode.conductor_mode_run_done_some' : 'components.crewMode.conductor_mode_run_done_all')
    : state === 'stopped'
      ? `${i18nT(run?.at_limit ? 'components.crewMode.conductor_mode_run_stopped_limit' : 'components.crewMode.conductor_mode_run_stopped_detail')} ${i18nT('components.crewMode.conductor_mode_run_continue')}`
      : ''
}

/** One member card. A member with a crew of its own gets a chevron (`onToggle`;
 *  absent while a search unfolds everything); folded, the card carries its child
 *  count and its subtree's needs-you and running counts, with the sidebar's glyphs.
 *  `gutter` keeps childless rows aligned beside rows that have a chevron.
 *  `pausedIn` (the paused root) marks a member Pause all stopped: its card shows
 *  "Paused" and its own Continue instead of the status dot. */
function MemberRow({ node, open, gutter, onToggle, onOpen, current, pausedIn }: {
  node: CrewTreeNode; open: boolean; gutter: boolean; onToggle?: () => void
  /** Absent on a remote crew's row that names no chat there to open. */
  onOpen?: () => void
  current: boolean; pausedIn?: string
}) {
  const { w, depth } = node
  const title = w.title || i18nT('commandCenter.worker_untitled')
  const indent = Math.min(depth, CREW_MAX_INDENT_DEPTH) * 16
  const done = crewWorkerGroup(w) === 'done'
  const kids = node.kids.length
  const folded = kids > 0 && !open
  const toggleLabel = i18nT(open ? 'pages.chatSidebar.collapse_sessions_this_one_opened' : 'pages.chatSidebar.expand_sessions_this_one_opened')
  return (
    <li data-testid="conductor-mode-member" data-depth={depth} data-done={done ? 'true' : undefined} data-paused={pausedIn ? 'true' : undefined} style={{ paddingLeft: indent }} className="relative flex items-start gap-0.5">
      {depth > 0 && <span aria-hidden="true" className="absolute top-0 bottom-0 border-l border-border" style={{ left: indent - 9 }} />}
      {kids > 0 && onToggle ? (
        <button type="button" onClick={onToggle} aria-expanded={open} aria-label={toggleLabel} title={toggleLabel} data-testid="conductor-mode-member-chevron"
          className="mt-2.5 w-4 h-4 shrink-0 rounded flex items-center justify-center text-muted hover:text-text">
          <ChevronRight size={12} aria-hidden="true" className={`shrink-0 transition-transform duration-200 ${open ? 'rotate-90' : ''}`} />
        </button>
      ) : gutter && <span aria-hidden="true" className="mt-2.5 w-4 h-4 shrink-0" />}
      <button type="button" onClick={onOpen} disabled={!onOpen} aria-current={current ? 'true' : undefined}
        className={`flex-1 min-w-0 text-left rounded-lg border px-3 py-2 text-sm transition-colors ${current ? 'crew-card' : onOpen ? 'border-border bg-bg-elevated/40 hover:bg-bg-hover' : 'border-border bg-bg-elevated/40 cursor-default'}`}>
        <span className="flex items-center gap-2 min-w-0">
          <span className={`font-medium truncate ${done ? 'text-muted' : 'text-text'}`} data-testid="conductor-mode-member-title">{title}</span>
          <span className="ml-auto flex items-center gap-1.5 shrink-0 text-[11px]">
            {depth > CREW_MAX_INDENT_DEPTH && (
              <span className="text-muted tabular-nums" title={i18nT('pages.chatSidebar.nesting_depth', { depth })} data-testid="conductor-mode-member-depth">&middot;{depth}</span>
            )}
            {kids > 0 && (
              <span className="text-muted tabular-nums" role="img" aria-label={i18nT('pages.chatSidebar.opened_sessions_count', { count: kids })}
                title={i18nT('pages.chatSidebar.opened_sessions_count', { count: kids })} data-testid="conductor-mode-member-kids">{kids}</span>
            )}
            {folded && node.needsYou > 0 && (
              <span className="inline-flex items-center gap-0.5 px-1 rounded bg-accent-subtle text-accent tabular-nums"
                title={i18nT('components.crewMode.conductor_mode_needs_you')} data-testid="conductor-mode-member-needs-you">
                <MessageCircleQuestionMark size={10} className="lucide-inline shrink-0" aria-hidden />{node.needsYou}
              </span>
            )}
            {folded && node.running > 0 && (
              <span className="inline-flex items-center gap-0.5 px-1 rounded bg-bg-hover text-muted tabular-nums"
                title={i18nT('pages.chatSidebar.running_session', { count: node.running })} data-testid="conductor-mode-member-running">
                <Loader2 size={10} className="lucide-inline shrink-0 motion-safe:animate-spin" aria-hidden />{node.running}
              </span>
            )}
            {!pausedIn && <CrewStatusDot group={crewWorkerGroup(w)} />}
          </span>
        </span>
        {w.summary ? <span className="block text-[12px] text-text/80 truncate mt-0.5">{w.summary}</span>
          : w.remote_crew && !w.ledger_item && (
            <span className="block text-[12px] text-text/80 truncate mt-0.5" data-testid="conductor-mode-member-no-task">{i18nT('commandCenter.worker_no_task_linked')}</span>
          )}
        <span className="block text-[11px] text-muted truncate mt-0.5">
          <span data-testid="conductor-mode-member-location">{w.remote_crew || i18nT('sessionEnvironments.picker.thisMachine')}</span>
          {w.last_activity && <> &middot; {fmtRelativeTime(w.last_activity)}</>}
        </span>
      </button>
      {pausedIn && <span className="pt-2 pl-1 shrink-0"><CrewMemberContinue root={pausedIn} worker={w.key} /></span>}
    </li>
  )
}

/** The open ledger items that wait on the PERSON: the board read's own
 *  `awaits_person` (goal-board's "needs you" rule, minus a report the conductor or
 *  the person has answered since, the same test as the sidebar's count), less an
 *  item whose tool approval already has its own card (`covered`). Exported for tests. */
export function personAsks(items: readonly WorkBoardItem[], covered: ReadonlySet<string> = new Set()): WorkBoardItem[] {
  return items.filter(item => !!item.awaits_person && !covered.has(item.item_id))
}

function clip(text: string, n: number): string {
  return text.length > n ? `${text.slice(0, n - 1)}…` : text
}

/** What a story that waits on the person offers under its row: goal-board's Reply,
 *  one Write a reply button and, when the conductor offered answers, one Suggested
 *  answers menu holding them (two controls in the row). Each only FILLS the chat box
 *  (`onAct`); the person still presses send, and the line under the buttons says
 *  so. With a crew member on screen (`onAct` absent) it offers the way back to the
 *  main chat (`back`: "Back to main chat", or "Open main chat" reopening a closed
 *  one), the same as the panel's Back row. Then Skip question on its own line, with
 *  what skipping does. */
function PersonAskActions({ root, item, options, onAct, back }: { root: string; item: WorkBoardItem; options: readonly string[]; onAct?: (text: string) => void; back: CrewBack }) {
  const draft = (answer: string) => i18nT('components.crewMode.conductor_mode_reply_draft', { t: clip(item.title, 70), id: item.item_id, answer })
  const small = 'inline-flex items-center gap-1 h-7 px-2 rounded-md text-[12px] font-medium border border-border hover:bg-bg-hover max-w-full'
  // Dismiss: the server writes a person steer on the item, so every surface drops
  // the ask until a newer report asks again. The board is re-read either way.
  // It names the report on screen, so a newer ask filed since the last poll is
  // refused rather than hidden unseen. No report in the tail: no button.
  const queryClient = useQueryClient()
  const reportId = [...(item.events || [])].reverse().find(e => e.kind === 'report')?.id || ''
  const dismiss = useMutation({
    mutationFn: () => api.crewBoardAction(root, item.item_id, 'dismiss', reportId),
    onSettled: () => { void queryClient.invalidateQueries({ queryKey: crewBoardQueryKey(root) }) },
  })
  const dismissHint = i18nT('components.crewMode.conductor_mode_dismiss_hint', { t: clip(item.title, 70) })
  return (
    <>
      {onAct ? (
        <div className="space-y-1">
          <div className="flex items-center gap-1.5" data-testid="conductor-mode-person-ask-actions">
            <button type="button" className={`${small} conductor-mode-accent`} onClick={() => onAct(draft(''))} data-testid="conductor-mode-person-ask-reply">{i18nT('components.crewMode.conductor_mode_reply')}</button>
            {options.length > 0 && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button type="button" className={small} data-testid="conductor-mode-person-ask-answers">{i18nT('components.crewMode.conductor_mode_answers')}<ChevronDown size={12} aria-hidden="true" /></button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="max-w-[280px]">
                  {options.map(o => (
                    <DropdownMenuItem key={o} onSelect={() => onAct(draft(clip(o, 200)))} data-testid="conductor-mode-person-ask-option">
                      <span className="break-words">{clip(o, 80)}</span>
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
          </div>
          <p className="text-[11px] text-muted" data-testid="conductor-mode-person-ask-hint">{i18nT('components.crewMode.conductor_mode_option_title')}</p>
        </div>
      ) : (
        <div className="space-y-1">
          <button type="button" onClick={back.back} data-testid="conductor-mode-person-ask-open-main"
            className="inline-flex items-center gap-1 h-7 px-2 rounded-md text-[12px] font-medium conductor-mode-accent hover:bg-bg-hover">
            <ChevronLeft size={12} aria-hidden="true" />{back.label}
          </button>
          {back.reopenFailed && <ErrorNotice variant="inline" message={i18nT('commandCenter.worker_reopen_failed')} askAgent />}
        </div>
      )}
      {reportId && (
        <p className="flex flex-wrap items-baseline gap-x-1.5 text-[11px] text-muted">
          <button type="button" onClick={() => dismiss.mutate()} disabled={dismiss.isPending} aria-label={dismissHint} title={dismissHint} data-testid="conductor-mode-person-ask-dismiss"
            className="inline-flex items-center gap-1 rounded-sm font-medium hover:text-text disabled:opacity-50">
            <X size={11} aria-hidden="true" />{i18nT('components.crewMode.conductor_mode_dismiss')}
          </button>
          <span data-testid="conductor-mode-person-ask-dismiss-note">{i18nT('components.crewMode.conductor_mode_dismiss_note')}</span>
        </p>
      )}
      {/* The row holds no unsaved input (Reply only fills the chat box), so the hand-off loses nothing. */}
      {dismiss.isError && <ErrorNotice variant="inline" message={errMessage(dismiss.error)} askAgent />}
    </>
  )
}

/** The time, moved on every half minute: an unchanged board poll keeps its data's
 *  reference and re-renders nothing, so a row's age (and the Waits on its
 *  Conductor line's turn to loud) would otherwise freeze. The all-projects page reads it too. */
export function useMinuteClock(): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(t)
  }, [])
  return now
}

export default function ConductorModeCrewPanel({ root, viewing, active, onAct }: {
  root: string
  viewing: string | null
  active: boolean
  /** Put a reply into the main chat's composer (the side panel's composer insert). */
  onAct?: (text: string) => void
}) {
  const dispatch = useAppDispatch()
  const store = useAppStore()
  const isMobile = useIsMobile()
  const listed = useCrewWorkers(root, active)
  const live = useLiveCrewWorkers(root)
  const workers = useMemo(() => mergeCrewWorkers(listed.data?.workers, live), [listed.data, live])
  // Pause all stops this gateway's turns only, so its counts leave remote rows out.
  const localWorkers = workers.filter(w => !w.remote_crew)
  // Pause all is offered only where conductor mode is, like the rest of this panel's crew surfaces.
  const crewModeOn = useCrewMode()
  const pause = crewModeOn ? listed.data?.paused : undefined
  const slots = useAppSelector(s => s.dashboard.slots)
  const approvals = crewApprovalRequests(slots, root, new Map())
  // The same pending-question record the chat's own card reads.
  const pendingQuestion = useAppSelector(st => pendingQuestionFor(st.chat.pendingQuestions, root))
  const questionCount = pendingQuestion?.questions?.length ? 1 : 0
  const c = conductorModeCounts(workers, approvals.length, questionCount)
  const { open, reopenFailed } = useOpenCrewWorker()
  // The main chat's own question is answered on its card in the chat: bring
  // the main chat on screen if a crew member is, then scroll to that card.
  const answerInChat = useCallback(() => {
    if (store.getState().chat.activeSlot !== root) openCrewSession(dispatch, root, !isMobile)
    // On a phone the panel is a sheet over the chat: close it so the card shows.
    else if (isMobile) dispatch(closeActivityPanel())
    let tries = 0
    const reach = () => {
      // The main chat's own card: the single chat's composer or its Split View pane.
      const card = document.querySelector<HTMLElement>(`[data-question-slot="${CSS.escape(root)}"]`)
      if (card) {
        card.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
        card.querySelector<HTMLElement>('button, input, textarea')?.focus({ preventScroll: true })
      } else if (++tries < 20) window.setTimeout(reach, 50)
    }
    reach()
  }, [dispatch, store, root, isMobile])
  // The List's search (from CREW_WORKER_SEARCH_MIN members up) filters live and
  // finished members alike; while it is on, finished matches show unfolded.
  const [query, setQuery] = useCrewWorkerQuery(root)
  const searching = query.trim() !== ''
  const showSearch = searching || workers.length >= CREW_WORKER_SEARCH_MIN
  const [finishedOpen, setFinishedOpen] = usePersistedBool(CREW_FINISHED_OPEN_KEY, false)
  const shown = searching ? searchCrewWorkers(workers, query) : workers
  // ONE tree: a finished member keeps its place under its lead and is only
  // styled. A top-level tree that is finished all through folds into Finished.
  const tree = useMemo(() => crewTree(shown, root), [shown, root])
  const unfinishedTops = tree.filter(n => !n.allDone)
  const finishedTops = tree.filter(n => n.allDone)
  const finishedCount = finishedTops.reduce((s, n) => s + n.size, 0)
  // Only a top-level row can have a crew when any row has one.
  const gutter = tree.some(n => n.kids.length > 0)
  const [foldChoices, setFoldChoices] = useState(readCrewBoardExpanded)
  // The row on screen starts visible: its leads default to open (a stored choice still wins).
  const pathToViewing = useMemo(() => crewAncestors(tree, viewing), [tree, viewing])
  // A search unfolds every match's lead; the stored choices come back after.
  const isOpen = (n: CrewTreeNode) => searching || crewRowOpen(foldChoices, n, pathToViewing.has(n.w.key))
  const toggleRow = useCallback((n: CrewTreeNode, onPath: boolean) => setFoldChoices(prev => {
    // shortcut: choices are never pruned (keys of long-closed sessions stay), a few
    // bytes per toggle; prune to the listed keys if the value ever grows large.
    const next = { ...prev, [n.w.key]: !crewRowOpen(prev, n, onPath) }
    safeSetItem(CREW_BOARD_EXPANDED_KEY, JSON.stringify(next))
    return next
  }), [])
  const unfinished = visibleCrewRows(unfinishedTops, isOpen)
  const finished = visibleCrewRows(finishedTops, isOpen)
  const memberRow = (n: CrewTreeNode) => (
    <MemberRow key={n.w.key} node={n} open={isOpen(n)} gutter={gutter} current={n.w.key === viewing}
      onToggle={searching ? undefined : () => toggleRow(n, pathToViewing.has(n.w.key))} onOpen={canOpenCrewWorker(n.w) ? () => open(n.w) : undefined}
      pausedIn={pause?.stopped.includes(n.w.key) ? root : undefined} />
  )
  // Moves opens first; the per-tab choice is not kept, as before.
  const [view, setView] = useState<BoardView>('moves')
  // The work ledger through the Crew page's own read (GET /api/crew-board), same key and cadence.
  const boardInterval = usePollRule(`crew-board:${root}`, CREW_BOARD_POLL_MS, active, crewBoardQueryKey(root))
  const board = useQuery({ queryKey: crewBoardQueryKey(root), queryFn: () => timedPoll(`crew-board:${root}`, () => api.crewBoard(root)) as Promise<WorkBoardResponse>, enabled: !!root && active, refetchInterval: boardInterval, retry: false })
  // ISO timestamps from the board: a byte compare orders them, no locale involved.
  const items = useMemo(() => [...(board.data?.items ?? [])].sort((a, b) => { const x = a.created_at || '', y = b.created_at || ''; return x < y ? -1 : x > y ? 1 : 0 }), [board.data])
  const goalText = (board.data?.conductor?.goal || '').replace(/\s+/g, ' ').trim()
  // A goal that says its own "Done when ..." wins over the acceptance kinds.
  const doneMatch = goalDoneWhen(goalText)
  const goalFull = goalSentence((doneMatch ? goalText.slice(0, doneMatch.at) : '') || goalText)
  const doneText = doneMatch ? i18nT('components.crewMode.conductor_mode_done_when', { what: doneMatch.what }) : doneWhen(items)
  const scope = goalScope(goalText)
  // The raw goal: its Changed entries may be one per line, which collapsing would lose.
  const changed = latestChange(board.data?.conductor?.goal || '')
  const now = useMinuteClock()
  // The stories whose worker waits on a tool approval with its own card in Needs you:
  // the live cards joined through the crew list. The one rule every Board reads.
  const carded = approvalCarded(approvals, workers)
  // The worker each card waits in, by ledger item (the crew list's join), for its line.
  const approvalWorker = new Map<string, string>()
  for (const a of approvals) {
    const w = workers.find(x => x.key === a.worker)
    if (w?.ledger_item) approvalWorker.set(w.ledger_item, w.title || w.agent || i18nT('commandCenter.worker_untitled'))
  }
  // The ledger items a worker said wait on the person, with the conductor's offered answers.
  const forYou = personAsks(items, carded)
  const asks = new Set(forYou.map(i => i.item_id))
  // Only while the main chat is on screen: the side panel's insert fills the
  // composer on screen, and a reply belongs in the main chat's.
  const replyInto = !viewing || viewing === root ? onAct : undefined
  const crewBack = useCrewBack(root)
  // An epic is the plan's container for its stories, never an outcome of its own.
  const milestones = items.filter(item => levelOf(item) !== 'epic').map(item => {
    const state = milestoneState(item, carded.has(item.item_id) || asks.has(item.item_id))
    const waitingOn = approvalWorker.has(item.item_id)
      ? i18nT('components.crewMode.worker_approval_waiting', { name: approvalWorker.get(item.item_id) })
      : firstSentence(item.summary) || i18nT('components.crewMode.conductor_mode_needs_you')
    return { item, state, waitingOn }
  })
  const openMs = milestones.filter(m => m.state !== 'done')
  const lastReport = items.map(i => i.last_report_at).filter(Boolean).sort().pop() || null
  // Stuck: a milestone with nothing left to move it. Quiet: one idle with
  // nothing scheduled to wake it, whose chat can still be resumed. Both muted.
  const stuck = openMs.some(m => m.state === 'stuck')
  // Any stale open item raises it, a not-started one included, as main's pulse did.
  const quiet = openMs.some(m => m.state === 'quiet' || (m.state === 'next' && m.item.stale))
  // "moving" only while a milestone runs; with nothing running, stuck or quiet
  // (waiting or next only) there is no pulse.
  const moving = openMs.some(m => m.state === 'running')
  const openItem = itemOpener(workers, open)
  const plan = useMemo(() => boardPlan(items), [items])
  // The closed stories, as the sprint held them while open (no epic, no sub-task that
  // shows inside its story): the Timeline keeps a lane for one closed in its window.
  const closedStories = useMemo(() => items.filter(item => {
    if (itemOpen(item) || !item.closed_at || levelOf(item) === 'epic') return false
    const parent = planOf(item).parent
    return !(levelOf(item) === 'task' && parent && plan.byId.has(parent))
  }), [items, plan])
  const byId = new Map(milestones.map(m => [m.item.item_id, m]))
  // Every open story the Conductor is working (not Up next), as one Board row: who
  // must act, how long it has waited, why, and its quiet facts. Moves and Steps draw
  // the same rows.
  const stories: BoardStory[] = plan.sprint.map(item => {
    const m = byId.get(item.item_id)
    const state: MilestoneState = m?.state ?? milestoneState(item, false)
    const wait = state === 'needs_you' ? null : conductorWaitMinutes(item, now)
    const move = boardMove(item, state, wait)
    const estimate = estimateMinutes(item)
    const age = wait != null ? fmtMinutes(wait)
      : move === 'working' && estimate !== undefined ? i18nT('components.crewMode.board_age_of', { age: milestoneAge(item, now), estimate: fmtEstimate(estimate) })
        : milestoneAge(item, now)
    const ask = asks.has(item.item_id)
    const subtasks = childProgress(item, plan.children)
    const w = workers.find(x => x.ledger_item === item.item_id)
    const worker = w ? w.title || w.agent || undefined : undefined
    return {
      item, move, age, late: wait != null && wait >= CONDUCTOR_WAIT_LOUD_MINUTES, onOpen: openItem(item),
      // A person's ask quotes what it asks; every other row says what its worker last
      // said or what it waits on.
      why: ask
        ? <span title={item.summary} data-testid="conductor-mode-person-ask-text">{i18nT('components.crewMode.board_asks', { what: firstSentence(item.summary) })}</span>
        : <MilestoneLine item={item} state={state} waitingOn={m?.waitingOn ?? ''} />,
      facts: storyHasFacts(item, worker, subtasks) ? <StoryFacts item={item} worker={worker} facts={board.data?.pr_facts?.[item.item_id]} subtasks={subtasks} /> : undefined,
      actions: ask ? <PersonAskActions root={root} item={item} options={board.data?.asks?.[item.item_id] ?? []} onAct={replyInto} back={crewBack} /> : undefined,
      testId: ask ? 'conductor-mode-person-ask' : undefined,
    }
  })
  const storyById = new Map(stories.map(s => [s.item.item_id, s]))
  // In Moves a story whose tool approval has its own card is that card, not a second row.
  const moveStories = movesStories(stories, carded)
  // Needs you, first in Moves: the main chat's own question and the crew's approval
  // cards, then the stories that wait on the person.
  const needsYouLeadCount = c.questions + c.approvals
  const needCount = needsYouCount(c.questions, c.approvals, moveStories)
  // The run's one state: what is live here first, then the gateway's verdict.
  // The main chat's own tool approval waits on the person too, though its turn still runs.
  const rootSlot = slots.find(s => s.key === root)
  const runState = conductorModeRunState({
    paused: !!pause, needsYou: needCount > 0 || !!rootSlot?.pending_approval, hasGoal: !!goalText, items: items.length, run: board.data?.run,
    working: moving || c.running > 0 || !!rootSlot?.running,
  })
  const needsYouLead: ReactNode = (
    <>
      {pendingQuestion?.questions?.length ? (
        <div className="flex items-center gap-2" data-testid="conductor-mode-main-question">
          <MessageCircleQuestionMark size={14} aria-hidden="true" className="shrink-0 crew-tone-warn" />
          <span className="flex-1 min-w-0 text-[13px] text-text truncate" title={pendingQuestion.questions[0].question}>{pendingQuestion.questions[0].question}</span>
          <button type="button" onClick={answerInChat} data-testid="conductor-mode-answer-in-chat"
            className="shrink-0 inline-flex items-center gap-1 h-7 px-2 rounded-md text-[12px] font-medium conductor-mode-accent hover:bg-bg-hover">
            {i18nT('components.crewMode.conductor_mode_answer_in_chat')}<ArrowDown size={12} aria-hidden="true" />
          </button>
        </div>
      ) : null}
      <CrewApprovalCards root={root} compact />
    </>
  )
  const upNext = <BoardUpNext root={root} backlog={plan.backlog} openItem={openItem} />
  const loaded = board.isSuccess || (board.isError && isNotFoundError(board.error))
  // The run line's counts, each one Crew can read: stories merged, and seats in use
  // against the goal's cap.
  const storyItems = items.filter(i => levelOf(i) === 'story')
  const cap = board.data?.conductor?.concurrency_cap
  const counts = joinParts([
    storyItems.length > 0 ? i18nT('components.crewMode.plan_epic_progress', { done: storyItems.filter(isMerged).length, total: storyItems.length }) : '',
    cap != null ? i18nT('components.crewMode.plan_slots_busy', { busy: seatsBusy(items), cap }) : '',
  ])
  const detail = runDetail(runState, board.data?.run)
  // The goal as ONE line above the stories. A click opens what sat under it: the
  // whole goal sentence, its "Done when", the scope and the latest change.
  const overview = (
    <section data-testid="conductor-mode-overview" className="space-y-1">
      {/* A ledger the root never opened (404) is the empty state; any other
          failure says so and offers the agent hand-off. */}
      {board.isError && !isNotFoundError(board.error) && <ErrorNotice variant="inline" message={errMessage(board.error) || i18nT('pages.crewBoard.error_title')} askAgent />}
      <details className="group" data-testid="conductor-mode-goal-line">
        <summary className="flex items-baseline gap-2 cursor-pointer list-none text-[14px] font-semibold text-text">
          <span className="shrink-0 inline-flex items-center h-[18px] px-1.5 rounded-sm border border-border text-[10px] font-semibold uppercase tracking-[0.5px] text-muted">{i18nT('components.crewMode.plan_goal_tag')}</span>
          <span className="flex-1 min-w-0 truncate group-open:whitespace-normal group-open:[overflow-wrap:anywhere]" data-testid="conductor-mode-goal">{goalFull || i18nT('components.crewMode.conductor_mode_goal_none')}</span>
          <ChevronRight size={14} aria-hidden="true" className="shrink-0 self-center text-muted transition-transform group-open:rotate-90" />
        </summary>
        <div className="mt-1 space-y-0.5">
          {/* "Done when" needs something to finish: no goal and no story, no line. */}
          {(goalText || items.length > 0) && <p className="text-[12px] text-text/85 [overflow-wrap:anywhere]" data-testid="conductor-mode-done-when">{doneText}</p>}
          {scope && <p className="text-[11px] text-muted [overflow-wrap:anywhere]">{scope}</p>}
          {/* The goal's own Changed line: the conductor's latest re-plan and how old it
              is, so a rewrite shows here and a goal nobody rewrote reads as old. */}
          {changed && <p className="text-[12px] text-muted [overflow-wrap:anywhere]" data-testid="conductor-mode-changed" title={changed.stamp || undefined}>{changed.at != null
            ? i18nT('components.crewMode.conductor_mode_changed_when', { when: fmtRelative(Math.min(changed.at, now)), what: changed.text })
            : i18nT('components.crewMode.conductor_mode_changed', { what: changed.text })}</p>}
        </div>
      </details>
    </section>
  )
  // The sprint's pulse: stuck or quiet (muted), else moving while a story runs.
  const pulse = (stuck || quiet || (moving && lastReport)) ? (
    <span className={`ml-auto text-[11px] inline-flex items-center gap-1 normal-case tracking-normal font-normal ${stuck || quiet ? 'text-muted' : 'crew-tone-ok'}`} data-testid="conductor-mode-pulse">
      <span aria-hidden="true" className="w-1.5 h-1.5 rounded-full bg-current" />
      {stuck ? i18nT('components.crewMode.conductor_mode_stuck') : quiet ? i18nT('components.crewMode.conductor_mode_quiet') : i18nT('components.crewMode.conductor_mode_moving', { when: fmtRelative(lastReport || '') })}
    </span>
  ) : null

  return (
    <div className="h-full flex flex-col min-h-0" data-testid="conductor-mode-crew-panel" data-view={view}>
      {/* A crew member on screen: one click back to its root. */}
      {viewing && viewing !== root && <CrewPanelBack root={root} />}
      {/* A failed crew read, shown in both views; a gateway that does not serve
          the route is not a failure (the live rows stand in). */}
      {listed.isError && !crewRouteNotServed(listed.error) && <div className="shrink-0 px-3 pt-3"><ErrorNotice variant="inline" message={i18nT(live.length ? 'commandCenter.workers_error_live' : 'commandCenter.workers_error')} askAgent /></div>}
      <div className="shrink-0 px-3 pt-3"><BoardViewSwitch value={view} onChange={setView} /></div>
      {view !== 'workers' ? (
        <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-3" data-testid="conductor-mode-board" data-board-view={view}>
          {/* The same head under every position of the switch; only the body changes. */}
          <section className="space-y-1" data-testid="plan-overview">
            {overview}
            {/* No separator between the word and the counts: on a narrow bar the counts
                wrap whole onto the next line, never leaving a lone dot behind. */}
            <p className="flex flex-wrap items-baseline gap-x-2 text-[12px] text-muted" data-testid="plan-sprint-line">
              {runState && <RunWord state={runState} run={board.data?.run} />}
              {counts && <span className="tabular-nums" data-testid="plan-sprint-counts">{counts}</span>}
              {pulse}
            </p>
            {detail && <p className="text-[12px] text-text/85" data-testid="conductor-mode-run-detail">{detail}</p>}
          </section>
          <SeatsStrip active={active} />
          {loaded && (view === 'moves'
            ? <BoardMovesView stories={moveStories} needsYouLead={needsYouLead} needsYouLeadCount={needsYouLeadCount} upNext={upNext} />
            : view === 'steps'
              ? <BoardStepsView steps={boardSteps(items, plan)} stories={storyById} upNext={upNext} />
              : <BoardTimelineView root={root} stories={stories} closed={closedStories} active={active} />)}
          <OtherProjectsLine root={root} now={now} />
        </div>
      ) : (
        <div className="relative flex-1 min-h-0 flex flex-col">
        {showSearch && <CrewWorkerSearch query={query} onChange={setQuery} />}
        {/* A paused crew can always be resumed, its root's tab closed or not; only Pause all needs a live root. */}
        {crewModeOn && (pause || !listed.data?.root_closed) && <CrewPauseRow root={root} paused={pause} running={localWorkers.filter(w => crewWorkerGroup(w) === 'working').length} leadRunning={!!slots.find(s => s.key === root)?.running} needsYou={localWorkers.filter(w => crewWorkerGroup(w) === 'approval').length} />}
        <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-1.5" data-testid="conductor-mode-list">
          {reopenFailed && <ErrorNotice variant="inline" message={i18nT('commandCenter.worker_reopen_failed')} askAgent />}
          {searching && shown.length === 0 && <p role="status" className="text-sm text-muted" data-testid="crew-search-empty">{i18nT('commandCenter.workers_search_empty', { query: query.trim() })}</p>}
          <h3 className="text-[11px] font-semibold uppercase tracking-[1px] text-muted">{`${i18nT('components.crewMode.conductor_mode_crew')} · ${i18nT('components.crewMode.conductor_mode_row_sessions', { count: c.total, running: c.running })}`}</h3>
          <ul className="space-y-1.5">
            {unfinished.map(memberRow)}
          </ul>
          {finishedTops.length > 0 && (
            <details className="group" data-testid="conductor-mode-finished" open={searching || finishedOpen}
              onToggle={e => { if (!searching) setFinishedOpen((e.currentTarget as HTMLDetailsElement).open) }}>
              <summary className="flex items-center gap-1 cursor-pointer list-none text-[12px] text-muted hover:text-text py-1">
                <ChevronRight size={14} className="transition-transform group-open:rotate-90" aria-hidden="true" />
                {i18nT('components.crewMode.conductor_mode_finished', { n: finishedCount })}
              </summary>
              <ul className="space-y-1.5 mt-1">
                {finished.map(memberRow)}
              </ul>
            </details>
          )}
          {/* The same line as under the Board's views, so All projects is one click away here too. */}
          <div className="pt-1.5"><OtherProjectsLine root={root} now={now} /></div>
        </div>
        </div>
      )}
    </div>
  )
}
