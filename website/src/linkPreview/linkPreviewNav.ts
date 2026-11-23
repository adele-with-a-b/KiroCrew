/**
 * What a link-preview card or a status go may ask of the chat it belongs to:
 * open one of its own app's targets (a side-panel tab, or its session control
 * under the message box), or jump the transcript to a message.
 *
 * `LinkPreviewLayer` lives above every page and knows which chat a show came
 * from; the chat page owns the side panel, the composer and the transcript. This
 * is the one hand-off between them. A request names the LOCAL slot its show sat
 * in, and the page acts only when that slot is the chat it has on screen, so a
 * card can never reach another chat. Each listener answers whether it took the
 * request, or, for an open it owns, why it refused it.
 *
 * @module linkPreview/linkPreviewNav
 */
import type { MessageRef } from './linkPreviewProtocol'

/** What an app may open in a chat: one of its own `contributes.panelTabs[]`, or
 *  one of its own `contributes.sessionControls[]` (the chip's popover). */
export type AppOpenTarget =
  | { kind: 'panelTab'; id: string }
  | { kind: 'sessionControl'; id: string }

/** Why an open was refused, as the app is told it:
 *  - `no_click`: no click in the card, and no click on its link left to spend
 *    (a hover show, a second open, or past `CLICK_GRANT_MS`);
 *  - `not_shown`: the card is not showing, or the request is for an earlier show;
 *  - `not_approved`: the owner did not approve this app for `sessionKey`;
 *  - `not_on_screen`: the chat is not the one on screen, or the target's chip is not drawn;
 *  - `not_declared`: this app has no such tab or control in that chat (not
 *    declared, disabled, or past the composer's cap);
 *  - `too_large`: a session control's payload is over `MAX_OPEN_PAYLOAD_BYTES`.
 *  The bootstrap adds `not_json`, and `no_answer` when a host never answers (an
 *  older host, a message dropped past the frame's size or burst limit). */
export type OpenRefusal = 'no_click' | 'not_shown' | 'not_approved' | 'not_on_screen' | 'not_declared' | 'too_large'

/** Largest payload a session control is opened with, as UTF-8 JSON: the same
 *  4 KB a status target's `arg` keeps. */
export const MAX_OPEN_PAYLOAD_BYTES = 4096

/** UTF-8 JSON size of `value`, or Infinity when it is not JSON. */
export function jsonBytes(value: unknown): number {
  try {
    const json = JSON.stringify(value === undefined ? null : value)
    return json === undefined ? Infinity : new TextEncoder().encode(json).length
  } catch {
    return Infinity
  }
}

export type LinkPreviewNav =
  /** Open `target` of `appName` and hand it `payload`; `via` says who asked (a card's click, a status go). */
  | { kind: 'open'; slot: string; appName: string; target: AppOpenTarget; payload: unknown; via: 'link' | 'status' }
  /** Scroll to `target`; `from` is the message the card's text sits in. */
  | { kind: 'jump'; slot: string; target: MessageRef; from?: MessageRef }

/** `true` took it, `false` not mine, a refusal: mine, and refused. */
type Listener = (req: LinkPreviewNav) => boolean | OpenRefusal

const listeners = new Set<Listener>()

/** Listen for card requests; returns the unsubscribe. */
export function onLinkPreviewNav(fn: Listener): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

/** Hand `req` to the chat page: `true` when one took it, else the first
 *  listener's refusal, else `not_on_screen` (no chat page has that chat). */
export function sendAppNav(req: LinkPreviewNav): true | OpenRefusal {
  let refusal: OpenRefusal | null = null
  let taken = false
  for (const fn of [...listeners]) {
    const r = fn(req)
    if (r === true) taken = true
    else if (r && !refusal) refusal = r
  }
  return taken ? true : refusal ?? 'not_on_screen'
}

// How long a click on a link-preview label may still lead to an open: while its
// show waits for the frame, and then for the click grant's bound. The layer that
// owns the frames reports it (`FrameHost.clickOpenEnd`); a popover that a press
// on a label would close holds the close that long, so the open that press
// asked for wins.
let clickOpenProbe: (() => number) | null = null

/** Report the end (`performance.now()` time) of the window a label click may still
 *  open something in; returns the unregister. */
export function setClickOpenProbe(fn: () => number): () => void {
  clickOpenProbe = fn
  return () => { if (clickOpenProbe === fn) clickOpenProbe = null }
}

/** Milliseconds a label click may still open something in, 0 when none. */
export function clickOpenTimeLeft(): number {
  const end = clickOpenProbe?.() ?? 0
  return Math.max(0, end - performance.now())
}

/** Hand `req` to the chat page; true when one took it. */
export function sendLinkPreviewNav(req: LinkPreviewNav): boolean {
  return sendAppNav(req) === true
}
