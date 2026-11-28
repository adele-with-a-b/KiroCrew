import { describe, it, expect, afterEach, beforeEach, onTestFinished, vi } from 'vitest'
import { FrameHost, type FrameHostOptions, type PreviewFrame, type ShowScope, type StepLanding, type StepRequest } from './linkPreviewFrames'
import { readFrameMessage, type FromFrame } from './linkPreviewProtocol'
import { onLinkPreviewNav, type JumpOutcome, type LinkPreviewNav } from './linkPreviewNav'
import { findStepLabel, runStep } from './linkPreviewStep'
import { registerTurnStepper } from '../pages/chat/turnStep'
import { APP_FRAME_TAG } from '../appFrame/frameProtocol'
import { installFrameWindows, sentTo } from '../test/LinkPreviewFramesTestUtils'

vi.mock('./textPatterns', () => ({
  resolveTextPreview: (text: string) => (/^\[[A-Z]\d+\]$/.test(text) ? { owner: text.startsWith('[X') ? 'other:labels' : 'labels-app:labels' } : null),
}))

installFrameWindows()

const KEY = 'labels-app:labels'
const SCOPE: ShowScope = { slot: 'chat-7', origin: { ts: 't1' }, messages: true }
const rect = (top: number) => ({ left: 100, top, width: 40, height: 16 })

let steps: { key: string; ask: StepRequest; hooks: StepLanding; cancelled: boolean }[] = []
let host: FrameHost

/** A frame host whose steps are recorded in `steps`, or run by `onStep`. */
function makeHost(onStep?: FrameHostOptions['onStep']) {
  return new FrameHost({
    mint: async () => ({ frame: '/app-frame/labels-app/lp/f/tok', capability: 'cap', ttl: 3600 }),
    theme: () => 'light',
    onFailed() {},
    onTextPatternsOff() {},
    onEscape() {},
    onFramesChanged() {},
    onStep: onStep ?? ((key: string, ask: StepRequest, hooks: StepLanding) => {
      const entry = { key, ask, hooks, cancelled: false }
      steps.push(entry)
      return () => { entry.cancelled = true }
    }),
  })
}

/** A ready card, shown for `[A1]` in message t1, opened, and pinned unless told not to. */
async function shownCard(opts: { pinned?: boolean; scope?: ShowScope } = {}): Promise<PreviewFrame> {
  const frame = host.hoverFrame(KEY, 'labels-app')
  host.handle(frame, { type: 'ready' })
  await frame.ready
  expect(await host.show(frame, { text: '[A1]' }, rect(50), undefined, { trigger: 'click', scope: opts.scope ?? SCOPE })).toBe(true)
  host.handle(frame, { type: 'open', invocation: frame.invocation })
  if (opts.pinned !== false) host.handle(frame, { type: 'pin', invocation: frame.invocation, pinned: true })
  return frame
}
const step = (frame: PreviewFrame, request: number, extra: Partial<Extract<FromFrame, { type: 'step' }>> = {}) =>
  host.handle(frame, { type: 'step', invocation: frame.invocation, target: { ts: 't9', id: 'm9' }, text: '[A9]', line: '[A9] · 9 of 9', request, ...extra })
const answers = (frame: PreviewFrame) => sentTo(frame.el).filter((m) => m.type === 'stepped')
const shows = (frame: PreviewFrame) => sentTo(frame.el).filter((m) => m.type === 'show')
const flush = () => new Promise((r) => setTimeout(r, 0))

function clickedIn(frame: PreviewFrame) {
  Object.defineProperty(navigator, 'userActivation', { configurable: true, value: { isActive: true } })
  frame.el.tabIndex = 0
  frame.el.focus()
}

beforeEach(() => {
  steps = []
  host = makeHost()
})
afterEach(() => {
  host.disposeAll()
  Reflect.deleteProperty(navigator, 'userActivation')
  document.body.replaceChildren()
})

describe('readFrameMessage: step', () => {
  const read = (m: Record<string, unknown>) => readFrameMessage({ [APP_FRAME_TAG]: 1, type: 'step', invocation: 2, ...m })
  it('reads the message, the label text, a clean spoken line and the request', () => {
    expect(read({ ts: 't', id: 'm', text: '[A2]', line: ' [A2]‮ · 2 of 5 ', request: 3 }))
      .toEqual({ type: 'step', invocation: 2, target: { ts: 't', id: 'm' }, text: '[A2]', line: '[A2] · 2 of 5', request: 3 })
  })
  it('drops a step with no message or no text, and a line that is blank or too long', () => {
    expect(read({ text: '[A2]' })).toBeNull()
    expect(read({ ts: 't', text: '' })).toBeNull()
    expect(read({ ts: 't', text: 'x'.repeat(129) })).toBeNull()
    expect(read({ ts: 't', text: '[A2]', line: 'x'.repeat(161) })).toEqual({ type: 'step', invocation: 2, target: { ts: 't' }, text: '[A2]' })
    expect(read({ ts: 't', text: '[A2]', line: '   ' })).toEqual({ type: 'step', invocation: 2, target: { ts: 't' }, text: '[A2]' })
  })
})

describe('FrameHost: a card steps', () => {
  it('is refused, and told why, unless shown, pinned, clicked in, approved and on screen', async () => {
    const unpinned = await shownCard({ pinned: false })
    clickedIn(unpinned)
    step(unpinned, 1)
    host.disposeAll()
    const frame = await shownCard()
    step(frame, 2)
    clickedIn(frame)
    step(frame, 3, { invocation: frame.invocation - 1 })
    host.disposeAll()
    const unapproved = await shownCard({ scope: { slot: 'chat-7', messages: false } })
    clickedIn(unapproved)
    step(unapproved, 4)
    host.disposeAll()
    const elsewhere = await shownCard({ scope: { messages: true } })
    clickedIn(elsewhere)
    step(elsewhere, 5)
    expect([unpinned, frame, unapproved, elsewhere].flatMap(answers).map((m) => [m.request, m.reason]))
      .toEqual([[1, 'not_pinned'], [2, 'no_click'], [3, 'not_shown'], [4, 'not_approved'], [5, 'not_on_screen']])
    expect(steps).toEqual([])
  })

  it('lands: the card is shown again at the label (trigger step, a scope for that message), then answered', async () => {
    const frame = await shownCard()
    clickedIn(frame)
    step(frame, 7)
    expect(steps).toHaveLength(1)
    expect(steps[0].key).toBe(KEY)
    expect(steps[0].ask).toEqual({ slot: 'chat-7', target: { ts: 't9', id: 'm9' }, text: '[A9]', line: '[A9] · 9 of 9' })
    const link = { text: '[A9]', sessionKey: 'dashboard:chat-7', message: { ts: 't9', id: 'm9' } }
    steps[0].hooks.land(rect(300), link)
    steps[0].hooks.done(true)
    await flush()
    const show = shows(frame).at(-1)
    expect(show).toMatchObject({ invocation: frame.invocation, trigger: 'step', ...link, rect: rect(300) })
    expect(answers(frame)).toEqual([expect.objectContaining({ request: 7, ok: true })])
    // The answer comes after the show, so the app hears where its card is first.
    const sent = sentTo(frame.el)
    expect(sent.indexOf(show!)).toBeLessThan(sent.indexOf(answers(frame)[0]))
    // The card moves with its label while the scroll settles.
    steps[0].hooks.land(rect(320), link)
    expect(frame.el.style.top).toBe(`${320 + 16 + 6}px`)
    // Its next step is in that message's chat, from that show.
    step(frame, 8, { target: { ts: 't1' }, text: '[A1]' })
    expect(steps).toHaveLength(2)
  })

  it('a newer step replaces one still on its way; a refusal from the chat is passed on', async () => {
    const frame = await shownCard()
    clickedIn(frame)
    step(frame, 1)
    step(frame, 2)
    expect(steps[0].cancelled).toBe(true)
    steps[0].hooks.land(rect(10), { text: '[A9]' })
    steps[1].hooks.done('load_failed')
    await flush()
    expect(answers(frame).map((m) => [m.request, m.ok, m.reason])).toEqual([[1, false, 'replaced'], [2, false, 'load_failed']])
    expect(shows(frame)).toHaveLength(1)
  })
})

/** Message `ts` of chat-7 drawn with `labels`, each at viewport top `top()`. */
function message(ts: string, labels: string[], top: () => number = () => 40) {
  const scope = document.createElement('div')
  scope.setAttribute('data-link-preview-scope', '')
  scope.setAttribute('data-link-preview-session', 'dashboard:chat-7')
  scope.setAttribute('data-link-preview-ts', ts)
  for (const text of labels) {
    const span = document.createElement('span')
    span.setAttribute('data-link-preview-text', '')
    span.textContent = text
    span.getClientRects = () => [{}] as unknown as DOMRectList
    span.getBoundingClientRect = () => {
      const y = top()
      return { left: 10, top: y, width: 30, height: 16, right: 40, bottom: y + 16, x: 10, y, toJSON() {} }
    }
    scope.append(span)
  }
  document.body.append(scope)
  return scope
}

describe('runStep: the host side', () => {
  const ASK = { key: KEY, slot: 'chat-7', target: { ts: 't9' }, text: '[A9]', line: '[A9] · 9 of 9' }

  it('finds only the asking provider\'s own label, in that chat and message', () => {
    message('t8', ['[A9]'])
    message('t9', ['[X9]', '[A9]'])
    const found = findStepLabel(ASK)
    expect(found?.textContent).toBe('[A9]')
    expect(found?.closest('[data-link-preview-scope]')?.getAttribute('data-link-preview-ts')).toBe('t9')
    expect(findStepLabel({ ...ASK, text: '[X9]' })).toBeNull()
    expect(findStepLabel({ ...ASK, slot: 'chat-8' })).toBeNull()
  })

  it('a label in view: the card lands there, nothing scrolls, and the line is said in the stepper\'s live region', () => {
    message('t9', ['[A9]'])
    const said: string[] = []
    const unregister = registerTurnStepper(() => true, (line) => said.push(line))
    const navs: LinkPreviewNav[] = []
    const off = onLinkPreviewNav((req) => { navs.push(req); return true })
    const land = vi.fn()
    const done = vi.fn()
    runStep(ASK, { land, done })
    expect(land).toHaveBeenCalledWith({ left: 10, top: 40, width: 30, height: 16 }, { text: '[A9]', sessionKey: 'dashboard:chat-7', message: { ts: 't9' } })
    expect(done).toHaveBeenCalledWith(true)
    expect(navs).toEqual([])
    expect(said).toEqual(['[A9] · 9 of 9'])
    off()
    unregister()
  })

  it('a label not mounted: the chat jumps there from the view, and a jump that fails is the step\'s answer', () => {
    let answer: ((o: JumpOutcome) => void) | undefined
    const navs: LinkPreviewNav[] = []
    const off = onLinkPreviewNav((req) => {
      navs.push(req)
      if (req.kind === 'jump') answer = req.onDone
      return true
    })
    const done = vi.fn()
    runStep(ASK, { land: vi.fn(), done })
    expect(navs).toEqual([expect.objectContaining({ kind: 'jump', slot: 'chat-7', target: { ts: 't9' }, from: 'view' })])
    answer?.('load_failed')
    expect(done).toHaveBeenCalledWith('load_failed')
    off()
    const nobody = vi.fn()
    runStep(ASK, { land: vi.fn(), done: nobody })
    expect(nobody).toHaveBeenCalledWith('not_on_screen')
  })
})

describe('a step whose message is reached and whose label never shows in it', () => {
  const ASK = { key: KEY, slot: 'chat-7', target: { ts: 't9' }, text: '[A9]', line: '[A9] · 9 of 9' }

  /** A chat whose jump to any message lands at once and hands back `putBack`. */
  function landingChat() {
    vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame', 'performance', 'Date'] })
    vi.setSystemTime(new Date('2026-11-26T12:00:00Z'))
    onTestFinished(() => { vi.useRealTimers() })
    const order: string[] = []
    const putBack = vi.fn(() => { order.push('put back') })
    onTestFinished(onLinkPreviewNav((req) => {
      if (req.kind !== 'jump') return false
      req.onDone?.(true, putBack)
      return true
    }))
    const done = vi.fn((r: unknown) => { order.push(`answered ${String(r)}`) })
    return { putBack, done, order }
  }

  it('answers not_found once the wait runs out, after the chat is put back where the step left from', async () => {
    const { putBack, done, order } = landingChat()
    // The message is on screen; the label the card named is not in it.
    message('t9', ['[A8]'])
    runStep(ASK, { land: vi.fn(), done })
    await vi.advanceTimersByTimeAsync(1900)
    expect(done).not.toHaveBeenCalled()
    expect(putBack).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(200)
    expect(order).toEqual(['put back', 'answered not_found'])
  })

  it('a label that shows while it waits lands, and the chat stays there', async () => {
    const { putBack, done } = landingChat()
    const land = vi.fn()
    runStep(ASK, { land, done })
    await vi.advanceTimersByTimeAsync(500)
    message('t9', ['[A9]'])
    await vi.advanceTimersByTimeAsync(3000)
    expect(done.mock.calls).toEqual([[true]])
    expect(land).toHaveBeenCalled()
    expect(putBack).not.toHaveBeenCalled()
  })

  // Seen on a 480-message chat: the commit that draws the far page's rows held
  // the thread for seconds after the jump had answered, the wait ran out on the
  // wall clock with no frame run, and the step said not_found of a label that
  // was drawn a moment later.
  it('a commit that holds the page past the wait does not use the wait up: the label drawn after it lands', async () => {
    const { putBack, done } = landingChat()
    const land = vi.fn()
    const clock = performance.now.bind(performance)
    let held = 0
    const now = vi.spyOn(performance, 'now').mockImplementation(() => clock() + held)
    onTestFinished(() => { now.mockRestore() })
    runStep(ASK, { land, done })
    await vi.advanceTimersByTimeAsync(100)
    // 3 s pass with no frame run. The label is not drawn in the first frames
    // after them (its row is; the label is marked a moment later), then it is.
    held = 3000
    await vi.advanceTimersByTimeAsync(48)
    expect(done).not.toHaveBeenCalled()
    message('t9', ['[A9]'])
    await vi.advanceTimersByTimeAsync(100)
    expect(done.mock.calls).toEqual([[true]])
    expect(land).toHaveBeenCalled()
    expect(putBack).not.toHaveBeenCalled()
  })

  it('the person scrolling while it waits ends the step, and the chat stays where they took it', async () => {
    const { putBack, done } = landingChat()
    runStep(ASK, { land: vi.fn(), done })
    await vi.advanceTimersByTimeAsync(500)
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'PageUp' }))
    await vi.advanceTimersByTimeAsync(3000)
    expect(done.mock.calls).toEqual([['replaced']])
    expect(putBack).not.toHaveBeenCalled()
  })

  it('a step cancelled while it waits (a newer step, the card going) puts nothing back', async () => {
    const { putBack, done } = landingChat()
    const cancel = runStep(ASK, { land: vi.fn(), done })
    await vi.advanceTimersByTimeAsync(500)
    cancel()
    await vi.advanceTimersByTimeAsync(3000)
    expect(done).not.toHaveBeenCalled()
    expect(putBack).not.toHaveBeenCalled()
  })
})

describe('a step while an earlier step is still paging older history in', () => {
  /** The chat's jump as the pin jump runs it (`transcriptPins.ts`): a loaded
   *  message is scrolled to at once, a message older than the loaded rows waits
   *  for its page, and a newer jump replaces the one waiting (told `replaced`). */
  function pagingChat(loaded: Set<string>) {
    const chat: { at: string; waiting: { ts: string; done?: (o: JumpOutcome) => void } | null } = { at: 't6', waiting: null }
    const scrollTo = (ts: string, done?: (o: JumpOutcome) => void) => { chat.at = ts; done?.(true) }
    const off = onLinkPreviewNav((req) => {
      if (req.kind !== 'jump') return false
      const older = chat.waiting
      chat.waiting = null
      if (loaded.has(req.target.ts)) scrollTo(req.target.ts, req.onDone)
      else chat.waiting = { ts: req.target.ts, done: req.onDone }
      older?.done?.('replaced')
      return true
    })
    /** The page holding `ts` arrives: a jump still waiting on it lands. */
    const pageArrives = (ts: string) => {
      loaded.add(ts)
      const waiting = chat.waiting
      chat.waiting = null
      if (waiting) scrollTo(waiting.ts, waiting.done)
    }
    return { chat, pageArrives, off }
  }

  it('the newer step replaces the far jump, so the chat stays on its label and the card by it', async () => {
    vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame', 'performance', 'Date'] })
    vi.setSystemTime(new Date('2026-11-26T12:00:00Z'))
    onTestFinished(() => { vi.useRealTimers() })
    host.disposeAll()
    host = makeHost((key, ask, hooks) => runStep({ key, ...ask }, hooks))
    const { chat, pageArrives, off } = pagingChat(new Set(['t6']))
    onTestFinished(off)
    // A label is on screen while the chat is on its message, else far off it.
    const at = (ts: string) => () => (chat.at === ts ? 300 : -5000)
    const [a6] = message('t6', ['[A6]'], at('t6')).children
    const frame = await shownCard()
    clickedIn(frame)

    // Up to [A1], far up: its page is still loading when the person steps back
    // down to [A6], which is on screen.
    step(frame, 1, { target: { ts: 't1' }, text: '[A1]' })
    expect(chat.waiting?.ts).toBe('t1')
    step(frame, 2, { target: { ts: 't6' }, text: '[A6]' })
    await vi.advanceTimersByTimeAsync(50)
    // The old messages arrive.
    message('t1', ['[A1]'], at('t1'))
    pageArrives('t1')
    await vi.advanceTimersByTimeAsync(3000)

    expect(chat.at).toBe('t6')
    expect(frame.el.style.top).toBe(`${a6.getBoundingClientRect().bottom + 6}px`)
    expect(answers(frame).map((m) => [m.request, m.ok, m.reason])).toEqual([[1, false, 'replaced'], [2, true, undefined]])
  })
})
