/** Opening a waiting chat at its ask. The row's click opens the chat as always;
 *  when the gateway named the transcript row the oldest ask sits on, the chat's
 *  own message jump (`sendAppNav`, the one a link-preview card and the pinned
 *  prompt use) then scrolls to that row and marks it. With no row id nothing more
 *  happens. */
import { sendAppNav } from '../../linkPreview/linkPreviewNav'
import type { NeedsYou } from '../../types'

/** How many frames the jump is offered for: the chat page takes it once the
 *  opened chat is the one on screen, a few renders after the switch. */
export const LANDING_TRIES = 120

export function landOnAsk(slot: string, needs: NeedsYou | null, schedule: (fn: () => void) => void = fn => { requestAnimationFrame(fn) }): void {
  if (!needs?.row_id) return
  const target = { id: needs.row_id, ts: needs.oldest_ts ? new Date(needs.oldest_ts * 1000).toISOString() : '' }
  let tries = 0
  const attempt = () => {
    if (sendAppNav({ kind: 'jump', slot, target }) === true) return
    tries += 1
    if (tries < LANDING_TRIES) schedule(attempt)
  }
  schedule(attempt)
}
