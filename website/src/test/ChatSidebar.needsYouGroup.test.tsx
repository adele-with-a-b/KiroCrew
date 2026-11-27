/**
 * Chat sidebar: a waiting chat is drawn once, in its own place.
 *
 * The rule (`needsYouOf`) decides what waits; `useNeedsGroup` decides where it
 * is drawn: in its place, lit; under the outermost collapsed folder holding it;
 * or, while the Needs you filter narrows, in the oldest-first list on top with
 * its folder's crumb, and nowhere else. The chip under the search field carries
 * the waiting count; the funnel counts unread.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { act, fireEvent, render, waitFor, within } from '@testing-library/react'
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
// The gateway's folders are the test's own: a refetch must not empty the tree.
const gatewayFolders = vi.hoisted(() => ({ current: [] as unknown[] }))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop) => (prop === 'chatFolders' ? () => Promise.resolve(gatewayFolders.current) : vi.fn().mockResolvedValue([])),
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
import { sseSlots } from '../store/dashboardSlice'
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

function makeStore(list: ChatSlot[] = slots, unread: string[] = ['trip']) {
  return createTestStore({
    dashboard: {
      status: {}, connected: true, slots: list, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: unread, updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: null, slotStatusDetail: {}, workflowRuns: {}, subagents: {}, slotActivity: {}, automations: {},
    } as unknown as RootState['chat'],
  })
}

function renderSidebar(list: ChatSlot[] = slots, opts: { folders?: unknown[]; unread?: string[] } = {}) {
  const unread = opts.unread ?? ['trip']
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  gatewayFolders.current = opts.folders ?? []
  qc.setQueryData(['chat-folders'], gatewayFolders.current)
  const store = makeStore(list, unread)
  const view = (rows: ChatSlot[]) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={rows as never} activeSlot={null} unreadSlots={unread}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const utils = render(view(list))
  /** A new frame from the gateway: the store and the list's props both move. */
  const rerenderSlots = (rows: ChatSlot[]) => {
    act(() => { store.dispatch(sseSlots(rows)) })
    utils.rerender(view(rows))
  }
  return { ...utils, rerenderSlots, qc }
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

describe('chat sidebar — Needs you, unfiled', () => {
  it('leaves each waiting chat in its place, lit, with no group on top', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(rowOf(container, 'train')).toHaveLength(1))
    expect(container.querySelector('[data-testid="needs-you-group"] [data-slot-key]')).toBeNull()
    for (const key of ['train', 'flaky', 'docs']) {
      expect(rowOf(container, key)).toHaveLength(1)
      expect(rowOf(container, key)[0].classList.contains('session-needs-you')).toBe(true)
    }
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
  })

  it('keeps unread as a small grey dot, lit as before', async () => {
    const { container } = renderSidebar()
    await waitFor(() => expect(rowOf(container, 'trip')).toHaveLength(1))
    const dot = within(rowOf(container, 'trip')[0]).getByTestId('session-unread-dot')
    expect(dot.className).toContain('bg-muted')
    expect(dot.getAttribute('aria-label')).toBe('Agent finished — your turn')
  })

  it('shows the waiting count once, on the Needs you chip; the funnel counts unread', async () => {
    const { container } = renderSidebar()
    const chip = await waitFor(() => {
      const c = container.querySelector<HTMLElement>('[data-testid="needs-you-off-chip"]')
      expect(c).toBeTruthy()
      return c!
    })
    expect(chip.textContent).toBe('Needs you3')
    expect(chip.getAttribute('aria-label')).toBe('Needs you: 3 chats wait on you')
    expect(container.querySelector('[data-testid="filter-menu-badge"]')?.textContent).toBe('1')
    // One click on the chip turns the filter on.
    fireEvent.click(chip)
    await waitFor(() => expect(container.querySelector('[data-testid="filter-chip-needs_you"]')?.textContent).toContain('Needs you · 3'))
    expect(container.querySelector('[data-testid="needs-you-off-chip"]')).toBeNull()
  })

  it('filters to the waiting chats alone, oldest first with no crumb, pinned ones included in the narrowing, and says what it hides', async () => {
    localStorage.setItem('mc-session-needs-you-only', '1')
    const { container } = renderSidebar()
    await waitFor(() => expect(container.querySelector('[data-testid="needs-you-hidden-note"]')?.textContent).toBe('3 other chats hidden by the Needs you filter'))
    expect([...container.querySelectorAll('[data-testid="needs-you-group"] [data-slot-key]')].map(n => n.getAttribute('data-slot-key'))).toEqual(['train', 'flaky', 'docs'])
    for (const key of ['train', 'flaky', 'docs']) expect(rowOf(container, key)).toHaveLength(1)
    expect(container.querySelector('[data-testid="session-row-folder-crumb"]')).toBeNull()
    expect(rowOf(container, 'digest')).toHaveLength(0)
    expect(rowOf(container, 'trip')).toHaveLength(0)
    expect(container.querySelector('[data-testid="filter-chip-needs_you"]')?.textContent).toContain('Needs you · 3')
  })
})

describe('chat sidebar — Needs you, filed', () => {
  // Made-up folders: Harbor open with two waits, Quill collapsed with one,
  // Pebble open whose only chat waits, Sundial collapsed with nothing waiting.
  const folders = [
    { id: 'f-harbor', name: 'Harbor', collapsed: false, order: 0 },
    { id: 'f-quill', name: 'Quill', collapsed: true, order: 1 },
    { id: 'f-pebble', name: 'Pebble', collapsed: false, order: 2 },
    { id: 'f-sundial', name: 'Sundial', collapsed: true, order: 3 },
  ]
  const quiet = (key: string, title: string, folder: string, min: number) =>
    ({ key, title, running: false, messages: 4, agent: 'kirocrew', folder_id: folder, last_message: 'Done', last_ts: at(min), needs_you: null })
  const asks = (key: string, title: string, folder: string | undefined, min: number, words: string) =>
    ({ key, title, running: false, messages: 4, agent: 'kirocrew', ...(folder ? { folder_id: folder } : {}), last_ts: at(min),
      needs_you: { count: 1, oldest_ts: (NOW - min * 60_000) / 1000, first_words: words, row_id: `m-${key}`, kinds: ['labelled'] } })
  const filed = [
    asks('h-cond', 'Harbor conductor', 'f-harbor', 40, 'Q2a Merge the dock fix?'),
    asks('h-docs', 'Harbor docs', 'f-harbor', 10, 'Q5a Keep the old page?'),
    quiet('h-idle', 'Harbor idle', 'f-harbor', 30),
    asks('q-import', 'Import parser', 'f-quill', 25, 'Q1a Drop the CSV path?'),
    quiet('q-two', 'Quill two', 'f-quill', 50),
    quiet('q-three', 'Quill three', 'f-quill', 60),
    quiet('q-four', 'Quill four', 'f-quill', 70),
    asks('p-lone', 'Pebble lone', 'f-pebble', 5, 'Q3a Ship it?'),
    quiet('s-one', 'Sundial one', 'f-sundial', 20),
    asks('loose', 'Loose ask', undefined, 15, 'Q7a Rename it?'),
  ] as unknown as ChatSlot[]
  // Quill's waiting chat is unread, so a dot on Quill would be a second mark for
  // it; Sundial's unread chat is folded away, so Sundial's dot stays.
  const unread = ['q-import', 's-one']

  it('draws every waiting chat once, in its own folder, and no group on top', async () => {
    const { container } = renderSidebar(filed, { folders, unread })
    await waitFor(() => expect(rowOf(container, 'h-cond')).toHaveLength(1))
    for (const key of ['h-cond', 'h-docs', 'q-import', 'p-lone', 'loose']) expect(rowOf(container, key)).toHaveLength(1)
    expect(container.querySelector('[data-testid="needs-you-group"] [data-slot-key]')).toBeNull()
    expect(container.querySelector('[data-testid="session-row-folder-crumb"]')).toBeNull()
    // Pebble's only chat waits: Pebble keeps it, the folder is not emptied by the wait.
    expect(container.querySelector('[data-folder-row="f-pebble"]')).toBeTruthy()
  })

  it('draws a collapsed folder\'s waiting chat under it, with the rest counted and no second mark on the folder', async () => {
    const { container } = renderSidebar(filed, { folders, unread })
    const strip = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('[data-testid="folder-waits-f-quill"]')
      expect(el).toBeTruthy()
      return el!
    })
    expect([...strip.querySelectorAll('[data-slot-key]')].map(n => n.getAttribute('data-slot-key'))).toEqual(['q-import'])
    expect(within(strip).getByTestId('folder-waits-more-f-quill').textContent).toBe('3 more in Quill')
    // Once: the closed body does not hold it too.
    expect(rowOf(container, 'q-import')).toHaveLength(1)
    // The folder face keeps its count and draws no unread dot for the chat shown under it.
    const quill = container.querySelector<HTMLElement>('[data-folder-row="f-quill"]')!
    expect(quill.textContent).toContain('4')
    expect(within(quill).queryByRole('img', { name: 'Agent finished — your turn' })).toBeNull()
    const sundial = container.querySelector<HTMLElement>('[data-folder-row="f-sundial"]')!
    expect(within(sundial).getByRole('img', { name: 'Agent finished — your turn' })).toBeTruthy()
    expect(container.querySelector('[data-testid="folder-waits-f-sundial"]')).toBeNull()
  })

  it('lists the waits oldest first across folders, each with its folder crumb, and draws no folder', async () => {
    localStorage.setItem('mc-session-needs-you-only', '1')
    const { container } = renderSidebar(filed, { folders, unread })
    await waitFor(() => expect(container.querySelectorAll('[data-testid="needs-you-group"] [data-slot-key]')).toHaveLength(5))
    expect([...container.querySelectorAll('[data-testid="needs-you-group"] [data-slot-key]')].map(n => n.getAttribute('data-slot-key')))
      .toEqual(['h-cond', 'q-import', 'loose', 'h-docs', 'p-lone'])
    for (const key of ['h-cond', 'h-docs', 'q-import', 'p-lone', 'loose']) expect(rowOf(container, key)).toHaveLength(1)
    const crumb = (key: string) => rowOf(container, key)[0].querySelector('[data-testid="session-row-folder-crumb"]')?.textContent ?? null
    expect(crumb('h-cond')).toBe('Harbor')
    expect(crumb('q-import')).toBe('Quill')
    expect(crumb('p-lone')).toBe('Pebble')
    expect(crumb('loose')).toBeNull()
    expect(container.querySelector('[data-folder-row]')).toBeNull()
    expect(container.querySelector('[data-testid^="folder-waits-"]')).toBeNull()
    expect(container.querySelector('[data-testid="needs-you-hidden-note"]')?.textContent).toBe('5 other chats hidden by the Needs you filter')
  })

  it('takes an answered chat out of the list at once and puts it back in its folder once', async () => {
    localStorage.setItem('mc-session-needs-you-only', '1')
    const view = renderSidebar(filed, { folders, unread })
    await waitFor(() => expect(view.container.querySelectorAll('[data-testid="needs-you-group"] [data-slot-key]')).toHaveLength(5))
    const answered = filed.map(s => (s.key === 'h-cond' || s.key === 'q-import' ? { ...s, needs_you: null } : s)) as ChatSlot[]
    view.rerenderSlots(answered)
    await waitFor(() => expect(view.container.querySelectorAll('[data-testid="needs-you-group"] [data-slot-key]')).toHaveLength(3))
    expect(rowOf(view.container, 'h-cond')).toHaveLength(0)
    expect(rowOf(view.container, 'q-import')).toHaveLength(0)
    // The filter off: each is back in its folder, drawn once, quiet.
    fireEvent.click(view.container.querySelector<HTMLElement>('[data-testid="filter-chip-needs_you-clear"], [data-testid="filter-chip-needs_you"] button[aria-label^="Clear"]')!)
    await waitFor(() => expect(rowOf(view.container, 'h-idle')).toHaveLength(1))
    expect(view.container.querySelector('[data-folder-row="f-harbor"]')).toBeTruthy()
    for (const key of ['h-cond', 'q-import']) {
      expect(rowOf(view.container, key)).toHaveLength(1)
      expect(rowOf(view.container, key)[0].classList.contains('session-needs-you')).toBe(false)
    }
    // Quill has nothing left waiting: no strip, its chat is back in the closed body.
    expect(view.container.querySelector('[data-testid="folder-waits-f-quill"]')).toBeNull()
    expect(view.container.querySelector('[data-testid="needs-you-group"] [data-slot-key]')).toBeNull()
  })

  const hover = (c: HTMLElement, key: string) => fireEvent.pointerOver(rowOf(c, key)[0], { pointerType: 'mouse', bubbles: true })
  const leave = (c: HTMLElement) => fireEvent.pointerLeave(c.querySelector('.sidebar-inner')!, { pointerType: 'mouse' })
  const stripKeys = (c: HTMLElement, folder: string) =>
    [...c.querySelectorAll(`[data-testid="folder-waits-${folder}"] [data-slot-key]`)].map(n => n.getAttribute('data-slot-key'))

  it('holds a chat that starts waiting in a collapsed folder until the pointer leaves the list', async () => {
    const view = renderSidebar(filed, { folders, unread })
    await waitFor(() => expect(stripKeys(view.container, 'f-quill')).toEqual(['q-import']))
    hover(view.container, 'h-idle')
    view.rerenderSlots(filed.map(s => (s.key === 'q-two' ? asks('q-two', 'Quill two', 'f-quill', 3, 'Q9a Keep it?') : s)) as unknown as ChatSlot[])
    // Under the pointer nothing joins; the closed body still holds it, out of sight.
    await waitFor(() => expect(view.container.querySelector('[data-testid="needs-you-off-chip"]')?.textContent).toBe('Needs you6'))
    expect(stripKeys(view.container, 'f-quill')).toEqual(['q-import'])
    expect(rowOf(view.container, 'q-two')).toHaveLength(1)
    leave(view.container)
    // It joins in the folder's own order: Quill two is the newer chat.
    await waitFor(() => expect(stripKeys(view.container, 'f-quill')).toEqual(['q-two', 'q-import']))
    expect(rowOf(view.container, 'q-two')).toHaveLength(1)
  })

  it('shows a folder\'s waiting chats under it the moment it is collapsed, pointer over the list or not', async () => {
    const view = renderSidebar(filed, { folders, unread })
    await waitFor(() => expect(rowOf(view.container, 'h-cond')).toHaveLength(1))
    // The strip keeps the folder's own order: collapsing reorders nothing.
    const openOrder = [...view.container.querySelectorAll('[data-slot-key]')]
      .map(n => n.getAttribute('data-slot-key')).filter(k => k === 'h-cond' || k === 'h-docs')
    expect(openOrder).toHaveLength(2)
    hover(view.container, 'h-idle')
    gatewayFolders.current = folders.map(f => (f.id === 'f-harbor' ? { ...f, collapsed: true } : f))
    act(() => { view.qc.setQueryData(['chat-folders'], gatewayFolders.current) })
    await waitFor(() => expect(stripKeys(view.container, 'f-harbor')).toEqual(openOrder))
    expect(view.container.querySelector('[data-testid="folder-waits-more-f-harbor"]')?.textContent).toBe('1 more in Harbor')
    for (const key of ['h-cond', 'h-docs']) expect(rowOf(view.container, key)).toHaveLength(1)
  })

  it('takes an answered chat out of a collapsed folder\'s strip, with no copy left behind', async () => {
    const view = renderSidebar(filed, { folders, unread })
    await waitFor(() => expect(view.container.querySelector('[data-testid="folder-waits-f-quill"]')).toBeTruthy())
    view.rerenderSlots(filed.map(s => (s.key === 'q-import' ? { ...s, needs_you: null } : s)) as ChatSlot[])
    await waitFor(() => expect(view.container.querySelector('[data-testid="folder-waits-f-quill"]')).toBeNull())
    expect(view.container.querySelector('[data-folder-row="f-quill"]')).toBeTruthy()
    expect(rowOf(view.container, 'q-import')).toHaveLength(1)
    expect(view.container.querySelector('[data-testid="needs-you-off-chip"]')?.textContent).toBe('Needs you4')
  })
})

describe('tab title count', () => {
  it('counts the chats that wait on the person, never the unread ones', () => {
    expect(selectChatsWaitingOnYou(makeStore().getState())).toBe(3)
  })
})

describe('a code-channel crew', () => {
  // A code-channel conductor and the worker it dispatched, folded under it; the
  // worker waits on a tool approval. The summaries are the gateway's, as PR 94's
  // `serialize_slots` writes them: the worker's own, none on the conductor.
  const crew = [
    { key: 'lead', title: 'Lockfile audit lead', running: false, messages: 4, agent: 'kirocrew', last_ts: at(3), needs_you: null,
      code_channel: { channel_name: 'audit', status: 'active', web_url: '' } },
    { key: 'worker', title: 'Lockfile audit worker', running: false, messages: 4, agent: 'kirocrew', last_ts: at(9),
      parent: { slot: 'lead', key: 'lead' }, pending_approval: true,
      needs_you: { count: 1, oldest_ts: (NOW - 9 * 60_000) / 1000, first_words: 'Bash · read the lockfile', row_id: 'm-r1', kinds: ['approval'] } },
    { key: 'trip', title: 'Trip planner', running: false, messages: 4, agent: 'kirocrew', last_message: 'Saved the plan', last_ts: at(8), needs_you: null },
  ] as unknown as ChatSlot[]

  const chipText = (c: HTMLElement) => c.querySelector('[data-testid="needs-you-off-chip"]')?.textContent

  it("lets the conductor's crew line speak for a folded worker's wait, so the chip and the tab title agree", async () => {
    const { container } = renderSidebar(crew)
    await waitFor(() => expect(rowOf(container, 'lead')).toHaveLength(1))
    // Folded: the worker is no row of its own, and no group draws it a second time.
    expect(rowOf(container, 'worker')).toHaveLength(0)
    expect(container.querySelector('[data-testid="needs-you-group"] [data-slot-key]')).toBeNull()
    expect(chipText(container)).toBe('Needs you1')
    expect(selectChatsWaitingOnYou(makeStore(crew).getState())).toBe(1)
  })

  it("lists a folded worker's wait once while the filter narrows", async () => {
    localStorage.setItem('mc-session-needs-you-only', '1')
    const { container } = renderSidebar(crew)
    await waitFor(() => expect(container.querySelectorAll('[data-testid="needs-you-group"] [data-slot-key]')).toHaveLength(1))
    expect([...container.querySelectorAll('[data-testid="needs-you-group"] [data-slot-key]')].map(n => n.getAttribute('data-slot-key'))).toEqual(['worker'])
    expect(rowOf(container, 'worker')).toHaveLength(1)
  })

  it('draws the waiting worker of a sleeping conductor once, in its place', async () => {
    // Quiet for eight days, the conductor sits in the shut dormant section, where the
    // fold hides no worker: the waiting worker is drawn loose in the list, once.
    const sleeping = crew.map(s => (s.key === 'lead' ? { ...s, last_ts: at(8 * 24 * 60) } : s)) as ChatSlot[]
    const { container } = renderSidebar(sleeping)
    await waitFor(() => expect(rowOf(container, 'worker')).toHaveLength(1))
    expect(rowOf(container, 'lead')).toHaveLength(0)
    expect(rowOf(container, 'worker')[0].classList.contains('session-needs-you')).toBe(true)
    expect(container.querySelector('[data-testid="needs-you-group"] [data-slot-key]')).toBeNull()
    expect(chipText(container)).toBe('Needs you1')
    expect(selectChatsWaitingOnYou(makeStore(sleeping).getState())).toBe(1)
  })
})

describe('a Crew Mode root with two waits', () => {
  // The root asks a question of its own while one of its crew members waits on a
  // tool approval. The member is no row in the person's list; the root speaks for it.
  const crew = [
    { key: 'root', title: 'Release crew', running: false, messages: 4, agent: 'kirocrew-conductor', last_ts: at(6), crew_mode_root: true, needs_input: true },
    { key: 'member', title: 'Release member', running: false, messages: 4, agent: 'kirocrew', last_ts: at(4), crew_root: 'root', created_by: 'root', pending_approval: true },
    { key: 'trip', title: 'Trip planner', running: false, messages: 4, agent: 'kirocrew', last_message: 'Saved the plan', last_ts: at(8) },
  ] as unknown as ChatSlot[]

  it('counts both asks on the row, and one waiting chat on the chip and the tab title', async () => {
    const { container } = renderSidebar(crew)
    await waitFor(() => expect(rowOf(container, 'root')).toHaveLength(1))
    const root = rowOf(container, 'root')[0]
    expect(within(root).getByTestId('crew-root-attention').textContent).toBe('2 need you')
    await waitFor(() => expect(within(root).getByTestId('session-needs-you-count').getAttribute('aria-label')).toBe('2 open asks'))
    expect(rowOf(container, 'member')).toHaveLength(0)
    expect(container.querySelector('[data-testid="needs-you-group"] [data-slot-key]')).toBeNull()
    expect(container.querySelector('[data-testid="needs-you-off-chip"]')?.getAttribute('aria-label')).toBe('Needs you: 1 chat waits on you')
    expect(selectChatsWaitingOnYou(makeStore(crew).getState())).toBe(1)
  })
})
