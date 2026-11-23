/**
 * An open of an app's own side-panel tab (target `panelTab`): a link-preview
 * card's `card.openPanelTab(tab, payload)` (`linkPreview/linkPreviewProtocol.ts`)
 * or a status go's `panelTab` (`shell/topbar/statusGo.ts`).
 *
 * Taken only for the chat on screen, and only for a tab the app itself declares
 * (`contributes.panelTabs[]`; any other is refused `not_declared`): the payload
 * is kept for that chat and tab (`app-sdk/panelTabRequest.ts`), then the tab is
 * revealed.
 *
 * @module pages/chat/linkPreviewPanelTabs
 */
import { useEffect, useRef, type MutableRefObject } from 'react'
import type { PanelTabDescriptor } from '../../hooks/panelTabRegistry'
import { onLinkPreviewNav } from '../../linkPreview/linkPreviewNav'
import { putPanelTabRequest } from '../../app-sdk/panelTabRequest'

export function useLinkPreviewPanelTabs(
  activeSlotRef: MutableRefObject<string | null>,
  descriptors: readonly PanelTabDescriptor[],
  reveal: (d: PanelTabDescriptor) => void,
): void {
  const latest = useRef({ descriptors, reveal })
  latest.current = { descriptors, reveal }
  useEffect(() => onLinkPreviewNav((req) => {
    if (req.kind !== 'open' || req.target.kind !== 'panelTab') return false
    if (!activeSlotRef.current || req.slot !== activeSlotRef.current) return false
    const tabId = req.target.id
    const d = latest.current.descriptors.find(x => x.appName === req.appName && x.tabId === tabId)
    if (!d) return 'not_declared'
    putPanelTabRequest(req.slot, d.kind, req.payload)
    latest.current.reveal(d)
    return true
  }), [activeSlotRef])
}
