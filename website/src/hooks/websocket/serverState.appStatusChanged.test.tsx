/**
 * The owner-only `app_status_changed` frame re-asks only the named app's status
 * probes: all of its top-bar items, and its session-control chips for one chat or
 * for every chat. No other app and no other chat moves.
 */
import { describe, expect, it } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { handleAppStatusChanged } from './serverState'
import { SESSION_CONTROL_STATUS_KEY } from '../useSessionControls'
import { APP_STATUS_ITEM_KEY } from '../useAppStatusItems'

describe('the app_status_changed frame', () => {
  const probe = (sessionKey: string, app: string) =>
    [SESSION_CONTROL_STATUS_KEY, sessionKey, `${app}:questions`, app, 'questions-status', '', '']
  // A top-bar status item: not per chat, keyed `${app}:${id}`.
  const pill = (app: string, id = 'waiting') => [APP_STATUS_ITEM_KEY, `${app}:${id}`, 'waiting-status', true]

  function seeded() {
    const qc = new QueryClient()
    const keys = [
      probe('dashboard:chat-7', 'ref-links'),
      probe('dashboard:chat-8', 'ref-links'),
      probe('dashboard:chat-7', 'other-app'),
      pill('ref-links'),
      // An app whose name starts with the other's: the `:` boundary keeps it out.
      pill('ref-links-two'),
      ['apps'],
    ]
    for (const k of keys) qc.setQueryData(k, { seeded: true })
    const label = (k: unknown[]) => k[0] === APP_STATUS_ITEM_KEY ? `pill|${String(k[1])}` : `${String(k[1] ?? k[0])}|${String(k[3] ?? '')}`
    const stale = () => keys.filter(k => qc.getQueryState(k)?.isInvalidated).map(label)
    return { qc, stale }
  }

  it('re-asks only that app: its chips in that chat, and its top-bar pills', () => {
    const { qc, stale } = seeded()
    handleAppStatusChanged(qc, { app: 'ref-links', session_key: 'dashboard:chat-7' })
    expect(stale()).toEqual(['dashboard:chat-7|ref-links', 'pill|ref-links:waiting'])
  })

  it('re-asks that app in every chat when no session is named', () => {
    const { qc, stale } = seeded()
    handleAppStatusChanged(qc, { app: 'ref-links' })
    expect(stale()).toEqual(['dashboard:chat-7|ref-links', 'dashboard:chat-8|ref-links', 'pill|ref-links:waiting'])
  })

  it('refetches a mounted pill at once, with no poll', async () => {
    const qc = new QueryClient()
    let reads = 0
    // Mounted as the top bar mounts it; never stale, so only the frame moves it.
    const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client: qc }, children)
    renderHook(() => useQuery({ queryKey: pill('ref-links'), queryFn: async () => ++reads, staleTime: Infinity }), { wrapper })
    await waitFor(() => expect(reads).toBe(1))
    act(() => { handleAppStatusChanged(qc, { app: 'ref-links' }) })
    await waitFor(() => expect(reads).toBe(2))
  })

  it.each([[{}], [{ app: '' }], [{ app: 7 }], [null], ['ref-links']])('ignores a frame with no app: %j', (data) => {
    const { qc, stale } = seeded()
    handleAppStatusChanged(qc, data)
    expect(stale()).toEqual([])
  })
})
