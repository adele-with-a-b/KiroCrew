/** What one story's view reads off the work ledger, worked out once so the view and
 *  its tests share one reading: the steps left before it lands, and who held it over
 *  the lane the Timeline read draws. Pure, but for `useOpenStory`, the Board's one
 *  open-story state. Nothing here is stored or estimated. */
import { useCallback, useEffect, useState } from 'react'
import { PLAN_STEPS, type PlanStep, type WorkBoardItem } from '../../api/crewBoard'
import { storyStep, type BoardMove } from '../../lib/boardMoves'
import type { TimelineLane } from '../../lib/boardTimeline'

/** The steps a story passes on its way to landing, in order. To do is where a story
 *  starts, not a step it takes. */
export const LAND_STEPS: readonly PlanStep[] = PLAN_STEPS.filter(step => step !== 'todo')

export type LandStepState = 'passed' | 'current' | 'ahead'

/** Each step and where the story stands on it, from the step the Board read gives it
 *  (`storyStep`): the ones before it are behind it, the rest ahead. A story still at
 *  To do has every step ahead of it. */
export function landSteps(item: WorkBoardItem): Array<{ step: PlanStep; state: LandStepState }> {
  const at = LAND_STEPS.indexOf(storyStep(item))
  return LAND_STEPS.map((step, n) => ({ step, state: n < at ? 'passed' : n === at ? 'current' : 'ahead' }))
}

/** Minutes each party held the story across its lane. `github` is `null` when the
 *  ledger has no record of it: no line is written when a wait on GitHub starts, so
 *  only the stretch running now is GitHub's, and only while the story's row says it
 *  waits on GitHub (`boardMove`). Every other stretch is its lane's own kind. */
export interface StoryHeld {
  worker: number
  github: number | null
  conductor: number
  you: number
  /** Nobody on it: never bound, judged and not taken up again, or stuck. */
  nobody: number
}

export function storyHeld(lane: TimelineLane, until: number, move: BoardMove | undefined): StoryHeld {
  const held: StoryHeld = { worker: 0, github: null, conductor: 0, you: 0, nobody: 0 }
  for (const span of lane.spans) {
    const minutes = Math.max(0, span.to - span.from) / 60_000
    if (move === 'github' && span.kind === 'idle' && span.to >= until) held.github = (held.github ?? 0) + minutes
    else if (span.kind === 'working') held.worker += minutes
    else if (span.kind === 'conductor') held.conductor += minutes
    else if (span.kind === 'person') held.you += minutes
    else held.nobody += minutes
  }
  return held
}

/** Minutes from the story's creation to its close, or to `now` while it is open;
 *  NaN when the ledger's stamp does not parse. */
export function storyMinutes(item: WorkBoardItem, now: number): number {
  const from = Date.parse(item.created_at)
  const to = item.closed_at ? Date.parse(item.closed_at) : now
  return Number.isFinite(from) && Number.isFinite(to) ? Math.max(0, Math.floor((to - from) / 60_000)) : NaN
}

/** The Board's open story: which one (`id`, a ledger item id), a row's click to open
 *  it, and the way back. Back puts the focus on that story's row title again, so a
 *  keyboard reader lands where they left. */
export function useOpenStory(): { id: string | null; open: (id: string) => void; back: () => void } {
  const [id, setId] = useState<string | null>(null)
  const [returnTo, setReturnTo] = useState<string | null>(null)
  useEffect(() => {
    if (!returnTo) return
    document.querySelector<HTMLElement>(`[data-item="${CSS.escape(returnTo)}"] [data-testid="board-row-title"]`)?.focus()
    setReturnTo(null)
  }, [returnTo])
  const open = useCallback((next: string) => setId(next), [])
  const back = useCallback(() => {
    setReturnTo(id)
    setId(null)
  }, [id])
  return { id, open, back }
}
