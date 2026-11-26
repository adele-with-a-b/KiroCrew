/** Steps, the Board's second view: the same rows as Moves, grouped by the step each
 *  story is at, furthest along first. On top, one count per step; at the foot, what
 *  landed, as counts rather than rows. A step with nothing in it is one quiet line.
 *  The step is the plan's, or the one the Board read derives from facts for a story
 *  nobody planned (`work_plan_vocab.shown_step`); the row's mark still says who must
 *  act. Narrow first: the count strip is three across, six from 22rem up. */
import type { ReactNode } from 'react'
import { CircleX, GitMerge, PackageCheck } from 'lucide-react'
import { PLAN_STEPS, type PlanStep } from '../../../api/crewBoard'
import { BOARD_MOVES, longestWaitingFirst, type BoardSteps, type MovedStory } from '../../../lib/boardMoves'
import { i18nT } from '../../../i18n/t'
import { fmtNumber } from '../../../i18n/format'
import { BoardRow, StoryRow, type BoardStory } from '../../../components/board/BoardRow'
import { BoardRows, BoardSection, BoardSections } from '../../../components/board/BoardSection'

export const STEP_KEY: Record<PlanStep, string> = {
  todo: 'components.crewMode.plan_step_todo',
  mocks: 'components.crewMode.plan_step_mocks',
  build: 'components.crewMode.plan_step_build',
  review: 'components.crewMode.plan_step_review',
  merged: 'components.crewMode.plan_step_merged',
  released: 'components.crewMode.plan_step_released',
}

/** Who must act first, then the one waiting longest. */
export function byMoveThenWait(a: MovedStory, b: MovedStory): number {
  return BOARD_MOVES.indexOf(a.move) - BOARD_MOVES.indexOf(b.move) || longestWaitingFirst(a.item, b.item)
}

function StepStrip({ strip }: { strip: Record<PlanStep, number> }) {
  return (
    <div className="@container">
      {/* A dl's own name is not announced, so the group around it carries it. Each
          cell reads term then count; the column puts the count on top for the eye. */}
      <div role="group" aria-label={i18nT('components.crewMode.board_steps_strip')}>
      <dl className="grid grid-cols-3 @[22rem]:grid-cols-6 rounded-lg border border-border overflow-hidden" data-testid="board-step-strip">
        {PLAN_STEPS.map((step, n) => (
          <div key={step} className={`flex flex-col-reverse px-2 py-1.5 min-w-0 ${n % 3 ? 'border-l border-border' : ''} ${n >= 3 ? 'border-t border-border @[22rem]:border-t-0' : ''} ${n === 3 ? '@[22rem]:border-l' : ''}`} data-testid="board-step-cell" data-step={step}>
            <dt className="text-[11px] text-muted truncate" title={i18nT(STEP_KEY[step])}>{i18nT(STEP_KEY[step])}</dt>
            <dd className={`text-[15px] font-semibold tabular-nums leading-tight ${strip[step] ? 'text-text' : 'text-muted'}`}>{fmtNumber(strip[step])}</dd>
          </div>
        ))}
      </dl>
      </div>
    </div>
  )
}

function LandedRow({ icon, label, value, testId }: { icon: ReactNode; label: string; value: string; testId: string }) {
  return <li><BoardRow mark={<span className="text-muted inline-flex">{icon}</span>} title={label} age={value} testId={testId} /></li>
}

/** What landed, as counts: merged, of those released, and closed not done. The
 *  all-projects page's bands draw the same rows. */
export function LandedRows({ landed }: { landed: BoardSteps['landed'] }) {
  return (
    <BoardRows>
      <LandedRow icon={<GitMerge size={14} aria-hidden="true" />} label={i18nT('components.crewMode.plan_step_merged')} value={fmtNumber(landed.merged)} testId="board-landed-merged" />
      {landed.merged > 0 && (
        <LandedRow icon={<PackageCheck size={14} aria-hidden="true" />} label={i18nT('components.crewMode.plan_step_released')}
          value={i18nT('components.crewMode.board_count_of', { n: landed.released, total: landed.merged })} testId="board-landed-released" />
      )}
      {landed.notDone > 0 && (
        <LandedRow icon={<CircleX size={14} aria-hidden="true" />} label={i18nT('components.crewMode.board_landed_not_done')} value={fmtNumber(landed.notDone)} testId="board-landed-not-done" />
      )}
    </BoardRows>
  )
}

export default function BoardStepsView({ steps, stories, upNext }: {
  steps: BoardSteps
  /** The Moves rows by item id: the same rows, regrouped. */
  stories: ReadonlyMap<string, BoardStory>
  upNext: ReactNode
}) {
  const { landed } = steps
  const landedCount = landed.merged + landed.notDone
  return (
    <div className="space-y-3" data-testid="board-steps">
      <StepStrip strip={steps.strip} />
      <BoardSections>
        {steps.groups.map(({ step, items }) => {
          const rows = items.map(i => stories.get(i.item_id)).filter((s): s is BoardStory => !!s).sort(byMoveThenWait)
          return (
            <BoardSection key={step} id={step} name={i18nT(STEP_KEY[step])} count={rows.length} testId="board-step-section">
              <BoardRows>{rows.map(s => <li key={s.item.item_id}><StoryRow story={s} /></li>)}</BoardRows>
            </BoardSection>
          )
        })}
        {upNext}
      </BoardSections>
      <BoardSections>
        <BoardSection id="landed" name={i18nT('components.crewMode.board_landed')} count={landedCount} testId="board-landed">
          <LandedRows landed={landed} />
        </BoardSection>
      </BoardSections>
    </div>
  )
}
