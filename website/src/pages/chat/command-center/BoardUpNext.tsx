/** Up next, the last section of Moves and Steps: the ranked backlog. The number on a
 *  row is its rank and the right edge its estimate; a drag on the grip (or the
 *  keyboard on it) reorders, as the backlog always has. A drop writes only
 *  `plan.rank` (`crew_board_rank.py`), and the Conductor's next pick follows it. The
 *  first three rows show; the rest fold into one line with their count and the
 *  estimates' total, opened with a click. */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { DndContext, DragOverlay, closestCenter, type DragEndEvent, type DragStartEvent } from '@dnd-kit/core'
import { SortableContext, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { GripVertical } from 'lucide-react'
import { api } from '../../../api/client'
import { crewBoardQueryKey, type WorkBoardItem } from '../../../api/crewBoard'
import { backlogCompare, estimateMinutes, planOf, projectedIndex, ranksForDrop } from '../../../lib/crewPlan'
import { useDndSensors } from '../../../hooks/useDndSensors'
import { fmtNumber, fmtUnit } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import ErrorNotice from '../../../components/ErrorNotice'
import { Btn } from '../../../components/ui'
import { errMessage } from '../../../utils/thunkError'
import { BoardRow, joinParts } from '../../../components/board/BoardRow'
import { BoardRows, BoardSection } from '../../../components/board/BoardSection'

/** Rows shown before the fold. */
export const UP_NEXT_SHOWN = 3

/** An estimate in minutes as the person reads it: minutes under an hour, else hours. */
export function fmtEstimate(minutes: number): string {
  // Rounded first, so 59.6 minutes reads as an hour, not as "60m".
  const whole = Math.round(minutes)
  return whole < 60
    ? fmtUnit(whole, 'minute', { maximumFractionDigits: 0 })
    : fmtUnit(minutes / 60, 'hour', { maximumFractionDigits: 1 })
}

function RankNumber({ n }: { n: number }) {
  return <span className="shrink-0 w-4 text-right text-[12px] text-muted tabular-nums mt-px" data-testid="board-up-next-rank">{fmtNumber(n)}</span>
}

function UpNextRow({ item, onOpen, rank, lead }: { item: WorkBoardItem; onOpen?: () => void; rank: number; lead?: ReactNode }) {
  const minutes = estimateMinutes(item)
  return (
    <BoardRow title={item.title} onOpen={onOpen} age={minutes === undefined ? undefined : fmtEstimate(minutes)} testId="board-up-next-row" item={item.item_id}
      lead={<span className="shrink-0 flex items-center gap-1">{lead}<RankNumber n={rank} /></span>} />
  )
}

function SortableUpNextRow({ item, onOpen }: { item: WorkBoardItem; onOpen?: () => void }) {
  const { setNodeRef, setActivatorNodeRef, listeners, attributes, transform, transition, isDragging, index, activeIndex, overIndex } = useSortable({ id: item.item_id })
  // Mid-drag each row shows the number it takes if the drop lands where it hovers.
  const rank = projectedIndex(index, activeIndex, overIndex) + 1
  const grip = (
    <Btn type="button" ref={setActivatorNodeRef} {...attributes} {...listeners} data-testid="plan-backlog-grip"
      aria-label={i18nT('components.crewMode.plan_drag_handle', { title: item.title })}
      className="p-0 border-0 bg-transparent hover:bg-transparent text-muted hover:text-text cursor-grab active:cursor-grabbing active:scale-100 rounded-sm mt-px" style={{ touchAction: 'none' }}>
      <GripVertical size={14} aria-hidden="true" />
    </Btn>
  )
  return (
    <li ref={setNodeRef} className="relative" style={{ transform: transform ? CSS.Transform.toString(transform) : undefined, transition: transition || undefined }} data-testid="plan-backlog-row">
      {/* The dragged row's own slot: the lifted copy (the DragOverlay) covers its box,
          so the drop line floats just above it and the slot itself shows nothing. */}
      {isDragging && overIndex >= 0 && (
        <span className="absolute inset-x-0 -top-2 z-10 flex items-center gap-2 px-3 text-[11px] leading-none font-semibold text-[var(--danger)] pointer-events-none" data-testid="plan-drop-line">
          <span className="flex-1 h-0.5 rounded bg-current" /><span className="px-1 rounded bg-bg-elevated">{i18nT('components.crewMode.plan_drop_here', { n: overIndex + 1 })}</span>
        </span>
      )}
      <div className={isDragging ? 'invisible' : undefined}><UpNextRow item={item} onOpen={onOpen} rank={rank} lead={grip} /></div>
    </li>
  )
}

export default function BoardUpNext({ root, backlog: served, openItem }: {
  root: string
  /** The open backlog stories in pick order (`boardPlan(...).backlog`). */
  backlog: readonly WorkBoardItem[]
  openItem: (item: WorkBoardItem) => (() => void) | undefined
}) {
  // The ranks the person's drops wrote, laid over the last board read until a read
  // carries them, so the list keeps the order dropped and a second drag before that
  // read works from the ranks shown, not the stale ones. The first read after the
  // writes settle is the server's whole answer, so it clears what is left: a rank
  // the conductor wrote over the person's meanwhile is shown, not held off.
  const [pending, setPending] = useState<ReadonlyMap<string, number> | null>(null)
  const saved = useRef(false)
  const [activeId, setActiveId] = useState<string | null>(null)
  const [all, setAll] = useState(false)
  useEffect(() => {
    if (saved.current) {
      saved.current = false
      setPending(null)
      return
    }
    setPending(prev => {
      if (!prev) return prev
      const byId = new Map(served.map(i => [i.item_id, i]))
      const left = new Map([...prev].filter(([id, r]) => {
        const held = byId.get(id)
        return held !== undefined && planOf(held).rank !== r
      }))
      return left.size ? left : null
    })
  }, [served])
  const backlog = useMemo(() => {
    if (!pending) return served
    return served
      .map(i => (pending.has(i.item_id) ? { ...i, plan: { ...planOf(i), rank: pending.get(i.item_id) } } : i))
      .sort(backlogCompare)
  }, [served, pending])

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
    const writes = ranksForDrop(backlog, from, to)
    setPending(prev => new Map([...(prev ?? []), ...writes.map(w => [w.item_id, w.rank] as const)]))
    saved.current = false
    rank.mutate(writes)
  }
  const active = activeId ? backlog.find(i => i.item_id === activeId) : undefined
  // Shown rows are a prefix of the ranked order, so a drop's indices are the list's own.
  const shown = all ? backlog : backlog.slice(0, UP_NEXT_SHOWN)
  const folded = backlog.length - shown.length
  const estimates = backlog.map(estimateMinutes).filter((m): m is number => m !== undefined)
  const total = estimates.reduce((a, b) => a + b, 0)

  return (
    <BoardSection id="up_next" name={i18nT('components.crewMode.board_up_next')} count={backlog.length} hint={i18nT('components.crewMode.board_up_next_hint')} testId="plan-backlog">
      {rank.isError && <div className="px-3 pt-2"><ErrorNotice variant="inline" message={errMessage(rank.error) || i18nT('components.crewMode.plan_rank_failed')} /></div>}
      <DndContext sensors={sensors} collisionDetection={closestCenter} onDragStart={onDragStart} onDragEnd={onDragEnd} onDragCancel={() => setActiveId(null)}>
        {/* One drop's writes at a time: a second drop while they run would rank from rows
            the server has not stored yet, and the two would interleave. */}
        <SortableContext items={shown.map(i => i.item_id)} strategy={verticalListSortingStrategy} disabled={rank.isPending}>
          <BoardRows>
            {shown.map(item => <SortableUpNextRow key={item.item_id} item={item} onOpen={openItem(item)} />)}
          </BoardRows>
        </SortableContext>
        <DragOverlay>{active ? <div className="rounded-md ring-2 ring-[var(--danger)] shadow-lg bg-bg-elevated"><UpNextRow item={active} rank={backlog.indexOf(active) + 1} /></div> : null}</DragOverlay>
      </DndContext>
      {/* One button that folds both ways and stays mounted, so a keyboard user's
          focus stays on it after the press rather than dropping to the page. */}
      {backlog.length > UP_NEXT_SHOWN && (
        <Btn type="button" onClick={() => setAll(v => !v)} aria-expanded={all} data-testid="board-up-next-more"
          className="flex w-full rounded-none border-0 border-t border-border bg-transparent px-3 py-2 pl-[52px] text-left text-[12px] text-muted hover:text-text hover:bg-bg-hover active:scale-100 tabular-nums">
          {all
            ? i18nT('components.crewMode.board_show_fewer')
            : joinParts([i18nT('components.crewMode.board_more', { count: folded }), estimates.length ? i18nT('components.crewMode.board_in_all', { total: fmtEstimate(total) }) : ''])}
        </Btn>
      )}
    </BoardSection>
  )
}
