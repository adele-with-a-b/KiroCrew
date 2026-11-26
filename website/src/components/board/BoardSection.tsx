/** A Board section: a band naming who must act (or the step), its count, and on the
 *  right the rule it follows ("red after 10m"), then its rows with a hairline
 *  between each. An empty section stays on the board as one quiet line, its name and
 *  "none", so the person sees nothing waits there instead of wondering where it went.
 *  Sections stack inside one `BoardSections` frame: one edge for the whole list,
 *  never a box per row. */
import type { ReactNode } from 'react'
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'

export function BoardSections({ children, testId }: { children: ReactNode; testId?: string }) {
  return <div className="rounded-lg border border-border overflow-hidden divide-y divide-border" data-testid={testId}>{children}</div>
}

export function BoardSection({ id, name, count, hint, children, testId = 'board-section' }: {
  /** Which section, for tests and styling hooks. */
  id: string
  name: string
  count: number
  hint?: ReactNode
  /** Its rows, each an `<li>`'s content; absent or empty when `count` is 0. */
  children?: ReactNode
  testId?: string
}) {
  const empty = count === 0
  return (
    <section data-testid={testId} data-section={id} data-count={count} data-empty={empty || undefined}>
      <h3 className={`flex items-baseline gap-2 px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[1px] text-muted ${empty ? '' : 'bg-bg-hover/50 border-b border-border'}`}>
        <span className="min-w-0 [overflow-wrap:anywhere]">{name}</span>
        {empty
          ? <span className="normal-case tracking-normal font-normal" data-testid="board-section-none">{i18nT('components.crewMode.board_section_none')}</span>
          : <span className="tabular-nums font-normal" data-testid="board-section-count">{fmtNumber(count)}</span>}
        {hint && <span className="ml-auto shrink-0 normal-case tracking-normal font-normal text-right" data-testid="board-section-hint">{hint}</span>}
      </h3>
      {!empty && children}
    </section>
  )
}

/** A section's rows: hairlines between them, no box around each. */
export function BoardRows({ children }: { children: ReactNode }) {
  return <ul className="divide-y divide-border">{children}</ul>
}
