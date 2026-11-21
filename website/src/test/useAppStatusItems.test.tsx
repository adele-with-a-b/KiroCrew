/**
 * App status items (`contributes.statusItems`): discovery, normalization of the
 * app's untrusted answer, polling, and the pill + card the top bar draws.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'

import { renderHookWithProviders, renderWithProviders } from './helpers'
import {
  MAX_STATUS_ITEMS,
  MAX_STATUS_TEXT,
  STATUS_ITEM_POLL_MS,
  normalizeStatusItemValue,
  resolveStatusItems,
  useAppStatusItems,
} from '../hooks/useAppStatusItems'
import AppStatusItems from '../shell/topbar/AppStatusItems'
import { api } from '../api/client'

vi.mock('../api/client', () => ({ api: { listApps: vi.fn(), appSessionStatus: vi.fn() } }))

const mockListApps = api.listApps as unknown as ReturnType<typeof vi.fn>
const mockStatus = api.appSessionStatus as unknown as ReturnType<typeof vi.fn>

type Item = { id?: string; label?: string; icon?: string; statusPath?: string }

const app = (name: string, statusItems: Item[], extra: Record<string, unknown> = {}) => ({
  name,
  enabled: true,
  ...extra,
  manifest: {
    displayName: name.toUpperCase(),
    backend: { entryPoint: 'backend/server.py' },
    contributes: { statusItems },
  },
})

const twoItems = () => app('cred-pill', [
  { id: 'creds', label: 'Model calls', icon: 'Cloud', statusPath: 'status/creds' },
  { id: 'midway', label: 'YubiKey tap', icon: 'Key', statusPath: 'status/midway' },
])

describe('resolveStatusItems', () => {
  it('flattens, keys and marks process-backed apps', () => {
    const out = resolveStatusItems([twoItems()])
    expect(out.map(i => i.key)).toEqual(['cred-pill:creds', 'cred-pill:midway'])
    expect(out[0]).toMatchObject({ label: 'Model calls', icon: 'Cloud', statusPath: 'status/creds', processBacked: true })
  })

  it('skips disabled apps and items missing an id, label or route', () => {
    const out = resolveStatusItems([
      app('off', [{ id: 'a', label: 'A', statusPath: 'a' }], { enabled: false }),
      app('on', [
        { label: 'No id', statusPath: 'x' },
        { id: 'no-label', label: '  ', statusPath: 'x' },
        { id: 'no-route', label: 'No route' },
        { id: 'ok', label: 'Ok', statusPath: '/ok' },
      ]),
    ])
    expect(out.map(i => i.key)).toEqual(['on:ok'])
    // The leading slash is the one normalization the backend also applies.
    expect(out[0].statusPath).toBe('ok')
  })

  it('refuses a route outside the allowlist, a protocol-relative one included', () => {
    const out = resolveStatusItems([
      app('a', [
        { id: 'x', label: 'X', statusPath: '//evil/x' },
        { id: 'y', label: 'Y', statusPath: '../other-app/y' },
        { id: 'z', label: 'Z', statusPath: 'z?q=1' },
      ]),
    ])
    expect(out).toEqual([])
  })

  it('dedupes a repeated id and caps the bar as a whole, in byte order', () => {
    const many = ['d', 'c', 'b', 'a'].map(n => app(n, [
      { id: 'one', label: '1', statusPath: 'one' },
      { id: 'one', label: 'dup', statusPath: 'dup' },
      { id: 'two', label: '2', statusPath: 'two' },
    ]))
    const out = resolveStatusItems(many)
    expect(out).toHaveLength(MAX_STATUS_ITEMS)
    expect(out.map(i => i.key)).toEqual(['a:one', 'a:two', 'b:one', 'b:two'])
    expect(out[0].label).toBe('1')
  })
})

describe('normalizeStatusItemValue', () => {
  it('keeps a well-formed answer', () => {
    expect(normalizeStatusItemValue({ text: '54m', tone: 'ok', tooltip: 'Fine.' }))
      .toEqual({ text: '54m', tone: 'ok', tooltip: 'Fine.' })
  })

  it('draws an unknown tone muted', () => {
    expect(normalizeStatusItemValue({ text: 'x', tone: 'rainbow' })?.tone).toBe('muted')
  })

  it('hides the pill for an empty or non-string text', () => {
    expect(normalizeStatusItemValue({ text: '   ' })).toBeNull()
    expect(normalizeStatusItemValue({ text: 5 })).toBeNull()
    expect(normalizeStatusItemValue(null)).toBeNull()
  })

  it('strips bidi and control characters and caps by code point', () => {
    const v = normalizeStatusItemValue({ text: `\u202eab\u0007${'🔑'.repeat(40)}` })
    expect(v?.text.startsWith('ab🔑')).toBe(true)
    expect(Array.from(v!.text)).toHaveLength(MAX_STATUS_TEXT)
  })
})

describe('useAppStatusItems', () => {
  beforeEach(() => vi.clearAllMocks())

  it('polls each item through the app route with no params, grouped per app', async () => {
    mockListApps.mockResolvedValue([twoItems()])
    mockStatus.mockImplementation((_app: string, path: string) =>
      Promise.resolve(path === 'status/creds' ? { text: '54m', tone: 'muted' } : { text: '19h', tone: 'ok' }))
    const { result } = renderHookWithProviders(() => useAppStatusItems())
    await waitFor(() => expect(result.current[0]?.readings).toHaveLength(2))
    expect(mockStatus).toHaveBeenCalledWith('cred-pill', 'status/creds', {}, true)
    expect(mockStatus).toHaveBeenCalledWith('cred-pill', 'status/midway', {}, true)
    expect(result.current).toHaveLength(1)
    expect(result.current[0].readings.map(r => r.value?.text)).toEqual(['54m', '19h'])
    expect(STATUS_ITEM_POLL_MS).toBe(60_000)
  })

  it('draws nothing while no app declares an item, and nothing for a failed app list', async () => {
    mockListApps.mockRejectedValue(new Error('HTTP 503'))
    const { result } = renderHookWithProviders(() => useAppStatusItems())
    await waitFor(() => expect(mockListApps).toHaveBeenCalled())
    expect(result.current).toEqual([])
  })

  it('keeps a failing item as a reading carrying its error', async () => {
    mockListApps.mockResolvedValue([twoItems()])
    mockStatus.mockImplementation((_app: string, path: string) =>
      path === 'status/creds' ? Promise.reject(new Error('HTTP 502')) : Promise.resolve({ text: '19h', tone: 'ok' }))
    const { result } = renderHookWithProviders(() => useAppStatusItems())
    await waitFor(() => expect(result.current[0]?.readings).toHaveLength(2))
    const creds = result.current[0].readings.find(r => r.item.id === 'creds')!
    expect(creds.value).toBeNull()
    expect(creds.error?.message).toBe('HTTP 502')
  })
})

describe('AppStatusItems', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders nothing when no app declares an item', async () => {
    mockListApps.mockResolvedValue([app('plain', [])])
    renderWithProviders(<AppStatusItems />)
    await waitFor(() => expect(mockListApps).toHaveBeenCalled())
    expect(screen.queryByTestId('app-status-items-bar')).toBeNull()
  })

  it('draws one button per app with a pill per item, and opens the card on a click', async () => {
    mockListApps.mockResolvedValue([twoItems()])
    mockStatus.mockImplementation((_app: string, path: string) =>
      Promise.resolve(path === 'status/creds'
        ? { text: 'tap', tone: 'danger', tooltip: 'Session cookie expired.' }
        : { text: '19h', tone: 'ok', tooltip: 'Tap due in 19 h.' }))
    renderWithProviders(<AppStatusItems />)
    const button = await screen.findByTestId('app-status-items')
    await waitFor(() => expect(button.querySelectorAll('[data-testid="app-status-pill"]')).toHaveLength(2))
    const pills = Array.from(button.querySelectorAll<HTMLElement>('[data-testid="app-status-pill"]'))
    expect(pills.map(p => p.dataset.tone)).toEqual(['danger', 'ok'])
    expect(pills.map(p => p.textContent)).toEqual(['tap', '19h'])
    // The labels are the accessible name; they are never drawn on the pill.
    expect(button.getAttribute('aria-label')).toContain('Model calls: tap')
    expect(button.getAttribute('aria-label')).toContain('YubiKey tap: 19h')

    fireEvent.click(button)
    const card = await screen.findByTestId('app-status-card')
    expect(card.textContent).toContain('Model calls: tap')
    expect(card.textContent).toContain('Session cookie expired.')
    expect(card.textContent).toContain('Tap due in 19 h.')
    expect(screen.getByTestId('app-status-updated').textContent).toMatch(/^Updated /)
  })

  it('marks a failing item unknown and explains the failure in the card', async () => {
    mockListApps.mockResolvedValue([twoItems()])
    mockStatus.mockImplementation((_app: string, path: string) =>
      path === 'status/creds' ? Promise.reject(new Error('HTTP 502')) : Promise.resolve({ text: '19h', tone: 'ok' }))
    renderWithProviders(<AppStatusItems />)
    const button = await screen.findByTestId('app-status-items')
    await waitFor(() => expect(button.querySelectorAll('[data-testid="app-status-pill"]')).toHaveLength(2))
    const first = button.querySelector<HTMLElement>('[data-testid="app-status-pill"]')!
    expect(first.dataset.tone).toBe('muted')
    expect(first.textContent).toBe('?')
    fireEvent.click(button)
    expect(await screen.findByTestId('app-status-item-error')).toBeTruthy()
  })
})
