import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { FrameHost, type PreviewFrame, type ShowScope, type StepLanding } from './linkPreviewFrames'
import type { ShowTrigger } from './linkPreviewProtocol'
import { installFrameWindows, sentTo } from '../test/LinkPreviewFramesTestUtils'

installFrameWindows()

const KEY = 'labels-app:labels'
const SCOPE: ShowScope = { slot: 'chat-7', origin: { ts: 't1' }, messages: true }
const MARGIN = 8
const GAP = 6

type Box = { left: number; top: number; width: number; height: number }

let steps: StepLanding[] = []
let host: FrameHost

function setWindow(width: number, height: number) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: height })
}

/** An element of Crew's page standing at `box`, found by `attr`. */
function stand(attr: string, value: string, box: Box): HTMLElement {
  const el = document.createElement('div')
  el.setAttribute(attr, value)
  el.getBoundingClientRect = () => new DOMRect(box.left, box.top, box.width, box.height)
  document.body.append(el)
  return el
}
const header = (box: Box) => stand('data-transcript-top-overlay', '', box)
const messageBox = (box: Box) => stand('data-testid', 'input-wrapper', box)
/** The composer dock at `box`, holding what Crew stacks above the message box. */
function dock(box: Box, ...inside: HTMLElement[]): HTMLElement {
  const el = stand('data-testid', 'composer-dock-root', box)
  el.append(...inside)
  return el
}

/** A 1440 x 900 window: the title row ends at 86, the message box starts at 777. */
function desktop() {
  setWindow(1440, 900)
  header({ left: 350, top: 44, width: 1084, height: 42 })
  messageBox({ left: 492, top: 777, width: 784, height: 85 })
}
/** A 390 x 844 phone: the title row ends at 52, the message box starts at 715. */
function phone() {
  setWindow(390, 844)
  header({ left: 0, top: 0, width: 384, height: 52 })
  messageBox({ left: 16, top: 715, width: 358, height: 85 })
}

/** A ready card shown for a label at `label` by `trigger`, opened, wanting `height` px. */
async function card(label: Box, height: number, trigger: ShowTrigger = 'click', width = 420): Promise<PreviewFrame> {
  const frame = host.hoverFrame(KEY, 'labels-app')
  host.handle(frame, { type: 'ready' })
  await frame.ready
  expect(await host.show(frame, { text: '[Q4a]' }, label, undefined, { trigger, scope: SCOPE })).toBe(true)
  host.handle(frame, { type: 'resize', invocation: frame.invocation, width, height })
  host.handle(frame, { type: 'open', invocation: frame.invocation })
  return frame
}
const pin = (frame: PreviewFrame) => host.handle(frame, { type: 'pin', invocation: frame.invocation, pinned: true })

/** The pinned card steps, and its label lands at `label`. */
async function stepTo(frame: PreviewFrame, label: Box) {
  Object.defineProperty(navigator, 'userActivation', { configurable: true, value: { isActive: true } })
  frame.el.tabIndex = 0
  frame.el.focus()
  host.handle(frame, { type: 'step', invocation: frame.invocation, target: { ts: 't9' }, text: '[Q6a]', request: 1 })
  steps[0].land(label, { text: '[Q6a]', sessionKey: 'dashboard:chat-7', message: { ts: 't9' } })
  steps[0].done(true)
  await vi.waitFor(() => expect(sentTo(frame.el).some((m) => m.type === 'stepped' && m.ok === true)).toBe(true))
  // The card answers the new show with its size again, as an app does.
  host.handle(frame, { type: 'resize', invocation: frame.invocation, width: frame.width, height: frame.height })
}

const px = (v: string) => Number.parseFloat(v)
/** Where the card is drawn. */
const drawn = (frame: PreviewFrame) => {
  const top = px(frame.el.style.top)
  const height = px(frame.el.style.height)
  return { top, height, bottom: top + height }
}

beforeEach(() => {
  steps = []
  host = new FrameHost({
    mint: async () => ({ frame: '/app-frame/labels-app/lp/f/tok', capability: 'cap', ttl: 3600 }),
    theme: () => 'light',
    onFailed() {},
    onTextPatternsOff() {},
    onEscape() {},
    onFramesChanged() {},
    onStep: (_key, _ask, hooks) => { steps.push(hooks); return () => {} },
  })
})
afterEach(() => {
  host.disposeAll()
  Reflect.deleteProperty(navigator, 'userActivation')
  document.body.replaceChildren()
  setWindow(1024, 768)
})

describe('FrameHost: a card stands clear of the message box and Crew\'s header', () => {
  it('1440 x 900: a pinned 545 px card under its label ends above the message box, shorter', async () => {
    desktop()
    const label = { left: 500, top: 227, width: 36, height: 17 }
    const frame = await card(label, 545)
    pin(frame)
    // At its wanted height it would stand at 250 to 795, over the box that starts at 777.
    expect(drawn(frame)).toEqual({ top: 250, height: 777 - MARGIN - 250, bottom: 777 - MARGIN })
    expect(frame.placement).toBe('below')
    // What the app asked for is kept: the card is that tall again once there is room.
    expect(frame.height).toBe(545)
  })

  it('1440 x 900: after a step in a long chat the card stays between the header and the box', async () => {
    desktop()
    const frame = await card({ left: 500, top: 227, width: 36, height: 17 }, 391)
    pin(frame)
    // The step centres its label: under it the whole card would stand at 486 to 877.
    const label = { left: 500, top: 463, width: 36, height: 17 }
    await stepTo(frame, label)
    const at = drawn(frame)
    expect(at.top).toBeGreaterThanOrEqual(86 + MARGIN)
    expect(at.bottom).toBeLessThanOrEqual(777 - MARGIN)
    // Beside its label, never over it: the side with more room, shortened to it.
    expect(at).toEqual({ top: 86 + MARGIN, height: 463 - GAP - (86 + MARGIN), bottom: 463 - GAP })
    expect(frame.placement).toBe('above')
  })

  it('390 x 844: a card after a step starts under the header and leaves its label in view', async () => {
    phone()
    const frame = await card({ left: 16, top: 120, width: 36, height: 17 }, 300, 'tap', 374)
    pin(frame)
    host.handle(frame, { type: 'resize', invocation: frame.invocation, width: 374, height: 560 })
    // Neither side holds 560 px, and pushed up to fit the window it would cover the
    // header and its own label.
    const label = { left: 16, top: 414, width: 36, height: 17 }
    await stepTo(frame, label)
    const at = drawn(frame)
    expect(at.top).toBe(52 + MARGIN)
    expect(at.bottom).toBe(label.top - GAP)
    expect(at.bottom).toBeLessThanOrEqual(715 - MARGIN)
  })

  it('390 x 844: a tall card a tap opened is cut to the room under its label', async () => {
    phone()
    const label = { left: 16, top: 300, width: 36, height: 17 }
    const frame = await card(label, 560, 'tap', 374)
    expect(drawn(frame)).toEqual({ top: 317 + GAP, height: 715 - MARGIN - (317 + GAP), bottom: 715 - MARGIN })
  })

  it('a hover card that is not pinned gets the same room', async () => {
    desktop()
    const frame = await card({ left: 500, top: 560, width: 36, height: 17 }, 300, 'hover')
    // Under the label it would run to 883, over the box; above it there is room for all of it.
    expect(drawn(frame)).toEqual({ top: 560 - GAP - 300, height: 300, bottom: 560 - GAP })
    expect(frame.placement).toBe('above')
  })

  it('the card is its wanted height again when the window gives it the room', async () => {
    desktop()
    const frame = await card({ left: 500, top: 227, width: 36, height: 17 }, 545)
    expect(drawn(frame).height).toBe(519)
    document.querySelector<HTMLElement>('[data-testid="input-wrapper"]')!.getBoundingClientRect = () => new DOMRect(492, 977, 784, 85)
    setWindow(1440, 1100)
    window.dispatchEvent(new Event('resize'))
    expect(drawn(frame)).toEqual({ top: 250, height: 545, bottom: 795 })
  })

  it('what Crew draws under the card (the answering bar) is kept above the message box too', async () => {
    desktop()
    const frame = await card({ left: 500, top: 227, width: 36, height: 17 }, 545)
    pin(frame)
    host.setBelow(frame, 130)
    expect(drawn(frame)).toEqual({ top: 250, height: 777 - MARGIN - 130 - 250, bottom: 777 - MARGIN - 130 })
    // Above its label, the bar still fits between the card and the label.
    host.handle(frame, { type: 'resize', invocation: frame.invocation, width: 420, height: 100 })
    await stepTo(frame, { left: 500, top: 700, width: 36, height: 17 })
    expect(drawn(frame)).toEqual({ top: 700 - GAP - 130 - 100, height: 100, bottom: 700 - GAP - 130 })
    host.setBelow(frame, 0)
    expect(drawn(frame).bottom).toBe(700 - GAP)
  })

  it('another pane\'s message box and header are not this card\'s', async () => {
    desktop()
    // A second chat beside this one, its box higher up and its title row lower.
    stand('data-pane-title-row', '', { left: 1500, top: 44, width: 400, height: 300 })
    messageBox({ left: 1500, top: 400, width: 400, height: 85 })
    const frame = await card({ left: 500, top: 227, width: 36, height: 17 }, 400)
    expect(drawn(frame)).toEqual({ top: 250, height: 400, bottom: 650 })
  })

  it('390 x 844: a pinned 580 px card ends above the Back pill and an approval card over the message box', async () => {
    phone()
    // The approval card is fused to the top of the message box, in the same pane;
    // the dock starts at its top, and the pill floats 48 px above the dock.
    const approval = stand('data-testid', 'composer-dock', { left: 16, top: 640, width: 358, height: 160 })
    const pill = stand('data-testid', 'back-to-spot', { left: 150, top: 592, width: 90, height: 34 })
    dock({ left: 0, top: 640, width: 384, height: 204 }, pill, approval)
    const label = { left: 16, top: 300, width: 36, height: 17 }
    const frame = await card(label, 580, 'tap', 374)
    pin(frame)
    const at = drawn(frame)
    expect(at.bottom).toBeLessThanOrEqual(640 - MARGIN)
    // The pill is narrower than the card and beside the link, not over it: it still bounds.
    expect(at).toEqual({ top: 317 + GAP, height: 592 - MARGIN - (317 + GAP), bottom: 592 - MARGIN })
    expect(frame.placement).toBe('below')
    expect(frame.height).toBe(580)
  })

  it('1440 x 900: a pinned 545 px card ends above the jump-to-bottom pill and an approval card', async () => {
    desktop()
    const approval = stand('data-testid', 'composer-dock', { left: 492, top: 690, width: 784, height: 172 })
    const pill = stand('data-testid', 'jump-to-bottom', { left: 873, top: 650, width: 32, height: 32 })
    dock({ left: 350, top: 690, width: 1078, height: 210 }, pill, approval)
    const label = { left: 500, top: 340, width: 36, height: 17 }
    const frame = await card(label, 545)
    pin(frame)
    expect(drawn(frame)).toEqual({ top: 357 + GAP, height: 650 - MARGIN - (357 + GAP), bottom: 650 - MARGIN })
    expect(frame.placement).toBe('below')
  })

  it('a dock grown upward by queued messages bounds the card with no pill showing', async () => {
    desktop()
    dock({ left: 350, top: 600, width: 1078, height: 300 })
    const frame = await card({ left: 500, top: 227, width: 36, height: 17 }, 545)
    expect(drawn(frame)).toEqual({ top: 250, height: 600 - MARGIN - 250, bottom: 600 - MARGIN })
  })

  it('a dock with no height, and another pane\'s dock and pill, leave the message box as the bound', async () => {
    desktop()
    dock({ left: 350, top: 900, width: 1078, height: 0 }, stand('data-testid', 'jump-to-bottom', { left: 873, top: 400, width: 32, height: 32 }))
    dock({ left: 1500, top: 300, width: 400, height: 600 }, stand('data-testid', 'back-to-spot', { left: 1650, top: 252, width: 90, height: 34 }))
    const frame = await card({ left: 500, top: 227, width: 36, height: 17 }, 545)
    expect(drawn(frame)).toEqual({ top: 250, height: 777 - MARGIN - 250, bottom: 777 - MARGIN })
  })

  it('a card the person dragged keeps the place they gave it', async () => {
    desktop()
    const frame = await card({ left: 500, top: 227, width: 36, height: 17 }, 545)
    pin(frame)
    host.handle(frame, { type: 'drag', invocation: frame.invocation, dx: 0, dy: 400 })
    expect(drawn(frame)).toEqual({ top: 900 - MARGIN - 545, height: 545, bottom: 900 - MARGIN })
  })

  it('with no header and no message box on the page, the window alone bounds the card', async () => {
    setWindow(1440, 900)
    const frame = await card({ left: 500, top: 463, width: 36, height: 17 }, 391)
    expect(drawn(frame)).toEqual({ top: 486, height: 391, bottom: 877 })
    host.handle(frame, { type: 'resize', invocation: frame.invocation, width: 420, height: 2000 })
    // Neither side holds 800 px: the wider side, cut to it.
    expect(drawn(frame)).toEqual({ top: MARGIN, height: 463 - GAP - MARGIN, bottom: 463 - GAP })
  })
})

describe('FrameHost: a pinned card and the answering bar hung under it, three driven layouts', () => {
  const BAR = 164
  /** 1440 x 900 as driven: the header ends at 132, the dock starts at 766, the message box at 776. */
  function driven(...inDock: HTMLElement[]) {
    setWindow(1440, 900)
    header({ left: 334, top: 42, width: 1100, height: 90 })
    messageBox({ left: 492, top: 776, width: 784, height: 88 })
    dock({ left: 334, top: 766, width: 1100, height: 134 }, ...inDock)
  }
  /** Where Crew draws its bar: from the card's last px down. */
  const barOf = (frame: PreviewFrame) => { const top = drawn(frame).bottom - 1; return { top, bottom: top + BAR } }

  it('label at 279: the card is cut under its label so the bar ends above the dock', async () => {
    driven()
    const frame = await card({ left: 500, top: 279, width: 36, height: 18 }, 332)
    pin(frame)
    host.setBelow(frame, BAR)
    // Uncut the card stood at 303 to 635 and the bar ran to 798, over the box at 776.
    expect(drawn(frame)).toEqual({ top: 303, height: 291, bottom: 594 })
    expect(barOf(frame)).toEqual({ top: 593, bottom: 757 })
    expect(barOf(frame).bottom).toBeLessThanOrEqual(766 - MARGIN)
    expect(drawn(frame).top).toBeGreaterThanOrEqual(132 + MARGIN)
  })

  it('after a step, label at 455: card and bar stand above the label, under the header, clear of the Back pill', async () => {
    driven(stand('data-testid', 'back-to-spot', { left: 725, top: 718, width: 318, height: 34 }))
    const frame = await card({ left: 500, top: 279, width: 36, height: 18 }, 253)
    pin(frame)
    host.setBelow(frame, BAR)
    const label = { left: 500, top: 455, width: 36, height: 18 }
    await stepTo(frame, label)
    // Under the label the card stood at 479 to 732 and the bar ran to 895, over the box and the pill.
    expect(drawn(frame)).toEqual({ top: 132 + MARGIN, height: 145, bottom: 285 })
    expect(frame.placement).toBe('above')
    expect(barOf(frame)).toEqual({ top: 284, bottom: 448 })
    expect(barOf(frame).bottom).toBeLessThanOrEqual(label.top - GAP)
    expect(barOf(frame).bottom).toBeLessThanOrEqual(718 - MARGIN)
  })

  it('label at 677: the card goes above and the bar ends above the label, not on it', async () => {
    driven(stand('data-testid', 'jump-to-bottom', { left: 868, top: 726, width: 32, height: 32 }))
    const label = { left: 500, top: 677, width: 36, height: 18 }
    const frame = await card(label, 217)
    pin(frame)
    host.setBelow(frame, BAR)
    // With the bar not counted the card stood at 454 to 671 and the bar at 670 to 834, on the label and the box.
    expect(drawn(frame)).toEqual({ top: 290, height: 217, bottom: 507 })
    expect(frame.placement).toBe('above')
    expect(barOf(frame)).toEqual({ top: 506, bottom: 670 })
    expect(barOf(frame).bottom).toBeLessThanOrEqual(label.top - GAP)
    expect(drawn(frame).top).toBeGreaterThanOrEqual(132 + MARGIN)
  })
})
