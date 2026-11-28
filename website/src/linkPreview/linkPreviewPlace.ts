/**
 * Where a link-preview card stands next to its link: which side, and how tall
 * it is drawn there.
 *
 * A card stands in the part of the window its chat leaves free: under the
 * chat's header (the title row and the pinned-prompt banner, the transcript's
 * top overlay) and above its message box. It goes below its link when its
 * wanted height fits there, else above; when neither side holds it, it takes
 * the side with more room and is drawn that much shorter, so the app's own
 * document scrolls inside the frame. It is never laid over the header, the
 * message box or its own link.
 *
 * The header and the message box are measured from layout by the hooks that
 * already mark them (`topOverlays`, a pane's `data-pane-title-row`, the
 * composer's `input-wrapper`); nothing is added to either.
 *
 * @module linkPreview/linkPreviewPlace
 */
import { topOverlays } from '../pages/chat/browserLiveClearance'
import type { AnchorRect, Placement } from './linkPreviewProtocol'

/** Kept between a card and the window's edge, the chat's header and the message box. */
export const MARGIN = 8
/** Kept between a card and its link. */
export const GAP = 6

const PANE_TITLE_ROW = '[data-pane-title-row]'
const MESSAGE_BOX = '[data-testid="input-wrapper"]'

/**
 * The band of the window, top to bottom, a card for the link at `a` may stand
 * in. Only the header and the message box of the link's own chat bound it: the
 * ones that share its column, the header above it and the box below it. A link
 * that is in no chat gets the window less its margin.
 *
 * shortcut: read when a card is placed (a show, its `resize`, a step's landing,
 * the window's resize), so a message box that grows under a pinned card is
 * cleared at the next of those, not as it grows.
 */
export function cardRoom(a: AnchorRect): { top: number; bottom: number } {
  let top = MARGIN
  let bottom = window.innerHeight - MARGIN
  const sameColumn = (r: DOMRect) => r.height > 0 && r.left < a.left + a.width && r.right > a.left
  for (const el of [...topOverlays(), ...document.querySelectorAll<HTMLElement>(PANE_TITLE_ROW)]) {
    const r = el.getBoundingClientRect()
    if (!sameColumn(r) || r.top > a.top) continue
    // A banner set aside (visibility hidden) keeps its box but covers nothing.
    if (getComputedStyle(el).visibility === 'hidden') continue
    top = Math.max(top, r.bottom + MARGIN)
  }
  for (const el of document.querySelectorAll<HTMLElement>(MESSAGE_BOX)) {
    const r = el.getBoundingClientRect()
    if (sameColumn(r) && r.bottom > a.top) bottom = Math.min(bottom, r.top - MARGIN)
  }
  return { top, bottom }
}

/**
 * Where a card that wants `want` px of height goes next to its link at `a`, and
 * the height it is drawn at there. `under` px are kept free below the card for
 * what Crew hangs there (the answering bar), inside the same room.
 *
 * shortcut: a room smaller than `min` still gets a `min` tall card, which then
 * runs past the room; the window clamp in `FrameHost.place` keeps it on screen.
 */
export function beside(a: AnchorRect, want: number, min: number, under = 0): { top: number; height: number; side: Placement } {
  const room = cardRoom(a)
  const belowTop = Math.max(a.top + a.height + GAP, room.top)
  const aboveBottom = Math.min(a.top - GAP, room.bottom) - under
  const below = room.bottom - under - belowTop
  const above = aboveBottom - room.top
  if (want <= below) return { top: belowTop, height: want, side: 'below' }
  if (want <= above) return { top: aboveBottom - want, height: want, side: 'above' }
  if (below >= above) return { top: belowTop, height: Math.max(min, below), side: 'below' }
  const height = Math.max(min, above)
  return { top: aboveBottom - height, height, side: 'above' }
}
