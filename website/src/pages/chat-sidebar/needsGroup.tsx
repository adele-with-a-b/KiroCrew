/** Where a chat that waits on the person (`needsYouOf`) is drawn: exactly once, and
 *  with no second mark for the same wait. This module is that one rule; every lane
 *  reads it.
 *
 *  - In its own place, drawn loud: its folder, the unfiled rows, the flat or the
 *    Conductor lane. Nothing moves to the top and no folder moves.
 *  - Under a COLLAPSED folder, the outermost collapsed one that holds it: the folder
 *    shows only its waiting rows below its header, then "N more in <folder>". Its
 *    copy in the closed body is not drawn, and the folder's unread dot does not
 *    count it.
 *  - While the "Needs you" filter narrows the list, in the list on top, oldest wait
 *    first, each row naming its folder on its agent line, and nowhere else: no
 *    folder is drawn around it.
 *
 *  The filter chip carries the wait count (`NeedsYouOffChip`), so the funnel's badge
 *  does not. */
import { useMemo, useRef } from 'react'
import { Hand } from 'lucide-react'
import type { ChatFolder, NeedsYou } from '../../types'
import { oldestWaitFirst } from '../../lib/needsYou'
import { fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import type { Slot } from './types'
import { isPeerRow, sessionRowIdentity } from './rowIdentity'
import type { LaneRows } from './laneRows'
import type { SidebarScope } from './scope'

/** The rule's answer for one shell render. */
export interface NeedsGroup {
  /** The oldest-first list the "Needs you" filter shows. Empty while the filter does
   *  not narrow, while a search ranks the list (the ranking is the answer) and on
   *  the board. */
  rows: Slot[]
  /** Chats the "Needs you" filter keeps off the list, or 0 while it does not narrow. */
  hiddenByFilter: number
  /** The waiting rows each collapsed folder shows under its header, by folder id. */
  underCollapsed: ReadonlyMap<string, Slot[]>
  /** Identities drawn by the list on top or under a collapsed folder, so their
   *  usual place leaves them out. */
  drawnElsewhere: ReadonlySet<string>
}

const NO_ROWS: Slot[] = []

/**
 * Holds what joins a drawn set while the pointer is over the list or a drag runs: a
 * row that STARTS waiting then does not join yet, so nothing moves under the pointer
 * (the hover hold keeps a row already drawn still); it joins when the pointer leaves.
 * Only the wait is held: a row that was already waiting joins at once, so collapsing
 * a folder (the pointer is on its header) shows its waiting rows under it right away.
 * A row that stops waiting leaves at once, because nothing may show a chat the rule
 * says is not waiting.
 */
function useJoinHold(live: Slot[], frozen: boolean, waitingNow: ReadonlySet<string>): Slot[] {
  const waitingRef = useRef(waitingNow)
  if (!frozen) waitingRef.current = waitingNow
  const before = waitingRef.current
  return useMemo(() => (frozen ? live.filter(s => before.has(sessionRowIdentity(s))) : live), [live, frozen, before])
}

/** The outermost collapsed folder on `folderId`'s chain to the root, itself
 *  included, or null when every folder on it is open. */
export function outermostCollapsed(folderId: string, byId: ReadonlyMap<string, ChatFolder>): string | null {
  let found: string | null = null
  const seen = new Set<string>()
  for (let f = byId.get(folderId); f && !seen.has(f.id); f = f.parent_id ? byId.get(f.parent_id) : undefined) {
    seen.add(f.id)
    if (f.collapsed) found = f.id
  }
  return found
}

/** The rule for this render. `listRows` are the lane's rows in lane order,
 *  `folderOf` names a local row's folder; `listOnTop` is true while the "Needs you"
 *  filter narrows a list lane, `folderTree` while the folder tree is the lane shown. */
export function useNeedsGroup({ listRows, needsOf, folders, folderOf, listOnTop, folderTree, frozen, hiddenByFilter }: {
  listRows: Slot[]
  needsOf: (slot: Slot) => NeedsYou | null
  folders: readonly ChatFolder[]
  folderOf: Readonly<Record<string, string>>
  listOnTop: boolean
  folderTree: boolean
  frozen: boolean
  hiddenByFilter: number
}): NeedsGroup {
  const liveList = useMemo(
    () => (listOnTop ? oldestWaitFirst(listRows.filter(s => needsOf(s) !== null), needsOf) : NO_ROWS),
    [listRows, needsOf, listOnTop],
  )
  const liveUnder = useMemo(() => {
    if (listOnTop || !folderTree) return NO_ROWS
    const byId = new Map(folders.map(f => [f.id, f] as const))
    return listRows.filter(s => {
      const folderId = isPeerRow(s) ? undefined : folderOf[s.key]
      return !!folderId && needsOf(s) !== null && outermostCollapsed(folderId, byId) !== null
    })
  }, [listRows, needsOf, folders, folderOf, listOnTop, folderTree])
  const waitingNow = useMemo(
    () => new Set(listRows.filter(s => needsOf(s) !== null).map(sessionRowIdentity)),
    [listRows, needsOf],
  )
  const rows = useJoinHold(liveList, frozen, waitingNow)
  const under = useJoinHold(liveUnder, frozen, waitingNow)
  return useMemo(() => {
    const byId = new Map(folders.map(f => [f.id, f] as const))
    const underCollapsed = new Map<string, Slot[]>()
    for (const s of under) {
      const holder = outermostCollapsed(folderOf[s.key], byId)
      if (holder === null) continue
      const list = underCollapsed.get(holder)
      if (list) list.push(s)
      else underCollapsed.set(holder, [s])
    }
    const drawnElsewhere = new Set([...rows, ...under].map(sessionRowIdentity))
    return { rows, hiddenByFilter, underCollapsed, drawnElsewhere }
  }, [rows, under, folders, folderOf, hiddenByFilter])
}

/** The "Needs you" chip while the filter is off: how many chats wait, one click
 *  from the oldest-first list. Absent while nothing waits; the filter's own chip
 *  takes its place while the filter is on. */
export function NeedsYouOffChip({ count, onShow }: { count: number; onShow: () => void }) {
  if (count === 0) return null
  return (
    <div className="px-3 pb-1">
      <button
        type="button"
        data-testid="needs-you-off-chip"
        onClick={onShow}
        aria-label={i18nT('pages.chatSidebar.needs_you_group_count', { count })}
        title={i18nT('pages.chatSidebar.filter_needs_you_description')}
        className="inline-flex items-center gap-1 pl-2 pr-1.5 py-0.5 rounded-full text-[11px] font-semibold cursor-pointer transition-colors bg-transparent border border-border text-muted hover:text-text"
      >
        <Hand size={11} className="shrink-0" aria-hidden="true" />
        <span aria-hidden="true">{i18nT('pages.chatSidebar.filter_needs_you')}</span>
        <span aria-hidden="true" className="rounded-full px-1.5 leading-[15px] text-[10px] font-bold bg-needs text-needs-fg tabular-nums">{fmtNumber(count)}</span>
      </button>
    </div>
  )
}

/** The list on top for one shell render. */
export function createNeedsGroup(shell: SidebarScope & LaneRows) {
  const { needsGroup, heldLane, isActiveRow, renderSessionRow, folders, slotFolders } = shell
  const byId = new Map(folders.map(f => [f.id, f] as const))
  /** The oldest-first list, drawn while the "Needs you" filter narrows the lane;
   *  null otherwise. */
  const renderNeedsGroup = (): React.ReactNode => {
    const { rows: groupRows, hiddenByFilter } = needsGroup
    if (groupRows.length === 0) return null
    const { rows, navScope, container } = heldLane(groupRows, 'needs', 'needs')
    return (
      <div data-testid="needs-you-group" className="flex flex-col">
        {rows.map((s, i) => renderSessionRow(s, 0, i < rows.length - 1 && !isActiveRow(s) && !isActiveRow(rows[i + 1]), 'needs', navScope, container,
          undefined, undefined, undefined, isPeerRow(s) ? undefined : byId.get(slotFolders[s.key])))}
        {hiddenByFilter > 0 && (
          <div className="mt-2 px-3.5 pt-2.5 pb-0.5 border-t border-dashed border-border text-[11px] text-muted" data-testid="needs-you-hidden-note">
            {i18nT('pages.chatSidebar.needs_you_hidden_by_filter', { count: hiddenByFilter })}
          </div>
        )}
      </div>
    )
  }
  /** A lane's rows without the ones the rule draws elsewhere.
   *  Whether a waiting code-channel worker also hides under its conductor is the
   *  fold's own call (`foldCodeChannelCrews`, handed the whole membership). */
  const withoutGroup = (list: Slot[]): Slot[] =>
    needsGroup.drawnElsewhere.size === 0 ? list : list.filter(s => !needsGroup.drawnElsewhere.has(sessionRowIdentity(s)))
  return { renderNeedsGroup, withoutGroup }
}

export type NeedsGroupRender = ReturnType<typeof createNeedsGroup>
