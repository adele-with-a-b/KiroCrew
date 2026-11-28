import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ExternalLink } from 'lucide-react'

import { Btn } from './ui'
import { OverlayPortal, useCloseWhenCrewHidden } from './overlayRoot'
import { i18nT } from '../i18n/t'

/** Where the chip stacks when the asking frame is part of the page: above the
 *  full-screen views and their menus (`z-[9999]`). */
const PAGE_Z = 10000
/** The highest z-index a browser honours. */
const MAX_Z = 2147483647
/** Between the chip and the floating layer it sits on. */
const GAP = 4
/** The chip's height until it is measured. */
const CHIP_HEIGHT = 32

/**
 * The floating layer the asking frame is drawn in, or null when the frame is part
 * of the page (an inline widget, an artifact, a dashboard tab).
 *
 * A layer that floats above the page (a pinned link-preview card, the
 * message-action popover) carries its own z-index, far above PAGE_Z, so a chip at
 * PAGE_Z would be drawn under the very frame it answers for. The order is the
 * layer's own: it is read from the frame, not kept here as a second copy. The
 * OUTERMOST element with a z-index decides, as it does in the browser: it is the
 * one that competes with the chip, which is portalled next to `<body>`.
 */
function layerOf(frame: HTMLIFrameElement): { el: HTMLElement; z: number } | null {
  let found: { el: HTMLElement; z: number } | null = null
  for (let el: HTMLElement | null = frame; el; el = el.parentElement) {
    const z = Number.parseInt(window.getComputedStyle(el).zIndex, 10)
    if (Number.isFinite(z)) found = { el, z }
  }
  return found && found.z >= PAGE_Z ? found : null
}

type Place = {
  top?: number
  bottom?: number
  right: number
  zIndex: number
}

/**
 * The chip's place and stacking.
 *
 * For a frame in the page: the frame's top right corner, kept on screen. For a
 * frame in a floating layer: one step above that layer in the stacking order, and
 * just outside its top right corner, so it covers neither the link that was
 * clicked nor the layer's own controls. With no room above the layer it falls back
 * to the corner inside, where it is at least on top.
 */
function placeOf(frame: HTMLIFrameElement, chipHeight: number = CHIP_HEIGHT): Place {
  const layer = layerOf(frame)
  const rect = (layer?.el ?? frame).getBoundingClientRect()
  const keepRight = (right: number) => Math.max(8, Math.min(right, window.innerWidth - 120))
  if (layer && rect.top >= chipHeight + GAP + 8) {
    return {
      bottom: window.innerHeight - rect.top + GAP,
      right: keepRight(window.innerWidth - rect.right),
      zIndex: Math.min(layer.z + 1, MAX_Z),
    }
  }
  return {
    top: Math.max(8, Math.min(rect.top + 8, window.innerHeight - 48)),
    right: keepRight(window.innerWidth - rect.right + 8),
    zIndex: layer ? Math.min(layer.z + 1, MAX_Z) : PAGE_Z,
  }
}

/**
 * "Open <host>": the one way out for a link whose click the dashboard cannot tie
 * to the frame that asks for it (see `useFrameLinkBridge`).
 *
 * A click on this button is the person's own click in the dashboard, so the open
 * it makes borrows nobody's. The full URL is the title, so it shows on hover, as
 * on the link itself. The button takes focus when it appears, so a keyboard user
 * presses Enter again to open. It has no timer, so a slow reader does not lose
 * it: it stays until Escape, any other click or key, a scroll or a resize (it is
 * placed by the frame, so it cannot follow a scroll), or its Crew being hidden.
 * It is always drawn above the frame that asked (`placeOf`).
 */
export function FrameLinkChip({ url, frame, onOpen, onDismiss }: {
  url: string
  frame: HTMLIFrameElement
  onOpen: () => void
  onDismiss: () => void
}) {
  const ref = useRef<HTMLButtonElement | null>(null)
  const [place, setPlace] = useState(() => placeOf(frame))
  const dismissRef = useRef(onDismiss)
  dismissRef.current = onDismiss
  // A switch to a remote Crew hides this layer but not its window listeners, and a
  // link asked for on this Crew must not open from there: the button goes.
  useCloseWhenCrewHidden(true, onDismiss)
  useLayoutEffect(() => { setPlace(placeOf(frame, ref.current?.offsetHeight || CHIP_HEIGHT)) }, [frame, url])
  useEffect(() => {
    ref.current?.focus({ preventScroll: true })
    const dismiss = () => dismissRef.current()
    const elsewhere = (e: Event) => {
      if (!(e.target instanceof Node) || !ref.current?.contains(e.target)) dismiss()
    }
    const opts = { capture: true, passive: true } as const
    window.addEventListener('pointerdown', elsewhere, opts)
    window.addEventListener('keydown', elsewhere, opts)
    window.addEventListener('scroll', dismiss, opts)
    window.addEventListener('resize', dismiss, opts)
    return () => {
      window.removeEventListener('pointerdown', elsewhere, opts)
      window.removeEventListener('keydown', elsewhere, opts)
      window.removeEventListener('scroll', dismiss, opts)
      window.removeEventListener('resize', dismiss, opts)
    }
  }, [url])
  const host = new URL(url).host
  return (
    <OverlayPortal>
      <Btn
        ref={ref}
        type="button"
        primary
        data-frame-link-chip=""
        title={url}
        className="fixed max-w-[min(32rem,calc(100vw-16px))] shadow-lg"
        style={place}
        onClick={onOpen}
        onKeyDown={(e) => { if (e.key === 'Escape') onDismiss() }}
      >
        <ExternalLink size={14} aria-hidden="true" />
        <span className="truncate">{i18nT('hooks.frameLinkBridge.openHost', { host })}</span>
      </Btn>
    </OverlayPortal>
  )
}
