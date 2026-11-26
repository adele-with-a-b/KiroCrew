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
 * to one of its messages, through the route a card's `jumpTo` takes; and
 * `useSessionControlGo()`: open its own box in another chat, on a click in it,
 * through the route a status item's go takes.
 *
 * @module app-sdk/sessionControlRequest
 */
import { createContext, createElement, useCallback, useContext, useMemo, useSyncExternalStore, type ReactNode } from 'react'
import type { PanelTabRequest } from './panelTabRequest'
import { sendAppNav } from '../linkPreview/linkPreviewNav'
import { readMessageRef } from '../linkPreview/linkPreviewProtocol'
import { useClickGrant } from '../components/appFileOpen'
import { isCrewLinkKey } from '../lib/crewDeepLink'
import { MAX_TARGET_ARG_BYTES, useStatusGo } from '../lib/statusGo'

/** Where an open came from: a click on the app's own link-preview card, a
 *  status row or go, one of the control's `keys` (its `arg` is the payload), or
 *  the control's own box in another chat (`useSessionControlGo`).
 *  One shape for every source, so a new one is a new `via`, not a new request. */
export type SessionControlOpenVia = 'link' | 'status' | 'key' | 'control'

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

/** What the scope gives the hooks below: where, and the box's click grant. */
interface Scope extends Where {
  /** True once per real click in the app's own module, within `CLICK_GRANT_MS` of it. */
  takeClick: () => boolean
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

const WhereContext = createContext<Scope | null>(null)

/** Host only: the chat and control the app module below is mounted for. It wraps
 *  the module (and nothing of the host's) in a `display: contents` box that
 *  records a real click for `useSessionControlGo`, as `AppFileOpenRoot` does for
 *  `useOpenFile`: the host's header and retry button never grant. */
export function SessionControlRequestScope({ sessionKey, control, children }: Where & { children: ReactNode }) {
  const { record, take } = useClickGrant()
  const value = useMemo(() => ({ sessionKey, control, takeClick: take }), [sessionKey, control, take])
  return createElement(
    WhereContext.Provider,
    { value },
    createElement('div', { 'data-session-control-scope': '', style: { display: 'contents' }, onClickCapture: record }, children),
  )
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
      // Read as the card's jump reads it: an `id` that is not a usable string (a
      // backend's `null` for a row with no mid) is left out, and `ts` names it.
      const target = m && typeof m === 'object' ? readMessageRef(m.ts, m.id) : null
      if (!target) { resolve({ ok: false, reason: 'not_found' }); return }
      if (!slot) { resolve({ ok: false, reason: 'not_on_screen' }); return }
      const taken = sendAppNav({
        kind: 'jump', slot, target, from: 'view',
        onDone: (o) => resolve(o === true ? { ok: true } : { ok: false, reason: o }),
      })
      if (taken !== true) resolve({ ok: false, reason: 'not_on_screen' })
    })
  }, [where])
}

/** How a go to the control's box in another chat started. `ok` means the switch
 *  began: the person may still keep an unsaved draft and stay. `no_click`: no real
 *  click in this box just before the call. `bad_chat`: `sid` is not a chat key.
 *  `not_json` / `too_large`: `payload` does not serialize, or is over 4 KB as JSON. */
export type SessionControlGoResult =
  | { ok: true }
  | { ok: false; reason: 'no_click' | 'bad_chat' | 'not_json' | 'too_large' }

const SESSION_KEY_PREFIX = 'dashboard:'

/** Open THIS control's box in chat `sid` (its slot key, or its session key
 *  `dashboard:<slot>`), handing it `payload` as a request with `via: 'control'`,
 *  the way a status item's row does: behind the unsaved-draft guard, reopening a
 *  closed chat, with `useStatusGoBack()` as the way back. Only on a real click in
 *  this box, one go per click. Null outside a session control. */
export function useSessionControlGo(): ((sid: string, payload?: unknown) => SessionControlGoResult) | null {
  const where = useContext(WhereContext)
  const go = useStatusGo()
  return useMemo(() => {
    // The host builds the key as `<appName>:<id>`, and an app name holds no `:`.
    const cut = where ? where.control.indexOf(':') : -1
    if (!where || cut <= 0) return null
    const appName = where.control.slice(0, cut)
    const id = where.control.slice(cut + 1)
    return (sid: string, payload?: unknown): SessionControlGoResult => {
      const key = typeof sid === 'string' && sid.startsWith(SESSION_KEY_PREFIX) ? sid.slice(SESSION_KEY_PREFIX.length) : sid
      if (!isCrewLinkKey(key)) return { ok: false, reason: 'bad_chat' }
      let arg: unknown
      if (payload !== undefined) {
        let json: string | undefined
        try {
          json = JSON.stringify(payload)
        } catch {
          return { ok: false, reason: 'not_json' }
        }
        if (json === undefined) return { ok: false, reason: 'not_json' }
        if (new TextEncoder().encode(json).length > MAX_TARGET_ARG_BYTES) return { ok: false, reason: 'too_large' }
        arg = JSON.parse(json) as unknown
      }
      // Last, so a refused call spends no click.
      if (!where.takeClick()) return { ok: false, reason: 'no_click' }
      go(appName, { sid: key, sessionControl: id, ...(arg !== undefined ? { arg } : {}) }, 'control')
      return { ok: true }
    }
  }, [where, go])
}
