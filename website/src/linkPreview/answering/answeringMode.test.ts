import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { FrameHost, type PreviewFrame, type ShowScope } from '../linkPreviewFrames'
import { readFrameMessage, type FromFrame } from '../linkPreviewProtocol'
import { APP_FRAME_TAG } from '../../appFrame/frameProtocol'
import { installFrameWindows, sentTo } from '../../test/LinkPreviewFramesTestUtils'
import { AnsweringMode } from './answeringMode'
import { setAnswersPage, type AnswersSend, type AnswersSendOutcome } from './answersPage'
import { keyName, readAnsweringMessage } from './answeringProtocol'

installFrameWindows()

const KEY = 'cards-app:cards'
const OTHER = 'other-app:cards'
const SCOPE: ShowScope = { slot: 'chat-7', origin: { ts: 't1' }, messages: true }
const rect = (top: number) => ({ left: 100, top, width: 40, height: 16 })

let host: FrameHost
let mode: AnsweringMode
let stop: () => void
let clearPage: () => void
let granted: Set<string>
let sends: AnswersSend[]
let answer: () => Promise<AnswersSendOutcome>
let replies: unknown[]

function page(slot = 'chat-7') {
  clearPage()
  clearPage = setAnswersPage({
    slot,
    chatTitle: 'garden-log',
    send: (req) => { sends.push(req); return answer() },
  })
}

beforeEach(() => {
  sends = []
  replies = []
  granted = new Set([KEY])
  answer = async () => ({ kind: 'sent' })
  clearPage = () => {}
  mode = new AnsweringMode({
    granted: (key) => granted.has(key),
    appTitle: (key) => (key === KEY ? 'Answer Cards' : 'Other'),
    post: (frame, msg) => frame.el.contentWindow?.postMessage({ [APP_FRAME_TAG]: 1, ...msg }, '*'),
  })
  stop = mode.start()
  host = new FrameHost({
    mint: async () => ({ frame: '/app-frame/cards-app/lp/f/tok', capability: 'cap', ttl: 3600 }),
    theme: () => 'light',
    onFailed() {},
    onTextPatternsOff() {},
    onEscape() {},
    onFramesChanged() {},
    onAnswering: (frame, msg, gate, reply) => mode.onFrameMessage(frame, msg, gate, (r) => { replies.push(r); reply(r) }),
    onCardGone: (frame) => mode.cardGone(frame),
  })
  page()
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
function clickedIn(frame: PreviewFrame) {
  Object.defineProperty(navigator, 'userActivation', { configurable: true, value: { isActive: true } })
  frame.el.tabIndex = 0
  frame.el.focus()
}
/** A frame message as the frame posts it, read the way the host reads one. */
function fromFrame(frame: PreviewFrame, m: Record<string, unknown>) {
  const msg = readFrameMessage({ [APP_FRAME_TAG]: 1, invocation: frame.invocation, ...m })
  if (msg) host.handle(frame, msg)
}
const enter = (frame: PreviewFrame, extra: Record<string, unknown> = {}) =>
  fromFrame(frame, { type: 'answering', op: 'enter', request: 1, place: 'Q4a · 2 of 3', hint: 'go', keys: [{ key: 'Tab', label: 'go' }], ...extra })
const hand = (frame: PreviewFrame, text: unknown, count: unknown, rev = mode.state?.lineRev ?? 0) =>
  fromFrame(frame, { type: 'answering', op: 'hand', request: 2, text, count, rev })
const events = (frame: PreviewFrame) => sentTo(frame.el).filter((m) => m.type === 'answering').map((m) => m.event)
const flush = () => new Promise((r) => setTimeout(r, 0))

async function onWithText(text = 'Q1a yes, after lunch. Q6a skip.', count = 2) {
  const frame = await pinnedCard()
  clickedIn(frame)
  enter(frame)
  expect(mode.state?.slot).toBe('chat-7')
  hand(frame, text, count)
  expect(mode.state?.handed?.text).toBe(text)
  return frame
}

describe('readAnsweringMessage', () => {
  it('reads a setup: declared keys from the allowed set only, short labels, buttons, keepAt', () => {
    const m = readAnsweringMessage({
      op: 'enter',
      place: ' Q4a ·\n 2 of 3 ',
      hint: 'x'.repeat(121),
      keys: [{ key: 'Tab', label: 'go' }, { key: 'Enter' }, { key: 'Shift+Enter' }, { key: 'Escape' }, { key: 'L' },
        { key: 'Meta+L' }, { key: 'Ctrl+L', label: 'later' }, { key: 'Ctrl+Alt+R' }, { key: 'Tab' }, { key: 'ArrowUp' }],
      buttons: [{ id: 'go', label: 'go', hint: '⇥' }, { id: '', label: 'x' }, { id: 'later', label: '' }, { id: 'go', label: 'dup' }],
      keepAt: 7,
    }, 3)
    expect(m).toEqual({
      type: 'answering', op: 'enter', invocation: 3,
      setup: {
        place: 'Q4a · 2 of 3', hint: '',
        // ArrowUp is the tenth entry: only the first eight are read.
        keys: [{ key: 'Tab', label: 'go' }, { key: 'Ctrl+L', label: 'later' }, { key: 'Ctrl+Alt+R', label: '' }],
        buttons: [{ id: 'go', label: 'go', hint: '⇥' }],
        keepAt: 1,
      },
    })
  })

  it('names a key press by its place on the keyboard, and never Enter, Escape or Meta', () => {
    const k = (key: string, code: string, mods: Partial<Record<'ctrlKey' | 'altKey' | 'shiftKey' | 'metaKey', boolean>> = {}) =>
      keyName({ key, code, ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, ...mods })
    expect(k('ø', 'KeyO', { altKey: true })).toBe('Alt+O')
    expect(k('R', 'KeyR', { ctrlKey: true, altKey: true, shiftKey: true })).toBe('Ctrl+Alt+Shift+R')
    expect(k('Tab', 'Tab', { shiftKey: true })).toBe('Shift+Tab')
    expect(k('l', 'KeyL')).toBeNull()
    expect(k('Enter', 'Enter', { shiftKey: true })).toBeNull()
    expect(k('Escape', 'Escape')).toBeNull()
    expect(k('l', 'KeyL', { metaKey: true, ctrlKey: true })).toBeNull()
  })
})

describe('AnsweringMode: entering', () => {
  it('a pinned, granted card on the chat on screen enters on a click in it; the frame hears its answer', async () => {
    const frame = await pinnedCard()
    clickedIn(frame)
    enter(frame)
    expect(replies).toEqual([true])
    expect(sentTo(frame.el).filter((m) => m.type === 'answered')).toEqual([expect.objectContaining({ request: 1, ok: true })])
    expect(mode.state).toMatchObject({ key: KEY, appTitle: 'Answer Cards', slot: 'chat-7', chatTitle: 'garden-log', line: '' })
  })

  it('an app without the grant cannot enter the mode', async () => {
    granted.clear()
    const frame = await pinnedCard()
    clickedIn(frame)
    enter(frame)
    expect(replies).toEqual(['not_granted'])
    expect(mode.state).toBeNull()
  })

  it('no click in the card, an unpinned card or another chat on screen: no mode', async () => {
    const frame = await pinnedCard()
    enter(frame)
    host.handle(frame, { type: 'pin', invocation: frame.invocation, pinned: false })
    clickedIn(frame)
    enter(frame)
    host.handle(frame, { type: 'pin', invocation: frame.invocation, pinned: true })
    page('chat-9')
    enter(frame)
    expect(replies).toEqual(['no_click', 'not_pinned', 'not_on_screen'])
    expect(mode.state).toBeNull()
  })
})

describe('AnsweringMode: the hard rule, a frame never sends', () => {
  it('no frame message of any shape sends, whatever the mode holds', async () => {
    const frame = await onWithText()
    const shapes: Record<string, unknown>[] = [
      { type: 'answering', op: 'send' }, { type: 'answering', op: 'press' }, { type: 'answering', op: 'keep' },
      { type: 'answering', op: 'hand', text: 'Q1a go', count: 1, rev: mode.state?.lineRev, send: true },
      { type: 'answering', op: 'enter', send: true, shift: true, key: 'Enter' },
      { type: 'answering', op: 'update', place: 'sent', keepAt: 0 },
      { type: 'answering', op: 'leave' },
      { type: 'key', key: 'Enter', shift: true }, { type: 'keydown', key: 'Enter', shiftKey: true },
      { type: 'send', text: 'Q1a go' }, { type: 'escape' }, { type: 'open' }, { type: 'close' },
      { type: 'step', ts: 't9', text: '[Q6a]' }, { type: 'jump', ts: 't9' },
      { type: 'openPanelTab', tab: 'x', payload: { send: true } },
      { type: 'openSessionControl', control: 'x', payload: { text: 'Q1a go' } },
      { type: 'regions', rects: [] }, { type: 'textPatternsOff', ids: [] }, { type: 'ready' }, { type: 'failed' },
    ]
    clickedIn(frame)
    for (const s of shapes) {
      fromFrame(frame, s)
      enter(frame)
      hand(frame, 'Q1a go', 1)
    }
    // And straight to the mode, unread shapes included.
    for (const op of ['enter', 'update', 'hand', 'leave', 'send', 'press']) {
      mode.onFrameMessage(frame, { type: 'answering', op, invocation: frame.invocation, setup: {}, text: 'x', count: 1, rev: 0 } as never, null, () => {})
    }
    await flush()
    expect(sends).toEqual([])
  })
})

describe('AnsweringMode: the one press', () => {
  it('sends the handed text byte for byte, once, with its count and the app, then ends the mode as sent', async () => {
    const text = 'Q1a yes, after lunch.\nQ6a skip — «ok» 🌱'
    const frame = await onWithText(text, 2)
    await mode.pressSend()
    expect(sends).toEqual([{ text, count: 2, app: 'Answer Cards' }])
    expect(mode.state).toBeNull()
    expect(events(frame).at(-1)).toEqual({ kind: 'ended', why: 'sent' })
  })

  it('a double press, or a press while sending, sends once', async () => {
    let release: (o: AnswersSendOutcome) => void = () => {}
    answer = () => new Promise((r) => { release = r })
    await onWithText()
    const first = mode.pressSend()
    void mode.pressSend()
    expect(mode.state?.status).toBe('sending')
    void mode.pressSend()
    release({ kind: 'sent' })
    await first
    await mode.pressSend()
    expect(sends).toHaveLength(1)
  })

  it('with no answer kept it sends nothing and says so', async () => {
    const frame = await pinnedCard()
    clickedIn(frame)
    enter(frame)
    await mode.pressSend()
    expect(mode.state?.note).toBe('nothing')
    hand(frame, '', 0)
    await mode.pressSend()
    expect(sends).toEqual([])
    expect(mode.state?.note).toBe('nothing')
  })

  it('a message built from an older line is not sent: the line changed after it was handed', async () => {
    const frame = await onWithText('Q1a go', 1)
    mode.setLine('go, but')
    await mode.pressSend()
    expect(sends).toEqual([])
    expect(mode.state?.note).toBe('updating')
    hand(frame, 'Q1a go. Q4a go, but', 2, (mode.state?.lineRev ?? 0) - 1)
    expect(replies.at(-1)).toBe('stale')
    hand(frame, 'Q1a go. Q4a go, but', 2)
    await mode.pressSend()
    expect(sends).toEqual([{ text: 'Q1a go. Q4a go, but', count: 2, app: 'Answer Cards' }])
  })

  it('a refused send keeps the mode and its text; nothing went out', async () => {
    answer = async () => ({ kind: 'refused', reason: 'offline' })
    await onWithText('Q1a go', 1)
    await mode.pressSend()
    expect(mode.state).toMatchObject({ status: 'on', note: 'refused', refusal: 'offline', handed: { text: 'Q1a go' } })
  })

  it('refuses text that would act as more than text, and text over the cap', async () => {
    const frame = await onWithText('Q1a go', 1)
    hand(frame, '/clear everything', 1)
    hand(frame, 'see @secrets', 1)
    hand(frame, 'x'.repeat(32_001), 1)
    hand(frame, 'Q1a go', 1.5)
    expect(replies.slice(-4)).toEqual(['not_literal', 'not_literal', 'too_large', 'bad_count'])
    expect(mode.state?.handed?.text).toBe('Q1a go')
  })
})

describe('AnsweringMode: Crew’s line and keys go to the card', () => {
  it('typing, Enter, a declared key and a button are told to the card; an undeclared key is not', async () => {
    const frame = await onWithText()
    mode.setLine('go, but')
    mode.keep()
    expect(mode.key('Tab')).toBe(true)
    expect(mode.key('Ctrl+L')).toBe(false)
    mode.button('nope')
    const lineRev = mode.state?.lineRev
    expect(events(frame)).toEqual([
      { kind: 'draft', text: 'go, but', rev: (lineRev ?? 0) - 1 },
      { kind: 'kept', text: 'go, but', rev: lineRev },
      { kind: 'key', key: 'Tab' },
    ])
    expect(mode.state?.line).toBe('')
  })
})

describe('AnsweringMode: who may touch it', () => {
  it('a second app cannot enter, hand text into or end this chat’s mode', async () => {
    const frame = await onWithText('Q1a go', 1)
    granted.add(OTHER)
    const other = await pinnedCard(OTHER, 'other-app')
    clickedIn(other)
    enter(other)
    hand(other, 'Q9z steal', 5, mode.state?.lineRev)
    fromFrame(other, { type: 'answering', op: 'leave' })
    expect(replies.slice(-2)).toEqual(['busy', 'not_on'])
    expect(mode.state).toMatchObject({ key: KEY, handed: { text: 'Q1a go', count: 1 } })
    await mode.pressSend()
    expect(sends).toEqual([{ text: 'Q1a go', count: 1, app: 'Answer Cards' }])
    expect(events(other)).toEqual([])
    expect(events(frame).at(-1)).toEqual({ kind: 'ended', why: 'sent' })
  })
})

describe('AnsweringMode: it ends with nothing sent', () => {
  it('on Escape', async () => {
    const frame = await onWithText()
    expect(mode.escape()).toBe(true)
    await mode.pressSend()
    expect(sends).toEqual([])
    expect(events(frame).at(-1)).toEqual({ kind: 'ended', why: 'escaped' })
  })

  it('when the chat on screen changes', async () => {
    const frame = await onWithText()
    page('chat-9')
    await mode.pressSend()
    expect(sends).toEqual([])
    expect(events(frame).at(-1)).toEqual({ kind: 'ended', why: 'chat' })
  })

  it.each([
    ['the card closes', (h: FrameHost, f: PreviewFrame) => h.handle(f, { type: 'close', invocation: f.invocation })],
    ['the card is unpinned', (h: FrameHost, f: PreviewFrame) => h.handle(f, { type: 'pin', invocation: f.invocation, pinned: false })],
    ['the frame goes', (h: FrameHost) => h.disposeKey(KEY)],
    ['the card turns into an overlay', (h: FrameHost, f: PreviewFrame) => h.handle(f, { type: 'regions', rects: [] } as FromFrame)],
  ])('when %s', async (_name, act) => {
    const frame = await onWithText()
    act(host, frame)
    expect(mode.state).toBeNull()
    await mode.pressSend()
    expect(sends).toEqual([])
  })

  it('a chat change while a send is in flight ends the mode once it is answered, as sent', async () => {
    let release: (o: AnswersSendOutcome) => void = () => {}
    answer = () => new Promise((r) => { release = r })
    const frame = await onWithText()
    const press = mode.pressSend()
    page('chat-9')
    expect(mode.state?.status).toBe('sending')
    release({ kind: 'sent' })
    await press
    expect(mode.state).toBeNull()
    expect(sends).toHaveLength(1)
    expect(events(frame).filter((e) => (e as { kind: string }).kind === 'ended')).toEqual([{ kind: 'ended', why: 'sent' }])
    vi.restoreAllMocks()
  })
})
