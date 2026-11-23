/**
 * Message-targeted scroll intent (pages/chat/page/transcriptJumps.ts), driven as
 * a hook against a stand-in search state and a spy for the transcript's
 * `navToDisplayIndex`.
 *
 * Pins the parts ChatPage-level suites never reach:
 *  - clicking a search result is a direct jump: it snaps (`behavior: 'auto'`)
 *    rather than gliding to an estimated offset;
 *  - re-clicking the SELECTED result still scrolls back to it (next frame) and
 *    re-centres the active mark, cancelling a previous converge loop first;
 *  - stepping (Enter/Arrow) centres each match and leaves the glide-vs-snap
 *    choice to `pickSearchScrollBehavior`;
 *  - the approval bar's "Show in chat" scrolls to the tool row by call id;
 *  - a row that is not in the display map is never scrolled to;
 *  - a link-preview card's jump acts only in the chat on screen, and keeps the
 *    spot it left from for the Back pill (the bottom, when the view rested there);
 *  - a session control's jump leaves from the view on screen.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'

const scroll = vi.hoisted(() => ({ cancel: vi.fn(), converge: vi.fn(), behavior: 'smooth' as ScrollBehavior }))
vi.mock('../utils/searchScroll', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../utils/searchScroll')>()),
  pickSearchScrollBehavior: () => scroll.behavior,
  scrollCurrentMatchIntoView: () => { scroll.converge(); return scroll.cancel },
}))

import { useTranscriptJumps } from '../pages/chat/page/transcriptJumps'
import { openActivityToTool } from '../store/chatSlice'
import type { ChatMessage } from '../types'
import { createTestStore } from './helpers'
import { sendLinkPreviewNav, type JumpOutcome } from '../linkPreview/linkPreviewNav'

type Opts = Parameters<typeof useTranscriptJumps>[0]
type Search = Opts['search']

function searchState(over: Partial<Search> = {}): Search {
  return {
    currentIdx: 0,
    currentMessageIdx: -1,
    matches: [{ msgIdx: 0 }, { msgIdx: 2 }, { msgIdx: 5 }],
    goTo: vi.fn(),
    ...over,
  } as unknown as Search
}

function harness(over: Partial<Opts> = {}) {
  const store = createTestStore()
  const map = new Map<number, number>([[0, 10], [2, 12], [3, 13]])
  const messages = [
    { role: 'user', content: 'a', ts: 't0', meta: { mid: 'm0' } },
    { role: 'assistant', content: 'b', ts: 't1' },
    { role: 'assistant', content: 'c', ts: 't2', meta: { mid: 'm2' } },
    { role: 'tool', content: 'd', meta: { tool_call_id: 'call-9' } },
  ] as ChatMessage[]
  const opts: Opts = {
    search: searchState(),
    messages,
    messageToDisplayIdx: map,
    messageToDisplayIdxRef: { current: map },
    navToDisplayIndex: vi.fn(),
    activeSlot: 'slot-a',
    cursorIsForActiveSlot: true,
    slotHasMore: false,
    slotOldestIndex: 0,
    initialMsgRef: { current: null },
    initialMidRef: { current: null },
    initialSidRef: { current: null },
    setHighlightTs: vi.fn(),
    handleJumpToPinnedMessage: vi.fn(),
    scrollerRef: { current: null },
    ...over,
  }
  const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
  const hook = renderHook((p: Opts) => useTranscriptJumps(p), { initialProps: opts, wrapper })
  return { opts, hook, store }
}

beforeEach(() => {
  scroll.cancel.mockClear()
  scroll.converge.mockClear()
  scroll.behavior = 'smooth'
})

describe('search result jumps', () => {
  it('a click on another result selects it and the jump snaps to the centred row', () => {
    const { opts, hook } = harness()
    act(() => { hook.result.current.jumpToSearchResult(1) })
    expect(opts.search.goTo).toHaveBeenCalledWith(1)
    expect(opts.navToDisplayIndex).not.toHaveBeenCalled()
    hook.rerender({ ...opts, search: searchState({ currentIdx: 1, currentMessageIdx: 2, goTo: opts.search.goTo }) })
    expect(opts.navToDisplayIndex).toHaveBeenCalledWith(12, { behavior: 'auto', align: 'center' })
  })

  it('stepping centres each match and takes the glide/snap choice from the step cadence', () => {
    const { opts, hook } = harness()
    hook.rerender({ ...opts, search: searchState({ currentIdx: 1, currentMessageIdx: 2 }) })
    expect(opts.navToDisplayIndex).toHaveBeenLastCalledWith(12, { behavior: 'smooth', align: 'center' })
    scroll.behavior = 'auto'
    hook.rerender({ ...opts, search: searchState({ currentIdx: 0, currentMessageIdx: 0 }) })
    expect(opts.navToDisplayIndex).toHaveBeenLastCalledWith(10, { behavior: 'auto', align: 'center' })
  })

  it('never scrolls to a match whose row is not in the display map', () => {
    const { opts, hook } = harness()
    hook.rerender({ ...opts, search: searchState({ currentIdx: 2, currentMessageIdx: 5 }) })
    expect(opts.navToDisplayIndex).not.toHaveBeenCalled()
  })

  it('a re-click on the selected result scrolls back next frame and re-centres its mark', () => {
    const rafs: FrameRequestCallback[] = []
    const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { rafs.push(cb); return rafs.length })
    try {
      const { opts, hook } = harness({ search: searchState({ currentIdx: 1, currentMessageIdx: -1 }) })
      act(() => { hook.result.current.jumpToSearchResult(1) })
      expect(opts.search.goTo).not.toHaveBeenCalled()
      expect(opts.navToDisplayIndex).not.toHaveBeenCalled()
      act(() => { rafs.shift()!(0) })
      expect(opts.navToDisplayIndex).toHaveBeenCalledWith(12, { behavior: 'auto', align: 'center' })
      expect(scroll.converge).toHaveBeenCalledTimes(1)
      expect(scroll.cancel).not.toHaveBeenCalled()
      // A second re-click cancels the first converge loop before starting one.
      act(() => { hook.result.current.jumpToSearchResult(1) })
      act(() => { rafs.shift()!(0) })
      expect(scroll.cancel).toHaveBeenCalledTimes(1)
      expect(scroll.converge).toHaveBeenCalledTimes(2)
    } finally { raf.mockRestore() }
  })

  it('a re-click on a selected result with no row does nothing', () => {
    const raf = vi.spyOn(window, 'requestAnimationFrame')
    try {
      const { opts, hook } = harness({ search: searchState({ currentIdx: 2 }) })
      act(() => { hook.result.current.jumpToSearchResult(2) })
      expect(raf).not.toHaveBeenCalled()
      expect(opts.search.goTo).not.toHaveBeenCalled()
    } finally { raf.mockRestore() }
  })
})

describe('"Show in chat" from the approval bar', () => {
  it('scrolls smoothly to the tool row that carries the focused call id', () => {
    const { opts, store } = harness()
    act(() => { store.dispatch(openActivityToTool('call-9')) })
    expect(opts.navToDisplayIndex).toHaveBeenCalledWith(13, { behavior: 'smooth', align: 'center' })
  })

  it('ignores a call id that no loaded tool row carries', () => {
    const { opts, store } = harness()
    act(() => { store.dispatch(openActivityToTool('call-missing')) })
    expect(opts.navToDisplayIndex).not.toHaveBeenCalled()
  })

  it('ignores a tool row that is not in the display map', () => {
    const { opts, store } = harness({ messageToDisplayIdx: new Map([[0, 10]]) })
    act(() => { store.dispatch(openActivityToTool('call-9')) })
    expect(opts.navToDisplayIndex).not.toHaveBeenCalled()
  })
})

describe("a link-preview card's jump", () => {
  /** A scroller at y=100 holding the rows of display index 10 (y=40) and 12 (y=340). */
  function scroller() {
    const sc = document.createElement('div')
    sc.getBoundingClientRect = () => ({ top: 100 }) as DOMRect
    for (const [di, top] of [[10, 40], [12, 340]]) {
      const row = document.createElement('div')
      row.setAttribute('data-display-index', String(di))
      row.getBoundingClientRect = () => ({ top }) as DOMRect
      sc.append(row)
    }
    return sc
  }

  it('jumps in the chat on screen and keeps where it left from; Back returns there and the pill goes', () => {
    const { opts, hook } = harness({ scrollerRef: { current: scroller() } })
    let taken = false
    act(() => { taken = sendLinkPreviewNav({ kind: 'jump', slot: 'slot-a', target: { ts: 't0', id: 'm0' }, from: { ts: 't2', id: 'm2' } }) })
    expect(taken).toBe(true)
    expect(opts.handleJumpToPinnedMessage).toHaveBeenCalledWith('t0', 'm0', { origin: 'link' })
    // The reply at t2 sat 240px below the scroller's top; the target is above it, so Back goes down.
    expect(hook.result.current.backSpot).toEqual({ from: { ts: 't2', id: 'm2' }, offset: 240, down: true })
    act(() => { hook.result.current.goBack() })
    expect(opts.navToDisplayIndex).toHaveBeenCalledWith(12, { behavior: 'smooth', align: 'start', offset: -240 })
    expect(hook.result.current.backSpot).toBeNull()
  })

  it('a jump down the transcript gives a Back that goes up; a row matched by ts alone', () => {
    const { hook } = harness({ scrollerRef: { current: scroller() } })
    act(() => { sendLinkPreviewNav({ kind: 'jump', slot: 'slot-a', target: { ts: 't2' }, from: { ts: 't0' } }) })
    expect(hook.result.current.backSpot).toEqual({ from: { ts: 't0' }, offset: -60, down: false })
  })

  it('a jump for another chat is not taken', () => {
    const { opts, hook } = harness({ scrollerRef: { current: scroller() } })
    let taken = true
    act(() => { taken = sendLinkPreviewNav({ kind: 'jump', slot: 'slot-b', target: { ts: 't0' }, from: { ts: 't2' } }) })
    expect(taken).toBe(false)
    expect(opts.handleJumpToPinnedMessage).not.toHaveBeenCalled()
    expect(hook.result.current.backSpot).toBeNull()
  })

  it('switching chats drops the Back pill', () => {
    const { opts, hook } = harness({ scrollerRef: { current: scroller() } })
    act(() => { sendLinkPreviewNav({ kind: 'jump', slot: 'slot-a', target: { ts: 't0' }, from: { ts: 't2' } }) })
    expect(hook.result.current.backSpot).not.toBeNull()
    hook.rerender({ ...opts, activeSlot: 'slot-b' })
    expect(hook.result.current.backSpot).toBeNull()
  })

  it('a target neither loaded nor older than the window jumps (to say so) with no Back pill', () => {
    const { opts, hook } = harness({ scrollerRef: { current: scroller() } })
    act(() => { sendLinkPreviewNav({ kind: 'jump', slot: 'slot-a', target: { ts: 'gone' }, from: { ts: 't2' } }) })
    expect(opts.handleJumpToPinnedMessage).toHaveBeenCalledWith('gone', undefined, { origin: 'link' })
    expect(hook.result.current.backSpot).toBeNull()
    // With older history still to page in, it may be there: the pill is offered.
    const more = harness({ scrollerRef: { current: scroller() }, slotHasMore: true, slotOldestIndex: 5 })
    act(() => { sendLinkPreviewNav({ kind: 'jump', slot: 'slot-a', target: { ts: 'gone' }, from: { ts: 't2' } }) })
    expect(more.hook.result.current.backSpot).toMatchObject({ down: true })
  })

  it('scrolling back to the bottom by hand after the jump drops the pill', () => {
    const sc = scroller()
    const { opts, hook } = harness({ scrollerRef: { current: sc }, isAtBottom: true })
    act(() => { sendLinkPreviewNav({ kind: 'jump', slot: 'slot-a', target: { ts: 't0' }, from: { ts: 't2' } }) })
    expect(hook.result.current.backSpot).not.toBeNull()
    hook.rerender({ ...opts, isAtBottom: false })
    expect(hook.result.current.backSpot).not.toBeNull()
    act(() => { sc.dispatchEvent(new Event('wheel')) })
    hook.rerender({ ...opts, isAtBottom: true })
    expect(hook.result.current.backSpot).toBeNull()
  })

  it('a jump that lands at the bottom keeps the pill until the person scrolls', () => {
    const sc = scroller()
    const { opts, hook } = harness({ scrollerRef: { current: sc }, isAtBottom: false })
    act(() => { sendLinkPreviewNav({ kind: 'jump', slot: 'slot-a', target: { ts: 't2' }, from: { ts: 't0' } }) })
    // The target sat in the last screenful: arriving there is not a scroll by hand.
    hook.rerender({ ...opts, isAtBottom: true })
    expect(hook.result.current.backSpot).not.toBeNull()
    hook.rerender({ ...opts, isAtBottom: false })
    act(() => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'End' })) })
    hook.rerender({ ...opts, isAtBottom: true })
    expect(hook.result.current.backSpot).toBeNull()
  })

  it('in split view a jump is not taken: the transcript it would move is not drawn', () => {
    const { opts, hook } = harness({ scrollerRef: { current: scroller() }, splitView: true })
    let taken = true
    act(() => { taken = sendLinkPreviewNav({ kind: 'jump', slot: 'slot-a', target: { ts: 't0' }, from: { ts: 't2' } }) })
    expect(taken).toBe(false)
    expect(opts.handleJumpToPinnedMessage).not.toHaveBeenCalled()
    expect(hook.result.current.backSpot).toBeNull()
  })

  it('a jump whose origin row is not drawn still jumps, with no Back pill', () => {
    const { opts, hook } = harness({ scrollerRef: { current: scroller() } })
    act(() => { sendLinkPreviewNav({ kind: 'jump', slot: 'slot-a', target: { ts: 't0' }, from: { ts: 't1' } }) })
    expect(opts.handleJumpToPinnedMessage).toHaveBeenCalledWith('t0', undefined, { origin: 'link' })
    expect(hook.result.current.backSpot).toBeNull()
  })
})

describe("a session control's jump (from: 'view')", () => {
  /** A scroller from y=100 to y=700 holding rows (display index, top, bottom). */
  function scroller(rows: [number, number, number][]) {
    const sc = document.createElement('div')
    sc.getBoundingClientRect = () => ({ top: 100, bottom: 700 }) as DOMRect
    for (const [di, top, bottom] of rows) {
      const row = document.createElement('div')
      row.setAttribute('data-display-index', String(di))
      row.getBoundingClientRect = () => ({ top, bottom }) as DOMRect
      sc.append(row)
    }
    return sc
  }
  /** The pins' jump, answering every jump with `o` the way it ends. */
  const ending = (o: JumpOutcome = true) => vi.fn((_ts: string, _id: string | undefined, opts: { onDone?: (o: JumpOutcome) => void }) => { opts.onDone?.(o) })
  const jump = (target: { ts: string; id?: string }, onDone?: (o: JumpOutcome) => void) =>
    sendLinkPreviewNav({ kind: 'jump', slot: 'slot-a', target, from: 'view', ...(onDone ? { onDone } : {}) })

  it('keeps the last reply whose top is on screen as the spot once the jump lands, and passes its answer on', () => {
    // t0 (10) is above the view, t1 is not drawn, t2 (12) starts on screen at 400.
    const { opts, hook } = harness({ scrollerRef: { current: scroller([[10, 20, 380], [12, 400, 900]]) }, handleJumpToPinnedMessage: ending() })
    const onDone = vi.fn()
    act(() => { jump({ ts: 't0', id: 'm0' }, onDone) })
    expect(opts.handleJumpToPinnedMessage).toHaveBeenCalledWith('t0', 'm0', { origin: 'link', onDone: expect.any(Function) })
    expect(onDone).toHaveBeenCalledWith(true)
    expect(hook.result.current.backSpot).toEqual({ from: { ts: 't2', id: 'm2' }, offset: 300, down: true })
    act(() => { hook.result.current.goBack() })
    expect(opts.navToDisplayIndex).toHaveBeenCalledWith(12, { behavior: 'smooth', align: 'start', offset: -300 })
  })

  it('a jump from the bottom of the chat (a card’s too) gives a Back to the bottom, through the chat’s own scroll', () => {
    const scrollBottom = vi.fn()
    const sc = scroller([[10, 20, 380], [12, 400, 900]])
    const { opts, hook } = harness({ scrollerRef: { current: sc }, isAtBottom: true, scrollBottom, handleJumpToPinnedMessage: ending() })
    act(() => { jump({ ts: 't0' }) })
    expect(hook.result.current.backSpot).toMatchObject({ from: { ts: 't2', id: 'm2' }, bottom: true })
    act(() => { hook.result.current.goBack() })
    expect(scrollBottom).toHaveBeenCalledTimes(1)
    expect(opts.navToDisplayIndex).not.toHaveBeenCalled()
    expect(hook.result.current.backSpot).toBeNull()
    act(() => { sendLinkPreviewNav({ kind: 'jump', slot: 'slot-a', target: { ts: 't0' }, from: { ts: 't2', id: 'm2' } }) })
    expect(hook.result.current.backSpot).toMatchObject({ bottom: true })
  })

  it('with no row top on screen, the row that fills the view is the spot', () => {
    const { hook } = harness({ scrollerRef: { current: scroller([[10, -50, 90], [12, 50, 1200]]) }, handleJumpToPinnedMessage: ending() })
    act(() => { jump({ ts: 't0' }) })
    expect(hook.result.current.backSpot).toEqual({ from: { ts: 't2', id: 'm2' }, offset: -50, down: true })
  })

  it('a row-index attribute inside message content is not a row', () => {
    const sc = scroller([[10, 20, 380], [12, 400, 900]])
    const forged = document.createElement('span')
    forged.setAttribute('data-display-index', '10')
    forged.getBoundingClientRect = () => ({ top: 600, bottom: 620 }) as DOMRect
    sc.lastElementChild!.append(forged)
    const { hook } = harness({ scrollerRef: { current: sc }, handleJumpToPinnedMessage: ending() })
    act(() => { jump({ ts: 't0' }) })
    expect(hook.result.current.backSpot).toMatchObject({ from: { ts: 't2', id: 'm2' }, offset: 300 })
  })

  it('a second jump keeps the spot the first left from, unless the person scrolled by hand since', () => {
    const sc = scroller([[12, 400, 900]])
    const { hook } = harness({ scrollerRef: { current: sc }, handleJumpToPinnedMessage: ending() })
    act(() => { jump({ ts: 't0' }) })
    const first = hook.result.current.backSpot
    act(() => { jump({ ts: 't2', id: 'm2' }) })
    expect(hook.result.current.backSpot).toEqual({ ...first, down: false })
    // The view now shows t0; what moved it decides where the next jump leaves from.
    sc.replaceChildren()
    const row = document.createElement('div')
    row.setAttribute('data-display-index', '10')
    row.getBoundingClientRect = () => ({ top: 250, bottom: 900 }) as DOMRect
    sc.append(row)
    const field = document.createElement('textarea')
    document.body.append(field)
    // A click in the chat scrolls nothing; keys typed in a field scroll nothing.
    act(() => { sc.dispatchEvent(new Event('pointerdown')) })
    act(() => { field.dispatchEvent(new KeyboardEvent('keydown', { key: 'PageUp', bubbles: true })) })
    act(() => { jump({ ts: 't2', id: 'm2' }) })
    expect(hook.result.current.backSpot).toEqual({ ...first, down: false })
    field.remove()
    // PageUp on the page moves the view: the next jump leaves from where the person is now.
    act(() => { document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'PageUp', bubbles: true })) })
    act(() => { jump({ ts: 't2', id: 'm2' }) })
    expect(hook.result.current.backSpot).toEqual({ from: { ts: 't0', id: 'm0' }, offset: 150, down: false })
    // So does the wheel.
    const again = hook.result.current.backSpot
    row.getBoundingClientRect = () => ({ top: 300, bottom: 900 }) as DOMRect
    act(() => { sc.dispatchEvent(new Event('wheel')) })
    act(() => { jump({ ts: 't2', id: 'm2' }) })
    expect(hook.result.current.backSpot).not.toEqual(again)
    expect(hook.result.current.backSpot).toMatchObject({ offset: 200 })
  })

  it('while the cursor is not yet this chat’s (a switch settling), a target not loaded is handed on to wait, not refused', () => {
    const { opts } = harness({ cursorIsForActiveSlot: false, scrollerRef: { current: scroller([[12, 400, 900]]) }, handleJumpToPinnedMessage: vi.fn() })
    const onDone = vi.fn()
    act(() => { jump({ ts: 'later', id: 'm9' }, onDone) })
    expect(opts.handleJumpToPinnedMessage).toHaveBeenCalledWith('later', 'm9', expect.objectContaining({ origin: 'link' }))
    expect(onDone).not.toHaveBeenCalled()
  })

  it('a target that is not in the chat is refused at once, unsent: no pill, and an earlier pill stays as it was', () => {
    const { opts, hook } = harness({ scrollerRef: { current: scroller([[12, 400, 900]]) }, handleJumpToPinnedMessage: ending() })
    const onDone = vi.fn()
    act(() => { jump({ ts: 'gone' }, onDone) })
    expect(onDone).toHaveBeenCalledWith('not_found')
    expect(opts.handleJumpToPinnedMessage).not.toHaveBeenCalled()
    expect(hook.result.current.backSpot).toBeNull()
    act(() => { jump({ ts: 't0' }) })
    const spot = hook.result.current.backSpot
    act(() => { jump({ ts: 'gone' }) })
    expect(hook.result.current.backSpot).toBe(spot)
  })

  it('with older history unloaded, a target newer than the oldest loaded message is refused at once; an older one pages', () => {
    const at = (m: number) => `2026-11-23T14:0${m}:00+00:00`
    const messages = [
      { role: 'user', content: 'a', ts: at(1), meta: { mid: 'm0' } },
      { role: 'assistant', content: 'b', ts: at(2) },
      { role: 'assistant', content: 'c', ts: at(3), meta: { mid: 'm2' } },
    ] as ChatMessage[]
    const { opts } = harness({ messages, slotHasMore: true, slotOldestIndex: 5, scrollerRef: { current: scroller([[12, 400, 900]]) }, handleJumpToPinnedMessage: ending('not_found') })
    const newer = vi.fn()
    act(() => { jump({ ts: at(5), id: 'other-chat' }, newer) })
    expect(newer).toHaveBeenCalledWith('not_found')
    expect(opts.handleJumpToPinnedMessage).not.toHaveBeenCalled()
    const older = vi.fn()
    act(() => { jump({ ts: '2026-11-23T13:00:00+00:00', id: 'old' }, older) })
    expect(opts.handleJumpToPinnedMessage).toHaveBeenCalledWith('2026-11-23T13:00:00+00:00', 'old', expect.objectContaining({ origin: 'link' }))
    expect(older).toHaveBeenCalledWith('not_found')
  })

  it('a jump that does not land (paged to the start, failed, replaced) leaves no pill and an earlier one as it was', () => {
    const handle = vi.fn((_ts: string, _id: string | undefined, opts: { onDone?: (o: JumpOutcome) => void }) => { opts.onDone?.(_ts === 't0' ? true : 'load_failed') })
    const { hook } = harness({ slotHasMore: true, slotOldestIndex: 5, scrollerRef: { current: scroller([[12, 400, 900]]) }, handleJumpToPinnedMessage: handle })
    act(() => { jump({ ts: 'old' }) })
    expect(hook.result.current.backSpot).toBeNull()
    act(() => { jump({ ts: 't0' }) })
    const spot = hook.result.current.backSpot
    expect(spot).not.toBeNull()
    act(() => { jump({ ts: 'old' }) })
    expect(hook.result.current.backSpot).toBe(spot)
  })
})
