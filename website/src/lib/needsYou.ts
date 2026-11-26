/** "Needs you": the ONE rule that says whether a chat waits on the person, and
 *  for what. The loud session row, the "Needs you" group, the filter chip and its
 *  count, the filter button's badge and the tab title all read `needsYouOf`, so
 *  none of them can call a chat waiting that another one calls settled.
 *
 *  The gateway's `needs_you` summary is the answer whenever the row carries the
 *  field, null included. A row from a gateway that does not build it yet has no
 *  such field, and the answer then comes from the rule the needs-me hover card
 *  and the Command Center already read (`slotNeedsMe`, `offeredReplyOptions`),
 *  plus a crew member's tool approval rolled onto its Crew Mode root
 *  (`crewWaitingApprovals`): one ask each. */
import type { ChatSlot, NeedsYou, NeedsYouKind } from '../types'
import { slotNeedsMe } from '../pages/chat-sidebar/needsMeCard'
import { offeredReplyOptions } from '../pages/chat/command-center/model'
import { fmtMinutes } from './crewWork'

export const NEEDS_YOU_KINDS: readonly NeedsYouKind[] = ['approval', 'question', 'options', 'labelled']

/** The longest first words a row shows, as the gateway's contract caps them. */
export const NEEDS_YOU_FIRST_WORDS_MAX = 80

/** The row fields the rule reads. */
export type NeedsYouRow = Pick<ChatSlot, 'key' | 'running' | 'pending_approval' | 'needs_input' | 'has_options' | 'options' | 'options_ts' | 'interrupted' | 'queue_depth' | 'last_turn_ts' | 'last_ts' | 'created'> & {
  needs_you?: NeedsYou | null
  crew_mode_root?: boolean
}

function epochSeconds(iso: string | undefined): number {
  if (!iso) return 0
  const ms = Date.parse(iso)
  return Number.isFinite(ms) ? ms / 1000 : 0
}

/** The gateway's summary, checked: a malformed or empty one waits on nobody. */
function checkedSummary(value: NeedsYou | null): NeedsYou | null {
  if (!value || typeof value !== 'object') return null
  const count = Number(value.count)
  if (!Number.isFinite(count) || count < 1) return null
  const words = typeof value.first_words === 'string' ? Array.from(value.first_words.trim()) : []
  return {
    count: Math.floor(count),
    oldest_ts: Number.isFinite(Number(value.oldest_ts)) ? Number(value.oldest_ts) : 0,
    first_words: words.slice(0, NEEDS_YOU_FIRST_WORDS_MAX).join(''),
    row_id: typeof value.row_id === 'string' && value.row_id ? value.row_id : null,
    kinds: Array.isArray(value.kinds) ? value.kinds.filter(k => NEEDS_YOU_KINDS.includes(k)) : [],
  }
}

/** What `row` waits on the person for, or null when it does not wait on them.
 *  `crewApprovals` is how many of its crew members wait on a tool approval
 *  (`crewWaitingApprovals`); it counts on a Crew Mode root only, whose members'
 *  own rows the person's list leaves out. */
export function needsYouOf(row: NeedsYouRow | null | undefined, crewApprovals = 0): NeedsYou | null {
  if (!row) return null
  if (row.needs_you !== undefined) return checkedSummary(row.needs_you)
  const crewApproval = !!row.crew_mode_root && crewApprovals > 0
  if (!slotNeedsMe(row, false) && !crewApproval) return null
  const kinds: NeedsYouKind[] = []
  if (row.pending_approval || crewApproval) kinds.push('approval')
  if (row.needs_input) kinds.push('question')
  if (offeredReplyOptions(row).length > 0) kinds.push('options')
  return {
    count: 1,
    oldest_ts: (kinds[0] === 'options' && epochSeconds(row.options_ts)) || epochSeconds(row.last_turn_ts || row.last_ts || row.created),
    first_words: '',
    row_id: null,
    kinds,
  }
}

/** Waiting rows, oldest wait first; rows with no known start go last, in list order. */
export function oldestWaitFirst<T>(rows: readonly T[], needsOf: (row: T) => NeedsYou | null): T[] {
  const at = (row: T) => needsOf(row)?.oldest_ts || Number.POSITIVE_INFINITY
  return rows
    .map((row, i) => ({ row, i, ts: at(row) }))
    .sort((a, b) => (a.ts === b.ts ? a.i - b.i : a.ts - b.ts))
    .map(x => x.row)
}

/** How long the oldest ask has waited, as the crew row says how long work stalled
 *  (`fmtMinutes`): `34m`, `2h 5m`. Empty when the start is not known. */
export function needsYouAge(oldestTs: number, nowMs: number): string {
  if (!oldestTs) return ''
  return fmtMinutes((nowMs / 1000 - oldestTs) / 60)
}
