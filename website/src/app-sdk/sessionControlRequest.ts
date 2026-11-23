/**
 * What an app's session control (its chip under the message box) was opened with.
 *
 * The same request shape a side-panel tab gets (`panelTabRequest.ts`), for the
 * other target an app may open: a link-preview card's
 * `card.openSessionControl(id, payload)`, or a status go whose target names
 * `sessionControl`. The host keeps the payload per chat and control here while
 * the popover is open, and the control reads it with `useSessionControlRequest()`.
 * The chat and control come from the host (`SessionControlRequestScope`, set by
 * `SessionControlHost`), so a control reads only what was sent to itself, in its
 * own chat.
 *
 * @module app-sdk/sessionControlRequest
 */
import { createContext, createElement, useCallback, useContext, useSyncExternalStore, type ReactNode } from 'react'
import type { PanelTabRequest } from './panelTabRequest'

/** Where an open came from: a click on the app's own link-preview card, or a
 *  status row or go. One shape for every source, so a later one (a shortcut key
 *  pressed again while the popover is open) is a new `via`, not a new request. */
export type SessionControlOpenVia = 'link' | 'status'

/** One open request: `seq` grows with every open, so the same payload sent twice still reads as new. */
export interface SessionControlRequest extends PanelTabRequest {
  via: SessionControlOpenVia
}

interface Where {
  /** `dashboard:<slot>`, the chat the control is for. */
  sessionKey: string
  /** `<appName>:<id>`, the control's composite key. */
  control: string
}

const requests = new Map<string, SessionControlRequest>()
const listeners = new Set<() => void>()
let seq = 0

const keyOf = (sessionKey: string, control: string) => `${sessionKey}\u0000${control}`
const emit = () => { for (const fn of [...listeners]) fn() }

/** Host only: record `payload` for control `control` (`<app>:<id>`) of chat `sessionKey`. */
export function putSessionControlRequest(sessionKey: string, control: string, payload: unknown, via: SessionControlOpenVia): void {
  seq += 1
  requests.set(keyOf(sessionKey, control), { seq, payload, via })
  emit()
}

/** Host only: the `seq` control `control` of chat `sessionKey` holds now, 0 for none. */
export function sessionControlRequestSeq(sessionKey: string, control: string): number {
  return requests.get(keyOf(sessionKey, control))?.seq ?? 0
}

/** Host only: the popover closed, so opening the chip by hand reads null, not the old payload. */
export function clearSessionControlRequest(sessionKey: string, control: string): void {
  if (requests.delete(keyOf(sessionKey, control))) emit()
}

/** Tests only: what control `control` of chat `sessionKey` holds now. */
export function peekSessionControlRequestForTest(sessionKey: string, control: string): SessionControlRequest | null {
  return requests.get(keyOf(sessionKey, control)) ?? null
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

const WhereContext = createContext<Where | null>(null)

/** Host only: the chat and control the app module below is mounted for. */
export function SessionControlRequestScope({ sessionKey, control, children }: Where & { children: ReactNode }) {
  return createElement(WhereContext.Provider, { value: { sessionKey, control } }, children)
}

/** The request this session control's popover was opened with, or null: opened by
 *  hand, or this component is not inside a session control. A request sent before
 *  the module mounted is read on mount, and it stays while the popover is open (a
 *  module mounted again reads the same one); act on a `seq` you have not handled.
 *  It is dropped when the popover closes. */
export function useSessionControlRequest(): SessionControlRequest | null {
  const where = useContext(WhereContext)
  const read = useCallback(() => (where ? requests.get(keyOf(where.sessionKey, where.control)) ?? null : null), [where])
  return useSyncExternalStore(subscribe, read, read)
}
