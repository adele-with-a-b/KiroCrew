/** A pending question quotes the reply it is about, and the quote jumps there. */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, screen, within } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import PendingQuestionCard from '../components/PendingQuestionCard'
import { questionSubject } from '../pages/chat/questionSubject'
import type { ChatMessage } from '../types'

vi.mock('../api/client', () => ({ api: { answerQuestion: vi.fn() }, ApiError: class extends Error {} }))
// The raised-by label reads the Shared Crew decisions; this file is about the quote.
vi.mock('../pages/chat/shared-crew/useRaisedBy', () => ({ useRaisedBy: () => '' }))

const row = (role: string, content: string, ts?: string, meta?: Record<string, unknown>): ChatMessage =>
  ({ role, content, cls: role, ...(ts ? { ts } : {}), ...(meta ? { meta } : {}) })
const tool = (name: string, ts: string) =>
  row('tool', `🔧 ${name}`, ts, { tool_name: name, mcp_server: 'kirocrew-core', tool_call_id: `tc-${ts}` })
const ask = (ts: string) => tool('ask_question', ts)

/** A long turn: the reply that sets up the question sits many tool rows above it. */
const longTurn = (): ChatMessage[] => [
  row('user', 'Earlier ask', 't0'),
  row('assistant', 'An earlier turn reply', 't1', { mid: 'm1' }),
  row('user', 'Review the branch', 't2'),
  row('assistant', 'Two findings. R1 the cache key drops the locale. R2 the retry has no cap.', 't3', { mid: 'm3' }),
  ...Array.from({ length: 12 }, (_, i) => tool('read_file', `t4-${i}`)),
  ask('t5'),
]

describe('questionSubject', () => {
  it('quotes the nearest reply above the ask row, past the tool rows between', () => {
    expect(questionSubject(longTurn())).toMatchObject({ role: 'assistant', ts: 't3', mid: 'm3', text: expect.stringMatching(/^Two findings/) })
  })

  it('skips text written after the ask row', () => {
    const msgs = [...longTurn(), row('assistant', 'Waiting on your answer.', 't6')]
    expect(questionSubject(msgs)?.ts).toBe('t3')
  })

  it('reads above the transcript end for a native card', () => {
    const msgs = [...longTurn(), row('assistant', 'Which file next?', 't6')]
    expect(questionSubject(msgs, true)?.ts).toBe('t6')
  })

  it('never quotes a reply from an earlier turn', () => {
    expect(questionSubject([row('user', 'Earlier', 't0'), row('assistant', 'Old reply', 't1'), row('user', 'Do it', 't2'), ask('t3')])).toBeNull()
  })
})

function mount(messages: ChatMessage[], onJumpToSubject = vi.fn()) {
  const store = createTestStore({
    chat: {
      activeSlot: 'chat-1', messages,
      pendingQuestions: { 'chat-1': { slot: 'chat-1', ask_id: 'ask-1', questions: [{ question: 'Fix R1 first?', options: [{ label: 'Yes' }, { label: 'No' }] }] } },
    },
  } as never)
  renderWithProviders(<PendingQuestionCard slotKey="chat-1" onFallbackSend={vi.fn()} messages={messages} onJumpToSubject={onJumpToSubject} />, { store })
  return onJumpToSubject
}

describe('PendingQuestionCard subject', () => {
  it('shows the subject with the question, and one click asks for the jump to that row', () => {
    const onJump = mount(longTurn())
    const quote = screen.getByTestId('quote-card-sent')
    expect(quote).toHaveTextContent('R1 the cache key drops the locale')
    // Two lines at most, so the quote cannot push the options down: `block` would cancel the clamp.
    const excerpt = within(quote).getByText(/R1 the cache key/)
    expect(excerpt).toHaveClass('line-clamp-2')
    expect(excerpt).not.toHaveClass('block')
    expect(screen.getByText('Fix R1 first?')).toBeInTheDocument()
    fireEvent.click(quote)
    expect(onJump).toHaveBeenCalledWith(expect.objectContaining({ ts: 't3', mid: 'm3' }))
  })

  it('shows no quote when the turn holds no reply text above the question', () => {
    mount([row('user', 'Do it', 't0'), ask('t1')])
    expect(screen.getByText('Fix R1 first?')).toBeInTheDocument()
    expect(screen.queryByTestId('quote-card-sent')).toBeNull()
  })
})
