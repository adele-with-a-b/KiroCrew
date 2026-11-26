/** The Board's one segmented switch: Moves, Steps, Timeline, Workers. The same
 *  switch heads every Board surface (the right bar today; a page that covers all
 *  projects passes the positions it serves as `views`), and only the body under it
 *  changes. A radio group (`RadioChipGroup`): one Tab stop, arrows move, Space or
 *  Enter picks. Four short words fit one row from 320 px up. */
import { RadioChipGroup, radioChip } from '../RadioChipGroup'
import { i18nT } from '../../i18n/t'

export type BoardView = 'moves' | 'steps' | 'timeline' | 'workers'

/** Every position, in the switch's order. Moves opens first. */
export const BOARD_VIEWS: readonly BoardView[] = ['moves', 'steps', 'timeline', 'workers']

const LABEL: Record<BoardView, string> = {
  moves: 'components.crewMode.board_view_moves',
  steps: 'components.crewMode.board_view_steps',
  timeline: 'components.crewMode.board_view_timeline',
  workers: 'components.crewMode.conductor_mode_view_list',
}

export default function BoardViewSwitch({ value, onChange, views = BOARD_VIEWS }: {
  value: BoardView
  onChange: (view: BoardView) => void
  views?: readonly BoardView[]
}) {
  return (
    <RadioChipGroup ariaLabel={i18nT('components.crewMode.board_view_switch')} className="flex gap-0.5 rounded-lg border border-border bg-bg-hover/40 p-0.5" data-testid="conductor-mode-view-switch">
      {/* A chip a narrow bar cuts short still names itself in full on hover. */}
      {views.map(v => (
        <button key={v} type="button" {...radioChip(value === v)} data-testid={`conductor-mode-view-${v}`} onClick={() => onChange(v)} title={i18nT(LABEL[v])}
          className={`flex-1 min-w-0 truncate rounded-md px-1.5 py-1 text-[12px] font-medium transition-colors ${value === v ? 'bg-bg-elevated text-text shadow-sm' : 'text-muted hover:text-text'}`}>
          {i18nT(LABEL[v])}
        </button>
      ))}
    </RadioChipGroup>
  )
}
