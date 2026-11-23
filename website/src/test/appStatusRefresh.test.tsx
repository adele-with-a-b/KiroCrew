/**
 * An app re-reads its own status: the SDK hook posts
 * `/api/apps/<its own app>/status-changed` (the host binds the name; the app cannot
 * pass one). What the owner-only frame then re-asks is pinned beside its handler,
 * in `hooks/websocket/serverState.appStatusChanged.test.tsx`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import '../i18n/all'
import { AppApiProvider, useAppStatusRefresh } from '../app-sdk/index'

describe('useAppStatusRefresh', () => {
  let requests: { path: string; method: string; body: unknown }[]

  beforeEach(() => {
    requests = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'http://localhost')
      requests.push({
        path: url.pathname,
        method: (init?.method ?? 'GET').toUpperCase(),
        body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined,
      })
      return new Response('{"ok":true}', { status: 200, headers: { 'Content-Type': 'application/json' } })
    }))
  })
  afterEach(() => { vi.unstubAllGlobals() })

  function mount(appName: string, allowedApiPaths: string[] = []) {
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(AppApiProvider, { appName, allowedApiPaths, allowedEvents: [] }, children)
    return renderHook(() => useAppStatusRefresh(), { wrapper })
  }

  it('posts for its own app, with no manifest declaration, one chat or every chat', async () => {
    const { result } = mount('ref-links')
    await act(async () => { await result.current({ sessionKey: 'dashboard:chat-7' }) })
    await act(async () => { await result.current() })
    expect(requests).toEqual([
      { path: '/api/apps/ref-links/status-changed', method: 'POST', body: { session_key: 'dashboard:chat-7' } },
      { path: '/api/apps/ref-links/status-changed', method: 'POST', body: {} },
    ])
  })

  it('sends every call, even two in a row', async () => {
    const { result } = mount('ref-links')
    await act(async () => {
      await result.current({ sessionKey: 'dashboard:chat-7' })
      await result.current({ sessionKey: 'dashboard:chat-7' })
    })
    expect(requests).toHaveLength(2)
  })
})
