/**
 * "Not now" under a reply that asks the person something: the line shows only on
 * the reply the slot row's `reply_ask` names, the press writes THAT row id, the
 * set-aside state reads "Not now · set aside by you." with "Bring back" on the
 * same button, and a stale press (409 not_waiting) says nothing was set aside.
 * The sidebar row's calm line follows the same field.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'

const { setReplyAskAside } = vi.hoisted(() => ({ setReplyAskAside: vi.fn() }))

vi.mock('../api/client', () => ({
  api: { setReplyAskAside },
}))

import NotNowLine from '../pages/chat/NotNowLine'
import { SetAsideLine } from '../pages/chat-sidebar/NeedsYouRow'
import { ApiError } from '../api/apiError'
import { store } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import type { ReplyAsk } from '../types'

function slotWith(ask: ReplyAsk | null) {
  store.dispatch(sseSlots([{ key: 'chat-a', title: 'A', messages: 2, running: false, reply_ask: ask }]))
}

function renderLine(rowId: string) {
  const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <NotNowLine slotKey="chat-a" rowId={rowId} />
      </Provider>
    </QueryClientProvider>,
  )
}

const OPEN: ReplyAsk = { row_id: 'm-2', first_words: 'Q3a Which tide source wins?', set_aside: false }

describe('NotNowLine', () => {
  beforeEach(() => { setReplyAskAside.mockReset() })
  afterEach(() => { store.dispatch(sseSlots([])) })

  it('draws nothing on a reply the ask is not on, nor when the slot has no ask', () => {
    slotWith(OPEN)
    const older = renderLine('m-1')
    expect(older.queryByTestId('not-now-line')).toBeNull()
    older.unmount()
    slotWith(null)
    expect(renderLine('m-2').queryByTestId('not-now-line')).toBeNull()
  })

  it('Not now writes that row id; the same button then reads Bring back', async () => {
    slotWith(OPEN)
    setReplyAskAside.mockResolvedValue({ ok: true, set_aside: true, changed: true })
    renderLine('m-2')
    expect(screen.getByText('Hides this ask. It comes back only if it asks again.')).toBeTruthy()
    const btn = screen.getByTestId('not-now')
    fireEvent.click(btn)
    await waitFor(() => expect(setReplyAskAside).toHaveBeenCalledWith('chat-a', 'm-2', true))
    // The gateway's slots frame is what flips the line, in every window alike.
    slotWith({ ...OPEN, set_aside: true })
    const back = await screen.findByTestId('not-now-bring-back')
    expect(back).toBe(btn)
    expect(screen.getByText('· set aside by you.')).toBeTruthy()
    expect(back.textContent).toContain('Bring back')
    setReplyAskAside.mockResolvedValue({ ok: true, set_aside: false, changed: true })
    fireEvent.click(back)
    await waitFor(() => expect(setReplyAskAside).toHaveBeenLastCalledWith('chat-a', 'm-2', false))
  })

  it('a stale press says nothing was set aside', async () => {
    slotWith(OPEN)
    setReplyAskAside.mockRejectedValue(new ApiError(409, 'stale', JSON.stringify({ code: 'not_waiting' })))
    renderLine('m-2')
    fireEvent.click(screen.getByTestId('not-now'))
    expect(await screen.findByText('Nothing was set aside: this ask changed or was answered since it was shown.')).toBeTruthy()
  })

  it('any other failure says the ask was not set aside', async () => {
    slotWith(OPEN)
    setReplyAskAside.mockRejectedValue(new ApiError(503, 'down', JSON.stringify({ code: 'set_aside_save_failed' })))
    renderLine('m-2')
    fireEvent.click(screen.getByTestId('not-now'))
    expect(await screen.findByText("Couldn't set this ask aside. Try again.")).toBeTruthy()
  })
})

describe('SetAsideLine', () => {
  it('says Not now with the first words of the ask it set aside', () => {
    render(<SetAsideLine ask={{ ...OPEN, set_aside: true }} lineCls="" iconPx={12} />)
    expect(screen.getByTestId('session-set-aside').textContent).toBe('Not now · Q3a Which tide source wins?')
  })
})
