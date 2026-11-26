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
 * through the route a status item's go takes; and `useInsertIntoComposer()`:
 * put text in its chat's message box from a docked box, through the composer's
 * own app-text write.
 *
 * @module app-sdk/sessionControlRequest
 */
import { createContext, createElement, useCallback, useContext, useMemo, useSyncExternalStore, type KeyboardEvent as ReactKeyboardEvent, type MouseEvent as ReactMouseEvent, type ReactNode } from 'react'
import type { PanelTabRequest } from './panelTabRequest'
import { sendAppNav } from '../linkPreview/linkPreviewNav'
import { readMessageRef } from '../linkPreview/linkPreviewProtocol'
import { useClickGrant } from '../components/appFileOpen'
import { isTrustedClick, isTrustedKey } from '../components/trustedClick'
import { readOfferedText } from '../composerActions/literalInsert'
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

/** Why the host put none of an app's text in the message box. `no_gesture`: no act
 *  of the person on this control in the last `INSERT_ACT_MS`. */
export type ComposerInsertCode = 'stale' | 'too_large' | 'not_allowed' | 'not_literal' | 'no_gesture'

/** Host only: put `text` in the message box of the box's own chat; nothing back
 *  when it went in, or why not. Given to the scope only while the box is open. */
export type ScopeComposerInsert = (text: unknown) => ComposerInsertCode | undefined

/** What the scope gives the hooks below: where, and the box's click grant. */
interface Scope extends Where {
  /** True once per real click in the app's own module, within `CLICK_GRANT_MS` of it. */
  takeClick: () => boolean
  /** The host's insert, for a box that may insert (`composerInsert`, approved). */
  insert?: ScopeComposerInsert
}

const requests = new Map<string, SessionControlRequest>()
const listeners = new Set<() => void>()
let seq = 0

const keyOf = (sessionKey: string, control: string) => `${sessionKey}\u0000${control}`
const emit = () => { for (const fn of [...listeners]) fn() }

/** How long after a real act of the person aimed at a control its box may put text
 *  in the message box (`useInsertIntoComposer`): the person opening the box with its
 *  chip or its key, pressing its key again, or a trusted key or click in the box.
 *  The newest act restarts it. Outside it a call answers `no_gesture`, so a timer
 *  in a box nobody touched writes nothing. */
export const INSERT_ACT_MS = 5_000
/** The clock the window is measured on; a test sets `now`. */
export const insertActClock = { now: (): number => performance.now() }
/** When the person last acted on each chat's control. */
const lastActs = new Map<string, number>()
const actIsRecent = (sessionKey: string, control: string): boolean => {
  const at = lastActs.get(keyOf(sessionKey, control))
  if (at === undefined) return false
  const age = insertActClock.now() - at
  return age >= 0 && age <= INSERT_ACT_MS
}

/** Host only: the person acted on control `control` of chat `sessionKey`, so its
 *  insert window restarts. Called only for a trusted event of the person: a click
 *  on the control's chip or a press of its key that opens the box or hands it a
 *  request, and a key or click inside the box (`SessionControlRequestScope`). An
 *  open the app asked for (a card, a status go, `useSessionControlGo`), the box
 *  mounting, and an open the host restores on its own are no act. */
export function noteSessionControlAct(sessionKey: string, control: string): void {
  lastActs.set(keyOf(sessionKey, control), insertActClock.now())
}

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

/** Host only: the popover closed, so opening the chip by hand reads null, not the old
 *  payload, and a box the app opens again does not inherit the person's last act. */
export function clearSessionControlRequest(sessionKey: string, control: string): void {
  lastActs.delete(keyOf(sessionKey, control))
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

/** Whether an event the scope's box heard happened on its own elements. */
const inBox = (e: ReactMouseEvent | ReactKeyboardEvent): boolean => e.target instanceof Node && e.currentTarget.contains(e.target)

/** Host only: the chat and control the app module below is mounted for. It wraps
 *  the module (and nothing of the host's) in a `display: contents` box that
 *  records a real click for `useSessionControlGo`, as `AppFileOpenRoot` does for
 *  `useOpenFile`: the host's header and retry button never grant. */
export function SessionControlRequestScope({ sessionKey, control, insert, children }: Where & { insert?: ScopeComposerInsert; children: ReactNode }) {
  const { record, take } = useClickGrant()
  const value = useMemo(() => ({ sessionKey, control, takeClick: take, insert }), [sessionKey, control, take, insert])
  // Each trusted key or click in the box restarts the insert window. In the box's
  // own elements only: React carries an event from a portal the module draws
  // elsewhere on the page up through this scope too.
  const onClickCapture = useCallback((e: ReactMouseEvent) => {
    record(e)
    if (isTrustedClick(e) && inBox(e)) noteSessionControlAct(sessionKey, control)
  }, [record, sessionKey, control])
  const onKeyDownCapture = useCallback((e: ReactKeyboardEvent) => {
    if (isTrustedKey(e) && inBox(e)) noteSessionControlAct(sessionKey, control)
  }, [sessionKey, control])
  return createElement(
    WhereContext.Provider,
    { value },
    createElement('div', { 'data-session-control-scope': '', style: { display: 'contents' }, onClickCapture, onKeyDownCapture }, children),
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

/** What an insert answers, and all it answers: never the draft, the caret or a
 *  send. For text that passes the rules on its own, a refusal says one thing about
 *  the box at that moment: `stale`, that it could not take text then; `not_literal`,
 *  that the characters next to the caret would have joined the text into something
 *  that resolves. At most one bit about the draft per call, and only while the
 *  person acts. */
export type ComposerInsertResult = { ok: true } | { ok: false; code: ComposerInsertCode }

/** Put `text` in the message box of the box's own chat, at the person's caret
 *  (at the end when they placed none), after one space when the text before it
 *  does not end in one; what they typed stays, focus moves to the box with the
 *  caret after the text, and nothing is sent. Taken while this control's box is
 *  open docked in its chat, within `INSERT_ACT_MS` of the person's last act on it
 *  (opening the box with its chip or key, its key again, a key or click in the
 *  box; never an event the module dispatches), else `no_gesture`.
 *  The text is judged on its own first, so a text refused alone answers the same
 *  whatever the box holds. Null outside a session control, and while the control
 *  has no approved `composerInsert`; a grant withdrawn while the box is open
 *  answers `not_allowed`. */
export function useInsertIntoComposer(): ((text: string) => ComposerInsertResult) | null {
  const where = useContext(WhereContext)
  return useMemo(() => {
    if (!where?.insert) return null
    const { insert, sessionKey, control } = where
    return (text: string): ComposerInsertResult => {
      const offered = readOfferedText(text)
      if (!offered.ok) return { ok: false, code: offered.code }
      if (!actIsRecent(sessionKey, control)) return { ok: false, code: 'no_gesture' }
      const code = insert(text)
      return code ? { ok: false, code } : { ok: true }
    }
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
