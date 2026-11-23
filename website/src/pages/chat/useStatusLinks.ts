/**
 * The chat page's half of app status items (`contributes.statusItems`):
 *
 *  - `?status=<app>:<item>` asks the mounted status bar to open that item's
 *    card (`shell/topbar/statusCardRequest.ts`). The URL sync drops the
 *    parameter on its next write, so it opens the card once.
 *  - A status go (`shell/topbar/statusGo.ts`) that switched to this chat hands
 *    its tab request over once the chat is on screen, and ends when the operator
 *    leaves that chat any other way.
 *
 * @module pages/chat/useStatusLinks
 */
import { useEffect, useRef } from 'react'

import { sendLinkPreviewNav } from '../../linkPreview/linkPreviewNav'
import { requestStatusCard } from '../../shell/topbar/statusCardRequest'
import { getActiveStatusGo, parseStatusCardParam, setActiveStatusGo, useActiveStatusGo } from '../../shell/topbar/statusGo'

/** Open `tab` of `appName` in chat `slot` with `arg`; true when the panel took it. */
export type OpenAppPanelTab = (slot: string, appName: string, tab: string, arg: unknown) => boolean

/**
 * The same hand-off a link-preview card's `openPanelTab` uses: the chat page
 * takes it only for the chat on screen and a tab `appName` declares, keeps `arg`
 * for `usePanelTabRequest()`, and reveals the tab.
 */
export const openStatusPanelTab: OpenAppPanelTab = (slot, appName, tab, arg) =>
  sendLinkPreviewNav({ kind: 'openPanelTab', slot, appName, tab, payload: arg })

/** How often, and for how long, a landed go retries a tab request the panel
 *  could not take yet (its tab list is still loading). */
const HANDOFF_RETRY_MS = 200
const HANDOFF_GIVE_UP_MS = 5_000

export function useStatusLinks({ statusParam, activeSlot, openPanelTab }: {
  /** The URL's `status` parameter. */
  statusParam: string | null
  activeSlot: string | null
  openPanelTab: OpenAppPanelTab
}): void {
  useEffect(() => {
    const key = parseStatusCardParam(statusParam)
    if (key) requestStatusCard(key)
  }, [statusParam])

  const go = useActiveStatusGo()
  const openRef = useRef(openPanelTab)
  openRef.current = openPanelTab
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
    const hand = (): boolean => {
      // A newer go replaced this one: it owns its own hand-off.
      if (getActiveStatusGo() !== go) return true
      if (Date.now() - started > HANDOFF_GIVE_UP_MS) {
        setActiveStatusGo({ ...go, pending: null })
        return true
      }
      if (!openRef.current(go.to, go.appName, pending.tab, pending.arg)) return false
      setActiveStatusGo({ ...go, pending: null })
      return true
    }
    if (hand()) return
    const t = setInterval(() => { if (hand()) clearInterval(t) }, HANDOFF_RETRY_MS)
    return () => clearInterval(t)
  }, [go, activeSlot])
}
