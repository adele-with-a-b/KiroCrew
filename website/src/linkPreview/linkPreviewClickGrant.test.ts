import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { FrameHost, type PreviewFrame, type ShowScope } from './linkPreviewFrames'
import type { FromFrame, ShowTrigger } from './linkPreviewProtocol'
import { installFrameWindows, sentTo } from '../test/LinkPreviewFramesTestUtils'

installFrameWindows()

const KEY = 'refs-app:cards'
const SCOPE: ShowScope = { slot: 'chat-7', origin: { ts: 't' }, messages: true }
const rect = (top: number) => ({ left: 100, top, width: 40, height: 16 })

let opened: unknown[][] = []
let host: FrameHost

function hostOptions(): ConstructorParameters<typeof FrameHost>[0] {
  return {
    mint: async () => ({ frame: '/app-frame/refs-app/lp/f/tok', capability: 'cap', ttl: 3600 }),
    theme: () => 'light',
    onFailed() {},
    onTextPatternsOff() {},
    onEscape() {},
    onFramesChanged() {},
    onOpen: (...args) => { opened.push(args); return true },
  }
}
function makeHost() {
  return new FrameHost(hostOptions())
}

/** A ready hover frame. */
async function readyFrame(): Promise<PreviewFrame> {
  const frame = host.hoverFrame(KEY, 'refs-app')
  host.handle(frame, { type: 'ready' })
  await frame.ready
  return frame
}

/** Show the label in `frame` by `trigger`, and have the card open itself. */
async function showOpen(frame: PreviewFrame, trigger: ShowTrigger, top = 50) {
  expect(await host.show(frame, { text: '[Q4a]' }, rect(top), undefined, { trigger, scope: SCOPE })).toBe(true)
  host.handle(frame, { type: 'open', invocation: frame.invocation })
}
const askTab = (frame: PreviewFrame, invocation = frame.invocation) =>
  host.handle(frame, { type: 'openPanelTab', invocation, tab: 'questions', payload: 1 } as FromFrame)
const lastShow = (frame: PreviewFrame) => sentTo(frame.el).filter((m) => m.type === 'show').at(-1)

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  opened = []
  host = makeHost()
})
afterEach(() => {
  host.disposeAll()
  document.body.replaceChildren()
  vi.useRealTimers()
})

describe('FrameHost: the click grant', () => {
  it('a click show opens one tab without a click in the card', async () => {
    const frame = await readyFrame()
    await showOpen(frame, 'click')
    askTab(frame)
    askTab(frame)
    expect(opened).toEqual([['refs-app', { kind: 'panelTab', id: 'questions' }, 1, SCOPE]])
  })

  it('leave, dismissal and the next show each end it', async () => {
    const frame = await readyFrame()
    await showOpen(frame, 'click')
    host.leave(KEY)
    askTab(frame)
    await showOpen(frame, 'tap')
    host.hideUnpinned('escape')
    host.handle(frame, { type: 'open', invocation: frame.invocation })
    askTab(frame)
    await showOpen(frame, 'click')
    const first = frame.invocation
    await showOpen(frame, 'hover')
    askTab(frame, first)
    askTab(frame)
    expect(opened).toEqual([])
  })

  it('a card that pins itself keeps no grant; an unpin of a hover card keeps it', async () => {
    const frame = await readyFrame()
    await showOpen(frame, 'click')
    host.handle(frame, { type: 'pin', invocation: frame.invocation, pinned: false })
    askTab(frame)
    expect(opened).toHaveLength(1)
    await showOpen(frame, 'click')
    host.handle(frame, { type: 'pin', invocation: frame.invocation, pinned: true })
    askTab(frame)
    expect(opened).toHaveLength(1)
  })

  it('a late ask from an earlier show leaves the latest click its tab', async () => {
    const frame = await readyFrame()
    await showOpen(frame, 'click')
    const first = frame.invocation
    await showOpen(frame, 'click')
    askTab(frame, first)
    expect(opened).toEqual([])
    askTab(frame)
    expect(opened).toEqual([['refs-app', { kind: 'panelTab', id: 'questions' }, 1, SCOPE]])
  })

  it('in overlay mode, leave and dismissal end it (no show is retired there)', async () => {
    const frame = await readyFrame()
    host.handle(frame, { type: 'regions', rects: [{ left: 0, top: 0, width: 10, height: 10 }] })
    await showOpen(frame, 'click')
    askTab(frame)
    expect(opened).toHaveLength(1)
    await showOpen(frame, 'click')
    host.leave(KEY)
    askTab(frame)
    await showOpen(frame, 'click')
    host.hideUnpinned('escape')
    askTab(frame)
    expect(opened).toHaveLength(1)
  })

  it('a click in the card still opens a tab after the grant is spent, or with none', async () => {
    const frame = await readyFrame()
    await showOpen(frame, 'click')
    askTab(frame)
    Object.defineProperty(navigator, 'userActivation', { configurable: true, value: { isActive: true } })
    frame.el.focus()
    try {
      askTab(frame)
      await showOpen(frame, 'hover')
      askTab(frame)
    } finally {
      Reflect.deleteProperty(navigator, 'userActivation')
      frame.el.blur()
    }
    expect(opened).toHaveLength(3)
  })

  it('a hidden card, or one past the bound, opens nothing', async () => {
    const frame = await readyFrame()
    await host.show(frame, { text: '[Q4a]' }, rect(50), undefined, { trigger: 'click', scope: SCOPE })
    askTab(frame)
    await showOpen(frame, 'click')
    vi.advanceTimersByTime(3_001)
    askTab(frame)
    expect(opened).toEqual([])
  })
})

describe('FrameHost: the click grant opens one target, a tab or a session control', () => {
  const CONTROL = { kind: 'sessionControl', id: 'questions' }
  const askControl = (frame: PreviewFrame, request?: number) =>
    host.handle(frame, { type: 'openSessionControl', invocation: frame.invocation, control: 'questions', payload: { label: '[Q4a]' }, ...(request === undefined ? {} : { request }) })
  const answers = (frame: PreviewFrame) => sentTo(frame.el).filter((m) => m.type === 'opened')

  it('one click opens the control once; a second open of either target is refused no_click', async () => {
    const frame = await readyFrame()
    await showOpen(frame, 'click')
    askControl(frame, 1)
    askControl(frame, 2)
    host.handle(frame, { type: 'openPanelTab', invocation: frame.invocation, tab: 'questions', payload: 1, request: 3 })
    expect(opened).toEqual([['refs-app', CONTROL, { label: '[Q4a]' }, SCOPE]])
    expect(answers(frame)).toEqual([
      expect.objectContaining({ request: 1, ok: true }),
      expect.objectContaining({ request: 2, ok: false, reason: 'no_click' }),
      expect.objectContaining({ request: 3, ok: false, reason: 'no_click' }),
    ])
  })

  it('a tab first spends the grant, so the control after it is refused', async () => {
    const frame = await readyFrame()
    await showOpen(frame, 'tap')
    askTab(frame)
    askControl(frame, 7)
    expect(opened).toEqual([['refs-app', { kind: 'panelTab', id: 'questions' }, 1, SCOPE]])
    expect(answers(frame)).toEqual([expect.objectContaining({ request: 7, ok: false, reason: 'no_click' })])
  })

  it('a hover show opens nothing and is told no_click', async () => {
    const frame = await readyFrame()
    await showOpen(frame, 'hover')
    askControl(frame, 4)
    expect(opened).toEqual([])
    expect(answers(frame)).toEqual([expect.objectContaining({ request: 4, ok: false, reason: 'no_click' })])
  })

  it('names why: a hidden card is not_shown, an unapproved one not_approved, the chat page its own refusal', async () => {
    const frame = await readyFrame()
    await host.show(frame, { text: '[Q4a]' }, rect(50), undefined, { trigger: 'click', scope: SCOPE })
    askControl(frame, 1)
    await host.show(frame, { text: '[Q4a]' }, rect(50), undefined, { trigger: 'click', scope: { ...SCOPE, messages: false } })
    host.handle(frame, { type: 'open', invocation: frame.invocation })
    askControl(frame, 2)
    expect(answers(frame).map((m) => m.reason)).toEqual(['not_shown', 'not_approved'])
    const refusing = new FrameHost({ ...hostOptions(), onOpen: () => 'not_declared' })
    const other = refusing.hoverFrame('refs-app:other', 'refs-app')
    refusing.handle(other, { type: 'ready' })
    await other.ready
    expect(await refusing.show(other, { text: '[Q4a]' }, rect(50), undefined, { trigger: 'click', scope: SCOPE })).toBe(true)
    refusing.handle(other, { type: 'open', invocation: other.invocation })
    refusing.handle(other, { type: 'openSessionControl', invocation: other.invocation, control: 'nope', payload: null, request: 3 })
    expect(sentTo(other.el).filter((m) => m.type === 'opened')).toEqual([expect.objectContaining({ request: 3, ok: false, reason: 'not_declared' })])
    refusing.disposeAll()
    expect(opened).toEqual([])
  })

  it('an open with no request number is not answered (a card built before level 11)', async () => {
    const frame = await readyFrame()
    await showOpen(frame, 'hover')
    askControl(frame)
    expect(answers(frame)).toEqual([])
  })
})

describe('FrameHost: the show says which side of its link the card is on', () => {
  it('below when it fits, above when it does not, none in overlay mode', async () => {
    const frame = await readyFrame()
    await showOpen(frame, 'hover', 50)
    expect(lastShow(frame)?.placement).toBe('below')
    await showOpen(frame, 'hover', window.innerHeight - 30)
    expect(lastShow(frame)?.placement).toBe('above')
    host.handle(frame, { type: 'regions', rects: [{ left: 0, top: 0, width: 10, height: 10 }] })
    await showOpen(frame, 'hover', 50)
    expect(lastShow(frame)).not.toHaveProperty('placement')
  })

  it('the card is told when its own size moves it to the other side', async () => {
    const frame = await readyFrame()
    const top = window.innerHeight - 200
    await showOpen(frame, 'hover', top)
    expect(lastShow(frame)?.placement).toBe('above')
    host.handle(frame, { type: 'resize', invocation: frame.invocation, width: 300, height: 120 })
    host.handle(frame, { type: 'resize', invocation: frame.invocation, width: 300, height: 110 })
    const told = sentTo(frame.el).filter((m) => m.type === 'placement')
    expect(told).toEqual([expect.objectContaining({ invocation: frame.invocation, placement: 'below' })])
    host.handle(frame, { type: 'drag', invocation: frame.invocation, dx: 5, dy: 5 })
    expect(sentTo(frame.el).filter((m) => m.type === 'placement')).toHaveLength(1)
  })
})
