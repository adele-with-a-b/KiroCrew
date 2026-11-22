/** The conductor mode chat's Crew tab, Backlog view: the board plan as a scrum
 *  team reads it. From the top: the goal and the sprint's seats, the releases,
 *  what needs the person, the sprint (round) the conductor is working, the one it
 *  picks next, the ranked backlog the person drags to reorder, and the epics,
 *  each opening a drawer of its stories and their sub-tasks. The data is the
 *  work ledger's `plan` (`work_plan_vocab.py`); a drag writes only `plan.rank`
 *  (`crew_board_rank.py`), and the conductor's next pick follows it. */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { DndContext, DragOverlay, closestCenter, type DragEndEvent, type DragStartEvent } from '@dnd-kit/core'
import { SortableContext, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { Check, ChevronRight, Circle, GripVertical, Square, SquareCheck } from 'lucide-react'
import { api } from '../../../api/client'
import { crewBoardQueryKey, PLAN_STEPS, type PlanStep, type WorkBoardConductor, type WorkBoardItem } from '../../../api/crewBoard'
import { backlogCompare, boardPlan, childProgress, epicOf, epicTone, isMerged, moveRow, planOf, projectedIndex, ranksForDrop, releaseViews, seatsBusy, stepIndex, type BoardPlan, type ReleaseView } from '../../../lib/crewPlan'
import { useDndSensors } from '../../../hooks/useDndSensors'
import { fmtDateFields } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import ErrorNotice from '../../../components/ErrorNotice'
import { errMessage } from '../../../utils/thunkError'

/** What the Status view already worked out for an item: whether it waits on the
 *  person, and on what. */
export interface BacklogRowState { needsYou: boolean; waitingOn: string }

const H3 = 'text-[11px] font-semibold uppercase tracking-[1px] text-muted'
const HINT = 'text-[11px] text-muted normal-case tracking-normal font-normal'
const CARD = 'rounded-lg border border-border bg-bg-elevated/40'
const WRAP = 'min-w-0 [overflow-wrap:anywhere]'
const NEEDS = 'bg-[color-mix(in_srgb,var(--warn)_12%,transparent)] border-[color-mix(in_srgb,var(--warn)_45%,transparent)]'

const STEP_KEY: Record<PlanStep, string> = {
  todo: 'components.crewMode.plan_step_todo',
  mocks: 'components.crewMode.plan_step_mocks',
  build: 'components.crewMode.plan_step_build',
  review: 'components.crewMode.plan_step_review',
  merged: 'components.crewMode.plan_step_merged',
  released: 'components.crewMode.plan_step_released',
}

/** The step tag: its word, and on an open step a dot per step filled to it. */
function StepTag({ step }: { step: PlanStep | undefined }) {
  if (!step) return null
  const at = stepIndex(step)
  const done = step === 'merged' || step === 'released'
  return (
    <span data-testid="plan-step" data-step={step}
      className={`inline-flex items-center gap-1 h-[18px] px-1.5 rounded-full border text-[11px] font-medium ${done ? 'crew-tone-ok border-current' : step === 'todo' ? 'text-muted border-border' : 'conductor-mode-accent border-current'}`}>
      {i18nT(STEP_KEY[step])}
      {!done && step !== 'todo' && (
        <span aria-hidden="true" className="inline-flex gap-[2px]">
          {PLAN_STEPS.slice(1, 5).map((s, n) => <span key={s} className={`w-1 h-1 rounded-full bg-current ${n + 1 <= at ? '' : 'opacity-30'}`} />)}
        </span>
      )}
    </span>
  )
}

function EpicChip({ epic, plan }: { epic: WorkBoardItem | null; plan: BoardPlan }) {
  if (!epic) return null
  const tone = epicTone(epic, plan.epics)
  return (
    <span className="inline-flex items-center h-[18px] pl-1.5 pr-1.5 border-l-[3px] rounded-sm text-[11px] font-semibold max-w-[14rem] truncate"
      style={{ borderColor: tone, color: tone, background: `color-mix(in srgb, ${tone} 10%, transparent)` }} title={epic.title}>{epic.title}</span>
  )
}

function Tags({ item }: { item: WorkBoardItem }) {
  const tags = planOf(item).tags ?? []
  return <>{tags.map(t => <span key={t} className="inline-flex items-center h-[18px] px-1.5 rounded-sm bg-bg-hover text-[11px] text-muted">{t}</span>)}</>
}

/** One story's row: its title, then its epic, step, tags and PR, and on the
 *  right how many of its sub-tasks are merged. A row that waits on the person is
 *  marked and says on what. */
function StoryRow({ item, plan, row, onOpen, lead }: {
  item: WorkBoardItem; plan: BoardPlan; row?: BacklogRowState; onOpen?: () => void; lead?: ReactNode
}) {
  const progress = childProgress(item, plan.children)
  const needs = !!row?.needsYou
  return (
    <div className={`flex items-start gap-2 px-2.5 py-2 ${CARD} ${needs ? NEEDS : ''}`} data-testid="plan-story" data-item={item.item_id} data-needs-you={needs || undefined}>
      {lead}
      <span className="flex-1 min-w-0 space-y-1">
        {onOpen
          ? <button type="button" onClick={onOpen} className={`block max-w-full text-left text-[13px] font-medium text-text hover:underline ${WRAP}`}>{item.title}</button>
          : <span className={`block text-[13px] font-medium text-text ${WRAP}`}>{item.title}</span>}
        <span className="flex flex-wrap items-center gap-1">
          <EpicChip epic={epicOf(item, plan.byId)} plan={plan} />
          <StepTag step={planOf(item).step} />
          {needs && <span className="inline-flex items-center h-[18px] px-1.5 rounded-full border border-current text-[11px] font-medium crew-tone-warn">{i18nT('components.crewMode.plan_needs_you_tag')}</span>}
          <Tags item={item} />
          {item.pr != null && <span className="text-[11px] text-muted tabular-nums">{i18nT('components.crewMode.plan_pr', { n: item.pr })}</span>}
        </span>
        {needs && row?.waitingOn && (
          <span className="flex flex-wrap items-center gap-1.5 text-[12px]">
            <span className={`crew-tone-warn font-medium ${WRAP}`}>{i18nT('components.crewMode.plan_needs_you_line', { what: row.waitingOn })}</span>
            {onOpen && <button type="button" onClick={onOpen} className="inline-flex items-center h-6 px-2 rounded-md text-[12px] font-medium border border-border hover:bg-bg-hover">{i18nT('components.crewMode.plan_open')}</button>}
          </span>
        )}
      </span>
      {progress.total > 0 && <span className="shrink-0 text-[11px] text-muted tabular-nums mt-0.5">{i18nT('components.crewMode.plan_progress', { done: progress.done, total: progress.total })}</span>}
    </div>
  )
}

function SortableBacklogRow({ item, plan, row, onOpen }: { item: WorkBoardItem; plan: BoardPlan; row?: BacklogRowState; onOpen?: () => void }) {
  const { setNodeRef, setActivatorNodeRef, listeners, attributes, transform, transition, isDragging, index, activeIndex, overIndex } = useSortable({ id: item.item_id })
  // Mid-drag each row shows the number it takes if the drop lands where it hovers.
  const rank = projectedIndex(index, activeIndex, overIndex) + 1
  const grip = (
    <span className="shrink-0 flex items-center gap-1.5 mt-0.5">
      <button type="button" ref={setActivatorNodeRef} {...attributes} {...listeners} data-testid="plan-backlog-grip"
        aria-label={i18nT('components.crewMode.plan_drag_handle', { title: item.title })}
        className="text-muted hover:text-text cursor-grab active:cursor-grabbing rounded-sm" style={{ touchAction: 'none' }}>
        <GripVertical size={14} aria-hidden="true" />
      </button>
      <span className="text-[12px] text-muted tabular-nums w-4 text-right">{rank}</span>
    </span>
  )
  return (
    <li ref={setNodeRef} className="relative" style={{ transform: transform ? CSS.Transform.toString(transform) : undefined, transition: transition || undefined }} data-testid="plan-backlog-row">
      {/* The dragged row's own slot: the lifted copy (the DragOverlay) covers its
          box, so the drop line floats just above it and the slot itself shows nothing. */}
      {isDragging && overIndex >= 0 && (
        <span className="absolute inset-x-0 -top-3.5 z-10 flex items-center gap-2 text-[11px] leading-none font-semibold text-[var(--danger)] pointer-events-none" data-testid="plan-drop-line">
          <span className="flex-1 h-0.5 rounded bg-current" /><span className="px-1 rounded bg-bg-elevated">{i18nT('components.crewMode.plan_drop_here', { n: overIndex + 1 })}</span>
        </span>
      )}
      <div className={isDragging ? 'invisible' : undefined}><StoryRow item={item} plan={plan} row={row} onOpen={onOpen} lead={grip} /></div>
    </li>
  )
}

function ReleaseBlock({ view, plan }: { view: ReleaseView; plan: BoardPlan }) {
  const { release, items } = view
  const tag = <span className="inline-flex items-center h-[18px] px-1.5 rounded-sm border border-border text-[10px] font-semibold uppercase tracking-[0.5px] text-muted">{i18nT('components.crewMode.plan_release_tag')}</span>
  if (release.state === 'shipped') {
    return (
      <details className={`group ${CARD} px-2.5 py-1.5`} data-testid="plan-release" data-state="shipped">
        <summary className="flex items-center gap-2 cursor-pointer list-none text-[12px]">
          <ChevronRight size={12} aria-hidden="true" className="shrink-0 text-muted transition-transform group-open:rotate-90" />
          {tag}
          <span className="font-semibold text-text text-[13px]">{release.name}</span>
          <span className="text-muted">{release.shipped_at
            ? i18nT('components.crewMode.plan_release_shipped', { when: fmtDateFields(release.shipped_at, { month: 'short', day: 'numeric' }), n: items.length })
            : i18nT('components.crewMode.plan_release_shipped_undated', { n: items.length })}</span>
          {release.note && <span className={`ml-auto text-[11px] text-muted ${WRAP}`}>{release.note}</span>}
        </summary>
        <ul className="mt-1 space-y-0.5 pl-5">{items.map(i => <ReleaseItem key={i.item_id} item={i} plan={plan} />)}</ul>
      </details>
    )
  }
  return (
    <div className={`rounded-lg border px-2.5 py-2 border-[color-mix(in_srgb,var(--ok)_55%,transparent)]`} data-testid="plan-release" data-state="planned">
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
        {tag}
        <span className="font-semibold text-text text-[13px]">{release.name}</span>
        <span className="text-muted">{i18nT('components.crewMode.plan_release_next', { merged: view.merged, total: items.length })}</span>
        {release.note && <span className={`ml-auto text-[11px] text-muted ${WRAP}`}>{release.note}</span>}
      </div>
      {items.length > 0 && <ul className="mt-1 space-y-0.5">{items.map(i => <ReleaseItem key={i.item_id} item={i} plan={plan} />)}</ul>}
    </div>
  )
}

function ReleaseItem({ item, plan }: { item: WorkBoardItem; plan: BoardPlan }) {
  const merged = isMerged(item)
  return (
    <li className="flex items-center gap-2 text-[12px]">
      {merged ? <Check size={13} strokeWidth={3} aria-hidden="true" className="shrink-0 crew-tone-ok" /> : <Circle size={12} aria-hidden="true" className="shrink-0 text-muted" />}
      <span className={`flex-1 ${merged ? 'text-text' : 'text-muted'} ${WRAP}`}>{item.title}</span>
      <EpicChip epic={epicOf(item, plan.byId)} plan={plan} />
      {item.pr != null && <span className="shrink-0 text-[11px] text-muted tabular-nums">{i18nT('components.crewMode.plan_pr', { n: item.pr })}</span>}
      {!merged && planOf(item).step === 'review' && <span className="shrink-0 text-[11px] text-muted">{i18nT('components.crewMode.plan_release_in_review')}</span>}
    </li>
  )
}

const LEVEL_KEY = { epic: 'components.crewMode.plan_epic_tag', story: 'components.crewMode.plan_story_tag', task: 'components.crewMode.plan_task_tag' } as const

function LevelTag({ level }: { level: keyof typeof LEVEL_KEY }) {
  return <span className="shrink-0 inline-flex items-center h-[16px] px-1 rounded-sm border border-border text-[9px] font-semibold uppercase tracking-[0.5px] text-muted">{i18nT(LEVEL_KEY[level])}</span>
}

/** The open epic: its stories, each folding open onto its sub-tasks. */
function EpicDrawer({ epic, plan, rowState, openItem }: { epic: WorkBoardItem; plan: BoardPlan; rowState: (id: string) => BacklogRowState | undefined; openItem: (i: WorkBoardItem) => (() => void) | undefined }) {
  const stories = plan.children.get(epic.item_id) ?? []
  const progress = childProgress(epic, plan.children)
  const tone = epicTone(epic, plan.epics)
  return (
    <div className={`${CARD} border-l-[3px] px-2.5 py-2 space-y-1`} style={{ borderLeftColor: tone }} data-testid="plan-epic-drawer" data-item={epic.item_id}>
      <div className="flex flex-wrap items-center gap-2">
        <LevelTag level="epic" />
        <span className={`text-[13px] font-semibold text-text ${WRAP}`}>{epic.title}</span>
        <span className="ml-auto text-[11px] text-muted">{i18nT('components.crewMode.plan_epic_progress', { done: progress.done, total: progress.total })}</span>
      </div>
      {stories.length === 0 && <p className="text-[12px] text-muted">{i18nT('components.crewMode.plan_epic_empty')}</p>}
      <ul className="divide-y divide-border">
        {stories.map(story => {
          const row = rowState(story.item_id)
          const tasks = plan.children.get(story.item_id) ?? []
          const p = childProgress(story, plan.children)
          const open = openItem(story)
          return (
            <li key={story.item_id} data-testid="plan-epic-story" className={row?.needsYou ? `rounded-md border ${NEEDS} px-1.5` : undefined}>
              <details className="group" open={row?.needsYou || undefined}>
                <summary className="flex items-center gap-1.5 py-1 cursor-pointer list-none text-[12px]">
                  <ChevronRight size={12} aria-hidden="true" className={`shrink-0 text-muted transition-transform group-open:rotate-90 ${tasks.length ? '' : 'invisible'}`} />
                  <LevelTag level="story" />
                  <span className={`flex-1 text-text ${WRAP}`}>{story.title}</span>
                  <StepTag step={planOf(story).step} />
                  {row?.needsYou && <span className="inline-flex items-center h-[18px] px-1.5 rounded-full border border-current text-[11px] font-medium crew-tone-warn">{i18nT('components.crewMode.plan_needs_you_tag')}</span>}
                  {p.total > 0 && <span className="text-[11px] text-muted tabular-nums">{i18nT('components.crewMode.plan_progress', { done: p.done, total: p.total })}</span>}
                </summary>
                {tasks.length > 0 && (
                  <ul className="pl-5 pb-1 space-y-0.5">
                    {tasks.map(task => (
                      <li key={task.item_id} className="flex items-center gap-1.5 text-[12px]" data-testid="plan-epic-task">
                        {isMerged(task) ? <SquareCheck size={13} aria-hidden="true" className="shrink-0 crew-tone-ok" /> : <Square size={13} aria-hidden="true" className="shrink-0 text-muted" />}
                        <LevelTag level="task" />
                        <span className={`flex-1 text-text ${WRAP}`}>{task.title}</span>
                        <StepTag step={planOf(task).step} />
                      </li>
                    ))}
                  </ul>
                )}
                {row?.needsYou && row.waitingOn && (
                  <p className="flex flex-wrap items-center gap-1.5 pb-1.5 text-[12px]">
                    <span className={`crew-tone-warn font-medium ${WRAP}`}>{i18nT('components.crewMode.plan_needs_you_line', { what: row.waitingOn })}</span>
                    {open && <button type="button" onClick={open} className="inline-flex items-center h-6 px-2 rounded-md text-[12px] font-medium border border-border hover:bg-bg-hover">{i18nT('components.crewMode.plan_open')}</button>}
                  </p>
                )}
              </details>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

export default function ConductorBacklogView({ root, items, conductor, rowState, openItem, needsYou, needCount }: {
  root: string
  items: readonly WorkBoardItem[]
  conductor: WorkBoardConductor | undefined
  rowState: (itemId: string) => BacklogRowState | undefined
  openItem: (item: WorkBoardItem) => (() => void) | undefined
  /** The Status view's Needs-you section, rendered here as it is there. */
  needsYou: ReactNode
  needCount: number
}) {
  const plan = useMemo(() => boardPlan(items), [items])
  const round = conductor?.round ?? 0
  const cap = conductor?.concurrency_cap
  const busy = seatsBusy(items)
  const free = cap != null ? Math.max(0, cap - busy) : 0
  const releases = releaseViews(conductor?.releases, items)
  const pick = conductor?.next_pick ? plan.byId.get(conductor.next_pick) : undefined

  // The ranks the person's drops wrote, laid over the last board read until a read
  // carries them, so the list keeps the order dropped and a second drag before that
  // read works from the ranks shown, not the stale ones. The first read after the
  // writes settle is the server's whole answer, so it clears what is left: a rank
  // the conductor wrote over the person's meanwhile is shown, not held off.
  const [pending, setPending] = useState<ReadonlyMap<string, number> | null>(null)
  const saved = useRef(false)
  const [wasPick, setWasPick] = useState<string | null>(null)
  const [activeId, setActiveId] = useState<string | null>(null)
  useEffect(() => {
    if (saved.current) {
      saved.current = false
      setPending(null)
      return
    }
    setPending(prev => {
      if (!prev) return prev
      const left = new Map([...prev].filter(([id, r]) => {
        const held = plan.byId.get(id)
        return held !== undefined && planOf(held).rank !== r
      }))
      return left.size ? left : null
    })
  }, [plan])
  const backlog = useMemo(() => {
    if (!pending) return plan.backlog
    return plan.backlog
      .map(i => (pending.has(i.item_id) ? { ...i, plan: { ...planOf(i), rank: pending.get(i.item_id) } } : i))
      .sort(backlogCompare)
  }, [plan.backlog, pending])

  const queryClient = useQueryClient()
  const rank = useMutation({
    mutationFn: async (writes: Array<{ item_id: string; rank: number }>) => {
      for (const w of writes) await api.crewBoardAction(root, w.item_id, 'rank', undefined, w.rank)
    },
    onError: () => setPending(null),
    onSettled: () => {
      saved.current = true
      void queryClient.invalidateQueries({ queryKey: crewBoardQueryKey(root) })
    },
  })
  const sensors = useDndSensors({ distance: 4, keyboard: true })
  const onDragStart = (e: DragStartEvent) => setActiveId(String(e.active.id))
  const onDragEnd = (e: DragEndEvent) => {
    setActiveId(null)
    const from = backlog.findIndex(i => i.item_id === e.active.id)
    const to = e.over ? backlog.findIndex(i => i.item_id === e.over?.id) : -1
    if (rank.isPending || from < 0 || to < 0 || from === to) return
    const next = moveRow(backlog, from, to)
    if (pick && next[0]?.item_id !== pick.item_id) setWasPick(pick.title)
    else if (next[0]?.item_id === pick?.item_id) setWasPick(null)
    const writes = ranksForDrop(backlog, from, to)
    setPending(prev => new Map([...(prev ?? []), ...writes.map(w => [w.item_id, w.rank] as const)]))
    saved.current = false
    rank.mutate(writes)
  }
  const active = activeId ? plan.byId.get(activeId) : undefined
  // While a drop is unsaved the pick shown is the row the person put on top.
  const shownPick = pending ? backlog[0] : pick
  const [epicOpen, setEpicOpen] = useState<string | null>(null)
  const openEpic = plan.epics.find(e => e.item_id === epicOpen) ?? null

  return (
    <div className="flex-1 min-h-0 overflow-y-auto p-3 space-y-4" data-testid="conductor-mode-backlog">
      <section className="space-y-0.5" data-testid="plan-overview">
        <p className="flex items-baseline gap-2 text-[14px] font-semibold text-text">
          <span className="shrink-0 inline-flex items-center h-[18px] px-1.5 rounded-sm border border-border text-[10px] font-semibold uppercase tracking-[0.5px] text-muted">{i18nT('components.crewMode.plan_goal_tag')}</span>
          <span className={WRAP}>{(conductor?.goal || '').split('\n')[0] || i18nT('components.crewMode.conductor_mode_goal_none')}</span>
        </p>
        <p className="text-[12px] text-muted" data-testid="plan-sprint-line">
          {[
            i18nT('components.crewMode.plan_sprint_round', { n: round }),
            cap != null ? i18nT('components.crewMode.plan_slots_busy', { busy, cap }) : null,
          ].filter(Boolean).join(' · ')}
          {needCount > 0 && <span className="crew-tone-warn font-semibold">{` · ${i18nT('components.crewMode.plan_needs_you_count', { n: needCount })}`}</span>}
        </p>
      </section>

      {releases.length > 0 && (
        <section className="space-y-1.5" data-testid="plan-releases">
          <h3 className={H3}>{i18nT('components.crewMode.plan_releases')} <span className={HINT}>{i18nT('components.crewMode.plan_releases_hint')}</span></h3>
          {releases.map(v => <ReleaseBlock key={v.release.name} view={v} plan={plan} />)}
        </section>
      )}

      {needsYou}

      <section className="space-y-1.5" data-testid="plan-sprint">
        <h3 className={H3}>{i18nT('components.crewMode.plan_sprint', { n: round })} <span className={HINT}>{i18nT('components.crewMode.plan_sprint_hint', { n: round })}</span></h3>
        {plan.sprint.length === 0 && <p className="text-[12px] text-muted">{i18nT('components.crewMode.plan_sprint_none')}</p>}
        {plan.sprint.map(item => <StoryRow key={item.item_id} item={item} plan={plan} row={rowState(item.item_id)} onOpen={openItem(item)} />)}
        {free > 0 && backlog.length > 0 && (
          <p className="rounded-lg border border-dashed border-border px-2.5 py-1.5 text-center text-[12px] text-muted" data-testid="plan-slot-free">{i18nT('components.crewMode.plan_slot_free', { n: free })}</p>
        )}
      </section>

      {shownPick && (
        <section className="rounded-lg border px-2.5 py-2 border-[color-mix(in_srgb,var(--ok)_55%,transparent)] bg-[color-mix(in_srgb,var(--ok)_8%,transparent)]" data-testid="plan-next-pick" data-item={shownPick.item_id}>
          <p className="flex flex-wrap items-baseline gap-2">
            <span className="text-[10px] font-semibold uppercase tracking-[1px] crew-tone-ok">{i18nT('components.crewMode.plan_next_pick')}</span>
            <span className={`text-[13px] font-semibold text-text ${WRAP}`}>{shownPick.title}</span>
          </p>
          <p className="text-[11px] text-muted">{i18nT('components.crewMode.plan_next_pick_hint')}</p>
          {wasPick && wasPick !== shownPick.title && <p className="text-[11px] text-muted line-through" data-testid="plan-next-pick-was">{i18nT('components.crewMode.plan_next_pick_was', { title: wasPick })}</p>}
        </section>
      )}

      <section className="space-y-1.5" data-testid="plan-backlog">
        <h3 className={H3}>{i18nT('components.crewMode.plan_backlog')} <span className={HINT}>{i18nT('components.crewMode.plan_backlog_hint')}</span></h3>
        {rank.isError && <ErrorNotice variant="inline" message={errMessage(rank.error) || i18nT('components.crewMode.plan_rank_failed')} />}
        {backlog.length === 0
          ? <p className="text-[12px] text-muted">{i18nT('components.crewMode.plan_backlog_none')}</p>
          : (
            <DndContext sensors={sensors} collisionDetection={closestCenter} onDragStart={onDragStart} onDragEnd={onDragEnd} onDragCancel={() => setActiveId(null)}>
              {/* One drop's writes at a time: a second drop while they run would rank from rows
                  the server has not stored yet, and the two would interleave. */}
              <SortableContext items={backlog.map(i => i.item_id)} strategy={verticalListSortingStrategy} disabled={rank.isPending}>
                <ol className="space-y-2 pt-2">
                  {backlog.map(item => <SortableBacklogRow key={item.item_id} item={item} plan={plan} row={rowState(item.item_id)} onOpen={openItem(item)} />)}
                </ol>
              </SortableContext>
              <DragOverlay>{active ? <div className="rounded-lg ring-2 ring-[var(--danger)] shadow-lg bg-bg-elevated"><StoryRow item={active} plan={plan} /></div> : null}</DragOverlay>
            </DndContext>
          )}
      </section>

      <section className="space-y-1.5" data-testid="plan-epics">
        <h3 className={H3}>{i18nT('components.crewMode.plan_epics')} <span className={HINT}>{i18nT('components.crewMode.plan_epics_hint')}</span></h3>
        {plan.epics.length === 0
          ? <p className="text-[12px] text-muted">{i18nT('components.crewMode.plan_epics_none')}</p>
          : (
            <div className="flex flex-wrap gap-1.5" role="group" aria-label={i18nT('components.crewMode.plan_epics')}>
              {plan.epics.map(epic => {
                const tone = epicTone(epic, plan.epics)
                const p = childProgress(epic, plan.children)
                const on = epic.item_id === epicOpen
                return (
                  <button key={epic.item_id} type="button" aria-pressed={on} data-testid="plan-epic-chip" onClick={() => setEpicOpen(on ? null : epic.item_id)}
                    className={`inline-flex items-center gap-1 h-7 px-2 rounded-md border-l-[3px] text-[12px] font-semibold ${on ? 'ring-2' : ''}`}
                    style={{ borderColor: tone, color: tone, background: `color-mix(in srgb, ${tone} 10%, transparent)`, ['--tw-ring-color' as string]: tone }}>
                    <span className="max-w-[12rem] truncate">{epic.title}</span>
                    <span className="tabular-nums font-medium">{i18nT('components.crewMode.plan_progress', { done: p.done, total: p.total })}</span>
                  </button>
                )
              })}
            </div>
          )}
        {openEpic && <EpicDrawer epic={openEpic} plan={plan} rowState={rowState} openItem={openItem} />}
      </section>
    </div>
  )
}
