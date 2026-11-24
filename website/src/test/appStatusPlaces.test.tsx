/**
 * A status item's card that lists places (`heading`, `rows[]`, `go`): what it
 * draws, where a tap goes, the `?status=` card request, and the go the global
 * key fires.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'

import { createTestStore, renderWithProviders } from './helpers'
import AppStatusItems from '../shell/topbar/AppStatusItems'
import { firePrimaryStatusGo, getActiveStatusGo, setActiveStatusGo } from '../lib/statusGo'
import { requestStatusCard, takeStatusCardRequest } from '../lib/statusCardRequest'
import { api } from '../api/client'
import { addSlotOptimistic } from '../store/dashboardSlice'
import { setActiveSlot } from '../store/chatSlice'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({ api: { listApps: vi.fn(), appSessionStatus: vi.fn(), resumeChatSlot: vi.fn(), sessionMeta: vi.fn() } }))

const mockListApps = api.listApps as unknown as ReturnType<typeof vi.fn>
const mockStatus = api.appSessionStatus as unknown as ReturnType<typeof vi.fn>
const mockResume = api.resumeChatSlot as unknown as ReturnType<typeof vi.fn>
const mockMeta = api.sessionMeta as unknown as ReturnType<typeof vi.fn>

/** A store where `key` is an open chat tab, as the sidebar lists it. */
function storeWithTab(key: string) {
  const store = createTestStore()
  store.dispatch(addSlotOptimistic({ key, title: key, agent: 'kirocrew' } as unknown as ChatSlot))
  return store
}

const waitingApp = {
  name: 'fixture-links',
  enabled: true,
  manifest: {
    displayName: 'Fixture Links',
    backend: { entryPoint: 'backend/server.py' },
    contributes: { statusItems: [{ id: 'waiting', label: 'Waiting', icon: 'Shield', statusPath: 'status/waiting' }] },
  },
}

const arg = { view: 'waiting', label: 'Q4a' }
const oldest = { sid: 'chat-4', panelTab: 'questions', arg }

const answer = {
  text: 'Blick 1h 12m +3',
  tone: 'danger',
  tooltip: 'Tap a row to answer it there.',
  heading: 'Waiting on you · 4',
  rows: [
    { lead: '1h 12m', text: 'Blick Q3a', detail: 'Ship Blick 0.9 tonight?', tone: 'danger', target: oldest },
    { lead: '38m', text: 'Agent Definitions Q6b', detail: 'Install the fix now?', tone: 'warn', target: { sid: 'chat-7' } },
    { lead: '4m', text: 'No target', tone: 'muted', target: { sid: '../evil' } },
  ],
  go: { label: 'Go to oldest', target: oldest },
}

beforeEach(() => {
  vi.clearAllMocks()
  mockListApps.mockResolvedValue([waitingApp])
  mockStatus.mockResolvedValue(answer)
})

afterEach(() => {
  act(() => setActiveStatusGo(null))
  takeStatusCardRequest(['fixture-links:waiting'], Number.MAX_SAFE_INTEGER)
})

async function openCard() {
  const button = await screen.findByTestId('app-status-items')
  await waitFor(() => expect(button.textContent).toContain('Blick'))
  fireEvent.click(button)
  return screen.findByTestId('app-status-card')
}

describe('a status card that lists places', () => {
  it('draws the heading, a row per place, the note and the go', async () => {
    renderWithProviders(<AppStatusItems />, { route: '/chat' })
    const card = await openCard()
    expect(within(card).getByText('Waiting on you · 4')).toBeTruthy()
    const rows = within(card).getAllByTestId('app-status-place')
    expect(rows.map(r => r.textContent)).toEqual([
      '1h 12mBlick Q3aShip Blick 0.9 tonight?',
      '38mAgent Definitions Q6bInstall the fix now?',
      '4mNo target',
    ])
    // A place whose target failed the check is drawn, never clickable.
    expect(rows.map(r => r.tagName)).toEqual(['BUTTON', 'BUTTON', 'DIV'])
    expect(within(card).getByText('Tap a row to answer it there.')).toBeTruthy()
    expect(within(card).getByTestId('app-status-go').textContent).toContain('Go to oldest')
  })

  it('closes the card and goes to the chat and tab a row names', async () => {
    const store = storeWithTab('chat-4')
    renderWithProviders(<AppStatusItems />, { route: '/chat', store })
    const card = await openCard()
    fireEvent.click(within(card).getAllByTestId('app-status-place')[0])
    await waitFor(() => expect(screen.queryByTestId('app-status-card')).toBeNull())
    expect(getActiveStatusGo()).toMatchObject({ to: 'chat-4', appName: 'fixture-links', pending: { target: { kind: 'panelTab', id: 'questions' }, arg } })
    expect(store.getState().chat.activeSlot).toBe('chat-4')
  })

  it('shows the key on the computer and never on the phone', async () => {
    const { unmount } = renderWithProviders(<AppStatusItems />, { route: '/chat' })
    let card = await openCard()
    expect(within(card).getByTestId('app-status-go').querySelectorAll('kbd').length).toBeGreaterThan(0)
    unmount()
    renderWithProviders(<AppStatusItems size="sm" />, { route: '/chat' })
    card = await openCard()
    expect(within(card).getByTestId('app-status-go').querySelectorAll('kbd')).toHaveLength(0)
  })

  it('opens its card from a ?status= request once the item has answered', async () => {
    act(() => requestStatusCard('fixture-links:waiting'))
    renderWithProviders(<AppStatusItems />, { route: '/chat' })
    expect(await screen.findByTestId('app-status-card')).toBeTruthy()
    // Taken once: it does not reopen the card after a close.
    expect(takeStatusCardRequest(['fixture-links:waiting'])).toBeNull()
  })

  it('reopens a closed chat from its transcript, then hands the tab its arg', async () => {
    // The probe answers the transcript's own key (`dashboard:<tab key>`) and title.
    mockMeta.mockResolvedValue({ key: 'dashboard:chat-4', title: 'Blick' })
    mockResume.mockResolvedValue({ ok: true, key: 'chat-4', mode: 'default', surface: 'default', messages: [] })
    const store = createTestStore()
    renderWithProviders(<AppStatusItems />, { route: '/chat', store })
    const card = await openCard()
    fireEvent.click(within(card).getAllByTestId('app-status-place')[0])
    await waitFor(() => expect(mockResume).toHaveBeenCalledWith('dashboard:chat-4', 'Blick'))
    expect(mockMeta).toHaveBeenCalledWith('chat-4')
    await waitFor(() => expect(getActiveStatusGo()).toMatchObject({ to: 'chat-4', opening: false, pending: { target: { kind: 'panelTab', id: 'questions' }, arg } }))
  })

  it('never makes a new chat for a target with no transcript, and says it is gone', async () => {
    mockMeta.mockResolvedValue(null)
    const store = createTestStore()
    renderWithProviders(<AppStatusItems />, { route: '/chat', store })
    const card = await openCard()
    fireEvent.click(within(card).getAllByTestId('app-status-place')[0])
    await waitFor(() => expect(getActiveStatusGo()).toBeNull())
    expect(mockResume).not.toHaveBeenCalled()
    expect(store.getState().chat.switchSlotGone).toMatchObject({ kind: 'gone' })
  })

  it('ends the go, and says so, when a closed chat cannot be reopened', async () => {
    mockMeta.mockResolvedValue({ key: 'dashboard:chat-4', title: 'Blick' })
    mockResume.mockRejectedValue(new Error('gone'))
    const store = createTestStore()
    renderWithProviders(<AppStatusItems />, { route: '/chat', store })
    const card = await openCard()
    fireEvent.click(within(card).getAllByTestId('app-status-place')[0])
    await waitFor(() => expect(mockResume).toHaveBeenCalled())
    await waitFor(() => expect(getActiveStatusGo()).toBeNull())
    expect(store.getState().chat.switchSlotGone).toMatchObject({ kind: 'failed' })
  })

  it('keeps the way back when a second go stays in the chat the first landed in', async () => {
    const store = storeWithTab('chat-4')
    store.dispatch(setActiveSlot('chat-4'))
    act(() => setActiveStatusGo({ to: 'chat-4', from: 'chat-1', appName: 'fixture-links', pending: null }))
    renderWithProviders(<AppStatusItems />, { route: '/chat', store })
    const card = await openCard()
    fireEvent.click(within(card).getAllByTestId('app-status-place')[0])
    await waitFor(() => expect(getActiveStatusGo()).toMatchObject({ to: 'chat-4', from: 'chat-1', pending: { target: { kind: 'panelTab', id: 'questions' }, arg } }))
  })

  it('registers its go for the global key, and drops it on unmount', async () => {
    const store = storeWithTab('chat-4')
    const { unmount } = renderWithProviders(<AppStatusItems />, { route: '/chat', store })
    await screen.findByTestId('app-status-items')
    await waitFor(() => expect(firePrimaryStatusGo()).toBe(true))
    expect(getActiveStatusGo()).toMatchObject({ to: 'chat-4', pending: { target: { kind: 'panelTab', id: 'questions' }, arg } })
    unmount()
    expect(firePrimaryStatusGo()).toBe(false)
  })
})
