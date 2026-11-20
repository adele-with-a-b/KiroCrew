/**
 * Find in chat in a Bot's chat (#39585). The Bot chat's transcript is
 * virtualized, so the browser's own find never reaches a message scrolled out
 * of view; ⌘F / Ctrl+F must open the Sessions chat's find over this pane, find
 * every loaded message, and bring an unmounted match row into the window.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, renderHook, screen, waitFor } from '@testing-library/react'
import { useRef } from 'react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { RootState } from '../store'
import { api } from '../api/client'
import ChatPane from './ChatPane'
import ChatMessageList, { type VirtualTranscriptHandle } from '../app-sdk/ChatMessageList'
import type { MessageRenderer } from '../app-sdk/messageRenderers'
import SearchHighlightContext, { useCurrentOcc } from '../hooks/SearchHighlightContext'
import type { DisplayItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'
import { useChatPaneFind } from './useChatPaneFind'
import { useNavToDisplayIndex } from '../pages/chat/useNavToDisplayIndex'

// The real hook, spied on: the pane's find must jump through the Sessions
// chat's own jump, not a copy of it.
vi.mock('../pages/chat/useNavToDisplayIndex', async (importOriginal) => {
  const real = await importOriginal<typeof import('../pages/chat/useNavToDisplayIndex')>()
  return { ...real, useNavToDisplayIndex: vi.fn(real.useNavToDisplayIndex) }
})

const { focusComposer } = vi.hoisted(() => ({ focusComposer: vi.fn() }))
vi.mock('../pages/chat/composerFocus', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../pages/chat/composerFocus')>()),
  focusComposer,
}))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Kiro Crew', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

afterEach(() => { cleanup(); vi.useRealTimers(); focusComposer.mockClear(); vi.mocked(useNavToDisplayIndex).mockClear() })

/** A 40-turn chat: 80 messages, the word "needle" only in the first prompt. */
const longChat: ChatMessage[] = Array.from({ length: 80 }, (_, i) => ({
  role: i % 2 === 0 ? 'user' : 'assistant',
  content: i === 0 ? 'the first prompt has a needle in it' : `message ${i}`,
  cls: '',
  ts: `2026-11-20T10:00:${String(i).padStart(2, '0')}Z`,
}))
const rows: DisplayItem[] = longChat.map((msg, idx) => ({ kind: 'single', msg, idx }))

const press = (init: KeyboardEventInit): KeyboardEvent => {
  const event = new KeyboardEvent('keydown', { key: 'f', cancelable: true, ...init })
  act(() => { document.dispatchEvent(event) })
  return event
}

function setup(enabled: boolean) {
  vi.useFakeTimers()
  const list = {
    mountIndex: vi.fn(() => true),
    estimateRowTop: vi.fn(() => 4000),
  } as unknown as VirtualTranscriptHandle
  // The window is at the bottom of the chat: no row of the first turn is mounted.
  const scroller = document.createElement('div')
  Object.defineProperty(scroller, 'clientHeight', { value: 600 })
  Object.defineProperty(scroller, 'scrollHeight', { value: 10000 })
  const scrollerRef = { current: scroller }
  const hook = renderHook(({ on }) => useChatPaneFind({
    enabled: on,
    messages: longChat,
    slotKey: 'member-bot',
    displayItemsRef: { current: rows },
    listRef: { current: list },
    scrollerRef,
  }), { initialProps: { on: enabled } })
  return { ...hook, list, scroller, scrollerRef }
}

describe('useChatPaneFind', () => {
  it('opens on Ctrl+F, finds a message far above the window and brings its row in', () => {
    const { result, list, scroller, scrollerRef } = setup(true)
    expect(useNavToDisplayIndex).toHaveBeenCalledWith(expect.objectContaining({ scrollerRef }))
    const event = press({ ctrlKey: true })
    expect(result.current.search.isOpen).toBe(true)
    expect(event.defaultPrevented).toBe(true)

    act(() => { result.current.search.setTerm('needle') })
    act(() => { vi.advanceTimersByTime(60) })
    expect(result.current.search.matches).toEqual([{ msgIdx: 0, occ: 0 }])
    expect(result.current.highlight).toMatchObject({ term: 'needle', currentMessageIdx: 0, currentOccurrenceIdx: 0 })
    expect(list.mountIndex).toHaveBeenCalledWith(0, undefined)

    // A far row takes several frames to commit: nothing scrolls while it is
    // missing (no jump to an estimate, no jump to the top)...
    act(() => { vi.advanceTimersByTime(100) })
    expect(scroller.scrollTop).toBe(0)
    // ...and once it mounts, the jump centres it.
    const row = document.createElement('div')
    row.setAttribute('data-display-index', '0')
    row.getBoundingClientRect = () => ({ top: 4000, height: 80, bottom: 4080, left: 0, right: 0, width: 0, x: 0, y: 4000, toJSON: () => ({}) })
    scroller.appendChild(row)
    act(() => { vi.advanceTimersByTime(20) })
    expect(scroller.scrollTop).toBe(4000 - 300)
  })

  it('leaves the key to the browser while the pane does not offer the find', () => {
    const { result } = setup(false)
    const event = press({ ctrlKey: true })
    expect(result.current.search.isOpen).toBe(false)
    expect(event.defaultPrevented).toBe(false)
  })

  it('closes an open bar when the pane stops offering the find, leaving focus where the user moved it', () => {
    const { result, rerender } = setup(true)
    press({ ctrlKey: true })
    act(() => { result.current.search.setTerm('needle') })
    expect(result.current.search.isOpen).toBe(true)
    rerender({ on: false })
    expect(result.current.search.isOpen).toBe(false)
    expect(result.current.search.term).toBe('')
    // The user just moved into a side panel or drawer: pulling the caret back
    // into the composer would take their focus away.
    expect(focusComposer).not.toHaveBeenCalled()
  })

  it('still hands typing back to the composer when the user closes the bar with Escape', () => {
    const { result } = setup(true)
    press({ ctrlKey: true })
    press({ key: 'Escape' })
    expect(result.current.search.isOpen).toBe(false)
    expect(focusComposer).toHaveBeenCalledTimes(1)
  })
})

/** Prints what `useCurrentOcc` reads for the row it is drawn in. */
function OccProbe({ text }: { text: string }) {
  return <span data-testid={text}>{String(useCurrentOcc())}</span>
}
const probeRenderer: MessageRenderer = {
  id: 'user',
  roles: ['user', 'assistant'],
  render: (m) => <OccProbe key={m.ts} text={m.content} />,
}

function List({ searchRows }: { searchRows: boolean }) {
  const ref = useRef<VirtualTranscriptHandle | null>(null)
  const messages: ChatMessage[] = [
    { role: 'user', content: 'first', cls: '', ts: 'a' },
    { role: 'user', content: 'second', cls: '', ts: 'b' },
  ]
  return (
    <SearchHighlightContext.Provider value={{ term: 'x', caseSensitive: false, currentMessageIdx: 1, currentOccurrenceIdx: 0 }}>
      <ChatMessageList ref={ref} messages={messages} running={false} renderers={[probeRenderer]} searchRows={searchRows} />
    </SearchHighlightContext.Provider>
  )
}

describe('ChatMessageList searchRows', () => {
  it('tells each row its message index, so only the current match row is current', () => {
    render(<List searchRows />)
    expect(screen.getByTestId('first').textContent).toBe('-1')
    expect(screen.getByTestId('second').textContent).toBe('0')
  })

  it('leaves rows unscoped when the host runs no find of its own', () => {
    render(<List searchRows={false} />)
    expect(screen.getByTestId('second').textContent).toBe('-1')
  })
})

function renderPane(slot: string, content: string, props: { findInChat: boolean; topInset?: number }) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
  })
  vi.mocked(api.chatSlotDetail).mockResolvedValue({
    messages: [{ role: 'user', content, cls: '', ts: '2026-11-20T10:00:00Z' }],
    running: false, has_more: false, total: 1,
  } as unknown as Awaited<ReturnType<typeof api.chatSlotDetail>>)
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slot, messages: 1, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const pane = (p: { findInChat: boolean; topInset?: number }) => (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slot} frameless findInChat={p.findInChat} topInset={p.topInset} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>
  )
  const view = render(pane(props))
  return { ...view, rerenderPane: (p: { findInChat: boolean; topInset?: number }) => view.rerender(pane(p)) }
}

describe('ChatPane rows across findInChat', () => {
  it('keeps every mounted row when the pane stops and starts answering the find', async () => {
    const view = renderPane('member-bot-rows', 'keep this row mounted', { findInChat: true })
    const row = await waitFor(() => screen.getByText('keep this row mounted'))
    // A side panel takes focus: the pane stops answering ⌘F. The row's DOM
    // node must survive; a remount would drop its state (an open menu, a
    // half-selected quote, an expanded block).
    view.rerenderPane({ findInChat: false })
    expect(screen.getByText('keep this row mounted')).toBe(row)
    view.rerenderPane({ findInChat: true })
    expect(screen.getByText('keep this row mounted')).toBe(row)
  })
})

describe('ChatPane find bar and pinned prompt', () => {
  it('moves the pinned-prompt band down under the open find bar', async () => {
    const view = renderPane('member-bot-pin', 'a prompt', { findInChat: true, topInset: 40 })
    await waitFor(() => screen.getByText('a prompt'))
    // The band is the parent of the zero-height fold sentinel the banner sticks to.
    const band = () => view.container.querySelector('div[aria-hidden].h-0')!.parentElement as HTMLElement
    expect(band().style.top).toBe('40px')
    press({ ctrlKey: true })
    expect(screen.getByRole('textbox')).toBeTruthy()
    expect(band().style.top).toBe('88px')
    press({ key: 'Escape' })
    expect(band().style.top).toBe('40px')
  })
})
