/** Moves, the view the Board opens on: one project's open stories in sections named
 *  by who must act, in the order that hurts most -- Needs you, Stuck, the
 *  Conductor's move, Waits on GitHub, Working -- then Up next, the ranked list. A
 *  section with nothing in it stays as one quiet line. The Conductor's move says its
 *  rule in its head: a story its Conductor has not picked up turns red after 10
 *  minutes. Who must act is `boardMove` (`lib/boardMoves.ts`), read on every render
 *  from facts the Board read derives; nothing here is stored. */
import type { ReactNode } from 'react'
import { BOARD_MOVES, CONDUCTOR_WAIT_LOUD_MINUTES, longestWaitingFirst, type BoardMove } from '../../../lib/boardMoves'
import { fmtMinutes } from '../../../lib/crewWork'
import { i18nT } from '../../../i18n/t'
import { MOVE_LABEL, StoryRow, type BoardStory } from '../../../components/board/BoardRow'
import { BoardRows, BoardSection, BoardSections } from '../../../components/board/BoardSection'

/** The rule a section follows, said in its head. */
function moveHint(move: BoardMove): string | undefined {
  if (move === 'stuck') return i18nT('components.crewMode.board_stuck_hint')
  if (move === 'conductor') return i18nT('components.crewMode.board_conductor_hint', { age: fmtMinutes(CONDUCTOR_WAIT_LOUD_MINUTES) })
  return undefined
}

/** The stories under each move, the one waiting longest first. */
export function storiesByMove(stories: readonly BoardStory[]): Map<BoardMove, BoardStory[]> {
  const by = new Map<BoardMove, BoardStory[]>()
  for (const s of stories) by.set(s.move, [...(by.get(s.move) ?? []), s])
  for (const list of by.values()) list.sort((a, b) => longestWaitingFirst(a.item, b.item))
  return by
}

export default function BoardMovesView({ stories, needsYouLead, needsYouLeadCount, upNext }: {
  stories: readonly BoardStory[]
  /** What needs the person and is not a story: the main chat's own question and the
   *  crew's tool approvals. Drawn first in Needs you. */
  needsYouLead?: ReactNode
  needsYouLeadCount: number
  /** The Up next section (`BoardUpNext`). */
  upNext: ReactNode
}) {
  const by = storiesByMove(stories)
  return (
    <BoardSections testId="board-moves">
      {BOARD_MOVES.map(move => {
        const rows = by.get(move) ?? []
        const lead = move === 'needs_you' ? needsYouLeadCount : 0
        return (
          <BoardSection key={move} id={move} name={i18nT(MOVE_LABEL[move])} count={rows.length + lead} hint={moveHint(move)}
            testId={move === 'needs_you' ? 'conductor-mode-needs-you' : `board-section-${move}`}>
            {lead > 0 && <div className={`px-3 py-2 space-y-1.5 ${rows.length ? 'border-b border-border' : ''}`}>{needsYouLead}</div>}
            {rows.length > 0 && <BoardRows>{rows.map(s => <li key={s.item.item_id}><StoryRow story={s} /></li>)}</BoardRows>}
          </BoardSection>
        )
      })}
      {upNext}
    </BoardSections>
  )
}
