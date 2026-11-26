/** The "Needs you" group: the chats that wait on the person (`needsYouOf`), on
 *  top of every list lane, above the pinned band, oldest wait first. In the flat
 *  lane and among the unfiled rows a waiting chat leaves its usual place, so it is
 *  drawn once; a chat filed in a folder stays in its folder too, drawn as loud
 *  there, because the folder is where the person keeps it. */
import { useMemo, useRef } from 'react'
import { Hand } from 'lucide-react'
import type { NeedsYou } from '../../types'
import { oldestWaitFirst } from '../../lib/needsYou'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import type { Slot } from './types'
import { sessionRowIdentity } from './rowIdentity'
import type { LaneRows } from './laneRows'
import type { SidebarScope } from './scope'
import { ListSectionHeading } from './kindSections'

/** The group a lane draws on top, as the shell resolved it this render. */
export interface NeedsGroup {
  /** The waiting rows, oldest wait first. Empty while nothing waits, while a search
   *  ranks the list (the ranking is the answer) and on the board. */
  rows: Slot[]
  /** Their row identities, for the lanes that draw each chat once. */
  ids: ReadonlySet<string>
  /** The group's own heading and the "Everything else" one below it. Off while the
   *  "Needs you" filter narrows the list: every row is then a waiting one. */
  headed: boolean
  /** Chats the "Needs you" filter keeps off the list, or 0 while it does not narrow. */
  hiddenByFilter: number
}

const NO_ROWS: Slot[] = []

/**
 * The group's rows for this render. While the pointer is over the list or a drag
 * runs, a chat that STARTS waiting does not join yet, so nothing moves under the
 * pointer (the hover hold keeps a row inside the group still): it is drawn loud
 * where it is and joins when the pointer leaves. A chat that stops waiting leaves
 * at once, because the group never shows a chat the rule says is not waiting.
 */
export function useNeedsGroupRows({ rows, needsOf, enabled, frozen }: {
  rows: Slot[]
  needsOf: (slot: Slot) => NeedsYou | null
  enabled: boolean
  frozen: boolean
}): Slot[] {
  const live = useMemo(
    () => (enabled ? oldestWaitFirst(rows.filter(s => needsOf(s) !== null), needsOf) : NO_ROWS),
    [rows, needsOf, enabled],
  )
  const drawnRef = useRef<Slot[]>(live)
  if (!frozen) drawnRef.current = live
  else {
    const kept = new Set(drawnRef.current.map(sessionRowIdentity))
    drawnRef.current = live.filter(s => kept.has(sessionRowIdentity(s)))
  }
  return drawnRef.current
}

/** The group's heading, the list's own section heading in the Needs-you colour,
 *  its count read out in words. */
export function NeedsGroupHeader({ count }: { count: number }) {
  return (
    <ListSectionHeading tone="text-needs" aria-label={i18nT('pages.chatSidebar.needs_you_group_count', { count })} data-testid="needs-you-group-header">
      <Hand size={11} className="shrink-0" aria-hidden="true" />
      <span aria-hidden="true">{i18nT('pages.chatSidebar.needs_you_group')}</span>
      <span aria-hidden="true" className="rounded-full px-1.5 leading-[15px] bg-needs text-needs-fg tabular-nums">{fmtNumber(count)}</span>
      <span aria-hidden="true" className="ml-auto font-medium normal-case tracking-normal text-muted">{i18nT('pages.chatSidebar.needs_you_oldest_first')}</span>
    </ListSectionHeading>
  )
}

/** The heading over the rest of the list, with how many chats it holds. */
export function NeedsRestHeader({ count }: { count: number }) {
  return (
    <ListSectionHeading data-testid="needs-you-rest-header">
      <span>{i18nT('pages.chatSidebar.needs_you_everything_else')}</span>
      <span className="tabular-nums">{fmtNumber(count)}</span>
    </ListSectionHeading>
  )
}

/** The group's renderer for one shell render: its heading, its rows, and the
 *  heading of what follows. Returns null when nothing waits. */
export function createNeedsGroup(shell: SidebarScope & LaneRows) {
  const { needsGroup, heldLane, isActiveRow, renderSessionRow } = shell
  /** Draw the group on top of a lane whose own rows number `laneCount`. */
  const renderNeedsGroup = (laneCount: number): React.ReactNode => {
    const { rows: groupRows, headed, hiddenByFilter } = needsGroup
    if (groupRows.length === 0) return null
    const { rows, navScope, container } = heldLane(groupRows, 'needs', 'needs')
    const rest = Math.max(0, laneCount - rows.length)
    return (
      <div data-testid="needs-you-group" className="flex flex-col">
        {headed && <NeedsGroupHeader count={rows.length} />}
        {rows.map((s, i) => renderSessionRow(s, 0, i < rows.length - 1 && !isActiveRow(s) && !isActiveRow(rows[i + 1]), 'needs', navScope, container))}
        {hiddenByFilter > 0 && (
          <div className="mt-2 px-3.5 pt-2.5 pb-0.5 border-t border-dashed border-border text-[11px] text-muted" data-testid="needs-you-hidden-note">
            {i18nT('pages.chatSidebar.needs_you_hidden_by_filter', { count: hiddenByFilter })}
          </div>
        )}
        {headed && rest > 0 && <NeedsRestHeader count={rest} />}
      </div>
    )
  }
  /** A lane's rows without the waiting ones, for the lanes that draw each chat once. */
  const withoutGroup = (list: Slot[]): Slot[] =>
    (needsGroup.ids.size === 0 ? list : list.filter(s => !needsGroup.ids.has(sessionRowIdentity(s))))
  return { renderNeedsGroup, withoutGroup }
}

export type NeedsGroupRender = ReturnType<typeof createNeedsGroup>
