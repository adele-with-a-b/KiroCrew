/**
 * The chat page's half of app status items (`contributes.statusItems`):
 *
 *  - `?status=<app>:<item>` asks the mounted status bar to open that item's
 *    card (`lib/statusCardRequest.ts`). The URL sync drops the
 *    parameter on its next write, so it opens the card once.
 *  - A status go (`lib/statusGo.ts`) that switched to this chat hands
 *    its open request (a side-panel tab or a session control) over once the chat
 *    is on screen, and ends when the operator leaves that chat any other way.
 *
 * @module pages/chat/useStatusLinks
 */
import { useEffect, useRef } from 'react'

import { sendAppNav, type AppOpenTarget, type OpenRefusal } from '../../linkPreview/linkPreviewNav'
import { requestStatusCard } from '../../lib/statusCardRequest'
import { getActiveStatusGo, parseStatusCardParam, setActiveStatusGo, useActiveStatusGo } from '../../lib/statusGo'

/** Open `target` of `appName` in chat `slot` with `arg`: true when the chat took it, else why not. */
export type OpenAppTarget = (slot: string, appName: string, target: AppOpenTarget, arg: unknown) => true | OpenRefusal

/**
 * The same hand-off a link-preview card's open uses: the chat page takes it
 * only for the chat on screen and a tab or control `appName` declares, keeps
 * `arg` for `usePanelTabRequest()` / `useSessionControlRequest()`, and opens it.
 */
export const openStatusTarget: OpenAppTarget = (slot, appName, target, arg) =>
  sendAppNav({ kind: 'open', slot, appName, target, payload: arg, via: 'status' })

/** How often, and for how long, a landed go retries an open the chat could
 *  not take yet (its tab or app list is still loading). */
const HANDOFF_RETRY_MS = 200
const HANDOFF_GIVE_UP_MS = 5_000

export function useStatusLinks({ statusParam, activeSlot, open }: {
  /** The URL's `status` parameter. */
  statusParam: string | null
  activeSlot: string | null
  open: OpenAppTarget
}): void {
  useEffect(() => {
    const key = parseStatusCardParam(statusParam)
    if (key) requestStatusCard(key)
  }, [statusParam])

  const go = useActiveStatusGo()
  const openRef = useRef(open)
  openRef.current = open
  useEffect(() => {
    // A closed chat still reopening has not landed yet.
    if (!go || !activeSlot || go.opening) return
    // `switchSlot` makes the target active as it is dispatched, so another
    // chat on screen means the operator left it (or the switch was refused,
    // the chat gone): the go is over.
    if (activeSlot !== go.to) {
      setActiveStatusGo(null)
      return
    }
    const pending = go.pending
    if (!pending) return
    const started = Date.now()
    let refusal: OpenRefusal | null = null
    const hand = (): boolean => {
      // A newer go replaced this one: it owns its own hand-off.
      if (getActiveStatusGo() !== go) return true
      if (Date.now() - started > HANDOFF_GIVE_UP_MS) {
        // Refused for good: the chat stays open alone, and the reason is named
        // where the app's author looks (the route has no way to be told).
        // eslint-disable-next-line no-console -- an app's refused open is its author's to see
        console.warn(`[status go] ${go.appName} could not open ${pending.target.kind} "${pending.target.id}": ${refusal ?? 'not_on_screen'}`)
        setActiveStatusGo({ ...go, pending: null })
        return true
      }
      const r = openRef.current(go.to, go.appName, pending.target, pending.arg)
      if (r !== true) {
        refusal = r
        return false
      }
      setActiveStatusGo({ ...go, pending: null })
      return true
    }
    if (hand()) return
    const t = setInterval(() => { if (hand()) clearInterval(t) }, HANDOFF_RETRY_MS)
    return () => clearInterval(t)
  }, [go, activeSlot])
}
