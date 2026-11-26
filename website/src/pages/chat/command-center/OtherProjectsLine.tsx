/** The Board's one line for the projects this chat is not: "Other projects: 1 needs
 *  you, 1 stuck, 2 late". The right bar shows the current chat's project only; this
 *  line is the rest, read from what every other Conductor's row already carries
 *  (`crew_work`, the same facts the Sessions list counts) and the live rows, so it
 *  costs no read of another board. Needs you is the sidebar's own count; stuck is
 *  its stalled count; late is a report its Conductor has not picked up for 10
 *  minutes (`conductor_since_ms`). Only counts above zero are said, and with none
 *  the sentence is not drawn. The "All projects" link (`AllProjectsLink`) ends the
 *  line, and stands alone when nothing is counted. */
import { useAppSelector } from '../../../store'
import AllProjectsLink from './AllProjectsLink'
import { crewRootOf } from '../../../lib/crewMode'
import { crewNeedsYou, crewWorkCounts, crewWorkLate } from '../../../lib/crewWork'
import type { CrewWorkFacts } from '../../../types'
import { fmtList } from '../../../i18n/format'
import { Trans } from 'react-i18next'
import { i18nT } from '../../../i18n/t'

/** The slot-row fields this reads. */
export interface OtherProjectRow {
  key: string
  running?: boolean
  crew_mode_root?: boolean
  crew_root?: string | null
  crew_work?: CrewWorkFacts
  pending_approval?: unknown
  needs_input?: boolean
}

export interface OtherProjectsCounts { needsYou: number; stuck: number; late: number }

/** What the other Conductor-mode projects hold, summed over their roots. */
export function otherProjectsCounts(rows: readonly OtherProjectRow[], root: string, now: number): OtherProjectsCounts {
  let needsYou = 0, stuck = 0, late = 0
  for (const r of rows) {
    if (!r.crew_mode_root || r.key === root || !r.crew_work) continue
    const live = rows.filter(x => x.pending_approval && crewRootOf(x) === r.key).map(x => x.key)
    needsYou += crewNeedsYou(undefined, live, r.crew_work.person, r.crew_work.person_workers) + (r.pending_approval || r.needs_input ? 1 : 0)
    // A running Conductor turn is picking its stalls up, as its Sessions row reads it.
    if (!r.running) stuck += crewWorkCounts(r.crew_work, rows, now)?.stalled ?? 0
    late += crewWorkLate(r.crew_work, rows, now)
  }
  return { needsYou, stuck, late }
}

export default function OtherProjectsLine({ root, now }: { root: string; now: number }) {
  const rows = useAppSelector(s => s.dashboard.slots) as readonly OtherProjectRow[]
  const { needsYou, stuck, late } = otherProjectsCounts(rows, root, now)
  const parts = [
    needsYou > 0 ? i18nT('components.crewMode.conductor_mode_row_count_needs_you', { count: needsYou }) : '',
    stuck > 0 ? i18nT('components.crewMode.board_count_stuck', { count: stuck }) : '',
    late > 0 ? i18nT('components.crewMode.board_count_late', { count: late }) : '',
  ].filter(Boolean)
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
      {parts.length > 0 && (
        <p className="min-w-0 text-[12px] text-muted [overflow-wrap:anywhere]" data-testid="board-other-projects">
          {/* One whole sentence with the list as its placeholder, so a language can put
              the list where its own word order wants it. */}
          <Trans i18nKey="components.crewMode.board_other_projects" values={{ list: fmtList(parts) }}
            components={{ list: <span className="font-medium text-text" /> }} />
        </p>
      )}
      <span className="ml-auto"><AllProjectsLink /></span>
    </div>
  )
}
