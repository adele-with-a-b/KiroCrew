import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'

import { FrameLinkChip } from './FrameLinkChip'
import { ACTIVATION_LIFESPAN_MS } from '../lib/widgetSrcdoc'
import { focusEntry, frameInputs, hostInput, tiedToFrame } from '../hooks/useFrameLinkBridge'

const URL_A = 'https://example.com/docs/page'
/** What the link-preview layer gives a card's frame (`FrameHost.create`), and the
 *  message-action popover its box. */
const CARD_Z = 2147482000

function sized(width: number, height: number) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: height })
}

function boxed(el: HTMLElement, left: number, top: number, width: number, height: number) {
  el.getBoundingClientRect = () => ({
    left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}),
  })
}

/** A card's frame as the link-preview layer mounts it: on `<body>`, fixed, with the layer's z-index. */
function cardFrame(left: number, top: number, width: number, height: number): HTMLIFrameElement {
  const frame = document.createElement('iframe')
  frame.setAttribute('data-test-node', '')
  Object.assign(frame.style, { position: 'fixed', zIndex: String(CARD_Z) })
  boxed(frame, left, top, width, height)
  document.body.appendChild(frame)
  return frame
}

/** A frame that is part of the page, as an inline widget's is. */
function pageFrame(left: number, top: number, width: number, height: number): HTMLIFrameElement {
  const pane = document.createElement('div')
  pane.setAttribute('data-test-node', '')
  // A pane with a small z-index of its own, as the chat column has.
  Object.assign(pane.style, { position: 'relative', zIndex: '10' })
  const frame = document.createElement('iframe')
  boxed(frame, left, top, width, height)
  pane.appendChild(frame)
  document.body.appendChild(pane)
  return frame
}

const chip = () => screen.queryByRole('button', { name: /example\.com/ })
const px = (v: string) => Number.parseFloat(v)

function show(frame: HTMLIFrameElement, onDismiss = vi.fn(), onOpen = vi.fn()) {
  render(<FrameLinkChip url={URL_A} frame={frame} onOpen={onOpen} onDismiss={onDismiss} />)
  const button = chip()
  if (!button) throw new Error('no chip')
  return { button, onDismiss, onOpen }
}

beforeEach(() => { sized(1440, 900) })

afterEach(() => {
  vi.restoreAllMocks()
  document.querySelectorAll('[data-test-node]').forEach((n) => n.remove())
})

describe('the Open <host> button over a floating layer', () => {
  it('is drawn above a pinned card, not under it', () => {
    const { button } = show(cardFrame(480, 240, 440, 320))
    expect(Number(button.style.zIndex)).toBeGreaterThan(CARD_Z)
  })

  it('is drawn above a frame that sits inside a floating box, such as the message-action popover', () => {
    const box = document.createElement('div')
    box.setAttribute('data-test-node', '')
    Object.assign(box.style, { position: 'fixed', zIndex: String(CARD_Z) })
    boxed(box, 500, 200, 400, 360)
    const frame = document.createElement('iframe')
    boxed(frame, 500, 240, 400, 320)
    box.appendChild(frame)
    document.body.appendChild(box)
    const { button } = show(frame)
    expect(Number(button.style.zIndex)).toBeGreaterThan(CARD_Z)
    // Outside the box, above its own header, not over the frame inside it.
    expect(px(button.style.bottom)).toBe(900 - 200 + 4)
  })

  it('never asks for a z-index a browser does not honour', () => {
    const frame = cardFrame(480, 240, 440, 320)
    frame.style.zIndex = '2147483647'
    const { button } = show(frame)
    expect(button.style.zIndex).toBe('2147483647')
  })

  it('sits just outside the card\'s top right corner at 1440 wide, covering nothing in the card', () => {
    const { button } = show(cardFrame(480, 240, 440, 320))
    // Its bottom edge is 4 px above the card's top edge; its right edge is the card's.
    expect(px(button.style.bottom)).toBe(900 - 240 + 4)
    expect(px(button.style.right)).toBe(1440 - 920)
    expect(button.style.top).toBe('')
  })

  it('stays inside the window at 390 wide', () => {
    sized(390, 844)
    const { button } = show(cardFrame(8, 300, 374, 360))
    expect(px(button.style.bottom)).toBe(844 - 300 + 4)
    expect(px(button.style.right)).toBe(8)
    expect(button.className).toContain('max-w-[min(32rem,calc(100vw-16px))]')
  })

  it('keeps its right edge on screen when the card reaches past the window', () => {
    sized(390, 844)
    const { button } = show(cardFrame(40, 300, 374, 360))
    expect(px(button.style.right)).toBe(8)
  })

  it('falls back to the corner inside the card when there is no room above it, still on top', () => {
    const { button } = show(cardFrame(480, 12, 440, 320))
    expect(px(button.style.top)).toBe(20)
    expect(px(button.style.right)).toBe(1440 - 920 + 8)
    expect(button.style.bottom).toBe('')
    expect(Number(button.style.zIndex)).toBeGreaterThan(CARD_Z)
  })

  it('takes focus, opens on a click, and goes on Escape or a click elsewhere, as for any frame', () => {
    const frame = cardFrame(480, 240, 440, 320)
    const { button, onDismiss, onOpen } = show(frame)
    expect(document.activeElement).toBe(button)
    act(() => { button.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true })) })
    expect(onDismiss).not.toHaveBeenCalled()
    fireEvent.click(button)
    expect(onOpen).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(button, { key: 'Escape' })
    expect(onDismiss).toHaveBeenCalledTimes(1)
    act(() => { frame.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true })) })
    expect(onDismiss).toHaveBeenCalledTimes(2)
  })
})

describe('the Open <host> button for a frame in the page', () => {
  it('stays where it was: the frame\'s top right corner, at the page\'s own stacking', () => {
    const { button } = show(pageFrame(300, 200, 600, 400))
    expect(px(button.style.top)).toBe(208)
    expect(px(button.style.right)).toBe(1440 - 900 + 8)
    expect(button.style.bottom).toBe('')
    expect(button.style.zIndex).toBe('10000')
  })

  it('is kept on screen for a frame scrolled half out of view', () => {
    const { button } = show(pageFrame(300, -120, 1400, 2000))
    expect(px(button.style.top)).toBe(8)
    expect(px(button.style.right)).toBe(8)
  })
})

// The fix above changes where the button is drawn, never when it is shown. This
// pins the rule that decides that, so a later change to it fails here by name.
describe('when a frame counts as tied to the person\'s press (unchanged)', () => {
  const frame = () => {
    const el = document.createElement('iframe')
    Object.defineProperty(el, 'contentWindow', { configurable: true, value: {} as Window })
    return el
  }

  beforeEach(() => {
    hostInput.lastAt = -Infinity
    frameInputs.clear()
    focusEntry.frame = null
    focusEntry.pointer = null
  })

  it('an activation lives 5 s', () => {
    expect(ACTIVATION_LIFESPAN_MS).toBe(5000)
  })

  it('a click or key in the dashboard unties every frame until 5 s have passed', () => {
    const f = frame()
    focusEntry.frame = f
    hostInput.lastAt = 1000
    expect(tiedToFrame(f, 1000)).toBe(false)
    expect(tiedToFrame(f, 5999)).toBe(false)
    expect(tiedToFrame(f, 6000)).toBe(true)
  })

  it('so does input reported by another frame, but not the frame\'s own', () => {
    const f = frame()
    const other = frame()
    focusEntry.frame = f
    frameInputs.set(f.contentWindow as Window, 2000)
    expect(tiedToFrame(f, 2001)).toBe(true)
    frameInputs.set(other.contentWindow as Window, 2000)
    expect(tiedToFrame(f, 6999)).toBe(false)
    expect(tiedToFrame(f, 7000)).toBe(true)
  })

  it('focus must have come into that frame from the dashboard, and the pointer must not be over another', () => {
    const f = frame()
    const other = frame()
    expect(tiedToFrame(f, 9e9)).toBe(false)
    focusEntry.frame = other
    expect(tiedToFrame(f, 9e9)).toBe(false)
    focusEntry.frame = f
    expect(tiedToFrame(f, 9e9)).toBe(true)
    focusEntry.pointer = other
    expect(tiedToFrame(f, 9e9)).toBe(false)
    focusEntry.pointer = f
    expect(tiedToFrame(f, 9e9)).toBe(true)
  })
})
