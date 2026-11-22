import { useCallback, useEffect, useRef, useState, type MutableRefObject } from 'react'

import type { useMessageSearch } from '../../../hooks/useMessageSearch'
import { store, useAppSelector } from '../../../store'
import type { ChatMessage } from '../../../types'
import { pickSearchScrollBehavior, scrollCurrentMatchIntoView } from '../../../utils/searchScroll'
import { resolveMsgIndex } from '../../../utils/shareUrl'
import { drawnSuccessorIdx } from './transcriptPins'
import { onLinkPreviewNav } from '../../../linkPreview/linkPreviewNav'
import type { MessageRef } from '../../../linkPreview/linkPreviewProtocol'

/** Where a link-preview card's jump left from, for the Back pill. */
export interface BackSpot {
  /** The message the card's label sat in; the pill names its time. */
  from: MessageRef
  /** That message's row top, from the scroller's top, before the jump. */
  offset: number
  /** The jump went up the transcript, so Back goes down. */
  down: boolean
}

/** Keys that scroll the transcript by hand. */
const SCROLL_KEYS = new Set(['ArrowDown', 'PageDown', 'End', ' '])

/** The index of `ref` in `messages`: by `mid` when it has one, else by `ts`. */
function indexOf(messages: ChatMessage[], ref: MessageRef): number {
  return ref.id
    ? messages.findIndex(m => (m.meta as Record<string, unknown> | undefined)?.mid === ref.id)
    : messages.findIndex(m => m.ts === ref.ts)
}

interface TranscriptJumpsOptions {
  search: ReturnType<typeof useMessageSearch>
  messages: ChatMessage[]
  messageToDisplayIdx: Map<number, number>
  /** The same map through a ref, so a rebuilt map does not re-run the search scroll. */
  messageToDisplayIdxRef: MutableRefObject<Map<number, number>>
  navToDisplayIndex: (idx: number, opts?: { behavior?: ScrollBehavior; align?: ScrollLogicalPosition; offset?: number }) => void
  activeSlot: string | null
  cursorIsForActiveSlot: boolean
  slotHasMore: boolean
  slotOldestIndex: number
  /** The session controller's mount-time `?msg=` / `?mid=` / `?sid=` capture. */
  initialMsgRef: MutableRefObject<string | null>
  initialMidRef: MutableRefObject<string | null>
  initialSidRef: MutableRefObject<string | null>
  setHighlightTs: (ts: string | null) => void
  /** The pins' jump, which pages back for a message not yet loaded. */
  handleJumpToPinnedMessage: (messageTs: string, mid: string | undefined, opts: { origin: 'link' }) => void
  /** The transcript scroller, for where a card's jump left from. */
  scrollerRef: MutableRefObject<HTMLElement | null>
  /** The transcript rests at its bottom: back there by hand, the Back pill goes. */
  isAtBottom?: boolean
  /** Split view draws other panes instead of this transcript: a card's jump is not taken. */
  splitView?: boolean
}

/**
 * Scroll intent that targets one message: stepping and clicking through search
 * results, the approval bar's "Show in chat", and a `?msg=` deep link on a cold
 * load (which hands a target outside the loaded window to the pins' paging jump).
 */
export function useTranscriptJumps({
  search,
  messages,
  messageToDisplayIdx,
  messageToDisplayIdxRef,
  navToDisplayIndex,
  activeSlot,
  cursorIsForActiveSlot,
  slotHasMore,
  slotOldestIndex,
  initialMsgRef,
  initialMidRef,
  initialSidRef,
  setHighlightTs,
  handleJumpToPinnedMessage,
  scrollerRef,
  isAtBottom = false,
  splitView = false,
}: TranscriptJumpsOptions) {
  // Track the timestamp of the previous search-nav step so we can tell "user is
  // holding Enter through many matches" apart from "user landed on one match".
  // Rapid consecutive steps snap instantly (behavior:'auto') — a smooth glide
  // would be interrupted and restarted on every keypress, producing the stutter
  // of half-finished eased scrolls. A lone step (or the final one after a pause)
  // glides smoothly and centers. navToDisplayIndex still forces 'auto' for FAR
  // jumps regardless; this only governs NEAR jumps, which is where the queued-
  // animation jank lived.
  const lastSearchStepAtRef = useRef(0)
  // Set when the user clicks a row in the results panel (vs. Enter/Arrow
  // stepping). A click is a direct jump that's usually FAR and to an unmeasured
  // virtualized row — a smooth scroll animates to the *estimated* offset and
  // then visibly corrects once the row mounts. Snapping instantly collapses
  // that into one jump.
  const searchClickJumpRef = useRef(false)
  // Cancel handle for the re-click converge loop (below) so repeated re-clicks
  // of the same result don't stack concurrent loops + window listeners.
  const reclickScrollCancelRef = useRef<(() => void) | null>(null)
  const jumpToSearchResult = useCallback((i: number) => {
    // Re-clicking the already-selected result won't change currentIdx, so the
    // nav effect won't fire — scroll back to it imperatively so a click always
    // returns to the match even after the user has scrolled away from it.
    if (i === search.currentIdx) {
      const m = search.matches[i]
      const di = m ? messageToDisplayIdxRef.current.get(m.msgIdx) : undefined
      if (di !== undefined) {
        requestAnimationFrame(() => {
          navToDisplayIndex(di, { behavior: 'auto', align: 'center' })
          // currentOcc is unchanged so the message's occurrence-scroll effect
          // won't re-run; converge-center the already-rendered active mark.
          reclickScrollCancelRef.current?.()
          reclickScrollCancelRef.current = scrollCurrentMatchIntoView()
        })
      }
      return
    }
    searchClickJumpRef.current = true
    search.goTo(i)
  }, [search, navToDisplayIndex, messageToDisplayIdxRef])
  useEffect(() => {
    if (search.currentMessageIdx < 0) return
    const di = messageToDisplayIdxRef.current.get(search.currentMessageIdx)
    if (di === undefined) return
    const now = performance.now()
    const behavior = searchClickJumpRef.current
      ? 'auto'
      : pickSearchScrollBehavior(now, lastSearchStepAtRef.current)
    searchClickJumpRef.current = false
    lastSearchStepAtRef.current = now
    navToDisplayIndex(di, { behavior, align: 'center' })
  }, [search.currentMessageIdx, search.currentIdx, navToDisplayIndex, messageToDisplayIdxRef])

  // "Show in chat" button on the approval bar dispatches openActivityToTool,
  // which sets `focusToolCallId`. Pulling a virtualised pill back into the DOM
  // requires Virtuoso's own scrollToIndex — direct DOM scrollIntoView fails
  // because the element doesn't exist. ToolCallLine's own effect then takes
  // over once it mounts: refines the scroll position and clears the focus.
  const focusToolCallId = useAppSelector(s => s.chat.focusToolCallId)
  useEffect(() => {
    if (!focusToolCallId) return
    const msgIdx = messages.findIndex(m =>
      m.role === 'tool' && m.meta?.tool_call_id === focusToolCallId
    )
    if (msgIdx < 0) return
    const di = messageToDisplayIdx.get(msgIdx)
    if (di === undefined) return
    navToDisplayIndex(di, { behavior: 'smooth', align: 'center' })
  }, [focusToolCallId, messages, messageToDisplayIdx, navToDisplayIndex])

  // Deep-link: scroll to ?msg= timestamp on cold load.
  // When ?mid= is also present (copied from a pinned-message link), resolve by
  // mid first (stable per-message identity) and fall back to ts for legacy links.
  // The scroll-to-bottom effect above is suppressed while initialMsgRef is set.
  // Safety net: clear both refs after 5s to restore scroll-to-bottom if deep-link fails.
  useEffect(() => {
    if (!initialMsgRef.current) return
    const timer = setTimeout(() => { initialMsgRef.current = null; initialMidRef.current = null }, 5000)
    return () => clearTimeout(timer)
  }, [initialMsgRef, initialMidRef])
  useEffect(() => {
    const targetTs = initialMsgRef.current
    const targetMid = initialMidRef.current
    if (!targetTs || messages.length === 0) return
    // `messages` can still be the chat being left while a ?sid= switch settles,
    // so decide only once this window is known to belong to the target chat.
    if (initialSidRef.current && initialSidRef.current !== activeSlot) return
    if (!cursorIsForActiveSlot) return
    // The captured pair predates the mount effect that dispatches `switchSlot`, whose
    // `pending` nulls the cursor key even on a same-key switch -- so read it live.
    const liveChat = store.getState().chat
    if (liveChat.slotCursorKey !== liveChat.activeSlot) return
    const resolved = resolveMsgIndex(messages, targetTs, targetMid)
    // A mid that is merely OFF-PAGE falls back to ts in the helper, and that is a
    // DIFFERENT row of the same tick -- treat it as unresolved so the hand-off runs.
    const exactIdx = targetMid && messages[resolved]?.meta?.mid !== targetMid ? -1 : resolved
    // A link to a superseded steer lands on the row drawn in its place.
    const msgIdx = exactIdx < 0 ? exactIdx : drawnSuccessorIdx(messages, exactIdx)
    if (msgIdx < 0) {
      // A bounded first page need not contain the target; the jump path already
      // gates on the cursor and reports a dead link, so the decision lives there.
      initialMsgRef.current = null
      // Carries `targetMid`: paging back re-resolves, and ts alone would pick the
      // wrong message of a same-ts pair that the mid exists to disambiguate.
      handleJumpToPinnedMessage(targetTs, targetMid ?? undefined, { origin: 'link' })
      return
    }
    const di = messageToDisplayIdx.get(msgIdx)
    if (di === undefined) return
    initialMsgRef.current = null
    initialMidRef.current = null
    setTimeout(() => {
      navToDisplayIndex(di, { behavior: 'auto', align: 'center' })
      setHighlightTs(messages[msgIdx].ts ?? targetTs)
      setTimeout(() => setHighlightTs(null), 3000)
    }, 500)
  }, [messages, messageToDisplayIdx, slotHasMore, slotOldestIndex, handleJumpToPinnedMessage, activeSlot, cursorIsForActiveSlot]) // eslint-disable-line react-hooks/exhaustive-deps

  // A link-preview card's "jump to it": only in the chat on screen. The spot it
  // leaves from is kept, so the Back pill can return there; a newer jump
  // replaces it, another chat drops it, and so does scrolling to the bottom by
  // hand.
  const messagesRef = useRef(messages)
  messagesRef.current = messages
  const moreRef = useRef(slotHasMore && cursorIsForActiveSlot)
  moreRef.current = slotHasMore && cursorIsForActiveSlot
  const [backSpot, setBackSpot] = useState<BackSpot | null>(null)
  useEffect(() => { setBackSpot(null) }, [activeSlot])
  // Only the person's own scroll counts: a jump that lands in the last screenful
  // reaches the bottom too, and keeps its pill.
  const byHandRef = useRef(false)
  useEffect(() => {
    byHandRef.current = false
    const sc = scrollerRef.current
    if (!backSpot || !sc) return
    const mark = () => { byHandRef.current = true }
    const onKey = (e: KeyboardEvent) => { if (SCROLL_KEYS.has(e.key)) mark() }
    sc.addEventListener('wheel', mark, { passive: true })
    sc.addEventListener('touchmove', mark, { passive: true })
    sc.addEventListener('pointerdown', mark)
    window.addEventListener('keydown', onKey)
    return () => {
      sc.removeEventListener('wheel', mark)
      sc.removeEventListener('touchmove', mark)
      sc.removeEventListener('pointerdown', mark)
      window.removeEventListener('keydown', onKey)
    }
  }, [backSpot, scrollerRef])
  useEffect(() => {
    if (backSpot && isAtBottom && byHandRef.current) setBackSpot(null)
  }, [backSpot, isAtBottom])
  const splitRef = useRef(splitView)
  splitRef.current = splitView
  useEffect(() => onLinkPreviewNav((req) => {
    if (req.kind !== 'jump' || !activeSlot || req.slot !== activeSlot || splitRef.current) return false
    const list = messagesRef.current
    const from = req.from
    const fromIdx = from ? indexOf(list, from) : -1
    const di = fromIdx < 0 ? undefined : messageToDisplayIdxRef.current.get(fromIdx)
    const sc = scrollerRef.current
    const row = di === undefined ? null : sc?.querySelector<HTMLElement>(`[data-display-index="${di}"]`)
    const toIdx = indexOf(list, req.target)
    // A target neither loaded nor older than the window is not there: the jump
    // only says so, and leaves no way back to offer.
    if (from && sc && row && (toIdx >= 0 || moreRef.current)) {
      setBackSpot({
        from,
        offset: row.getBoundingClientRect().top - sc.getBoundingClientRect().top,
        // A target not loaded yet is older than the whole window.
        down: toIdx < 0 || toIdx < fromIdx,
      })
    } else {
      setBackSpot(null)
    }
    handleJumpToPinnedMessage(req.target.ts, req.target.id, { origin: 'link' })
    return true
  }), [activeSlot, handleJumpToPinnedMessage, messageToDisplayIdxRef, scrollerRef])
  /** Back to where the chat was before the card's jump; the pill goes. */
  const goBack = useCallback(() => {
    const spot = backSpot
    setBackSpot(null)
    if (!spot) return
    const idx = indexOf(messagesRef.current, spot.from)
    const di = idx < 0 ? undefined : messageToDisplayIdxRef.current.get(idx)
    if (di !== undefined) navToDisplayIndex(di, { behavior: 'smooth', align: 'start', offset: -spot.offset })
  }, [backSpot, messageToDisplayIdxRef, navToDisplayIndex])
  return { jumpToSearchResult, backSpot, goBack }
}
