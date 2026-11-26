/**
 * The all-projects Board at `/crew-board`: every Conductor's project on one page,
 * reached by the right bar's "All projects" link. `/crew-board?conductor=KEY` keeps
 * serving that one Conductor's board (`CrewBoardPage`), so no saved link breaks.
 *
 * On top, the capacity line (worker chats per computer, and this computer's room)
 * and one table row per project: what its Conductor is doing, stories merged, and
 * four counts, coloured only when not zero. Under it, the same switch as the right
 * bar (Moves, Steps, Timeline) over all projects:
 *
 * - Moves: one column per kind of move -- Needs you, Stuck, the Conductor's move
 *   (red after 10 minutes, the rule said in its head), Waits on GitHub, Working --
 *   and Up next. Who must act is the right bar's own reading (`boardMove`, and
 *   `approvalCarded` over the live approval cards and the crew list), so a story sits
 *   in the same column here as in its project's right bar, and Needs you counts what
 *   the right bar counts.
 * - Steps: one band per project, one cell per step, landed work as counts.
 * - Timeline: each project's Timeline lives in its own Board; the slot says so.
 *
 * A story row opens its worker the way the right bar does (`useOpenCrewWorker`: a
 * chat on a linked Crew in the crew window, a closed one reopened), then shows the
 * chat; a project's name and its asks open its Conductor's chat.
 *
 * Read from what Crew already serves, under the right bar's own query keys and poll
 * rule: each root's `GET /api/crew-board` and crew list, the live session rows, and
 * `GET /api/crew-board/capacity`. Nothing is stored; every number is a count or a
 * clock time.
 */
import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useQueries, useQuery } from '@tanstack/react-query'
import { LayoutGrid } from 'lucide-react'

import { EmptyState, PageHeader } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { api } from '../api/client'
import { isNotFoundError } from '../api/apiError'
import { CREW_BOARD_POLL_MS, PLAN_STEPS, crewBoardQueryKey, type PlanStep, type WorkBoardItem, type WorkBoardResponse } from '../api/crewBoard'
import { CREW_BOARD_CAPACITY_POLL_MS, crewBoardCapacityQueryKey } from '../api/crewBoardCapacity'
import { useAppSelector } from '../store'
import { pendingQuestionFor } from '../store/chatSlice'
import { useCrewMode } from '../hooks/usePreviewFlag'
import { i18nT } from '../i18n/t'
import { fmtNumber } from '../i18n/format'
import { errMessage } from '../utils/thunkError'
import { fmtMinutes } from '../lib/crewWork'
import { estimateMinutes } from '../lib/crewPlan'
import { BOARD_MOVES } from '../lib/boardMoves'
import { liveCrewWorkers, mergeCrewWorkers } from '../lib/crewWorkers'
import { pollGapMs, timedPoll, usePollRule } from '../lib/pollRule'
import {
  allProjectsBoard, computerLoads, projectRoots,
  type AllProjects, type AllProjectsStory, type ProjectBoard, type ProjectLive, type ProjectRow, type ProjectSlotRow, type ProjectSteps,
} from '../lib/allProjectsBoard'
import BoardViewSwitch, { type BoardView } from '../components/board/BoardViewSwitch'
import { BoardRow, MOVE_LABEL, MoveMark, StoryRow, joinParts } from '../components/board/BoardRow'
import { BoardColumn, BoardRows, BoardSection } from '../components/board/BoardSection'
import StoryFacts, { storyHasFacts } from '../components/board/StoryFacts'
import MergedRing from '../components/board/MergedRing'
import CapacityLine from '../components/board/CapacityLine'
import { crewApprovalRequests } from '../components/crew/CrewApprovalCards'
import { RUN_WORD, conductorModeCounts, firstSentence, useMinuteClock } from './chat/command-center/ConductorModeCrewPanel'
import { moveHint, storiesByMove } from './chat/command-center/BoardMovesView'
import { LandedRows, STEP_KEY, byMoveThenWait } from './chat/command-center/BoardStepsView'
import { fmtEstimate } from './chat/command-center/BoardUpNext'
import { crewWorkersQuery, useCrewWorkersPush } from './chat/command-center/CrewWorkerList'
import { itemOpener, useOpenCrewWorker } from './chat/command-center/openCrewWorker'
import CrewBoardPage from './CrewBoardPage'

/** The positions this page serves: no Workers list across projects. */
const PAGE_VIEWS: readonly BoardView[] = ['moves', 'steps', 'timeline']

/** Up next rows shown before "N more". */
const UP_NEXT_SHOWN = 5

/** A row's click: its worker opened, or none when no worker can be opened for it. */
type OpenStory = (s: { root: string; item: WorkBoardItem }) => (() => void) | undefined

function chatPath(root: string): string {
  return `/chat?sid=${encodeURIComponent(root)}`
}

/** A count cell: muted at zero, its tone otherwise. */
function Count({ n, tone }: { n: number; tone?: 'warn' | 'danger' }) {
  const cls = n === 0 ? 'text-muted' : tone === 'warn' ? 'crew-tone-warn font-semibold' : tone === 'danger' ? 'crew-tone-danger font-semibold' : 'text-text font-semibold'
  return <span className={`tabular-nums ${cls}`}>{fmtNumber(n)}</span>
}

function conductorWord(p: ProjectRow): string {
  if (!p.loaded) return ''
  if (p.stories === 0 && (p.run === null || p.run === 'working')) return i18nT('pages.allProjectsBoard.no_stories')
  return p.run ? i18nT(RUN_WORD[p.run]) : ''
}

function ProjectTable({ projects }: { projects: readonly ProjectRow[] }) {
  const head = 'px-3 py-2 text-[12px] font-medium text-muted text-left'
  const num = `${head} text-right`
  const wide = 'hidden md:table-cell'
  return (
    <div className="crew-board-card rounded-xl border border-border overflow-x-auto">
      <table className="w-full text-[13px]" aria-label={i18nT('pages.allProjectsBoard.projects_label')} data-testid="board-projects">
        <thead>
          <tr>
            <th scope="col" className={head}>{i18nT('pages.allProjectsBoard.col_project')}</th>
            <th scope="col" className={`${head} ${wide}`}>{i18nT('pages.allProjectsBoard.col_conductor')}</th>
            <th scope="col" className={`${head} ${wide}`}>{i18nT('pages.allProjectsBoard.col_merged')}</th>
            <th scope="col" className={num}>{i18nT('components.crewMode.board_move_needs_you')}</th>
            <th scope="col" className={num}>{i18nT('components.crewMode.board_move_stuck')}</th>
            <th scope="col" className={num}>{i18nT('pages.allProjectsBoard.col_late')}</th>
            <th scope="col" className={num}>{i18nT('components.crewMode.board_move_working')}</th>
            <th scope="col" className={`${num} ${wide}`}>{i18nT('pages.allProjectsBoard.col_last_look')}</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border border-t border-border">
          {projects.map(p => {
            const quiet = p.loaded && p.stories === 0
            return (
              <tr key={p.root} data-testid="board-project" data-root={p.root} data-quiet={quiet || undefined}>
                <th scope="row" className={`px-3 py-2 text-left font-medium ${quiet ? 'text-muted' : 'text-text'} [overflow-wrap:anywhere]`}>
                  <Link to={chatPath(p.root)} className="hover:underline" data-testid="board-project-name">{p.project}</Link>
                </th>
                <td className={`px-3 py-2 ${wide} ${quiet ? 'text-muted' : 'text-text/85'}`} data-testid="board-project-conductor">{conductorWord(p)}</td>
                <td className={`px-3 py-2 ${wide} tabular-nums`} data-testid="board-project-merged">
                  {p.stories > 0 && <span className="inline-flex items-baseline gap-1"><MergedRing done={p.merged} total={p.stories} />{i18nT('components.crewMode.board_count_of', { n: p.merged, total: p.stories })}</span>}
                </td>
                <td className="px-3 py-2 text-right" data-testid="board-project-needs-you"><Count n={p.needsYou} tone="warn" /></td>
                <td className="px-3 py-2 text-right" data-testid="board-project-stuck"><Count n={p.stuck} tone="danger" /></td>
                <td className="px-3 py-2 text-right" data-testid="board-project-late"><Count n={p.late} tone="danger" /></td>
                <td className="px-3 py-2 text-right" data-testid="board-project-working"><Count n={p.working} /></td>
                <td className={`px-3 py-2 text-right tabular-nums text-muted ${wide}`} data-testid="board-project-last-look">
                  {p.lastLook == null ? i18nT('pages.allProjectsBoard.last_look_none') : fmtMinutes(p.lastLook)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** A row's project, above the shared row, so a column can mix projects. */
function ProjectLabel({ name }: { name: string }) {
  return <div className="px-3 pt-2 -mb-1.5 text-[11px] text-muted [overflow-wrap:anywhere]" data-testid="board-story-project">{name}</div>
}

function storyAge(s: AllProjectsStory): string {
  const estimate = estimateMinutes(s.item)
  return s.move === 'working' && estimate !== undefined
    ? i18nT('components.crewMode.board_age_of', { age: fmtMinutes(s.minutes), estimate: fmtEstimate(estimate) })
    : fmtMinutes(s.minutes)
}

function StoryCell({ s, open, labelled = true }: { s: AllProjectsStory; open: OpenStory; labelled?: boolean }) {
  const said = firstSentence(s.item.summary || '')
  const why = s.move === 'needs_you' && s.item.awaits_person && said ? i18nT('components.crewMode.board_asks', { what: said }) : said
  return (
    <li data-testid="all-projects-story" data-root={s.root}>
      {labelled && <ProjectLabel name={s.project} />}
      <StoryRow story={{
        item: s.item, move: s.move, late: s.late, age: storyAge(s), onOpen: open(s),
        why: why || undefined,
        facts: storyHasFacts(s.item, undefined, s.subtasks) ? <StoryFacts item={s.item} subtasks={s.subtasks} /> : undefined,
      }} />
    </li>
  )
}

function MovesView({ board, open, openRoot }: { board: AllProjects; open: OpenStory; openRoot: (root: string) => void }) {
  const shown = board.upNext.slice(0, UP_NEXT_SHOWN)
  const folded = board.upNext.length - shown.length
  const upNextEstimates = board.upNext.map(u => estimateMinutes(u.item)).filter((m): m is number => m !== undefined)
  // The right bar's Moves: a story whose approval has its own card is that card.
  const by = storiesByMove(board.moves)
  return (
    <div className="grid grid-cols-1 md:grid-cols-3 xl:grid-cols-6 gap-3 items-start" data-testid="all-projects-moves">
      {BOARD_MOVES.map(move => {
        const rows = by.get(move) ?? []
        const asks = move === 'needs_you' ? board.asks : []
        const count = rows.length + asks.reduce((n, a) => n + (a.conductor ? 1 : 0) + a.approvals, 0)
        return (
          <BoardColumn key={move} testId={`all-projects-column-${move}`}>
            <BoardSection id={move} name={i18nT(MOVE_LABEL[move])} count={count} hint={moveHint(move)} testId={`all-projects-section-${move}`}>
              <BoardRows>
                {asks.map(a => (
                  <li key={`ask-${a.root}`} data-testid="all-projects-ask" data-root={a.root}>
                    <ProjectLabel name={a.project} />
                    {/* The question card and the approval cards live in the Conductor's chat. */}
                    <BoardRow mark={<MoveMark move="needs_you" />} tone="warn" onOpen={() => openRoot(a.root)} testId="all-projects-ask-row"
                      title={[a.conductor ? i18nT('pages.allProjectsBoard.conductor_asks') : '', a.approvals > 0 ? i18nT('pages.allProjectsBoard.approvals_wait', { count: a.approvals }) : ''].filter(Boolean).join(' · ')} />
                  </li>
                ))}
                {rows.map(s => <StoryCell key={`${s.root}:${s.item.item_id}`} s={s} open={open} />)}
              </BoardRows>
            </BoardSection>
          </BoardColumn>
        )
      })}
      <BoardColumn testId="all-projects-column-up-next">
        <BoardSection id="up_next" name={i18nT('components.crewMode.board_up_next')} count={board.upNext.length} testId="all-projects-section-up-next">
          <BoardRows>
            {shown.map(u => {
              const estimate = estimateMinutes(u.item)
              return (
                <li key={`${u.root}:${u.item.item_id}`} data-testid="all-projects-up-next" data-root={u.root}>
                  <ProjectLabel name={u.project} />
                  <BoardRow title={u.item.title} onOpen={open(u)} testId="all-projects-up-next-row" item={u.item.item_id}
                    age={estimate === undefined ? undefined : fmtEstimate(estimate)}
                    lead={<span className="shrink-0 w-4 text-right text-[12px] text-muted tabular-nums mt-px">{fmtNumber(u.rank)}</span>} />
                </li>
              )
            })}
          </BoardRows>
          {/* The estimates' total only when the ledger holds some: nothing made up. */}
          {folded > 0 && <p className="px-0.5 pt-2 text-[12px] text-muted tabular-nums" data-testid="all-projects-up-next-more">
            {joinParts([i18nT('components.crewMode.board_more', { count: folded }), upNextEstimates.length ? i18nT('components.crewMode.board_in_all', { total: fmtEstimate(upNextEstimates.reduce((a, b) => a + b, 0)) }) : ''])}
          </p>}
        </BoardSection>
      </BoardColumn>
    </div>
  )
}

/** The cells a project's band draws, in the board's order. */
const BAND_STEPS: readonly PlanStep[] = PLAN_STEPS.filter(step => step !== 'merged' && step !== 'released')

function StepsBand({ band, project, stories, open }: { band: ProjectSteps; project: ProjectRow | undefined; stories: ReadonlyMap<string, AllProjectsStory>; open: OpenStory }) {
  const byStep = new Map(band.steps.groups.map(g => [g.step, g.items]))
  const upNext = band.upNext.slice(0, 3)
  const cell = 'min-w-0 border-t border-border md:border-t-0 md:border-l'
  return (
    <section className="crew-board-card rounded-xl border border-border overflow-hidden" data-testid="all-projects-band" data-root={band.root}>
      <h3 className="crew-board-band flex flex-wrap items-baseline gap-x-3 px-3 py-2 border-b border-border">
        <Link to={chatPath(band.root)} className="text-[13px] font-semibold text-text hover:underline [overflow-wrap:anywhere]">{band.project}</Link>
        {project && <span className="text-[12px] text-muted">{conductorWord(project)}</span>}
        {project && project.stories > 0 && <span className="text-[12px] text-muted tabular-nums">{i18nT('components.crewMode.plan_epic_progress', { done: project.merged, total: project.stories })}</span>}
      </h3>
      <div className="grid grid-cols-1 md:grid-cols-6">
        <div className="min-w-0" data-testid="all-projects-band-up-next">
          <BoardSection id="up_next" name={i18nT('components.crewMode.board_up_next')} count={band.upNext.length}>
            <BoardRows>
              {upNext.map((item, n) => <li key={item.item_id}><BoardRow title={item.title} onOpen={open({ root: band.root, item })} lead={<span className="shrink-0 w-4 text-right text-[12px] text-muted tabular-nums mt-px">{fmtNumber(n + 1)}</span>} /></li>)}
            </BoardRows>
            {band.upNext.length > upNext.length && <p className="px-3 py-2 text-[12px] text-muted">{i18nT('components.crewMode.board_more', { count: band.upNext.length - upNext.length })}</p>}
          </BoardSection>
        </div>
        {BAND_STEPS.map(step => {
          const rows = (byStep.get(step) ?? []).map(i => stories.get(`${band.root}:${i.item_id}`)).filter((s): s is AllProjectsStory => !!s).sort(byMoveThenWait)
          return (
            <div key={step} className={cell} data-testid="all-projects-band-step" data-step={step}>
              <BoardSection id={step} name={i18nT(STEP_KEY[step])} count={rows.length}>
                <BoardRows>{rows.map(s => <StoryCell key={s.item.item_id} s={s} open={open} labelled={false} />)}</BoardRows>
              </BoardSection>
            </div>
          )
        })}
        <div className={cell} data-testid="all-projects-band-landed">
          <BoardSection id="landed" name={i18nT('components.crewMode.board_landed')} count={band.steps.landed.merged + band.steps.landed.notDone}>
            <LandedRows landed={band.steps.landed} />
          </BoardSection>
        </div>
      </div>
    </section>
  )
}

function StepsView({ board, open }: { board: AllProjects; open: OpenStory }) {
  const stories = new Map(board.stories.map(s => [`${s.root}:${s.item.item_id}`, s]))
  const projects = new Map(board.projects.map(p => [p.root, p]))
  return (
    <div className="space-y-3" data-testid="all-projects-steps">
      {board.steps.map(band => <StepsBand key={band.root} band={band} project={projects.get(band.root)} stories={stories} open={open} />)}
    </div>
  )
}

/** Each project's Timeline is drawn in its own Board, on its own read; this page
 *  says where to find it. */
function TimelineSlot() {
  return (
    <div className="rounded-lg border border-dashed border-border px-3 py-6 text-center text-[13px] text-muted" data-testid="all-projects-timeline-slot">
      {i18nT('pages.allProjectsBoard.timeline_in_board')}
    </div>
  )
}

/** Re-reads one root's crew list and board on every push for its crew, and once
 *  when the tab is shown again, as the right bar does (`useCrewWorkersPush`). */
function CrewPush({ root }: { root: string }) {
  useCrewWorkersPush(root, true)
  return null
}

/** Every project's reads, the right bar's own: its board and its crew list under the
 *  right bar's query keys (one cache entry for both pages), the board polled by the
 *  one rule for slow reads (`lib/pollRule`): each board's gap stretches with its own
 *  slow or failed answers, under the right bar's poll id. Coming back to the tab
 *  re-reads each board and crew list once, through `CrewPush`; no second re-read is
 *  asked here. */
function useProjectReads(roots: readonly string[]) {
  const boardReads = useQueries({
    queries: roots.map(root => ({
      queryKey: crewBoardQueryKey(root),
      queryFn: () => timedPoll(`crew-board:${root}`, () => api.crewBoard(root)) as Promise<WorkBoardResponse>,
      refetchInterval: () => pollGapMs(`crew-board:${root}`, CREW_BOARD_POLL_MS),
      retry: false,
    })),
  })
  const crewReads = useQueries({ queries: roots.map(root => crewWorkersQuery(root)) })
  const boards = new Map<string, ProjectBoard>()
  let error: unknown = null
  boardReads.forEach((r, n) => {
    if (r.data) boards.set(roots[n], r.data)
    else if (r.isError && isNotFoundError(r.error)) boards.set(roots[n], null)
    else {
      boards.set(roots[n], undefined)
      if (r.isError && !error) error = r.error
    }
  })
  const crews = new Map(roots.map((root, n) => [root, crewReads[n].data] as const))
  return { boards, crews, error }
}

export function AllProjectsBoard() {
  const slots = useAppSelector(s => s.dashboard.slots)
  const rows = slots as readonly ProjectSlotRow[]
  const pendingQuestions = useAppSelector(s => s.chat.pendingQuestions)
  const crewModeOn = useCrewMode()
  const now = useMinuteClock()
  const navigate = useNavigate()
  const [view, setView] = useState<BoardView>('moves')
  const roots = useMemo(() => projectRoots(rows).map(r => r.key), [rows])
  const { boards, crews, error } = useProjectReads(roots)
  const capacityInterval = usePollRule('crew-board-capacity', CREW_BOARD_CAPACITY_POLL_MS, true, crewBoardCapacityQueryKey)
  const capacity = useQuery({ queryKey: crewBoardCapacityQueryKey, queryFn: () => timedPoll('crew-board-capacity', () => api.crewBoardCapacity()), refetchInterval: capacityInterval, retry: false })
  // Each project's crew as the right bar merges it, and what the right bar reads
  // live beside its board: Pause all, the question card, the approval cards, the
  // crew (which worker each story is bound to) and the members running.
  const workers = new Map(roots.map(root => [root, mergeCrewWorkers(crews.get(root)?.workers, liveCrewWorkers(slots, root))] as const))
  const live = new Map<string, ProjectLive>(roots.map(root => {
    const approvals = crewApprovalRequests(slots, root, new Map())
    const questions = pendingQuestionFor(pendingQuestions, root)?.questions?.length ? 1 : 0
    const crew = workers.get(root) ?? []
    const running = conductorModeCounts(crew, approvals.length, questions).running
    return [root, { paused: crewModeOn && !!crews.get(root)?.paused, questions, approvals, workers: crew, running }]
  }))
  const board = allProjectsBoard(rows, boards, now, live)
  const loads = capacity.data ? computerLoads(boards.values(), capacity.data.name) : []
  const toChat = useCallback(() => navigate('/chat'), [navigate])
  const { open, reopenFailed } = useOpenCrewWorker(toChat)
  const openers = new Map(roots.map(root => [root, itemOpener(workers.get(root) ?? [], open)] as const))
  const openStory: OpenStory = s => openers.get(s.root)?.(s.item)
  const openRoot = (root: string) => navigate(chatPath(root))

  let body: ReactNode
  if (roots.length === 0) {
    body = <EmptyState icon={<LayoutGrid size={20} />} title={i18nT('pages.allProjectsBoard.empty_title')} subtitle={i18nT('pages.allProjectsBoard.empty_body')} testId="all-projects-empty" />
  } else {
    body = (
      <>
        <ProjectTable projects={board.projects} />
        {view === 'moves' && <MovesView board={board} open={openStory} openRoot={openRoot} />}
        {view === 'steps' && <StepsView board={board} open={openStory} />}
        {view === 'timeline' && <TimelineSlot />}
      </>
    )
  }
  return (
    <div className="space-y-3" data-testid="all-projects-board">
      {roots.map(root => <CrewPush key={root} root={root} />)}
      <div className="flex flex-wrap items-start justify-between gap-3">
        {roots.length > 0 && <div className="w-full max-w-[28rem]"><BoardViewSwitch value={view} onChange={setView} views={PAGE_VIEWS} /></div>}
        <CapacityLine loads={loads} capacity={capacity.data} />
      </div>
      {error ? <ErrorNotice variant="inline" message={errMessage(error) || i18nT('pages.crewBoard.error_title')} askAgent /> : null}
      {reopenFailed && <ErrorNotice variant="inline" message={i18nT('commandCenter.worker_reopen_failed')} askAgent />}
      {body}
    </div>
  )
}

/** `/crew-board`: all projects, or one Conductor's board when `?conductor=` names it. */
export default function AllProjectsBoardPage() {
  const [params] = useSearchParams()
  if ((params.get('conductor') ?? '').trim()) return <CrewBoardPage />
  // The Board's own surfaces (`.crew-board`) behind the whole page, header included.
  return (
    <div className="crew-board flex-1 min-h-0 flex flex-col">
      <PageHeader title={i18nT('pages.crewBoard.page_title')} />
      <div className="px-4 pb-8 overflow-y-auto flex-1 min-h-0">
        <AllProjectsBoard />
      </div>
    </div>
  )
}
