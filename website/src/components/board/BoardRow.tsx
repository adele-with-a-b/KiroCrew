/** The Board's one row shape, the same in every view: a state mark, the title, and
 *  the age at the right edge in even-width digits; then one line saying why; then
 *  one quiet line of facts (worker and machine, PR, checks, sub-tasks). A row draws
 *  no box itself: the layout it sits in does (`BoardSection`), as the Board mocks
 *  draw it -- in a bar's list a hairline between rows, on a wide page each
 *  row its own rounded card. Each state reads as its colour: a `warn` tone (it waits
 *  on the person) or a `danger` tone (stuck or late) colours the mark and the age,
 *  and a working story's mark is green; the rest stays neutral, and there is no
 *  colour per project. */
import type { ReactNode } from 'react'
import { CircleAlert, CirclePlay, GitPullRequest, MessageCircleQuestionMark, Zap } from 'lucide-react'
import type { WorkBoardItem } from '../../api/crewBoard'
import type { BoardMove } from '../../lib/boardMoves'
import { i18nT } from '../../i18n/t'
import { Btn } from '../ui'

export type BoardTone = 'warn' | 'danger'

/** Between two parts of one Board line, written at the END of the part before: the
 *  no-break space keeps the dot with that part, so a line that wraps never starts
 *  with a lone separator. */
export const PART_SEP = '\xa0·'

/** One line's text parts, empty ones dropped, each separator kept with the part before it. */
export function joinParts(parts: readonly string[]): string {
  return parts.filter(Boolean).join(`${PART_SEP} `)
}

const TONE_CLASS: Record<BoardTone, string> = { warn: 'crew-tone-warn', danger: 'crew-tone-danger' }

const WRAP = 'min-w-0 [overflow-wrap:anywhere]'

export function BoardRow({ mark, title, onOpen, age, tone, why, facts, actions, lead, testId = 'board-row', item, move }: {
  /** The state mark, drawn left of the title. */
  mark?: ReactNode
  title: string
  /** Opens the row's chat; the title becomes a button. */
  onOpen?: () => void
  /** The age (or the estimate, on a ranked row) at the right edge. */
  age?: string
  tone?: BoardTone
  /** Line two: why it is here. */
  why?: ReactNode
  /** Line three: quiet facts. */
  facts?: ReactNode
  /** Under the lines: at most two buttons per row. */
  actions?: ReactNode
  /** Before the mark: a rank, a drag grip. */
  lead?: ReactNode
  testId?: string
  item?: string
  move?: BoardMove
}) {
  const toneClass = tone ? TONE_CLASS[tone] : 'text-muted'
  return (
    <div className="flex items-start gap-2 px-3 py-2" data-testid={testId} data-item={item} data-move={move} data-tone={tone}>
      {lead}
      {mark && <span className="shrink-0 mt-[3px]">{mark}</span>}
      <div className="flex-1 min-w-0 space-y-0.5">
        <div className="flex items-baseline gap-2">
          {onOpen
            ? <Btn type="button" onClick={onOpen} className={`flex-1 block p-0 border-0 rounded-sm bg-transparent hover:bg-transparent active:scale-100 text-left text-[13px] font-medium text-text hover:underline ${WRAP}`} data-testid="board-row-title">{title}</Btn>
            : <span className={`flex-1 text-[13px] font-medium text-text ${WRAP}`} data-testid="board-row-title">{title}</span>}
          {age && <span className={`shrink-0 text-[12px] tabular-nums ${toneClass}`} data-testid="board-row-age">{age}</span>}
        </div>
        {why && <div className={`text-[12px] text-text/85 ${WRAP}`} data-testid="board-row-why">{why}</div>}
        {facts && <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-muted" data-testid="board-row-facts">{facts}</div>}
        {actions && <div className="pt-1 space-y-1">{actions}</div>}
      </div>
    </div>
  )
}

const MOVE_ICON = {
  needs_you: MessageCircleQuestionMark,
  stuck: CircleAlert,
  conductor: Zap,
  github: GitPullRequest,
  working: CirclePlay,
} as const

/** The move's own word, the section it heads in Moves. */
export const MOVE_LABEL: Record<BoardMove, string> = {
  needs_you: 'components.crewMode.board_move_needs_you',
  stuck: 'components.crewMode.board_move_stuck',
  conductor: 'components.crewMode.board_move_conductor',
  github: 'components.crewMode.board_move_github',
  working: 'components.crewMode.board_move_working',
}

/** The tone a move is drawn in: the person's yellow, red for stuck or a late
 *  Conductor's move, neutral for the rest. */
export function moveTone(move: BoardMove, late: boolean): BoardTone | undefined {
  return move === 'needs_you' ? 'warn' : move === 'stuck' || (move === 'conductor' && late) ? 'danger' : undefined
}

/** One mark per move, each a different shape so it reads without colour. Named,
 *  because in Steps it is the only place the move is said. A working story's mark
 *  is green, as in the mocks; its age stays neutral. */
export function MoveMark({ move, late = false }: { move: BoardMove; late?: boolean }) {
  const Icon = MOVE_ICON[move]
  const tone = moveTone(move, late)
  const label = i18nT(MOVE_LABEL[move])
  return (
    <span role="img" aria-label={label} title={label} data-testid="board-move-mark" data-move={move}
      className={`inline-flex ${tone ? TONE_CLASS[tone] : move === 'working' ? 'crew-tone-ok' : 'text-muted'}`}>
      <Icon size={14} aria-hidden="true" />
    </span>
  )
}

/** A story as the Board views draw it: who must act, how long it has waited, and
 *  the lines the panel worked out from its reports. */
export interface BoardStory {
  item: WorkBoardItem
  move: BoardMove
  age: string
  /** A Conductor's move past its 10 minutes. */
  late: boolean
  why?: ReactNode
  facts?: ReactNode
  actions?: ReactNode
  onOpen?: () => void
  /** The row's own test id; a person's ask keeps `conductor-mode-person-ask`. */
  testId?: string
}

export function StoryRow({ story }: { story: BoardStory }) {
  const { item, move, late } = story
  return (
    <BoardRow mark={<MoveMark move={move} late={late} />} title={item.title} onOpen={story.onOpen} age={story.age} tone={moveTone(move, late)}
      why={story.why} facts={story.facts} actions={story.actions} testId={story.testId ?? 'board-story'} item={item.item_id} move={move} />
  )
}
