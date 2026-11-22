/**
 * A link-preview card opening its own app's side-panel tab
 * (`card.openPanelTab(tab, payload)`, `linkPreview/linkPreviewProtocol.ts`).
 *
 * Taken only for the chat on screen, and only for a tab the card's own app
 * declares (`contributes.panelTabs[]`): the payload is kept for that chat and tab
 * (`app-sdk/panelTabRequest.ts`), then the tab is revealed.
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
    if (req.kind !== 'openPanelTab' || !activeSlotRef.current || req.slot !== activeSlotRef.current) return false
    const d = latest.current.descriptors.find(x => x.appName === req.appName && x.tabId === req.tab)
    if (!d) return false
    putPanelTabRequest(req.slot, d.kind, req.payload)
    latest.current.reveal(d)
    return true
  }), [activeSlotRef])
}
