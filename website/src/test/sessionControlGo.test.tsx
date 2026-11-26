/**
 * `useSessionControlGo()`: a session control opens its OWN box in another chat,
 * on a real click in it, through the route a status item's go takes, and the
 * landed chat's box reads the request with `via: 'control'`.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, renderHook, screen } from '@testing-library/react'

import { createTestStore, renderHookWithProviders, renderWithProviders } from './helpers'
import { addSlotOptimistic } from '../store/dashboardSlice'
import { setActiveSlot } from '../store/chatSlice'
import type { ChatSlot } from '../types'

// jsdom marks every event it is handed as untrusted, and `isTrusted` cannot be
// set on one, so a test stands in for a real click here.
const trust = vi.hoisted(() => ({ next: true }))
vi.mock('../components/trustedClick', () => ({ isTrustedClick: () => trust.next }))

import { SessionControlRequestScope, useSessionControlGo, type SessionControlGoResult } from '../app-sdk/sessionControlRequest'
import { getActiveStatusGo, setActiveStatusGo } from '../lib/statusGo'
import { openStatusTarget, useStatusLinks } from '../pages/chat/useStatusLinks'
import { onLinkPreviewNav } from '../linkPreview/linkPreviewNav'

const KEY = 'refs-app:questions'

/** The chat page with `here` on screen and `others` open as tabs. */
function chatStore(here: string, ...others: string[]) {
  const store = createTestStore()
  for (const key of [here, ...others]) store.dispatch(addSlotOptimistic({ key, title: key, agent: 'kirocrew' } as unknown as ChatSlot))
  store.dispatch(setActiveSlot(here))
  return store
}

type Go = (sid: string, payload?: unknown) => SessionControlGoResult

/** An app's box in chat `chat-1`: a button that goes to `sid` with `payload`.
 *  `held.go` is the same go, for a call made outside any click. */
function Box({ sid, payload, seen, held }: { sid: string; payload?: unknown; seen: SessionControlGoResult[]; held: { go?: Go } }) {
  const go = useSessionControlGo()
  held.go = go ?? undefined
  return <button type="button" onClick={() => seen.push(go!(sid, payload))}>go</button>
}

function mountBox(sid: string, payload?: unknown, store = chatStore('chat-1', 'chat-4')) {
  const seen: SessionControlGoResult[] = []
  const held: { go?: Go } = {}
  renderWithProviders(
    <SessionControlRequestScope sessionKey="dashboard:chat-1" control={KEY}>
      <Box sid={sid} payload={payload} seen={seen} held={held} />
    </SessionControlRequestScope>,
    { route: '/chat', store },
  )
  return { seen, store, held }
}

afterEach(() => {
  cleanup()
  setActiveStatusGo(null)
  trust.next = true
})

describe('useSessionControlGo', () => {
  it('is null outside a session control, and in a scope whose control key names no app', () => {
    expect(renderHookWithProviders(() => useSessionControlGo()).result.current).toBeNull()
    const seen: SessionControlGoResult[] = []
    const held: { go?: Go } = {}
    renderWithProviders(
      <SessionControlRequestScope sessionKey="dashboard:chat-1" control="questions">
        <Box sid="chat-4" seen={seen} held={held} />
      </SessionControlRequestScope>,
      { route: '/chat', store: chatStore('chat-1', 'chat-4') },
    )
    expect(held.go).toBeUndefined()
  })

  it("on a click in the box, switches to the chat and queues this control's open there, as the box's own", () => {
    const { seen, store } = mountBox('chat-4', { label: 'Q4a' })
    fireEvent.click(screen.getByText('go'))
    expect(seen).toEqual([{ ok: true }])
    expect(store.getState().chat.activeSlot).toBe('chat-4')
    expect(getActiveStatusGo()).toMatchObject({
      to: 'chat-4',
      from: 'chat-1',
      appName: 'refs-app',
      pending: { target: { kind: 'sessionControl', id: 'questions' }, arg: { label: 'Q4a' }, via: 'control' },
    })
  })

  it("takes the chat's session key as well as its slot key", () => {
    const { seen, store } = mountBox('dashboard:chat-4')
    fireEvent.click(screen.getByText('go'))
    expect(seen).toEqual([{ ok: true }])
    expect(store.getState().chat.activeSlot).toBe('chat-4')
    expect(getActiveStatusGo()?.pending).toEqual({ target: { kind: 'sessionControl', id: 'questions' }, arg: undefined, via: 'control' })
  })

  it('goes nowhere without a real click in the box, and one click is one go', () => {
    trust.next = false
    const { seen, store, held } = mountBox('chat-4')
    fireEvent.click(screen.getByText('go'))
    expect(seen).toEqual([{ ok: false, reason: 'no_click' }])
    expect(getActiveStatusGo()).toBeNull()
    expect(store.getState().chat.activeSlot).toBe('chat-1')
    // A call made outside any click (a timer, an effect) has no grant either.
    trust.next = true
    expect(held.go!('chat-4')).toEqual({ ok: false, reason: 'no_click' })
    expect(getActiveStatusGo()).toBeNull()
    // A real click grants one go: a second call on the same click is refused.
    fireEvent.click(screen.getByText('go'))
    expect(seen[1]).toEqual({ ok: true })
    expect(held.go!('chat-4')).toEqual({ ok: false, reason: 'no_click' })
  })

  it('refuses a bad chat key or payload before it spends the click', () => {
    const cyclic: Record<string, unknown> = {}
    cyclic.self = cyclic
    for (const [sid, payload, reason] of [
      ['', undefined, 'bad_chat'],
      ['../chat-4', undefined, 'bad_chat'],
      ['chat-4', cyclic, 'not_json'],
      ['chat-4', () => 1, 'not_json'],
      ['chat-4', { big: 'x'.repeat(4097) }, 'too_large'],
    ] as const) {
      cleanup()
      const { seen } = mountBox(sid, payload)
      fireEvent.click(screen.getByText('go'))
      expect(seen).toEqual([{ ok: false, reason }])
      expect(getActiveStatusGo()).toBeNull()
    }
  })

  it('opens only its own control: the go names the scope\'s app and control id, never one the app passes', () => {
    const { seen } = mountBox('chat-4', { sessionControl: 'other', appName: 'other-app' })
    fireEvent.click(screen.getByText('go'))
    expect(seen).toEqual([{ ok: true }])
    expect(getActiveStatusGo()).toMatchObject({ appName: 'refs-app', pending: { target: { kind: 'sessionControl', id: 'questions' } } })
  })

  it("the landed chat hands the open over as via 'control'; a status go still says 'status'", async () => {
    const opener = vi.fn().mockReturnValue(true)
    setActiveStatusGo({ to: 'chat-4', from: 'chat-1', appName: 'refs-app', pending: { target: { kind: 'sessionControl', id: 'questions' }, arg: 1, via: 'control' } })
    const landed = renderHook(() => useStatusLinks({ statusParam: null, activeSlot: 'chat-4', open: opener }))
    await act(async () => {})
    expect(opener).toHaveBeenCalledWith('chat-4', 'refs-app', { kind: 'sessionControl', id: 'questions' }, 1, 'control')
    landed.unmount()
    opener.mockClear()
    setActiveStatusGo({ to: 'chat-4', from: 'chat-1', appName: 'refs-app', pending: { target: { kind: 'sessionControl', id: 'questions' }, arg: 2 } })
    renderHook(() => useStatusLinks({ statusParam: null, activeSlot: 'chat-4', open: opener }))
    await act(async () => {})
    // A status go's open is made exactly as before: four arguments, no `via`.
    expect(opener).toHaveBeenCalledWith('chat-4', 'refs-app', { kind: 'sessionControl', id: 'questions' }, 2)
  })

  it("sends the composer's open request with the go's via, 'status' when it names none", () => {
    const vias: string[] = []
    const off = onLinkPreviewNav((req) => { if (req.kind === 'open') vias.push(req.via); return true })
    try {
      openStatusTarget('chat-4', 'refs-app', { kind: 'sessionControl', id: 'questions' }, null)
      openStatusTarget('chat-4', 'refs-app', { kind: 'sessionControl', id: 'questions' }, null, 'control')
    } finally { off() }
    expect(vias).toEqual(['status', 'control'])
  })
})
