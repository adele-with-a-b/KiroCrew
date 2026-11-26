/**
 * Send all held on the collapsed queue stack. After a restart the gateway marks
 * the cards nobody chose `meta.held`; with two or more cards and one held, the
 * front card shows the held count and one button that, after a confirm naming
 * the count, hands the held cards' ids to `onSendAllHeld`. The cards the server
 * kept held come back to be named above the stack (the hook half is pinned in
 * useQueuedMessageActions.test.tsx, the server half in test_queue_send_held.py).
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import QueueStack from '../components/QueueStack'
import type { ChatMessage } from '../types'

function queued(queueId: string, content: string): ChatMessage {
  return { role: 'queued', content, cls: 'msg msg-queued', ts: '', meta: { queueId } } as ChatMessage
}

function held(queueId: string, content: string): ChatMessage {
  return { role: 'queued', content, cls: 'msg msg-queued', ts: '', meta: { queueId, restored: true, held: true } } as ChatMessage
}

const MIXED = [held('h1', 'first note'), queued('q1', 'typed since'), held('h2', 'second note'), held('h3', 'third note')]

/** The props the dashboard hosts pass, so the held line and the button show. */
const host = { onCancel: () => {}, onInterrupt: () => {}, onEdit: () => {} }

const stackBox = () => screen.getByRole('button', { expanded: false })

describe('QueueStack Send all held', () => {
  it('shows on a collapsed stack of two or more with a held card, with the held count', () => {
    render(<QueueStack messages={MIXED} {...host} onSendAllHeld={async () => []} />)
    expect(screen.getByTestId('queue-send-all-held')).toHaveAccessibleName('Send all held…')
    expect(screen.getByTestId('queue-send-all-held')).toHaveAttribute('title', 'Send every held message, in order')
    expect(screen.getByText('4 queued · 3 held')).toBeInTheDocument()
  })

  it('is not offered for one card, with nothing held, or when the host cannot send', () => {
    const { rerender } = render(<QueueStack messages={MIXED.slice(0, 1)} {...host} onSendAllHeld={async () => []} />)
    expect(screen.queryByTestId('queue-send-all-held')).not.toBeInTheDocument()
    rerender(<QueueStack messages={[queued('q1', 'a'), queued('q2', 'b')]} {...host} onSendAllHeld={async () => []} />)
    expect(screen.queryByTestId('queue-send-all-held')).not.toBeInTheDocument()
    expect(screen.getByText('2 queued')).toBeInTheDocument()
    rerender(<QueueStack messages={MIXED} {...host} />)
    expect(screen.queryByTestId('queue-send-all-held')).not.toBeInTheDocument()
  })

  it('is not on the expanded stack, where each card has its own Send now', () => {
    render(<QueueStack messages={MIXED} {...host} onSendAllHeld={async () => []} />)
    fireEvent.click(stackBox())
    expect(screen.queryByTestId('queue-send-all-held')).not.toBeInTheDocument()
  })

  it('asks first in a box that names the count, and Cancel sends nothing', async () => {
    const user = userEvent.setup()
    const onSendAllHeld = vi.fn(async () => [])
    render(<QueueStack messages={MIXED} {...host} onSendAllHeld={onSendAllHeld} />)
    await user.click(screen.getByTestId('queue-send-all-held'))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Send 3 held messages?')).toBeInTheDocument()
    expect(within(dialog).getByText('They go one after another, oldest first. Each one is its own turn. Nothing else in the queue moves.')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(onSendAllHeld).not.toHaveBeenCalled()
    expect(stackBox()).toHaveAttribute('aria-expanded', 'false')
  })

  it('names one held message in the singular', async () => {
    const user = userEvent.setup()
    render(<QueueStack messages={[held('h1', 'only note'), queued('q1', 'typed')]} {...host} onSendAllHeld={async () => []} />)
    await user.click(screen.getByTestId('queue-send-all-held'))
    expect(within(await screen.findByRole('dialog')).getByText('Send 1 held message?')).toBeInTheDocument()
  })

  it('on Send all hands over the held cards only, in queue order, leaving out one in flight', async () => {
    const user = userEvent.setup()
    const onSendAllHeld = vi.fn(async () => [])
    render(<QueueStack messages={MIXED} {...host} onSendAllHeld={onSendAllHeld} pendingIds={new Set(['h2'])} />)
    await user.click(screen.getByTestId('queue-send-all-held'))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Send 2 held messages?')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Send all' }))
    expect(onSendAllHeld).toHaveBeenCalledTimes(1)
    expect(onSendAllHeld).toHaveBeenCalledWith(['h1', 'h3'])
  })

  it('sends only the cards counted when the box opened', async () => {
    const user = userEvent.setup()
    const onSendAllHeld = vi.fn(async () => [])
    const { rerender } = render(<QueueStack messages={MIXED.slice(0, 2)} {...host} onSendAllHeld={onSendAllHeld} />)
    await user.click(screen.getByTestId('queue-send-all-held'))
    const dialog = await screen.findByRole('dialog')
    rerender(<QueueStack messages={MIXED} {...host} onSendAllHeld={onSendAllHeld} />)
    await user.click(within(dialog).getByRole('button', { name: 'Send all' }))
    expect(onSendAllHeld).toHaveBeenCalledWith(['h1'])
  })

  it('names the cards the server kept held, with its reason, until none of them is held', async () => {
    const user = userEvent.setup()
    const onSendAllHeld = vi.fn(async () => [{ queueId: 'h2', reason: 'It is open for editing.' }])
    const { rerender } = render(<QueueStack messages={MIXED} {...host} onSendAllHeld={onSendAllHeld} />)
    await user.click(screen.getByTestId('queue-send-all-held'))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Send all' }))
    const line = await screen.findByTestId('queue-send-all-held-error')
    expect(line).toHaveTextContent('Could not send 1 held message')
    expect(line).toHaveTextContent('It is open for editing.')
    // The server let the others go: only the refused card is still held, and
    // the button stays to try again.
    const after = [queued('h1', 'first note'), MIXED[1], MIXED[2], queued('h3', 'third note')]
    rerender(<QueueStack messages={after} {...host} onSendAllHeld={onSendAllHeld} />)
    expect(screen.getByTestId('queue-send-all-held-error')).toBeInTheDocument()
    expect(screen.getByText('4 queued · 1 held')).toBeInTheDocument()
    expect(screen.getByTestId('queue-send-all-held')).toBeInTheDocument()
    rerender(<QueueStack messages={[after[0], after[1], queued('h2', 'second note'), after[3]]} {...host} onSendAllHeld={onSendAllHeld} />)
    expect(screen.queryByTestId('queue-send-all-held-error')).not.toBeInTheDocument()
    expect(screen.queryByTestId('queue-send-all-held')).not.toBeInTheDocument()
  })

  it('says each reason the gateway named by code in the catalog\'s words, once', async () => {
    const user = userEvent.setup()
    const onSendAllHeld = vi.fn(async () => [
      { queueId: 'h1', code: 'queue_item_contained', reason: 'gateway wording' },
      { queueId: 'h2', code: 'queue_item_editing', reason: 'gateway wording' },
      { queueId: 'h3', code: 'queue_item_contained', reason: 'gateway wording' },
    ])
    render(<QueueStack messages={MIXED} {...host} onSendAllHeld={onSendAllHeld} />)
    await user.click(screen.getByTestId('queue-send-all-held'))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Send all' }))
    const line = await screen.findByTestId('queue-send-all-held-error')
    expect(line).toHaveTextContent('Could not send 3 held messages')
    const contained = "This chat's channel link or limits don't allow it. The words stay in the queue."
    expect(line.textContent?.split(contained).length).toBe(2)
    expect(line).toHaveTextContent('It is open for editing.')
    expect(line).not.toHaveTextContent('gateway wording')
  })

  it('names every card when the request itself failed', async () => {
    const user = userEvent.setup()
    const onSendAllHeld = vi.fn(async (ids: readonly string[]) => ids.map(queueId => ({ queueId, reason: new Error('network down') })))
    render(<QueueStack messages={MIXED} {...host} onSendAllHeld={onSendAllHeld} />)
    await user.click(screen.getByTestId('queue-send-all-held'))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Send all' }))
    const line = await screen.findByTestId('queue-send-all-held-error')
    expect(line).toHaveTextContent('Could not send 3 held messages')
    expect(line).toHaveTextContent('network down')
  })
})
