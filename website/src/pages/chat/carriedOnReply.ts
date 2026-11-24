/**
 * A reply a gateway restart cut while its chat keeper kept the turn running.
 *
 * The dying gateway saves the text streamed so far as an assistant row marked
 * `meta.carries_on` (backend `REPLY_CARRIES_ON_META_KEY`). The next gateway
 * adopts the turn, and the rest of the same reply arrives as the agent-started
 * turn's first text row: `streaming` while it streams, then an `assistant` row
 * with `meta.origin.kind === 'agent'`. The transcript keeps both rows (rewind,
 * redaction and native message ids stay per row); the display draws them as
 * ONE bubble that carries on, under the cut row's identity, so the bubble the
 * reader was watching keeps growing in place, live and after a reload.
 *
 * Only a marked row joins, and only with the row that continues it: a reply
 * that simply ended, or an agent-started turn after one (a background command
 * finishing), keeps its own bubble. Design:
 * docs/system-specs/modules/runtime-ownership.md, "Chats that outlive the gateway".
 */
import type { ChatMessage } from '../../types'
import { isSystemNoticeRow } from './CompactionCard'
// groupDisplayItems imports this module too; the predicate is only called, never
// read while either module loads, so the cycle is safe.
import { isReasoningRole } from './groupDisplayItems'

/** The backend's `REPLY_CARRIES_ON_META_KEY`. */
export const CARRIES_ON_META_KEY = 'carries_on'

const meta = (m: ChatMessage): Record<string, unknown> | undefined =>
  m.meta as Record<string, unknown> | undefined

/** Whether `m` is a reply cut by a gateway restart whose turn a keeper kept running. */
export const carriesOn = (m: ChatMessage): boolean =>
  m.role === 'assistant' && meta(m)?.[CARRIES_ON_META_KEY] === true && !isSystemNoticeRow(m)

const isAgentStartedReply = (m: ChatMessage): boolean => {
  const origin = meta(m)?.origin as { kind?: unknown } | undefined
  return m.role === 'assistant' && origin?.kind === 'agent' && !isSystemNoticeRow(m)
}

/** Rows that may sit between the cut reply and its rest without ending the reply:
 *  a gateway notice the restart wrote, a system notice, a hidden approval row, an
 *  empty reasoning placeholder. */
const passesOver = (m: ChatMessage): boolean =>
  m.role === 'notice' ||
  m.role === 'permission' ||
  (isReasoningRole(m) && !m.content) ||
  isSystemNoticeRow(m)

/** Index of the row that carries on the cut reply at `idx`, or -1. */
export function continuationIdx(messages: ChatMessage[], idx: number): number {
  const leader = messages[idx]
  if (!leader || !carriesOn(leader)) return -1
  for (let i = idx + 1; i < messages.length; i++) {
    const m = messages[i]
    if (m.role === 'streaming' || isAgentStartedReply(m)) return i
    if (!passesOver(m)) return -1
  }
  return -1
}

/** Per-row record lists the server computes from each row's own text
 *  (`security/blocked_links.py`, the credential redactor), which the joined bubble
 *  carries from both rows. */
const PER_ROW_RECORD_KEYS = ['link_blocks', 'redactions'] as const

const asList = (value: unknown): unknown[] => (Array.isArray(value) ? value : [])

/** `follower`'s identity keys that must not override the cut row's own. */
const IDENTITY_KEYS = ['clientTs', 'mid', CARRIES_ON_META_KEY] as const

// One joined object per (cut row, rest) pair, so a re-group that sees the same
// two rows hands back the same object and the memoized row bails out.
const joinedCache = new WeakMap<ChatMessage, { follower: ChatMessage; joined: ChatMessage }>()

/**
 * The bubble drawn for a cut reply and its rest: the rest's role, content tail
 * and meta (its footer, its streaming state), under the cut row's identity
 * (`ts`, `meta.clientTs`, `meta.mid`), so the row key stays the one on screen.
 */
export function joinCarriedOn(leader: ChatMessage, follower: ChatMessage): ChatMessage {
  const hit = joinedCache.get(leader)
  if (hit && hit.follower === follower) return hit.joined
  const merged: Record<string, unknown> = { ...(meta(follower) ?? {}) }
  for (const key of IDENTITY_KEYS) delete merged[key]
  const own = meta(leader) ?? {}
  for (const key of ['clientTs', 'mid'] as const) if (own[key] !== undefined) merged[key] = own[key]
  // The server judges each row's text alone: a link it blocked or a credential it
  // removed in the cut part must keep its chip and card in the joined bubble.
  for (const key of PER_ROW_RECORD_KEYS) {
    const records = [...asList(own[key]), ...asList(meta(follower)?.[key])]
    if (records.length) merged[key] = records
    else delete merged[key]
  }
  const content = leader.content + follower.content
  const joined: ChatMessage = { ...follower, content, rawText: content, meta: merged }
  if (leader.ts !== undefined) joined.ts = leader.ts
  else delete joined.ts
  joinedCache.set(leader, { follower, joined })
  return joined
}

/**
 * The single item a transcript grouping draws for row `idx`: the cut reply joined
 * with its rest, whose index goes into `joinedInto` so the grouping skips it, or
 * the row itself. A joined item carries the REST's index, the reply's last row, so
 * the controls a row index decides (Regenerate and variants on the latest reply,
 * the footer, fork and rewind targets) act on the whole reply; its React key is
 * the message identity, which stays the cut row's. The one call every grouping
 * makes (`groupDisplayItems`, app-sdk `ChatMessageList`).
 */
export function drawnRow(
  messages: ChatMessage[],
  idx: number,
  joinedInto: Set<number>,
): { kind: 'single'; msg: ChatMessage; idx: number } {
  const rest = continuationIdx(messages, idx)
  if (rest < 0) return { kind: 'single', msg: messages[idx], idx }
  joinedInto.add(rest)
  return { kind: 'single', msg: joinCarriedOn(messages[idx], messages[rest]), idx: rest }
}
