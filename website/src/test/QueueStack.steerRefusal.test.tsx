/**
 * A steer that could not go into the running turn waits on the queue like any
 * other message. Its row says why (meta.steerRefusal, a code the gateway sets),
 * so the person can tell "this waits for a turn that may run for hours" from a
 * message that was simply sent to the queue.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import React from 'react'
import type { ChatMessage } from '../types'
import en from '../i18n/locales/en.json'

vi.mock('framer-motion', () => ({
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  motion: {
    div: React.forwardRef(
      ({ children, animate: _a, transition: _t, initial: _i, exit: _e, ...props }: Record<string, unknown> & { children?: React.ReactNode }, ref: React.Ref<HTMLDivElement>) => (
        <div ref={ref} {...(props as React.HTMLAttributes<HTMLDivElement>)}>{children}</div>
      ),
    ),
  },
  useMotionValue: () => ({ set: vi.fn(), get: () => 0, jump: vi.fn() }),
  useSpring: () => ({ set: vi.fn(), get: () => 0, jump: vi.fn() }),
}))

import QueueStack, { queuedSteerRefusal } from '../components/QueueStack'

const LABELS = en.components.queueStack.steer_refusal

function queued(id: string, meta: Record<string, unknown> = {}): ChatMessage {
  return { role: 'queued', content: 'rebase first', meta: { queueId: id, ...meta } } as ChatMessage
}

describe('a queued steer names why it could not go in', () => {
  it('shows the reason on the row, with the wait spelled out on hover', () => {
    render(<QueueStack messages={[queued('q1', { steerRefusal: 'agent_turn' })]} onCancel={vi.fn()} onEdit={vi.fn()} />)
    const chip = screen.getByTestId('queue-card-steer-refusal')
    expect(chip).toHaveTextContent(LABELS.agent_turn)
    expect(chip.getAttribute('title')).toContain(LABELS.agent_turn)
    expect(chip.getAttribute('title')).toMatch(/until that turn ends/)
  })

  it('a message sent to the queue on purpose has no reason chip', () => {
    render(<QueueStack messages={[queued('q1')]} onCancel={vi.fn()} onEdit={vi.fn()} />)
    expect(screen.queryByTestId('queue-card-steer-refusal')).toBeNull()
  })

  it('an unknown code reads as the plain fallback, never as raw text', () => {
    expect(queuedSteerRefusal(queued('q1', { steerRefusal: '<b>x</b>' }))).toBe(LABELS.refused)
    expect(queuedSteerRefusal(queued('q1', { steerRefusal: 'toString' }))).toBe(LABELS.refused)
  })

  it('every code the gateway can send has its own row label', () => {
    for (const [code, label] of Object.entries(LABELS)) {
      expect(queuedSteerRefusal(queued('q1', { steerRefusal: code }))).toBe(label)
    }
  })
})
