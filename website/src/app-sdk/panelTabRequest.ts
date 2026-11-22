/**
 * What an app's side-panel tab was opened with.
 *
 * A link-preview card of the same app can open that app's own tab
 * (`card.openPanelTab(tab, payload)`, `linkPreview/linkPreviewProtocol.ts`). The
 * host keeps the latest payload per chat and tab here, and the tab body reads it
 * with `usePanelTabRequest()`. The body's chat and tab come from the host
 * (`PanelTabRequestScope`, set by the side panel around the tab's `AppHost`), so a
 * tab reads only what was sent to itself, in its own chat.
 *
 * @module app-sdk/panelTabRequest
 */
import { createContext, createElement, useCallback, useContext, useSyncExternalStore, type ReactNode } from 'react'

/** One open request: `seq` grows with every open, so the same payload sent twice still reads as new. */
export interface PanelTabRequest {
  seq: number
  payload: unknown
}

interface Where {
  slot: string
  kind: string
}

const requests = new Map<string, PanelTabRequest>()
const listeners = new Set<() => void>()
let seq = 0

const keyOf = (slot: string, kind: string) => `${slot}\u0000${kind}`

/** Host only: forget what tab `kind` of chat `slot` was opened with (the tab closed),
 *  so opening it again from the side-panel menu reads null, not the old payload.
 *  Without `kind`, every app tab of that chat. */
export function clearPanelTabRequests(slot: string, kind?: string): void {
  let changed = false
  for (const key of [...requests.keys()]) {
    if (kind ? key === keyOf(slot, kind) : key.startsWith(keyOf(slot, ''))) changed = requests.delete(key) || changed
  }
  if (changed) for (const fn of [...listeners]) fn()
}

/** Host only: record `payload` for the tab `kind` (`app:<app>:<id>`) of chat `slot`. */
export function putPanelTabRequest(slot: string, kind: string, payload: unknown): void {
  seq += 1
  requests.set(keyOf(slot, kind), { seq, payload })
  for (const fn of [...listeners]) fn()
}

/** Tests only: what tab `kind` of chat `slot` holds now. */
export function peekPanelTabRequestForTest(slot: string, kind: string): PanelTabRequest | null {
  return requests.get(keyOf(slot, kind)) ?? null
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

const WhereContext = createContext<Where | null>(null)

/** Host only: the chat and tab the app body below is mounted for. */
export function PanelTabRequestScope({ slot, kind, children }: Where & { children: ReactNode }) {
  return createElement(WhereContext.Provider, { value: { slot, kind } }, children)
}

/** The latest request this side-panel tab was opened with, or null: none since the
 *  tab was last closed, or this component is not inside an app's side-panel tab. The
 *  request stays while the tab is open, so a body mounted again (the chat left and
 *  come back to) reads the same one; act on a `seq` you have not handled. */
export function usePanelTabRequest(): PanelTabRequest | null {
  const where = useContext(WhereContext)
  const read = useCallback(() => (where ? requests.get(keyOf(where.slot, where.kind)) ?? null : null), [where])
  return useSyncExternalStore(subscribe, read, read)
}
