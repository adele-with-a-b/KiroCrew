/**
 * `version_notice_dismissed` (owner sockets only): an untested-version notice
 * was dismissed on some device. The frame must mark the dismissals read stale so
 * every copy of that notice on this page hides without polling, and a reconnect
 * must re-read it, because a frame sent while the socket was down is lost.
 */
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { refreshServerStateAfterReconnect } from '../hooks/websocket/serverState'
import { VERSION_NOTICE_DISMISSALS_QUERY_KEY } from '../pages/chat/UntestedVersionNotice'

vi.mock('../api/client', async () => ({
  ApiError: (await vi.importActual<typeof import('../api/apiError')>('../api/apiError')).ApiError,
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
    getCredentialRedaction: () => new Promise(() => {}),
  },
}))

const WS_INSTANCES: MockWebSocket[] = []
class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn()
  constructor() { WS_INSTANCES.push(this) }
  simulateOpen() { this.readyState = MockWebSocket.OPEN; this.onopen?.(new Event('open')) }
  simulateMessage(data: object) { this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) })) }
}

describe('useWebSocket version_notice_dismissed frame', () => {
  let qc: QueryClient
  beforeEach(() => {
    WS_INSTANCES.length = 0
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    vi.stubGlobal('WebSocket', MockWebSocket)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('marks the dismissals read stale', () => {
    const store = createTestStore()
    renderHook(() => useWebSocket(), {
      wrapper: ({ children }: { children: React.ReactNode }) =>
        createElement(Provider, { store }, createElement(QueryClientProvider, { client: qc }, children)),
    })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })
    // Seeded AFTER the open, so only the frame can invalidate it.
    qc.setQueryData(VERSION_NOTICE_DISMISSALS_QUERY_KEY, { dismissed: {} })
    expect(qc.getQueryState(VERSION_NOTICE_DISMISSALS_QUERY_KEY)?.isInvalidated).toBe(false)
    act(() => { ws.simulateMessage({ type: 'version_notice_dismissed', data: { harness: 'claude', version: '0.109.0' } }) })
    expect(qc.getQueryState(VERSION_NOTICE_DISMISSALS_QUERY_KEY)?.isInvalidated).toBe(true)
  })

  it('is re-read after a reconnect, which may have missed the frame', () => {
    qc.setQueryData(VERSION_NOTICE_DISMISSALS_QUERY_KEY, { dismissed: {} })
    refreshServerStateAfterReconnect(qc)
    expect(qc.getQueryState(VERSION_NOTICE_DISMISSALS_QUERY_KEY)?.isInvalidated).toBe(true)
  })
})
