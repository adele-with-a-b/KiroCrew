import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { createElement } from 'react'
import { render, waitFor, act, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { APP_FRAME_TAG } from '../appFrame/frameProtocol'
import { hasTextPatterns, resetTextPatternsForTest, setTextPatternWorkerFactoryForTest } from './textPatterns'
import { scanTexts, type FromWorker, type ToWorker } from './textPatternScan'
import { readFrameMessage } from './linkPreviewProtocol'
import { onLinkPreviewNav, type LinkPreviewNav } from './linkPreviewNav'
import { framesIn, fromFrame, installFrameWindows, sentTo } from '../test/LinkPreviewFramesTestUtils'

installFrameWindows()

const listApps = vi.fn()
vi.mock('../api/client', () => ({ api: { listApps: () => listApps() } }))

const LABEL = { id: 'label', pattern: '\\[R\\d+\\]' }
const SLOT = 'chat-7-1789049480'
const TS = '2026-11-21T20:40:12.345Z'
const MID = 'm_3f9c'

function app(sessionKey: boolean, granted = sessionKey) {
  const lp = { id: 'cards', entry: 'dist/lp.mjs', hosts: [], textPatterns: [LABEL], ...(sessionKey ? { sessionKey: true } : {}) }
  return {
    name: 'refs-app',
    enabled: true,
    manifest: { contributes: { linkPreviews: [lp], panelTabs: [{ id: 'questions', title: 'Questions', entry: 'dist/tab.mjs' }] } },
    linkPreviewGrant: { hosts: [], textPatterns: ['label::' + LABEL.pattern], sessionKey: granted },
  }
}

/** The scan worker, run in this thread on a microtask (vitest has no `Worker`). */
function worker() {
  const w = {
    onmessage: null as ((e: MessageEvent<FromWorker>) => void) | null,
    terminate() {},
    postMessage(msg: ToWorker) {
      const hits = scanTexts(msg.patterns, msg.texts, () => {}, () => {})
      queueMicrotask(() => w.onmessage?.({ data: { type: 'done', batch: msg.batch, hits, rejected: [] } } as MessageEvent<FromWorker>))
    },
  }
  return w
}

const settle = async () => { await act(async () => { for (let i = 0; i < 6; i++) await Promise.resolve() }) }
const mint = vi.fn(async (appName: string) => ({ frame: `/app-frame/${appName}/lp/f/tok`, capability: 'cap', ttl: 3600 }))

/** The layer, an approved app, and one chat message with a `[R4]` span in it. */
async function mount(apps: unknown[], props: { previewSlot?: string; messageTs?: string; previewMid?: string } = { previewSlot: SLOT, messageTs: TS, previewMid: MID }) {
  listApps.mockResolvedValue(apps)
  const mod = await import('./LinkPreviewLayer')
  mod.setLinkPreviewMintForTest(mint)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(createElement(QueryClientProvider, { client: qc }, createElement(mod.default)))
  await waitFor(() => expect(hasTextPatterns()).toBe(true), { timeout: 5000 })
  const { container } = render(<MarkdownRenderer textPreviews content="see [R4] here" {...props} />)
  await settle()
  const span = container.querySelector('[data-link-preview-text]') as HTMLElement
  if (!span) throw new Error('no span')
  return span
}

/** Let a frame created for `span` load and answer ready. */
async function ready() {
  await act(async () => { await vi.advanceTimersByTimeAsync(0) })
  for (const f of framesIn()) fromFrame(f, 'ready')
  await act(async () => { await vi.advanceTimersByTimeAsync(50) })
  const [frame] = framesIn()
  return frame
}
const shows = (f: HTMLIFrameElement) => sentTo(f).filter((m) => m.type === 'show')
/** A message from `frame` answering its latest show (or `invocation`). */
function ask(frame: HTMLIFrameElement, type: string, data: Record<string, unknown>) {
  const last = shows(frame).at(-1)
  fromFrame(frame, type, { invocation: last?.invocation ?? 0, ...data })
}

let heard: LinkPreviewNav[] = []
let unsub = () => {}
beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  setTextPatternWorkerFactoryForTest(worker)
  heard = []
  unsub = onLinkPreviewNav((req) => { heard.push(req); return true })
})
afterEach(async () => {
  unsub()
  ;(await import('./LinkPreviewLayer')).setLinkPreviewMintForTest()
  setTextPatternWorkerFactoryForTest()
  resetTextPatternsForTest()
  listApps.mockReset()
  mint.mockClear()
  document.body.replaceChildren()
  vi.useRealTimers()
})

describe('link-preview cards: a click opens one', () => {
  it('a mouse click on a span shows its card at once, told it was a click', async () => {
    const span = await mount([app(false)])
    fireEvent.pointerDown(span, { pointerType: 'mouse' })
    fireEvent.click(span)
    const frame = await ready()
    expect(shows(frame)).toEqual([expect.objectContaining({ text: '[R4]', trigger: 'click' })])
  })

  it('a click on a span whose hover card is already up tells the app again, as a click', async () => {
    const span = await mount([app(false)])
    fireEvent.pointerOver(span)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const frame = await ready()
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(shows(frame).map((m) => m.trigger)).toEqual(['hover'])
    fireEvent.pointerDown(span, { pointerType: 'mouse' })
    fireEvent.click(span)
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(shows(frame).map((m) => m.trigger)).toEqual(['hover', 'click'])
    expect(sentTo(frame).some((m) => m.type === 'leave')).toBe(false)
  })

  it('a modified click, a non-primary button, or a drag that selected the span opens nothing', async () => {
    const span = await mount([app(false)])
    fireEvent.click(span, { metaKey: true })
    fireEvent.click(span, { button: 1 })
    const sel = window.getSelection()
    sel?.selectAllChildren(span.parentElement as HTMLElement)
    fireEvent.click(span)
    sel?.removeAllRanges()
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(framesIn()).toEqual([])
  })
})

describe('link-preview cards: which message the span is in', () => {
  it('an app approved for sessionKey is told the chat and the message', async () => {
    const span = await mount([app(true)])
    fireEvent.click(span)
    const frame = await ready()
    expect(shows(frame)[0]).toMatchObject({ sessionKey: `dashboard:${SLOT}`, message: { ts: TS, id: MID } })
  })

  it('an app not approved for it is told neither', async () => {
    for (const a of [app(false), app(true, false)]) {
      const span = await mount([a])
      fireEvent.click(span)
      const frame = await ready()
      expect(shows(frame)[0]).not.toHaveProperty('message')
      expect(shows(frame)[0]).not.toHaveProperty('sessionKey')
      document.body.replaceChildren()
      resetTextPatternsForTest()
    }
  })

  it('a row with no mid sends its ts alone; a host that named no local slot sends no message', async () => {
    let span = await mount([app(true)], { previewSlot: SLOT, messageTs: TS })
    fireEvent.click(span)
    let frame = await ready()
    expect(shows(frame)[0].message).toEqual({ ts: TS })
    document.body.replaceChildren()
    resetTextPatternsForTest()
    span = await mount([app(true)], { messageTs: TS, previewMid: MID })
    fireEvent.click(span)
    frame = await ready()
    expect(shows(frame)[0]).not.toHaveProperty('message')
  })
})

describe('link-preview cards: open the app tab, jump to a message', () => {
  async function shown(apps: unknown[], props?: Parameters<typeof mount>[1]) {
    const span = await mount(apps, props)
    fireEvent.click(span)
    const frame = await ready()
    fromFrame(frame, 'open')
    clickIn(frame)
    return frame
  }
  /** The person's click in the card: a live activation, and focus in its frame. */
  function clickIn(frame: HTMLIFrameElement) {
    Object.defineProperty(navigator, 'userActivation', { configurable: true, value: { isActive: true } })
    frame.focus()
  }
  afterEach(() => {
    Reflect.deleteProperty(navigator, 'userActivation')
    ;(document.activeElement as HTMLElement | null)?.blur?.()
  })

  it('a card asks nothing without the person\'s click in it: no activation, or focus elsewhere', async () => {
    const frame = await shown([app(true)])
    Reflect.deleteProperty(navigator, 'userActivation')
    ask(frame, 'openPanelTab', { tab: 'questions', payload: 1 })
    ask(frame, 'jump', { ts: TS })
    clickIn(frame)
    frame.blur()
    ask(frame, 'openPanelTab', { tab: 'questions', payload: 1 })
    ask(frame, 'jump', { ts: TS })
    expect(heard).toEqual([])
    clickIn(frame)
    ask(frame, 'jump', { ts: TS })
    expect(heard).toHaveLength(1)
  })

  it("a shown card opens its own app's tab in the chat it was shown in, with its payload", async () => {
    const frame = await shown([app(true)])
    ask(frame, 'openPanelTab', { tab: 'questions', payload: { label: '[R4]' } })
    expect(heard).toEqual([{ kind: 'openPanelTab', slot: SLOT, appName: 'refs-app', tab: 'questions', payload: { label: '[R4]' } }])
  })

  it('a card of an app not approved for sessionKey opens no tab: the tab would tell it the chat', async () => {
    for (const a of [app(false), app(true, false)]) {
      const frame = await shown([a])
      ask(frame, 'openPanelTab', { tab: 'questions', payload: 1 })
      document.body.replaceChildren()
      resetTextPatternsForTest()
    }
    expect(heard).toEqual([])
  })

  it('a card that turns itself into an overlay cannot answer for a show made before', async () => {
    const frame = await shown([app(true)])
    const before = shows(frame).at(-1)?.invocation
    fromFrame(frame, 'regions', { rects: [{ left: 0, top: 0, width: 100, height: 100 }] })
    clickIn(frame)
    fromFrame(frame, 'jump', { invocation: before, ts: TS })
    expect(heard).toEqual([])
    // A show made while it is an overlay can. The first click closes the
    // clicked card; the next one shows it again.
    const span = document.querySelector('[data-link-preview-text]') as HTMLElement
    fireEvent.click(span)
    fireEvent.click(span)
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    clickIn(frame)
    ask(frame, 'jump', { ts: TS })
    expect(heard).toEqual([expect.objectContaining({ kind: 'jump', slot: SLOT })])
  })

  it('an approved card jumps to any message of its own chat, not only the one its text is in, and says which it left from', async () => {
    const frame = await shown([app(true)])
    ask(frame, 'jump', { ts: '2026-11-21T19:00:00.000Z', id: 'm_1' })
    expect(heard).toEqual([{ kind: 'jump', slot: SLOT, target: { ts: '2026-11-21T19:00:00.000Z', id: 'm_1' }, from: { ts: TS, id: MID } }])
  })

  it('a jump from an app not approved for sessionKey is refused', async () => {
    const frame = await shown([app(false)])
    ask(frame, 'jump', { ts: TS })
    expect(heard).toEqual([])
  })

  it('a hidden card, an older show, or a message in no local chat asks nothing', async () => {
    let frame = await shown([app(true)])
    ask(frame, 'openPanelTab', { tab: 'questions', payload: 1, invocation: 0 })
    ask(frame, 'jump', { ts: TS, invocation: 0 })
    fromFrame(frame, 'close')
    ask(frame, 'openPanelTab', { tab: 'questions', payload: 1 })
    ask(frame, 'jump', { ts: TS })
    expect(heard).toEqual([])
    document.body.replaceChildren()
    resetTextPatternsForTest()
    frame = await shown([app(true)], { messageTs: TS })
    ask(frame, 'openPanelTab', { tab: 'questions', payload: 1 })
    ask(frame, 'jump', { ts: TS })
    expect(heard).toEqual([])
  })
})

describe('linkPreviewProtocol: openPanelTab and jump', () => {
  const t = { [APP_FRAME_TAG]: 1 }
  it('reads a tab slug and a JSON copy of the payload, and nothing else', () => {
    const payload = { a: [1, 'x'] }
    const read = readFrameMessage({ ...t, type: 'openPanelTab', invocation: 3, tab: 'questions', payload })
    expect(read).toEqual({ type: 'openPanelTab', invocation: 3, tab: 'questions', payload })
    expect(read && 'payload' in read && read.payload).not.toBe(payload)
    expect(readFrameMessage({ ...t, type: 'openPanelTab', invocation: 3, tab: 'questions' })).toMatchObject({ payload: null })
    for (const tab of ['app:other:tab', 'Questions', '', 'x'.repeat(65), 7]) {
      expect(readFrameMessage({ ...t, type: 'openPanelTab', invocation: 3, tab })).toBeNull()
    }
    expect(readFrameMessage({ ...t, type: 'openPanelTab', invocation: 3, tab: 'q', payload: 10n })).toBeNull()
  })

  it('reads a jump as a short ts and an optional short id', () => {
    expect(readFrameMessage({ ...t, type: 'jump', invocation: 1, ts: TS, id: MID })).toEqual({ type: 'jump', invocation: 1, target: { ts: TS, id: MID } })
    expect(readFrameMessage({ ...t, type: 'jump', invocation: 1, ts: TS, id: 5 })).toEqual({ type: 'jump', invocation: 1, target: { ts: TS } })
    expect(readFrameMessage({ ...t, type: 'jump', invocation: 1, ts: TS, id: 'x'.repeat(129) })).toEqual({ type: 'jump', invocation: 1, target: { ts: TS } })
    for (const ts of [undefined, '', 'x'.repeat(129), 12]) {
      expect(readFrameMessage({ ...t, type: 'jump', invocation: 1, ts })).toBeNull()
    }
  })
})

describe('link-preview cards: a clicked or tapped card stays until it is closed', () => {
  const closes = (f: HTMLIFrameElement) => sentTo(f).filter((m) => m.type === 'leave' || m.type === 'hide').map((m) => (m.type === 'hide' ? `hide:${m.reason}` : 'leave'))
  async function clicked() {
    const span = await mount([app(true)])
    fireEvent.pointerDown(span, { pointerType: 'mouse' })
    fireEvent.click(span)
    const frame = await ready()
    return { span, frame }
  }

  it('leaving the span, and hovering another, does not close a clicked card', async () => {
    const { span, frame } = await clicked()
    fireEvent.pointerOut(span, { relatedTarget: frame })
    fireEvent.pointerOut(frame, { relatedTarget: document.body })
    fireEvent.pointerMove(document.body, { clientX: 500, clientY: 500 })
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(closes(frame)).toEqual([])
    expect(shows(frame).map((m) => m.trigger)).toEqual(['click'])
  })

  it('Escape closes it', async () => {
    const { frame } = await clicked()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(closes(frame)).toEqual(['hide:escape'])
  })

  it('a click outside it closes it', async () => {
    const { frame } = await clicked()
    fireEvent.pointerDown(document.body, { pointerType: 'mouse' })
    fireEvent.click(document.body)
    expect(closes(frame)).toEqual(['leave', 'hide:click'])
  })

  it('a second click on the span closes it and opens nothing new', async () => {
    const { span, frame } = await clicked()
    fireEvent.pointerDown(span, { pointerType: 'mouse' })
    fireEvent.click(span)
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(closes(frame)).toEqual(['leave', 'hide:click'])
    expect(shows(frame)).toHaveLength(1)
  })

  it('a clicked card the app closed itself opens again on the next click, and hover works again', async () => {
    const { span, frame } = await clicked()
    fromFrame(frame, 'open')
    ask(frame, 'close', {})
    fireEvent.pointerDown(span, { pointerType: 'mouse' })
    fireEvent.click(span)
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(shows(frame).map((m) => m.trigger)).toEqual(['click', 'click'])
    expect(closes(frame)).toEqual([])
  })

  it('a hover card still goes on leave', async () => {
    const span = await mount([app(true)])
    fireEvent.pointerOver(span)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const frame = await ready()
    await act(async () => { await vi.advanceTimersByTimeAsync(400) })
    expect(shows(frame).map((m) => m.trigger)).toEqual(['hover'])
    fireEvent.pointerOut(span, { relatedTarget: document.body })
    expect(closes(frame)).toEqual(['leave'])
  })

  it('a tapped card closes on the next tap, which the dashboard also hides', async () => {
    const span = await mount([app(true)])
    const touch = { pointerType: 'touch' }
    fireEvent.pointerDown(span, touch)
    fireEvent.pointerUp(span, touch)
    const frame = await ready()
    expect(shows(frame).map((m) => m.trigger)).toEqual(['tap'])
    fireEvent.pointerOut(span, touch)
    expect(closes(frame)).toEqual([])
    fireEvent.pointerDown(span, touch)
    fireEvent.pointerUp(span, touch)
    expect(closes(frame)).toEqual(['leave', 'hide:click'])
  })
})
