/**
 * What a link-preview card may ask of the chat it was shown in: open its own
 * app's side-panel tab, or jump the transcript to a message.
 *
 * `LinkPreviewLayer` lives above every page and knows which chat a show came
 * from; the chat page owns the side panel and the transcript. This is the one
 * hand-off between them. A request names the LOCAL slot its show sat in, and the
 * page acts only when that slot is the chat it has on screen, so a card can never
 * reach another chat. Each listener answers whether it took the request.
 *
 * @module linkPreview/linkPreviewNav
 */
import type { MessageRef } from './linkPreviewProtocol'

export type LinkPreviewNav =
  /** Open `app:<appName>:<tab>` and hand it `payload`. */
  | { kind: 'openPanelTab'; slot: string; appName: string; tab: string; payload: unknown }
  /** Scroll to `target`; `from` is the message the card's text sits in. */
  | { kind: 'jump'; slot: string; target: MessageRef; from?: MessageRef }

type Listener = (req: LinkPreviewNav) => boolean

const listeners = new Set<Listener>()

/** Listen for card requests; returns the unsubscribe. */
export function onLinkPreviewNav(fn: Listener): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

/** Hand `req` to the chat page; true when one took it. */
export function sendLinkPreviewNav(req: LinkPreviewNav): boolean {
  let taken = false
  for (const fn of [...listeners]) if (fn(req)) taken = true
  return taken
}
