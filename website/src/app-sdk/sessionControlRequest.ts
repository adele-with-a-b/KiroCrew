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
 * The same scope gives a control `useSessionControlJump()`: scroll its own chat
 * to one of its messages, through the route a card's `jumpTo` takes.
 *
 * @module app-sdk/sessionControlRequest
 */
import { createContext, createElement, useCallback, useContext, useMemo, useSyncExternalStore, type ReactNode } from 'react'
import type { PanelTabRequest } from './panelTabRequest'
import { sendAppNav } from '../linkPreview/linkPreviewNav'
import { readMessageRef } from '../linkPreview/linkPreviewProtocol'

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

/** How a session control's jump ended. `not_found`: this chat has no such
 *  message (a message of another chat reads this; so does a `ts` or `id` that is
 *  not a string of 1 to 128 characters). `load_failed`: a page of older history
 *  failed to load. `not_on_screen`: the control's chat is not the one drawn (Split
 *  View, or it was left). `replaced`: a newer jump took its place first. */
export type SessionControlJumpResult =
  | { ok: true }
  | { ok: false; reason: 'not_found' | 'load_failed' | 'not_on_screen' | 'replaced' }

/** Scroll the control's own chat to one of its messages, `{ ts, id? }` (the
 *  shape a card's `link.message` and `card.jumpTo` use), and light it, with the
 *  Back pill to where the view was; the popover stays open. A message older
 *  than the loaded window is paged in first, so the answer comes once the
 *  message is on screen or the chat's history is read to its start. Null outside
 *  a session control. */
export function useSessionControlJump(): ((message: { ts: string; id?: string }) => Promise<SessionControlJumpResult>) | null {
  const where = useContext(WhereContext)
  return useMemo(() => {
    if (!where) return null
    const slot = where.sessionKey.startsWith('dashboard:') ? where.sessionKey.slice('dashboard:'.length) : ''
    return (message: { ts: string; id?: string }) => new Promise<SessionControlJumpResult>((resolve) => {
      const m = message as { ts?: unknown; id?: unknown } | null | undefined
      const target = m && typeof m === 'object' ? readMessageRef(m.ts, m.id) : null
      // An `id` that is given but unreadable must not fall back to `ts`, which can
      // name a same-tick twin.
      if (!target || (m?.id !== undefined && target.id === undefined)) { resolve({ ok: false, reason: 'not_found' }); return }
      if (!slot) { resolve({ ok: false, reason: 'not_on_screen' }); return }
      const taken = sendAppNav({
        kind: 'jump', slot, target, from: 'view',
        onDone: (o) => resolve(o === true ? { ok: true } : { ok: false, reason: o }),
      })
      if (taken !== true) resolve({ ok: false, reason: 'not_on_screen' })
    })
  }, [where])
}
