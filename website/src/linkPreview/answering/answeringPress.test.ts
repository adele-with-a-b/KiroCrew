import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { FrameHost, type PreviewFrame, type ShowScope, type StepRequest } from '../linkPreviewFrames'
import { readFrameMessage, type MessageRef } from '../linkPreviewProtocol'
import { APP_FRAME_TAG } from '../../appFrame/frameProtocol'
import { installFrameWindows, sentTo } from '../../test/LinkPreviewFramesTestUtils'
import { AnsweringMode, RELAYED_PRESS_MS } from './answeringMode'
import { setAnswersPage } from './answersPage'

// The person's own presses are the events `press()` makes; every other event is
// script-made, as one a page script dispatches would be.
const real = vi.hoisted(() => new WeakSet<Event>())
vi.mock('./personPress', () => ({ isPersonPress: (e: Event) => real.has(e) }))

installFrameWindows()

const KEY = 'cards-app:cards'
const OTHER = 'other-app:cards'
const SCOPE: ShowScope = { slot: 'chat-7', origin: { ts: 't1' }, messages: true }
const rect = (top: number) => ({ left: 100, top, width: 40, height: 16 })

let host: FrameHost
let mode: AnsweringMode
let stop: () => void
let clearPage: () => void
let now: number
let steps: StepRequest[]
let jumps: MessageRef[]

beforeEach(() => {
  now = 5_000
  steps = []
  jumps = []
  mode = new AnsweringMode({
    granted: () => true,
    appTitle: (key) => key,
    post: (frame, msg) => frame.el.contentWindow?.postMessage({ [APP_FRAME_TAG]: 1, ...msg }, '*'),
    now: () => now,
  })
  stop = mode.start()
  host = new FrameHost({
    mint: async () => ({ frame: '/app-frame/cards-app/lp/f/tok', capability: 'cap', ttl: 3600 }),
    theme: () => 'light',
    onFailed() {},
    onTextPatternsOff() {},
    onEscape() {},
    onFramesChanged() {},
    onStep: (_key, ask) => { steps.push(ask); return () => {} },
    onJump: (_app, target) => { jumps.push(target) },
    onAnswering: (frame, msg, gate, reply) => mode.onFrameMessage(frame, msg, gate, reply),
    onCardGone: (frame) => mode.cardGone(frame),
    relayedPress: (frame) => mode.takePress(frame),
  })
  clearPage = setAnswersPage({ slot: 'chat-7', chatTitle: 'garden-log', send: async () => ({ kind: 'sent' }) })
})
afterEach(() => {
  stop()
  clearPage()
  host.disposeAll()
  Reflect.deleteProperty(navigator, 'userActivation')
  document.body.replaceChildren()
})

/** A ready, shown, opened and pinned card of `key`. */
async function pinnedCard(key = KEY, app = 'cards-app'): Promise<PreviewFrame> {
  const frame = host.hoverFrame(key, app)
  host.handle(frame, { type: 'ready' })
  await frame.ready
  expect(await host.show(frame, { text: '[Q4a]' }, rect(50), undefined, { trigger: 'click', scope: SCOPE })).toBe(true)
  host.handle(frame, { type: 'open', invocation: frame.invocation })
  host.handle(frame, { type: 'pin', invocation: frame.invocation, pinned: true })
  return frame
}
/** The person clicked in `frame`: a live activation with focus in the card. */
function clickedIn(frame: PreviewFrame) {
  Object.defineProperty(navigator, 'userActivation', { configurable: true, value: { isActive: true } })
  frame.el.tabIndex = 0
  frame.el.focus()
}
/** Focus is in Crew's own page, as it is while the person answers in Crew's line:
 *  the browser's activation is live (they are typing) but not in any card. */
function focusInCrewsLine() {
  const line = document.createElement('input')
  document.body.appendChild(line)
  line.focus()
}
/** A frame message as the frame posts it, read the way the host reads one. */
function fromFrame(frame: PreviewFrame, m: Record<string, unknown>) {
  const msg = readFrameMessage({ [APP_FRAME_TAG]: 1, invocation: frame.invocation, ...m })
  if (msg) host.handle(frame, msg)
}
const SETUP = { place: 'Q4a · 2 of 3', keys: [{ key: 'Tab', label: 'next' }], buttons: [{ id: 'later', label: 'Later' }] }
/** `frame`'s mode is on and focus has moved to Crew's line. */
async function modeOn(): Promise<PreviewFrame> {
  const frame = await pinnedCard()
  clickedIn(frame)
  fromFrame(frame, { type: 'answering', op: 'enter', request: 1, ...SETUP })
  expect(mode.state?.frame).toBe(frame)
  focusInCrewsLine()
  return frame
}
let requests = 100
const step = (frame: PreviewFrame) => {
  requests += 1
  fromFrame(frame, { type: 'step', ts: 't9', id: 'm9', text: '[Q6a]', request: requests })
}
const jump = (frame: PreviewFrame) => fromFrame(frame, { type: 'jump', ts: 't9', id: 'm9' })
/** Why `frame`'s steps were refused at the gate (`replaced` is a taken step a newer one ended). */
const refusals = (frame: PreviewFrame) =>
  sentTo(frame.el).filter((m) => m.type === 'stepped' && m.ok === false && m.reason !== 'replaced').map((m) => m.reason)
/** The person's own key or click on Crew's page. */
const press = () => { const e = new Event('keydown'); real.add(e); return e }

describe('the answering mode: a press Crew relayed lets its card move', () => {
  it('Enter, a declared key and a button in Crew’s line each let the card step, with focus in Crew’s line', async () => {
    const frame = await modeOn()
    mode.setLine('yes')
    mode.keep(press())
    step(frame)
    expect(mode.key('Tab', press())).toBe(true)
    step(frame)
    mode.button('later', press())
    step(frame)
    expect(refusals(frame)).toEqual([])
    expect(steps).toHaveLength(3)
    expect(steps[0]).toMatchObject({ slot: 'chat-7', target: { ts: 't9', id: 'm9' }, text: '[Q6a]' })
  })

  it('and lets it jump the chat', async () => {
    const frame = await modeOn()
    expect(mode.key('Tab', press())).toBe(true)
    jump(frame)
    expect(jumps).toEqual([{ ts: 't9', id: 'm9' }])
  })

  it('one press is one move: a second step or jump from it is refused, in either order', async () => {
    const frame = await modeOn()
    mode.keep(press())
    step(frame)
    step(frame)
    jump(frame)
    expect(steps).toHaveLength(1)
    expect(refusals(frame)).toEqual(['no_click'])
    expect(jumps).toEqual([])

    mode.keep(press())
    jump(frame)
    jump(frame)
    step(frame)
    expect(jumps).toHaveLength(1)
    expect(steps).toHaveLength(1)
    expect(refusals(frame)).toEqual(['no_click', 'no_click'])
  })

  it('two presses before the card moves are still one move', async () => {
    const frame = await modeOn()
    mode.keep(press())
    expect(mode.key('Tab', press())).toBe(true)
    step(frame)
    step(frame)
    expect(steps).toHaveLength(1)
    expect(refusals(frame)).toEqual(['no_click'])
  })

  it('a step refused for another reason still uses the press up', async () => {
    const frame = await modeOn()
    mode.keep(press())
    fromFrame(frame, { type: 'step', invocation: frame.invocation - 1, ts: 't9', text: '[Q6a]', request: 7 })
    step(frame)
    expect(refusals(frame)).toEqual(['not_shown', 'no_click'])
    expect(steps).toEqual([])
  })

  it('it lapses: good up to the bound, gone a millisecond past it, and never with a clock that ran back', async () => {
    const frame = await modeOn()
    mode.keep(press())
    now += RELAYED_PRESS_MS
    step(frame)
    expect(steps).toHaveLength(1)

    mode.keep(press())
    now += RELAYED_PRESS_MS + 1
    step(frame)
    mode.keep(press())
    now -= 1
    jump(frame)
    expect(steps).toHaveLength(1)
    expect(jumps).toEqual([])
    expect(refusals(frame)).toEqual(['no_click'])
    // A lapsed press is gone, not stored: nothing is left for a later ask.
    step(frame)
    expect(refusals(frame)).toEqual(['no_click', 'no_click'])
  })

  it('typing in Crew’s line is not a press: a draft lets nothing move', async () => {
    const frame = await modeOn()
    mode.setLine('y')
    mode.setLine('ye')
    step(frame)
    jump(frame)
    expect(refusals(frame)).toEqual(['no_click'])
    expect(steps).toEqual([])
    expect(jumps).toEqual([])
  })
})

describe('the answering mode: what never makes a press', () => {
  it('no message from the card does, whatever it says', async () => {
    const frame = await modeOn()
    const shapes: Record<string, unknown>[] = [
      { type: 'answering', op: 'update', place: 'pressed', pressed: true, press: 1 },
      { type: 'answering', op: 'hand', text: 'Q1a go', count: 1, rev: mode.state?.lineRev, pressed: true },
      { type: 'answering', op: 'enter', ...SETUP, pressed: true },
      { type: 'answering', op: 'kept', text: 'x' }, { type: 'answering', op: 'key', key: 'Tab' },
      { type: 'answering', op: 'button', id: 'later' }, { type: 'answering', op: 'press' },
      { type: 'answering', event: { kind: 'key', key: 'Tab' } }, { type: 'key', key: 'Tab' },
      { type: 'keydown', key: 'Enter' }, { type: 'click' }, { type: 'pressed' },
    ]
    for (const s of shapes) {
      fromFrame(frame, s)
      step(frame)
      jump(frame)
    }
    expect(steps).toEqual([])
    expect(jumps).toEqual([])
    expect(refusals(frame)).toEqual(shapes.map(() => 'no_click'))
    expect(mode.takePress(frame)).toBe(false)
  })

  it('a script-made event does not, and neither does a relay with no event', async () => {
    const frame = await modeOn()
    mode.keep(new Event('keydown'))
    step(frame)
    expect(mode.key('Tab', new KeyboardEvent('keydown', { key: 'Tab' }))).toBe(true)
    step(frame)
    mode.button('later', new MouseEvent('click'))
    step(frame)
    mode.keep()
    expect(mode.key('Tab')).toBe(true)
    mode.button('later')
    step(frame)
    jump(frame)
    expect(steps).toEqual([])
    expect(jumps).toEqual([])
    expect(refusals(frame)).toEqual(['no_click', 'no_click', 'no_click', 'no_click'])
  })

  it('a key the card did not declare, or a button it did not ask for, is not relayed and makes none', async () => {
    const frame = await modeOn()
    expect(mode.key('Ctrl+L', press())).toBe(false)
    mode.button('nope', press())
    step(frame)
    expect(refusals(frame)).toEqual(['no_click'])
  })

  it('another card cannot use it, and its ask leaves the press for the card it was relayed to', async () => {
    const frame = await modeOn()
    const other = await pinnedCard(OTHER, 'other-app')
    focusInCrewsLine()
    mode.keep(press())
    fromFrame(other, { type: 'step', ts: 't9', text: '[Q6a]', request: 3 })
    jump(other)
    expect(refusals(other)).toEqual(['no_click'])
    expect(steps).toEqual([])
    expect(jumps).toEqual([])
    step(frame)
    expect(steps).toHaveLength(1)
  })

  it.each([
    ['Escape', (f: PreviewFrame) => { void f; mode.escape() }],
    ['the card’s leave', (f: PreviewFrame) => fromFrame(f, { type: 'answering', op: 'leave' })],
    ['the chat on screen changing', (f: PreviewFrame) => { void f; clearPage(); clearPage = setAnswersPage({ slot: 'chat-9', chatTitle: 'other', send: async () => ({ kind: 'sent' }) }) }],
  ])('leaving the mode by %s clears it', async (_name, leave) => {
    const frame = await modeOn()
    mode.keep(press())
    leave(frame)
    expect(mode.state).toBeNull()
    step(frame)
    jump(frame)
    expect(refusals(frame)).toEqual(['no_click'])
    expect(steps).toEqual([])
    expect(jumps).toEqual([])
  })

  it('a press does not carry into a mode entered again', async () => {
    const frame = await modeOn()
    mode.keep(press())
    clickedIn(frame)
    fromFrame(frame, { type: 'answering', op: 'enter', request: 2, ...SETUP })
    focusInCrewsLine()
    step(frame)
    expect(refusals(frame)).toEqual(['no_click'])
  })

  it('it never starts the mode: enter still needs the click in the card, and leaves the press alone', async () => {
    const frame = await modeOn()
    mode.keep(press())
    fromFrame(frame, { type: 'answering', op: 'enter', request: 9, ...SETUP })
    expect(sentTo(frame.el).filter((m) => m.type === 'answered' && m.request === 9)).toEqual([
      expect.objectContaining({ ok: false, reason: 'no_click' }),
    ])
    step(frame)
    expect(steps).toHaveLength(1)
  })
})

describe('outside the answering mode the gate is as it was', () => {
  it('no mode: a step or jump needs the click in the card, and a click moves it as before', async () => {
    const frame = await pinnedCard()
    focusInCrewsLine()
    step(frame)
    jump(frame)
    expect(refusals(frame)).toEqual(['no_click'])
    expect(steps).toEqual([])
    expect(jumps).toEqual([])

    clickedIn(frame)
    step(frame)
    step(frame)
    jump(frame)
    jump(frame)
    expect(steps).toHaveLength(2)
    expect(jumps).toHaveLength(2)
    expect(refusals(frame)).toEqual(['no_click'])
  })

  it('a host with no answering mode asks nothing and refuses as before', async () => {
    host.disposeAll()
    host = new FrameHost({
      mint: async () => ({ frame: '/app-frame/cards-app/lp/f/tok', capability: 'cap', ttl: 3600 }),
      theme: () => 'light',
      onFailed() {},
      onTextPatternsOff() {},
      onEscape() {},
      onFramesChanged() {},
      onStep: (_key, ask) => { steps.push(ask); return () => {} },
      onJump: (_app, target) => { jumps.push(target) },
    })
    const frame = await pinnedCard()
    focusInCrewsLine()
    step(frame)
    jump(frame)
    expect(refusals(frame)).toEqual(['no_click'])
    expect(steps).toEqual([])
    expect(jumps).toEqual([])
  })

  it('in the mode, a click in the card still moves it with no press relayed', async () => {
    const frame = await modeOn()
    clickedIn(frame)
    step(frame)
    jump(frame)
    expect(steps).toHaveLength(1)
    expect(jumps).toHaveLength(1)
  })
})
