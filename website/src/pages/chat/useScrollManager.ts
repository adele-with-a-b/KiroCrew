import { useCallback, useRef } from 'react'

export interface ScrollToDisplayIndexOptions {
  behavior?: ScrollBehavior
  align?: ScrollLogicalPosition
  offset?: number
  /** Land on the row drawn for the message with this ts (`data-msg-ts`)
   *  inside the item; the item itself when that row is not drawn. */
  rowTs?: string
  /** With `rowTs`: the strip at the scroller's top that chrome covers. */
  topInset?: number
}

/**
 * Scroll management hook for chat — provides scroller ref, isAtBottom
 * tracking, scroll-to-bottom, and scroll-to-display-index.
 *
 * The chat virtualizer (`useVirtualChat`) consumes `scrollerRef` via its
 * `externalScrollerRef` option so both share a single DOM element; the
 * virtualizer's own ResizeObserver + layout effect drive streaming
 * auto-pin and append-pin, so this hook intentionally does NOT include
 * a streaming-scroll rAF loop or a follow-output effect — those would
 * race with the virtualizer's pin logic.
 */
export function useScrollManager() {
  const scrollerRef = useRef<HTMLDivElement>(null)

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
    const el = scrollerRef.current
    if (!el) return
    if (typeof el.scrollTo === 'function') {
      el.scrollTo({ top: el.scrollHeight, behavior })
    } else {
      el.scrollTop = el.scrollHeight
    }
  }, [])

  /** Scroll to a specific display index via data attribute. The caller mounts
   * the target first (ChatPage's navToDisplayIndex → virt.mountIndex), so by
   * the time this runs the element is in the DOM and — for near targets, where
   * mountIndex unions the window — everything between the current view and the
   * target is mounted too, so the smooth glide doesn't shift mid-scroll. A
   * single smooth scroll keeps it buttery; no settle-correction, which would
   * cause a visible "scroll past then snap back". */
  const scrollToDisplayIndex = useCallback((
    index: number,
    options: ScrollToDisplayIndexOptions = {}
  ): boolean => {
    const { behavior = 'smooth', align = 'center', offset = 0, rowTs, topInset = 0 } = options
    const container = scrollerRef.current
    if (!container) return false
    const item = container.querySelector(`[data-display-index="${index}"]`) as HTMLElement | null
    // A message jump lands on the message's own row, not on the middle of the
    // turn that holds it: a long turn's middle is its tool calls.
    const row = rowTs && item ? item.querySelector<HTMLElement>(`[data-msg-ts="${CSS.escape(rowTs)}"]`) : null
    const el = row ?? item
    if (!el) {
      // Row not committed to the DOM yet. Report the miss so the caller can
      // retry on a later frame, and do NOTHING here. We deliberately do NOT
      // teleport to top:0 — for a FAR jump the virtualizer REPLACES its window
      // via a React state update, which takes more than one frame to commit and
      // paint the target row; scrolling to top while we wait would make a far
      // jump visibly jump to the top before the target row commits.
      return false
    }
    // getBoundingClientRect (not el.offsetTop, which is relative to the
    // element's offsetParent) so the offset within the scroll content — and
    // the header chrome above the list — is always accounted for.
    const elTop = el.getBoundingClientRect().top - container.getBoundingClientRect().top + container.scrollTop
    const max = Math.max(0, container.scrollHeight - container.clientHeight)
    let top: number
    if (row) {
      // Land in the band the reader can see: the floating dock covers the
      // scroller's bottom padding, the pinned prompt `topInset`. A row taller
      // than the band shows its start rather than its middle.
      const band = container.clientHeight - topInset - (parseFloat(getComputedStyle(container).paddingBottom) || 0)
      top = el.offsetHeight > band ? elTop - topInset : elTop - topInset - band / 2 + el.offsetHeight / 2
    } else if (align === 'center') top = elTop - container.clientHeight / 2 + el.offsetHeight / 2
    else if (align === 'end') top = elTop - container.clientHeight + el.offsetHeight
    else top = elTop + offset // 'start' — offset is usually negative to clear the header
    top = Math.max(0, Math.min(max, top))
    if (typeof container.scrollTo === 'function') container.scrollTo({ top, behavior })
    else container.scrollTop = top
    return true
  }, [])

  return {
    scrollerRef,
    scrollToBottom,
    scrollToDisplayIndex,
  }
}
