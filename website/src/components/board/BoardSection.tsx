/** A Board section: a head naming who must act (or the step), its count, and on the
 *  right the rule it follows ("red after 10m"), then its rows. An empty section stays
 *  on the board as one quiet line, its name and "none", so the person sees nothing
 *  waits there instead of wondering where it went.
 *
 *  Two layouts, as the Board mocks draw them. In a narrow bar, sections stack
 *  inside one `BoardSections` list: a rounded list in the card tone, each head a
 *  band in the page tone, a hairline between rows. On a wide page, a `BoardColumn`
 *  holds one section: its head is a plain line and each row is its own rounded card
 *  with a gap between them. The rows are the same `BoardRows` in both; the layout
 *  around them picks the look. */
import { createContext, useContext, type ReactNode } from 'react'
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'

/** Which layout the rows sit in: a bar's list, or a page column of cards. */
const CardsLayout = createContext(false)

export function BoardSections({ children, testId }: { children: ReactNode; testId?: string }) {
  return <div className="crew-board-card rounded-xl border border-border overflow-hidden divide-y divide-border" data-testid={testId}>{children}</div>
}

/** One section on a wide page: no frame of its own, its rows drawn as cards. */
export function BoardColumn({ children, testId }: { children: ReactNode; testId?: string }) {
  return <CardsLayout.Provider value={true}><div className="min-w-0" data-testid={testId}>{children}</div></CardsLayout.Provider>
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
  const cards = useContext(CardsLayout)
  const empty = count === 0
  const head = cards ? 'px-0.5 pb-1.5 text-muted' : `px-3 py-1.5 crew-board-band ${empty ? '' : 'border-b border-border'}`
  return (
    <section data-testid={testId} data-section={id} data-count={count} data-empty={empty || undefined}>
      <h3 className={`flex items-baseline gap-2 text-[11px] font-semibold uppercase tracking-[1px] ${head}`}>
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

/** A section's rows: in a bar's list, hairlines between them; in a page column,
 *  each `<li>` is its own card (`.crew-board-cards` in `styles/crew-mode.css`). */
export function BoardRows({ children }: { children: ReactNode }) {
  const cards = useContext(CardsLayout)
  return <ul className={cards ? 'crew-board-cards flex flex-col gap-1.5' : 'divide-y divide-border'}>{children}</ul>
}
