/**
 * Chat sidebar: the "Needs you" group, the loud row, the filter and the tab count.
 *
 * The rule (`needsYouOf`) decides; the list draws it loud, gathers it on top
 * oldest first, counts it on the filter button, and filters to it. A chat the
 * rule calls settled is never drawn waiting, and the unread dot is the quiet one.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { render, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  // One component per tag: a fresh type on every read would remount the subtree.
  const byTag = new Map<string, ReturnType<typeof make>>()
  const motion = new Proxy({}, {
    get: (_t, tag: string) => {
      if (!byTag.has(tag)) byTag.set(tag, make(tag))
      return byTag.get(tag)
    },
  })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: () => vi.fn().mockResolvedValue([]),
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'
import { selectChatsWaitingOnYou } from '../lib/needsYouCount'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

const NOW = Date.parse('2026-11-26T11:42:00Z')
const at = (min: number) => new Date(NOW - min * 60_000).toISOString()

const slots = [
  // The gateway's summary: two asks, the oldest 34 minutes old, on a known row.
  { key: 'train', title: 'Release train', running: false, messages: 4, agent: 'kirocrew', last_ts: at(1),
    needs_you: { count: 2, oldest_ts: (NOW - 34 * 60_000) / 1000, first_words: 'Q4a Ship 0.13.2 tonight?', row_id: 'm-q4a', kinds: ['labelled'] } },
  // No summary field: the flags speak. An approval, 12 minutes old.
  { key: 'flaky', title: 'Flaky test sweep', running: false, messages: 4, agent: 'kirocrew', pending_approval: true, last_turn_ts: at(12), last_ts: at(12) },
  // An idle reply that ends with choices, 2 minutes old.
  { key: 'docs', title: 'Docs site refresh', running: false, messages: 4, agent: 'kirocrew', has_options: true, options: ['Drop it', 'Keep it'], options_ts: at(2), last_ts: at(2) },
  // The gateway says it waits on nobody, whatever the flag says.
  { key: 'settled', title: 'Settled chat', running: false, messages: 4, agent: 'kirocrew', pending_approval: true, needs_you: null, last_ts: at(3) },
  { key: 'digest', title: 'Morning digest', running: false, messages: 4, agent: 'kirocrew', pinned: true, last_message: 'Digest ready', last_ts: at(5) },
  { key: 'trip', title: 'Trip planner', running: false, messages: 4, agent: 'kirocrew', last_message: 'Saved the plan', last_ts: at(8) },
] as unknown as ChatSlot[]

function makeStore() {
  return createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: ['trip'], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: null, slotStatusDetail: {}, workflowRuns: {}, subagents: {}, slotActivity: {}, automations: {},
    } as unknown as RootState['chat'],
  })
}

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={makeStore()}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots as never} activeSlot={null} unreadSlots={['trip']}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

const rowOf = (container: HTMLElement, key: string) => container.querySelectorAll<HTMLElement>(`[data-slot-key="${key}"] .session-row`)

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
})
afterEach(() => {
  vi.useRealTimers()
  localStorage.clear()
  vi.clearAllMocks()
})

describe('chat sidebar — Needs you', () => {
  it('gathers the waiting chats on top, oldest wait first, under a heading that reads its count', async () => {
    const { container } = renderSidebar()
    const group = await waitFor(() => {
      const g = container.querySelector<HTMLElement>('[data-testid="needs-you-group"]')
      expect(g).toBeTruthy()
      return g!
    })
    expect(within(group).getByTestId('needs-you-group-header').getAttribute('aria-label')).toBe('Needs you: 3 chats wait on you')
    expect([...group.querySelectorAll('[data-slot-key]')].map(n => n.getAttribute('data-slot-key'))).toEqual(['train', 'flaky', 'docs'])
    expect(within(group).getByTestId('needs-you-rest-header').textContent).toBe('Everything else3')
    // Unfiled, so drawn once: the group is where they are.
    for (const key of ['train', 'flaky', 'docs']) expect(rowOf(container, key)).toHaveLength(1)
  })

  it('draws a waiting row loud: the ask, the open asks, the wait', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(rowOf(container, 'train')).toHaveLength(1))
    const train = rowOf(container, 'train')[0]
    expect(train.classList.contains('session-needs-you')).toBe(true)
    expect(within(train).getByTestId('session-needs-you').textContent).toBe('Needs you · Q4a Ship 0.13.2 tonight?')
    expect(within(train).getByTestId('session-needs-you-more').textContent).toBe('+1 more')
    expect(within(train).getByTestId('session-needs-you-count').getAttribute('aria-label')).toBe('2 open asks')
    expect(within(train).getByTestId('session-needs-you-age').textContent).toBe('34m')
    // With no first words, the line says the row's own word for that ask.
    expect(within(rowOf(container, 'flaky')[0]).getByTestId('session-needs-you').textContent).toBe('Needs approval')
    expect(within(rowOf(container, 'docs')[0]).getByTestId('session-needs-you').textContent).toBe('Waiting on you')
  })

  it('never draws a chat the gateway calls settled as waiting', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(rowOf(container, 'settled')).toHaveLength(1))
    const settled = rowOf(container, 'settled')[0]
    expect(settled.classList.contains('session-needs-you')).toBe(false)
    expect(within(settled).queryByTestId('session-needs-you')).toBeNull()
    expect(container.querySelector('[data-testid="needs-you-group"] [data-slot-key="settled"]')).toBeNull()
  })

  it('keeps unread as a small grey dot, lit as before', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(rowOf(container, 'trip')).toHaveLength(1))
    const dot = within(rowOf(container, 'trip')[0]).getByTestId('session-unread-dot')
    expect(dot.className).toContain('bg-muted')
    expect(dot.getAttribute('aria-label')).toBe('Agent finished — your turn')
  })

  it('puts the waiting count, in its colour, on the filter button', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(container.querySelector('[data-badge-tone="needs"]')?.textContent).toBe('3'))
  })

  it('filters to the waiting chats alone, pinned ones included in the narrowing, and says what it hides', async () => {
    localStorage.setItem('mc-session-needs-you-only', '1')
    const { container } = renderSidebar()
    await waitFor(() => expect(container.querySelector('[data-testid="needs-you-hidden-note"]')?.textContent).toBe('3 other chats hidden by the Needs you filter'))
    expect(container.querySelector('[data-testid="needs-you-group-header"]')).toBeNull()
    expect(rowOf(container, 'digest')).toHaveLength(0)
    expect(rowOf(container, 'trip')).toHaveLength(0)
    expect(container.querySelector('[data-testid="filter-chip-needs_you"]')?.textContent).toContain('Needs you · 3')
  })
})

describe('tab title count', () => {
  it('counts the chats that wait on the person, never the unread ones', () => {
    expect(selectChatsWaitingOnYou(makeStore().getState())).toBe(3)
  })
})
