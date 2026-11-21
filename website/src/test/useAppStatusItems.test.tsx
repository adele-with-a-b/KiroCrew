/**
 * App status items (`contributes.statusItems`): discovery, normalization of the
 * app's untrusted answer, polling, and the pill + card the top bar draws.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'

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

const twoItems = () => app('status-app', [
  { id: 'disk', label: 'Disk free', icon: 'Database', statusPath: 'status/disk' },
  { id: 'sync', label: 'Last sync', icon: 'Shield', statusPath: 'status/sync' },
])

describe('resolveStatusItems', () => {
  it('flattens, keys and marks process-backed apps', () => {
    const out = resolveStatusItems([twoItems()])
    expect(out.map(i => i.key)).toEqual(['status-app:disk', 'status-app:sync'])
    expect(out[0]).toMatchObject({ label: 'Disk free', icon: 'Database', statusPath: 'status/disk', processBacked: true })
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

  it('dedupes a repeated id, mirrors the per-app cap, and sorts in byte order', () => {
    const many = ['d', 'c', 'b', 'a'].map(n => app(n, [
      { id: 'one', label: '1', statusPath: 'one' },
      { id: 'one', label: 'dup', statusPath: 'dup' },
      { id: 'two', label: '2', statusPath: 'two' },
      { id: 'three', label: '3', statusPath: 'three' },
    ]))
    const out = resolveStatusItems(many)
    expect(out.map(i => i.key)).toEqual(['a:one', 'a:two', 'b:one', 'b:two', 'c:one', 'c:two', 'd:one', 'd:two'])
    expect(out[0].label).toBe('1')
  })

  it('cleans the label and skips one past the install-time length limit', () => {
    const out = resolveStatusItems([app('a', [
      { id: 'x', label: 'Disk\u202e free', statusPath: 'x' },
      { id: 'y', label: 'y'.repeat(41), statusPath: 'y' },
    ])])
    expect(out.map(i => i.label)).toEqual(['Disk free'])
  })
})

describe('normalizeStatusItemValue', () => {
  it('keeps a well-formed answer', () => {
    expect(normalizeStatusItemValue({ text: '54 GB', tone: 'ok', tooltip: 'Fine.' }))
      .toEqual({ text: '54 GB', tone: 'ok', tooltip: 'Fine.' })
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
      Promise.resolve(path === 'status/disk' ? { text: '54 GB', tone: 'muted' } : { text: '2h', tone: 'ok' }))
    const { result } = renderHookWithProviders(() => useAppStatusItems())
    await waitFor(() => expect(result.current[0]?.readings).toHaveLength(2))
    expect(mockStatus).toHaveBeenCalledWith('status-app', 'status/disk', {}, true)
    expect(mockStatus).toHaveBeenCalledWith('status-app', 'status/sync', {}, true)
    expect(result.current).toHaveLength(1)
    expect(result.current[0].readings.map(r => r.value?.text)).toEqual(['54 GB', '2h'])
    expect(STATUS_ITEM_POLL_MS).toBe(60_000)
  })

  it('draws nothing while no app declares an item, and nothing for a failed app list', async () => {
    mockListApps.mockRejectedValue(new Error('HTTP 503'))
    const { result } = renderHookWithProviders(() => useAppStatusItems())
    await waitFor(() => expect(mockListApps).toHaveBeenCalled())
    expect(result.current).toEqual([])
  })

  it('caps the drawn readings after hiding empty ones, so a hidden item holds no slot', async () => {
    mockListApps.mockResolvedValue(['a', 'b', 'c'].map(n => app(n, [
      { id: 'one', label: '1', statusPath: 'one' },
      { id: 'two', label: '2', statusPath: 'two' },
    ])))
    mockStatus.mockImplementation((name: string) =>
      Promise.resolve(name === 'a' ? { text: '' } : { text: 'v', tone: 'ok' }))
    const { result } = renderHookWithProviders(() => useAppStatusItems())
    await waitFor(() => expect(result.current.flatMap(g => g.readings)).toHaveLength(MAX_STATUS_ITEMS))
    expect(result.current.map(g => g.appName)).toEqual(['b', 'c'])
  })

  it('keeps a failing item as a reading carrying its error', async () => {
    mockListApps.mockResolvedValue([twoItems()])
    mockStatus.mockImplementation((_app: string, path: string) =>
      path === 'status/disk' ? Promise.reject(new Error('HTTP 502')) : Promise.resolve({ text: '2h', tone: 'ok' }))
    const { result } = renderHookWithProviders(() => useAppStatusItems())
    await waitFor(() => expect(result.current[0]?.readings).toHaveLength(2))
    const disk = result.current[0].readings.find(r => r.item.id === 'disk')!
    expect(disk.value).toBeNull()
    expect(disk.error?.message).toBe('HTTP 502')
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
      Promise.resolve(path === 'status/disk'
        ? { text: 'low', tone: 'danger', tooltip: 'Under 1 GB free.' }
        : { text: '2h', tone: 'ok', tooltip: 'Synced 2 h ago.' }))
    renderWithProviders(<AppStatusItems />)
    const button = await screen.findByTestId('app-status-items')
    await waitFor(() => expect(button.querySelectorAll('[data-testid="app-status-pill"]')).toHaveLength(2))
    const pills = Array.from(button.querySelectorAll<HTMLElement>('[data-testid="app-status-pill"]'))
    expect(pills.map(p => p.dataset.tone)).toEqual(['danger', 'ok'])
    expect(pills.map(p => p.textContent)).toEqual(['low', '2h'])
    // The labels are the accessible name; they are never drawn on the pill.
    expect(button.getAttribute('aria-label')).toContain('Disk free: low')
    expect(button.getAttribute('aria-label')).toContain('Last sync: 2h')

    fireEvent.click(button)
    const card = await screen.findByTestId('app-status-card')
    expect(card.textContent).toContain('Disk free: low')
    expect(card.textContent).toContain('Under 1 GB free.')
    expect(card.textContent).toContain('Synced 2 h ago.')
    expect(screen.getByTestId('app-status-updated').textContent).toMatch(/^Updated /)
  })

  it('marks a failing item unknown and explains the failure in the card', async () => {
    mockListApps.mockResolvedValue([twoItems()])
    mockStatus.mockImplementation((_app: string, path: string) =>
      path === 'status/disk' ? Promise.reject(new Error('HTTP 502')) : Promise.resolve({ text: '2h', tone: 'ok' }))
    renderWithProviders(<AppStatusItems />)
    const button = await screen.findByTestId('app-status-items')
    await waitFor(() => expect(button.querySelectorAll('[data-testid="app-status-pill"]')).toHaveLength(2))
    const first = button.querySelector<HTMLElement>('[data-testid="app-status-pill"]')!
    expect(first.dataset.tone).toBe('muted')
    expect(first.textContent).toBe('?')
    fireEvent.click(button)
    expect(await screen.findByTestId('app-status-item-error')).toBeTruthy()
  })

  it('drops the last good value when a later poll fails, instead of freezing it', async () => {
    mockListApps.mockResolvedValue([twoItems()])
    mockStatus.mockResolvedValue({ text: '54 GB', tone: 'ok' })
    const { queryClient } = renderWithProviders(<AppStatusItems />)
    const button = await screen.findByTestId('app-status-items')
    await waitFor(() => expect(button.querySelectorAll('[data-testid="app-status-pill"]')).toHaveLength(2))
    mockStatus.mockRejectedValue(new Error('HTTP 502'))
    await queryClient.refetchQueries({ queryKey: ['app-status-item'] })
    await waitFor(() => {
      const pills = Array.from(button.querySelectorAll<HTMLElement>('[data-testid="app-status-pill"]'))
      expect(pills.map(p => [p.dataset.tone, p.textContent])).toEqual([['muted', '?'], ['muted', '?']])
    })
  })

  it('keeps a hover-opened card open when the pointer then clicks', async () => {
    mockListApps.mockResolvedValue([twoItems()])
    mockStatus.mockResolvedValue({ text: '54 GB', tone: 'ok' })
    renderWithProviders(<AppStatusItems />)
    const button = await screen.findByTestId('app-status-items')
    fireEvent.pointerEnter(button, { pointerType: 'mouse' })
    await screen.findByTestId('app-status-card')
    await act(async () => { fireEvent.click(button) })
    expect(screen.queryByTestId('app-status-card')).not.toBeNull()
  })

  it('opens on a touch tap without the hover path', async () => {
    mockListApps.mockResolvedValue([twoItems()])
    mockStatus.mockResolvedValue({ text: '54 GB', tone: 'ok' })
    renderWithProviders(<AppStatusItems />)
    const button = await screen.findByTestId('app-status-items')
    fireEvent.pointerEnter(button, { pointerType: 'touch' })
    expect(screen.queryByTestId('app-status-card')).toBeNull()
    fireEvent.click(button)
    expect(await screen.findByTestId('app-status-card')).toBeTruthy()
  })
})
