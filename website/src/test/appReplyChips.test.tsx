/**
 * An app supplies the strings of Crew's reply chips for one chat, through the
 * `replyChips` field of its session control's status answer.
 *
 * Pins the three layers: the answer's bounds (`normalizeReplyChips`), which one
 * app's list shows (`pickAppReplyChips`), and how the list sits against the
 * reply's own `[OPTIONS:]` line (`deriveChips`). The last block drives the real
 * status probe across a chat switch, under production's `staleTime: Infinity`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  APP_REPLY_CHIP_MAX_CHARS,
  APP_REPLY_CHIPS_MAX,
  normalizeReplyChips,
  normalizeStatus,
  pickAppReplyChips,
  useSessionControlStatuses,
  type AppReplyChipsOffer,
  type ResolvedSessionControl,
} from '../hooks/useSessionControls'
import { deriveChips, isTurnActivityRow, settleSteer, useFollowUpChips } from '../pages/chat/page/followUpChips'
import { deriveFollowUpOptions } from '../app-sdk/protocol'
import { api } from '../api/client'
import type { ChatMessage } from '../types'

vi.mock('../api/client', () => ({ api: { appSessionStatus: vi.fn() } }))
const mockStatus = vi.mocked(api.appSessionStatus)

const control = (over: Partial<ResolvedSessionControl> = {}): ResolvedSessionControl => ({
  key: 'test-app:scope',
  appName: 'test-app',
  appDisplayName: 'Test App',
  appVersion: '0.1.0',
  id: 'scope',
  entryPoint: 'dist/session-control.mjs',
  label: 'Scope',
  icon: 'Tag',
  allowedApi: [],
  allowedEvents: [],
  statusPath: 'session-status',
  processBacked: false,
  width: 340,
  openFiles: false,
  keys: [],
  installedAt: '2026-11-01T00:00:00Z',
  ...over,
})

describe('normalizeReplyChips', () => {
  it('carries a list and its pick mode, one-pick by default', () => {
    expect(normalizeReplyChips({ options: ['Q4a agreed', 'Q4a no'] })).toEqual({ options: ['Q4a agreed', 'Q4a no'], multi: false })
    expect(normalizeReplyChips({ options: ['a', 'b'], multi: true })).toEqual({ options: ['a', 'b'], multi: true })
  })

  it('is null when the app supplies none', () => {
    for (const raw of [undefined, null, {}, { options: [] }, 'Q4a', ['Q4a']]) expect(normalizeReplyChips(raw)).toBeNull()
  })

  it('drops the whole list when any part is malformed, never a part of it', () => {
    expect(normalizeReplyChips({ options: 'Q4a' })).toBeNull()
    expect(normalizeReplyChips({ options: ['ok', 3] })).toBeNull()
    expect(normalizeReplyChips({ options: ['ok', '   '] })).toBeNull()
    expect(normalizeReplyChips({ options: ['ok', 'x'.repeat(APP_REPLY_CHIP_MAX_CHARS + 1)] })).toBeNull()
    expect(normalizeReplyChips({ options: Array.from({ length: APP_REPLY_CHIPS_MAX + 1 }, (_, i) => `o${i}`) })).toBeNull()
    expect(normalizeReplyChips({ options: ['ok'], multi: 'yes' })).toBeNull()
  })

  it('takes at least 6 strings of 80 characters', () => {
    expect(APP_REPLY_CHIPS_MAX).toBeGreaterThanOrEqual(6)
    expect(APP_REPLY_CHIP_MAX_CHARS).toBeGreaterThanOrEqual(80)
    const six = Array.from({ length: 6 }, (_, i) => `${i}`.padEnd(80, 'x'))
    expect(normalizeReplyChips({ options: six })?.options).toEqual(six)
  })

  it('removes hidden and reordering characters, counts code points, keeps the first of a repeat', () => {
    const hidden = '  Q4a' + String.fromCodePoint(0x202e) + ' agreed' + String.fromCodePoint(0x200b) + ' '
    expect(normalizeReplyChips({ options: [hidden, 'Q4a agreed', 'no'] })?.options).toEqual(['Q4a agreed', 'no'])
    // 80 astral code points are 160 UTF-16 units and still fit.
    expect(normalizeReplyChips({ options: ['😀'.repeat(80)] })?.options).toHaveLength(1)
  })

  it('rides on the status answer and leaves the rest of it as it was', () => {
    const s = normalizeStatus({ state: 'warn', detail: 'Q4a', replyChips: { options: ['Q4a agreed'] } })
    expect(s).toEqual({ state: 'warn', tooltip: '', detail: 'Q4a', replyChips: { options: ['Q4a agreed'], multi: false } })
    expect(normalizeStatus({ state: 'ok' }).replyChips).toBeNull()
  })
})

const a = control({ key: 'a-app:q', appName: 'a-app', installedAt: '2026-11-10T00:00:00Z' })
const b = control({ key: 'b-app:q', appName: 'b-app', installedAt: '2026-11-02T00:00:00Z' })

describe('pickAppReplyChips', () => {
  it('is null when no app supplies strings', () => {
    expect(pickAppReplyChips([a, b], {}, {})).toBeNull()
  })

  it('shows the earliest-installed app alone, never a merge, with when it answered', () => {
    const chips = { [a.key]: { options: ['A1'], multi: true }, [b.key]: { options: ['B1'], multi: false } }
    expect(pickAppReplyChips([a, b], chips, { [a.key]: 5, [b.key]: 7 })).toEqual({ key: b.key, options: ['B1'], multi: false, askedAt: 7 })
  })

  it('puts an unknown install time last, then orders by control key', () => {
    const unknown = { ...b, installedAt: '' }
    const chips = { [a.key]: { options: ['A1'], multi: false }, [b.key]: { options: ['B1'], multi: false } }
    expect(pickAppReplyChips([unknown, a], chips, {})?.key).toBe(a.key)
    const tie = { ...b, installedAt: a.installedAt }
    expect(pickAppReplyChips([tie, a], chips, {})?.key).toBe(a.key)
  })
})

const reply = (content: string, ts = '2026-11-25T10:00:00Z'): ChatMessage => ({ role: 'assistant', content, ts } as ChatMessage)
const APP: AppReplyChipsOffer = { key: 'test-app:scope', options: ['Q4a agreed', 'Q4a no'], multi: false, askedAt: 100 }

describe('deriveChips', () => {
  it('shows the app strings, keyed on the app', () => {
    const d = deriveChips([reply('A question.')], false, false, false, APP)
    expect(d).toEqual({ followUpOptions: APP.options, followUpSourceKey: 'app:test-app:scope', followUpMulti: false })
  })

  it("replaces the reply's own [OPTIONS:] line while the app supplies any", () => {
    const msgs = [reply('Pick.\n\n[OPTIONS: Yes | No]')]
    expect(deriveChips(msgs, false, false, false, APP).followUpOptions).toEqual(APP.options)
  })

  it("leaves the reply's own line exactly as today when the app supplies none", () => {
    const msgs = [reply('Pick.\n\n[OPTIONS: Yes | No]')]
    expect(deriveChips(msgs, false, false, false, null)).toEqual(deriveFollowUpOptions(msgs, false, false))
    const running = [...msgs, { role: 'tool', content: 'ls' } as ChatMessage]
    expect(deriveChips(running, false, false, true, null)).toEqual(deriveFollowUpOptions(running, false, false, isTurnActivityRow))
  })

  it('is steady across streaming text and tool steps of a running turn', () => {
    const msgs = [reply('Q4a: ship it?\n\n[OPTIONS: Yes | No]'), { role: 'tool', content: 'ls' } as ChatMessage, { role: 'streaming', content: 'typing' } as ChatMessage]
    for (let i = 1; i <= msgs.length; i++) {
      for (const streaming of [false, true]) {
        expect(deriveChips(msgs.slice(0, i), streaming, false, true, APP).followUpOptions).toEqual(APP.options)
      }
    }
    // The reply's own chips do go there; the app's do not.
    expect(deriveChips(msgs, true, false, true, null).followUpOptions).toEqual([])
  })

  it('hides under a question card and once a chip from the list was sent', () => {
    expect(deriveChips([reply('A question.')], false, true, false, APP).followUpOptions).toEqual([])
    expect(deriveChips([reply('Pick.\n\n[OPTIONS: Yes | No]')], false, false, false, APP, true).followUpOptions).toEqual([])
  })

  it('stays across a wake row, a note from another chat, a queued row and a user row', () => {
    const base = [reply('Q4a: ship it?\n\n[OPTIONS: Yes | No]')]
    const later: ChatMessage[] = [
      { role: 'inject', content: 'Wake: a worker reported.', ts: '2026-11-25T10:01:00Z' } as ChatMessage,
      { role: 'user', content: 'From chat-9: status?', ts: '2026-11-25T10:02:00Z' } as ChatMessage,
      reply('Patrol done, nothing new.', '2026-11-25T10:03:00Z'),
      { role: 'queued', content: 'later', ts: '2026-11-25T10:04:00Z' } as ChatMessage,
    ]
    for (let i = 1; i <= later.length; i++) {
      const msgs = [...base, ...later.slice(0, i)]
      // The reply's own chips are gone by now; the app's are not.
      expect(deriveChips(msgs, false, false, false, APP).followUpOptions).toEqual(APP.options)
    }
    expect(deriveFollowUpOptions([...base, ...later], false, false).followUpOptions).toEqual([])
  })
})

describe('a sent chip turns its list off until the app answers again', () => {
  // A fixed clock: the send's time is compared with each answer's ask time.
  const T = 5_000_000
  beforeEach(() => { vi.spyOn(Date, 'now').mockReturnValue(T) })
  afterEach(() => vi.restoreAllMocks())

  function chips(initial: { app: AppReplyChipsOffer | null; slot: string; card?: boolean }) {
    const inputRef = { current: '' }
    const setInput = vi.fn((v: string) => { inputRef.current = v })
    const hook = renderHook((p: { app: AppReplyChipsOffer | null; slot: string; card?: boolean }) => useFollowUpChips({
      messages: [reply('Q4a: ship it?')], isStreaming: true, appReplyChips: p.app, turnRunning: true,
      pendingQuestion: (p.card ? { id: 'q1' } : null) as never, activeSlot: p.slot,
      inputRef, setInput, prefillEdited: false, setPrefillEdited: () => {},
    }), { initialProps: initial })
    return { ...hook, inputRef }
  }

  it('after a double-click or quick send, until a newer answer', () => {
    const { result, rerender } = chips({ app: APP, slot: 'chat-1' })
    act(() => { result.current.noteFollowUpSend('Q4a agreed')(true) })
    expect(result.current.followUpOptions).toEqual([])
    // The same answer again (a re-render, a new object) keeps it off.
    rerender({ app: { ...APP }, slot: 'chat-1' })
    expect(result.current.followUpOptions).toEqual([])
    // An answer asked for before the send, landing after it, keeps it off too.
    rerender({ app: { ...APP, askedAt: T - 50 }, slot: 'chat-1' })
    expect(result.current.followUpOptions).toEqual([])
    // An answer asked for after the send shows whatever the app says.
    rerender({ app: { ...APP, askedAt: T + 1000 }, slot: 'chat-1' })
    expect(result.current.followUpOptions).toEqual(APP.options)
  })

  it('after the box is sent with a picked chip, and the pick is cleared', () => {
    const { result } = chips({ app: APP, slot: 'chat-1' })
    act(() => result.current.toggleFollowUpOption('Q4a no'))
    expect(result.current.followUpPicked.has('Q4a no')).toBe(true)
    act(() => { result.current.noteFollowUpSend()(true) })
    expect(result.current.followUpOptions).toEqual([])
    expect(result.current.followUpPicked.size).toBe(0)
  })

  it('not after a click that only fills the box, nor a send of typed text', () => {
    const { result, inputRef } = chips({ app: APP, slot: 'chat-1' })
    act(() => result.current.toggleFollowUpOption('Q4a agreed'))
    expect(inputRef.current).toBe('Q4a agreed')
    expect(result.current.followUpOptions).toEqual(APP.options)
    act(() => result.current.toggleFollowUpOption('Q4a agreed'))
    act(() => { result.current.noteFollowUpSend()(true) })
    act(() => { result.current.noteFollowUpSend('something I typed')(true) })
    expect(result.current.followUpOptions).toEqual(APP.options)
  })

  it('after a box send that holds one of the strings with no pick (Tab on the hint, a kept draft)', () => {
    const { result, inputRef } = chips({ app: APP, slot: 'chat-1' })
    inputRef.current = 'Q4a agreed, and ship it today'
    act(() => { result.current.noteFollowUpSend()(true) })
    expect(result.current.followUpOptions).toEqual([])
  })

  it('not when a string is only part of a word the person typed', () => {
    const yesNo: AppReplyChipsOffer = { ...APP, options: ['No', 'Yes'] }
    const { result, inputRef } = chips({ app: yesNo, slot: 'chat-1' })
    inputRef.current = 'Not now, check the logs first'
    act(() => { result.current.noteFollowUpSend()(true) })
    expect(result.current.followUpOptions).toEqual(['No', 'Yes'])
    inputRef.current = 'No, wait for the build'
    act(() => { result.current.noteFollowUpSend()(true) })
    expect(result.current.followUpOptions).toEqual([])
  })

  it('not when the picked string is gone from the box, or the box is a slash command', () => {
    const { result, inputRef } = chips({ app: APP, slot: 'chat-1' })
    act(() => result.current.toggleFollowUpOption('Q4a agreed'))
    inputRef.current = ''
    act(() => { result.current.noteFollowUpSend()(false) })
    expect(result.current.followUpOptions).toEqual(APP.options)
    inputRef.current = '/side Q4a agreed'
    act(() => { result.current.noteFollowUpSend()(true) })
    expect(result.current.followUpOptions).toEqual(APP.options)
  })

  it('takes the note and the pick back when the send never went out', () => {
    const { result } = chips({ app: APP, slot: 'chat-1' })
    act(() => result.current.toggleFollowUpOption('Q4a no'))
    let settle: ((sent: boolean) => void) | null = null
    act(() => { settle = result.current.noteFollowUpSend() })
    // Off at once, so a second click during the send repeats nothing.
    expect(result.current.followUpOptions).toEqual([])
    act(() => settle!(false))
    expect(result.current.followUpOptions).toEqual(APP.options)
    expect(result.current.followUpPicked.has('Q4a no')).toBe(true)
  })

  it('settles a steer by the send it handed off to, or by the connection', async () => {
    const { result } = chips({ app: APP, slot: 'chat-1' })
    let settle: ((sent: boolean) => void) = () => {}
    act(() => { settle = result.current.noteFollowUpSend('Q4a agreed') })
    await act(async () => { settleSteer(Promise.resolve(false), settle, true) })
    expect(result.current.followUpOptions).toEqual(APP.options)
    act(() => { settleSteer(undefined, result.current.noteFollowUpSend('Q4a agreed'), true) })
    expect(result.current.followUpOptions).toEqual([])
  })

  it('only in the chat it was sent from, and only for the list that was sent', () => {
    const { result, rerender } = chips({ app: APP, slot: 'chat-1' })
    act(() => { result.current.noteFollowUpSend('Q4a agreed')(true) })
    rerender({ app: { ...APP }, slot: 'chat-2' })
    expect(result.current.followUpOptions).toEqual(APP.options)
    // Back in chat-1, another app's list (older answer) now on offer is not held.
    rerender({ app: { ...APP, key: 'other-app:q', askedAt: 50 }, slot: 'chat-1' })
    expect(result.current.followUpOptions).toEqual(APP.options)
  })

  it('keeps a pick while a question card hides the list and gives it back', () => {
    const { result, rerender } = chips({ app: APP, slot: 'chat-1' })
    act(() => result.current.toggleFollowUpOption('Q4a no'))
    rerender({ app: APP, slot: 'chat-1', card: true })
    expect(result.current.followUpOptions).toEqual([])
    rerender({ app: APP, slot: 'chat-1' })
    expect(result.current.followUpPicked.has('Q4a no')).toBe(true)
  })
})

describe('the status probe across a chat switch', () => {
  // A clock the test moves, so an answer and a switch never share a millisecond.
  let now = 1_000_000
  beforeEach(() => {
    mockStatus.mockReset()
    vi.spyOn(Date, 'now').mockImplementation(() => now)
  })
  afterEach(() => vi.restoreAllMocks())
  const tick = () => { now += 1000 }

  function harness() {
    // Production's default: nothing is re-asked unless the probe itself says so.
    const qc = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client: qc }, children)
    const controls = [control()]
    const hook = renderHook(({ sessionKey }: { sessionKey: string }) => {
      const { replyChips, replyChipsAt } = useSessionControlStatuses(controls, sessionKey)
      return pickAppReplyChips(controls, replyChips, replyChipsAt)
    }, { wrapper, initialProps: { sessionKey: 'dashboard:chat-1' } })
    return { ...hook, qc }
  }

  it("never shows the last visit's list, nor the reply's own line, before the app answers again", async () => {
    let release: (() => void) | null = null
    let calls = 0
    mockStatus.mockImplementation(async (_app: string, _path: string, params: Record<string, string>) => {
      calls++
      // chat-1's re-ask on the way back is held.
      if (params.session_key === 'dashboard:chat-1' && calls === 3) await new Promise<void>(r => { release = r })
      tick()
      return params.session_key === 'dashboard:chat-1' ? { replyChips: { options: [`Q1a agreed ${calls}`] } } : { state: 'none' }
    })
    const { result, rerender } = harness()
    await waitFor(() => expect(result.current?.options).toEqual(['Q1a agreed 1']))
    tick(); rerender({ sessionKey: 'dashboard:chat-2' })
    await waitFor(() => expect(mockStatus).toHaveBeenCalledTimes(2))
    tick(); rerender({ sessionKey: 'dashboard:chat-1' })
    // On the first frame back: an empty list, which hides the bar.
    expect(result.current?.options).toEqual([])
    await waitFor(() => expect(release).not.toBeNull())
    expect(result.current?.options).toEqual([])
    await act(async () => { release?.() })
    await waitFor(() => expect(result.current?.options).toEqual(['Q1a agreed 3']))
  })

  it('keeps an answer that was in flight when the person came back', async () => {
    let release: (() => void) | null = null
    mockStatus.mockImplementation(async (_app: string, _path: string, params: Record<string, string>) => {
      if (params.session_key === 'dashboard:chat-1') {
        await new Promise<void>(r => { release = r })
        tick()
        return { replyChips: { options: ['Q1a agreed'] } }
      }
      return { state: 'none' }
    })
    const { result, rerender } = harness()
    await waitFor(() => expect(release).not.toBeNull())
    tick(); rerender({ sessionKey: 'dashboard:chat-2' })
    tick(); rerender({ sessionKey: 'dashboard:chat-1' })
    // The first request is shared, not repeated; its answer lands after the return.
    await act(async () => { release?.() })
    await waitFor(() => expect(result.current?.options).toEqual(['Q1a agreed']))
    expect(mockStatus.mock.calls.filter(c => c[2].session_key === 'dashboard:chat-1')).toHaveLength(1)
  })

  it('drops the list when the re-ask fails', async () => {
    let fail = false
    mockStatus.mockImplementation(async () => {
      tick()
      if (fail) throw new Error('app down')
      return { replyChips: { options: ['Q1a agreed'] } }
    })
    const { result, qc } = harness()
    await waitFor(() => expect(result.current?.options).toEqual(['Q1a agreed']))
    fail = true
    await act(async () => { await qc.invalidateQueries() })
    await waitFor(() => expect(result.current).toBeNull())
  })

  it('gives a newer answer a later time, so a send against the old one stops holding', async () => {
    mockStatus.mockImplementation(async () => { tick(); return { replyChips: { options: ['Q1a agreed'] } } })
    const { result, qc } = harness()
    await waitFor(() => expect(result.current?.options).toEqual(['Q1a agreed']))
    const first = result.current!.askedAt
    await act(async () => { await qc.invalidateQueries() })
    await waitFor(() => expect(result.current!.askedAt).toBeGreaterThan(first))
  })

  it("re-asks on every switch into a chat and never shows another chat's strings", async () => {
    const answers: Record<string, unknown[]> = {
      'dashboard:chat-1': [{ replyChips: { options: ['Q1a agreed'] } }, { state: 'none' }],
      'dashboard:chat-2': [{ state: 'none' }],
    }
    let release: (() => void) | null = null
    mockStatus.mockImplementation(async (_app: string, _path: string, params: Record<string, string>) => {
      const key = params.session_key
      // chat-2's first answer is held, so the switch renders before it lands.
      if (key === 'dashboard:chat-2' && !release) await new Promise<void>(r => { release = r })
      const list = answers[key]
      tick()
      return list.length > 1 ? list.shift() : list[0]
    })
    const { result, rerender } = harness()
    await waitFor(() => expect(result.current?.options).toEqual(['Q1a agreed']))

    tick(); rerender({ sessionKey: 'dashboard:chat-2' })
    // While chat-2's answer is in flight, chat-1's strings are not shown here.
    expect(result.current).toBeNull()
    await act(async () => { release?.() })
    await waitFor(() => expect(mockStatus).toHaveBeenCalledTimes(2))
    expect(result.current).toBeNull()

    // Back into chat-1: asked again, and the app has closed the question since.
    tick(); rerender({ sessionKey: 'dashboard:chat-1' })
    await waitFor(() => expect(mockStatus).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(result.current).toBeNull())
  })
})
