/**
 * The message protocol between the dashboard and a link-preview frame.
 *
 * An app's card runs in an app frame (`apps/app_frame.py`): `sandbox="allow-scripts"`
 * with no `allow-same-origin`, so its origin is opaque and `postMessage` is the
 * only channel. What every app frame shares (the tag, the rate bound) is in
 * `appFrame/frameProtocol.ts`.
 *
 * Dashboard to frame (the frame's bootstrap turns these into provider calls):
 *
 * - `init { capability, theme }` once the frame has loaded
 * - `capability { capability }` a renewed capability for `ctx.fetch`
 * - `show { invocation, url? | text?, rect, theme, modifiers, trigger, placement?, sessionKey?, message? }`
 *   the hovered link or text, where it is, the modifier keys held when the hover
 *   began, how the card was asked for (`hover`, a mouse `click`, a touch `tap`),
 *   and which side of the link the card is put on (`above` or `below`; absent in
 *   overlay mode).
 *   For an app the owner approved for `sessionKey`: the chat and the message
 *   (`{ ts, id? }`) the link or text sits in.
 *   `invocation` numbers the shows of one frame; a card message (`open`, `close`,
 *   `resize`, `pin`, `drag`, `dock`) carries the one it answers, and one for an
 *   older show is dropped.
 * - `placement { invocation, placement }` the latest show's card was moved to the
 *   other side of its link (its own `resize`, or the window's).
 * - `opened { request, ok, reason? }` the answer to an `openPanelTab` or
 *   `openSessionControl` that carried a `request` number: opened, or refused
 *   with an `OpenRefusal` (`linkPreviewNav.ts`).
 * - `leave`, `hide { reason }`, `theme { theme }`, `dispose`
 * - `key { key, code, shift }` a key pressed in the dashboard while a card shows,
 *   so a card's own shortcuts work without a click into it first. Never while
 *   the dashboard's focus is in a text field, and never with Ctrl, Alt or Meta.
 *
 * Frame to dashboard (read by `readFrameMessage`, which drops anything else):
 *
 * - `ready`, `failed { message }` the module activated, or did not
 * - `open`, `close` show or hide this frame
 * - `resize { width, height }` the card's size; the dashboard clamps it
 * - `pin { pinned }` a pinned card stays when the pointer leaves or the chat scrolls
 * - `drag { dx, dy }` move the card by this much
 * - `dock { edge }` stick the card to the left or right edge, or `null` to undock
 * - `textPatternsOff { ids }`, `escape`, `capabilityExpired`
 * - `openPanelTab { invocation, tab, payload }` open this app's own side-panel tab
 *   `tab` (a `contributes.panelTabs[].id`) in the chat that show came from, and hand
 *   the tab `payload` (`usePanelTabRequest`). Only for an app approved for
 *   `sessionKey` (the tab is told its chat), while that chat is on screen, right
 *   after the person's click in the card, or once, within `CLICK_GRANT_MS`, for
 *   the show a click or tap on the link made while its card is up.
 * - `openSessionControl { invocation, control, payload, request? }` open this
 *   app's own session control `control` (a `contributes.sessionControls[].id`):
 *   its chip's popover under the message box of the chat that show came from,
 *   handing it `payload` (`useSessionControlRequest`). The same rules as
 *   `openPanelTab`, and the same click grant: one click on a link opens ONE
 *   target, a tab or a control, never both.
 * - `jump { invocation, ts, id? }` scroll the chat that show came from to one of its
 *   messages and light it, with a Back pill. Only for an app approved for
 *   `sessionKey`, while that chat is on screen, right after a click in the card.
 * - `regions { rects }` overlay mode, for a provider that manages several windows
 *   of its own (pinned cards, a dock, dialogs): the frame covers the viewport and
 *   is clipped to these rects, so everywhere else the page stays clickable. Its
 *   first `regions` puts the frame in this mode; `open`, `close`, `resize`, `pin`,
 *   `drag` and `dock` are then ignored, since the provider moves its own windows.
 *
 * A message larger than `MAX_MESSAGE_BYTES`, or past a frame's burst allowance
 * (`BURST_CAPACITY`, refilled at `BURST_PER_SECOND`), is dropped unread.
 *
 * The dashboard positions and sizes the frame. Nothing the frame says reaches the
 * dashboard's DOM: a message only moves, sizes, clips, shows or hides that one
 * iframe, opens that app's own tab, or scrolls the chat its show came from.
 *
 * @module linkPreview/linkPreviewProtocol
 */

import { finite, readTagged, tagged } from '../appFrame/frameProtocol'
import type { OpenRefusal } from './linkPreviewNav'

export { BURST_CAPACITY, BURST_PER_SECOND, messageBytes } from '../appFrame/frameProtocol'

export interface AnchorRect {
  left: number
  top: number
  width: number
  height: number
}

export interface Modifiers {
  alt: boolean
  shift: boolean
  ctrl: boolean
  meta: boolean
}

/** One chat message: its `ts`, and its `mid` (`id`) when it has one. */
export interface MessageRef {
  ts: string
  id?: string
}

/** How a card was asked for: the hover delay ran out, a mouse click, a touch tap. */
export type ShowTrigger = 'hover' | 'click' | 'tap'

/** Which side of its link the dashboard put the card on. */
export type Placement = 'above' | 'below'

/** What one hover shows: a link's URL or a pattern's matched text, and, for an app
 *  the owner approved for it, the session key of the chat the message is in and
 *  that message. */
export type PreviewLink = {
  url?: string
  text?: string
  sessionKey?: string
  message?: MessageRef
}

export type ToFrame =
  | { type: 'init'; capability: string | null; theme: 'dark' | 'light' }
  | { type: 'capability'; capability: string | null }
  | ({ type: 'show'; invocation: number; rect: AnchorRect; theme: 'dark' | 'light'; modifiers?: Modifiers; trigger?: ShowTrigger; placement?: Placement } & PreviewLink)
  | { type: 'leave' }
  | { type: 'hide'; reason: string }
  | { type: 'theme'; theme: 'dark' | 'light' }
  | { type: 'dispose' }
  | { type: 'key'; key: string; code: string; shift: boolean }
  | { type: 'placement'; invocation: number; placement: Placement }
  | { type: 'opened'; request: number; ok: boolean; reason?: OpenRefusal }

export type FromFrame =
  | { type: 'ready' }
  | { type: 'failed'; message: string }
  | { type: 'open'; invocation: number }
  | { type: 'close'; invocation: number }
  | { type: 'resize'; invocation: number; width: number; height: number }
  | { type: 'pin'; invocation: number; pinned: boolean }
  | { type: 'drag'; invocation: number; dx: number; dy: number }
  | { type: 'dock'; invocation: number; edge: 'left' | 'right' | null }
  | { type: 'textPatternsOff'; ids: string[] }
  | { type: 'escape' }
  | { type: 'capabilityExpired' }
  | { type: 'regions'; rects: AnchorRect[] }
  | { type: 'openPanelTab'; invocation: number; tab: string; payload: unknown; request?: number }
  | { type: 'openSessionControl'; invocation: number; control: string; payload: unknown; request?: number }
  | { type: 'jump'; invocation: number; target: MessageRef }

/** A `panelTabs[].id`: the slug the manifest allows (`_PANEL_TAB_ID_RE`). */
const TAB_ID = /^[a-z][a-z0-9_-]{0,63}$/
/** A `sessionControls[].id`: a kebab slug (`APP_ITEM_ID_RE`), as long as a tab id. */
const CONTROL_ID = /^[a-z0-9][a-z0-9-]{0,63}$/
/** Longest message `ts` or `mid` a frame may name. */
const MAX_ID_CHARS = 128

/** A frame's `{ ts, id? }`, or null when `ts` is not a short string. */
export function readMessageRef(ts: unknown, id: unknown): MessageRef | null {
  if (typeof ts !== 'string' || !ts || ts.length > MAX_ID_CHARS) return null
  return typeof id === 'string' && id && id.length <= MAX_ID_CHARS ? { ts, id } : { ts }
}

/** A JSON copy of `value`, or undefined when it is not plain JSON. */
function jsonCopy(value: unknown): unknown {
  if (value === undefined) return null
  try {
    return JSON.parse(JSON.stringify(value)) as unknown
  } catch {
    return undefined
  }
}

/** `{ request }` when the frame numbered its open so it can be answered, else nothing. */
function requestOf(raw: unknown): { request?: number } {
  return typeof raw === 'number' && Number.isSafeInteger(raw) && raw >= 0 ? { request: raw } : {}
}

/** At most this many rects in one `regions` message; the rest are dropped. */
export const MAX_REGIONS = 32
/** Largest message a frame may send, as JSON text. */
export const MAX_MESSAGE_BYTES = 8192
/** The frame message in `data`, or null when it is not one this protocol defines. */
export function readFrameMessage(data: unknown): FromFrame | null {
  const m = readTagged(data)
  if (!m) return null
  const invocation = typeof m.invocation === 'number' && Number.isInteger(m.invocation) && m.invocation >= 0 ? m.invocation : -1
  switch (m.type) {
    case 'ready':
    case 'escape':
    case 'capabilityExpired':
      return { type: m.type }
    case 'open':
    case 'close':
      return { type: m.type, invocation }
    case 'failed':
      return { type: 'failed', message: typeof m.message === 'string' ? m.message.slice(0, 200) : '' }
    case 'resize':
      return { type: 'resize', invocation, width: finite(m.width), height: finite(m.height) }
    case 'pin':
      return { type: 'pin', invocation, pinned: m.pinned === true }
    case 'drag':
      return { type: 'drag', invocation, dx: finite(m.dx), dy: finite(m.dy) }
    case 'dock':
      return { type: 'dock', invocation, edge: m.edge === 'left' || m.edge === 'right' ? m.edge : null }
    case 'textPatternsOff':
      return {
        type: 'textPatternsOff',
        ids: Array.isArray(m.ids) ? m.ids.filter((x): x is string => typeof x === 'string').slice(0, 64) : [],
      }
    case 'regions': {
      const raw = Array.isArray(m.rects) ? m.rects.slice(0, MAX_REGIONS) : []
      const rects: AnchorRect[] = []
      for (const r of raw) {
        if (!r || typeof r !== 'object') continue
        const q = r as Record<string, unknown>
        const rect = { left: finite(q.left), top: finite(q.top), width: finite(q.width), height: finite(q.height) }
        if (rect.width > 0 && rect.height > 0) rects.push(rect)
      }
      return { type: 'regions', rects }
    }
    case 'openPanelTab': {
      if (typeof m.tab !== 'string' || !TAB_ID.test(m.tab)) return null
      const payload = jsonCopy(m.payload)
      return payload === undefined ? null : { type: 'openPanelTab', invocation, tab: m.tab, payload, ...requestOf(m.request) }
    }
    case 'openSessionControl': {
      if (typeof m.control !== 'string' || !CONTROL_ID.test(m.control)) return null
      const payload = jsonCopy(m.payload)
      return payload === undefined ? null : { type: 'openSessionControl', invocation, control: m.control, payload, ...requestOf(m.request) }
    }
    case 'jump': {
      const target = readMessageRef(m.ts, m.id)
      return target ? { type: 'jump', invocation, target } : null
    }
    default:
      return null
  }
}

export function toFrame(msg: ToFrame): Record<string, unknown> {
  return tagged(msg)
}
