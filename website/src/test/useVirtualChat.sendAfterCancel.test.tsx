/**
 * A reply is followed after the reader's own send, whatever the turn before
 * it did.
 *
 * The send path asks for a SMOOTH pin when the bottom is a short hop away
 * (`scrollBottom` in useChatPageTranscriptController). A send that lands while
 * the previous turn is still being stopped draws no bubble, so the reader is
 * already exactly at the bottom when that pin is written: the glide has no
 * distance, the browser animates nothing and dispatches no scroll event. The
 * smooth-pin guard was armed all the same, and only a scroll event disarms
 * it, so it stayed armed: every automatic pin stood down for a glide that was
 * never in flight, and its abort listeners released follow on the first input
 * that touched the scroller, even one that moved nothing. The reply then
 * streamed below the fold.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { RefObject } from 'react'
import { useVirtualChat, type UseVirtualChatOptions } from '../hooks/virtualizer/useVirtualChat'
import { smoothPinHasDistance } from '../hooks/virtualizer/FollowController'

interface Geom { scrollTop: number; scrollHeight: number; clientHeight: number }
interface Item { id: string }
const getKey = (it: Item) => it.id
const mkItems = (n: number): Item[] => Array.from({ length: n }, (_, i) => ({ id: `m${i}` }))

/** A scroller that behaves like the browser where it matters here: an instant
 *  write moves `scrollTop`, a smooth write moves nothing by itself (its frames
 *  are the test's to play), and a scroll event exists only for a position that
 *  actually changed. */
function makeScroller(initial: Geom) {
  const el = document.createElement('div')
  const state: Geom = { ...initial }
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => state.scrollTop,
    set: (v: number) => { state.scrollTop = v },
  })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => state.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => state.clientHeight })
  ;(el as unknown as { scrollTo: (o: { top: number; behavior?: string }) => void }).scrollTo =
    (o) => { if (o.behavior !== 'smooth') state.scrollTop = o.top }
  el.getBoundingClientRect = () =>
    ({ top: 0, bottom: 400, left: 0, right: 390, width: 390, height: 400, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
  let dispatchedTop = state.scrollTop
  const flushScroll = () => {
    if (state.scrollTop === dispatchedTop) return
    dispatchedTop = state.scrollTop
    act(() => { el.dispatchEvent(new Event('scroll')) })
  }
  return { el, state, flushScroll }
}

describe('useVirtualChat: a reply is followed after a send that follows a cancelled turn', () => {
  let origRaf: typeof requestAnimationFrame
  let origRO: typeof ResizeObserver | undefined
  let fire: ((entries: { target: Element }[]) => void) | undefined

  beforeEach(() => {
    localStorage.clear()
    origRaf = globalThis.requestAnimationFrame
    globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) => { cb(0); return 0 }) as typeof requestAnimationFrame
    origRO = globalThis.ResizeObserver
    globalThis.ResizeObserver = class {
      constructor(cb: ResizeObserverCallback) {
        fire = (entries) => cb(entries as unknown as ResizeObserverEntry[], this as unknown as ResizeObserver)
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    globalThis.requestAnimationFrame = origRaf
    if (origRO) globalThis.ResizeObserver = origRO
    fire = undefined
  })

  /** A reader following a running turn at the bottom, whose turn is then
   *  cancelled, and who sends: the page's send-time pin for a short hop. */
  function sendAfterCancel(sessionId: string) {
    const { el, state, flushScroll } = makeScroller({ scrollTop: 4600, scrollHeight: 5000, clientHeight: 400 })
    const ref: RefObject<HTMLDivElement | null> = { current: el }
    const base: UseVirtualChatOptions<Item> = {
      items: mkItems(30), sessionId, getKey, externalScrollerRef: ref, followOutput: true, runActive: true,
    }
    const view = renderHook((props: UseVirtualChatOptions<Item>) => useVirtualChat<Item>(props), { initialProps: base })
    // Past the mount: the observer has its first reading of the scroller's box.
    act(() => { fire?.([{ target: el }]) })
    // The turn is cancelled.
    act(() => { view.rerender({ ...base, runActive: false }) })
    // The send. Nothing was appended (the stopped turn is still winding down),
    // so the reader is at the bottom and the glide has nowhere to go.
    act(() => { view.result.current.scrollToBottom('smooth') })
    // The pin is written on the next frame (fake timers own the frame clock).
    act(() => { vi.advanceTimersToNextFrame() })
    flushScroll()
    expect(state.scrollTop).toBe(4600)
    /** The queued message runs: the reply's first content lands under the tail. */
    const replyArrives = (rows: number, scrollHeight: number) => {
      state.scrollHeight = scrollHeight
      act(() => { view.rerender({ ...base, runActive: true, items: mkItems(rows) }) })
      flushScroll()
    }
    return { el, state, view, flushScroll, replyArrives }
  }

  it('puts the view at the bottom when the reply arrives, across the queue band mounting', () => {
    const h = sendAfterCancel('send-after-cancel-queued')
    // The queued card mounts below the transcript and takes 60px of the box.
    h.state.clientHeight = 340
    act(() => { fire?.([{ target: h.el }]) })
    h.flushScroll()
    expect(h.state.scrollTop).toBe(5000 - 340)
    h.replyArrives(31, 5300)
    expect(h.state.scrollTop).toBe(5300 - 340)
    expect(h.view.result.current.getFollow()).toBe(true)
  })

  it('keeps following when an input that moves nothing lands between the send and the reply', () => {
    const h = sendAfterCancel('send-after-cancel-wheel')
    // A wheel-down at the end: there is nothing below to scroll to.
    act(() => { h.el.dispatchEvent(new WheelEvent('wheel', { deltaY: 40 })) })
    h.flushScroll()
    expect(h.view.result.current.getFollow()).toBe(true)
    act(() => { vi.advanceTimersByTime(300) })
    h.replyArrives(31, 5300)
    expect(h.state.scrollTop).toBe(5300 - 400)
    expect(h.view.result.current.getFollow()).toBe(true)
  })

  it('still leaves a reader who scrolls up during the reply where they are', () => {
    const h = sendAfterCancel('send-after-cancel-reader-leaves')
    h.replyArrives(31, 5300)
    expect(h.state.scrollTop).toBe(5300 - 400)
    // Reading back while the reply streams: a wheel-up and its scroll event.
    act(() => { h.el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 })) })
    h.state.scrollTop = 3000
    h.flushScroll()
    expect(h.view.result.current.getFollow()).toBe(false)
    act(() => { vi.advanceTimersByTime(300) })
    h.replyArrives(32, 5900)
    expect(h.state.scrollTop).toBe(3000)
    expect(h.view.result.current.getFollow()).toBe(false)
  })
})

describe('smoothPinHasDistance', () => {
  const geom = (scrollTop: number): Geom => ({ scrollTop, scrollHeight: 5000, clientHeight: 400 })

  it('is false for a reader already on the target, to the sub-pixel', () => {
    expect(smoothPinHasDistance(geom(4600), 4600)).toBe(false)
    expect(smoothPinHasDistance(geom(4598.5), 4600)).toBe(false)
  })

  it('is true for a reader the glide has to carry', () => {
    expect(smoothPinHasDistance(geom(4000), 4600)).toBe(true)
  })

  it('measures against the position the engine can reach, not a target past the end', () => {
    expect(smoothPinHasDistance(geom(4600), 9000)).toBe(false)
  })
})
